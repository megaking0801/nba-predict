#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import datetime as dt
from typing import Dict, List, Tuple, Optional

import requests
import pandas as pd
import psycopg2.extras

from jobs.db_utils import db_connect


SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"


def parse_date_us(raw: str) -> dt.date:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("date_us is empty")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    ts = pd.to_datetime(text, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"unsupported date_us format: {text}")
    return ts.date()


def to_iso_date(raw: str) -> str:
    return parse_date_us(raw).strftime("%Y-%m-%d")

def normalize_abbr(a: Optional[str]) -> str:
    x = (a or "").strip().upper()
    if x in ("GS", "GSW"):
        return "GSW"
    if x in ("NO", "NOP"):
        return "NOP"
    if x in ("NY", "NYK"):
        return "NYK"
    if x in ("SA", "SAS"):
        return "SAS"
    if x in ("UTAH", "UTA"):
        return "UTA"
    return x


def ensure_schema(conn) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.game_player_stats (
                  game_id TEXT NOT NULL,
                  season TEXT NOT NULL,
                  game_date_us TEXT NOT NULL,
                  team_abbr TEXT NOT NULL,
                  player_id TEXT NOT NULL,
                  player_name TEXT,
                  minutes DOUBLE PRECISION,
                  pts DOUBLE PRECISION,
                  orb DOUBLE PRECISION,
                  drb DOUBLE PRECISION,
                  ast DOUBLE PRECISION,
                  tov DOUBLE PRECISION,
                  fgm DOUBLE PRECISION,
                  fga DOUBLE PRECISION,
                  fg_pct DOUBLE PRECISION,
                  ftm DOUBLE PRECISION,
                  fta DOUBLE PRECISION,
                  plus_minus DOUBLE PRECISION,
                  updated_at_tw TEXT,
                  PRIMARY KEY (game_id, team_abbr, player_id)
                );
                CREATE INDEX IF NOT EXISTS idx_gps_season_date_team ON public.game_player_stats (season, game_date_us, team_abbr);
                """
            )


def load_games(conn, season: str) -> List[Tuple[str, str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT game_id, game_date_us, away_abbr, home_abbr
            FROM public.games
            WHERE season=%s
            ORDER BY game_date_us ASC
            """,
            (season,),
        )
        return cur.fetchall()


def fetch_scoreboard(date_us: str) -> List[dict]:
    ymd = to_iso_date(date_us).replace("-", "")
    r = requests.get(SCOREBOARD_URL, params={"dates": ymd, "limit": 300}, timeout=25)
    r.raise_for_status()
    return (r.json() or {}).get("events") or []


def event_map_for_date(date_us: str) -> Dict[Tuple[str, str], str]:
    out: Dict[Tuple[str, str], str] = {}
    for ev in fetch_scoreboard(date_us):
        eid = str(ev.get("id") or "")
        comps = ev.get("competitions") or []
        if not comps or not eid:
            continue
        comp = comps[0]
        away = home = None
        for c in (comp.get("competitors") or []):
            team = c.get("team") or {}
            ab = normalize_abbr(team.get("abbreviation"))
            if (c.get("homeAway") or "").lower() == "home":
                home = ab
            else:
                away = ab
        if away and home:
            out[(away, home)] = eid
    return out


def to_float(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "--"):
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            return float(parts[0]) + float(parts[1]) / 60.0
        except Exception:
            return None
    if s.endswith("%"):
        s = s[:-1]
    try:
        return float(s)
    except Exception:
        return None


