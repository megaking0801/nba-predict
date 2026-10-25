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
