"""Stat-universe families: no future information, sane values, and the search's selection logic."""
from __future__ import annotations

import copy
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from jobs import families as fam
from jobs import family_search as fs
from tests.test_features_point_in_time import make_bundles


def _blank_after(bundles, i):
    """Same schedule, but every game after index i has no result, box score or score."""
    out = copy.deepcopy(bundles)
    for b in out[i + 1:]:
        b["margin"] = None
        b["home_score"] = b["away_score"] = None
        b["team_stats"], b["player_stats"] = {}, []
    return out


def _buckets(bundles):
    rows = []
    for b in bundles:
        for t, st in (b.get("team_stats") or {}).items():
            fga = round(st["fga"])
            rows += [(b["game_id"], t, 2, "0-4", False, fga // 3), (b["game_id"], t, 2, "8-14", False, fga // 3),
                     (b["game_id"], t, 3, "24+", True, fga - 2 * (fga // 3))]
    return pd.DataFrame(rows, columns=["game_id", "team_abbr", "pts_value", "dist_bucket", "corner", "fga"])


def _officials(bundles):
    names = ["A", "B", "C", "D", "E"]
    return pd.DataFrame([(b["game_id"], names[(k + j) % 5]) for k, b in enumerate(bundles) for j in range(3)],
                        columns=["game_id", "official_name"])


def _extras(bundles):
    return {"buckets": _buckets(bundles), "officials": _officials(bundles),
            "total_lines": {b["game_id"]: 220.0 for b in bundles}}


@pytest.mark.parametrize("entry", fam.REGISTRY, ids=[e["family"] for e in fam.REGISTRY])
def test_every_family_uses_only_earlier_games(entry):
    bundles = make_bundles(n_games=80, seed=3)
    for b in bundles:
        b["home_score"] = 100 + (b["margin"] or 0)
        b["away_score"] = 100
    variant = entry["variants"][0]
    for i in (5, 40, 79):
        full = entry["build"](bundles, variant, **{k: _extras(bundles)[k] for k in entry.get("needs", [])})
        cut = _blank_after(bundles, i)
        part = entry["build"](cut, variant, **{k: _extras(bundles)[k] for k in entry.get("needs", [])})
        gid = bundles[i]["game_id"]
        a = full.set_index("game_id").loc[gid]
        b = part.set_index("game_id").loc[gid]
        pd.testing.assert_series_equal(a, b, check_names=False)


def test_srs_recovers_the_strength_order():
    rng = np.random.default_rng(0)
    strength = {"BOS": 6.0, "LAL": 2.0, "GSW": -2.0, "WAS": -6.0}
    teams = list(strength)
    bundles = []
    d = dt.date(2024, 10, 22)
    for i in range(160):
        h, a = rng.choice(teams, 2, replace=False)
        m = strength[h] - strength[a] + 3 + rng.normal(0, 8)
        bundles.append({"game_id": f"g{i}", "season": "2024-25", "season_type": "regular", "date": d,
                        "home_abbr": h, "away_abbr": a, "margin": round(m) or 1})
        d += dt.timedelta(days=1)
    out = fam.family_srs(bundles, {"lam": 1.0}).set_index("game_id")
    last = bundles[-1]
    assert np.sign(out.loc[last["game_id"], "srs_diff"]) == np.sign(strength[last["home_abbr"]] - strength[last["away_abbr"]])
    assert out.loc["g0", "srs_diff"] == 0.0                     # fewer than 30 games: no rating yet


def test_shots_skip_games_whose_buckets_do_not_match_the_box():
    bundles = make_bundles(n_games=30, seed=4)
    good = _buckets(bundles)
    bad = good.copy()
    bad.loc[bad["game_id"] == bundles[0]["game_id"], "fga"] += 10   # game 0 no longer adds up
    b = fam.family_shots(bundles, {"n": 10}, bad).set_index("game_id")
    assert (b.loc[bundles[0]["game_id"]] == 0).all()


def test_motivation_is_zero_when_many_games_are_left():
    bundles = make_bundles(n_games=40, seed=5)
    out = fam.family_motivation(bundles, {"left": 0})
    assert (out.drop(columns="game_id") == 0).all().all()


def test_noise_is_deterministic_and_order_free():
    bundles = make_bundles(n_games=20, seed=6)
    a = fam.family_noise(bundles, {"seed": 1}).set_index("game_id")
    b = fam.family_noise(list(reversed(bundles)), {"seed": 1}).set_index("game_id")
    pd.testing.assert_frame_equal(a.sort_index(), b.sort_index())
    assert not a.equals(fam.family_noise(bundles, {"seed": 2}).set_index("game_id"))


def test_registry_is_frozen_and_declares_the_search_space():
    assert [e["family"] for e in fam.REGISTRY] == ["F1_srs", "F2_player_eff", "F3_rotation", "F4_shots",
                                                    "F5_motivation", "F7_refs_total", "N_noise"]
    assert all(len(e["variants"]) == 3 for e in fam.REGISTRY)
    assert fam.SEARCH_SPACE == 36
    assert {x["family"] for x in fam.REGISTERED_NOT_RUN} == {"F6_rapm", "F8_line_move", "F9_availability"}
    refs = next(e for e in fam.REGISTRY if e["family"] == "F7_refs_total")
    assert refs["target"] == "total" and "oracle" in refs["caveat"]


# ---------- selection logic ----------

def test_benjamini_hochberg():
    p = {"a": 0.001, "b": 0.004, "c": 0.03, "d": 0.5}
    assert fs.benjamini_hochberg(p, 0.10, 4) == ["a", "b", "c"]
    assert fs.benjamini_hochberg(p, 0.10, 36) == ["a", "b"]     # m = 36: thresholds 0.0028, 0.0056, ...
    assert fs.benjamini_hochberg({"a": 0.004}, 0.10, 36) == []  # alone, 0.004 > 0.10/36
    assert fs.benjamini_hochberg({"x": 0.2}, 0.10, 1) == []


def test_summarize_reports_detectability_and_mde():
    rng = np.random.default_rng(1)
    d = pd.DataFrame({"date": [dt.date(2020, 1, 1) + dt.timedelta(days=i // 5) for i in range(4000)],
                      "season": "2019-20", "d": rng.normal(-0.05, 1.0, 4000)})
    s = fs.summarize(d, 0.99, 300, 1, 36)
    assert s["n"] == 4000 and s["p_one_sided"] < 0.05
    assert s["mde_bonferroni_search_space"] > s["mde_at_level"] > 0
    assert s["detectable"] == (s["half_width"] <= 0.02)


def _stub_run(monkeypatch, winners):
    """Stub the expensive parts: each hypothesis gets an explore result with a chosen p and mean."""
    def fake_eval(cand, meta, seasons, level, reps, seed, m_tests):
        hid = cand.attrs["hid"]
        good = hid in winners
        return {"n": 3000, "mean": -0.05 if good else 0.0, "lo": -0.08 if good else -0.03,
                "hi": -0.02 if good else 0.03, "p_one_sided": 1e-6 if good else 0.6, "half_width": 0.03,
                "share_seasons_negative": 1.0 if good else 0.4, "beta_lower": 0.2 if good else -0.1}
    monkeypatch.setattr(fs, "evaluate", fake_eval)
    monkeypatch.setattr(fs, "build_feature_table", lambda bundles, cfg: pd.DataFrame(
        {"game_id": ["g"], "season": ["2016-17"], "season_type": ["regular"], "margin": [1.0], "total": [200.0],
         "date": [dt.date(2017, 1, 1)], "eligible": [True]}))

    def fake_pred(t, feats):
        f = pd.DataFrame({"game_id": ["g"]})
        f.attrs["hid"] = fake_pred.current
        return f
    fake_pred.current = None
    monkeypatch.setattr(fs, "predictions", fake_pred)
    reg = []
    for e in fam.REGISTRY:
        e2 = dict(e)
        e2["build"] = (lambda fam_name: (lambda bundles, variant, **kw: _mark(fake_pred, fam_name, variant)))(e["family"])
        reg.append(e2)
    return reg


def _mark(fake_pred, fam_name, variant):
    idx = [str(v) for v in next(e for e in fam.REGISTRY if e["family"] == fam_name)["variants"]].index(str(variant))
    fake_pred.current = f"{fam_name}/v{idx + 1}"
    return pd.DataFrame({"game_id": ["g"], "x": [0.0]})


def test_a_picked_noise_family_voids_the_round(monkeypatch):
    reg = _stub_run(monkeypatch, {"N_noise/v1"})
    out = fs.run([], pd.DataFrame({"game_id": ["g"], "home_spread": [-3.0], "total_line": [200.0]}), {},
                 registry=reg, n_shuffles=0)
    assert out["voided"] is True and "validate" not in str(out["hypotheses"])


def test_candidates_go_to_validation_once(monkeypatch):
    reg = _stub_run(monkeypatch, {"F1_srs/v2"})
    out = fs.run([], pd.DataFrame({"game_id": ["g"], "home_spread": [-3.0], "total_line": [200.0]}), {},
                 registry=reg, n_shuffles=0)
    assert out["voided"] is False and out["candidates"] == ["F1_srs/v2"]
    assert set(h for h, v in out["hypotheses"].items() if "validate" in v) == {"F1_srs/v2"}
    assert out["search_space_declared"] == 36 and len(out["not_run"]) == 3
