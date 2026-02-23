import os, json, base64, math
from datetime import datetime
import pytz
import pandas as pd
import psycopg2
import pickle
from sklearn.isotonic import IsotonicRegression

tw_tz = pytz.timezone("Asia/Taipei")

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

def brier_score(y_true, p):
    # y_true can be 0/0.5/1; brier still meaningful
    return float(((y_true - p) ** 2).mean())

def log_loss(y_true, p, eps=1e-12):
    # handle 0.5 label (push) by treating it as soft label
    # logloss = -[y*log(p) + (1-y)*log(1-p)]
    p = p.clip(eps, 1 - eps)
    return float((-(y_true * p.apply(math.log) + (1 - y_true) * (1 - p).apply(math.log))).mean())

def main():
    ensure_model_registry()

    conn = pg_conn()
    try:
        df = pd.read_sql("""
            SELECT
              game_date,
              home_score,
              away_score,
              f_edge,
              cover
            FROM games
            WHERE cover IS NOT NULL
              AND f_edge IS NOT NULL
              AND home_score IS NOT NULL
              AND away_score IS NOT NULL
        """, conn)

        n = len(df)
        if n < 200:
            print(f"[WARN] train skipped: not enough settled rows n={n} (<200)")
            return

        # cover: 1=home cover, 0=not, 2=push -> 0.5
        y = df["cover"].astype(int).map(lambda c: 0.5 if c == 2 else (1.0 if c == 1 else 0.0)).astype(float)
        x = df["f_edge"].astype(float)

        # time split (last 20% as validation)
        df2 = df.assign(y=y, x=x).sort_values("game_date").reset_index(drop=True)
        cut = int(len(df2) * 0.8)
        train = df2.iloc[:cut]
        valid = df2.iloc[cut:]

        if len(valid) < 30:
            # avoid meaningless validation
            train = df2
            valid = None

        iso = IsotonicRegression(y_min=0.05, y_max=0.95, increasing=True, out_of_bounds="clip")
        iso.fit(train["x"], train["y"])

        # evaluation
        train_p = pd.Series(iso.predict(train["x"]), index=train.index)
        train_metrics = {
            "brier": brier_score(train["y"], train_p),
            "logloss": log_loss(train["y"], train_p),
            "rows": int(len(train)),
        }

        valid_metrics = None
        if valid is not None and len(valid) > 0:
            valid_p = pd.Series(iso.predict(valid["x"]), index=valid.index)
            valid_metrics = {
                "brier": brier_score(valid["y"], valid_p),
                "logloss": log_loss(valid["y"], valid_p),
                "rows": int(len(valid)),
                "cutoff_game_date": str(valid["game_date"].iloc[0]),
            }

        payload = base64.b64encode(pickle.dumps(iso)).decode("utf-8")
        now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

        metrics = {
            "type": "isotonic",
            "target": "home_cover_prob",
            "note": "P(home_cover)=f(f_edge) calibrated from settled games",
            "push": "0.5",
            "n_total": int(n),
            "train": train_metrics,
            "valid": valid_metrics,
            "assumption": "increasing=True means larger f_edge -> higher P(home_cover)",
        }

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
                "cover_prob_calibrator",
                now_tw,
                payload,
                int(n),
                json.dumps(metrics),
                now_tw
            ))

        conn.commit()
        print(f"[OK] trained calibrator rows={n} version={now_tw} train={train_metrics} valid={valid_metrics}")

    finally:
        conn.close()

if __name__ == "__main__":
    main()
