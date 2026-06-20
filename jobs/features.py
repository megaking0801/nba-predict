"""Point-in-time feature builder — the heart of the rebuilt ML system.

The structural rule that makes leakage impossible:

    for game in games_sorted_by(date, game_id):
        features[game] = builder.emit(game)   # read state BEFORE this game
        builder.update(game)                  # then fold the result in

The same builder replays settled games and emits for today's slate, so
training and serving share one code path. Unknown team abbreviations raise
instead of silently producing zeros (the old system's worst bug).
"""
from __future__ import annotations

import datetime as dt
import statistics
import unicodedata
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from jobs.config import CONFIG
from jobs.ratings import Elo
from jobs.teams import require_abbr

FEATURE_NAMES = [
    "elo_diff", "net_rtg10_diff", "net_rtg30_diff", "venue_net10_diff",
    "efg10_diff", "tov10_diff", "orb10_diff", "ftr10_diff", "pace10_avg",
    "rest_diff", "home_b2b", "away_b2b", "three_in_four_diff",
    "realized_talent_diff", "talent_gap_home", "talent_gap_away",
    "games_played_min",
]

# Totals (大小分) model uses LEVEL/SUM features — combined offense/defense and
# pace drive total points, unlike the margin model's difference features.
TOTAL_FEATURE_NAMES = [
    "off10_home", "def10_home", "off10_away", "def10_away",
    "off30_home", "def30_home", "off30_away", "def30_away",
    "pace10_home", "pace10_away", "pace10_avg",
    "rest_home", "rest_away", "home_b2b", "away_b2b",
    "three_in_four_home", "three_in_four_away", "games_played_min",
]

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_player_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s)
    toks = [t for t in s.split() if t not in _SUFFIXES]
    return " ".join(toks)


def shrunk(values, k: float, prior: float) -> float:
    n = len(values)
    if n == 0:
        return prior
    mean = sum(values) / n
    return (n / (n + k)) * mean + (k / (n + k)) * prior


def game_score(p: dict) -> float:
    """Hollinger Game Score from a player stat dict (None-safe)."""
    g = lambda k: float(p.get(k) or 0)
    return (g("pts") + 0.4 * g("fgm") - 0.7 * g("fga") - 0.4 * (g("fta") - g("ftm"))
            + 0.7 * g("oreb") + 0.3 * g("dreb") + g("stl") + 0.7 * g("ast")
            + 0.7 * g("blk") - 0.4 * g("pf") - g("tov"))


class PlayerState:
    __slots__ = ("v", "games", "recent_minutes")

    def __init__(self):
        self.v: Optional[float] = None      # EWMA of GameScore per minute
        self.games: int = 0
        self.recent_minutes: deque = deque(maxlen=10)


class TeamState:
    def __init__(self, cfg=CONFIG):
        self.gp = 0
        self.nets: deque = deque(maxlen=cfg.WINDOW_LONG)   # dicts per game
        self.home_nets: deque = deque(maxlen=cfg.WINDOW_SHORT)
        self.away_nets: deque = deque(maxlen=cfg.WINDOW_SHORT)
        self.dates: deque = deque(maxlen=10)
        self.realized: deque = deque(maxlen=cfg.WINDOW_TALENT_GAMES)
        self.prev_final_net30: Optional[float] = None
        self.player_minutes: Dict[int, float] = {}
        self.last_game_players: Set[str] = set()

    def season_reset(self, k_long: float, prior_w: float) -> None:
        if self.gp > 0:
            old_prior = (self.prev_final_net30 or 0.0) * prior_w
            self.prev_final_net30 = shrunk([g["net"] for g in self.nets], k_long, old_prior)
        self.gp = 0
        self.nets.clear()
        self.home_nets.clear()
        self.away_nets.clear()
        self.dates.clear()
        self.realized.clear()
        self.player_minutes.clear()
        self.last_game_players.clear()


