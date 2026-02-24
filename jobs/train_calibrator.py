#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import base64
import pickle

import numpy as np
import psycopg2
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss


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


def main():
    min_rows = int((os.environ.get("CAL_MIN_ROWS") or "200").strip())

    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f_edge, cover
                FROM public.games
                WHERE cover IS NOT NULL AND f_edge IS NOT NULL
            """)
            rows = cur.fetchall()

        if len(rows) < min_rows:
            print(f"[WARN] calibrator train rows too small: {len(rows)} < {min_rows}; skip")
            return

        x = np.array([float(r[0]) for r in rows], dtype=float)

        # cover: 1 home cover, 0 not, 2 push -> 0.5
        y = []
        for _, c in rows:
            if c == 2:
                y.append(0.5)
            else:
                y.append(float(c))
        y = np.array(y, dtype=float)

        ir = IsotonicRegression(out_of_bounds="clip")
        ir.fit(x, y)

        p = ir.predict(x)
        brier = float(brier_score_loss((y >= 0.5).astype(int), p))

        payload = base64.b64encode(pickle.dumps(ir)).decode("utf-8")
        metrics = {"brier_in_sample": brier}

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
                        ("cover_prob_calibrator", now_tw_str(), payload, len(rows), json.dumps(metrics), now_tw_str())
                    )
            print(f"[OK] trained cover_prob_calibrator rows={len(rows)} brier={brier:.4f}")
        finally:
            conn2.close()

    finally:
        conn.close()


if __name__ == "__main__":
    main()
