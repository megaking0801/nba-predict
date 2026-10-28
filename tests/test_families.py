"""Stat-universe families: no future or same-day information, sane values, and the search's selection logic."""
from __future__ import annotations

import copy
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from jobs import families as fam
from jobs import family_search as fs
from tests.test_features_point_in_time import make_bundles


def _blank_from(bundles, extras, i):
    """Same schedule, but game i, every other game on its date, and every later game have no result,
    box score, player stats, score, shot buckets or total line. A family's row for game i must not change."""
    out = copy.deepcopy(bundles)
    day = out[i]["date"]
    gone = set()
    for b in out:
        if b["date"] >= day:
            gone.add(b["game_id"])
            b["margin"] = None
            b["home_score"] = b["away_score"] = None
            b["team_stats"], b["player_stats"] = {}, []
    ex = dict(extras)
    ex["buckets"] = extras["buckets"][~extras["buckets"]["game_id"].isin(gone)]
    ex["total_lines"] = {g: v for g, v in extras["total_lines"].items() if g not in gone}
    return out, ex


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


def _world(n=80, seed=3):
    bundles = make_bundles(n_games=n, seed=seed)
    for b in bundles:
        b["home_score"] = 100 + (b["margin"] or 0)
        b["away_score"] = 100
    extras = {"buckets": _buckets(bundles), "officials": _officials(bundles),
              "total_lines": {b["game_id"]: 210.0 + (k % 7) * 3 for k, b in enumerate(bundles)}}
    return bundles, extras


def _pit_mismatches(build, variant, needs, bundles, extras, at=(5, 21, 40, 61, 79)):
    """Indices whose row changes when that game, its date and everything later are blanked."""
    full = build(bundles, variant, **{k: extras[k] for k in needs}).set_index("game_id")
    bad = []
    for i in at:
        part_b, part_x = _blank_from(bundles, extras, i)
        part = build(part_b, variant, **{k: part_x[k] for k in needs}).set_index("game_id")
        gid = bundles[i]["game_id"]
        if not full.loc[gid].equals(part.loc[gid]):
            bad.append(i)
    return bad


CASES = [(e, v) for e in fam.REGISTRY for v in e["variants"]] + \
        [(next(e for e in fam.REGISTRY if e["family"] == "F5_motivation"), {"left": 999})]   # standings always on


@pytest.mark.parametrize("entry,variant", CASES, ids=[f'{e["family"]}-{v}' for e, v in CASES])
def test_every_family_uses_only_earlier_days(entry, variant):
    bundles, extras = _world()
    assert _pit_mismatches(entry["build"], variant, entry.get("needs", []), bundles, extras) == []


def test_the_leak_check_catches_a_family_that_reads_its_own_result():
    def leaky(bundles, variant):
        return pd.DataFrame([{"game_id": b["game_id"], "x": float(b["margin"] or 0)} for b in fam._ordered(bundles)])
    bundles, extras = _world()
    assert _pit_mismatches(leaky, {}, [], bundles, extras) != []


def test_the_leak_check_catches_same_day_standings():
    def same_day(bundles, variant):
        wins, rows = {}, []
        for b in fam._ordered(bundles):
            rows.append({"game_id": b["game_id"], "x": float(wins.get(b["home_abbr"], 0))})
            if b.get("margin") is not None:                       # updated immediately: same-day games see it
                wins[b["home_abbr"]] = wins.get(b["home_abbr"], 0) + int(b["margin"] > 0)
                wins[b["away_abbr"]] = wins.get(b["away_abbr"], 0) + int(b["margin"] < 0)
        return pd.DataFrame(rows)
    bundles, extras = _world()
    assert _pit_mismatches(same_day, {}, [], bundles, extras, at=range(1, 80, 2)) != []


