#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jobs/train_calibrator.py

Trains isotonic regression to calibrate:
  P(home_cover) = f(f_edge)

Uses rows where:
  cover IS NOT NULL AND f_edge IS NOT NULL

push (cover=2) treated as 0.5 target.
"""

import os
import json
import base64
import pickle
import datetime as dt

import psycopg2
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss


def now_tw_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Taipei")
        return dt.datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


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
        raise RuntimeError("DB env missing: set DATABASE_URL or SUPABASE_*")

    return psycopg2.connect(host=host, dbname=dbname, user=user, password=password, port=int(port), sslmode="require")


UPSERT_MODEL_SQL = """
INSERT INTO public.model_registry(model_name, model_version, payload_base64, trained_rows, metrics, created_at_tw)
VALUES (%(model_name)s, %(model_version)s, %(payload_base64)s, %(trained_rows)s, %(metrics)s, %(created_at_tw)s)
ON CONFLICT(model_name) DO UPDATE SET
  model_version = EXCLUDED.model_version,
  payload_base64 = EXCLUDED.payload_base64,
  trained_rows = EXCLUDED.trained_rows,
  metrics = EXCLUDED.metrics,
  created_at_tw = EXCLUDED.created_at_tw;
"""


def main():
    min_rows = int((os.environ.get("CAL_MIN_ROWS") or "200").strip())
    max_rows = int((os.environ.get("CAL_MAX_ROWS") or "10000").strip())

    conn = db_connect()
    try:
        df = pd.read_sql("""
            SELECT f_edge, cover
            FROM public.games
            WHERE cover IS NOT NULL
              AND f_edge IS NOT NULL
        """, conn)
    finally:
        conn.close()

    if df.empty or len(df) < min_rows:
        print(f"[WARN] not enough rows for calibrator rows={len(df)} min_rows={min_rows}")
        return

    if len(df) > max_rows:
        df = df.sample(n=max_rows, random_state=42)

    x = df["f_edge"].astype(float).values
    y_raw = df["cover"].astype(int).values
    y = []
    for v in y_raw:
        if v == 2:
            y.append(0.5)
        else:
            y.append(float(v))
    y = pd.Series(y).values

    ir = IsotonicRegression(out_of_bounds="clip")
    ir.fit(x, y)

    p = ir.predict(x)
    # brier for informational only
    brier = float(brier_score_loss((y > 0.5).astype(int), p))

    metrics = {
        "rows": int(len(df)),
        "brier": brier,
        "model": "IsotonicRegression(out_of_bounds=clip)",
    }

    payload_b64 = base64.b64encode(pickle.dumps(ir)).decode("utf-8")
    model_version = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    created_at = now_tw_str()

    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(UPSERT_MODEL_SQL, {
                    "model_name": "cover_prob_calibrator",
                    "model_version": model_version,
                    "payload_base64": payload_b64,
                    "trained_rows": int(len(df)),
                    "metrics": json.dumps(metrics),
                    "created_at_tw": created_at,
                })
        print(f"[OK] calibrator trained rows={len(df)} brier={brier:.4f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
