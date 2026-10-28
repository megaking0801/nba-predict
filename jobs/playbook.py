"""Playbook search (read-only): does any bet type have a positive backtested ROI?

    python -m jobs.playbook fetch     --out data/moneylines.jsonl.gz [--seasons 2023-24,...] [--limit N]
    python -m jobs.playbook moneyline --in  data/moneylines.jsonl.gz [--out report.json]
    python -m jobs.playbook totals    [--out report.json]

A path ending in .gz is read and written gzip-compressed (about 25x smaller), so fetched data can be
committed under data/ and a fresh clone never has to fetch it again.
fetch only GETs ESPN's public core odds API and SELECTs game ids; nothing is written to the database.
The rules (providers, thresholds, pass condition) are fixed in
docs/superpowers/specs/2026-10-26-playbook-search-design.md and in the constants below.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
import time
import zlib
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from jobs.config import CONFIG
from jobs.espn_http import odds as espn_odds
from jobs.evaluate import walk_forward
from jobs.feature_ablation import _prepare_session, block_bootstrap_ci
from jobs.features import FEATURE_NAMES, TOTAL_FEATURE_NAMES, build_feature_table, load_bundles
from webui.build_site import connect_readonly, make_run

Run = Callable[[str, tuple], pd.DataFrame]

SEASON_TYPES = ("regular", "playoffs", "playin")
EDGES = (0.02, 0.04, 0.06)           # win-probability gap needed to bet (fixed in advance)
GAPS = (1.0, 2.0, 3.0)               # total-points gap needed to bet (fixed in advance)
PRICES = {"intl_-110": 1 + 100 / 110, "taiwan_est_1.75": 1.75}
LEVEL = 0.992                        # three thresholds -> Bonferroni on a 99% interval
MIN_BETS = 1000
SEASON_SHARE = 2 / 3
ML_RANGE = (1.01, 15.0)
POINTS = ("close", "current", "open")
EXCLUDED_NAMES = {"consensus", "opening"}

_OUTCOMES = """/* pb:OUT */
SELECT g.game_id, g.season, g.season_type, g.game_date_et AS date, g.margin
  FROM public.games_v2 g
 WHERE g.status = 'final' AND g.margin IS NOT NULL AND g.season_type = ANY(%s)
"""

_IDS = """/* pb:IDS */
SELECT g.game_id FROM public.games_v2 g
 WHERE g.status = 'final' AND g.margin IS NOT NULL AND g.season_type = ANY(%s)
 ORDER BY g.game_date_et, g.game_id
"""

_TOTALS = """/* pb:TOT */
SELECT g.game_id, g.season, g.season_type, g.game_date_et AS date,
       (g.home_score + g.away_score)::float AS total, l.total_line::float AS total_line
  FROM public.games_v2 g
  JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final' AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
   AND g.season_type = ANY(%s) AND l.total_line BETWEEN 170 AND 280
