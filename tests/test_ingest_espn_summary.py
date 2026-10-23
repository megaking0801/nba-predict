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
