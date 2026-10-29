"""Taiwan sports-lottery odds from Jbot (https://sportsbot.tech) and the comparison maths (read-only).

    JBOT_TOKEN=... python -m jobs.jbot fetch --dates 2026-10-20 [--mode close] [--max-calls 1]
    python -m jobs.jbot schema  --in data_local/jbot/2026-10-20_close.json
    python -m jobs.jbot compare --raw-dir data_local/jbot --ml data_local/moneylines.jsonl --out report.json
    python -m jobs.jbot analyze --table data_local/jbot_normalised.csv

Rules that come from the provider's terms (as read by the main session on 2026-10-26, not re-verified here):
calls at least 5 seconds apart, a trial key allows 20 calls a day shared by all trial users and only the last
30 days, no automated repetition (this tool is run by hand and has no timer or loop), and the data is for
research and analysis only, so it is saved under data_local/ (git-ignored) and never shown on the site.
The token is read from the JBOT_TOKEN environment variable only; it is never printed, logged or saved.
The response layout used here is the one in the provider's documentation (see normalize_day); `schema`
prints the shape of a real saved response so the two can be checked against each other.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
import pandas as pd

BASE_URL = "https://api.sportsbot.tech/v2/odds"
MIN_INTERVAL = 6.0                 # the provider asks for more than 5 seconds between calls
MODES = ("close", "open", "both", "all")
MIN_GAMES_FOR_HINT = 100


class JbotError(RuntimeError):
    """An error whose message is safe to print (it never contains the token)."""


class RateLimiter:
    def __init__(self, min_interval: float = MIN_INTERVAL):
        self.min_interval = min_interval
        self.last: Optional[float] = None

    def wait(self, clock: Callable[[], float], sleeper: Callable[[float], None]) -> None:
        if self.last is not None:
            need = self.min_interval - (clock() - self.last)
            if need > 0:
                sleeper(need)

    def mark(self, clock: Callable[[], float]) -> None:
        self.last = clock()


_LIMITER = RateLimiter()


def token_from_env(env=None) -> str:
    env = os.environ if env is None else env
    t = (env.get("JBOT_TOKEN") or "").strip()
    if not t:
        raise JbotError("JBOT_TOKEN is not set; export your Jbot key in the environment (never paste it in chat or a file).")
    return t


def request_odds(date: str, mode: str, token: str, sport: str = "NBA", opener=urllib.request.urlopen,
                 sleeper=time.sleep, clock=time.monotonic, limiter: Optional[RateLimiter] = None) -> dict:
    if mode not in MODES:
        raise JbotError(f"mode must be one of {MODES}")
    limiter = limiter or _LIMITER
    url = BASE_URL + "?" + urllib.parse.urlencode({"sport": sport, "date": date, "mode": mode})
    req = urllib.request.Request(url, headers={"X-JBot-Token": token, "Accept": "application/json"})
    limiter.wait(clock, sleeper)
    try:
        with opener(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        raise JbotError(f"HTTP {exc.code} from the odds API") from None      # no message text: it may echo headers
    except Exception as exc:  # noqa: BLE001
        raise JbotError(f"request failed ({type(exc).__name__})") from None
    finally:
        limiter.mark(clock)
    try:
        return json.loads(raw)
    except ValueError:
        raise JbotError("the response was not JSON") from None


def quota_of(data: dict) -> Tuple[Optional[int], Optional[int]]:
    u = (data or {}).get("user") or {}
    return u.get("day_limit"), u.get("day_used")


def schema_of(obj, depth: int = 12):
    """The shape of a JSON value (key names and value types), never the values themselves."""
    if depth <= 0:
        return "..."
    if isinstance(obj, dict):
        return {k: schema_of(v, depth - 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [schema_of(obj[0], depth - 1)] if obj else []
    return type(obj).__name__


def fetch_days(dates: List[str], mode: str, out_dir, token: str, max_calls: int = 1, opener=urllib.request.urlopen,
               sleeper=time.sleep, clock=time.monotonic, limiter: Optional[RateLimiter] = None) -> dict:
    """Save the raw response for each date not already saved; stop at max_calls or when the daily quota is used up."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    calls, stopped = 0, None
    for d in dates:
        path = out_dir / f"{d}_{mode}.json"
        if path.exists():
            continue
        if calls >= max_calls:
            stopped = "max calls"
            break
        data = request_odds(d, mode, token, opener=opener, sleeper=sleeper, clock=clock, limiter=limiter)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        calls += 1
        limit, used = quota_of(data)
        print(f"[OK] {d} {mode}: saved ({'quota ' + str(used) + '/' + str(limit) if limit else 'quota unknown'})", flush=True)
        if limit is not None and used is not None and used >= limit:
            stopped = "daily quota used up"
            break
    return {"calls": calls, "stopped": stopped}


