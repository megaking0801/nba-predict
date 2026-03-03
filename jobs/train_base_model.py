#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import base64
import pickle
import datetime as dt

import numpy as np
import psycopg2
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import mean_absolute_error, brier_score_loss
from sklearn.model_selection import KFold


CORE_FEATURES = [
    "home_pts_sum", "away_pts_sum",
    "home_impact_mean", "away_impact_mean",
    "home_b2b", "away_b2b",
    "home_recent_w", "away_recent_w",
    "home_ts_pct", "away_ts_pct",
    "home_orb_rate", "away_orb_rate",
    "home_usage_proxy", "away_usage_proxy",
    "home_onoff_proxy", "away_onoff_proxy",
]

ADV_FEATURES = [
    "home_starters_out", "away_starters_out",      # starter/rest signal
    "home_minutes_proj", "away_minutes_proj",      # minutes projection signal
    "home_spread", "home_odds", "away_odds",     # market base
    "spread_move", "home_odds_move", "away_odds_move",  # market drift
]

FEATURES = CORE_FEATURES + ADV_FEATURES


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
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Taipei")
        return dt.datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def get_column_set(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='games'
        """)
        return {r[0] for r in cur.fetchall()}


def expr(colset, name, fallback="0.0"):
    return f"COALESCE({name}, {fallback}) AS {name}" if name in colset else f"{fallback}::double precision AS {name}"


def load_training_rows(conn):
    colset = get_column_set(conn)

    select_cols = [
        "margin",
        "cover",
        "game_date_us",
    ]

    # core + existing market
    for c in CORE_FEATURES:
        select_cols.append(expr(colset, c, "0.0"))

    # starter/minutes optional
    select_cols.append(expr(colset, "home_starters_out", "0.0"))
    select_cols.append(expr(colset, "away_starters_out", "0.0"))
    select_cols.append(expr(colset, "home_minutes_proj", "0.0"))
    select_cols.append(expr(colset, "away_minutes_proj", "0.0"))

    # base market
    select_cols.append(expr(colset, "home_spread", "0.0"))
    select_cols.append(expr(colset, "home_odds", "1.9"))
    select_cols.append(expr(colset, "away_odds", "1.9"))

    # market drift from open->now if open columns exist
    if ("open_home_spread" in colset) and ("home_spread" in colset):
        select_cols.append("COALESCE(home_spread - open_home_spread, 0.0) AS spread_move")
    else:
        select_cols.append("0.0::double precision AS spread_move")

    if ("open_home_odds" in colset) and ("home_odds" in colset):
        select_cols.append("COALESCE(home_odds - open_home_odds, 0.0) AS home_odds_move")
    else:
        select_cols.append("0.0::double precision AS home_odds_move")

    if ("open_away_odds" in colset) and ("away_odds" in colset):
        select_cols.append("COALESCE(away_odds - open_away_odds, 0.0) AS away_odds_move")
    else:
        select_cols.append("0.0::double precision AS away_odds_move")

    sql = f"""
        SELECT {", ".join(select_cols)}
        FROM public.games
        WHERE margin IS NOT NULL
    """

    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    return rows


def parse_date_us(s):
    try:
        return dt.datetime.strptime(str(s), "%m/%d/%Y").date()
    except Exception:
        return None


def build_arrays(rows):
    # Row layout: margin, cover, game_date_us, then FEATURES in order
    y_margin = np.array([float(r[0]) for r in rows], dtype=float)
    cover_raw = [r[1] for r in rows]
    game_dates = [parse_date_us(r[2]) for r in rows]

    X = []
    for r in rows:
        feat = []
        for i in range(3, 3 + len(FEATURES)):
            v = r[i]
            if isinstance(v, bool):
                v = 1.0 if v else 0.0
            feat.append(float(v or 0.0))
        X.append(feat)
    X = np.array(X, dtype=float)

    # cover target for calibration/slice: keep only 0/1 later
    y_cover = np.array([
        np.nan if c is None else (0.5 if int(c) == 2 else float(int(c)))
        for c in cover_raw
    ], dtype=float)

    return X, y_margin, y_cover, game_dates


def build_time_decay_weights(game_dates, half_life_days=30.0):
    valid = [d for d in game_dates if d is not None]
    if not valid:
        return np.ones(len(game_dates), dtype=float)
    anchor = max(valid)
    w = []
    for d in game_dates:
        if d is None:
            w.append(1.0)
            continue
        age = max(0, (anchor - d).days)
        # exp decay: 0.5 every half-life days
        wt = 0.5 ** (age / float(half_life_days))
        w.append(float(max(0.05, wt)))
    return np.array(w, dtype=float)


def compute_feature_health_report(X, game_dates):
    report = {}
    for j, name in enumerate(FEATURES):
        col = X[:, j]
        null_rate = float(np.mean(np.isnan(col)))
        # drift: last 30d mean vs previous window mean
        last_mask, prev_mask = [], []
        valid = [d for d in game_dates if d is not None]
        if valid:
            anchor = max(valid)
            for d in game_dates:
                if d is None:
                    last_mask.append(False)
                    prev_mask.append(False)
                    continue
                age = (anchor - d).days
                last_mask.append(age <= 30)
                prev_mask.append(30 < age <= 90)
        else:
            last_mask = [False] * len(game_dates)
            prev_mask = [False] * len(game_dates)

        last_vals = col[np.array(last_mask, dtype=bool)]
        prev_vals = col[np.array(prev_mask, dtype=bool)]
        last_mean = float(np.nanmean(last_vals)) if last_vals.size else None
        prev_mean = float(np.nanmean(prev_vals)) if prev_vals.size else None
        drift = None
        if (last_mean is not None) and (prev_mean is not None):
            drift = float(last_mean - prev_mean)

        report[name] = {
            "null_rate": null_rate,
            "mean": float(np.nanmean(col)) if col.size else None,
            "std": float(np.nanstd(col)) if col.size else None,
            "last30_mean": last_mean,
            "prev31_90_mean": prev_mean,
            "drift_last30_minus_prev": drift,
        }
    return report


def slice_eval(y_margin, pred_margin, y_cover01, p_cover, X):
    # X order knows home_b2b(4), away_b2b(5), home_spread(20)
    idx_home_b2b = FEATURES.index("home_b2b")
    idx_away_b2b = FEATURES.index("away_b2b")
    idx_spread = FEATURES.index("home_spread")

    out = {}

    def add_slice(name, mask):
        m = np.array(mask, dtype=bool)
        if m.sum() < 20:
            return
        mm = float(mean_absolute_error(y_margin[m], pred_margin[m]))
        # cover metrics only where 0/1
        cmask = m & (~np.isnan(y_cover01))
        if cmask.sum() >= 20:
            yy = y_cover01[cmask].astype(int)
            pp = p_cover[cmask]
            brier = float(brier_score_loss(yy, pp))
            acc = float(np.mean((pp >= 0.5) == (yy == 1)))
        else:
            brier, acc = None, None
        out[name] = {
            "rows": int(m.sum()),
            "mae_margin": mm,
            "brier_cover": brier,
            "acc_cover": acc,
        }

    hb = X[:, idx_home_b2b]
    ab = X[:, idx_away_b2b]
    sp = np.abs(X[:, idx_spread])

    add_slice("home_b2b", hb >= 0.5)
    add_slice("away_b2b", ab >= 0.5)
    add_slice("spread_small_0_3", sp < 3)
    add_slice("spread_mid_3_6", (sp >= 3) & (sp < 6))
    add_slice("spread_large_6p", sp >= 6)

    return out


def upsert_model(conn, model_name, payload_obj, trained_rows, metrics):
    payload = base64.b64encode(pickle.dumps(payload_obj)).decode("utf-8")
    with conn:
        with conn.cursor() as cur:
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
                (model_name, now_tw_str(), payload, int(trained_rows), json.dumps(metrics), now_tw_str())
            )


def main():
    min_rows = int((os.environ.get("BASE_MIN_ROWS") or "200").strip())
    decay_half_life_days = float((os.environ.get("DECAY_HALF_LIFE_DAYS") or "30").strip())

    conn = db_connect()
    try:
        rows = load_training_rows(conn)
        if len(rows) < min_rows:
            print(f"[WARN] base train rows too small: {len(rows)} < {min_rows}; skip")
            return

        X, y_margin, y_cover, game_dates = build_arrays(rows)
        sw = build_time_decay_weights(game_dates, half_life_days=decay_half_life_days)

        # margin head
        margin_model = HistGradientBoostingRegressor(
            max_depth=4,
            learning_rate=0.06,
            max_iter=280,
            random_state=42
        )
        margin_model.fit(X, y_margin, sample_weight=sw)
        pred_margin = margin_model.predict(X)
        mae = float(mean_absolute_error(y_margin, pred_margin))

        # cover head via margin calibration mapping: edge = pred_margin + home_spread
        spread_idx = FEATURES.index("home_spread")
        pred_edge = pred_margin + X[:, spread_idx]
        cover_mask = ~np.isnan(y_cover) & ((y_cover == 0.0) | (y_cover == 1.0))

        if int(cover_mask.sum()) < max(100, min_rows // 2):
            print(f"[WARN] cover rows too small for calibrator: {int(cover_mask.sum())}; skip calibrator")
            calibrator = None
            brier = None
            acc = None
            p_cover = np.full_like(y_margin, 0.5, dtype=float)
        else:
            # Out-of-fold calibration target to reduce leakage
            kf = KFold(n_splits=5, shuffle=True, random_state=42)
            oof_edge = np.full(int(cover_mask.sum()), np.nan, dtype=float)
            idx_cover = np.where(cover_mask)[0]
            Xc = X[idx_cover]
            yc_margin = y_margin[idx_cover]
            yc_cover = y_cover[idx_cover]
            swc = sw[idx_cover]

            for tr_idx, va_idx in kf.split(Xc):
                m = HistGradientBoostingRegressor(max_depth=4, learning_rate=0.06, max_iter=220, random_state=42)
                m.fit(Xc[tr_idx], yc_margin[tr_idx], sample_weight=swc[tr_idx])
                pm = m.predict(Xc[va_idx])
                oof_edge[va_idx] = pm + Xc[va_idx, spread_idx]

            calibrator = IsotonicRegression(out_of_bounds="clip")
            calibrator.fit(oof_edge, yc_cover)

            p_cover = np.full_like(y_margin, 0.5, dtype=float)
            p_cover[cover_mask] = calibrator.predict(pred_edge[cover_mask])
            brier = float(brier_score_loss(y_cover[cover_mask].astype(int), p_cover[cover_mask]))
            acc = float(np.mean((p_cover[cover_mask] >= 0.5) == (y_cover[cover_mask] == 1.0)))

        feature_health = compute_feature_health_report(X, game_dates)
        slices = slice_eval(y_margin, pred_margin, y_cover, p_cover, X)

        metrics_margin = {
            "mae_in_sample": mae,
            "rows": int(len(rows)),
            "decay_half_life_days": decay_half_life_days,
            "feature_health": feature_health,
            "slice_eval": slices,
            "features": FEATURES,
        }

        with db_connect() as conn2:
            upsert_model(conn2, "margin_base_model", margin_model, len(rows), metrics_margin)

            if calibrator is not None:
                metrics_cal = {
                    "brier_in_sample": brier,
                    "acc_in_sample": acc,
                    "rows": int(cover_mask.sum()),
                    "calibration_input": "pred_margin_plus_home_spread",
                    "slice_eval": slices,
                }
                # keep both names for backward compatibility
                upsert_model(conn2, "margin_calibrator", calibrator, int(cover_mask.sum()), metrics_cal)
                upsert_model(conn2, "cover_prob_calibrator", calibrator, int(cover_mask.sum()), metrics_cal)

        print(f"[OK] trained margin_base_model rows={len(rows)} mae={mae:.3f}")
        if calibrator is not None:
            print(f"[OK] trained margin_calibrator rows={int(cover_mask.sum())} brier={brier:.4f} acc={acc:.4f}")
        print("[INFO] feature health report generated (saved in model_registry.metrics)")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