def test_motivation_standings_are_exercised_and_vary():
    bundles, _ = _world()
    out = fam.family_motivation(bundles, {"left": 999})
    assert out["gb10_diff"].abs().sum() > 0


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
    bundles, extras = _world(30, 4)
    bad = extras["buckets"].copy()
    bad.loc[bad["game_id"] == bundles[0]["game_id"], "fga"] += 10   # game 0 no longer adds up
    good_out = fam.family_shots(bundles, {"n": 10}, extras["buckets"]).set_index("game_id")
    bad_out = fam.family_shots(bundles, {"n": 10}, bad).set_index("game_id")
    first_next = next(b for b in bundles[1:] if b["date"] > bundles[0]["date"] and
                      {b["home_abbr"], b["away_abbr"]} & {bundles[0]["home_abbr"], bundles[0]["away_abbr"]})
    assert not good_out.loc[first_next["game_id"]].equals(bad_out.loc[first_next["game_id"]])


def test_player_rotation_only_counts_players_from_the_last_five_games():
    bundles, _ = _world(60, 7)
    team = bundles[0]["home_abbr"]
    with_star = copy.deepcopy(bundles)
    team_games = [b for b in with_star if team in (b["home_abbr"], b["away_abbr"])]
    for b in team_games[:3]:                                        # a star plays the team's first 3 games, then leaves
        b["player_stats"].append({"player_id": 999999, "team_abbr": team, "min_played": 40.0, "pts": 60.0,
                                  "fga": 20.0, "fta": 0.0, "tov": 1.0})
    a = fam.family_player_eff(bundles, {"lam": 0.3}).set_index("game_id")
    b = fam.family_player_eff(with_star, {"lam": 0.3}).set_index("game_id")
    later = [g["game_id"] for g in team_games[3 + 5 + 1:]]         # 5 team games after he left, plus a day's buffer
    assert later and a.loc[later].equals(b.loc[later])
    assert not a.loc[team_games[2]["game_id"]].equals(b.loc[team_games[2]["game_id"]]) or \
        not a.loc[team_games[3]["game_id"]].equals(b.loc[team_games[3]["game_id"]])


def test_motivation_does_not_learn_the_season_length_from_later_games():
    bundles, _ = _world()
    for left in (10, 20, 30, 999):
        full = fam.family_motivation(bundles, {"left": left}).set_index("game_id")
        for i in (10, 41, 70):
            cut = [b for b in bundles if b["date"] <= bundles[i]["date"]]   # the season "ends" early
            part = fam.family_motivation(cut, {"left": left}).set_index("game_id")
            assert full.loc[bundles[i]["game_id"]].equals(part.loc[bundles[i]["game_id"]]), (left, i)


def test_season_length_is_known_in_advance():
    assert fam.season_length("2024-25", dt.date(2025, 1, 1), [])["BOS"] == 82
    assert fam.season_length("2020-21", dt.date(2021, 1, 1), [])["BOS"] == 72
    assert fam.season_length("2019-20", dt.date(2020, 2, 1), [])["BOS"] == 82      # before the stop


def test_motivation_is_zero_when_many_games_are_left():
    bundles, _ = _world(40, 5)
    out = fam.family_motivation(bundles, {"left": 0})
    assert (out.drop(columns="game_id") == 0).all().all()


def test_noise_is_deterministic_and_order_free():
    bundles, _ = _world(20, 6)
    a = fam.family_noise(bundles, {"seed": 1}).set_index("game_id")
    b = fam.family_noise(list(reversed(bundles)), {"seed": 1}).set_index("game_id")
    pd.testing.assert_frame_equal(a.sort_index(), b.sort_index())
    assert not a.equals(fam.family_noise(bundles, {"seed": 2}).set_index("game_id"))


