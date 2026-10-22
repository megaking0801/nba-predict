"""Unit tests for webui.render (pure HTML rendering)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from webui import render
from tests.fakes import BOARD_VIEW_COLS, UTC

ROOT = Path(__file__).resolve().parents[1]


def _game(**kw) -> pd.Series:
    base = {c: None for c in BOARD_VIEW_COLS}
    base.update(game_id="G1", season="2025-26", game_date_et=dt.date(2026, 10, 24),
                tipoff_utc=dt.datetime(2026, 10, 24, 23, 30, tzinfo=UTC), status="scheduled",
                home_abbr="LAL", away_abbr="GSW", p_home_win=0.66, pred_margin=4.0)
    base.update(kw)
    return pd.Series(base)


class _Links:
    def home(self): return "../index.html"
    def game(self, game_id): return f"../game/{game_id}.html"
    def team(self, abbr): return f"../team/{abbr}.html"


def test_game_card_uses_links():
    html = render.game_card_html(_game(), links=_Links())
    assert 'href="../game/G1.html"' in html


def test_game_card_default_links_are_streamlit_query():
    assert 'href="?game=G1"' in render.game_card_html(_game())


def test_card_upcoming_and_final():
    up = render.game_card_html(_game())
    assert "尚未開賽" in up and "命中" not in up
    fin = render.game_card_html(_game(status="final", win_result=1, home_score=100,
                                      away_score=90))
    assert "終場" in fin and "✅ 命中" in fin


def test_card_missing_tipoff():
    for missing in (None, pd.NaT):
        html = render.game_card_html(_game(tipoff_utc=missing))
        assert "2026-10-24" in html


def test_bet_total_variants():
    none = render._bet_total(_game())
    assert "🔧 本季開賽後顯示" in none
    line_only = render._bet_total(_game(total_line=220.5))
    assert "本季開賽後顯示模型估總分" in line_only
    pred_only = render._bet_total(_game(pred_total=225.0))
    assert "無總分盤口可比較" in pred_only
    push = render._bet_total(_game(pred_total=220.7, total_line=220.5))
    assert "貼盤" in push


def test_bet_spread_missing_line():
    assert "這場沒有盤口資料" in render._bet_spread(_game())


def test_injury_panel_empty_df():
    cols = ["player_name", "status", "detail", "snap_date"]
    html = render.injury_panel(pd.DataFrame([], columns=cols), "LAL", "主")
    assert "（無傷病回報）" in html


def _tr(**kw) -> dict:
    base = {"wf_acc": 0.65, "wf_n": 100, "rec_hit": 3, "rec_n": 5, "over_wf_acc": None,
            "over_wf_n": None, "over_rec_hit": 0, "over_rec_n": 0}
    base.update(kw)
    return base


NOW = dt.datetime(2026, 10, 23, 1, 0, tzinfo=UTC)


def test_board_chunks_empty_games_returns_two_chunks():
    cols = ["game_id", "game_date_et", "tipoff_utc", "status", "home_abbr", "away_abbr",
            "home_score", "away_score", "pred_margin", "p_home_win", "win_result"]
    chunks = render.board_chunks(pd.DataFrame([], columns=cols), _tr(), 0.65, NOW)
    assert len(chunks) == 2


def test_board_chunks_only_final_has_no_featured():
    games = pd.DataFrame([_game(status="final", win_result=1, home_score=100,
                                away_score=90)])
    chunks = render.board_chunks(games, _tr(), 0.65, NOW)
    assert not any("今晚最有把握的一場" in c for c in chunks)
    assert any("休賽季" in c for c in chunks)


def test_no_streamlit_import():
    for name in ("render.py", "queries.py"):
        path = ROOT / "webui" / name
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        assert "import streamlit" not in src and "from streamlit" not in src
