"""Characterization + new-output tests for jobs.ingest_espn.parse_summary.

The fixture is a trimmed real ESPN summary of a finished game
(tests/fixtures/espn/summary_final_sample.json). summary_expected_rows.json holds
the team/player rows the parser produced BEFORE it was extended, so any change to
the existing outputs (and thus to player_game_stats / team_game_stats) shows up.
"""
from __future__ import annotations

import json
from pathlib import Path

from jobs import ingest_espn

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def meta_from_header(bs: dict) -> dict:
    comp = bs["header"]["competitions"][0]
    sides = {c["homeAway"]: c for c in comp["competitors"]}
    return {
        "game_id": str(bs["header"]["id"]),
        "home_abbr": ingest_espn.normalize_espn_abbr(sides["home"]["team"]["abbreviation"]),
        "away_abbr": ingest_espn.normalize_espn_abbr(sides["away"]["team"]["abbreviation"]),
        "home_score": int(sides["home"]["score"]),
        "away_score": int(sides["away"]["score"]),
    }


def _rows(parsed):
    """Works for both the old (team, player) tuple and the extended NamedTuple."""
    return parsed[0], parsed[1]


def test_parse_summary_rows_unchanged():
    bs = _load("summary_final_sample.json")
    expected = _load("summary_expected_rows.json")
    team_rows, player_rows = _rows(ingest_espn.parse_summary(meta_from_header(bs), bs))
    assert [list(r) for r in team_rows] == expected["team_rows"]
    assert [list(r) for r in player_rows] == expected["player_rows"]
    assert len(team_rows) == 2 and len(player_rows) > 10


# ---------- extended outputs (status, officials, shot buckets) ----------

import copy  # noqa: E402

import pytest  # noqa: E402


def _parse(bs: dict):
    return ingest_espn.parse_summary(meta_from_header(bs), bs)


def _listed_players(bs: dict) -> int:
    return sum(1 for tb in bs["boxscore"]["players"]
               for a in tb["statistics"][0]["athletes"]
               if (a.get("athlete") or {}).get("id") is not None)


def test_status_rows_cover_every_listed_player():
    bs = _load("summary_final_sample.json")
    parsed = _parse(bs)
    expected = _load("summary_expected_rows.json")
    assert len(parsed.status_rows) == _listed_players(bs) == 29
    assert sum(1 for r in parsed.status_rows if r[4]) == 10                 # starters
    dnp = [r for r in parsed.status_rows if r[5]]
    assert len(dnp) == 8 and all(r[6] for r in dnp)                         # reason present
    assert all(r[6] is None for r in parsed.status_rows if not r[5])
    assert all(len(r) == 8 for r in parsed.status_rows)
    # the played-players output is exactly what it was before: no DNP rows leak in
    assert len(parsed.player_rows) == len(expected["player_rows"])


def test_officials_parsed():
    parsed = _parse(_load("summary_final_sample.json"))
    assert [(r[1], r[2]) for r in parsed.official_rows] == [
        (1, "Sean Wright"), (2, "Marat Kogut"), (3, "Marc Davis")]
    assert all(r[0] == "401869396" for r in parsed.official_rows)


def test_officials_missing_is_empty():
    bs = _load("summary_final_sample.json")
    del bs["gameInfo"]
    assert _parse(bs).official_rows == []
    bs = _load("summary_final_sample.json")
    bs["gameInfo"]["officials"] = []
    assert _parse(bs).official_rows == []


def test_shot_bucket_boundaries():
    f = ingest_espn.shot_bucket
    assert f(25, 3, 2) == ("0-4", False)
    assert f(25, 4, 2) == ("4-8", False)
    assert f(25, 8, 2) == ("8-14", False)
    assert f(25, 14, 2) == ("14-24", False)
    assert f(25, 24, 3) == ("24+", False)
    assert f(48, 9, 3) == ("24+", True)          # corner three: y <= 9
    assert f(48, 10, 3) == ("24+", False)
    assert f(48, 9, 2) == ("24+", False)         # corner needs a three
    assert f(None, None, 2) == ("unknown", False)
    assert f(-214748340, 0, 2) == ("unknown", False)
    assert f(25, None, 3) == ("unknown", False)


def _shots_by_team(bs: dict) -> dict:
    team_abbr = {t["team"]["id"]: ingest_espn.normalize_espn_abbr(t["team"]["abbreviation"])
                 for t in bs["boxscore"]["teams"]}
    out: dict = {}
    for p in bs["plays"]:
        if (p.get("shootingPlay") and "Free Throw" not in (p.get("type") or {}).get("text", "")
                and p.get("pointsAttempted") in (2, 3) and (p.get("team") or {}).get("id") in team_abbr):
            a = team_abbr[p["team"]["id"]]
            fga, fgm = out.get(a, (0, 0))
            out[a] = (fga + 1, fgm + (1 if p.get("scoringPlay") else 0))
    return out


def test_shot_rows_sum_to_attempts():
    bs = _load("summary_final_sample.json")
    parsed = _parse(bs)
    expected = _shots_by_team(bs)
    got: dict = {}
    for gid, abbr, pts, bucket, corner, fga, fgm in parsed.shot_rows:
        assert gid == "401869396" and pts in (2, 3)
        a, b = got.get(abbr, (0, 0))
        got[abbr] = (a + fga, b + fgm)
    assert got == expected and sum(v[0] for v in expected.values()) == 179
    # one row per (team, pts, bucket, corner): the aggregation key is unique
    keys = [r[1:5] for r in parsed.shot_rows]
    assert len(keys) == len(set(keys))


def test_shot_play_without_team_is_skipped(capsys):
    bs = _load("summary_final_sample.json")
    victim = next(p for p in bs["plays"] if p.get("shootingPlay")
                  and "Free Throw" not in (p.get("type") or {}).get("text", ""))
    del victim["team"]
    parsed = _parse(bs)
    assert sum(r[5] for r in parsed.shot_rows) == 178
    assert "without a matching team" in capsys.readouterr().out


def test_shot_play_with_unknown_team_id_is_skipped():
    bs = _load("summary_final_sample.json")
    victim = next(p for p in bs["plays"] if p.get("shootingPlay")
                  and "Free Throw" not in (p.get("type") or {}).get("text", ""))
    victim["team"] = {"id": "9999"}
    assert sum(r[5] for r in _parse(bs).shot_rows) == 178


def test_ejected_player_kept():
    bs = _load("summary_final_sample.json")
    expected = _load("summary_expected_rows.json")
    target = next(a for a in bs["boxscore"]["players"][0]["statistics"][0]["athletes"]
                  if a.get("stats") and not a.get("didNotPlay"))
    target["ejected"] = True
    pid = int(target["athlete"]["id"])
    parsed = _parse(bs)
    assert len(parsed.player_rows) == len(expected["player_rows"])
    assert any(r[1] == pid for r in parsed.player_rows)
    assert next(r for r in parsed.status_rows if r[1] == pid)[7] is True


def test_no_box_returns_all_empty():
    parsed = ingest_espn.parse_summary({"game_id": "1", "home_abbr": "BOS", "away_abbr": "PHI",
                                        "home_score": 1, "away_score": 0}, {})
    assert all(rows == [] for rows in parsed)
    assert len(parsed) == 5