def test_registry_is_frozen_and_declares_the_search_space():
    assert [e["family"] for e in fam.REGISTRY] == ["F1_srs", "F2_player_eff", "F3_rotation", "F4_shots",
                                                    "F5_motivation", "F7_refs_total", "N_noise", "N_noise_total"]
    assert all(len(e["variants"]) == 3 for e in fam.REGISTRY)
    assert {e["target"] for e in fam.REGISTRY if e["family"].startswith("N_noise")} == {"margin", "total"}
    assert fam.SEARCH_SPACE == 42
    assert {x["family"] for x in fam.REGISTERED_NOT_RUN} == {"F6_rapm", "F8_line_move", "F9_availability"}
    refs = next(e for e in fam.REGISTRY if e["family"] == "F7_refs_total")
    assert refs["target"] == "total" and "oracle" in refs["caveat"]


# ---------- residual tables, scoring and controls ----------

def _df(n=10):
    return pd.DataFrame({"game_id": [f"g{i}" for i in range(n)], "season": "2016-17", "season_type": "regular",
                         "date": [dt.date(2017, 1, 1) + dt.timedelta(days=i) for i in range(n)], "eligible": True,
                         "margin": [7.0] * n, "total": [230.0] * (n - 1) + [None]})


def test_target_table_signs_and_total_filter():
    lines = pd.DataFrame({"game_id": [f"g{i}" for i in range(10)], "home_spread": [-5.0] * 10,
                          "total_line": [220.0] * 8 + [150.0, 220.0]})
    m = fs.target_table(_df(), lines, "margin")
    assert (m["resid"] == 2.0).all() and (m["line_mu"] == 5.0).all()          # 7 - 5: home beat the line by 2
    t = fs.target_table(_df(), lines, "total")
    assert len(t) == 8 and (t["resid"] == 10.0).all()                          # 150 dropped, NaN total dropped
    assert (t["spread_for_beta"] == -220.0).all()


