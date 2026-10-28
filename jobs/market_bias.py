"""C1: does the closing line have a fixed error in particular kinds of games? (read-only)

    python -m jobs.market_bias [--out docs/handoff/market-bias-c1.json]

Everything here is fixed in advance (docs/handoff/2026-10-28-unused-data-inventory.md, section C1):
the subset list, the residual definition, the thresholds, the multiple-comparison correction and the
segments. Only the exploration segment is scored; subsets that pass are looked at ONCE on the
validation segment. The confirmation segment (2025-26 onward) is never used by this command.

Residual of a subset's "focus team" (points): focus margin + focus spread (spread from the focus
team's side, negative = favourite). Positive = the focus team beat the line. For totals: total - line
(positive = over). Pushes (residual exactly 0) count as 0 in the mean and are left out of the cover rate.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from jobs import context
from jobs.feature_ablation import _prepare_session, _scrub, block_bootstrap_ci

SEASON_TYPES = ("regular", "playoffs", "playin")
EXPLORE = ("2015-16", "2021-22")
VALIDATE = ("2022-23", "2024-25")
ALPHA = 0.01
SPREAD_INTL, SPREAD_TW = 0.75, 2.24          # points of residual needed to pay -110 / Taiwan 1.75
PROB_INTL, PROB_TW = 0.5238 - 0.5, 1 / 1.75 - 0.5
LONG_TRIP_KM = 2500.0
STREAK = 5
BLOWOUT = 20
EARLY_GP = 10
LATE_LEFT = 10

_LINES = """/* mb:LINES */
SELECT g.game_id, l.home_spread::float AS home_spread, l.total_line::float AS total_line
  FROM public.games_v2 g
  JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final' AND g.margin IS NOT NULL AND g.season_type = ANY(%s)
