"""Pre-registered feature families for the stat-universe search (read-only, point in time).

Each family replays the games in (date, game_id) order and, for every game, emits its columns from
state built ONLY from earlier games, then folds the game in (the same emit-then-update rule as
jobs/features.py). Definitions and variants are frozen here; see
docs/superpowers/specs/2026-10-29-stat-universe-design.md section 3.2. A family returns a frame
keyed by game_id with its columns; missing state means 0 (the ridge model sees "no information").
"""
from __future__ import annotations

import math
import zlib
from collections import defaultdict, deque
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from jobs.teams import TEAMS

SEASON_TYPES = ("regular", "playoffs", "playin")


def _ordered(bundles: List[dict]) -> List[dict]:
    return sorted((b for b in bundles if b.get("season_type") in SEASON_TYPES),
                  key=lambda b: (b["date"], b["game_id"]))


def _replay(bundles: List[dict], emit: Callable[[dict], dict], update: Callable[[dict], None]) -> pd.DataFrame:
    """Emit every game from state, but fold a date's games in only when the next date starts, so games
    played on the same day never see each other."""
    rows, day, pending = [], None, []
    for b in _ordered(bundles):
        if b["date"] != day:
            for done in pending:
                update(done)
            day, pending = b["date"], []
        rows.append({"game_id": b["game_id"], **emit(b)})
        pending.append(b)
    return pd.DataFrame(rows)


def _f(d: dict, k: str) -> float:
    v = d.get(k)
    return 0.0 if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


# ----- F1: opponent-adjusted rating (SRS-like ridge), season to date -----

def family_srs(bundles: List[dict], variant: dict) -> pd.DataFrame:
    """srs_diff = r_home - r_away from a ridge fit of margin = r_home - r_away + hca over the season's
    earlier games (fit once per date, so same-day games never see each other)."""
    lam, min_games = variant["lam"], 30
    teams = sorted(TEAMS)
    idx = {t: i for i, t in enumerate(teams)}
    rows, hist, season, cache_date, ratings = [], [], None, None, None
    for b in _ordered(bundles):
        if b["season"] != season:
            season, hist, cache_date, ratings = b["season"], [], None, None
        if b["date"] != cache_date:
            cache_date, ratings = b["date"], None
            if len(hist) >= min_games:
                x = np.zeros((len(hist), len(teams) + 1))
                y = np.zeros(len(hist))
                for k, (h, a, m) in enumerate(hist):
                    x[k, idx[h]], x[k, idx[a]], x[k, -1], y[k] = 1.0, -1.0, 1.0, m
                reg = lam * np.eye(len(teams) + 1)
                reg[-1, -1] = 0.0                          # home advantage is not shrunk
                ratings = np.linalg.solve(x.T @ x + reg, x.T @ y)
        r = 0.0
        if ratings is not None:
            r = float(ratings[idx[b["home_abbr"]]] - ratings[idx[b["away_abbr"]]])
        rows.append({"game_id": b["game_id"], "srs_diff": r})
        if b.get("margin") is not None:
            hist.append((b["home_abbr"], b["away_abbr"], float(b["margin"])))
    return pd.DataFrame(rows)


# ----- F2: player efficiency aggregated over the expected rotation -----

