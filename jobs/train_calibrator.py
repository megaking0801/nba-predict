import os, base64, json
from datetime import datetime
import pytz
import pandas as pd
import psycopg2

from sklearn.isotonic import IsotonicRegression
import pickle

tw_tz = pytz.timezone("Asia/Taipei")

def pg_conn():
    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    db   = (os.environ.get("SUPABASE_DB") or "postgres").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    pw   = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    port_raw = (os.environ.get("SUPABASE_PORT") or "").strip()
    port = int(port_raw) if port_raw.isdigit() else 5432

    return psycopg2.connect(
        host=host, dbname=db, user=user, password=pw, port=port,
        sslmode="require", connect_timeout=10
    )

def main():
    conn = pg_conn()
    try:
        # 只拿已結算（final）的資料
        df = pd.read_sql("""
            select f_edge, cover
            from games
            where cover is not null
              and f_edge is not null
            """, conn)

        if df.empty or len(df) < 200:
            print(f"[WARN] not enough settled rows to train calibrator: n={len(df)}")
            return

        # cover: 1=home cover, 0=not, 2=push
        # 這裡把 push(2) 當 0.5（也可選擇丟掉 push）
        y = df["cover"].map(lambda c: 0.5 if int(c) == 2 else (1.0 if int(c) == 1 else 0.0)).astype(float)
        x = df["f_edge"].astype(float)

        # isotonic regression: 讓 P 隨 f_edge 單調增加
        iso = IsotonicRegression(y_min=0.05, y_max=0.95, increasing=True, out_of_bounds="clip")
        iso.fit(x, y)

        payload = base64.b64encode(pickle.dumps(iso)).decode("utf-8")
        now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

        metrics = {
            "note": "IsotonicRegression calibrator: P(home_cover) = f(f_edge)",
            "rows": int(len(df)),
            "push_handling": "push=0.5",
        }

        with conn.cursor() as cur:
            cur.execute("""
                insert into model_registry (model_name, model_version, payload_base64, trained_rows, metrics, created_at_tw)
                values (%s, %s, %s, %s, %s::jsonb, %s)
                on conflict (model_name) do update set
                  model_version=excluded.model_version,
                  payload_base64=excluded.payload_base64,
                  trained_rows=excluded.trained_rows,
                  metrics=excluded.metrics,
                  created_at_tw=excluded.created_at_tw
            """, (
                "cover_prob_calibrator",
                now_tw,   # 用時間當版本
                payload,
                int(len(df)),
                json.dumps(metrics),
                now_tw
            ))
        conn.commit()
        print(f"[OK] trained calibrator rows={len(df)} version={now_tw}")

    finally:
        conn.close()

if __name__ == "__main__":
    main()
