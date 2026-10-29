"""C3 step 1: the split-half referee persistence check finds real tilts, ignores era and team effects,
keeps crews intact when permuting, and only reads the DB."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import numpy as np
import pandas as pd

from jobs import referee_persistence as rp

TEAMS = ["ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW"]


def _world(n_refs=60, games_per_season=900, seasons=("2016-17", "2017-18", "2018-19"), ref_sd=0.0,
           era=None, team_sd=0.0, seed=0, team_refs=False):
    """Synthetic regular seasons: total residual = era shift + team tilts + crew tilt + noise (sd 18)."""
    rng = np.random.default_rng(seed)
    refs = [f"Ref {i}" for i in range(n_refs)]
    tilt = dict(zip(refs, rng.normal(0, ref_sd, n_refs)))
    team_tilt = dict(zip(TEAMS, rng.normal(0, team_sd, len(TEAMS))))
    games, offs = [], []
    k = 0
    for si, s in enumerate(seasons):
        start = dt.date(2016 + si, 10, 20)
        active = refs[: n_refs // 2] if era and si == 0 else refs       # half the refs only in later seasons
        for j in range(games_per_season):
            h, a = rng.choice(TEAMS, 2, replace=False)
            if team_refs:                                                # some refs always work BOS games
                pool = refs[:5] if "BOS" in (h, a) else refs[5:]
            else:
                pool = active
            crew = list(rng.choice(pool, 3, replace=False))
            r = (era or {}).get(s, 0.0) + team_tilt[h] + team_tilt[a] + sum(tilt[c] for c in crew) + rng.normal(0, 18)
            gid = f"g{k}"
            games.append({"game_id": gid, "season": s, "date": start + dt.timedelta(days=j // 6), "home_abbr": h,
                          "away_abbr": a, "total": 220.0 + r, "total_line": 220.0, "book": "pinnacle",
                          "total_price_point": "close"})
            offs += [{"game_id": gid, "official_order": o + 1, "official_name": c} for o, c in enumerate(crew)]
            k += 1
    return pd.DataFrame(games), pd.DataFrame(offs)


def test_a_real_referee_tilt_is_detected():
    games, offs = _world(ref_sd=4.0, seed=1)
    out = rp.run(games, offs, boot_reps=200, perm_reps=100)
    p = out["versions"]["primary"]
    assert p["rho"] > 0.4 and p["cluster_bootstrap"]["lo"] > 0
    assert p["rho"] > p["permutation_season_x_home"]["q99"]
    assert out["continue_to_step_2"] is True


def test_no_tilt_gives_no_persistence():
    games, offs = _world(ref_sd=0.0, seed=2)
    out = rp.run(games, offs, boot_reps=200, perm_reps=100)
    assert abs(out["versions"]["primary"]["rho"]) < 0.35
    assert out["continue_to_step_2"] is False


def test_season_centring_removes_an_era_effect():
    era = {"2016-17": -8.0, "2017-18": 0.0, "2018-19": 0.0}            # early-only refs look low in both halves
    games, offs = _world(ref_sd=0.0, era=era, seed=3)
    out = rp.run(games, offs, boot_reps=50, perm_reps=20)
    assert out["versions"]["raw_uncentred"]["rho"] > 0.3
    assert abs(out["versions"]["primary"]["rho"]) < 0.35


def test_team_adjustment_removes_refs_tied_to_a_team():
    games, offs = _world(ref_sd=0.0, team_sd=0.0, seed=4, team_refs=True)
    games.loc[(games["home_abbr"] == "BOS") | (games["away_abbr"] == "BOS"), "total"] += 6.0   # BOS games go over
    out = rp.run(games, offs, boot_reps=50, perm_reps=20)
    assert out["versions"]["team_adjusted"]["rho"] < out["versions"]["primary"]["rho"]
    assert out["continue_to_step_2"] is False


def test_halves_alternate_by_date_and_small_refs_are_dropped():
    g = pd.DataFrame({"game_id": [f"g{i}" for i in range(6)], "date": [dt.date(2017, 1, d) for d in (5, 1, 3, 2, 4, 6)],
                      "r_c": [5.0, 1.0, 3.0, 2.0, 4.0, 6.0]})
    p = rp.pairs(g, {f"g{i}": ("X",) for i in range(6)}, "r_c")
    assert list(p["y"]) == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0] and list(p["half"]) == [0, 1, 0, 1, 0, 1]
    assert rp.half_means(p, min_half=3).loc["X", "mean_A"] == 3.0
    assert rp.half_means(p, min_half=4).empty


def test_permutation_keeps_crews_whole_and_inside_the_stratum():
    games, offs = _world(n_refs=12, games_per_season=60, seed=5)
    g, _ = rp.residual_table(games)
    crew_of, _ = rp.crews(offs)
    perm = rp.permuted_crews(g, crew_of, ["season", "home_abbr"], np.random.default_rng(0))
    assert sorted(perm.values()) == sorted(crew_of.values())             # same crews, whole
    for (s, h), grp in g.groupby(["season", "home_abbr"]):
        assert sorted(perm[x] for x in grp["game_id"]) == sorted(crew_of[x] for x in grp["game_id"])


def test_lines_outside_the_range_and_missing_lines_are_counted_and_dropped():
    games, _ = _world(n_refs=6, games_per_season=10, seasons=("2016-17",))
    games.loc[0, "total_line"] = 150.0
    games.loc[1, "total_line"] = None
    g, counts = rp.residual_table(games)
    assert counts["line_outside_170_280"] == 1 and counts["no_total_line"] == 1 and len(g) == 8


def test_only_the_three_on_court_officials_count():
    offs = pd.DataFrame({"game_id": ["a"] * 4, "official_order": [1, 2, 3, 4],
                         "official_name": [" Ann ", "Bob", "Cy", "Alt"]})
    crew_of, info = rp.crews(offs)
    assert crew_of["a"] == ("Ann", "Bob", "Cy") and info["rows_order_above_3"] == 1


def test_name_variants_are_reported_not_merged():
    offs = pd.DataFrame({"game_id": ["a", "a", "a", "b", "b", "b"], "official_order": [1, 2, 3, 1, 2, 3],
                         "official_name": ["Tony Brown", "B", "C", "tony brown", "B", "C"]})
    crew_of, info = rp.crews(offs)
    assert crew_of["a"][0] == "Tony Brown" and crew_of["b"][0] == "tony brown"
    assert info["names_differing_only_in_case_or_punctuation"] == [["Tony Brown", "tony brown"]]


def test_power_table_arithmetic():
    t = rp.power_table(18.0, 150, 70)
    assert abs(t["expected_rho"]["1.0"] - 1 / (1 + 324 / 150)) < 1e-9
    assert abs(t["s_at_rho_0.2"] - 0.735) < 0.01


def test_the_module_only_selects():
    src = Path(rp.__file__).read_text(encoding="utf-8")
    sql = re.findall(r'"""/\* c3:.*?"""', src, re.S)
    assert len(sql) == 3
    for s in sql:
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", s, re.I)
    assert "ensure_schema" not in src and "connect_readonly" in src
