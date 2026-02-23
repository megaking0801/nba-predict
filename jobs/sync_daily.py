#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import datetime as dt
import requests
import psycopg2
import psycopg2.extras
import pickle
import base64
import pandas as pd
from typing import Dict, Tuple, Optional, List

# =========================================================
# Utility
# =========================================================

def norm_name(s: str) -> str:
    if s is None:
        return ""
    s = s.strip().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def us_eastern_today():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(tz=ZoneInfo("America/New_York")).date()

def now_tw_str():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(tz=ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")

# =========================================================
# DB
# =========================================================

def db_connect():
    return psycopg2.connect(
        host=os.environ["SUPABASE_HOST"],
        dbname=os.environ["SUPABASE_DB"],
        user=os.environ["SUPABASE_USER"],
        password=os.environ["SUPABASE_PASSWORD"],
        port=int(os.environ["SUPABASE_PORT"]),
        sslmode="require",
    )

def load_model(model_name):
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT payload_base64
                FROM model_registry
                WHERE model_name = %s
            """, (model_name,))
            row = cur.fetchone()
            if not row:
                return None
            return pickle.loads(base64.b64decode(row[0]))
    finally:
        conn.close()

# =========================================================
# UPSERT SQL（加入 p_raw, p_cal）
# =========================================================

UPSERT_SQL = """
INSERT INTO public.games (
    game_id,
    game_date_us,
    season,
    away_abbr,
    home_abbr,
    away_name,
    home_name,
    home_spread,
    home_odds,
    away_odds,
    line_source,
    status,
    away_score,
    home_score,
    p_raw,
    p_cal,
    created_at_tw,
    updated_at_tw
) VALUES (
    %(game_id)s,
    %(game_date_us)s,
    %(season)s,
    %(away_abbr)s,
    %(home_abbr)s,
    %(away_name)s,
    %(home_name)s,
    %(home_spread)s,
    %(home_odds)s,
    %(away_odds)s,
    %(line_source)s,
    %(status)s,
    %(away_score)s,
    %(home_score)s,
    %(p_raw)s,
    %(p_cal)s,
    %(created_at_tw)s,
    %(updated_at_tw)s
)
ON CONFLICT (game_id)
DO UPDATE SET
    home_spread = EXCLUDED.home_spread,
    home_odds   = EXCLUDED.home_odds,
    away_odds   = EXCLUDED.away_odds,
    line_source = EXCLUDED.line_source,
    status      = EXCLUDED.status,
    away_score  = EXCLUDED.away_score,
    home_score  = EXCLUDED.home_score,
    p_raw       = EXCLUDED.p_raw,
    p_cal       = EXCLUDED.p_cal,
    updated_at_tw = EXCLUDED.updated_at_tw;
"""

# =========================================================
# MAIN
# =========================================================

def main():

    anchor = us_eastern_today()
    season = os.environ.get("NBA_SEASON", "2025-26")
    ts = now_tw_str()

    print("[INFO] loading models...")
    base_model = load_model("cover_base_model")
    calibrator = load_model("cover_prob_calibrator")
    print(f"[INFO] base_model={base_model is not None}")
    print(f"[INFO] calibrator={calibrator is not None}")

    # ESPN scoreboard
    ymd = anchor.strftime("%Y%m%d")
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={ymd}"
    r = requests.get(url, timeout=20)
    data = r.json()
    events = data.get("events", [])

    rows = []

    for ev in events:
        comp = ev["competitions"][0]
        competitors = comp["competitors"]

        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        home_abbr = home["team"]["abbreviation"]
        away_abbr = away["team"]["abbreviation"]

        status = "final" if comp["status"]["type"]["completed"] else "scheduled"

        home_score = int(home["score"]) if status == "final" else None
        away_score = int(away["score"]) if status == "final" else None

        spread = 0.0  # fallback

        # ===== ML PREDICT =====
        p_raw = None
        p_cal = None

        if base_model:
            try:
                X = pd.DataFrame([{"home_spread": spread}])
                p_raw = float(base_model.predict_proba(X)[0][1])
            except:
                pass

        if calibrator and p_raw is not None:
            try:
                p_cal = float(calibrator.predict([p_raw])[0])
            except:
                pass

        rows.append({
            "game_id": f"{ymd}_{away_abbr}_{home_abbr}",
            "game_date_us": anchor.strftime("%m/%d/%Y"),
            "season": season,
            "away_abbr": away_abbr,
            "home_abbr": home_abbr,
            "away_name": away["team"]["displayName"],
            "home_name": home["team"]["displayName"],
            "home_spread": spread,
            "home_odds": 1.90,
            "away_odds": 1.90,
            "line_source": "Fallback ⚠️",
            "status": status,
            "away_score": away_score,
            "home_score": home_score,
            "p_raw": p_raw,
            "p_cal": p_cal,
            "created_at_tw": ts,
            "updated_at_tw": ts
        })

    conn = db_connect()
    with conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows)

    print(f"[OK] sync complete rows={len(rows)}")

if __name__ == "__main__":
    main()
