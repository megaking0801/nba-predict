"""P4.0: paired comparison tools in jobs.feature_ablation (pure functions, no DB)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from jobs import feature_ablation as fa


def _preds(n=600, seed=0, shift=0.0, start=dt.date(2020, 1, 1)):
    rng = np.random.default_rng(seed)
    margin = rng.normal(2.0, 13.0, n)
    mu = margin * 0.2 + rng.normal(0.0, 6.0, n) + shift
    dates = [start + dt.timedelta(days=int(i // 5)) for i in range(n)]
    seasons = ["2019-20" if d < dt.date(2020, 7, 1) else "2020-21" for d in dates]
    return pd.DataFrame({"game_id": [f"g{i}" for i in range(n)], "date": dates, "season": seasons,
                         "margin": margin, "mu": mu, "sigma": 13.0})


# ---------- paired delta and bootstrap ----------

def test_paired_delta_of_identical_predictions_is_zero():
    p = _preds()
    d = fa.paired_delta(p, p)
    assert len(d) == len(p) and (d["d"] == 0).all()


def test_paired_delta_only_uses_common_games():
    a, b = _preds(), _preds().iloc[:100]
    assert len(fa.paired_delta(a, b)) == 100


def test_paired_delta_sign_is_candidate_minus_base():
    base = _preds()
    cand = base.copy()
    cand["mu"] = cand["margin"]                      # perfect candidate
    assert fa.paired_delta(base, cand)["d"].mean() < 0


def test_bootstrap_is_deterministic_and_brackets_the_mean():
    d = fa.paired_delta(_preds(), _preds(seed=0, shift=0.5))
    m1, lo1, hi1 = fa.block_bootstrap_ci(d, reps=300, seed=1)
    m2, lo2, hi2 = fa.block_bootstrap_ci(d, reps=300, seed=1)
    assert (m1, lo1, hi1) == (m2, lo2, hi2)
    assert lo1 < m1 < hi1


def test_bootstrap_of_zero_difference_contains_zero():
    p = _preds()
    m, lo, hi = fa.block_bootstrap_ci(fa.paired_delta(p, p), reps=200, seed=3)
    assert m == 0 and lo <= 0 <= hi


def test_bootstrap_detects_a_real_improvement():
    base = _preds(n=3000)
    cand = base.copy()
    cand["mu"] = 0.5 * base["mu"] + 0.5 * base["margin"]      # clearly better
    m, lo, hi = fa.block_bootstrap_ci(fa.paired_delta(base, cand), reps=300, seed=2)
    assert hi < 0 and m < -0.5


def test_blocks_are_fourteen_day_windows():
    d = pd.DataFrame({"date": [dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in (0, 13, 14, 27, 28)],
                      "d": [1.0] * 5})
    assert list(fa._block_ids(d, 14)) == [0, 0, 1, 1, 2]


# ---------- per season and the keep rule ----------

def test_per_season_share_negative():
    d = pd.DataFrame({"season": ["a", "a", "b", "b", "c"], "d": [-1.0, -1.0, -2.0, 0.5, 0.3]})
    seasons, share = fa.per_season_delta(d)
    assert seasons == {"a": -1.0, "b": -0.75, "c": 0.3}
    assert abs(share - 2 / 3) < 1e-9


def _summary(mean=-0.03, hi=-0.01, share=0.8, cover=-0.001):
    return {"mean": mean, "lo": mean - 0.02, "hi": hi, "share_negative": share, "cover_brier_delta": cover}


def test_decide_keeps_a_clear_gain():
    assert fa.decide(_summary())["keep"] is True


def test_decide_rule_boundaries():
    assert fa.decide(_summary(mean=-0.02, hi=-0.001))["keep"] is True          # exactly the minimum gain
    assert fa.decide(_summary(mean=-0.0199))["keep"] is False
    assert fa.decide(_summary(hi=0.0))["keep"] is False                         # interval touches zero
    assert fa.decide(_summary(share=0.6))["keep"] is False
    assert fa.decide(_summary(cover=0.0001))["keep"] is False                   # Brier got worse
    assert fa.decide(_summary(cover=None))["keep"] is False                     # unknown is not "fine"


def test_decide_reports_which_rule_failed():
    r = fa.decide(_summary(share=0.5, cover=0.01))
    assert set(r["failed"]) == {"seasons", "cover_brier"}


def test_undetectable_when_halfwidth_exceeds_minimum_gain():
    assert fa.detectability(0.03)["detectable"] is False
    assert fa.detectability(0.01)["detectable"] is True


# ---------- info over market ----------

def test_info_over_market_is_undefined_when_model_equals_the_line():
    rng = np.random.default_rng(5)
    n = 4000
    spread = rng.normal(-2, 6, n)
    margin = -spread + rng.normal(0, 12, n)
    df = pd.DataFrame({"margin": margin, "mu": -spread, "home_spread": spread})
    r = fa.info_over_market(df)
    assert r["n"] == n and r["beta"] is None          # model == line: no variation, nothing to estimate


def test_info_over_market_recovers_real_signal():
    rng = np.random.default_rng(6)
    n = 20000
    spread = rng.normal(-2, 6, n)
    signal = rng.normal(0, 3, n)
    margin = -spread + signal + rng.normal(0, 12, n)
    df = pd.DataFrame({"margin": margin, "mu": -spread + signal, "home_spread": spread})
    r = fa.info_over_market(df)
    assert 0.85 < r["beta"] < 1.15 and r["se"] < 0.1


# ---------- compare and experiment wiring ----------

def test_compare_returns_summary_and_applies_the_segment():
    base = _preds(n=3000)
    cand = base.copy()
    cand["mu"] = 0.5 * base["mu"] + 0.5 * base["margin"]
    meta = base[["game_id"]].assign(season_type="regular")
    meta.loc[meta.index[:100], "season_type"] = "preseason"
    out = fa.compare(base, cand, lines=pd.DataFrame(), meta=meta, season_types=("regular",),
                     reps=100, seed=1)
    assert out["n"] == 2900
    assert {"mean", "lo", "hi", "share_negative", "failed", "halfwidth"} <= set(out)
    assert out["mean"] < 0


def test_data_scope_experiments_are_the_preregistered_set():
    names = [e["name"] for e in fa.DATA_SCOPE_EXPERIMENTS]
    assert names == ["E1_exclude_preseason", "E2a_train_from_2016-17", "E2b_train_from_2018-19"]
    assert fa.SEGMENT == {"first_season": "2019-20", "last_season": "2024-25",
                          "season_types": ("regular", "playoffs", "playin")}


def test_run_data_scope_with_stubbed_walk_forward(monkeypatch, tmp_path):
    base_preds = _preds(n=800)
    seen = []

    def fake_table(bundles, cfg):
        seen.append((cfg.EXCLUDE_PRESEASON, cfg.TRAIN_FROM_SEASON))
        return pd.DataFrame({"game_id": base_preds["game_id"], "season": base_preds["season"],
                             "season_type": "regular", "date": base_preds["date"]})

    def fake_walk(df, kind, cfg=None, **kw):
        out = base_preds.copy()
        if seen[-1] != (False, ""):
            out["mu"] = out["mu"] * 0.9
        return out

    monkeypatch.setattr(fa, "build_feature_table", fake_table)
    monkeypatch.setattr(fa, "walk_forward", fake_walk)
    monkeypatch.setattr(fa, "SEGMENT", {"first_season": "2019-20", "last_season": "2020-21",
                                        "season_types": ("regular",)})
    res = fa.run_data_scope([], pd.DataFrame(), reps=50, seed=1)
    assert seen[0] == (False, "")                                  # baseline first
    assert (True, "") in seen and (False, "2016-17") in seen and (False, "2018-19") in seen
    assert set(res["experiments"]) == {"E1_exclude_preseason", "E2a_train_from_2016-17",
                                       "E2b_train_from_2018-19"}
    assert "baseline_selfcheck" in res and "E3_combined" in res
    json.dumps(res, default=str)


# ---------- CLI ----------

def test_cli_writes_json_and_closes_connection(monkeypatch, tmp_path):
    class Conn:
        closed = False

        def close(self):
            self.closed = True

    conn = Conn()
    monkeypatch.setattr(fa, "connect_readonly", lambda: conn)
    monkeypatch.setattr(fa, "load_bundles", lambda c: [])
    monkeypatch.setattr(fa, "load_closing_lines", lambda c: pd.DataFrame())
    monkeypatch.setattr(fa, "_prepare_session", lambda c: None)
    monkeypatch.setattr(fa, "run_data_scope", lambda b, l, **kw: {"experiments": {}})
    out = tmp_path / "o.json"
    assert fa.main(["--experiment", "data_scope", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"experiments": {}}
    assert conn.closed


def test_cli_error_does_not_leak_a_dsn(monkeypatch, capsys):
    def boom():
        raise RuntimeError("postgresql://user:secret@host/db failed")
    monkeypatch.setattr(fa, "connect_readonly", boom)
    assert fa.main(["--experiment", "data_scope"]) == 2
    assert "secret" not in capsys.readouterr().out


def test_module_never_writes():
    src = Path(fa.__file__).read_text(encoding="utf-8")
    for word in ("commit()", "executemany", "ensure_schema", "INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER "):
        assert word not in src


def test_info_over_market_is_near_zero_for_unrelated_noise():
    rng = np.random.default_rng(7)
    n = 20000
    spread = rng.normal(-2, 6, n)
    margin = -spread + rng.normal(0, 12, n)
    df = pd.DataFrame({"margin": margin, "mu": -spread + rng.normal(0, 3, n), "home_spread": spread})
    r = fa.info_over_market(df)
    assert abs(r["beta"]) < 4 * r["se"]


def test_coverage_loss_fails_the_keep_rule():
    s = _summary()
    s["n_base_in_segment"], s["n_cand_in_segment"] = 1000, 950
    assert fa.decide(s)["failed"] == ["coverage"]
    s["n_cand_in_segment"] = 1000
    assert fa.decide(s)["keep"] is True


def test_compare_reports_segment_sizes_and_survives_empty_predictions():
    base = _preds(n=300)
    cand = base.iloc[:250].copy()
    meta = base[["game_id"]].assign(season_type="regular")
    out = fa.compare(base, cand, lines=pd.DataFrame(), meta=meta, season_types=("regular",), reps=50, seed=1)
    assert (out["n_base_in_segment"], out["n_cand_in_segment"], out["n"]) == (300, 250, 250)
    assert "coverage" in out["failed"] and out["keep"] is False
    empty = fa.compare(base, pd.DataFrame(), lines=pd.DataFrame(), meta=meta, season_types=("regular",))
    assert empty["keep"] is False and empty["failed"] == ["no_predictions"]


def test_e3_needs_one_kept_variant_from_each_family(monkeypatch):
    base_preds = _preds(n=800)

    def fake_table(bundles, cfg):
        return pd.DataFrame({"game_id": base_preds["game_id"], "season": base_preds["season"],
                             "season_type": "regular", "date": base_preds["date"]})

    monkeypatch.setattr(fa, "build_feature_table", fake_table)
    monkeypatch.setattr(fa, "walk_forward", lambda df, kind, cfg=None, **kw: base_preds.copy())
    monkeypatch.setattr(fa, "SEGMENT", {"first_season": "2019-20", "last_season": "2020-21",
                                        "season_types": ("regular",)})
    res = fa.run_data_scope([], pd.DataFrame(), reps=30, seed=1)
    assert "skipped" in res["E3_combined"]


# ---------- cumulative feature steps (P4.1) ----------

def test_feature_steps_are_the_preregistered_order():
    names = [s["name"] for s in fa.CONTEXT_STEPS]
    assert names == ["F1_ctx", "F2_elo_neutral", "F3_travel", "F4_altitude"]
    assert fa.CONTEXT_STEPS[1]["overrides"] == {"ELO_NEUTRAL_HCA0": True}
    assert fa.FEATURE_SEGMENT == {"first_season": "2015-16", "last_season": "2024-25",
                                  "season_types": ("regular", "playoffs", "playin")}


def _stub_feature_world(monkeypatch, better_after):
    """walk_forward stub: predictions improve once the feature list contains `better_after`."""
    base_preds = _preds(n=3000)
    calls = []

    def fake_table(bundles, cfg):
        calls.append(("table", cfg.ELO_NEUTRAL_HCA0))
        return pd.DataFrame({"game_id": base_preds["game_id"], "season": base_preds["season"],
                             "season_type": "regular", "date": base_preds["date"]})

    def fake_walk(df, kind, cfg=None, features=None, **kw):
        calls.append(("walk", tuple(features or ())))
        out = base_preds.copy()
        if features and better_after in features:
            out["mu"] = 0.5 * out["mu"] + 0.5 * out["margin"]
        return out

    monkeypatch.setattr(fa, "build_feature_table", fake_table)
    monkeypatch.setattr(fa, "walk_forward", fake_walk)
    return calls


def test_steps_accumulate_only_kept_groups(monkeypatch):
    calls = _stub_feature_world(monkeypatch, better_after="road_trip_len_away")
    seg = {"first_season": "2019-20", "last_season": "2020-21", "season_types": ("regular",)}
    ids = _preds(n=3000)["game_id"]
    lines = pd.DataFrame({"game_id": ids, "home_spread": np.random.default_rng(3).normal(-2, 5, len(ids)),
                          "home_price": 1.91, "away_price": 1.91})
    res = fa.run_feature_steps([], lines, fa.CONTEXT_STEPS, seg, reps=60, seed=1)
    kept = {s["name"]: s["judgement"]["keep"] for s in res["steps"]}
    assert kept == {"F1_ctx": False, "F2_elo_neutral": False, "F3_travel": True, "F4_altitude": False}
    # F3 was judged against the base (F1/F2 not kept), and F4 against base + travel
    f4_features = [c[1] for c in calls if c[0] == "walk"][-1]
    assert "road_trip_len_away" in f4_features and "altitude_edge" in f4_features
    assert "neutral_game" not in f4_features                      # F1 was not kept
    assert "road_trip_len_away" in res["final_features"] and "neutral_game" not in res["final_features"]


def test_elo_override_only_applies_when_its_step_is_kept(monkeypatch):
    calls = _stub_feature_world(monkeypatch, better_after="never")
    seg = {"first_season": "2019-20", "last_season": "2020-21", "season_types": ("regular",)}
    res = fa.run_feature_steps([], pd.DataFrame(), fa.CONTEXT_STEPS, seg, reps=30, seed=1)
    assert res["final_overrides"] == {}
    assert ("table", True) in calls                                # the step itself was evaluated


def test_cli_context_experiment_dispatches(monkeypatch, tmp_path):
    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(fa, "connect_readonly", lambda: Conn())
    monkeypatch.setattr(fa, "load_bundles", lambda c: [])
    monkeypatch.setattr(fa, "load_closing_lines", lambda c: pd.DataFrame())
    monkeypatch.setattr(fa, "_prepare_session", lambda c: None)
    monkeypatch.setattr(fa, "run_feature_steps", lambda b, l, steps, seg, **kw: {"steps": [], "final_features": []})
    out = tmp_path / "c.json"
    assert fa.main(["--experiment", "context", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["steps"] == []


# ---------- P4.2 independent candidates ----------

def test_four_factor_steps_are_the_preregistered_set():
    steps = fa.FOUR_FACTOR_STEPS
    assert [s["name"] for s in steps] == ["G1_split_k5", "G2_split_k10", "G3_split_k20", "G4_three_rate"]
    assert [s["overrides"].get("SHRINK_K_DEF") for s in steps[:3]] == [5, 10, 20]
    assert steps[0]["remove"] == ["efg10_diff", "tov10_diff", "orb10_diff", "ftr10_diff"]
    assert len(steps[0]["add"]) == 8 and steps[3]["remove"] == []


def test_non_cumulative_steps_are_each_judged_against_fs1(monkeypatch):
    calls = _stub_feature_world(monkeypatch, better_after="efg_off_diff")
    seg = {"first_season": "2019-20", "last_season": "2020-21", "season_types": ("regular",)}
    ids = _preds(n=3000)["game_id"]
    lines = pd.DataFrame({"game_id": ids, "home_spread": np.random.default_rng(3).normal(-2, 5, len(ids)),
                          "home_price": 1.91, "away_price": 1.91})
    res = fa.run_feature_steps([], lines, fa.FOUR_FACTOR_STEPS, seg, reps=60, seed=1, cumulative=False)
    assert res["kept"] == ["G1_split_k5", "G2_split_k10", "G3_split_k20"]
    walks = [c[1] for c in calls if c[0] == "walk"]
    last = walks[-1]                                   # G4 must not inherit the G1-G3 split columns
    assert "three_rate_off_diff" in last and "efg_off_diff" not in last and "efg10_diff" in last
    assert "efg10_diff" not in walks[1]                # the split replaced the old four columns
    assert "final_features" not in res


def test_cli_four_factor_dispatches(monkeypatch, tmp_path):
    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(fa, "connect_readonly", lambda: Conn())
    monkeypatch.setattr(fa, "load_bundles", lambda c: [])
    monkeypatch.setattr(fa, "load_closing_lines", lambda c: pd.DataFrame())
    monkeypatch.setattr(fa, "_prepare_session", lambda c: None)
    seen = {}
    monkeypatch.setattr(fa, "run_feature_steps",
                        lambda b, l, steps, seg, **kw: seen.update(steps=steps, cum=kw.get("cumulative")) or {"kept": []})
    assert fa.main(["--experiment", "four_factor", "--out", str(tmp_path / "f.json")]) == 0
    assert seen["steps"] is fa.FOUR_FACTOR_STEPS and seen["cum"] is False
