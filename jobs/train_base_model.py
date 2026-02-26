#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import base64
import pickle

import numpy as np
import psycopg2
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error


def db_connect():
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if db_url:
        return psycopg2.connect(db_url)

    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    dbname = (os.environ.get("SUPABASE_DB") or "").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    password = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    port = (os.environ.get("SUPABASE_PORT") or "5432").strip()

    if not all([host, dbname, user, password, port]):
        raise RuntimeError("DB env missing")

    return psycopg2.connect(host=host, dbname=dbname, user=user, password=password, port=int(port), sslmode="require")


def now_tw_str():
    import datetime as dt
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Taipei")
        return dt.datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


FEATURES = [
    "home_pts_sum", "away_pts_sum",
    "home_impact_mean", "away_impact_mean",
    "home_b2b", "away_b2b",
    "home_recent_w", "away_recent_w",
    "home_ts_pct", "away_ts_pct",
    "home_orb_rate", "away_orb_rate",
    "home_usage_proxy", "away_usage_proxy",
    "home_onoff_proxy", "away_onoff_proxy",
]


def main():
    min_rows = int((os.environ.get("BASE_MIN_ROWS") or "200").strip())
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT margin, {",".join(FEATURES)}
                FROM public.games
                WHERE margin IS NOT NULL
                  AND home_pts_sum IS NOT NULL AND away_pts_sum IS NOT NULL
                  AND home_impact_mean IS NOT NULL AND away_impact_mean IS NOT NULL
                  AND home_b2b IS NOT NULL AND away_b2b IS NOT NULL
                  AND home_recent_w IS NOT NULL AND away_recent_w IS NOT NULL
                  AND home_ts_pct IS NOT NULL AND away_ts_pct IS NOT NULL
                  AND home_orb_rate IS NOT NULL AND away_orb_rate IS NOT NULL
                  AND home_usage_proxy IS NOT NULL AND away_usage_proxy IS NOT NULL
                  AND home_onoff_proxy IS NOT NULL AND away_onoff_proxy IS NOT NULL
            """)
            rows = cur.fetchall()

        if len(rows) < min_rows:
            print(f"[WARN] base train rows too small: {len(rows)} < {min_rows}; skip")
            return

        y = np.array([r[0] for r in rows], dtype=float)
        X = []
        for r in rows:
            feat = []
            # booleans -> 0/1
            for i, name in enumerate(FEATURES, start=1):
                v = r[i]
                if isinstance(v, bool):
                    v = 1.0 if v else 0.0
                feat.append(float(v))
            X.append(feat)
        X = np.array(X, dtype=float)

        model = HistGradientBoostingRegressor(
            max_depth=4,
            learning_rate=0.06,
            max_iter=250,
            random_state=42
        )
        model.fit(X, y)

        pred = model.predict(X)
        mae = float(mean_absolute_error(y, pred))

        payload = base64.b64encode(pickle.dumps(model)).decode("utf-8")
        metrics = {"mae_in_sample": mae}

        conn2 = db_connect()
        try:
            with conn2:
                with conn2.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.model_registry(model_name, model_version, payload_base64, trained_rows, metrics, created_at_tw)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(model_name) DO UPDATE SET
                          model_version=EXCLUDED.model_version,
                          payload_base64=EXCLUDED.payload_base64,
                          trained_rows=EXCLUDED.trained_rows,
                          metrics=EXCLUDED.metrics,
                          created_at_tw=EXCLUDED.created_at_tw
                        """,
                        ("margin_base_model", now_tw_str(), payload, len(rows), json.dumps(metrics), now_tw_str())
                    )
            print(f"[OK] trained margin_base_model rows={len(rows)} mae={mae:.3f}")
        finally:
            conn2.close()

    finally:
        conn.close()


if __name__ == "__main__":
    main()