# ----- the documented response: {"status", "user", "data": [game...]} -----
# game: id, time, away, home (Chinese full names), league, odds: [ {update, normal{a,h,s}, handi{"-5.5":{a,h,m,s}},
#       total{"221.5":{o,u,m,s}}, ...} ]   (decimal odds; a = away / over side naming per the provider's docs)
# handi key = the HOME handicap as a string: "-5.5" means the home team gives 5.5, the same sign as ESPN's
# home_spread (negative = home favourite). tests/test_jbot.py locks this.

SIGMA_MARGIN = 12.5      # rough spread of margin - line, only used to turn a line gap into a probability
SIGMA_TOTAL = 19.0       # rough spread of total - line (CONFIG notes 18-20)


def team_abbr_from_cn(name: str) -> Optional[str]:
    """Match a Chinese full name ('奧蘭多魔術') to a team by its nickname (jobs.teams.TEAM_NAME_CH) at the end of
    the name. None when no nickname or more than one matches."""
    from jobs.teams import abbr_from_cn
    try:
        return abbr_from_cn(name)
    except ValueError:
        return None


def _main_key(block: Optional[dict]) -> Optional[str]:
    """The key whose entry has m == true (the main line); None if there is none."""
    for k, v in (block or {}).items():
        if isinstance(v, dict) and v.get("m") is True:
            return k
    return None


def normalize_day(raw: dict) -> pd.DataFrame:
    """One row per game from a saved raw response, using the LAST odds entry (the newest update)."""
    rows = []
    for g in (raw or {}).get("data") or []:
        odds = g.get("odds") or []
        o = odds[-1] if odds else {}
        normal, handi, total = o.get("normal") or {}, o.get("handi") or {}, o.get("total") or {}
        hk, tk = _main_key(handi), _main_key(total)
        rows.append({
            "jbot_id": g.get("id"), "time": g.get("time"), "home_cn": g.get("home"), "away_cn": g.get("away"),
            "home_abbr": team_abbr_from_cn(g.get("home")), "away_abbr": team_abbr_from_cn(g.get("away")),
            "update": o.get("update"), "n_odds": len(odds),
            "tw_ml_home_dec": normal.get("h"), "tw_ml_away_dec": normal.get("a"),
            "tw_spread_line": float(hk) if hk is not None else None,
            "tw_spread_home_dec": handi[hk].get("h") if hk else None,
            "tw_spread_away_dec": handi[hk].get("a") if hk else None,
            "tw_total_line": float(tk) if tk is not None else None,
            "tw_over_dec": total[tk].get("o") if tk else None, "tw_under_dec": total[tk].get("u") if tk else None})
    return pd.DataFrame(rows)


def _phi(z):
    import math
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def spread_ev(tw_line: float, home_dec: float, away_dec: float, market_home_spread: float,
              sigma: float = SIGMA_MARGIN) -> dict:
    """EV of each side of a Taiwan handicap, taking the international line as the truth for the margin:
    expected home margin m = -market_home_spread, margin ~ Normal(m, sigma). Approximate (sigma is rough)."""
    m = -float(market_home_spread)
    p_home = 1.0 - _phi((-float(tw_line) - m) / sigma)
    return {"p_home_cover": p_home, "ev_home": home_dec * p_home - 1.0, "ev_away": away_dec * (1.0 - p_home) - 1.0}


def total_ev(tw_total: float, over_dec: float, under_dec: float, market_total: float,
             sigma: float = SIGMA_TOTAL) -> dict:
    p_over = 1.0 - _phi((float(tw_total) - float(market_total)) / sigma)
    return {"p_over": p_over, "ev_over": over_dec * p_over - 1.0, "ev_under": under_dec * (1.0 - p_over) - 1.0}