def parse_summary_rows(summary: dict, game_id: str, season: str, game_date_us: str) -> List[tuple]:
    rows: List[tuple] = []
    players = ((summary or {}).get("boxscore") or {}).get("players") or []
    for team_block in players:
        team = team_block.get("team") or {}
        team_abbr = normalize_abbr(team.get("abbreviation"))
        for stat_group in (team_block.get("statistics") or []):
            labels = [str(x).upper() for x in (stat_group.get("labels") or [])]
            if not labels or "PTS" not in labels:
                continue
            for ath in (stat_group.get("athletes") or []):
                athlete = ath.get("athlete") or {}
                pid = str(athlete.get("id") or "")
                if not pid:
                    continue
                vals = ath.get("stats") or []
                m = {labels[i]: vals[i] for i in range(min(len(labels), len(vals)))}
                row = (
                    game_id,
                    season,
                    game_date_us,
                    team_abbr,
                    pid,
                    athlete.get("displayName"),
                    to_float(m.get("MIN")),
                    to_float(m.get("PTS")),
                    to_float(m.get("OREB")),
                    to_float(m.get("DREB")),
                    to_float(m.get("AST")),
                    to_float(m.get("TO")),
                    to_float(m.get("FGM")),
                    to_float(m.get("FGA")),
                    to_float(m.get("FG%")),
                    to_float(m.get("FTM")),
                    to_float(m.get("FTA")),
                    to_float(m.get("+/-")),
                )
                rows.append(row)
            break
    return rows


def upsert_rows(conn, rows: List[tuple]) -> None:
    if not rows:
        return
    sql = """
    INSERT INTO public.game_player_stats(
      game_id, season, game_date_us, team_abbr, player_id, player_name,
      minutes, pts, orb, drb, ast, tov, fgm, fga, fg_pct, ftm, fta, plus_minus,
      updated_at_tw
    ) VALUES %s
    ON CONFLICT (game_id, team_abbr, player_id) DO UPDATE SET
      player_name=EXCLUDED.player_name,
      minutes=EXCLUDED.minutes,
      pts=EXCLUDED.pts,
      orb=EXCLUDED.orb,
      drb=EXCLUDED.drb,
      ast=EXCLUDED.ast,
      tov=EXCLUDED.tov,
      fgm=EXCLUDED.fgm,
      fga=EXCLUDED.fga,
      fg_pct=EXCLUDED.fg_pct,
      ftm=EXCLUDED.ftm,
      fta=EXCLUDED.fta,
      plus_minus=EXCLUDED.plus_minus,
      updated_at_tw=EXCLUDED.updated_at_tw
    """
    from jobs.time_utils import now_tw_str
    payload = [(*r, now_tw_str()) for r in rows]
    with conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, payload, page_size=500)


def main() -> None:
    import os
    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    conn = db_connect()
    try:
        ensure_schema(conn)
        games = load_games(conn, season)
        if not games:
            print(f"[WARN] no games found for season={season}", flush=True)
            return

        by_date: Dict[str, List[Tuple[str, str, str]]] = {}
        for gid, d, away, home in games:
            iso_d = to_iso_date(d)
            by_date.setdefault(iso_d, []).append((gid, normalize_abbr(away), normalize_abbr(home)))

        total_rows = 0
        for d, game_list in by_date.items():
            try:
                ev_map = event_map_for_date(d)
            except Exception as e:
                print(f"[WARN] scoreboard fetch failed date={d} err={e}", flush=True)
                continue

            batch_rows: List[tuple] = []
            for gid, away, home in game_list:
                eid = ev_map.get((away, home))
                if not eid:
                    continue
                try:
                    r = requests.get(SUMMARY_URL, params={"event": eid}, timeout=25)
                    r.raise_for_status()
                    summary = r.json() or {}
                    batch_rows.extend(parse_summary_rows(summary, gid, season, d))
                except Exception as e:
                    print(f"[WARN] summary fetch failed game_id={gid} event={eid} err={e}", flush=True)

            upsert_rows(conn, batch_rows)
            total_rows += len(batch_rows)
            print(f"[INFO] boxscore date={d} games={len(game_list)} rows={len(batch_rows)}", flush=True)

        print(f"[OK] boxscore cache complete rows={total_rows}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
