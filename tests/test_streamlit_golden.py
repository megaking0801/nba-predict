"""Whole-page characterization (golden) tests for the Streamlit app.

Run with UPDATE_GOLDEN=1 to (re)write tests/golden/*.txt. The page output must
stay byte-identical while rendering code is moved out of auto_nba_predict.py.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg2
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tests import fakes

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden"
APP = str(ROOT / "auto_nba_predict.py")
SEP = "\n<!--chunk-->\n"


def render_page(db: fakes.FakeDb, query_params: dict | None = None) -> str:
    st.cache_data.clear()
    at = AppTest.from_file(APP, default_timeout=30)
    if query_params:
        at.query_params = query_params
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    parts = [f"markdown:{m.value}" for m in at.markdown]
    parts += [f"info:{i.value}" for i in at.info]
    parts += [f"caption:{c.value}" for c in at.caption]
    for s in at.selectbox:
        parts.append(f"selectbox:{s.label}|{list(s.options)}")
    for e in at.expander:
        parts.append(f"expander:{e.label}")
    text = SEP.join(parts)
    return re.sub(r"\d{4}\.\d{2}\.\d{2}", "DATE", text)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")


def _check(name: str, db: fakes.FakeDb, monkeypatch, query_params: dict | None = None):
    monkeypatch.setattr(psycopg2, "connect", db.connect)
    got = render_page(db, query_params)
    path = GOLDEN / f"{name}.txt"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(got, encoding="utf-8", newline="\n")
    want = path.read_text(encoding="utf-8")
    assert got == want


def test_golden_board(monkeypatch):
    _check("board", fakes.scenario_board(), monkeypatch)


def test_golden_detail_upcoming(monkeypatch):
    _check("detail_upcoming", fakes.scenario_board(), monkeypatch,
           {"game": fakes.GAME_UPCOMING})


def test_golden_detail_final(monkeypatch):
    _check("detail_final", fakes.scenario_board(), monkeypatch, {"game": fakes.GAME_FINAL})


def test_golden_detail_noline(monkeypatch):
    _check("detail_noline", fakes.scenario_board(), monkeypatch, {"game": fakes.GAME_NOLINE})


def test_golden_detail_missing(monkeypatch):
    _check("detail_missing", fakes.scenario_board(), monkeypatch, {"game": fakes.GAME_MISSING})


def test_golden_offseason(monkeypatch):
    _check("offseason", fakes.scenario_offseason(), monkeypatch)


def test_golden_empty(monkeypatch):
    _check("empty", fakes.scenario_empty(), monkeypatch)


def test_all_markdown_blocks_allow_html_except_explainer(monkeypatch):
    db = fakes.scenario_board()
    monkeypatch.setattr(psycopg2, "connect", db.connect)
    st.cache_data.clear()
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    plain = [m for m in at.markdown if not m.proto.allow_html]
    assert len(plain) == 1 and "**資料**" in plain[0].value
