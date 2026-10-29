"""C3 step 1: do individual referees have a persistent tilt on the total residual? (read-only)

    python -m jobs.referee_persistence [--out docs/handoff/c3-referee-persistence.json]

For each referee, his explore-segment games (2015-16..2021-22, regular season) are split by date into
alternating halves A and B; the Spearman correlation across referees of the mean total residual in A and
in B measures persistence. Pre-registered in docs/handoff/2026-10-28-c1-result-and-c2-c3-designs.md
section 3.1, tightened by docs/handoff/2026-10-30-c3-referee-plan.md (game-level cluster bootstrap,
crews permuted within season x home team, team-adjusted version). Only SELECTs; never writes the DB.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from jobs.feature_ablation import _prepare_session, _scrub

EXPLORE = ("2015-16", "2021-22")
MIN_HALF = 40
RHO_MIN = 0.2
LEVEL = 0.99
BUBBLE_START = dt.date(2020, 7, 30)
LINE_RANGE = (170.0, 280.0)

_GAMES = """/* c3:GAMES */
SELECT g.game_id, g.season, g.game_date_et AS date, g.home_abbr, g.away_abbr,
       (g.home_score + g.away_score)::float AS total,
       l.total_line::float AS total_line, l.book, l.total_price_point
  FROM public.games_v2 g LEFT JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final' AND g.season_type = 'regular' AND g.season BETWEEN %s AND %s
   AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
"""
_OFFICIALS = """/* c3:OFFICIALS */
SELECT o.game_id, o.official_order, o.official_name
  FROM public.game_officials o JOIN public.games_v2 g ON g.game_id = o.game_id
 WHERE g.season_type = 'regular' AND g.season BETWEEN %s AND %s
"""
_MINUTES = """/* c3:MINUTES */
SELECT t.game_id, MIN(t.min) AS min_min, MAX(t.min) AS max_min, COUNT(*) AS n_teams
  FROM public.team_game_stats t JOIN public.games_v2 g ON g.game_id = t.game_id
 WHERE g.season_type = 'regular' AND g.season BETWEEN %s AND %s
 GROUP BY t.game_id
"""


# ----- tables -----

def residual_table(games: pd.DataFrame, minutes: Optional[pd.DataFrame] = None) -> tuple[pd.DataFrame, dict]:
    """Games with a usable closing total and residual columns:
    r (raw), r_c (centred within season x total_price_point x bubble), r_team (r_c minus each team's
    leave-one-out season mean of r_c), no_ot (both teams played exactly 240 minutes)."""
    counts = {"games_final_regular": int(len(games))}
    g = games.copy()
    g["date"] = pd.to_datetime(g["date"]).dt.date
    has_line = g["total_line"].notna()
    in_range = g["total_line"].between(*LINE_RANGE)
    counts["no_total_line"] = int((~has_line).sum())
    counts["line_outside_170_280"] = int((has_line & ~in_range).sum())
    g = g[in_range].copy()
    g["r"] = g["total"] - g["total_line"]
    g["bubble"] = g["date"] >= BUBBLE_START
    g["pp"] = g["total_price_point"].fillna("unknown")
    g["r_c"] = g["r"] - g.groupby(["season", "pp", "bubble"])["r"].transform("mean")
    long = pd.concat([g[["game_id", "season", "home_abbr", "r_c"]].rename(columns={"home_abbr": "team"}),
                      g[["game_id", "season", "away_abbr", "r_c"]].rename(columns={"away_abbr": "team"})])
    agg = long.groupby(["season", "team"])["r_c"].agg(["sum", "count"])

    def loo(team_col):
        a = agg.reindex(pd.MultiIndex.from_arrays([g["season"], g[team_col]])).to_numpy()
        return np.where(a[:, 1] > 1, (a[:, 0] - g["r_c"].to_numpy()) / np.maximum(a[:, 1] - 1, 1), 0.0)
    g["r_team"] = g["r_c"] - loo("home_abbr") - loo("away_abbr")
    if minutes is not None and len(minutes):
        m = minutes.set_index("game_id")
        ok = (m["n_teams"] == 2) & (m["min_min"] == 240) & (m["max_min"] == 240)
        g["no_ot"] = g["game_id"].map(ok).fillna(False).astype(bool)
    else:
        g["no_ot"] = False
    counts["games_used"] = int(len(g))
    counts["games_no_ot"] = int(g["no_ot"].sum())
    return g.sort_values(["date", "game_id"]).reset_index(drop=True), counts


def crews(officials: pd.DataFrame) -> tuple[Dict[str, tuple], dict]:
    """game_id -> the on-court crew (official_order 1-3, names trimmed, not merged)."""
    o = officials.copy()
    o["official_name"] = o["official_name"].astype(str).str.strip()
    info = {"rows": int(len(o)), "rows_order_above_3": int((o["official_order"] > 3).sum())}
    o = o[o["official_order"].between(1, 3)].sort_values(["game_id", "official_order"])
    out = {gid: tuple(grp["official_name"]) for gid, grp in o.groupby("game_id")}
    names = sorted(set(o["official_name"]))
    key = {n: re.sub(r"[^a-z]", "", n.casefold()) for n in names}
    groups: Dict[str, List[str]] = {}
    for n, k in key.items():
        groups.setdefault(k, []).append(n)
    info["names"] = len(names)
    info["names_differing_only_in_case_or_punctuation"] = [v for v in groups.values() if len(v) > 1]
    return out, info


def pairs(g: pd.DataFrame, crew_of: Dict[str, tuple], col: str) -> pd.DataFrame:
    """One row per (referee, game) with the game's residual, the referee's half (A = 1st, 3rd, ... game by
    date) and the game's row position."""
    rows = [(ref, i, gid, d) for i, (gid, d) in enumerate(zip(g["game_id"], g["date"])) for ref in crew_of.get(gid, ())]
    p = pd.DataFrame(rows, columns=["ref", "pos", "game_id", "date"])
    p["y"] = g[col].to_numpy()[p["pos"].to_numpy(dtype=int)]
    p = p.sort_values(["ref", "date", "game_id"], kind="mergesort")
    p["half"] = p.groupby("ref").cumcount() % 2
    return p.reset_index(drop=True)


