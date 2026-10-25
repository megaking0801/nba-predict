"""Helpers for the jobs.quality tests: a fake `run` routed by /* qc:ID */ markers."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pandas as pd

from tests.fakes import FakeDb

WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|COPY|MERGE|CALL|LOCK)\b", re.I)


def fake_run(routes: dict):
    """routes: {"qc:A3": DataFrame | callable(params)->DataFrame}. Returns (run, db)."""
    db = FakeDb(routes)
    return db.run, db


def frame(rows: list, cols: list) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=cols)


def sql_strings(module_path: Path) -> list:
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def assert_select_only(module_path) -> None:
    sqls = [s for s in sql_strings(Path(module_path)) if re.search(r"\bSELECT\b", s, re.I)]
    assert sqls, f"no SQL found in {module_path}"
    for s in sqls:
        assert not WRITE_WORDS.search(s), f"write keyword in SQL: {s[:80]!r}"