def family_player_eff(bundles: List[dict], variant: dict) -> pd.DataFrame:
    """Minutes-weighted EWMA true shooting of each team's expected top-8 (players who appeared in its last
    5 games, ranked by recent minutes), and the top-2 share of usage."""
    lam = variant["lam"]
    ts_ewma: Dict[int, float] = {}
    usg_ewma: Dict[int, float] = {}
    recent_min: Dict[tuple, deque] = defaultdict(lambda: deque(maxlen=10))   # (season, team, player)
    lineups: Dict[tuple, deque] = defaultdict(lambda: deque(maxlen=5))       # (season, team) -> last 5 games' players

    def team_vals(season, team):
        roster = set().union(*lineups[(season, team)])
        players = [(p, sum(recent_min[(season, team, p)]) / len(recent_min[(season, team, p)]))
                   for p in roster if recent_min[(season, team, p)]]
        top = sorted(players, key=lambda x: (-x[1], x[0]))[:8]
        w = sum(m for _, m in top)
        if not top or w <= 0:
            return 0.0, 0.0
        ts = sum(ts_ewma.get(p, 0.55) * m for p, m in top) / w - 0.55
        usg = sorted((usg_ewma.get(p, 0.0) * m for p, m in top), reverse=True)
        tot = sum(usg)
        return ts, (sum(usg[:2]) / tot if tot > 0 else 0.0)

    def emit(b):
        th, uh = team_vals(b["season"], b["home_abbr"])
        ta, ua = team_vals(b["season"], b["away_abbr"])
        return {"ts_agg_diff": th - ta, "usg_top2_diff": uh - ua}

    def update(b):
        s = b["season"]
        played = {b["home_abbr"]: set(), b["away_abbr"]: set()}
        for p in b.get("player_stats") or []:
            mins = _f(p, "min_played")
            if mins <= 0 or p.get("team_abbr") not in played:
                continue
            pid, team = int(p["player_id"]), p["team_abbr"]
            played[team].add(pid)
            recent_min[(s, team, pid)].append(mins)
            shots = _f(p, "fga") + 0.44 * _f(p, "fta")
            if mins >= 8 and shots > 0:
                ts = _f(p, "pts") / (2 * shots)
                ts_ewma[pid] = ts if pid not in ts_ewma else (1 - lam) * ts_ewma[pid] + lam * ts
            use = (shots + _f(p, "tov")) / mins
            usg_ewma[pid] = use if pid not in usg_ewma else (1 - lam) * usg_ewma[pid] + lam * use
        for team, ids in played.items():
            if ids:                                  # placeholder box scores add nothing
                lineups[(s, team)].append(ids)

    return _replay(bundles, emit, update)


# ----- F3: rotation depth -----

def family_rotation(bundles: List[dict], variant: dict) -> pd.DataFrame:
    """Over a team's last N games: share of minutes played by its top 5, and how many average 15+."""
    n = variant["n"]
    games: Dict[tuple, deque] = defaultdict(lambda: deque(maxlen=n))   # (season, team) -> [ {pid: min} ]

    def vals(season, team):
        hist = games[(season, team)]
        if not hist:
            return 0.0, 0.0
        tot = defaultdict(float)
        for g in hist:
            for pid, m in g.items():
                tot[pid] += m
        mins = sorted(tot.values(), reverse=True)
        all_m = sum(mins)
        share = sum(mins[:5]) / all_m if all_m > 0 else 0.0
        regulars = sum(1 for m in mins if m / len(hist) >= 15)
        return share, float(regulars)

    def emit(b):
        sh, rh = vals(b["season"], b["home_abbr"])
        sa, ra = vals(b["season"], b["away_abbr"])
        return {"top5_share_diff": sh - sa, "regulars_diff": rh - ra}

    def update(b):
        per = {b["home_abbr"]: {}, b["away_abbr"]: {}}
        for p in b.get("player_stats") or []:
            if p.get("team_abbr") in per and _f(p, "min_played") > 0:
                per[p["team_abbr"]][int(p["player_id"])] = _f(p, "min_played")
        for team, g in per.items():
            if g:                                    # placeholder box scores add nothing
                games[(b["season"], team)].append(g)

    return _replay(bundles, emit, update)


# ----- F4: shot distribution (needs team_shot_buckets) -----