def half_means(p: pd.DataFrame, min_half: int = MIN_HALF) -> pd.DataFrame:
    """Per referee: mean and count in each half; only referees with >= min_half games in both halves."""
    t = p.groupby(["ref", "half"])["y"].agg(["mean", "count"]).unstack("half")
    t.columns = [f"{a}_{'AB'[b]}" for a, b in t.columns]
    t = t.dropna()
    return t[(t["count_A"] >= min_half) & (t["count_B"] >= min_half)]


def spearman(a, b) -> float:
    a, b = pd.Series(np.asarray(a, float)), pd.Series(np.asarray(b, float))
    if len(a) < 3:
        return float("nan")
    return float(a.rank().corr(b.rank()))


def rho_of(g: pd.DataFrame, crew_of: Dict[str, tuple], col: str) -> tuple[float, pd.DataFrame]:
    hm = half_means(pairs(g, crew_of, col))
    return spearman(hm["mean_A"], hm["mean_B"]), hm


def cluster_bootstrap(g: pd.DataFrame, crew_of: Dict[str, tuple], col: str, reps: int, seed: int) -> dict:
    """Resample GAMES with replacement (a game's residual sits in three referees' means), keep each pair's
    half and the eligible referees fixed, recompute every referee's half means and the correlation."""
    p = pairs(g, crew_of, col)
    elig = set(half_means(p).index)
    p = p[p["ref"].isin(elig)]
    refs = sorted(elig)
    ri = p["ref"].map({r: k for k, r in enumerate(refs)}).to_numpy()
    cell = ri * 2 + p["half"].to_numpy()
    pos, y = p["pos"].to_numpy(), p["y"].to_numpy()
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(reps):
        w = np.bincount(rng.integers(0, len(g), len(g)), minlength=len(g))[pos].astype(float)
        s = np.bincount(cell, weights=w * y, minlength=2 * len(refs))
        n = np.bincount(cell, weights=w, minlength=2 * len(refs))
        ok = (n[0::2] > 0) & (n[1::2] > 0)
        out.append(spearman((s[0::2] / np.where(n[0::2] > 0, n[0::2], 1))[ok],
                            (s[1::2] / np.where(n[1::2] > 0, n[1::2], 1))[ok]))
    out = np.array(out)
    a = (1 - LEVEL) / 2
    return {"reps": reps, "lo": float(np.nanquantile(out, a)), "hi": float(np.nanquantile(out, 1 - a)),
            "se": float(np.nanstd(out, ddof=1))}


def permuted_crews(g: pd.DataFrame, crew_of: Dict[str, tuple], strata: List[str], rng) -> Dict[str, tuple]:
    """Each game's whole crew moved to a random other game of the same stratum (crews stay intact)."""
    out = {}
    for _, grp in g.groupby(strata):
        ids = [x for x in grp["game_id"] if x in crew_of]
        for src, dst in zip(ids, rng.permutation(ids)):
            out[dst] = crew_of[src]
    return out


def permutation_null(g: pd.DataFrame, crew_of: Dict[str, tuple], col: str, strata: List[str],
                     reps: int, seed: int, observed: float) -> dict:
    rng = np.random.default_rng(seed)
    null = np.array([rho_of(g, permuted_crews(g, crew_of, strata, rng), col)[0] for _ in range(reps)])
    null = null[~np.isnan(null)]
    return {"strata": strata, "reps": reps, "q99": float(np.quantile(null, 0.99)), "mean": float(null.mean()),
            "p_one_sided": float((1 + (null >= observed).sum()) / (1 + len(null)))}


