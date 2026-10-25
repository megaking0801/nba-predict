"""Playbook search: ESPN moneyline parsing, price selection, ROI backtests (no network, no DB)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from jobs import playbook as pb

FIX = json.loads((Path(__file__).parent / "fixtures" / "espn_odds_samples.json").read_text(encoding="utf-8"))


def _wrap(key):
    return {"items": FIX[key]}


# ---------- price conversion and parsing ----------

def test_american_to_decimal():
    assert pb.american_to_decimal(100) == pytest.approx(2.0)
    assert pb.american_to_decimal("+320") == pytest.approx(4.2)
    assert pb.american_to_decimal(-110) == pytest.approx(1 + 100 / 110)
    assert pb.american_to_decimal("-420") == pytest.approx(1 + 100 / 420)
    assert pb.american_to_decimal(0) is None and pb.american_to_decimal(None) is None
    assert pb.american_to_decimal("abc") is None


def _by_name(parsed):
    return {p["name"]: p for p in parsed}


def test_old_format_moneylines_and_default_trap():
    p = _by_name(pb.parse_providers(_wrap("old_2015_16")))
    d = p["5Dimes.eu"]
    assert d["home_ml"] == pytest.approx(pb.american_to_decimal(-103)) and d["ml_point"] in ("current", "flat")
    assert d["away_ml"] == pytest.approx(pb.american_to_decimal(-117))
    # the spread prices exist in the old format (this is what parse_close_line missed)
    assert d["home_spread_odds"] == pytest.approx(pb.american_to_decimal(-118))
    assert d["away_spread_odds"] == pytest.approx(pb.american_to_decimal(-102))
    assert d["over_odds"] == pytest.approx(pb.american_to_decimal(-118))
    assert d["under_odds"] == pytest.approx(pb.american_to_decimal(-102))
    assert p["BOVADA.lv"]["home_ml"] is None                      # flat 0 means no price
    assert p["consensus"]["home_ml"] is None


def test_new_format_prefers_close_and_reads_decimals():
    p = _by_name(pb.parse_providers(_wrap("new_2023_24")))
    c = p["Caesars Sportsbook (Colorado)"]
    assert c["ml_point"] == "close"
    assert c["home_ml"] == pytest.approx(4.2) and c["away_ml"] == pytest.approx(1.24, abs=0.01)


def test_live_odds_are_flagged_and_not_eligible():
    parsed = pb.parse_providers(_wrap("live_2020_21"))
    live = [p for p in parsed if "Live" in p["name"]][0]
    assert live["live"] is True and not pb.eligible(live)
    ok = [p for p in parsed if "Live" not in p["name"]][0]
    assert pb.eligible(ok)


def test_eligibility_rules():
    base = {"name": "Book", "live": False, "home_ml": 1.9, "away_ml": 1.95}
    assert pb.eligible(base)
    assert not pb.eligible({**base, "name": "consensus"}) and not pb.eligible({**base, "name": "Opening"})
    assert not pb.eligible({**base, "home_ml": None}) and not pb.eligible({**base, "away_ml": 1.0})
    assert not pb.eligible({**base, "home_ml": 16.0})
    assert not pb.eligible({**base, "live": True})


def test_single_provider_season():
    parsed = pb.parse_providers(_wrap("single_2025_26"))
    assert len(parsed) == 1 and pb.eligible(parsed[0])


def test_parse_handles_missing_json():
    assert pb.parse_providers(None) == [] and pb.parse_providers({}) == []


def test_market_uses_medians_and_devigs():
    pairs = [(1.80, 2.10), (1.85, 2.05), (1.90, 2.00)]
    provs = [{"name": f"b{i}", "live": False, "home_ml": h, "away_ml": a} for i, (h, a) in enumerate(pairs)]
    m = pb.market(provs)
    assert m["n"] == 3 and m["home_dec"] == pytest.approx(1.85) and m["away_dec"] == pytest.approx(2.05)
    assert m["home_dec_best"] == pytest.approx(1.90) and m["away_dec_best"] == pytest.approx(2.10)
    raw = [(1 / h) / (1 / h + 1 / a) for h, a in pairs]
    assert m["p_home"] == pytest.approx(float(np.median(raw)))
    assert pb.market([{"name": "x", "live": True, "home_ml": 2.0, "away_ml": 2.0}]) is None


# ---------- ROI ----------

def _bets(n, p_win, dec, seed=0):
    rng = np.random.default_rng(seed)
    dates = [dt.date(2020, 1, 1) + dt.timedelta(days=int(i // 5)) for i in range(n)]
    return pd.DataFrame({"date": dates, "season": "2019-20", "won": rng.random(n) < p_win, "dec": dec,
                         "push": False})


def test_roi_of_a_fair_bet_is_near_zero_and_of_a_vig_bet_is_negative():
    fair = pb.roi_summary(_bets(20000, 0.5, 2.0), reps=200, seed=1)
    assert abs(fair["roi"]) < 0.03 and fair["lo"] < 0 < fair["hi"]
    vig = pb.roi_summary(_bets(20000, 0.5, 1.909), reps=200, seed=1)
    assert -0.07 < vig["roi"] < -0.02 and vig["hi"] < 0


def test_roi_counts_push_as_a_refund():
    b = pd.DataFrame({"date": [dt.date(2020, 1, 1)] * 3, "season": "s", "won": [True, False, False],
                      "dec": 2.0, "push": [False, False, True]})
    s = pb.roi_summary(b, reps=20, seed=1)
    assert s["n"] == 3 and s["roi"] == pytest.approx((1 + (-1) + 0) / 3)


def test_roi_summary_handles_no_bets():
    empty = pd.DataFrame(columns=["date", "season", "won", "dec", "push"])
    assert pb.roi_summary(empty, reps=10)["n"] == 0


def _games(n=2000, seed=0, edge=0.0):
    """Games whose market probability is calibrated; the model sees `edge` extra true probability."""
    rng = np.random.default_rng(seed)
    p_true = rng.uniform(0.25, 0.8, n)
    p_market = np.clip(p_true - edge, 0.05, 0.95)
    home_won = rng.random(n) < p_true
    vig = 0.045
    home_dec = 1 / (p_market * (1 + vig))
    away_dec = 1 / ((1 - p_market) * (1 + vig))
    dates = [dt.date(2019, 10, 25) + dt.timedelta(days=int(i // 8)) for i in range(n)]
    return pd.DataFrame({"game_id": [f"g{i}" for i in range(n)], "date": dates, "season": "2019-20",
                         "home_won": home_won, "p_market": p_market, "home_dec": home_dec, "away_dec": away_dec,
                         "home_dec_best": home_dec * 1.01, "away_dec_best": away_dec * 1.01, "p_model": p_true})


def test_baseline_strategies_lose_the_vig_when_the_market_is_calibrated():
    out = pb.baseline_roi(_games(8000, seed=2), reps=100, seed=1)
    assert set(out) == {"favorite", "underdog", "home", "away"}
    for name, s in out.items():
        assert -0.10 < s["roi"] < 0.0, (name, s["roi"])
    assert out["favorite"]["hit_rate"] > 0.55 and out["underdog"]["hit_rate"] < 0.45


def test_model_with_no_information_does_not_beat_the_vig():
    g = _games(8000, seed=3)
    g["p_model"] = g["p_market"] + np.random.default_rng(9).normal(0, 0.05, len(g))
    out = pb.model_roi(g, edges=(0.02, 0.04, 0.06), reps=100, seed=1)
    for e, s in out.items():
        assert s["n"] == 0 or s["roi"] < 0.02, (e, s)


def test_model_with_a_real_edge_is_found():
    g = _games(12000, seed=4, edge=0.05)         # market prices 5 points below the truth, model is right
    out = pb.model_roi(g, edges=(0.02, 0.04, 0.06), reps=100, seed=1)
    assert out[0.04]["roi"] > 0.05 and out[0.04]["lo"] > 0


def test_model_bets_pick_the_side_and_the_price():
    g = pd.DataFrame({"game_id": ["a", "b", "c"], "date": [dt.date(2020, 1, 1)] * 3, "season": "s",
                      "home_won": [True, True, False], "p_market": [0.5, 0.5, 0.5],
                      "home_dec": [2.0, 2.0, 2.0], "away_dec": [1.8, 1.8, 1.8],
                      "home_dec_best": 2.0, "away_dec_best": 1.8, "p_model": [0.6, 0.4, 0.45]})
    b = pb.model_bets(g, edge=0.05)
    assert list(b["side"]) == ["home", "away"] and list(b["won"]) == [True, False]
    assert list(b["dec"]) == [2.0, 1.8]


def test_pass_rule():
    good = {"n": 1500, "lo": 0.01, "share_seasons_positive": 0.8}
    assert pb.passes(good) is True
    assert pb.passes({**good, "lo": -0.01}) is False
    assert pb.passes({**good, "n": 900}) is False
    assert pb.passes({**good, "share_seasons_positive": 0.5}) is False


# ---------- totals ----------

def test_total_bets_direction_gap_push_and_price():
    t = pd.DataFrame({"game_id": ["a", "b", "c", "d"], "date": [dt.date(2020, 1, 1)] * 4, "season": "s",
                      "total": [230.0, 210.0, 220.0, 225.0], "total_line": [220.5, 220.5, 220.0, 224.0],
                      "mu": [224.0, 216.0, 223.0, 225.5]})
    b = pb.total_bets(t, gap=2.0, dec=1.909)
    assert list(b["side"]) == ["over", "under", "over"]
    assert list(b["won"]) == [True, True, False]
    assert list(b["push"]) == [False, False, True]          # game c: total == line
    assert list(b["dec"]) == [1.909] * 3


# ---------- fetch ----------

def test_fetch_is_resumable_and_never_calls_twice(tmp_path, monkeypatch):
    out = tmp_path / "ml.jsonl"
    calls = []

    def fake_odds(gid, **kw):
        calls.append(gid)
        return _wrap("old_2015_16") if gid != "404" else None

    run = lambda sql, params=(): pd.DataFrame({"game_id": ["1", "2", "404"]})
    monkeypatch.setattr(pb.time, "sleep", lambda s: None)
    pb.fetch_moneylines(run, out, fetch=fake_odds, sleep=0)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["game_id"] for r in rows] == ["1", "2", "404"] and rows[2]["providers"] == []
    assert any(p["name"] == "5Dimes.eu" for p in rows[0]["providers"])
    pb.fetch_moneylines(run, out, fetch=fake_odds, sleep=0)
    assert calls == ["1", "2", "404"]                        # nothing fetched again


def test_fetch_respects_limit(tmp_path, monkeypatch):
    out = tmp_path / "ml.jsonl"
    monkeypatch.setattr(pb.time, "sleep", lambda s: None)
    run = lambda sql, params=(): pd.DataFrame({"game_id": ["1", "2", "3"]})
    pb.fetch_moneylines(run, out, fetch=lambda gid, **kw: _wrap("single_2025_26"), sleep=0, limit=2)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2


def test_module_is_read_only():
    src = Path(pb.__file__).read_text(encoding="utf-8")
    for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "commit()", "executemany", "ensure_schema"):
        assert word not in src


def test_cli_error_does_not_leak_a_dsn(monkeypatch, capsys):
    def boom():
        raise RuntimeError("postgresql://user:secret@host/db failed")
    monkeypatch.setattr(pb, "connect_readonly", boom)
    assert pb.main(["totals"]) == 2
    assert "secret" not in capsys.readouterr().out
