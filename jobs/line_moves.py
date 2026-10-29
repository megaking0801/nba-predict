"""C2 step 1: a 50-game feasibility sample of opening vs closing spreads (read-only, GET only).

    python -m jobs.line_moves --out-data data/line-moves-sample.jsonl.gz --out docs/handoff/c2-line-moves-sample.json

Pre-registered in docs/handoff/2026-10-28-c1-result-and-c2-c3-designs.md section 2.1: a fixed-seed sample
of 15 regular-season games from 2023-24, 15 from 2024-25 and 20 from 2015-16..2022-23; for each, the
ESPN core odds providers (trimmed raw blocks, so the file can be re-parsed later) and the parsed opening
and closing home spreads. Continue to the full fetch only if, in each era, the opening spread is found
for >= 80% of games, the spread moved in >= 30% of covered games, and the move's SD is >= 0.5 points.
The DB is only SELECTed (game ids); nothing is written to it. ESPN is only fetched with GET.
"""
from __future__ import annotations

import argparse
import functools
import gzip
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from jobs.espn_http import odds as espn_odds
from jobs.feature_ablation import _scrub
from jobs.ingest_espn import _provider_priority, _spread_num, parse_close_line

SEED = 20261030
SAMPLE = (("recent", ("2023-24", "2023-24"), 15), ("recent", ("2024-25", "2024-25"), 15),
          ("older", ("2015-16", "2022-23"), 20))
MIN_COVERAGE = 0.80
MIN_MOVED = 0.30
MIN_SD = 0.5
POINTS = ("open", "current", "close")

_GAMES = """/* c2:GAMES */
SELECT g.game_id, g.season, g.game_date_et AS date
  FROM public.games_v2 g
 WHERE g.status = 'final' AND g.season_type = 'regular'
 ORDER BY g.game_id
"""


# ----- sampling -----

def sample_games(games: pd.DataFrame, seed: int = SEED) -> pd.DataFrame:
    """The fixed-seed sample: rows of games with an 'era' column, in a stable order."""
    rng = np.random.default_rng(seed)
    parts = []
    for era, (lo, hi), n in SAMPLE:
        pool = games[games["season"].between(lo, hi)].sort_values("game_id")
        pick = rng.choice(len(pool), size=min(n, len(pool)), replace=False)
        parts.append(pool.iloc[np.sort(pick)].assign(era=era))
    return pd.concat(parts, ignore_index=True)


# ----- parsing -----

def trim_items(odds_json: Optional[dict]) -> List[dict]:
    """Keep what a later parse needs: provider name and priority, each side's open/current/close blocks,
    and the provider-level spread / overUnder / open/current/close blocks."""
    out = []
    for prov in (odds_json or {}).get("items") or []:
        keep = {"provider": {k: (prov.get("provider") or {}).get(k) for k in ("id", "name", "priority")}}
        for k in ("spread", "overUnder", "details") + POINTS:
            if prov.get(k) is not None:
                keep[k] = prov[k]
        for side in ("homeTeamOdds", "awayTeamOdds"):
            s = prov.get(side) or {}
            keep[side] = {k: v for k, v in s.items() if k not in ("team", "links", "$ref")}
        out.append(keep)
    return out


def _block_spread(side: dict, point: str) -> Optional[float]:
    b = side.get(point)
    if isinstance(b, dict):
        return _spread_num((b.get("pointSpread") or {}).get("american"))
    return None


def _name(p: dict) -> str:
    return ((p.get("provider") or {}).get("name") or "").strip()


def spreads(items: List[dict]) -> dict:
    """Opening and closing HOME spread for one game, from ONE book where possible.
    1. The best-priority book (not live, not 'Opening') that has both an open block and a close or current
       block: open and close both come from it, so the move is a real move, not a book difference.
    2. Otherwise, older seasons: the 'Opening' provider's spread (its open, current or close block) paired
       with the closing spread chosen the DB's way (ingest_espn.parse_close_line) over the other books.
    A close that is itself only an 'open' block is no close. move = close - open (negative = toward home)."""
    books = sorted((p for p in items if "live" not in _name(p).lower()), key=_provider_priority)
    for p in books:
        home = p.get("homeTeamOdds") or {}
        if _name(p).lower() == "opening":
            continue
        o = _block_spread(home, "open")
        c, point = next(((v, pt) for pt in ("close", "current") if (v := _block_spread(home, pt)) is not None),
                        (None, None))
        if o is not None and c is not None:
            return {"open_spread": o, "open_source": f"{_name(p)}:open", "close_spread": c, "close_point": point,
                    "close_source": _name(p), "move": c - o, "providers": [_name(q) for q in items]}
    open_spread = open_source = None
    for p in books:
        if _name(p).lower() == "opening":
            for point in POINTS:
                v = _block_spread(p.get("homeTeamOdds") or {}, point)
                if v is not None:
                    open_spread, open_source = v, f"Opening:{point}"
                    break
            break
    close = parse_close_line([p for p in books if _name(p).lower() != "opening"])
    close_spread = close["home_spread"] if close and close.get("price_point") != "open" else None
    move = (close_spread - open_spread) if (open_spread is not None and close_spread is not None) else None
    return {"open_spread": open_spread, "open_source": open_source, "close_spread": close_spread,
            "close_point": close.get("price_point") if close else None, "close_source": "db_rule" if close else None,
            "move": move, "providers": [_name(q) for q in items]}