def power_table(sigma: float, n_half: float, k_refs: int) -> dict:
    """Expected split-half correlation rho = s^2 / (s^2 + sigma^2 / n) for a true referee SD s, and the
    sampling SE of a Spearman correlation over k referees (about 1.06 / sqrt(k - 3))."""
    v = sigma ** 2 / n_half
    return {"sigma": sigma, "n_half_median": n_half, "k_refs": k_refs,
            "rho_se_approx": 1.06 / np.sqrt(max(k_refs - 3, 1)),
            "expected_rho": {str(s): s * s / (s * s + v) for s in (0.5, 0.75, 1.0, 1.5, 2.0)},
            "s_at_rho_0.2": float(np.sqrt(0.25 * v))}


# ----- the check -----

def run(games: pd.DataFrame, officials: pd.DataFrame, minutes: Optional[pd.DataFrame] = None,
        boot_reps: int = 2000, perm_reps: int = 1000, seed: int = 0) -> dict:
    g, counts = residual_table(games, minutes)
    crew_of, info = crews(officials)
    counts["games_without_crew"] = int((~g["game_id"].isin(crew_of)).sum())
    out = {"explore": EXPLORE, "season_type": "regular", "min_games_per_half": MIN_HALF, "level": LEVEL,
           "counts": counts, "officials": info,
           "lines_by_season": {s: {"book": grp["book"].fillna("none").value_counts().to_dict(),
                                   "total_price_point": grp["pp"].value_counts().to_dict()}
                               for s, grp in g.groupby("season")},
           "caveats": ["referee announcement time before tip-off is not verified",
                       "a passing rho says referees as a group persist; it does not single out any referee",
                       "crews are not assigned at random; the team-adjusted version and the stratified "
                       "permutation only remove team and season structure"],
           "versions": {}}
    for name, col, sub in (("primary", "r_c", None), ("team_adjusted", "r_team", None),
                           ("raw_uncentred", "r", None), ("no_overtime", "r_c", "no_ot")):
        gg = g[g[sub]].reset_index(drop=True) if sub else g
        if gg.empty:
            out["versions"][name] = {"column": col, "games": 0, "refs": 0, "rho": None,
                                     "note": "no games (team minutes missing)"}
            continue
        rho, hm = rho_of(gg, crew_of, col)
        v = {"column": col, "games": int(len(gg)), "refs": int(len(hm)), "rho": rho,
             "n_half_median": float(np.median(np.r_[hm["count_A"], hm["count_B"]])) if len(hm) else None}
        if len(hm) >= 3:
            v["cluster_bootstrap"] = cluster_bootstrap(gg, crew_of, col, boot_reps, seed)
        if name in ("primary", "team_adjusted") and len(hm) >= 3:
            v["permutation_season_x_home"] = permutation_null(gg, crew_of, col, ["season", "home_abbr"],
                                                              perm_reps, seed + 1, rho)
        if name == "primary" and len(hm) >= 3:
            v["permutation_season_only_report"] = permutation_null(gg, crew_of, col, ["season"],
                                                                   max(perm_reps // 5, 1), seed + 2, rho)
        out["versions"][name] = v
    p, t = out["versions"]["primary"], out["versions"]["team_adjusted"]
    if p["refs"] >= 3:
        out["power"] = power_table(float(g["r_c"].std(ddof=1)), p["n_half_median"], p["refs"])
    checks = {
        "rho_at_least_0.2": bool(p["rho"] >= RHO_MIN),
        "cluster_lower_above_0": bool(p.get("cluster_bootstrap", {}).get("lo", -1) > 0),
        "beats_99pct_of_permutations": bool(p["rho"] > p.get("permutation_season_x_home", {}).get("q99", np.inf)),
        "team_adjusted_rho_at_least_0.2": bool(t["rho"] >= RHO_MIN),
    }
    out["continue_checks"] = checks
    out["continue_to_step_2"] = all(checks.values())
    if out["continue_to_step_2"]:
        out["conclusion"] = "persistent referee tilt on the total residual (step 2 may be designed)"
    elif all(v for k, v in checks.items() if k != "team_adjusted_rho_at_least_0.2"):
        out["conclusion"] = "persistence only before the team adjustment: possibly team confounding; stop"
    else:
        out["conclusion"] = "no detectable persistent referee tilt on the total residual; stop"
    return out


def main(argv: Optional[List[str]] = None) -> int:
    from webui.build_site import connect_readonly, make_run
    ap = argparse.ArgumentParser(prog="python -m jobs.referee_persistence",
                                 description="C3 step 1: referee persistence on the total residual (read-only)")
    ap.add_argument("--out")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--perms", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    try:
        conn = connect_readonly()
    except Exception as exc:  # the message may carry a connection string: print the class only
        print(f"DB connection failed ({type(exc).__name__}); check the DB credentials.")
        return 2
    try:
        _prepare_session(conn)
        q = make_run(conn)
        games = q(_GAMES, EXPLORE)
        officials = q(_OFFICIALS, EXPLORE)
        minutes = q(_MINUTES, EXPLORE)
    except Exception as exc:
        print(f"Loading data failed: {type(exc).__name__}: {_scrub(str(exc))}")
        return 2
    finally:
        conn.close()
    result = run(games, officials, minutes, boot_reps=args.boot, perm_reps=args.perms, seed=args.seed)
    text = json.dumps(result, indent=1, default=str, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
