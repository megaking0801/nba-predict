"""Snapshot current spreads/odds from the Odds API into append-only
market_lines. One request covers all upcoming games across the preferred
books. A new row is inserted only when a book's line actually moved, so the
table stays small while open/close/drift remain derivable.

Gated by USE_ODDS=1 + ODDS_API_KEY (same convention as the legacy pipeline).
Run: python -m jobs.snapshot_lines
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from typing import Dict, Optional, Tuple

import requests

from jobs.config import CONFIG
from jobs.db_utils import db_connect
from jobs.schema import ensure_schema
from jobs.teams import abbr_from_team_name
from jobs.tz import et_date_of

ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"


def fetch_odds(api_key: str) -> Tuple[list, dict]:
    params = {
        "apiKey": api_key,
        "regions": "us,eu",
        "markets": "spreads",
        "oddsFormat": "decimal",
        "bookmakers": ",".join(CONFIG.BOOK_PREFERENCE),
    }
    r = requests.get(ODDS_URL, params=params, timeout=25)
    r.raise_for_status()
    quota = {
        "remaining": r.headers.get("x-requests-remaining"),
        "used": r.headers.get("x-requests-used"),
    }
    return r.json(), quota


def find_game_id(cur, date_et: dt.date, home_abbr: str, away_abbr: str) -> Optional[str]:
    cur.execute(
        """
        SELECT game_id FROM public.games_v2
        WHERE home_abbr = %s AND away_abbr = %s
          AND game_date_et BETWEEN %s AND %s
        ORDER BY game_date_et
        LIMIT 1
        """,
        (home_abbr, away_abbr, date_et - dt.timedelta(days=1), date_et + dt.timedelta(days=1)),
    )
    row = cur.fetchone()
    return row[0] if row else None


def latest_book_lines(cur, game_id: str) -> Dict[str, tuple]:
    """book -> (home_spread, home_price, away_price) of the most recent oddsapi row."""
    cur.execute(
        """
        SELECT DISTINCT ON (book) book, home_spread, home_price, away_price
        FROM public.market_lines
        WHERE game_id = %s AND source = 'oddsapi'
        ORDER BY book, captured_at DESC
        """,
        (game_id,),
    )
    return {b: (s, hp, ap) for b, s, hp, ap in cur.fetchall()}


def parse_event(ev: dict) -> Optional[dict]:
    home_abbr = abbr_from_team_name(ev.get("home_team") or "")
    away_abbr = abbr_from_team_name(ev.get("away_team") or "")
    if not home_abbr or not away_abbr:
        print(f"[WARN] unmatched team names: {ev.get('home_team')!r} vs "
              f"{ev.get('away_team')!r}", flush=True)
        return None
    try:
        commence = dt.datetime.fromisoformat(str(ev.get("commence_time")).replace("Z", "+00:00"))
    except ValueError:
        return None

    books = {}
    for bm in ev.get("bookmakers") or []:
        key = bm.get("key")
        market = next((m for m in bm.get("markets") or [] if m.get("key") == "spreads"), None)
        if not key or not market:
            continue
        home_point = home_price = away_price = None
        for o in market.get("outcomes") or []:
            if abbr_from_team_name(o.get("name") or "") == home_abbr:
                home_point, home_price = o.get("point"), o.get("price")
            elif abbr_from_team_name(o.get("name") or "") == away_abbr:
                away_price = o.get("price")
        if home_point is None:
            continue
        books[key] = (float(home_point),
                      float(home_price) if home_price else None,
                      float(away_price) if away_price else None)
    return {"home_abbr": home_abbr, "away_abbr": away_abbr,
            "date_et": et_date_of(commence), "books": books}


def main() -> None:
    if (os.environ.get("USE_ODDS") or "0").strip() != "1":
        print("[INFO] USE_ODDS != 1; skipping line snapshot", flush=True)
        return
    api_key = (os.environ.get("ODDS_API_KEY") or "").strip()
    if not api_key:
        print("[WARN] ODDS_API_KEY missing; skipping line snapshot", flush=True)
        return

    events, quota = fetch_odds(api_key)
    print(f"[INFO] odds events={len(events)} quota_remaining={quota['remaining']}", flush=True)

    conn = db_connect()
    inserted = unchanged = unmatched = 0
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            for ev in events:
                parsed = parse_event(ev)
                if not parsed or not parsed["books"]:
                    unmatched += 1
                    continue
                game_id = find_game_id(cur, parsed["date_et"],
                                       parsed["home_abbr"], parsed["away_abbr"])
                if not game_id:
                    print(f"[WARN] no games_v2 match for {parsed['away_abbr']}@"
                          f"{parsed['home_abbr']} {parsed['date_et']}", flush=True)
                    unmatched += 1
                    continue
                existing = latest_book_lines(cur, game_id)
                for book, (spread, hp, ap) in parsed["books"].items():
                    if existing.get(book) == (spread, hp, ap):
                        unchanged += 1
                        continue
                    cur.execute(
                        """
                        INSERT INTO public.market_lines
                          (game_id, source, book, home_spread, home_price, away_price)
                        VALUES (%s, 'oddsapi', %s, %s, %s, %s)
                        """,
                        (game_id, book, spread, hp, ap),
                    )
                    inserted += 1
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] snapshot_lines inserted={inserted} unchanged={unchanged} "
          f"unmatched={unmatched}", flush=True)


if __name__ == "__main__":
    main()
