"""Tests for webui.queries (read-only SQL helpers)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from tests import fakes
from webui import queries

SRC = Path(__file__).resolve().parents[1] / "webui" / "queries.py"
WRITE_WORDS = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT)\b", re.I)


def test_queries_are_read_only():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    strings = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    sqlish = [s for s in strings if re.search(r"\bSELECT\b", s, re.I)]
    assert sqlish, "expected SQL strings in queries.py"
    for s in strings:
        assert not WRITE_WORDS.search(s), f"write keyword in: {s[:60]!r}"


def test_write_keyword_scan_ignores_column_names():
    assert not WRITE_WORDS.search("SELECT updated_at, created_at FROM t")
    assert WRITE_WORDS.search("update t set a=1")


def test_track_record_shape():
    tr = queries.track_record(fakes.scenario_board().run)
    assert set(tr) == {"wf_acc", "wf_n", "rec_hit", "rec_n", "over_wf_acc",
                       "over_wf_n", "over_rec_hit", "over_rec_n"}
    assert tr["rec_n"] == 60 and tr["wf_acc"] == 0.655


def test_latest_season_fallback():
    assert queries.latest_season(fakes.scenario_empty().run) == "2025-26"


def test_recent_games_passes_limit():
    db = fakes.scenario_board()
    queries.recent_games(db.run, 7)
    assert db.calls[-1][1] == (7,)
