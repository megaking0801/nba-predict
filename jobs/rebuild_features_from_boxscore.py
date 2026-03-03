#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import datetime as dt
from typing import Dict, Tuple

import pandas as pd
import psycopg2.extras

from jobs.db_utils import db_connect
from jobs.time_utils import now_tw_str


def ensure_columns(conn) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_ts_pct DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_ts_pct DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_orb_rate DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_orb_rate DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_usage_proxy DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_usage_proxy DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_onoff_proxy DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_onoff_proxy DOUBLE PRECISION;")


def load_tables(conn, season: str):
    games = pd.read_sql(
        """
        SELECT game_id, game_date_us, season, away_abbr, home_abbr,
               away_score, home_score, status
        FROM public.games
        WHERE season=%s
        """,
        conn,
        params=(season,),
    )
    gps = pd.read_sql(
        """
        SELECT game_id, game_date_us, season, team_abbr,
               minutes, pts, orb, drb, ast, tov, fgm, fga, fg_pct, ftm, fta, plus_minus
        FROM public.game_player_stats
        WHERE season=%s
        """,
        conn,
        params=(season,),
    )
    return games, gps




def parse_game_date_us(raw) -> dt.date:
    """Accept MM/DD/YYYY or YYYY-MM-DD (and timestamp-like values) and return date."""
    if raw is None:
        raise ValueError("game_date_us is null")
    text = str(raw).strip()
    if not text:
        raise ValueError("game_date_us is empty")

    # strict known formats first
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    # fallback for timestamp-like strings
    ts = pd.to_datetime(text, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"unsupported game_date_us format: {text}")
    return ts.date()

def _weighted(vals):
    if not vals:
        return None
    w = list(range(len(vals), 0, -1))
    s = sum(v * ww for v, ww in zip(vals, w))
    return float(s / sum(w))


def build_team_game_agg(gps: pd.DataFrame) -> pd.DataFrame:
    if gps.empty:
        return pd.DataFrame(columns=["game_id", "game_date_us", "team_abbr", "pts", "orb", "drb", "ast", "tov", "fga", "fta", "plus_minus", "ts_pct", "usage_proxy", "onoff_proxy"])
    grp = gps.groupby(["game_id", "game_date_us", "team_abbr"], as_index=False).agg(
        pts=("pts", "sum"),
        orb=("orb", "sum"),
        drb=("drb", "sum"),
        ast=("ast", "sum"),
        tov=("tov", "sum"),
        fga=("fga", "sum"),
        fta=("fta", "sum"),
        plus_minus=("plus_minus", "mean"),
    )
    denom = 2 * (grp["fga"].fillna(0) + 0.44 * grp["fta"].fillna(0))
    grp["ts_pct"] = grp["pts"].where(denom > 0, 0) / denom.where(denom > 0, 1)
    grp["usage_proxy"] = grp["fga"].fillna(0) + 0.44 * grp["fta"].fillna(0) + grp["tov"].fillna(0)
    grp["onoff_proxy"] = grp["plus_minus"].fillna(0)
    return grp


def build_team_context(games: pd.DataFrame) -> Dict[Tuple[str, dt.date], Tuple[int, float]]:
    rows = []
    for _, r in games.iterrows():
        d = parse_game_date_us(r["game_date_us"])
        hs = r["home_score"]
        aws = r["away_score"]
        rows.append((r["home_abbr"], d, hs is not None and aws is not None and hs > aws))
        rows.append((r["away_abbr"], d, hs is not None and aws is not None and aws > hs))
    team_df = pd.DataFrame(rows, columns=["team", "date", "win"]).sort_values("date")
    out: Dict[Tuple[str, dt.date], Tuple[int, float]] = {}
    for team, tdf in team_df.groupby("team"):
        dates = list(tdf["date"])
        wins = list(tdf["win"])
        for i, d in enumerate(dates):
            prior_dates = dates[:i]
            prior_wins = wins[:i]
            b2b = 1 if prior_dates and (d - prior_dates[-1]).days == 1 else 0
            recent = float(sum(prior_wins[-5:]) / len(prior_wins[-5:])) if prior_wins else 0.5
            out[(team, d)] = (b2b, recent)
    return out


def team_prior_metrics(team_games: pd.DataFrame, team: str, game_day: dt.date):
    t = team_games[(team_games["team_abbr"] == team) & (team_games["game_date"] < game_day)].sort_values("game_date", ascending=False).head(5)
    if t.empty:
        return {
            "pts": 0.0,
            "impact": 0.0,
            "ts_pct": 0.0,
            "orb_rate": 0.0,
            "usage": 0.0,
            "onoff": 0.0,
        }
    vals = t.to_dict(orient="records")
    pts = _weighted([float(x.get("pts") or 0) for x in vals]) or 0.0
    impact = _weighted([float((x.get("pts") or 0) + 1.2 * (x.get("orb") or 0) + 1.5 * (x.get("ast") or 0) - 1.8 * (x.get("tov") or 0)) for x in vals]) or 0.0
    ts_pct = _weighted([float(x.get("ts_pct") or 0) for x in vals]) or 0.0
    orb_rate = _weighted([float((x.get("orb") or 0) / max(1.0, (x.get("orb") or 0) + (x.get("drb") or 0))) for x in vals]) or 0.0
    usage = _weighted([float(x.get("usage_proxy") or 0) for x in vals]) or 0.0
    onoff = _weighted([float(x.get("onoff_proxy") or 0) for x in vals]) or 0.0
    return {"pts": pts, "impact": impact, "ts_pct": ts_pct, "orb_rate": orb_rate, "usage": usage, "onoff": onoff}


def main() -> None:
    import os
    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    conn = db_connect()
    try:
        ensure_columns(conn)
        games, gps = load_tables(conn, season)
        if games.empty:
            print(f"[WARN] no games found season={season}", flush=True)
            return

        tg = build_team_game_agg(gps)
        if not tg.empty:
            tg["game_date"] = tg["game_date_us"].map(parse_game_date_us)
        else:
            tg["game_date"] = pd.Series(dtype=object)
        ctx = build_team_context(games)

        updates = []
        for _, g in games.iterrows():
            d = parse_game_date_us(g["game_date_us"])
            home = g["home_abbr"]
            away = g["away_abbr"]
            hm = team_prior_metrics(tg, home, d)
            am = team_prior_metrics(tg, away, d)
            hb2b, hr = ctx.get((home, d), (0, 0.5))
            ab2b, ar = ctx.get((away, d), (0, 0.5))
            updates.append((
                hm["pts"], am["pts"], hm["impact"], am["impact"],
                int(hb2b), int(ab2b), float(hr), float(ar),
                hm["ts_pct"], am["ts_pct"], hm["orb_rate"], am["orb_rate"],
                hm["usage"], am["usage"], hm["onoff"], am["onoff"],
                now_tw_str(), g["game_id"],
            ))

        sql = """
        UPDATE public.games
        SET home_pts_sum=%s,
            away_pts_sum=%s,
            home_impact_mean=%s,
            away_impact_mean=%s,
            home_b2b=%s,
            away_b2b=%s,
            home_recent_w=%s,
            away_recent_w=%s,
            home_ts_pct=%s,
            away_ts_pct=%s,
            home_orb_rate=%s,
            away_orb_rate=%s,
            home_usage_proxy=%s,
            away_usage_proxy=%s,
            home_onoff_proxy=%s,
            away_onoff_proxy=%s,
            updated_at_tw=%s
        WHERE game_id=%s
        """
        with conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, updates, page_size=500)

        print(f"[OK] rebuilt features season={season} games={len(updates)}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