"""


# ----- prices -----

def american_to_decimal(a) -> Optional[float]:
    try:
        v = float(str(a).strip().replace("+", ""))
    except (TypeError, ValueError):
        return None
    if v == 0 or math.isnan(v):
        return None
    return 1.0 + v / 100.0 if v > 0 else 1.0 + 100.0 / abs(v)


def _dec(v) -> Optional[float]:
    """A price block ({american, decimal}) or a bare American number -> decimal odds."""
    if isinstance(v, dict):
        if v.get("decimal") is not None:
            try:
                return float(v["decimal"])
            except (TypeError, ValueError):
                return None
        return american_to_decimal(v.get("american"))
    return american_to_decimal(v)


def _side_price(side: dict, block_key: str, flat_key: str) -> Tuple[Optional[float], Optional[str]]:
    for point in POINTS:
        b = side.get(point)
        if isinstance(b, dict) and b.get(block_key) is not None:
            d = _dec(b[block_key])
            if d:
                return d, point
    d = american_to_decimal(side.get(flat_key))
    return (d, "flat") if d else (None, None)


def _total_price(prov: dict, key: str, flat_key: str) -> Optional[float]:
    for point in POINTS:
        b = prov.get(point)
        if isinstance(b, dict) and b.get(key) is not None:
            d = _dec(b[key])
            if d:
                return d
    return american_to_decimal(prov.get(flat_key))


def parse_providers(odds_json: Optional[dict]) -> List[dict]:
    """One dict per provider: moneylines, spread prices, total prices (decimal odds), and a live flag."""
    out = []
    for prov in (odds_json or {}).get("items") or []:
        name = (prov.get("provider") or {}).get("name") or ""
        home, away = prov.get("homeTeamOdds") or {}, prov.get("awayTeamOdds") or {}
        hml, hpoint = _side_price(home, "moneyLine", "moneyLine")
        aml, _ = _side_price(away, "moneyLine", "moneyLine")
        hsp, _ = _side_price(home, "spread", "spreadOdds")
        asp, _ = _side_price(away, "spread", "spreadOdds")
        out.append({"name": name, "priority": (prov.get("provider") or {}).get("priority"),
                    "live": "live" in name.lower(), "home_ml": hml, "away_ml": aml, "ml_point": hpoint,
                    "home_spread_odds": hsp, "away_spread_odds": asp,
                    "over_odds": _total_price(prov, "over", "overOdds"),
                    "under_odds": _total_price(prov, "under", "underOdds")})
    return out


def eligible(p: dict) -> bool:
    if p.get("live") or (p.get("name") or "").strip().lower() in EXCLUDED_NAMES:
        return False
    h, a = p.get("home_ml"), p.get("away_ml")
    if h is None or a is None:
        return False
    return ML_RANGE[0] <= h <= ML_RANGE[1] and ML_RANGE[0] <= a <= ML_RANGE[1]


def market(providers: List[dict]) -> Optional[dict]:
    """Median de-vigged home probability and median / best prices over the eligible providers."""
    el = [p for p in providers if eligible(p)]
    if not el:
        return None
    ph = [(1 / p["home_ml"]) / (1 / p["home_ml"] + 1 / p["away_ml"]) for p in el]
    hs, as_ = [p["home_ml"] for p in el], [p["away_ml"] for p in el]
    return {"n": len(el), "p_home": float(np.median(ph)), "home_dec": float(np.median(hs)),
            "away_dec": float(np.median(as_)), "home_dec_best": float(max(hs)), "away_dec_best": float(max(as_))}


# ----- ROI -----

def roi_summary(bets: pd.DataFrame, reps: int = 2000, seed: int = 0, level: float = 0.99) -> dict:
    """bets: date, season, won, dec, push. Profit per unit stake; pushes are refunded."""
    if bets is None or bets.empty:
        return {"n": 0}
    won = bets["won"].to_numpy(dtype=bool)
    push = bets["push"].to_numpy(dtype=bool) if "push" in bets else np.zeros(len(bets), dtype=bool)
    profit = np.where(push, 0.0, np.where(won, bets["dec"].to_numpy(dtype=float) - 1.0, -1.0))
    d = pd.DataFrame({"date": bets["date"].to_numpy(), "d": profit})
    mean, lo, hi = block_bootstrap_ci(d, reps=reps, seed=seed, level=level)
    by = pd.DataFrame({"season": bets["season"].to_numpy(), "p": profit}).groupby("season")["p"].mean()
    return {"n": int(len(bets)), "hit_rate": float((won & ~push).mean()), "roi": float(mean), "lo": lo, "hi": hi,
            "per_season": {str(k): float(v) for k, v in by.items()},
            "share_seasons_positive": float((by > 0).mean())}


def passes(s: dict) -> bool:
    """The pre-registered pass rule: enough bets, interval above zero, positive in 2/3 of the seasons."""
    return bool(s.get("n", 0) >= MIN_BETS and s.get("lo", -1.0) > 0
                and s.get("share_seasons_positive", 0.0) >= SEASON_SHARE - 1e-9)


def _side_bets(g: pd.DataFrame, home_mask: pd.Series, away_mask: pd.Series) -> pd.DataFrame:
    h, a = g[home_mask], g[away_mask]
    home = pd.DataFrame({"game_id": h["game_id"], "date": h["date"], "season": h["season"], "side": "home",
                         "won": h["home_won"].astype(bool), "dec": h["home_dec"], "push": False})
    away = pd.DataFrame({"game_id": a["game_id"], "date": a["date"], "season": a["season"], "side": "away",
                         "won": ~a["home_won"].astype(bool), "dec": a["away_dec"], "push": False})
    return pd.concat([home, away]).sort_index().reset_index(drop=True)


def baseline_roi(g: pd.DataFrame, reps: int = 2000, seed: int = 0) -> dict:
    """Strategies that use no model: they show what the price itself costs."""
    fav_home = g["p_market"] >= 0.5
    everyone = pd.Series(True, index=g.index)
    none = pd.Series(False, index=g.index)
    plans = {"favorite": (fav_home, ~fav_home), "underdog": (~fav_home, fav_home),
             "home": (everyone, none), "away": (none, everyone)}
    return {name: roi_summary(_side_bets(g, hm, am), reps=reps, seed=seed) for name, (hm, am) in plans.items()}


def model_bets(g: pd.DataFrame, edge: float) -> pd.DataFrame:
    d = g["p_model"] - g["p_market"]
    return _side_bets(g, d >= edge, d <= -edge)


def model_roi(g: pd.DataFrame, edges=EDGES, reps: int = 2000, seed: int = 0, level: float = LEVEL) -> dict:
    out = {}
    for e in edges:
        s = roi_summary(model_bets(g, e), reps=reps, seed=seed, level=level)
        s["passes"] = passes(s)
        out[e] = s
    return out


def total_bets(t: pd.DataFrame, gap: float, dec: float) -> pd.DataFrame:
    """Over when the model total is at least ``gap`` above the line, under when at least ``gap`` below."""
    d = t["mu"] - t["total_line"]
    over, under = t[d >= gap], t[d <= -gap]
    def build(x, side):
        return pd.DataFrame({"game_id": x["game_id"], "date": x["date"], "season": x["season"], "side": side,
                             "won": (x["total"] > x["total_line"]) if side == "over" else (x["total"] < x["total_line"]),
                             "dec": dec, "push": x["total"] == x["total_line"]})
    return pd.concat([build(over, "over"), build(under, "under")]).sort_index().reset_index(drop=True)


# ----- fetching -----

def _open_text(path, mode: str):
    path = Path(path)
    if path.suffix == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def read_lines(path):
    """Yield the non-empty lines of a plain or .gz file; a run cut short mid-block keeps what was readable."""
    try:
        with _open_text(path, "r") as fh:
            for line in fh:
                if line.strip():
                    yield line
    except (EOFError, gzip.BadGzipFile, zlib.error):
        return

def fetch_moneylines(run: Run, out_path, fetch: Callable = espn_odds, sleep: float = 0.15,
                     limit: Optional[int] = None, seasons: Optional[List[str]] = None) -> int:
    """Append one JSON line per final game: its parsed providers. Resumable, polite, read-only."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_path.exists():
        kept = list(read_lines(out_path))
        done = {json.loads(line)["game_id"] for line in kept}
        if out_path.suffix == ".gz":     # a cut-short gzip tail would corrupt what we append: rewrite it clean first
            tmp = out_path.with_name(out_path.name + ".tmp.gz")
            with _open_text(tmp, "w") as fh:
                fh.writelines(kept)
            tmp.replace(out_path)        # atomic: an interrupted rewrite never loses the fetched rows
    ids = [str(x) for x in run(_IDS, (list(SEASON_TYPES),))["game_id"]]
    if seasons:      # season filter is applied on the id list through the outcomes query
        keep = set(run(_OUTCOMES, (list(SEASON_TYPES),)).query("season in @seasons")["game_id"].astype(str))
        ids = [i for i in ids if i in keep]
    todo = [i for i in ids if i not in done]
    if limit is not None:
        todo = todo[:limit]
    n = 0
    with _open_text(out_path, "a") as fh:
        for gid in todo:
            d = fetch(gid)
            fh.write(json.dumps({"game_id": gid, "providers": parse_providers(d)}, ensure_ascii=False) + "\n")
            fh.flush()
            n += 1
            if n % 200 == 0:
                print(f"[..] fetched {n}/{len(todo)}", flush=True)
            time.sleep(sleep)
    print(f"[OK] fetched {n} games into {out_path} ({len(done)} were already there)", flush=True)
    return n


