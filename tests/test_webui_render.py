"""Unit tests for webui.render (pure HTML rendering)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

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
    assert "終場" in fin and '<span class="nb-hit">命中</span>' in fin


def test_card_missing_tipoff():
    for missing in (None, pd.NaT):
        html = render.game_card_html(_game(tipoff_utc=missing))
        assert "2026-10-24" in html


def test_bet_total_variants():
    none = render._bet_total(_game())
    assert "本季開賽後顯示——模型估總分" in none
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
    assert "沒有傷病回報" in html


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
    assert "頁面建置於 2026-10-23 09:00（台北時間）" in page
    assert "資料更新於" not in page  # no data timestamp known: do not claim one


def test_static_page_uses_data_time_for_staleness():
    data_at = dt.datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    page = render.static_page("T", "", NOW, data_at=data_at)
    assert 'data-built="2026-10-20T12:00:00+00:00"' in page
    assert "資料更新於 2026-10-20 20:00（台北時間）" in page
    assert "頁面建置於 2026-10-23 09:00（台北時間）" in page


def test_static_page_trims_microseconds():
    page = render.static_page("T", "", NOW.replace(microsecond=613402))
    assert 'data-built="2026-10-23T01:00:00+00:00"' in page


def test_static_page_rejects_naive_data_time():
    import pytest
    with pytest.raises(ValueError):
        render.static_page("T", "", NOW, data_at=dt.datetime(2026, 10, 20, 12, 0))


def test_record_counts_from_json_are_escaped():
    tr = _tr(wf_n="<b>1</b>", over_wf_acc=0.5, over_wf_n="<i>2</i>")
    html = "".join(render.record_chunks(tr, 0.65))
    assert "<b>1</b>" not in html and "<i>2</i>" not in html


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


# ---------- review follow-ups ----------

def test_hero_uses_taipei_date():
    utc = dt.datetime(2026, 10, 22, 17, 0, tzinfo=UTC)  # 01:00 next day in Taipei
    html = render.hero_html(2, utc)
    assert '<div class="nb-day">23</div>' in html and "10 月 週五，台北時間" in html
    assert "今晚 2 場" in html


@pytest.mark.parametrize("utc,day,sub", [
    (dt.datetime(2026, 9, 4, 16, 30, tzinfo=UTC), "5", "9 月 週六"),       # 00:30 Taipei on 9/5
    (dt.datetime(2026, 9, 4, 15, 59, tzinfo=UTC), "4", "9 月 週五"),       # still 9/4 in Taipei
    (dt.datetime(2026, 12, 31, 15, 0, tzinfo=UTC), "31", "12 月 週四"),    # 23:00 Taipei on 12/31
    (dt.datetime(2026, 12, 31, 16, 0, tzinfo=UTC), "1", "1 月 週五"),      # 00:00 Taipei, new year
])
def test_big_date_is_the_taipei_date_across_month_and_year(utc, day, sub):
    html = render.hero_html(3, utc)
    assert f'<div class="nb-day">{day}</div>' in html and f"{sub}，台北時間" in html


def test_headline_says_when_the_next_games_are_if_not_today():
    now = dt.datetime(2026, 10, 30, 3, 0, tzinfo=UTC)          # 10/29 23:00 in New York
    assert "今晚 5 場" in render.hero_html(5, now, dt.date(2026, 10, 29))
    later = render.hero_html(5, now, dt.date(2026, 10, 31))
    assert "今天沒有比賽。下一場是 10/31 週六，5 場。" in later and "今晚" not in later


def test_hero_rejects_naive_now():
    import pytest
    with pytest.raises(ValueError):
        render.hero_html(1, dt.datetime(2026, 10, 23, 1, 0))


def test_team_tables_html_populated():
    from tests.fakes import TEAM_RECENT_COLS, PLAYER_COLS
    team_df = pd.DataFrame([(dt.date(2026, 10, 22), "LAL", "NYK", 101, 99, "W", 101, 44, 25, 2)],
                           columns=TEAM_RECENT_COLS)
    player_df = pd.DataFrame([("Gamma Three", 5, 30.5, 24.0, 5.5, 6.0, 1.5)], columns=PLAYER_COLS)
    html = render.team_tables_html("LAL", team_df, player_df)
    for cell in ("2026-10-22", "NYK", ">101<", ">W<", "Gamma Three", "30.5"):
        assert cell in html
    assert "湖人 近 10 場" in html


def test_static_page_centers_and_pads_container():
    css = render.static_page("T", "", NOW).split("</head>")[0]
    assert "margin: 0 auto" in css and "padding-left: 16px" in css


def test_static_page_keeps_streamlit_element_gap():
    css = render.static_page("T", "", NOW).split("</head>")[0]
    assert ".block-container > * + *" in css


# ---------- P1: "today's picks are not out yet" notice ----------

def test_should_show_not_yet_boundaries():
    now = dt.datetime(2026, 10, 23, 13, 0, tzinfo=UTC)
    h = dt.timedelta
    f = render.should_show_not_yet
    assert f(now, now - h(hours=20, minutes=1), now + h(hours=10))
    assert not f(now, now - h(hours=20), now + h(hours=10))            # exactly 20 h
    assert not f(now, now - h(hours=21), now + h(hours=25))            # next game too far
    assert not f(now, now - h(hours=21), now + h(hours=24))            # exactly 24 h
    assert not f(now, now - h(hours=21), now - h(minutes=1))           # tipoff already passed
    assert not f(now, now - h(hours=21), None)
    assert not f(now, None, now + h(hours=10))


def test_static_page_next_tip_marker():
    data_at = dt.datetime(2026, 10, 22, 10, 30, tzinfo=UTC)
    tip = dt.datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    page = render.static_page("T", "", NOW, data_at=data_at, next_tip=tip)
    assert 'data-next-tip="2026-10-24T23:00:00+00:00"' in page
    assert 'id="notyet"' in page and "10/22 的預測" in page
    assert "20*3600*1000" in page and "24*3600*1000" in page
    plain = render.static_page("T", "", NOW, data_at=data_at)
    assert 'data-next-tip="' not in plain
    import pytest
    with pytest.raises(ValueError):
        render.static_page("T", "", NOW, data_at=data_at, next_tip=dt.datetime(2026, 10, 24, 23, 0))


def test_static_page_without_data_time_has_no_notyet_div():
    tip = dt.datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    assert 'id="notyet"' not in render.static_page("T", "", NOW, next_tip=tip)


# ---------- P6.1: the spread card no longer shows a cover probability or a pick ----------

def _spread_game(**kw):
    return _game(home_spread=-4.5, p_home_cover=0.58, pred_margin=6.0, line_source="espn", **kw)


def test_spread_card_has_no_cover_probability_or_pick():
    html = render._bet_spread(_spread_game())
    for banned in ("看好", "破受讓盤", "nb-track", "nb-pct", "58%", "42%"):
        assert banned not in html, banned


def test_spread_card_shows_the_line_and_the_model_gap():
    html = render._bet_spread(_spread_game())
    assert "盤口" in html and "-4.5" in html or "+4.5" in html
    assert "模型估計分差" in html and "+6.0" in html
    assert "比盤口多給主隊 1.5 分" in html            # 6.0 - 4.5


def test_spread_card_gap_direction_and_tie():
    away = render._bet_spread(_game(home_spread=-4.5, pred_margin=2.0, line_source="espn"))
    assert "比盤口多給客隊 2.5 分" in away
    tie = render._bet_spread(_game(home_spread=-4.5, pred_margin=4.5, line_source="espn"))
    assert "與盤口幾乎一致" in tie


def test_spread_card_states_the_validation_result_instead_of_a_pick():
    html = render._bet_spread(_spread_game())
    assert "不提供過盤機率" in html and "0.36" in html


def test_spread_card_without_a_prediction_shows_the_line_only():
    html = render._bet_spread(_game(home_spread=-4.5, line_source="espn", pred_margin=None))
    assert "-4.5" in html or "+4.5" in html
    assert "模型估計分差" not in html


def test_spread_card_result_has_no_hit_or_miss_badge():
    covered = render._bet_spread(_spread_game(status="final", cover_result=1))
    missed = render._bet_spread(_spread_game(status="final", cover_result=0))
    push = render._bet_spread(_spread_game(status="final", cover_result=2))
    for html in (covered, missed, push):
        assert "命中" not in html and "未中" not in html and "nb-hit" not in html and "nb-miss" not in html
    assert "LAL" in covered and "破盤" in covered
    assert "GSW" in missed and "破盤" in missed
    assert "push" in push


# ---------- site simplification step 2: rows, folded dates, no featured card ----------

def test_row_has_two_lines_and_puts_the_time_or_the_score_on_the_centre_line():
    up = render.game_card_html(_game(p_home_win=0.66))
    assert up.count('class="nb-rline"') == 1 and up.count('class="nb-prob"') == 1
    assert '<span class="nb-rmid"><span class="nb-when">' in up
    fin = render.game_card_html(_game(p_home_win=0.66, status="final", win_result=1, home_score=110, away_score=105))
    assert ('<span class="nb-state">終場</span><span class="nb-score">105 : 110</span>'
            '<span class="nb-hit">命中</span>') in fin


def test_shown_days_share_one_board_so_the_centre_line_is_unbroken():
    html = "".join(render.board_chunks(_board_games(), {"rec_n": 0}, None, NOW))
    head = html.split('<details class="nb-more">')[0]
    assert head.count('<div class="nb-board">') == 1 and head.count('<section class="nb-group">') == 2


def _board_games():
    rows = []
    for d, status, n in ((dt.date(2026, 10, 20), "final", 2), (dt.date(2026, 10, 21), "final", 3),
                         (dt.date(2026, 10, 22), "scheduled", 4), (dt.date(2026, 10, 23), "scheduled", 2)):
        for i in range(n):
            rows.append(_game(game_id=f"{d:%m%d}{i}", game_date_et=d, status=status,
                              win_result=1 if status == "final" else None, home_score=100, away_score=90))
    return pd.DataFrame(rows)


def test_board_shows_tonight_and_the_last_finished_day_and_folds_the_rest():
    html = "".join(render.board_chunks(_board_games(), {"rec_n": 0}, None, NOW))
    head, more = html.split('<details class="nb-more">')
    assert "10/22 週四" in head and "10/21 週三" in head
    assert "10/20 週二" not in head and "10/23 週五" not in head          # (the headline shows today's date)
    assert "其他日期（4 場）" in more and "10/20" in more and "10/23" in more
    assert "今晚 4 場" in head


def test_board_without_upcoming_shows_the_two_latest_days():
    g = _board_games()
    g = g[g["status"] == "final"]
    html = "".join(render.board_chunks(g, {"rec_n": 0}, None, NOW))
    head = html.split('<details class="nb-more">')[0]
    assert "10/21" in head and "10/20" in head and "休賽季" in head


def test_board_has_no_featured_card_or_confidence_word():
    html = "".join(render.board_chunks(_board_games(), {"rec_n": 0}, None, NOW))
    for gone in ("nb-featured", "最有把握", "信心", "🔮", "🗓", "🔍", "→"):
        assert gone not in html, gone


def test_game_row_marks_the_favourite_without_a_pick_sentence():
    html = render.game_card_html(_game(p_home_win=0.66))
    assert "34%" in html and "66%" in html and "看好" not in html
    assert '<span class="nb-pct fav">66%</span>' in html


# ---------- site simplification step 3: stale or pick-like copy must not come back ----------

BANNED_COPY = ("三季", "4000", "近三季", "最有把握", "信心", "看好", "擅長", "穩賺明牌！")


def test_banned_copy_is_not_in_the_web_sources():
    for rel in ("webui/render.py", "webui/build_site.py", "auto_nba_predict.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for word in BANNED_COPY[:-1]:
            assert word not in src, (rel, word)


def test_banned_copy_is_not_in_rendered_pages(tmp_path):
    from tests import fakes
    from webui import build_site
    build_site.build(fakes.scenario_board().run, tmp_path / "site", NOW)
    for page in (tmp_path / "site").rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        for word in BANNED_COPY[:-1]:
            assert word not in html, (page.name, word)


def test_honesty_copy_is_still_there():
    assert "僅供球迷參考與娛樂，非投注建議" in render.DISCLAIMER_HTML
    assert "高命中率不等於能獲利" in render.DISCLAIMER_HTML
    assert "不是穩賺明牌" in "".join(render.record_chunks({"rec_n": 0}, 0.65))


# ---------- site v2: centre-line probability bar ----------

def test_centre_line_fill_grows_toward_the_favourite_by_the_distance_from_fifty():
    home = render.prob_bar(_game(p_home_win=0.66))
    assert '<i class="fill h" style="width:16%"></i>' in home
    away = render.prob_bar(_game(p_home_win=0.20))
    assert '<i class="fill a" style="width:30%"></i>' in away
    assert '<span class="nb-pct fav">80%</span>' in away


def test_coin_flip_has_no_fill_but_still_shows_both_numbers():
    html = render.prob_bar(_game(p_home_win=0.50))
    assert "fill" not in html and html.count("50%") == 2 and "fav" not in html
    near = render.prob_bar(_game(p_home_win=0.51))
    assert 'style="width:1%"' in near                     # tiny fill; the centre ring is drawn by CSS on top


def test_bar_is_hidden_from_screen_readers_and_the_row_has_a_text_label():
    row = render.game_card_html(_game(p_home_win=0.66))
    assert 'class="nb-mid" aria-hidden="true"' in row
    assert 'aria-label="勇士（客）34%，湖人（主）66%，' in row
    fin = render.game_card_html(_game(p_home_win=0.66, status="final", win_result=0, home_score=99, away_score=101))
    assert "101 比 99，勝率較高的一方輸了" in fin


def test_detail_head_carries_the_bar_and_the_moneyline_card_keeps_its_sentence():
    chunks = render.detail_chunks(_game(p_home_win=0.66), {}, _inj_df("a"), _inj_df("b"),
                                  pd.DataFrame(), pd.DataFrame(), 0.6)
    head = next(c for c in chunks if "nb-detailhead" in c)
    bets = next(c for c in chunks if "nb-bets" in c)
    assert "nb-mid" in head and "nb-mid" not in bets and "勝率較高：" in bets