def _scored(n=600, signal=0.0, seed=0):
    rng = np.random.default_rng(seed)
    line = rng.normal(0, 6, n)
    actual = line + rng.normal(0, 12, n)
    dates = [dt.date(2016, 11, 1) + dt.timedelta(days=i // 4) for i in range(n)]
    cand = pd.DataFrame({"game_id": [f"g{i}" for i in range(n)], "date": dates, "season": "2016-17",
                         "actual": actual, "margin": actual, "line_mu": line, "spread_for_beta": -line,
                         "mu": line + signal * (actual - line)})
    meta = cand[["game_id", "season"]].assign(season_type="regular")
    return cand, meta


def test_evaluate_scores_against_the_line_and_reports_power_mde():
    cand, meta = _scored()
    same = fs.evaluate(cand, meta, ("2015-16", "2021-22"), 0.99, 200, 1, 36)
    assert same["mean"] == 0 and same["n"] == 600
    good = fs.evaluate(_scored(signal=0.5)[0], meta, ("2015-16", "2021-22"), 0.99, 200, 1, 36)
    assert good["mean"] < -1 and good["p_one_sided"] < 0.001 and good["beta_lower"] > 0
    assert good["mde_80pct_power"] > good["mde_50pct_power"] > 0
    assert fs.evaluate(cand, meta, ("2022-23", "2024-25"), 0.99, 200, 1, 36)["too_small"] is True


def test_evaluate_reports_the_increment_over_the_base_features():
    base, meta = _scored(signal=0.5)
    same = fs.evaluate(base, meta, ("2015-16", "2021-22"), 0.99, 200, 1, 36, base=base)
    assert same["mean"] < -1 and same["vs_base"]["mean"] == 0          # beats the line, adds nothing to the base
    better = _scored(signal=0.7)[0]
    more = fs.evaluate(better, meta, ("2015-16", "2021-22"), 0.99, 200, 1, 36, base=base)
    assert more["vs_base"]["mean"] < 0 and more["vs_base"]["p_one_sided"] < 0.01


def test_block_bootstrap_beta_has_no_lower_bound_above_zero_without_signal():
    cand, _ = _scored(n=2000, signal=0.0, seed=5)
    cand["mu"] = cand["line_mu"] + np.random.default_rng(1).normal(0, 1, len(cand))   # noise forecast
    assert fs.beta_lower_block(cand, 0.99, 500, 0) < 0


def test_shuffle_moves_only_the_family_columns_within_a_season():
    t = pd.DataFrame({"season": ["a"] * 4 + ["b"] * 4, "x": [1.0, 2, 3, 4, 10, 20, 30, 40],
                      "resid": range(8), "base": range(8)})
    s = fs.shuffle_within_season(t, ["x"], np.random.default_rng(0))
    assert sorted(s["x"][:4]) == [1, 2, 3, 4] and sorted(s["x"][4:]) == [10, 20, 30, 40]
    assert list(s["resid"]) == list(range(8)) and list(s["base"]) == list(range(8))


def test_null_p_value_is_not_small():
    rng = np.random.default_rng(3)
    d = pd.DataFrame({"date": [dt.date(2020, 1, 1) + dt.timedelta(days=i // 5) for i in range(4000)],
                      "season": "2019-20", "d": rng.normal(0.0, 1.0, 4000)})
    s = fs.summarize(d, 0.99, 400, 1, 36)
    assert 0.05 < s["p_one_sided"] < 0.95


def test_shift_by_team_uses_the_home_teams_next_game():
    t = pd.DataFrame({"game_id": ["a", "b", "c", "d"], "home_abbr": ["BOS", "LAL", "MIA", "BOS"],
                      "away_abbr": ["LAL", "MIA", "BOS", "MIA"],
                      "date": [dt.date(2020, 1, d) for d in (1, 2, 3, 4)], "x": [1.0, 2.0, 3.0, 4.0]})
    s = fs.shift_by_team(t, ["x"])
    # BOS (home in a) next plays in c; LAL (home in b) has no later game; MIA (home in c) next plays in d
    assert list(s["game_id"]) == ["a", "c"] and list(s["x"]) == [3.0, 4.0]


# ---------- selection logic ----------

def test_benjamini_hochberg():
    p = {"a": 0.001, "b": 0.004, "c": 0.03, "d": 0.5}
    assert fs.benjamini_hochberg(p, 0.10, 4) == ["a", "b", "c"]
    assert fs.benjamini_hochberg(p, 0.10, 36) == ["a", "b"]     # m = 36: thresholds 0.0028, 0.0056, ...
    assert fs.benjamini_hochberg({"a": 0.004}, 0.10, 36) == []  # alone, 0.004 > 0.10/36
    assert fs.benjamini_hochberg({"x": 0.2}, 0.10, 1) == []


def _stub_run(monkeypatch, winners, calls):
    """Stub the expensive parts; record every evaluate call (hypothesis, seasons, level, m)."""
    def fake_eval(cand, meta, seasons, level, reps, seed, m_tests, base=None):
        hid = cand.attrs.get("hid") or "base"
        calls.append((hid, seasons, level, m_tests))
        good = hid in winners
        res = {"n": 3000, "mean": -0.05 if good else 0.0, "lo": -0.08 if good else -0.03,
               "hi": -0.02 if good else 0.03, "p_one_sided": 1e-6 if good else 0.6, "half_width": 0.03,
               "share_seasons_negative": 1.0 if good else 0.4, "beta_lower": 0.2 if good else -0.1}
        if base is not None:
            res["vs_base"] = {"mean": -0.04 if good else 0.0, "hi": -0.01 if good else 0.02,
                              "p_one_sided": 1e-6 if good else 0.5}
        return res
    monkeypatch.setattr(fs, "evaluate", fake_eval)
    monkeypatch.setattr(fs, "build_feature_table", lambda bundles, cfg: pd.DataFrame(
        {"game_id": ["g"], "season": ["2016-17"], "season_type": ["regular"], "margin": [1.0], "total": [200.0],
         "date": [dt.date(2017, 1, 1)], "eligible": [True], "home_abbr": ["BOS"], "away_abbr": ["LAL"]}))

    def fake_pred(t, feats):
        f = pd.DataFrame({"game_id": ["g"]})
        f.attrs["hid"] = fake_pred.current
        return f
    fake_pred.current = None
    monkeypatch.setattr(fs, "predictions", fake_pred)
    monkeypatch.setattr(fs, "controls", lambda *a, **k: calls.append(("controls",)) or
                        {"true_beats_every_shuffle": True})
    reg = []
    for e in fam.REGISTRY:
        e2 = dict(e)
        e2["build"] = (lambda name: (lambda bundles, variant, **kw: _mark(fake_pred, name, variant)))(e["family"])
        reg.append(e2)
    return reg


def _mark(fake_pred, fam_name, variant):
    idx = [str(v) for v in next(e for e in fam.REGISTRY if e["family"] == fam_name)["variants"]].index(str(variant))
    fake_pred.current = f"{fam_name}/v{idx + 1}"
    return pd.DataFrame({"game_id": ["g"], "x": [0.0]})


LINES = pd.DataFrame({"game_id": ["g"], "home_spread": [-3.0], "total_line": [200.0]})


def test_a_picked_noise_family_voids_the_round(monkeypatch):
    calls = []
    reg = _stub_run(monkeypatch, {"N_noise/v1"}, calls)
    out = fs.run([], LINES, {}, registry=reg, n_shuffles=0)
    assert out["voided"] is True
    assert not any(c[1] == fs.VALIDATE for c in calls if len(c) > 1)


def test_the_total_noise_family_also_voids_the_round(monkeypatch):
    calls = []
    reg = _stub_run(monkeypatch, {"N_noise_total/v2"}, calls)
    assert fs.run([], LINES, {}, registry=reg, n_shuffles=0)["voided"] is True


def test_selection_uses_the_increment_over_the_base(monkeypatch):
    """A family whose vs-line numbers look great but adds nothing over the base is not a candidate."""
    calls = []
    reg = _stub_run(monkeypatch, {"F7_refs_total/v1"}, calls)
    real_eval = fs.evaluate

    def no_increment(*a, **k):
        res = real_eval(*a, **k)
        if "vs_base" in res:
            res["vs_base"] = {"mean": 0.0, "hi": 0.02, "p_one_sided": 0.5}
        return res
    monkeypatch.setattr(fs, "evaluate", no_increment)
    out = fs.run([], LINES, {}, registry=reg, n_shuffles=0)
    assert out["bh_picked"] == [] and out["candidates"] == [] and out["passed_validation"] == []


def test_each_candidate_is_validated_exactly_once_at_the_bonferroni_level(monkeypatch):
    calls = []
    reg = _stub_run(monkeypatch, {"F1_srs/v2", "F3_rotation/v1"}, calls)
    out = fs.run([], LINES, {}, registry=reg, n_shuffles=5)
    assert out["voided"] is False and set(out["candidates"]) == {"F1_srs/v2", "F3_rotation/v1"}
    val = [c for c in calls if len(c) > 1 and c[1] == fs.VALIDATE]
    assert sorted(c[0] for c in val) == ["F1_srs/v2", "F3_rotation/v1"]
    assert all(c[2] == pytest.approx(1 - 0.01 / 2) and c[3] == 2 for c in val)
    assert calls.count(("controls",)) == 2
    assert all(c[1] in (fs.EXPLORE, fs.VALIDATE) for c in calls if len(c) > 1)     # never the confirmation seasons
    assert out["search_space_declared"] == 42 and len(out["not_run"]) == 3
    assert "base_reference" in out and "cumulative_steps" in out and out["not_evaluated"]
    assert set(out["passed_validation"]) == {"F1_srs/v2", "F3_rotation/v1"}
    explore = [c for c in calls if len(c) > 1 and c[1] == fs.EXPLORE and c[0] != "base"]
    assert len(explore) == 24 and all(c[3] == 42 for c in explore)