def family_shots(bundles: List[dict], variant: dict, buckets: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Rolling shares of rim (0-4 ft), three-point and corner-three attempts, own and allowed. Games whose
    buckets do not add up to the box-score FGA within 2 are skipped (no state update)."""
    n = variant["n"]
    by_game: Dict[str, Dict[str, dict]] = defaultdict(dict)
    if buckets is not None and len(buckets):
        for r in buckets.itertuples():
            d = by_game[r.game_id].setdefault(r.team_abbr, {"fga": 0.0, "rim": 0.0, "three": 0.0, "corner": 0.0})
            d["fga"] += r.fga
            if r.dist_bucket == "0-4":
                d["rim"] += r.fga
            if r.pts_value == 3:
                d["three"] += r.fga
                if r.corner:
                    d["corner"] += r.fga
    own: Dict[tuple, deque] = defaultdict(lambda: deque(maxlen=n))
    allowed: Dict[tuple, deque] = defaultdict(lambda: deque(maxlen=n))
    keys = ("rim", "three", "corner")

    def share(hist, key):
        f = sum(h["fga"] for h in hist)
        return sum(h[key] for h in hist) / f if f > 0 else 0.0

    def emit(b):
        s, h, a = b["season"], b["home_abbr"], b["away_abbr"]
        row = {}
        for k in keys:
            row[f"{k}_off_diff"] = share(own[(s, h)], k) - share(own[(s, a)], k)
            row[f"{k}_def_diff"] = share(allowed[(s, a)], k) - share(allowed[(s, h)], k)
        return row

    def update(b):
        s, h, a = b["season"], b["home_abbr"], b["away_abbr"]
        g = by_game.get(b["game_id"], {})
        box = b.get("team_stats") or {}
        ok = all(t in g and t in box and abs(g[t]["fga"] - _f(box[t], "fga")) <= 2 and g[t]["fga"] > 0 for t in (h, a))
        if ok:
            own[(s, h)].append(g[h]); own[(s, a)].append(g[a])
            allowed[(s, h)].append(g[a]); allowed[(s, a)].append(g[h])

    return _replay(bundles, emit, update)


# ----- F5: motivation (standings late in the regular season) -----

def family_motivation(bundles: List[dict], variant: dict) -> pd.DataFrame:
    """Games behind the 10th place of the team's conference, and eliminated / top-6-clinched flags, only
    when a team has at most N regular-season games left (otherwise 0)."""
    left_max = variant["left"]
    games = _ordered(bundles)
    scheduled = defaultdict(int)
    for b in games:
        if b["season_type"] == "regular":
            scheduled[(b["season"], b["home_abbr"])] += 1
            scheduled[(b["season"], b["away_abbr"])] += 1
    w, gp = defaultdict(int), defaultdict(int)

    def team_vals(season, team):
        left = scheduled[(season, team)] - gp[(season, team)]
        if left > left_max:
            return 0.0, 0.0, 0.0
        conf = TEAMS[team][3]
        table = sorted(((w[(season, t)], scheduled[(season, t)] - gp[(season, t)]) for t in TEAMS
                        if TEAMS[t][3] == conf), key=lambda x: -x[0])
        mine = w[(season, team)]
        tenth_w = table[9][0] if len(table) >= 10 else 0
        seventh_max = (table[6][0] + table[6][1]) if len(table) >= 7 else 0
        gb = max(-10.0, min(10.0, float(mine - tenth_w)))
        eliminated = float(mine + left < tenth_w)
        clinched = float(mine > seventh_max)
        return gb, eliminated, clinched

    def emit(b):
        if b["season_type"] != "regular":
            return {"gb10_diff": 0.0, "eliminated_home": 0.0, "eliminated_away": 0.0,
                    "clinched_home": 0.0, "clinched_away": 0.0}
        gh, eh, ch = team_vals(b["season"], b["home_abbr"])
        ga, ea, ca = team_vals(b["season"], b["away_abbr"])
        return {"gb10_diff": gh - ga, "eliminated_home": eh, "eliminated_away": ea,
                "clinched_home": ch, "clinched_away": ca}

    def update(b):
        if b["season_type"] == "regular" and b.get("margin") is not None:
            for team, won in ((b["home_abbr"], b["margin"] > 0), (b["away_abbr"], b["margin"] < 0)):
                gp[(b["season"], team)] += 1
                w[(b["season"], team)] += int(won)

    return _replay(games, emit, update)


# ----- F7: referees and the total (target = total - total line) -----

def family_refs(bundles: List[dict], variant: dict, officials: Optional[pd.DataFrame] = None,
                total_lines: Optional[Dict[str, float]] = None) -> pd.DataFrame:
    """Mean over the game's officials of each official's shrunk average total residual in earlier games.
    May be an oracle: the time officials are announced before tip-off is not verified."""
    k = variant["k"]
    crew = defaultdict(list)
    if officials is not None and len(officials):
        for r in officials.itertuples():
            crew[r.game_id].append(r.official_name)
    total_lines = total_lines or {}
    s_sum, s_n = defaultdict(float), defaultdict(int)

    def emit(b):
        vals = [s_sum[o] / (s_n[o] + k) for o in crew.get(b["game_id"], [])]   # shrunk toward 0
        return {"ref_total_tilt": float(np.mean(vals)) if vals else 0.0}

    def update(b):
        line = total_lines.get(b["game_id"])
        if line is not None and b.get("home_score") is not None and b.get("away_score") is not None:
            resid = float(b["home_score"]) + float(b["away_score"]) - float(line)
            for o in crew.get(b["game_id"], []):
                s_sum[o] += resid
                s_n[o] += 1

    return _replay(bundles, emit, update)


# ----- noise family: must never be selected -----

def family_noise(bundles: List[dict], variant: dict) -> pd.DataFrame:
    """Three columns of pure noise seeded by the game id (no time structure, no information)."""
    rows = []
    for b in _ordered(bundles):
        rng = np.random.default_rng(zlib.crc32(f'{variant["seed"]}:{b["game_id"]}'.encode()))
        x = rng.normal(size=3)
        rows.append({"game_id": b["game_id"], "noise_1": x[0], "noise_2": x[1], "noise_3": x[2]})
    return pd.DataFrame(rows)


# ----- the registry (frozen) -----

REGISTRY = [
    {"family": "F1_srs", "target": "margin", "build": family_srs, "needs": [],
     "variants": [{"lam": 1.0}, {"lam": 5.0}, {"lam": 20.0}]},
    {"family": "F2_player_eff", "target": "margin", "build": family_player_eff, "needs": [],
     "variants": [{"lam": 0.1}, {"lam": 0.2}, {"lam": 0.3}]},
    {"family": "F3_rotation", "target": "margin", "build": family_rotation, "needs": [],
     "variants": [{"n": 5}, {"n": 10}, {"n": 20}]},
    {"family": "F4_shots", "target": "margin", "build": family_shots, "needs": ["buckets"],
     "variants": [{"n": 10}, {"n": 20}, {"n": 40}]},
    {"family": "F5_motivation", "target": "margin", "build": family_motivation, "needs": [],
     "variants": [{"left": 10}, {"left": 20}, {"left": 30}]},
    {"family": "F7_refs_total", "target": "total", "build": family_refs, "needs": ["officials", "total_lines"],
     "variants": [{"k": 10}, {"k": 30}, {"k": 100}], "caveat": "possible oracle: announcement time unverified"},
    {"family": "N_noise", "target": "margin", "build": family_noise, "needs": [],
     "variants": [{"seed": 1}, {"seed": 2}, {"seed": 3}]},
]
REGISTERED_NOT_RUN = [
    {"family": "F6_rapm", "reason": "needs play-by-play (not stored); external files end in 2024-25, so at most "
                                    "explore+validate, never confirmed"},
    {"family": "F8_line_move", "reason": "needs ESPN opening lines re-fetched (download needs the user's consent)"},
    {"family": "F9_availability", "reason": "registered only: official report history from 2021-22 (unverified), "
                                            "own injury_reports from 2026-10-24; run after one full season"},
]
SEARCH_SPACE = 9 * 3 + 9          # families x variants + cumulative steps (declared upper bound)
