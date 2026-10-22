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


# ---------- Task 4: HTML escape of DB-sourced text ----------

def _inj_df(name: str, detail: str = "Knee") -> pd.DataFrame:
    return pd.DataFrame([(name, "Out", detail, dt.date(2026, 10, 23))],
                        columns=["player_name", "status", "detail", "snap_date"])


def test_esc_special_chars():
    assert render.esc("D'Angelo") == "D&#x27;Angelo"
    assert "<" not in render.esc("<script>")
    assert render.esc(None) == ""


def test_injury_panel_escapes():
    html = render.injury_panel(_inj_df("<script>alert(1)</script>", "Knee <b>"), "LAL", "主")
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html


def test_table_html_escapes_cells():
    df = pd.DataFrame([("A&B", 1), ("C<D", 2)], columns=["球員", "場次"])
    html = render.table_html(df)
    assert "A&amp;B" in html and "C&lt;D" in html
    assert "球員" in html


def test_table_html_keeps_none_and_numbers():
    df = pd.DataFrame({"球員": ["x"], "勝負": [None], "得分": [12.5]})
    plain = df.to_html(index=False, classes="ntable", border=0, escape=False)
    assert render.table_html(df) == plain


def test_card_escapes_game_id_in_href():
    html = render.game_card_html(_game(game_id='x"><script>'))
    assert "<script>" not in html


def test_no_double_escape():
    assert render.fmt_team("A&B") == "A&amp;B"
    html = render.injury_panel(_inj_df("A&B"), "A&B", "主")
    assert html.count("A&amp;B") >= 2 and "&amp;amp;" not in html
    tbl = render.table_html(pd.DataFrame([("A&B",)], columns=["球員"]))
    assert tbl.count("A&amp;B") == 1 and "&amp;amp;" not in tbl


def test_line_source_and_season_escaped():
    g = _game(line_source="<i>x</i>", home_spread=-3.5, p_home_cover=0.6)
    assert "<i>x</i>" not in render._bet_spread(g)
    detail = render.detail_chunks(
        _game(season="<b>s</b>"), {}, _inj_df("a"), _inj_df("b"),
        pd.DataFrame(), pd.DataFrame(), 0.6)
    assert not any("<b>s</b>" in c for c in detail)


# ---------- Task 5: static page shell ----------

def test_static_links_depth0_and_1():
    top, deep = render.StaticLinks(0), render.StaticLinks(1)
    assert top.home() == "index.html"
    assert top.game("G1") == "game/G1.html" and top.team("LAL") == "team/LAL.html"
    assert deep.home() == "../index.html"
    assert deep.game("G1") == "../game/G1.html" and deep.team("LAL") == "../team/LAL.html"


def test_static_links_no_leading_slash():
    for depth in (0, 1):
        links = render.StaticLinks(depth)
        for url in (links.home(), links.game("x"), links.team("LAL")):
            assert not url.startswith("/")


def test_static_page_has_built_marker():
    page = render.static_page("T", "<p>x</p>", NOW)
    assert 'data-built="2026-10-23T01:00:00+00:00"' in page
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert '<div class="stApp"><div class="block-container">' in page


def test_static_page_shows_taipei_time():
    page = render.static_page("T", "", NOW)
    assert "資料更新於 2026-10-23 09:00（台北時間）" in page


def test_stale_threshold_is_36h():
    assert render.STALE_HOURS == 36
    assert "36*3600*1000" in render.static_page("T", "", NOW)
    assert "資料可能已過期" in render.static_page("T", "", NOW)


def test_static_page_rejects_naive_datetime():
    import pytest
    with pytest.raises(ValueError):
        render.static_page("T", "", dt.datetime(2026, 10, 23, 1, 0))


def test_static_page_escapes_title():
    assert "<script>" not in render.static_page("<script>", "", NOW).split("</title>")[0]
