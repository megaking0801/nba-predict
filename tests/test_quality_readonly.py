"""The whole jobs.quality package is read-only."""
from __future__ import annotations

from pathlib import Path

from tests.quality_fakes import assert_select_only, sql_strings, WRITE_WORDS

PKG = Path(__file__).resolve().parents[1] / "jobs" / "quality"


def test_every_module_with_sql_is_select_only():
    checked = 0
    for path in sorted(PKG.glob("*.py")):
        if any("SELECT" in s.upper() for s in sql_strings(path)):
            assert_select_only(path)
            checked += 1
    assert checked >= 7


def test_no_module_executes_ddl_or_dml():
    for path in sorted(PKG.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "commit()" not in text and "executemany" not in text, path.name
        assert "ensure_schema" not in text, path.name        # would create tables in the shared DB


def test_connection_helper_is_the_read_only_one():
    text = (PKG / "runner.py").read_text(encoding="utf-8")
    assert "connect_readonly" in text and "db_connect(" not in text