# ----- the check -----

def summarize(records: List[dict]) -> dict:
    out = {"eras": {}, "criteria": {"min_open_coverage": MIN_COVERAGE, "min_share_moved": MIN_MOVED,
                                    "min_move_sd": MIN_SD}}
    ok_all = True
    for era in sorted({r["era"] for r in records}):
        rs = [r for r in records if r["era"] == era]
        fetched = [r for r in rs if r["fetched"]]
        covered = [r for r in fetched if r["move"] is not None]
        moves = [r["move"] for r in covered]
        cov = len(covered) / len(rs) if rs else 0.0
        moved = sum(1 for m in moves if abs(m) > 1e-9) / len(moves) if moves else 0.0
        sd = statistics.stdev(moves) if len(moves) >= 2 else 0.0
        checks = {"open_coverage": cov >= MIN_COVERAGE, "share_moved": moved >= MIN_MOVED, "move_sd": sd >= MIN_SD}
        ok_all &= all(checks.values())
        out["eras"][era] = {"games": len(rs), "fetched": len(fetched), "with_open_and_close": len(covered),
                            "open_coverage": cov, "share_moved": moved, "move_sd": sd,
                            "move_mean": statistics.fmean(moves) if moves else None,
                            "abs_move_median": statistics.median([abs(m) for m in moves]) if moves else None,
                            "open_sources": pd.Series([r["open_source"] for r in covered]).value_counts().to_dict(),
                            "checks": checks}
    out["continue_to_full_fetch"] = bool(ok_all and out["eras"])
    return out


def fetch_sample(sample: pd.DataFrame, out_data: Path, fetch: Callable = functools.partial(espn_odds, retries=1),
                 sleep: float = 0.3) -> List[dict]:
    """GET each sampled game's odds (one try plus at most one retry on an error); write one gzip JSON line
    per game (overwrites)."""
    records = []
    tmp = out_data.with_suffix(out_data.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        for r in sample.itertuples():
            d = fetch(r.game_id)
            items = trim_items(d)
            rec = {"game_id": r.game_id, "season": r.season, "date": str(r.date), "era": r.era,
                   "fetched": d is not None, **spreads(items), "items": items}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            records.append(rec)
            if sleep:
                time.sleep(sleep)
    tmp.replace(out_data)
    return records


def main(argv: Optional[List[str]] = None) -> int:
    from webui.build_site import connect_readonly, make_run
    ap = argparse.ArgumentParser(prog="python -m jobs.line_moves", description="C2 step 1: 50-game line-move sample")
    ap.add_argument("--out-data", required=True, help="gzip JSONL with the trimmed odds (data repo)")
    ap.add_argument("--out", help="the summary JSON")
    args = ap.parse_args(argv)
    try:
        conn = connect_readonly()
    except Exception as exc:  # the message may carry a connection string: print the class only
        print(f"DB connection failed ({type(exc).__name__}); check the DB credentials.")
        return 2
    try:
        games = make_run(conn)(_GAMES, ())
    except Exception as exc:
        print(f"Loading game ids failed: {type(exc).__name__}: {_scrub(str(exc))}")
        return 2
    finally:
        conn.close()
    sample = sample_games(games)
    records = fetch_sample(sample, Path(args.out_data))
    result = {"seed": SEED, "sample_plan": [list(map(str, s)) for s in SAMPLE], "summary": summarize(records),
              "games": [{k: r[k] for k in ("game_id", "season", "era", "fetched", "open_spread", "open_source",
                                           "close_spread", "close_point", "close_source", "move", "providers")}
                        for r in records]}
    text = json.dumps(result, indent=1, default=str, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
