import os, json, base64
from datetime import datetime
import pytz
import pandas as pd
import psycopg2
import pickle

from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

tw_tz = pytz.timezone("Asia/Taipei")

FEATURES = [
    "home_spread",
    "diff_pts",
    "diff_impact",
    "diff_recent_w",
    "diff_b2b",
    "pin_ok",
    "base_diff",
    "f_edge",
]

def pg_conn():
    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    db   = (os.environ.get("SUPABASE_DB") or "postgres").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    pw   = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    port_raw = (os.environ.get("SUPABASE_PORT") or "").strip()
    port = int(port_raw) if port_raw.isdigit() else 5432

    if not host or not user or not pw:
        raise RuntimeError("Missing DB env vars. Check GitHub Actions secrets.")

    return psycopg2.connect(
        host=host, dbname=db, user=user, password=pw, port=port,
        sslmode="require", connect_timeout=12
    )

def ensure_model_registry():
    sql = """
    CREATE TABLE IF NOT EXISTS model_registry (
      model_name TEXT PRIMARY KEY,
      model_version TEXT,
      payload_base64 TEXT,
      trained_rows INT,
      metrics JSONB,
      created_at_tw TEXT
    );
    """
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()

def main():
    ensure_model_registry()

    conn = pg_conn()
    try:
        df = pd.read_sql("""
            SELECT
              game_date_us,
              cover,
              home_score, away_score,
              home_spread,
              base_diff, f_edge,
              diff_pts, diff_impact, diff_recent_w, diff_b2b,
              COALESCE(pin_ok, 0) AS pin_ok
            FROM games
            WHERE cover IS NOT NULL
              AND home_score IS NOT NULL
              AND away_score IS NOT NULL
              AND home_spread IS NOT NULL
              AND base_diff IS NOT NULL
              AND f_edge IS NOT NULL
              AND diff_pts IS NOT NULL
              AND diff_impact IS NOT NULL
              AND diff_recent_w IS NOT NULL
              AND diff_b2b IS NOT NULL
        """, conn)

        n = len(df)
        if n < 300:
            print(f"[WARN] base model train skipped: not enough rows n={n} (<300)")
            return

        # label: exclude push to keep classification clean
        df["cover"] = df["cover"].astype(int)
        df = df[df["cover"].isin([0, 1])].copy()
        if len(df) < 250:
            print(f"[WARN] base model train skipped after filtering push: n={len(df)} (<250)")
            return

        X = df[FEATURES].astype(float)
        y = df["cover"].astype(int)

        # time split: last 20% validation (by game_date_us string; ok as it’s consistent)
        df = df.assign(_idx=range(len(df))).sort_values("game_date_us").reset_index(drop=True)
        cut = int(len(df) * 0.8)
        train = df.iloc[:cut]
        valid = df.iloc[cut:] if len(df) - cut >= 50 else None

        X_train = train[FEATURES].astype(float)
        y_train = train["cover"].astype(int)

        # Pipeline: scaler + logistic regression
        model = Pipeline(steps=[
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(
                max_iter=2000,
                C=1.0,
                solver="lbfgs"
            ))
        ])
        model.fit(X_train, y_train)

        # simple metrics
        metrics = {
            "type": "logistic_regression",
            "features": FEATURES,
            "rows_total": int(len(df)),
            "rows_train": int(len(train)),
        }

        if valid is not None:
            X_valid = valid[FEATURES].astype(float)
            y_valid = valid["cover"].astype(int)
            acc = float((model.predict(X_valid) == y_valid).mean())
            metrics["valid_acc"] = acc
            metrics["valid_rows"] = int(len(valid))
            metrics["valid_cutoff_game_date_us"] = str(valid["game_date_us"].iloc[0])

        payload = base64.b64encode(pickle.dumps(model)).decode("utf-8")
        now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO model_registry (model_name, model_version, payload_base64, trained_rows, metrics, created_at_tw)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (model_name) DO UPDATE SET
                  model_version=EXCLUDED.model_version,
                  payload_base64=EXCLUDED.payload_base64,
                  trained_rows=EXCLUDED.trained_rows,
                  metrics=EXCLUDED.metrics,
                  created_at_tw=EXCLUDED.created_at_tw
            """, (
                "cover_prob_base_model",
                now_tw,
                payload,
                int(len(df)),
                json.dumps(metrics),
                now_tw
            ))

        conn.commit()
        print(f"[OK] trained base model rows={len(df)} version={now_tw} metrics={metrics}")

    finally:
        conn.close()

if __name__ == "__main__":
    main()
