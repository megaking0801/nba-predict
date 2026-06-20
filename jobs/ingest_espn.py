"""Ingest games + team/player box scores from ESPN into the v2 schema.

Replaces the stats.nba.com path (NBA's Akamai edge geo/IP-blocks both GitHub
runners and non-US homes). game_id is the ESPN event id — self-consistent as
long as every ingest goes through ESPN.

Backfill:  python -m jobs.ingest_espn --seasons "2023-24,2024-25,2025-26"
Daily:     python -m jobs.ingest_espn --days-back 3
"""
from __future__ import annotations

import argparse
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import psycopg2.extras

from jobs.db_utils import db_connect
from jobs.espn_http import odds, scoreboard, summary
from jobs.schema import ensure_schema
from jobs.teams import normalize_espn_abbr

ET = ZoneInfo("America/New_York")
SEASON_TYPE = {1: "preseason", 2: "regular", 3: "playoffs", 4: "allstar", 5: "playin"}
MAX_WORKERS = 8


# ----- parse helpers -----

def _ma(v) -> Tuple[Optional[int], Optional[int]]:
    """'46-86' -> (46, 86); blanks/dashes -> (None, None)."""
    if not v or "-" not in str(v):
        return None, None
    a, b = str(v).split("-", 1)
    return _int(a), _int(b)


def _int(v) -> Optional[int]:
    try:
        return int(round(float(str(v).strip())))
    except (TypeError, ValueError):
        return None


def _float(v) -> Optional[float]:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _season_str(year: int) -> str:
    """ESPN season.year is the ENDING year (2025 == '2024-25')."""
    return f"{year - 1}-{str(year)[-2:]}"


def _et_date(iso_utc: str) -> Optional[dt.date]:
    """'2025-01-16T00:00Z' -> ET calendar date."""
    try:
        d = dt.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        return d.astimezone(ET).date()
    except Exception:
        return None


