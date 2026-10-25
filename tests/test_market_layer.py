"""P5.0: market baselines (pure summaries and the read-only report wiring)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from jobs import market_layer as ml
from tests.quality_fakes import fake_run


def _lines(n=400, seed=0, skew_from=None):
    rng = np.random.default_rng(seed)
    spread = np.round(rng.normal(-2, 6, n) * 2) / 2
    margin = (-spread + rng.normal(0, 12, n)).round()
    seasons = np.where(np.arange(n) < n // 2, "2022-23", "2023-24")
    home_price = np.full(n, 1.91)
    away_price = np.full(n, 1.91)
    if skew_from is not None:
        home_price[skew_from:] = 1.87
        away_price[skew_from:] = 1.95
    dates = [dt.date(2022, 10, 20) + dt.timedelta(days=int(i // 4)) for i in range(n)]
    return pd.DataFrame({"game_id": [f"g{i}" for i in range(n)], "season": seasons, "season_type": "regular",
                         "date": dates, "margin": margin, "home_spread": spread,
                         "home_price": home_price, "away_price": away_price})


def test_line_frame_uses_the_negative_spread_as_expected_margin():
    f = ml.line_frame(_lines(5))
    assert list(f.columns) == ["game_id", "date", "season", "margin", "mu"]
    l = _lines(5)
    assert (f["mu"] == -l["home_spread"]).all()


def test_price_summary_flags_default_prices():
    l = _lines(400, skew_from=300)
    s = ml.price_summary(l).set_index("season")
    assert s.loc["2022-23", "n_asymmetric"] == 0 and s.loc["2022-23", "share_asymmetric"] == 0
    assert s.loc["2023-24", "n_asymmetric"] == 100 and s.loc["2023-24", "n"] == 200


def test_price_summary_ignores_missing_prices():
    l = _lines(10)
    l.loc[:4, "away_price"] = np.nan
    s = ml.price_summary(l)
    assert int(s["n_prices"].sum()) == 5 and int(s["n_asymmetric"].sum()) == 0


def test_line_baseline_mae_bias_and_push_share():
    l = pd.DataFrame({"season": ["a"] * 4, "margin": [10.0, -4.0, 6.0, 3.0],
                      "home_spread": [-7.0, 4.0, -6.0, -3.0]})        # errors 3, 0, 0, 0
    b = ml.line_baseline(l).iloc[0]
    assert b["n"] == 4 and b["mae"] == pytest.approx(0.75) and b["bias"] == pytest.approx(0.75)
    assert b["push_share"] == pytest.approx(0.75)


def test_constant_half_brier_is_a_quarter_without_a_cover_edge():
    l = _lines(4000, seed=3)
    r = ml.constant_half_brier(l)
    assert r["brier"] == pytest.approx(0.25, abs=0.003) and 0.45 < r["home_cover_rate"] < 0.55


def test_constant_half_brier_excludes_pushes():
    l = pd.DataFrame({"margin": [5.0, 5.0, 8.0], "home_spread": [-5.0, -5.0, -3.0]})
    r = ml.constant_half_brier(l)
    assert r["n"] == 1 and r["brier"] == 0.25


def test_gap_vs_line_is_positive_when_the_model_is_worse_than_the_line():
    l = _lines(1200, seed=1)
    ridge = ml.line_frame(l)
    ridge["mu"] = ridge["mu"] * 0.5            # shrunk toward zero: worse than the line
    out = ml.gap_vs_line(ridge, l, reps=200, seed=1)
    assert out["n"] == 1200 and out["mean_mae_gap"] > 0 and out["lo"] > 0
    assert out["share_seasons_ridge_better"] == 0


def test_gap_vs_line_is_zero_for_the_line_itself():
    l = _lines(300)
    out = ml.gap_vs_line(ml.line_frame(l), l, reps=100, seed=1)
    assert out["mean_mae_gap"] == 0


def test_clv_summary_numbers_and_sample_size():
    rng = np.random.default_rng(4)
    n = 500
    used = np.round(rng.normal(-2, 5, n) * 2) / 2
    close = used + rng.normal(0, 1.0, n)
    tip = pd.Timestamp("2026-11-01 00:00", tz="UTC")
    pred = pd.DataFrame({"home_spread_used": used, "close_spread": close, "tipoff_utc": tip,
                         "predicted_at": tip - pd.Timedelta(hours=14)})
    s = ml.clv_summary(pred)
    assert s["n"] == n and abs(s["sd_move"] - 1.0) < 0.1 and abs(s["mean_move"]) < 0.15
    assert s["mean_hours_before_tipoff"] == pytest.approx(14)
    assert s["picks_needed_99_80"] == pytest.approx(((2.58 + 0.84) * s["sd_move"] / 0.2) ** 2)
    assert s["picks_needed_99_half_width"] < s["picks_needed_99_80"]


def test_clv_summary_handles_nothing():
    assert ml.clv_summary(pd.DataFrame()) == {"n": 0}
    assert ml.clv_summary(None) == {"n": 0}


def test_clv_report_queries_the_first_prediction_per_game_with_model_and_start_date():
    run, db = fake_run({"ml:CLV": pd.DataFrame()})
    assert ml.clv_report(run) == {"n": 0}
    sql, params = db.calls[0]
    assert "DISTINCT ON (p.game_id)" in sql and "predicted_at ASC" in sql
    assert params == (ml.MODEL_NAME, ml.LIVE_START)


def test_sql_is_select_only_and_uses_the_closing_view():
    src = Path(ml.__file__).read_text(encoding="utf-8")
    for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "commit()", "executemany", "ensure_schema"):
        assert word not in src
    assert "v_closing_lines" in ml._LINES and "v_closing_lines" in ml._CLV


def test_cli_clv_writes_json_and_closes(monkeypatch, tmp_path):
    class Conn:
        closed = False

        def close(self):
            self.closed = True

    conn = Conn()
    monkeypatch.setattr(ml, "connect_readonly", lambda: conn)
    monkeypatch.setattr(ml, "_prepare_session", lambda c: None)
    monkeypatch.setattr(ml, "make_run", lambda c: (lambda sql, params=(): pd.DataFrame()))
    out = tmp_path / "c.json"
    assert ml.main(["--report", "clv", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"n": 0}
    assert conn.closed


def test_cli_error_does_not_leak_a_dsn(monkeypatch, capsys):
    def boom():
        raise RuntimeError("postgresql://user:secret@host/db failed")
    monkeypatch.setattr(ml, "connect_readonly", boom)
    assert ml.main(["--report", "clv"]) == 2
    assert "secret" not in capsys.readouterr().out


def test_cli_report_failure_is_scrubbed(monkeypatch, capsys):
    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(ml, "connect_readonly", lambda: Conn())
    monkeypatch.setattr(ml, "_prepare_session", lambda c: None)

    def bad_run(c):
        def run(sql, params=()):
            raise RuntimeError("could not connect to postgresql://u:pw@h/db")
        return run

    monkeypatch.setattr(ml, "make_run", bad_run)
    assert ml.main(["--report", "clv"]) == 2
    out = capsys.readouterr().out
    assert "pw" not in out and "RuntimeError" in out


# ---------- P5.1 second layer ----------

def _season_of(d):
    y = d.year if d.month >= 8 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def _residual_world(n=4200, signal=0.0, seed=0):
    """Synthetic games across ~11 seasons; the market residual is `signal * f1` plus noise."""
    rng = np.random.default_rng(seed)
    start = dt.date(2015, 10, 25)
    dates = [start + dt.timedelta(days=int(i * 3900 / n)) for i in range(n)]
    f1, f2 = rng.normal(0, 1, n), rng.normal(0, 1, n)
    spread = np.round(rng.normal(-2, 6, n) * 2) / 2
    m = -spread
    resid = signal * f1 + rng.normal(0, 12, n)
    margin = m + resid
    games = pd.DataFrame({"game_id": [f"g{i}" for i in range(n)], "date": dates,
                          "season": [_season_of(d) for d in dates], "season_type": "regular",
                          "eligible": True, "margin": margin, "f1": f1, "f2": f2})
    lines = pd.DataFrame({"game_id": games["game_id"], "season": games["season"], "season_type": "regular",
                          "date": dates, "margin": margin, "home_spread": spread,
                          "home_price": 1.91, "away_price": 1.91})
    return games, lines


def test_residual_table_defines_m_and_the_target():
    games, lines = _residual_world(60)
    t = ml.residual_table(games, lines)
    assert (t["m"] == -t["home_spread"]).all() and (t["absm"] == t["m"].abs()).all()
    assert np.allclose(t["resid"], t["margin"] - t["m"])


def test_prediction_is_the_line_plus_the_scaled_residual():
    games, lines = _residual_world(1500, signal=3.0)
    t = ml.residual_table(games, lines)
    full = ml.residual_predictions(t, ["f1", "f2", "m", "absm"], alpha=10.0, lam=1.0)
    half = ml.residual_predictions(t, ["f1", "f2", "m", "absm"], alpha=10.0, lam=0.5)
    assert np.allclose(full["mu"], full["m"] + full["r_hat"])
    assert np.allclose(half["mu"], half["m"] + 0.5 * half["r_hat"])
    assert len(full) < len(t)                                   # the first 800 rows are warm-up


def test_a_real_residual_signal_is_found_and_kept():
    games, lines = _residual_world(signal=4.0)
    t = ml.residual_table(games, lines)
    meta = t[["game_id", "season", "season_type"]]
    cand = ml.residual_predictions(t, ["f1", "f2", "m", "absm"], alpha=10.0)
    j = ml.judge_vs_line(cand, lines, meta, ml.VALID_SEGMENT, reps=100, seed=1, level=ml.VALID_LEVEL)
    assert j["mean"] < -0.1 and j["info_over_market_candidate"]["beta"] > 0.7
    assert j["market_judgement"]["keep"] is True, j["market_judgement"]


def test_no_signal_is_not_kept_and_beta_is_near_zero():
    games, lines = _residual_world(signal=0.0, seed=2)
    t = ml.residual_table(games, lines)
    meta = t[["game_id", "season", "season_type"]]
    cand = ml.residual_predictions(t, ["f1", "f2", "m", "absm"], alpha=100.0)
    j = ml.judge_vs_line(cand, lines, meta, ml.VALID_SEGMENT, reps=100, seed=1, level=ml.VALID_LEVEL)
    assert j["market_judgement"]["keep"] is False
    assert abs(j["info_over_market_candidate"]["beta"]) < 4 * j["info_over_market_candidate"]["se"]


def test_decide_market_lists_every_failed_rule():
    good = {"mean": -0.05, "hi": -0.01, "share_negative": 0.9, "cover_brier_delta": -0.001,
            "info_over_market_candidate": {"beta": 0.8, "se": 0.1}, "n_base_in_segment": 10, "n_cand_in_segment": 10}
    assert ml.decide_market(good)["keep"] is True
    bad = dict(good, mean=-0.01, hi=0.01, share_negative=0.4, cover_brier_delta=0.001,
               info_over_market_candidate={"beta": 0.1, "se": 0.1}, n_cand_in_segment=9)
    assert set(ml.decide_market(bad)["failed"]) == {"gain", "interval", "beta", "cover_brier", "seasons", "coverage"}
    assert "beta" in ml.decide_market(dict(good, info_over_market_candidate={"beta": None, "se": None}))["failed"]


def test_shuffle_control_breaks_a_real_signal():
    games, lines = _residual_world(signal=4.0, seed=3)
    t = ml.residual_table(games, lines)
    meta = t[["game_id", "season", "season_type"]]
    real = ml.judge_vs_line(ml.residual_predictions(t, ["f1", "m", "absm"], 10.0), lines, meta, ml.VALID_SEGMENT, reps=50)
    shuf = ml.judge_vs_line(ml.residual_predictions(t, ["f1", "m", "absm"], 10.0, shuffle_seed=7), lines, meta,
                            ml.VALID_SEGMENT, reps=50)
    assert real["mean"] < shuf["mean"] and shuf["mean"] > -0.05


def test_shift_control_removes_the_signal():
    games, lines = _residual_world(signal=4.0, seed=4)
    t = ml.residual_table(games, lines)
    meta = t[["game_id", "season", "season_type"]]
    real = ml.judge_vs_line(ml.residual_predictions(t, ["f1", "m", "absm"], 10.0), lines, meta, ml.VALID_SEGMENT, reps=50)
    shifted = ml.judge_vs_line(ml.residual_predictions(t, ["f1", "m", "absm"], 10.0, shift=True), lines, meta,
                               ml.VALID_SEGMENT, reps=50)
    assert shifted["mean"] > real["mean"] + 0.1


def test_run_residual_stops_after_a_failed_prior_check(monkeypatch):
    games, lines = _residual_world(signal=0.0, seed=5)
    monkeypatch.setattr(ml, "build_feature_table", lambda bundles, cfg: games)
    monkeypatch.setattr(ml, "PRIOR_FEATURES", ["f1", "m"])
    monkeypatch.setattr(ml, "FULL_FEATURES", ["f1", "f2", "m", "absm"])
    out = ml.run_residual([], lines, reps=50, seed=1, force_full=False)
    if "stopped" in out:
        assert "tune_grid" not in out
    else:                                                          # a lucky positive beta: it must then run in full
        assert len(out["tune_grid"]) == len(ml.ALPHAS) * len(ml.LAMBDAS)


def test_run_residual_full_path_on_a_real_signal(monkeypatch):
    games, lines = _residual_world(signal=4.0, seed=6)
    monkeypatch.setattr(ml, "build_feature_table", lambda bundles, cfg: games)
    monkeypatch.setattr(ml, "PRIOR_FEATURES", ["f1", "m"])
    monkeypatch.setattr(ml, "FULL_FEATURES", ["f1", "f2", "m", "absm"])
    out = ml.run_residual([], lines, reps=60, seed=1, n_controls=2)
    assert "stopped" not in out and len(out["tune_grid"]) == 14
    assert out["chosen"]["alpha"] in ml.ALPHAS and out["chosen"]["lam"] in ml.LAMBDAS
    assert out["validation"]["market_judgement"]["keep"] is True
    assert out["controls"]["true_beats_every_shuffle"] is True and out["controls"]["shift_does_not_help"] is True
    json.dumps(out, default=str)


def test_grid_sizes_match_the_registration():
    assert len(ml.ALPHAS) == 7 and ml.LAMBDAS == (0.5, 1.0) and ml.VALID_LEVEL == 0.995
    assert ml.TUNE_SEGMENT["last_season"] == "2021-22" and ml.VALID_SEGMENT["first_season"] == "2022-23"
