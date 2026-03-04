#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import datetime as dt
from typing import List, Optional, Dict, Any

import requests
import psycopg2
from jobs.db_utils import db_connect
from jobs.time_utils import now_tw_str, us_eastern_today
import psycopg2.extras


def ensure_schema():
    ddl = """
    CREATE TABLE IF NOT EXISTS public.games (
        game_id TEXT PRIMARY KEY,
        game_date_us TEXT,
        season TEXT,
        away_abbr TEXT,
        home_abbr TEXT,
        away_name TEXT,
        home_name TEXT,

        home_spread DOUBLE PRECISION,
        home_odds DOUBLE PRECISION,
        away_odds DOUBLE PRECISION,
        line_source TEXT,

        status TEXT,
        away_score INTEGER,
        home_score INTEGER,

        margin INTEGER,
        cover INTEGER,
        settled_at_tw TEXT,

        created_at_tw TEXT,
        updated_at_tw TEXT,
        game_date_tw TEXT
    );
    """
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS margin INTEGER;")
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS cover INTEGER;")
                cur.execute("ALTER TABLE public.games ADD COLUMN IF NOT EXISTS settled_at_tw TEXT;")
        print("[INFO] schema ensured")
    finally:
        conn.close()


def fetch_espn_scoreboard(date_us: dt.date) -> List[dict]:
    ymd = date_us.strftime("%Y%m%d")
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    r = requests.get(url, params={"dates": ymd, "limit": 300}, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data.get("events") or []


def parse_finals(events: List[dict], date_us: dt.date) -> List[Dict[str, Any]]:
    out = []
    for ev in events:
        espn_event_id = str(ev.get("id") or "").strip()
        competitions = ev.get("competitions") or []
        if not competitions:
            continue
        comp = competitions[0]
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        home_team = home.get("team") or {}
        away_team = away.get("team") or {}
        home_abbr = home_team.get("abbreviation")
        away_abbr = away_team.get("abbreviation")
        if not home_abbr or not away_abbr:
            continue

        st = (comp.get("status") or {}).get("type") or {}
        state = (st.get("state") or "").lower()
        completed = bool(st.get("completed"))
        is_final = completed or state == "post"
        if not is_final:
            continue

        try:
            hs = int(home.get("score")) if home.get("score") is not None else None
            as_ = int(away.get("score")) if away.get("score") is not None else None
        except Exception as e:
            print(f"[WARN] score parse failed date={date_us.isoformat()} home={home_abbr} away={away_abbr} err={e}")
            hs, as_ = None, None

        if hs is None or as_ is None:
            continue

        game_id = espn_event_id or f"{date_us.strftime('%Y%m%d')}_{away_abbr}_{home_abbr}"
        out.append({
            "game_id": game_id,
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "home_score": hs,
            "away_score": as_,
            "status": "final",
            "margin": int(hs - as_),
        })

    return out


def load_spreads(game_ids: List[str]) -> Dict[str, Optional[float]]:
    if not game_ids:
        return {}
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT game_id, home_spread
                FROM public.games
                WHERE game_id = ANY(%s)
                """,
                (game_ids,),
            )
            rows = cur.fetchall()
        return {gid: (float(sp) if sp is not None else None) for gid, sp in rows}
    finally:
        conn.close()


def write_settles(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return

    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    """
                    UPDATE public.games
                    SET status=%(status)s,
                        home_score=%(home_score)s,
                        away_score=%(away_score)s,
                        margin=%(margin)s,
                        cover=%(cover)s,
                        settled_at_tw=%(settled_at_tw)s,
                        updated_at_tw=%(settled_at_tw)s
                    WHERE game_id=%(game_id)s
                    """,
                    rows,
                    page_size=200,
                )
        print(f"[OK] settle updated rows={len(rows)}")
    finally:
        conn.close()


def main():
    ensure_schema()

    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    anchor = dt.datetime.strptime(override, "%m/%d/%Y").date() if override else us_eastern_today()

    past_days = int((os.environ.get("SETTLE_PAST_DAYS") or "180").strip())
    if past_days < 1:
        past_days = 1

    dates = [anchor - dt.timedelta(days=i) for i in range(past_days)]
    ts = now_tw_str()

    total = 0
    for d in dates:
        try:
            events = fetch_espn_scoreboard(d)
            finals = parse_finals(events, d)
        except Exception as e:
            print(f"[WARN] ESPN failed date={d.isoformat()} err={e}")
            continue

        if not finals:
            continue

        spreads = load_spreads([x["game_id"] for x in finals])

        payload = []
        for f in finals:
            sp = spreads.get(f["game_id"])
            cover = None
            # cover only if spread exists
            if sp is not None:
                # home covers if home_margin + home_spread > 0
                v = f["margin"] + sp
                if abs(v) < 1e-9:
                    cover = 2  # push
                elif v > 0:
                    cover = 1
                else:
                    cover = 0

            payload.append({
                **f,
                "cover": cover,
                "settled_at_tw": ts,
            })

        write_settles(payload)
        total += len(payload)

    print(f"[OK] settle complete total={total}")


if __name__ == "__main__":
    main()