def match_games(norm: pd.DataFrame, games: pd.DataFrame) -> Tuple[pd.DataFrame, dict]:
    """Attach ESPN games (game_id, tipoff_utc, home_abbr, away_abbr, date) to the Taiwan rows by the two teams
    and a date within one day. The Taiwan time zone is not documented, so the offset between the Taiwan 'time'
    and ESPN's tipoff (UTC) is MEASURED from the unambiguous matches and reported, not assumed."""
    n = norm.dropna(subset=["home_abbr", "away_abbr"]).copy()
    n["t"] = pd.to_datetime(n["time"], errors="coerce")
    g = games.copy()
    g["tipoff"] = pd.to_datetime(g["tipoff_utc"], utc=True).dt.tz_localize(None)
    cand = n.reset_index().merge(g, on=["home_abbr", "away_abbr"], suffixes=("", "_espn"))
    cand["hours"] = (cand["t"] - cand["tipoff"]).dt.total_seconds() / 3600.0
    cand = cand[cand["hours"].abs() <= 48]
    single = cand.groupby("index").filter(lambda x: len(x) == 1)
    info = {"unambiguous": int(len(single))}
    if single.empty:
        return cand.iloc[0:0], {**info, "offset_hours_median": None}
    off = float(single["hours"].median())
    info.update({"offset_hours_median": off, "offset_hours_iqr": [float(single["hours"].quantile(0.25)),
                                                                  float(single["hours"].quantile(0.75))]})
    cand["err"] = (cand["hours"] - off).abs()
    best = cand.sort_values("err").drop_duplicates("index")
    best = best[best["err"] <= 3.0]
    info["matched"] = int(len(best))
    return best.drop(columns=["t", "tipoff", "err", "index"]).reset_index(drop=True), info


def comparison_table(matched: pd.DataFrame, espn_ml: pd.DataFrame) -> pd.DataFrame:
    """Add the ESPN consensus (median de-vigged home probability and median prices) from jobs.playbook.market."""
    d = matched.merge(espn_ml.rename(columns={"p_home": "p_consensus", "home_dec": "espn_ml_home_dec",
                                              "away_dec": "espn_ml_away_dec"}), on="game_id", how="inner")
    d["espn_home_spread"] = d["home_spread"]
    d["espn_total_line"] = d.get("total_line")
    return d


# ----- comparison maths on a normalised table -----

def overround(dec_a: pd.Series, dec_b: pd.Series) -> pd.Series:
    """Two-way overround: 1/a + 1/b - 1 (the bookmaker's margin)."""
    return 1.0 / dec_a + 1.0 / dec_b - 1.0


def _boot_mean(x: np.ndarray, reps: int, seed: int, level: float = 0.95) -> Tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(x, len(x), replace=True).mean() for _ in range(reps)])
    a = (1 - level) / 2
    return float(x.mean()), float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def _best_side_ev(p_home: np.ndarray, dec_home: np.ndarray, dec_away: np.ndarray) -> np.ndarray:
    return np.maximum(dec_home * p_home - 1.0, dec_away * (1.0 - p_home) - 1.0)


def ev_vs_consensus(d: pd.DataFrame, reps: int = 2000, seed: int = 0) -> dict:
    """Expected value per unit of the better side of each game, taking the international consensus
    probability as the truth. Biased upward (the consensus probability is itself an estimate and we pick the
    better side), so only the Taiwan-minus-international difference and a clearly positive interval mean anything."""
    p = d["p_consensus"].to_numpy(dtype=float)
    out = {}
    for name, (h, a) in {"tw": ("tw_ml_home_dec", "tw_ml_away_dec"),
                         "espn": ("espn_ml_home_dec", "espn_ml_away_dec")}.items():
        ev = _best_side_ev(p, d[h].to_numpy(dtype=float), d[a].to_numpy(dtype=float))
        mean, lo, hi = _boot_mean(ev, reps, seed)
        out[name] = {"n": int(len(ev)), "mean_best_side_ev": mean, "lo": lo, "hi": hi,
                     "share_positive_ev": float((ev > 0).mean())}
    diff = (_best_side_ev(p, d["tw_ml_home_dec"].to_numpy(float), d["tw_ml_away_dec"].to_numpy(float))
            - _best_side_ev(p, d["espn_ml_home_dec"].to_numpy(float), d["espn_ml_away_dec"].to_numpy(float)))
    mean, lo, hi = _boot_mean(diff, reps, seed)
    out["tw_minus_espn"] = {"mean": mean, "lo": lo, "hi": hi}
    out["hint_of_value"] = hint_of_value({"n": out["tw"]["n"], "lo": lo})
    return out