class FeatureBuilder:
    def __init__(self, cfg=CONFIG):
        self.cfg = cfg
        self.elo = Elo()
        self.teams: Dict[str, TeamState] = {}
        self.players: Dict[int, PlayerState] = {}
        self.player_names: Dict[int, str] = {}
        self.current_season: Optional[str] = None
        self.first_season: Optional[str] = None
        self._pace_sum = 0.0
        self._pace_n = 0
        self._ortg_sum = 0.0   # league-average offensive rating (totals shrink prior)
        self._drtg_sum = 0.0
        self._rtg_n = 0

    # ----- state access -----

    def team(self, abbr: str) -> TeamState:
        abbr = require_abbr(abbr)
        if abbr not in self.teams:
            self.teams[abbr] = TeamState(self.cfg)
        return self.teams[abbr]

    def _v_used(self, pid: int) -> float:
        cfg = self.cfg
        ps = self.players.get(pid)
        if ps is None or ps.v is None:
            return cfg.PLAYER_PRIOR_VALUE
        w = ps.games / (ps.games + cfg.PLAYER_SHRINK_GAMES)
        return w * ps.v + (1 - w) * cfg.PLAYER_PRIOR_VALUE

    def _league_pace(self) -> float:
        return self._pace_sum / self._pace_n if self._pace_n else 100.0

    def _league_ortg(self) -> float:
        return self._ortg_sum / self._rtg_n if self._rtg_n else 112.0

    def _league_drtg(self) -> float:
        return self._drtg_sum / self._rtg_n if self._rtg_n else 112.0

    def _full_strength(self, ts: TeamState) -> float:
        cfg = self.cfg
        top = sorted(ts.player_minutes.items(), key=lambda kv: kv[1],
                     reverse=True)[:cfg.ROTATION_TOP_N]
        total = 0.0
        for pid, _ in top:
            ps = self.players.get(pid)
            if ps is None or not ps.recent_minutes:
                continue
            total += statistics.median(ps.recent_minutes) * self._v_used(pid) / 240.0
        return total

    def team_top_talent(self, abbr: str, top_n: int) -> List[Tuple[str, float]]:
        """(normalized name, talent value) for the injury veto, best first."""
        ts = self.team(abbr)
        vals = []
        for pid, _ in ts.player_minutes.items():
            ps = self.players.get(pid)
            if ps is None or not ps.recent_minutes:
                continue
            value = (statistics.median(ps.recent_minutes) / 240.0) * self._v_used(pid)
            name = self.player_names.get(pid)
            if name:
                vals.append((norm_player_name(name), value))
        vals.sort(key=lambda x: x[1], reverse=True)
        return vals[:top_n]

    # ----- emit -----

    def _rest_days(self, ts: TeamState, date: dt.date) -> int:
        if not ts.dates:
            return self.cfg.REST_CLIP_DAYS
        days_since = (date - ts.dates[-1]).days
        return max(0, min(days_since - 1, self.cfg.REST_CLIP_DAYS))

    def _three_in_four(self, ts: TeamState, date: dt.date) -> int:
        recent = sum(1 for d in ts.dates if 0 < (date - d).days <= 3)
        return 1 if recent >= 2 else 0

    def emit(self, home_abbr: str, away_abbr: str, date: dt.date) -> Dict[str, float]:
        cfg = self.cfg
        h, a = self.team(home_abbr), self.team(away_abbr)

        def s10(ts: TeamState, key: str) -> float:
            vals = [g[key] for g in list(ts.nets)[-cfg.WINDOW_SHORT:]]
            return shrunk(vals, cfg.SHRINK_K_SHORT, 0.0)

        def s30_net(ts: TeamState) -> float:
            prior = (ts.prev_final_net30 or 0.0) * cfg.PREV_SEASON_PRIOR_W
            return shrunk([g["net"] for g in ts.nets], cfg.SHRINK_K_LONG, prior)

        pace_prior = self._league_pace()
        pace10 = lambda ts: shrunk([g["pace"] for g in list(ts.nets)[-cfg.WINDOW_SHORT:]],
                                   cfg.SHRINK_K_SHORT, pace_prior)

        h_rest, a_rest = self._rest_days(h, date), self._rest_days(a, date)
        h_b2b = 1 if (h.dates and (date - h.dates[-1]).days == 1) else 0
        a_b2b = 1 if (a.dates and (date - a.dates[-1]).days == 1) else 0

        rt = lambda ts: (sum(ts.realized) / len(ts.realized)) if ts.realized else 0.0
        gap = lambda ts: (self._full_strength(ts) - ts.realized[-1]) if ts.realized else 0.0

        return {
            "elo_diff": self.elo.diff(require_abbr(home_abbr), require_abbr(away_abbr)),
            "net_rtg10_diff": s10(h, "net") - s10(a, "net"),
            "net_rtg30_diff": s30_net(h) - s30_net(a),
            "venue_net10_diff": (shrunk(h.home_nets, cfg.SHRINK_K_SHORT, 0.0)
                                 - shrunk(a.away_nets, cfg.SHRINK_K_SHORT, 0.0)),
            "efg10_diff": s10(h, "efg") - s10(a, "efg"),
            "tov10_diff": s10(h, "tov") - s10(a, "tov"),
            "orb10_diff": s10(h, "orb") - s10(a, "orb"),
            "ftr10_diff": s10(h, "ftr") - s10(a, "ftr"),
            "pace10_avg": (pace10(h) + pace10(a)) / 2.0,
            "rest_diff": float(h_rest - a_rest),
            "home_b2b": float(h_b2b),
            "away_b2b": float(a_b2b),
            "three_in_four_diff": float(self._three_in_four(h, date)
                                        - self._three_in_four(a, date)),
            "realized_talent_diff": rt(h) - rt(a),
            "talent_gap_home": gap(h),
            "talent_gap_away": gap(a),
            "games_played_min": float(min(h.gp, a.gp)),
        }

    def emit_totals(self, home_abbr: str, away_abbr: str, date: dt.date) -> Dict[str, float]:
        """Level/sum features for the totals model (read state BEFORE this game)."""
        cfg = self.cfg
        h, a = self.team(home_abbr), self.team(away_abbr)
        o_prior, d_prior, pace_prior = self._league_ortg(), self._league_drtg(), self._league_pace()

        def s_short(ts: TeamState, key: str, prior: float) -> float:
            return shrunk([g[key] for g in list(ts.nets)[-cfg.WINDOW_SHORT:]],
                          cfg.SHRINK_K_SHORT, prior)

        def s_long(ts: TeamState, key: str, prior: float) -> float:
            return shrunk([g[key] for g in ts.nets], cfg.SHRINK_K_LONG, prior)

        h_rest, a_rest = self._rest_days(h, date), self._rest_days(a, date)
        h_b2b = 1 if (h.dates and (date - h.dates[-1]).days == 1) else 0
        a_b2b = 1 if (a.dates and (date - a.dates[-1]).days == 1) else 0
        pace_h, pace_a = s_short(h, "pace", pace_prior), s_short(a, "pace", pace_prior)

        return {
            "off10_home": s_short(h, "ortg", o_prior), "def10_home": s_short(h, "drtg", d_prior),
            "off10_away": s_short(a, "ortg", o_prior), "def10_away": s_short(a, "drtg", d_prior),
            "off30_home": s_long(h, "ortg", o_prior), "def30_home": s_long(h, "drtg", d_prior),
            "off30_away": s_long(a, "ortg", o_prior), "def30_away": s_long(a, "drtg", d_prior),
            "pace10_home": pace_h, "pace10_away": pace_a, "pace10_avg": (pace_h + pace_a) / 2.0,
            "rest_home": float(h_rest), "rest_away": float(a_rest),
            "home_b2b": float(h_b2b), "away_b2b": float(a_b2b),
            "three_in_four_home": float(self._three_in_four(h, date)),
            "three_in_four_away": float(self._three_in_four(a, date)),
            "games_played_min": float(min(h.gp, a.gp)),
        }

    # ----- update -----

    @staticmethod
    def _team_game_metrics(own: dict, opp: dict) -> Optional[dict]:
        f = lambda d, k: float(d.get(k) or 0)
        own_poss = f(own, "fga") - f(own, "oreb") + f(own, "tov") + 0.44 * f(own, "fta")
        opp_poss = f(opp, "fga") - f(opp, "oreb") + f(opp, "tov") + 0.44 * f(opp, "fta")
        if own_poss <= 0 or opp_poss <= 0 or f(own, "fga") <= 0 or f(opp, "fga") <= 0:
            return None
        ortg = 100.0 * f(own, "pts") / own_poss
        drtg = 100.0 * f(opp, "pts") / opp_poss
        efg = lambda d: (f(d, "fgm") + 0.5 * f(d, "fg3m")) / f(d, "fga")
        ftr = lambda d: f(d, "fta") / f(d, "fga")
        own_orb_pct = f(own, "oreb") / max(1.0, f(own, "oreb") + f(opp, "dreb"))
        opp_orb_pct = f(opp, "oreb") / max(1.0, f(opp, "oreb") + f(own, "dreb"))
        return {
            "net": ortg - drtg,
            "ortg": ortg,          # absolute offensive rating (for totals model)
            "drtg": drtg,          # absolute defensive rating (for totals model)
            "efg": efg(own) - efg(opp),
            "tov": (f(own, "tov") / own_poss) - (f(opp, "tov") / opp_poss),
            "orb": own_orb_pct - opp_orb_pct,
            "ftr": ftr(own) - ftr(opp),
            "pace": (own_poss + opp_poss) / 2.0,
        }

    def update(self, bundle: dict) -> None:
        cfg = self.cfg
        season = bundle["season"]
        if self.first_season is None:
            self.first_season = season
        if season != self.current_season:
            if self.current_season is not None:
                self.elo.new_season()
                for ts in self.teams.values():
                    ts.season_reset(cfg.SHRINK_K_LONG, cfg.PREV_SEASON_PRIOR_W)
            self.current_season = season

        home_abbr = require_abbr(bundle["home_abbr"])
        away_abbr = require_abbr(bundle["away_abbr"])
        h, a = self.team(home_abbr), self.team(away_abbr)
        date: dt.date = bundle["date"]

        tstats = bundle.get("team_stats") or {}
        own_h, own_a = tstats.get(home_abbr), tstats.get(away_abbr)
        m_h = self._team_game_metrics(own_h, own_a) if own_h and own_a else None
        m_a = self._team_game_metrics(own_a, own_h) if own_h and own_a else None

        # realized talent + player updates (v_used read BEFORE EWMA update)
        pstats = bundle.get("player_stats") or []
        by_team: Dict[str, List[dict]] = {home_abbr: [], away_abbr: []}
        for p in pstats:
            t = p.get("team_abbr")
            if t in by_team:
                by_team[t].append(p)

        for abbr, ts in ((home_abbr, h), (away_abbr, a)):
            rows = by_team[abbr]
            played = [p for p in rows if (p.get("min_played") or 0) > 0]
            if played:
                ts.realized.append(sum((p["min_played"] or 0) * self._v_used(int(p["player_id"]))
                                       for p in played) / 240.0)
                ts.last_game_players = {norm_player_name(p.get("player_name") or "")
                                        for p in played}
            for p in played:
                pid = int(p["player_id"])
                minutes = float(p["min_played"] or 0)
                self.player_names[pid] = p.get("player_name") or self.player_names.get(pid, "")
                ps = self.players.setdefault(pid, PlayerState())
                ps.recent_minutes.append(minutes)
                ts.player_minutes[pid] = ts.player_minutes.get(pid, 0.0) + minutes
                if minutes >= cfg.PLAYER_MIN_MINUTES:
                    rate = game_score(p) / minutes
                    ps.v = rate if ps.v is None else ((1 - cfg.PLAYER_EWMA_LAMBDA) * ps.v
                                                      + cfg.PLAYER_EWMA_LAMBDA * rate)
                    ps.games += 1

        if m_h and m_a:
            h.nets.append(m_h)
            a.nets.append(m_a)
            h.home_nets.append(m_h["net"])
            a.away_nets.append(m_a["net"])
            self._pace_sum += m_h["pace"]
            self._pace_n += 1
            self._ortg_sum += m_h["ortg"] + m_a["ortg"]
            self._drtg_sum += m_h["drtg"] + m_a["drtg"]
            self._rtg_n += 2

        h.dates.append(date)
        a.dates.append(date)
        h.gp += 1
        a.gp += 1

        margin = bundle.get("margin")
        if margin is not None:
            self.elo.update(home_abbr, away_abbr, int(margin))