def _utc(iso_utc: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    except Exception:
        return None


# ----- scoreboard -> game meta -----

def parse_scoreboard_events(sb: dict) -> List[dict]:
    """Pull final games out of a scoreboard payload as flat meta dicts."""
    out = []
    for e in (sb or {}).get("events", []):
        try:
            comp = e["competitions"][0]
            status = comp["status"]["type"]["name"]  # STATUS_FINAL / STATUS_SCHEDULED / STATUS_IN_PROGRESS
            comps = {c["homeAway"]: c for c in comp["competitors"]}
            home, away = comps["home"], comps["away"]
            season = e.get("season", {}) or {}
            year = season.get("year")
            out.append({
                "game_id": str(e["id"]),
                "date_utc": comp.get("date") or e.get("date"),
                "season": _season_str(year) if year else None,
                "season_type": SEASON_TYPE.get(season.get("type"), "regular"),
                "status": "final" if status == "STATUS_FINAL" else
                          ("live" if status == "STATUS_IN_PROGRESS" else "scheduled"),
                "home_abbr": normalize_espn_abbr(home["team"]["abbreviation"]),
                "away_abbr": normalize_espn_abbr(away["team"]["abbreviation"]),
                "home_score": _int(home.get("score")),
                "away_score": _int(away.get("score")),
            })
        except Exception as ex:  # noqa: BLE001
            print(f"[WARN] scoreboard event parse failed id={e.get('id')} err={ex}", flush=True)
    return out


# ----- summary -> team + player rows -----

def _player_stat_dict(keys: List[str], stats: List[str]) -> Dict[str, str]:
    return dict(zip(keys, stats))


def parse_summary(meta: dict, bs: dict) -> Tuple[List[tuple], List[tuple]]:
    """Returns (team_rows, player_rows) for one game. Empty lists if no box."""
    box = (bs or {}).get("boxscore") or {}
    players_block = box.get("players") or []
    teams_block = box.get("teams") or []
    if not players_block or not teams_block:
        return [], []

    gid = meta["game_id"]
    score = {meta["home_abbr"]: meta["home_score"], meta["away_abbr"]: meta["away_score"]}

    # --- players (also accumulate team minutes) ---
    player_rows: List[tuple] = []
    team_minutes: Dict[str, float] = {}
    for tb in players_block:
        abbr = normalize_espn_abbr(tb["team"]["abbreviation"])
        st = (tb.get("statistics") or [{}])[0]
        keys = st.get("keys") or []
        for ath in st.get("athletes") or []:
            if ath.get("didNotPlay") or not ath.get("stats"):
                continue
            athlete = ath.get("athlete") or {}
            pid = _int(athlete.get("id"))
            if pid is None:  # team-total rows / players without an ESPN id
                continue
            s = _player_stat_dict(keys, ath["stats"])
            mins = _float(s.get("minutes"))
            if mins:
                team_minutes[abbr] = team_minutes.get(abbr, 0.0) + mins
            fgm, fga = _ma(s.get("fieldGoalsMade-fieldGoalsAttempted"))
            fg3m, fg3a = _ma(s.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
            ftm, fta = _ma(s.get("freeThrowsMade-freeThrowsAttempted"))
            player_rows.append((
                gid, pid, abbr, athlete.get("displayName"), mins,
                _int(s.get("points")), fgm, fga, fg3m, fg3a, ftm, fta,
                _int(s.get("offensiveRebounds")), _int(s.get("defensiveRebounds")),
                _int(s.get("rebounds")), _int(s.get("assists")), _int(s.get("steals")),
                _int(s.get("blocks")), _int(s.get("turnovers")), _int(s.get("fouls")),
                _int(s.get("plusMinus")),
            ))

    # --- teams ---
    team_rows: List[tuple] = []
    margin = ((meta["home_score"] or 0) - (meta["away_score"] or 0))
    for tb in teams_block:
        abbr = normalize_espn_abbr(tb["team"]["abbreviation"])
        is_home = tb.get("homeAway") == "home"
        d = {s.get("name"): s.get("displayValue") for s in (tb.get("statistics") or [])}
        fgm, fga = _ma(d.get("fieldGoalsMade-fieldGoalsAttempted"))
        fg3m, fg3a = _ma(d.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
        ftm, fta = _ma(d.get("freeThrowsMade-freeThrowsAttempted"))
        pts = score.get(abbr)
        pm = margin if is_home else -margin
        wl = None
        if meta["home_score"] is not None and meta["away_score"] is not None:
            wl = "W" if pm > 0 else ("L" if pm < 0 else None)
        team_rows.append((
            gid, abbr, is_home, wl, _int(team_minutes.get(abbr)),
            pts, fgm, fga, fg3m, fg3a, ftm, fta,
            _int(d.get("offensiveRebounds")), _int(d.get("defensiveRebounds")),
            _int(d.get("totalRebounds")), _int(d.get("assists")), _int(d.get("steals")),
            _int(d.get("blocks")), _int(d.get("turnovers")), _int(d.get("fouls")), pm,
        ))
    return team_rows, player_rows


# ----- DB upserts -----

UPSERT_GAMES = """
INSERT INTO public.games_v2
  (game_id, season, season_type, game_date_et, tipoff_utc, home_abbr, away_abbr,
   status, home_score, away_score, margin)
VALUES %s
ON CONFLICT (game_id) DO UPDATE SET
  season=EXCLUDED.season, season_type=EXCLUDED.season_type,
  game_date_et=EXCLUDED.game_date_et, tipoff_utc=EXCLUDED.tipoff_utc,
  home_abbr=EXCLUDED.home_abbr, away_abbr=EXCLUDED.away_abbr,
  status=EXCLUDED.status, home_score=EXCLUDED.home_score,
  away_score=EXCLUDED.away_score, margin=EXCLUDED.margin, updated_at=now()
"""

UPSERT_TEAM = """
INSERT INTO public.team_game_stats
  (game_id, team_abbr, is_home, wl, min, pts, fgm, fga, fg3m, fg3a, ftm, fta,
   oreb, dreb, reb, ast, stl, blk, tov, pf, plus_minus)
VALUES %s
ON CONFLICT (game_id, team_abbr) DO UPDATE SET
  is_home=EXCLUDED.is_home, wl=EXCLUDED.wl, min=EXCLUDED.min, pts=EXCLUDED.pts,
  fgm=EXCLUDED.fgm, fga=EXCLUDED.fga, fg3m=EXCLUDED.fg3m, fg3a=EXCLUDED.fg3a,
  ftm=EXCLUDED.ftm, fta=EXCLUDED.fta, oreb=EXCLUDED.oreb, dreb=EXCLUDED.dreb,
  reb=EXCLUDED.reb, ast=EXCLUDED.ast, stl=EXCLUDED.stl, blk=EXCLUDED.blk,
  tov=EXCLUDED.tov, pf=EXCLUDED.pf, plus_minus=EXCLUDED.plus_minus, updated_at=now()
"""

UPSERT_PLAYER = """
INSERT INTO public.player_game_stats
  (game_id, player_id, team_abbr, player_name, min_played, pts, fgm, fga,
   fg3m, fg3a, ftm, fta, oreb, dreb, reb, ast, stl, blk, tov, pf,
   plus_minus)
VALUES %s
ON CONFLICT (game_id, player_id) DO UPDATE SET
  team_abbr=EXCLUDED.team_abbr, player_name=EXCLUDED.player_name,
  min_played=EXCLUDED.min_played, pts=EXCLUDED.pts, fgm=EXCLUDED.fgm,
  fga=EXCLUDED.fga, fg3m=EXCLUDED.fg3m, fg3a=EXCLUDED.fg3a, ftm=EXCLUDED.ftm,
  fta=EXCLUDED.fta, oreb=EXCLUDED.oreb, dreb=EXCLUDED.dreb, reb=EXCLUDED.reb,
  ast=EXCLUDED.ast, stl=EXCLUDED.stl, blk=EXCLUDED.blk, tov=EXCLUDED.tov,
  pf=EXCLUDED.pf, plus_minus=EXCLUDED.plus_minus, updated_at=now()
"""


def _game_row(m: dict) -> tuple:
    return (m["game_id"], m["season"], m["season_type"], _et_date(m["date_utc"]),
            _utc(m["date_utc"]), m["home_abbr"], m["away_abbr"], m["status"],
            m["home_score"], m["away_score"],
            (m["home_score"] - m["away_score"]) if m["home_score"] is not None
            and m["away_score"] is not None else None)


def _date_range(start: dt.date, end: dt.date) -> List[str]:
    days, d = [], start
    while d <= end:
        days.append(d.strftime("%Y%m%d"))
        d += dt.timedelta(days=1)
    return days


SEASON_WINDOWS = {  # ET calendar windows generous enough to cover play-in/finals
    "2023-24": (dt.date(2023, 10, 1), dt.date(2024, 6, 30)),
    "2024-25": (dt.date(2024, 10, 1), dt.date(2025, 6, 30)),
    "2025-26": (dt.date(2025, 10, 1), dt.date(2026, 6, 30)),
}


def ingest_dates(dates: List[str], finals_only: bool) -> Tuple[int, int, int]:
    # 1) scoreboards (concurrent)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        sbs = list(ex.map(lambda d: scoreboard(d), dates))
    metas: List[dict] = []
    for sb in sbs:
        metas.extend(parse_scoreboard_events(sb))
    if finals_only:
        metas = [m for m in metas if m["status"] == "final"]
    if not metas:
        return 0, 0, 0
    # dedup by game_id (a game can appear under adjacent date queries)
    metas = list({m["game_id"]: m for m in metas}.values())

    # 2) summaries (concurrent)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        summaries = list(ex.map(lambda m: (m, summary(m["game_id"])), metas))

    game_rows, team_rows, player_rows = [], [], []
    for m, bs in summaries:
        game_rows.append(_game_row(m))
        if m["status"] == "final" and bs:
            try:
                tr, pr = parse_summary(m, bs)
                team_rows.extend(tr)
                player_rows.extend(pr)
            except Exception as ex:  # noqa: BLE001 — one odd game must not kill the batch
                print(f"[WARN] parse_summary failed game={m['game_id']} err={ex}", flush=True)

    # Fresh short-lived connection for the write only — never hold one idle
    # across the long concurrent fetch phase (Supabase's pooler drops idle conns).
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, UPSERT_GAMES, game_rows, page_size=500)
            if team_rows:
                psycopg2.extras.execute_values(cur, UPSERT_TEAM, team_rows, page_size=500)
            if player_rows:
                psycopg2.extras.execute_values(cur, UPSERT_PLAYER, player_rows, page_size=500)
        conn.commit()
    finally:
        conn.close()
    return len(game_rows), len(team_rows), len(player_rows)


def backfill(seasons: List[str]) -> None:
    for s in seasons:
        if s not in SEASON_WINDOWS:
            print(f"[WARN] unknown season {s}; skip", flush=True)
            continue
        start, end = SEASON_WINDOWS[s]
        dates = _date_range(start, end)
        # process in monthly chunks to bound memory and show progress
        g = t = p = 0
        for i in range(0, len(dates), 31):
            chunk = dates[i:i + 31]
            cg, ct, cp = ingest_dates(chunk, finals_only=True)
            g += cg; t += ct; p += cp
            print(f"[..] {s} {chunk[0]}..{chunk[-1]}: +{cg} games (cum games={g})", flush=True)
        print(f"[OK] {s}: games={g} team_rows={t} player_rows={p}", flush=True)


def daily(days_back: int, days_ahead: int = 2) -> None:
    today = dt.datetime.now(ET).date()
    dates = _date_range(today - dt.timedelta(days=days_back),
                        today + dt.timedelta(days=days_ahead))
    g, t, p = ingest_dates(dates, finals_only=False)
    print(f"[OK] daily: games={g} team_rows={t} player_rows={p}", flush=True)


# ----- odds (market lines) from ESPN core API -----

def _spread_num(american: Optional[str]) -> Optional[float]:
    """ESPN point-spread string ('+5.5'/'-4.5'/'PK'/'EVEN') -> float home handicap."""
    if american is None:
        return None
    s = str(american).strip().upper().replace("+", "")
    if s in ("PK", "EVEN", "EV", ""):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def _block_total(block: Optional[dict]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(total_line, over_decimal, under_decimal) from an ESPN open/close/current block."""
    block = block or {}
    tl = _spread_num((block.get("total") or {}).get("american"))
    op = (block.get("over") or {}).get("decimal")
    up = (block.get("under") or {}).get("decimal")
    return tl, (float(op) if op else None), (float(up) if up else None)


def parse_close_line(items: List[dict]) -> Optional[dict]:
    """Spread + total lines from the priority provider, preferring close → current
    → open (so settled games use the close, upcoming games still get a live line).
    Decimal prices default to 1.91 (-110) when absent. Returns None if no spread."""
    prov = next((it for it in items if (it.get("provider") or {}).get("priority") == 0),
                items[0] if items else None)
    if not prov:
        return None

    def side_block(side: str) -> dict:
        o = prov.get(side) or {}
        return o.get("close") or o.get("current") or o.get("open") or {}

    hb, ab = side_block("homeTeamOdds"), side_block("awayTeamOdds")
    hs = _spread_num((hb.get("pointSpread") or {}).get("american"))
    if hs is None:
        return None  # market_lines.home_spread is NOT NULL
    hp = (hb.get("spread") or {}).get("decimal")
    ap_ = (ab.get("spread") or {}).get("decimal")

    tl = op = up = None
    for blk in (prov.get("close"), prov.get("current"), prov.get("open")):
        tl, op, up = _block_total(blk)
        if tl is not None:
            break
    if tl is None and prov.get("overUnder") is not None:
        tl = _spread_num(prov.get("overUnder"))

    return {
        "home_spread": hs,
        "home_price": float(hp or 1.91),
        "away_price": float(ap_ or 1.91),
        "total_line": tl,
        "over_price": float(op or 1.91) if tl is not None else None,
        "under_price": float(up or 1.91) if tl is not None else None,
    }


INSERT_LINE = """
INSERT INTO public.market_lines
  (game_id, captured_at, source, book, home_spread, home_price, away_price,
   total_line, over_price, under_price)
VALUES %s
"""


def backfill_odds() -> None:
    """Fetch ESPN closing lines for every final game and (re)load market_lines.
    captured_at is set to tipoff so v_closing_lines includes them."""
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT game_id, tipoff_utc, game_date_et FROM public.games_v2 "
                        "WHERE status='final' ORDER BY game_date_et")
            games = cur.fetchall()
    finally:
        conn.close()

    def fetch(g):
        gid, tip, gdate = g
        d = odds(gid)
        if not d:
            return None
        parsed = parse_close_line(d.get("items", []))
        if not parsed:
            return None
        captured = tip or dt.datetime.combine(gdate, dt.time(23, 0), tzinfo=ET)
        return (gid, captured, "espn", "espn", parsed["home_spread"],
                parsed["home_price"], parsed["away_price"],
                parsed["total_line"], parsed["over_price"], parsed["under_price"])

    rows = []
    total = len(games)
    with_total = 0
    for i in range(0, total, 200):
        chunk = games[i:i + 200]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for r in (r for r in ex.map(fetch, chunk) if r):
                rows.append(r)
                if r[7] is not None:
                    with_total += 1
        print(f"[..] odds {i + len(chunk)}/{total} (with line: {len(rows)}, "
              f"with total: {with_total})", flush=True)

    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.market_lines WHERE source='espn'")  # idempotent re-load
            psycopg2.extras.execute_values(cur, INSERT_LINE, rows, page_size=500)
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] odds backfill: {len(rows)}/{total} games got a line "
          f"({with_total} with totals)", flush=True)


def snapshot_espn_odds(days_back: int = 3, days_ahead: int = 2) -> None:
    """Append current ESPN spread+total lines for recent/upcoming games so live
    predictions have fresh lines. Append-only; v_latest_lines picks the newest."""
    today = dt.datetime.now(ET).date()
    lo, hi = today - dt.timedelta(days=days_back), today + dt.timedelta(days=days_ahead)
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT game_id, tipoff_utc, game_date_et FROM public.games_v2 "
                        "WHERE game_date_et BETWEEN %s AND %s ORDER BY game_date_et", (lo, hi))
            games = cur.fetchall()
    finally:
        conn.close()
    if not games:
        print(f"[OK] snapshot_espn_odds: no games in {lo}..{hi}", flush=True)
        return

    now_utc = dt.datetime.now(dt.timezone.utc)

    def fetch(g):
        gid, _tip, _gdate = g
        d = odds(gid)
        if not d:
            return None
        parsed = parse_close_line(d.get("items", []))
        if not parsed:
            return None
        return (gid, now_utc, "espn", "espn", parsed["home_spread"],
                parsed["home_price"], parsed["away_price"],
                parsed["total_line"], parsed["over_price"], parsed["under_price"])

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        rows = [r for r in ex.map(fetch, games) if r]
    if not rows:
        print(f"[OK] snapshot_espn_odds: no lines for {len(games)} games", flush=True)
        return
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, INSERT_LINE, rows, page_size=500)
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] snapshot_espn_odds: appended {len(rows)} lines ({lo}..{hi})", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", help="comma-separated, e.g. 2023-24,2024-25,2025-26")
    ap.add_argument("--days-back", type=int, default=3)
    ap.add_argument("--odds", action="store_true", help="backfill ESPN closing lines (spread+total) for all final games")
    ap.add_argument("--snapshot-odds", action="store_true", help="append current ESPN lines for recent/upcoming games")
    args = ap.parse_args()
    conn = db_connect()
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    if args.odds:
        backfill_odds()
    elif args.snapshot_odds:
        snapshot_espn_odds(args.days_back)
    elif args.seasons:
        backfill([s.strip() for s in args.seasons.split(",") if s.strip()])
    else:
        daily(args.days_back)


if __name__ == "__main__":
    main()