"""


# ----- point-in-time context (state before each game) -----

def context_rows(bundles: List[dict]) -> pd.DataFrame:
    """One row per regular/playoff/play-in game with schedule facts known before tip-off."""
    games = sorted((b for b in bundles if b.get("season_type") in SEASON_TYPES),
                   key=lambda b: (b["date"], b["game_id"]))
    scheduled = defaultdict(int)                         # regular games per (season, team): the schedule is public
    for b in games:
        if b["season_type"] == "regular":
            scheduled[(b["season"], b["home_abbr"])] += 1
            scheduled[(b["season"], b["away_abbr"])] += 1
    st: Dict[tuple, dict] = {}
    rows = []
    for b in games:
        season, d = b["season"], b["date"]
        neutral = bool(b.get("neutral_site")) or context.is_bubble(d)
        arena = None if neutral else context.arena_key(b["home_abbr"], d)
        row = {"game_id": b["game_id"], "season": season, "season_type": b["season_type"], "date": d,
               "home_abbr": b["home_abbr"], "away_abbr": b["away_abbr"], "neutral": neutral,
               "margin": float(b["margin"]),
               "total": (float(b["home_score"]) + float(b["away_score"]))
               if b.get("home_score") is not None and b.get("away_score") is not None else None}
        for side, abbr in (("home", b["home_abbr"]), ("away", b["away_abbr"])):
            s = st.setdefault((season, abbr), {"last": None, "venue": None, "streak": 0, "last_margin": None,
                                                "gp": 0, "w": 0, "minutes": defaultdict(float), "prev_played": None})
            days = (d - s["last"]).days if s["last"] else None
            row[f"b2b_{side}"] = days == 1
            row[f"rest_{side}"] = min(days - 1, 3) if days else 3
            row[f"streak_{side}"] = s["streak"]
            row[f"last_margin_{side}"] = s["last_margin"]
            row[f"gp_{side}"] = s["gp"]
            row[f"winpct_{side}"] = s["w"] / s["gp"] if s["gp"] else 0.5
            row[f"left_{side}"] = scheduled[(season, abbr)] - s["gp"] if b["season_type"] == "regular" else None
            if side == "away":
                prev = s["venue"]
                row["travel_away_km"] = context.distance_km(prev, arena) if (prev and arena) else None
                row["tz_shift_away"] = context.tz_shift_hours(prev, arena, d) if (prev and arena) else None
            # empty prev_played = the previous box score was a placeholder: no claim either way
            top2 = [p for p, _ in sorted(s["minutes"].items(), key=lambda kv: -kv[1])[:2]]
            row[f"star_out_prev_{side}"] = bool(s["gp"] >= 5 and s["prev_played"] and top2
                                                and any(p not in s["prev_played"] for p in top2))
        # update after the row is complete (state only ever comes from earlier games)
        played = defaultdict(set)
        for p in b.get("player_stats") or []:
            if (p.get("min_played") or 0) > 0:
                played[p.get("team_abbr")].add(p.get("player_id"))
        for side, abbr, m in (("home", b["home_abbr"], row["margin"]), ("away", b["away_abbr"], -row["margin"])):
            s = st[(season, abbr)]
            s["last"], s["last_margin"] = d, m
            if arena:
                s["venue"] = arena
            s["streak"] = (s["streak"] + 1 if s["streak"] >= 0 else 1) if m > 0 else (s["streak"] - 1 if s["streak"] <= 0 else -1)
            if b["season_type"] == "regular":
                s["gp"] += 1
                s["w"] += int(m > 0)
            for p in b.get("player_stats") or []:
                if p.get("team_abbr") == abbr and (p.get("min_played") or 0) > 0:
                    s["minutes"][p.get("player_id")] += float(p["min_played"])
            s["prev_played"] = played.get(abbr, set())
        rows.append(row)
    return pd.DataFrame(rows)


# ----- the pre-registered subsets -----

def _spread_resid(df: pd.DataFrame) -> pd.Series:
    return df["margin"] + df["home_spread"]                # home side; away side is the negative


def subsets(df: pd.DataFrame) -> List[dict]:
    """(name, kind, mask, sign): sign +1 = focus is home (or over), -1 = focus is away (or under)."""
    one = pd.Series(1.0, index=df.index)
    hs = df["home_spread"]
    fav_home = np.where(hs <= 0, 1.0, -1.0)
    def team_sign(cond_home, cond_away):                   # focus = the team meeting the condition (home first)
        return pd.Series(np.where(cond_home, 1.0, np.where(cond_away, -1.0, 0.0)), index=df.index)
    reg = df["season_type"] == "regular"
    rest_diff = df["rest_home"] - df["rest_away"]
    out = [
        ("away_b2b", "spread", df["b2b_away"] & ~df["b2b_home"], -one),
        ("home_b2b", "spread", df["b2b_home"] & ~df["b2b_away"], one),
        ("both_b2b", "spread", df["b2b_home"] & df["b2b_away"], one),
        ("rest_edge_2plus", "spread", rest_diff.abs() >= 2, pd.Series(np.sign(rest_diff), index=df.index)),
        ("away_long_trip", "spread", df["travel_away_km"].fillna(0) > LONG_TRIP_KM, -one),
        ("away_east_2tz", "spread", df["tz_shift_away"].fillna(0) >= 2, -one),
        ("big_favorite_10", "spread", hs.abs() >= 10, pd.Series(fav_home, index=df.index)),
        ("small_spread_3", "spread", hs.abs() <= 3, one),
        ("home_underdog", "spread", hs > 0, one),
        ("playoffs", "spread", df["season_type"] == "playoffs", one),
        ("playin", "spread", df["season_type"] == "playin", one),
        ("neutral_site", "spread", df["neutral"], one),
        ("win_streak_5", "spread", (df["streak_home"] >= STREAK) | (df["streak_away"] >= STREAK),
         team_sign(df["streak_home"] >= STREAK, df["streak_away"] >= STREAK)),
        ("lose_streak_5", "spread", (df["streak_home"] <= -STREAK) | (df["streak_away"] <= -STREAK),
         team_sign(df["streak_home"] <= -STREAK, df["streak_away"] <= -STREAK)),
        ("after_blowout_loss_20", "spread", (df["last_margin_home"].fillna(0) <= -BLOWOUT) | (df["last_margin_away"].fillna(0) <= -BLOWOUT),
         team_sign(df["last_margin_home"].fillna(0) <= -BLOWOUT, df["last_margin_away"].fillna(0) <= -BLOWOUT)),
        ("after_blowout_win_20", "spread", (df["last_margin_home"].fillna(0) >= BLOWOUT) | (df["last_margin_away"].fillna(0) >= BLOWOUT),
         team_sign(df["last_margin_home"].fillna(0) >= BLOWOUT, df["last_margin_away"].fillna(0) >= BLOWOUT)),
        ("early_season", "spread", reg & (df["gp_home"] <= EARLY_GP) & (df["gp_away"] <= EARLY_GP), one),
        ("late_season_worse_team", "spread", reg & ((df["left_home"].fillna(99) <= LATE_LEFT) | (df["left_away"].fillna(99) <= LATE_LEFT)),
         pd.Series(np.where(df["winpct_home"] <= df["winpct_away"], 1.0, -1.0), index=df.index)),
        ("star_out_prev_game", "spread", df["star_out_prev_home"] | df["star_out_prev_away"],
         team_sign(df["star_out_prev_home"], df["star_out_prev_away"])),
        ("key_number_3_or_7", "spread", hs.abs().isin([3.0, 7.0]), pd.Series(fav_home, index=df.index)),
        ("around_key_numbers", "spread", hs.abs().isin([2.5, 3.5, 6.5, 7.5]), pd.Series(fav_home, index=df.index)),
        ("high_total_235", "total", df["total_line"] >= 235, one),
        ("low_total_205", "total", df["total_line"] <= 205, one),
        ("early_season_total", "total", reg & (df["gp_home"] <= EARLY_GP) & (df["gp_away"] <= EARLY_GP), one),
    ]
    # Not tested: national TV games (games_v2 has no broadcast column; scoreboard 'broadcasts' was never stored).
    res = []
    for name, kind, mask, sign in out:
        m = mask.fillna(False).astype(bool) & (sign != 0)
        if kind == "spread":
            m &= df["home_spread"].notna()
        else:
            m &= df["total_line"].notna() & df["total"].notna() & df["total_line"].between(170, 280)
        res.append({"name": name, "kind": kind, "mask": m, "sign": sign})
    return res


def thresholds(kind: str, sigma_total: Optional[float]) -> Dict[str, float]:
    """Residual (points) needed to pay the margin; totals convert through their own spread of residuals."""
    if kind == "spread":
        return {"intl": SPREAD_INTL, "taiwan": SPREAD_TW}
    per_point = NormalDist().pdf(0) / sigma_total
    return {"intl": PROB_INTL / per_point, "taiwan": PROB_TW / per_point}


def test_subset(r: pd.Series, dates: pd.Series, seasons: pd.Series, level: float, reps: int, seed: int) -> dict:
    d = pd.DataFrame({"date": dates.to_numpy(), "d": r.to_numpy(dtype=float)})
    if len(d) < 30:
        return {"n": int(len(d)), "too_small": True}
    mean, lo, hi = block_bootstrap_ci(d, reps=reps, seed=seed, level=level)
    nonpush = r[r.abs() > 1e-9]
    by = pd.DataFrame({"s": seasons.to_numpy(), "r": r.to_numpy()}).groupby("s")["r"].mean()
    return {"n": int(len(r)), "n_push": int((r.abs() <= 1e-9).sum()), "mean_resid": mean, "lo": lo, "hi": hi,
            "cover_rate": float((nonpush > 0).mean()) if len(nonpush) else None,
            "per_season": {str(k): float(v) for k, v in by.items()},
            "share_seasons_same_sign": float((np.sign(by) == np.sign(mean)).mean()) if mean else 0.0}


def run(ctx: pd.DataFrame, lines: pd.DataFrame, reps: int = 2000, seed: int = 0) -> dict:
    df = ctx.merge(lines, on="game_id", how="inner")
    sub = subsets(df)
    k = len(sub)
    level = 1 - ALPHA / k
    explore = df["season"].between(*EXPLORE)
    validate = df["season"].between(*VALIDATE)
    tot = df["total"] - df["total_line"]
    sigma_total = float(tot[explore & df["total_line"].between(170, 280)].std())
    out = {"tests": k, "level": level, "explore": EXPLORE, "validate": VALIDATE, "sigma_total_explore": sigma_total,
           "not_tested": ["national TV games: no broadcast data stored"], "subsets": {}}
    for s in sub:
        base = _spread_resid(df) if s["kind"] == "spread" else tot
        r = base * s["sign"]
        th = thresholds(s["kind"], sigma_total)
        m = s["mask"] & explore
        res = test_subset(r[m], df.loc[m, "date"], df.loc[m, "season"], level, reps, seed)
        res["thresholds"] = th
        res["passes_explore"] = bool(not res.get("too_small") and (res["lo"] > 0 or res["hi"] < 0)
                                     and abs(res["mean_resid"]) >= th["intl"])
        res["pays_taiwan_explore"] = bool(res["passes_explore"] and abs(res["mean_resid"]) >= th["taiwan"])
        out["subsets"][s["name"]] = {"kind": s["kind"], "explore": res}
    passing = [n for n, v in out["subsets"].items() if v["explore"]["passes_explore"]]
    out["passing_explore"] = passing
    if passing:                                            # one look at the validation segment, corrected for the looks
        vlevel = 1 - ALPHA / len(passing)
        for s in sub:
            if s["name"] not in passing:
                continue
            base = _spread_resid(df) if s["kind"] == "spread" else tot
            r = base * s["sign"]
            m = s["mask"] & validate
            v = test_subset(r[m], df.loc[m, "date"], df.loc[m, "season"], vlevel, reps, seed)
            e = out["subsets"][s["name"]]["explore"]
            v["replicates"] = bool(not v.get("too_small") and np.sign(v["mean_resid"]) == np.sign(e["mean_resid"])
                                   and (v["lo"] > 0 or v["hi"] < 0))
            out["subsets"][s["name"]]["validate"] = v
    out["replicated"] = [n for n, v in out["subsets"].items() if v.get("validate", {}).get("replicates")]
    return out


def main(argv: Optional[List[str]] = None) -> int:
    from jobs.features import load_bundles
    from webui.build_site import connect_readonly, make_run
    ap = argparse.ArgumentParser(prog="python -m jobs.market_bias", description="C1 subset bias check (read-only)")
    ap.add_argument("--out")
    ap.add_argument("--reps", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    try:
        conn = connect_readonly()
    except Exception as exc:  # the message may carry a connection string: print the class only
        print(f"DB connection failed ({type(exc).__name__}); check the DB credentials.")
        return 2
    try:
        _prepare_session(conn)
        lines = make_run(conn)(_LINES, (list(SEASON_TYPES),))
        bundles = load_bundles(conn)
    except Exception as exc:
        print(f"Loading data failed: {type(exc).__name__}: {_scrub(str(exc))}")
        return 2
    finally:
        conn.close()
    result = run(context_rows(bundles), lines, reps=args.reps, seed=args.seed)
    text = json.dumps(result, indent=1, default=str, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