# ----- data loading + table building -----

def load_bundles(conn, through_date: Optional[dt.date] = None) -> List[dict]:
    """Final games with team+player boxscores, ordered by (date, game_id)."""
    clause, params = "", []
    if through_date is not None:
        clause = "AND g.game_date_et <= %s"
        params.append(through_date)

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT g.game_id, g.season, g.season_type, g.game_date_et,
                   g.home_abbr, g.away_abbr, g.margin, g.home_score, g.away_score
            FROM public.games_v2 g
            WHERE g.status = 'final' AND g.margin IS NOT NULL {clause}
            ORDER BY g.game_date_et, g.game_id
        """, params)
        games = cur.fetchall()

        cur.execute(f"""
            SELECT t.game_id, t.team_abbr, t.pts, t.fgm, t.fga, t.fg3m, t.fg3a,
                   t.ftm, t.fta, t.oreb, t.dreb, t.ast, t.stl, t.blk, t.tov, t.pf
            FROM public.team_game_stats t
            JOIN public.games_v2 g ON g.game_id = t.game_id
            WHERE g.status = 'final' AND g.margin IS NOT NULL {clause}
        """, params)
        tcols = [d[0] for d in cur.description]
        team_stats: Dict[str, Dict[str, dict]] = {}
        for row in cur.fetchall():
            d = dict(zip(tcols, row))
            team_stats.setdefault(d["game_id"], {})[d["team_abbr"]] = d

        cur.execute(f"""
            SELECT p.game_id, p.player_id, p.team_abbr, p.player_name, p.min_played,
                   p.pts, p.fgm, p.fga, p.ftm, p.fta, p.oreb, p.dreb, p.ast,
                   p.stl, p.blk, p.tov, p.pf
            FROM public.player_game_stats p
            JOIN public.games_v2 g ON g.game_id = p.game_id
            WHERE g.status = 'final' AND g.margin IS NOT NULL {clause}
        """, params)
        pcols = [d[0] for d in cur.description]
        player_stats: Dict[str, List[dict]] = {}
        for row in cur.fetchall():
            d = dict(zip(pcols, row))
            player_stats.setdefault(d["game_id"], []).append(d)

    bundles = []
    for game_id, season, season_type, date, home, away, margin, hs, as_ in games:
        ts = team_stats.get(game_id)
        if not ts or home not in ts or away not in ts:
            continue  # boxscore not landed yet; replay skips it
        bundles.append({
            "game_id": game_id, "season": season, "season_type": season_type,
            "date": date, "home_abbr": home, "away_abbr": away,
            "margin": margin, "home_score": hs, "away_score": as_,
            "team_stats": ts, "player_stats": player_stats.get(game_id, []),
        })
    return bundles


def build_feature_table(bundles: List[dict], cfg=CONFIG) -> pd.DataFrame:
    """One row per final game: meta + label + features + train eligibility."""
    builder = FeatureBuilder(cfg)
    first_season = bundles[0]["season"] if bundles else None
    rows = []
    for b in bundles:
        feats = builder.emit(b["home_abbr"], b["away_abbr"], b["date"])
        tfeats = builder.emit_totals(b["home_abbr"], b["away_abbr"], b["date"])
        warmup = (cfg.WARMUP_GP_FIRST_SEASON if b["season"] == first_season
                  else cfg.MIN_GP_OTHER_SEASONS)
        total = (None if b.get("home_score") is None or b.get("away_score") is None
                 else float(b["home_score"]) + float(b["away_score"]))
        rows.append({
            "game_id": b["game_id"], "season": b["season"],
            "season_type": b["season_type"], "date": b["date"],
            "home_abbr": b["home_abbr"], "away_abbr": b["away_abbr"],
            "margin": float(b["margin"]),
            "total": total,
            "eligible": feats["games_played_min"] >= warmup,
            **feats,
            **{k: tfeats[k] for k in tfeats if k not in feats},
        })
        builder.update(b)
    return pd.DataFrame(rows)


def replay_and_emit(bundles: List[dict], upcoming: List[dict],
                    cfg=CONFIG) -> Tuple[List[dict], FeatureBuilder]:
    """Replay history, then emit features for upcoming games (no update).

    upcoming: dicts with game_id, season, date, home_abbr, away_abbr.
    Returns (rows, builder) — the builder is exposed for the injury veto.
    """
    builder = FeatureBuilder(cfg)
    for b in bundles:
        builder.update(b)
    rows = []
    for g in upcoming:
        feats = builder.emit(g["home_abbr"], g["away_abbr"], g["date"])
        tfeats = builder.emit_totals(g["home_abbr"], g["away_abbr"], g["date"])
        rows.append({**g, **feats, **{k: tfeats[k] for k in tfeats if k not in feats}})
    return rows, builder