def line_difference(d: pd.DataFrame, tw_col: str, espn_col: str) -> dict:
    diff = (d[tw_col].astype(float) - d[espn_col].astype(float)).dropna()
    return {"n": int(len(diff)), "mean_diff": float(diff.mean()) if len(diff) else None,
            "mean_abs_diff": float(diff.abs().mean()) if len(diff) else None,
            "share_same_line": float((diff.abs() < 1e-9).mean()) if len(diff) else None}


def hint_of_value(s: dict) -> bool:
    """Only a clearly positive interval on enough games counts as a hint; it is never proof."""
    return bool(s.get("n", 0) >= MIN_GAMES_FOR_HINT and s.get("lo", -1.0) > 0)


def compare_report(norm: pd.DataFrame, games: pd.DataFrame, espn_ml: pd.DataFrame, reps: int = 2000,
                   seed: int = 0) -> dict:
    """Taiwan prices against the international market for the same games (small sample by construction)."""
    matched, info = match_games(norm, games)
    out = {"taiwan_rows": int(len(norm)), "matching": info}
    d = comparison_table(matched, espn_ml) if not matched.empty else matched
    out["compared_games"] = int(len(d))
    if d.empty:
        return out
    out["overround"] = {
        "moneyline": _describe(overround(d["tw_ml_home_dec"], d["tw_ml_away_dec"])),
        "spread": _describe(overround(d["tw_spread_home_dec"], d["tw_spread_away_dec"])),
        "total": _describe(overround(d["tw_over_dec"], d["tw_under_dec"])),
        "espn_moneyline_median": _describe(overround(d["espn_ml_home_dec"], d["espn_ml_away_dec"]))}
    out["spread_line_vs_espn"] = line_difference(d, "tw_spread_line", "espn_home_spread")
    out["total_line_vs_espn"] = line_difference(d, "tw_total_line", "espn_total_line")
    out["moneyline_ev_vs_consensus"] = ev_vs_consensus(d.dropna(subset=["tw_ml_home_dec", "tw_ml_away_dec",
                                                                       "p_consensus"]), reps=reps, seed=seed)
    sp = d.dropna(subset=["tw_spread_line", "tw_spread_home_dec", "tw_spread_away_dec", "espn_home_spread"])
    ev = [spread_ev(r.tw_spread_line, r.tw_spread_home_dec, r.tw_spread_away_dec, r.espn_home_spread)
          for r in sp.itertuples()]
    best = np.array([max(e["ev_home"], e["ev_away"]) for e in ev])
    out["spread_best_side_ev_at_market_margin"] = (
        {"n": int(len(best)), "mean": float(best.mean()), "max": float(best.max()), "share_positive": float((best > 0).mean()),
         "note": f"sigma={SIGMA_MARGIN} is a rough assumption; the market line is taken as the truth"} if len(best) else {"n": 0})
    tt = d.dropna(subset=["tw_total_line", "tw_over_dec", "tw_under_dec", "espn_total_line"])
    tev = [total_ev(r.tw_total_line, r.tw_over_dec, r.tw_under_dec, r.espn_total_line) for r in tt.itertuples()]
    tbest = np.array([max(e["ev_over"], e["ev_under"]) for e in tev])
    out["total_best_side_ev_at_market_total"] = (
        {"n": int(len(tbest)), "mean": float(tbest.mean()), "max": float(tbest.max()), "share_positive": float((tbest > 0).mean()),
         "note": f"sigma={SIGMA_TOTAL} is a rough assumption"} if len(tbest) else {"n": 0})
    out["verdict_hint"] = ("no sign that Taiwan prices are cheap enough to beat their overround"
                           if not (out["moneyline_ev_vs_consensus"]["hint_of_value"]
                                   or out["spread_best_side_ev_at_market_margin"].get("mean", -1) > 0
                                   or out["total_best_side_ev_at_market_total"].get("mean", -1) > 0)
                           else "a positive signal in at least one block: look at it by hand, the sample is small")
    return out


