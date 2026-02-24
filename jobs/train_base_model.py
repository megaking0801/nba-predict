#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jobs/train_base_model.py

Trains a Gradient Boosting regression model to predict:
  margin = home_score - away_score

Features (from public.games):
  home_pts_sum, away_pts_sum,
  home_impact_mean, away_impact_mean,
  home_b2b, away_b2b,
  home_recent_w, away_recent_w

Model:
  sklearn.ensemble.HistGradientBoostingRegressor

Saves to public.model_registry:
  model_name = "margin_base_model"
"""

import os
import json
import base64
import pickle
import datetime as dt

import psycopg2
import pandas as pd

from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error


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
    min_rows = int((os.environ.get("BASE_MIN_ROWS") or "200").strip())
    max_rows = int((os.environ.get("BASE_MAX_ROWS") or "5000").strip())

    # pull training data
    conn = db_connect()
    try:
        df = pd.read_sql("""
            SELECT
              margin,
              home_pts_sum, away_pts_sum,
              home_impact_mean, away_impact_mean,
              home_b2b, away_b2b,
              home_recent_w, away_recent_w
            FROM public.games
            WHERE margin IS NOT NULL
              AND home_pts_sum IS NOT NULL AND away_pts_sum IS NOT NULL
              AND home_impact_mean IS NOT NULL AND away_impact_mean IS NOT NULL
              AND home_b2b IS NOT NULL AND away_b2b IS NOT NULL
              AND home_recent_w IS NOT NULL AND away_recent_w IS NOT NULL
            ORDER BY game_date_us DESC
        """, conn)
    finally:
        conn.close()

    if df.empty or len(df) < min_rows:
        print(f"[WARN] not enough rows to train base model. rows={len(df)} min_rows={min_rows}")
        return

    # cap rows to keep training stable
    if len(df) > max_rows:
        df = df.head(max_rows).copy()

    y = df["margin"].astype(float).values
    X = df[[
        "home_pts_sum", "away_pts_sum",
        "home_impact_mean", "away_impact_mean",
        "home_b2b", "away_b2b",
        "home_recent_w", "away_recent_w",
    ]].astype(float).values

    X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2, random_state=42)

    model = HistGradientBoostingRegressor(
        max_depth=6,
        learning_rate=0.06,
        max_iter=300,
        min_samples_leaf=20,
        l2_regularization=0.2,
        random_state=42,
    )
    model.fit(X_train, y_train)

    pred = model.predict(X_val)
    mae = float(mean_absolute_error(y_val, pred))
    rmse = float(mean_squared_error(y_val, pred, squared=False))

    metrics = {
        "mae": mae,
        "rmse": rmse,
        "rows_total": int(len(df)),
        "rows_train": int(len(y_train)),
        "rows_val": int(len(y_val)),
        "feature_order": [
            "home_pts_sum", "away_pts_sum",
            "home_impact_mean", "away_impact_mean",
            "home_b2b", "away_b2b",
            "home_recent_w", "away_recent_w",
        ],
        "model": "HistGradientBoostingRegressor",
        "params": {
            "max_depth": 6,
            "learning_rate": 0.06,
            "max_iter": 300,
            "min_samples_leaf": 20,
            "l2_regularization": 0.2,
        }
    }

    payload_b64 = base64.b64encode(pickle.dumps(model)).decode("utf-8")
    model_version = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    created_at = now_tw_str()

    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(UPSERT_MODEL_SQL, {
                    "model_name": "margin_base_model",
                    "model_version": model_version,
                    "payload_base64": payload_b64,
                    "trained_rows": int(len(df)),
                    "metrics": json.dumps(metrics),
                    "created_at_tw": created_at,
                })
        print(f"[OK] base model trained rows={len(df)} mae={mae:.3f} rmse={rmse:.3f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