# ----- reports -----

def _phi(z):
    return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(z, dtype=float) / math.sqrt(2.0)))


def _model_preds(bundles: list, kind_features, target: str) -> pd.DataFrame:
    df = build_feature_table(bundles, CONFIG)
    return walk_forward(df, "ridge", cfg=CONFIG, features=kind_features, target=target)


def load_market_games(path, outcomes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for line in read_lines(path):
        r = json.loads(line)
        m = market(r["providers"])
        if m:
            rows.append({"game_id": r["game_id"], **m})
    mk = pd.DataFrame(rows)
    if mk.empty:
        return mk
    g = outcomes.merge(mk, on="game_id")
    g["home_won"] = g["margin"] > 0
    g["p_market"] = g["p_home"]
    return g


def moneyline_report(run: Run, bundles: list, path, reps: int = 2000, seed: int = 0) -> dict:
    outcomes = run(_OUTCOMES, (list(SEASON_TYPES),))
    g = load_market_games(path, outcomes)
    out = {"outcomes": int(len(outcomes)), "with_market": int(len(g))}
    if g.empty:
        return out
    out["providers_per_game"] = {str(k): float(v) for k, v in g.groupby("season")["n"].mean().items()}
    out["baselines"] = baseline_roi(g, reps=reps, seed=seed)
    preds = _model_preds(bundles, FEATURE_NAMES, "margin")
    preds["p_model"] = _phi(preds["mu"] / preds["sigma"])
    gm = g.merge(preds[["game_id", "p_model"]], on="game_id")
    out["model_games"] = int(len(gm))
    out["model_median_price"] = model_roi(gm, reps=reps, seed=seed)
    best = gm.assign(home_dec=gm["home_dec_best"], away_dec=gm["away_dec_best"])
    out["model_best_price_upper_bound"] = model_roi(best, reps=reps, seed=seed)
    out["any_passes"] = bool(any(s.get("passes") for s in out["model_median_price"].values()))
    return out


def totals_report(run: Run, bundles: list, reps: int = 2000, seed: int = 0) -> dict:
    t = run(_TOTALS, (list(SEASON_TYPES),))
    out = {"games_with_total_line": int(len(t))}
    if t.empty:
        return out
    preds = _model_preds(bundles, TOTAL_FEATURE_NAMES, "total")
    tm = t.merge(preds[["game_id", "mu"]], on="game_id")
    out["model_games"] = int(len(tm))
    out["baselines"], out["model"] = {}, {}
    for pname, dec in PRICES.items():
        for side in ("over", "under"):
            won = tm["total"] > tm["total_line"] if side == "over" else tm["total"] < tm["total_line"]
            bets = pd.DataFrame({"date": tm["date"], "season": tm["season"], "won": won, "dec": dec,
                                 "push": tm["total"] == tm["total_line"]})
            out["baselines"][f"always_{side}@{pname}"] = roi_summary(bets, reps=reps, seed=seed)
        out["model"][pname] = {}
        for gap in GAPS:
            s = roi_summary(total_bets(tm, gap, dec), reps=reps, seed=seed, level=LEVEL)
            s["passes"] = passes(s)
            out["model"][pname][str(gap)] = s
    out["any_passes"] = bool(any(s.get("passes") for d in out["model"].values() for s in d.values()))
    return out


# ----- CLI -----

def _scrub(text: str) -> str:
    return re.sub(r"postgres(?:ql)?://\S+", "[redacted]", text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobs.playbook", description="Playbook search (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", required=True)
    f.add_argument("--seasons")
    f.add_argument("--limit", type=int)
    f.add_argument("--sleep", type=float, default=0.15)
    m = sub.add_parser("moneyline")
    m.add_argument("--in", dest="inp", required=True)
    t = sub.add_parser("totals")
    for p in (m, t):
        p.add_argument("--out")
        p.add_argument("--reps", type=int, default=2000)
        p.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    try:
        conn = connect_readonly()
    except Exception as exc:  # the message may carry a connection string: print the class only
        print(f"DB connection failed ({type(exc).__name__}); check the DB credentials.")
        return 2
    try:
        _prepare_session(conn)
        run = make_run(conn)
        if args.cmd == "fetch":
            seasons = [s.strip() for s in args.seasons.split(",")] if args.seasons else None
            fetch_moneylines(run, args.out, sleep=args.sleep, limit=args.limit, seasons=seasons)
            return 0
        bundles = load_bundles(conn)
        result = (moneyline_report(run, bundles, args.inp, reps=args.reps, seed=args.seed) if args.cmd == "moneyline"
                  else totals_report(run, bundles, reps=args.reps, seed=args.seed))
    except Exception as exc:
        print(f"Playbook failed: {type(exc).__name__}: {_scrub(str(exc))}")
        return 2
    finally:
        conn.close()
    text = json.dumps(result, indent=1, default=str, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