def _describe(x: pd.Series) -> dict:
    x = x.dropna()
    return {"n": int(len(x)), "mean": float(x.mean()), "min": float(x.min()), "max": float(x.max())} if len(x) else {"n": 0}


def _compare_from_files(args) -> dict:
    """Needs a read-only DB connection (game ids, tipoffs, closing lines) and the ESPN moneyline file."""
    import re as _re
    from webui.build_site import connect_readonly, make_run
    norm = pd.concat([normalize_day(json.loads(p.read_text(encoding="utf-8")))
                      for p in sorted(Path(args.raw_dir).glob(f"*_{args.mode}.json"))] or [pd.DataFrame()],
                     ignore_index=True)
    if norm.empty:
        raise JbotError("no saved responses found; run fetch first")
    try:
        conn = connect_readonly()
    except Exception as exc:  # noqa: BLE001
        raise JbotError(f"DB connection failed ({type(exc).__name__})") from None
    try:
        run = make_run(conn)
        games = run("/* jb:GAMES */ SELECT g.game_id, g.game_date_et AS date, g.tipoff_utc, g.home_abbr, g.away_abbr, "
                    "l.home_spread::float AS home_spread, l.total_line::float AS total_line "
                    "FROM public.games_v2 g JOIN public.v_closing_lines l ON l.game_id = g.game_id "
                    "WHERE g.tipoff_utc IS NOT NULL AND g.game_date_et >= %s", (str(pd.to_datetime(norm['time']).min().date()),))
    except Exception as exc:  # noqa: BLE001
        msg = _re.sub(r"postgres(?:ql)?://\S+", "[redacted]", str(exc))
        raise JbotError(f"query failed: {type(exc).__name__}: {msg}") from None
    finally:
        conn.close()
    from jobs.playbook import market as _market
    rows = []
    for line in Path(args.ml).read_text(encoding="utf-8").splitlines():
        r = json.loads(line) if line.strip() else None
        m = _market(r["providers"]) if r else None
        if m:
            rows.append({"game_id": r["game_id"], **m})
    return compare_report(norm, games, pd.DataFrame(rows))


# ----- CLI -----

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobs.jbot", description="Jbot Taiwan lottery odds (read-only, manual)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--dates", required=True, help="comma-separated YYYY-MM-DD, within the last 30 days")
    f.add_argument("--mode", default="close", choices=MODES)
    f.add_argument("--out-dir", default="data_local/jbot")
    f.add_argument("--max-calls", type=int, default=1)
    s = sub.add_parser("schema")
    s.add_argument("--in", dest="inp", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--raw-dir", default="data_local/jbot")
    c.add_argument("--ml", default="data_local/moneylines.jsonl", help="ESPN moneylines saved by jobs.playbook fetch")
    c.add_argument("--mode", default="close", choices=MODES)
    c.add_argument("--out")
    a = sub.add_parser("analyze")
    a.add_argument("--table", required=True)
    a.add_argument("--out")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "fetch":
            res = fetch_days([x.strip() for x in args.dates.split(",") if x.strip()], args.mode, args.out_dir,
                             token_from_env(), max_calls=args.max_calls)
            print(json.dumps(res))
        elif args.cmd == "schema":
            print(json.dumps(schema_of(json.loads(Path(args.inp).read_text(encoding="utf-8"))),
                             indent=1, ensure_ascii=False))
        elif args.cmd == "compare":
            result = _compare_from_files(args)
            text = json.dumps(result, indent=1, ensure_ascii=False, default=str)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
            print(text)
        else:
            d = pd.read_csv(args.table)
            result = {"ev": ev_vs_consensus(d)}
            if {"tw_spread_line", "espn_home_spread"} <= set(d.columns):
                result["spread_line"] = line_difference(d, "tw_spread_line", "espn_home_spread")
            text = json.dumps(result, indent=1, ensure_ascii=False)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
            print(text)
    except JbotError as exc:
        print(f"Jbot: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
