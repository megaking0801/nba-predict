"""Tests for webui.build_site (static site generation from a fake DB)."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from jobs.teams import TEAM_NAME_CH
from tests import fakes
from webui import build_site

NOW = dt.datetime(2026, 10, 23, 1, 0, tzinfo=dt.timezone.utc)
RECENT_KEY = "FROM public.v_app_board WHERE p_home_win IS NOT NULL"


def _build(db: fakes.FakeDb, out: Path) -> Path:
    build_site.build(db.run, out, NOW)
    return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_build_creates_all_pages(tmp_path):
    out = _build(fakes.scenario_board(), tmp_path / "site")
    assert (out / "index.html").exists()
    assert (out / "404.html").exists() and (out / ".nojekyll").exists()
    games = sorted(p.stem for p in (out / "game").glob("*.html"))
    assert games == sorted([fakes.GAME_UPCOMING, fakes.GAME_FINAL, fakes.GAME_NOLINE])
    teams = sorted(p.stem for p in (out / "team").glob("*.html"))
    assert teams == sorted(TEAM_NAME_CH) and len(teams) == 30


def test_build_links_all_resolve(tmp_path):
    out = _build(fakes.scenario_board(), tmp_path / "site")
    assert build_site.check_site(out) == []


def test_no_absolute_paths(tmp_path):
    out = _build(fakes.scenario_board(), tmp_path / "site")
    for page in out.rglob("*.html"):
        if page.name == "404.html":
            continue
        assert not re.search(r'(?:href|src)="/', _read(page)), page


def test_build_empty_db(tmp_path):
    out = _build(fakes.scenario_empty(), tmp_path / "site")
    assert "目前資料庫尚無預測" in _read(out / "index.html")
    assert len(list((out / "team").glob("*.html"))) == 30
    assert build_site.check_site(out) == []


def test_team_without_data_shows_empty(tmp_path):
    out = _build(fakes.scenario_board(), tmp_path / "site")
    assert "尚無資料" in _read(out / "team" / "ATL.html")


def test_team_page_links_back_home(tmp_path):
    out = _build(fakes.scenario_board(), tmp_path / "site")
    assert 'href="../index.html"' in _read(out / "team" / "LAL.html")


def test_game_page_escapes_player_name(tmp_path):
    db = fakes.scenario_board()
    key = "FROM public.injury_snapshots"
    import pandas as pd
    db.routes[key] = lambda p: pd.DataFrame(
        [("<script>alert(1)</script>", "Out", "Knee", dt.date(2026, 10, 23))],
        columns=fakes.INJURY_COLS)
    out = _build(db, tmp_path / "site")
    page = _read(out / "game" / f"{fakes.GAME_UPCOMING}.html")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_staleness_marker_in_every_page(tmp_path):
    out = _build(fakes.scenario_board(), tmp_path / "site")
    for page in out.rglob("*.html"):
        assert 'data-built="2026-10-22T20:15:00+00:00"' in _read(page), page


def _with_recent(db: fakes.FakeDb, **changes) -> fakes.FakeDb:
    df = db.routes[RECENT_KEY].copy()
    for col, val in changes.items():
        df.loc[0, col] = val
    db.routes[RECENT_KEY] = df
    return db


def test_unsafe_game_id_fails_and_keeps_old_site(tmp_path):
    out = tmp_path / "site"
    out.mkdir()
    (out / "marker.txt").write_text("old", encoding="utf-8")
    db = _with_recent(fakes.scenario_board(), game_id="../x")
    with pytest.raises(ValueError):
        build_site.build(db.run, out, NOW)
    assert (out / "marker.txt").exists()
    assert not (tmp_path / "site.tmp").exists() and not (tmp_path / "site.old").exists()


def test_rebuild_replaces_existing_site(tmp_path):
    out = tmp_path / "site"
    _build(fakes.scenario_board(), out)
    (out / "game" / "stale_leftover.html").write_text("old", encoding="utf-8")
    _build(fakes.scenario_board(), out)
    assert not (out / "game" / "stale_leftover.html").exists()
    assert (out / "index.html").exists()
    assert not (tmp_path / "site.tmp").exists() and not (tmp_path / "site.old").exists()


@pytest.mark.parametrize("abbr", ["ZZZ", "bos"])
def test_unknown_abbr_fails(tmp_path, abbr):
    db = _with_recent(fakes.scenario_board(), home_abbr=abbr)
    with pytest.raises(ValueError, match=abbr):
        build_site.build(db.run, tmp_path / "site", NOW)


def test_check_site_detects_broken_link(tmp_path):
    (tmp_path / "index.html").write_text('<a href="missing.html">x</a>', encoding="utf-8")
    problems = build_site.check_site(tmp_path)
    assert any("missing.html" in p for p in problems)


def test_check_site_flags_absolute_and_accepts_query_fragment(tmp_path):
    (tmp_path / "other.html").write_text("ok", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<img src="/x.png"><a href="other.html?a=1#b">ok</a>'
        '<a href="https://example.com/">ext</a><a href="#top">top</a>', encoding="utf-8")
    problems = build_site.check_site(tmp_path)
    assert len(problems) == 1 and "/x.png" in problems[0]


def test_check_site_requires_index(tmp_path):
    assert build_site.check_site(tmp_path)


def test_404_has_no_relative_links_and_uses_js_home(tmp_path):
    out = _build(fakes.scenario_board(), tmp_path / "site")
    page = _read(out / "404.html")
    assert not re.search(r'href="(?!#)[^"]', page.replace('id="home"', ""))
    assert "location.pathname" in page


def test_explainer_html_has_no_raw_bold_and_keeps_continuation():
    html = build_site.explainer_html(0.655)
    assert "**" not in html
    assert "再換算成勝率" in html
    assert html.startswith("<ul>") and html.rstrip().endswith("</ul>")
    assert "<b>資料</b>" in html


def test_safe_game_id():
    assert build_site.safe_game_id("0022600001") == "0022600001"
    for bad in ("", "../x", "a/b", "a b", "a.html"):
        with pytest.raises(ValueError):
            build_site.safe_game_id(bad)


# ---------- Task 7: connection hardening and CLI ----------

def test_connect_sets_readonly_and_timeout(monkeypatch):
    db = fakes.scenario_board()
    monkeypatch.setattr(build_site, "db_connect", db.connect)
    conn = build_site.connect_readonly()
    assert db.session == {"readonly": True}
    assert any("statement_timeout" in s for s in db.settings)
    assert conn is not None
    import os
    assert os.environ.get("PGCONNECT_TIMEOUT")


def test_main_builds_into_out_dir(monkeypatch, tmp_path):
    db = fakes.scenario_board()
    monkeypatch.setattr(build_site, "db_connect", db.connect)
    out = tmp_path / "site"
    assert build_site.main(["--out", str(out)]) == 0
    assert (out / "index.html").exists()
    assert db.closed


def test_main_returns_1_on_db_error_without_leaking_dsn(monkeypatch, capsys, tmp_path):
    def boom():
        raise RuntimeError("postgresql://u:secret@h/db")
    monkeypatch.setattr(build_site, "db_connect", boom)
    assert build_site.main(["--out", str(tmp_path / "site")]) == 1
    captured = capsys.readouterr()
    assert "secret" not in captured.out + captured.err
    assert "RuntimeError" in captured.out + captured.err


def test_main_prints_build_error_message(monkeypatch, capsys, tmp_path):
    db = _with_recent(fakes.scenario_board(), home_abbr="ZZZ")
    monkeypatch.setattr(build_site, "db_connect", db.connect)
    assert build_site.main(["--out", str(tmp_path / "site")]) == 1
    assert "ZZZ" in capsys.readouterr().out + ""


# ---------- final review follow-ups ----------

def test_build_uses_data_freshness_for_staleness(tmp_path):
    out = _build(fakes.scenario_board(), tmp_path / "site")
    page = _read(out / "index.html")
    assert 'data-built="2026-10-22T20:15:00+00:00"' in page
    assert "資料更新於 2026-10-23 04:15（台北時間）" in page


def test_build_without_prediction_time_falls_back_to_build_time(tmp_path):
    out = _build(fakes.scenario_empty(), tmp_path / "site")
    page = _read(out / "index.html")
    assert 'data-built="2026-10-23T01:00:00+00:00"' in page
    assert "資料更新於" not in page


def test_data_freshness_query_is_tz_aware_or_none():
    got = build_site.queries.data_freshness(fakes.scenario_board().run)
    assert got is not None and got.tzinfo is not None
    assert build_site.queries.data_freshness(fakes.scenario_empty().run) is None


def test_check_site_flags_query_only_links(tmp_path):
    (tmp_path / "index.html").write_text('<a href="?game=1">x</a><a href="?">y</a>',
                                         encoding="utf-8")
    problems = build_site.check_site(tmp_path)
    assert len(problems) == 2


def test_failed_build_restores_orphaned_old_site(tmp_path):
    old = tmp_path / "site.old"
    old.mkdir()
    (old / "marker.txt").write_text("keep", encoding="utf-8")
    db = _with_recent(fakes.scenario_board(), game_id="../x")
    with pytest.raises(ValueError):
        build_site.build(db.run, tmp_path / "site", NOW)
    assert (tmp_path / "site" / "marker.txt").exists()


def test_not_yet_marker_in_every_page(tmp_path):
    out = _build(fakes.scenario_board(), tmp_path / "site")
    for page in out.rglob("*.html"):
        assert 'data-next-tip="' in _read(page), page
    empty = _build(fakes.scenario_empty(), tmp_path / "empty")
    for page in empty.rglob("*.html"):
        assert 'data-next-tip="' not in _read(page), page
