"""Read-only state check for the v2 pipeline — answers "what's actually in
the DB?" before we trust any performance claim. Pure SELECTs, no writes.

Run: python -m jobs.diagnose
"""
from __future__ import annotations

from jobs.db_utils import db_connect
from jobs.model import CALIBRATOR_NAME, MODEL_NAME


def _rows(conn, sql, params=()):
    """Run a SELECT; on any error (e.g. table/column not present yet) roll the
    aborted transaction back and return None so the report keeps going."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — diagnostic must never crash
        conn.rollback()
        return [("__error__", str(e).splitlines()[0])]


def _scalar(conn, sql, params=()):
    r = _rows(conn, sql, params)
    if r and r[0] and r[0][0] != "__error__":
        return r[0][0]
    return None


def main() -> None:
    conn = db_connect()
    try:
        conn.autocommit = True  # read-only; avoid holding a transaction open
        print("=" * 60)
        print("NBA Edge v2 — DB 狀態體檢（唯讀）")
        print("=" * 60)

        # ---- models ----
        print("\n## 模型 model_registry_v2")
        models_ok = True
        for name in (MODEL_NAME, CALIBRATOR_NAME):
            r = _rows(conn,
                      "SELECT model_version, trained_rows, metrics, created_at "
                      "FROM public.model_registry_v2 "
                      "WHERE model_name = %s AND is_active", (name,))
            if r and r[0] and r[0][0] == "__error__":
                print(f"  [錯誤] 讀取 {name} 失敗：{r[0][1]}")
                models_ok = False
                continue
            if not r:
                print(f"  ❌ {name}: 無啟用版本（尚未訓練）")
                models_ok = False
                continue
            ver, rows, metrics, created = r[0]
            metrics = metrics if isinstance(metrics, dict) else {}
            line = f"  ✅ {name}: {ver}  rows={rows}  created={created}"
            if name == MODEL_NAME:
                rm = metrics.get("report_margin") or {}
                rc = metrics.get("report_cover") or {}
                rb = (metrics.get("report_betting") or {}).get("ats") or {}
                line += (f"\n     MAE={rm.get('mae')}  Brier={rc.get('brier')}  "
                         f"gates_passed={metrics.get('gates_passed')}")
                if rb.get("hit_rate") is not None:
                    line += (f"\n     回測 ATS={rb['hit_rate'] * 100:.1f}% "
                             f"ROI={rb['roi'] * 100:+.1f}% (n={rb['n_graded']})")
                else:
                    line += "\n     回測：無（舊模型尚未含 report_betting，需重訓）"
            else:
                line += f"  method={metrics.get('method')}  alarm={metrics.get('alarm')}"
            print(line)

        # ---- table volumes ----
        print("\n## 資料量")
        for label, sql in [
            ("games_v2 (依 status)",
             "SELECT status, count(*) FROM public.games_v2 GROUP BY status ORDER BY status"),
            ("games_v2 (依 season)",
             "SELECT season, count(*) FROM public.games_v2 GROUP BY season ORDER BY season"),
            ("market_lines (依 source)",
             "SELECT source, count(*) FROM public.market_lines GROUP BY source ORDER BY source"),
            ("predictions (依 pick_side)",
             "SELECT coalesce(pick_side, '(abstain)'), count(*) "
             "FROM public.predictions GROUP BY pick_side ORDER BY 1"),
            ("predictions (依 abstain_reason)",
             "SELECT coalesce(abstain_reason, '(picked)'), count(*) "
             "FROM public.predictions GROUP BY abstain_reason ORDER BY 2 DESC"),
        ]:
            print(f"  {label}:")
            for row in (_rows(conn, sql) or []):
                if row and row[0] == "__error__":
                    print(f"    [錯誤] {row[1]}")
                else:
                    print(f"    {row[0]}: {row[1]}")

        for label, sql in [
            ("team_game_stats 列數", "SELECT count(*) FROM public.team_game_stats"),
            ("player_game_stats 列數", "SELECT count(*) FROM public.player_game_stats"),
            ("predictions 列數", "SELECT count(*) FROM public.predictions"),
            ("predictions 已結算", "SELECT count(*) FROM public.predictions WHERE settled_at IS NOT NULL"),
            ("predictions paper", "SELECT count(*) FROM public.predictions WHERE is_paper"),
        ]:
            print(f"  {label}: {_scalar(conn, sql)}")

        # ---- settled pick record (matches the app's calc) ----
        print("\n## 已結算推薦戰績")
        rec = _rows(conn,
                    "SELECT cover_result, count(*) FROM public.predictions "
                    "WHERE pick_side IS NOT NULL AND settled_at IS NOT NULL "
                    "AND cover_result IS NOT NULL GROUP BY cover_result ORDER BY cover_result")
        label = {0: "客過盤", 1: "主過盤", 2: "push"}
        if rec and rec[0] and rec[0][0] == "__error__":
            print(f"  [錯誤] {rec[0][1]}")
        elif not rec:
            print("  尚無已結算推薦。")
        else:
            for cr, n in rec:
                print(f"  {label.get(cr, cr)}: {n}")

        # ---- conclusion ----
        n_pred = _scalar(conn, "SELECT count(*) FROM public.predictions") or 0
        n_picks = _scalar(conn,
                          "SELECT count(*) FROM public.predictions WHERE pick_side IS NOT NULL") or 0
        n_games = _scalar(conn,
                         "SELECT count(*) FROM public.games_v2 WHERE status = 'final'") or 0
        print("\n## 結論")
        if not models_ok or n_games == 0:
            print("  🟥 幾乎沒跨過：模型未啟用或無 final 賽事 → 先跑 v2_backfill + v2_train。")
        elif n_picks == 0:
            print(f"  🟧 跨過但沒出 picks：{n_pred} 筆預測但 0 筆推薦 "
                  "→ 模型在跑,但門檻/盤口/閘門讓它都不出手,需檢視 diagnose 的 abstain_reason 分布。")
        else:
            print(f"  🟩 跨過且有 picks:{n_games} 場 final、{n_pred} 筆預測、{n_picks} 筆推薦。"
                  "可信任 v2_evaluate 的回測數字。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
