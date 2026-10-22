"""Build the static NBA prediction site from the DB (read-only).

    python -m webui.build_site --out site

Pages are written to ``<out>.tmp``, checked, and only then swapped in, so a
failed build never leaves a half-written site behind.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
from pathlib import Path

import pandas as pd

from jobs.db_utils import db_connect
from jobs.teams import TEAM_NAME_CH, require_abbr
from webui import queries, render
from webui.queries import Run
from webui.render import StaticLinks

SITE_NAME = "NBA 勝負預測"
_GAME_ID = re.compile(r"[0-9A-Za-z_-]+")
_ATTR = re.compile(r'(?:href|src)="([^"]*)"')
_EXTERNAL = ("http:", "https:", "mailto:", "data:", "javascript:")


def safe_game_id(gid: str) -> str:
    """Game ids become file names, so only a conservative character set is allowed."""
    if not isinstance(gid, str) or not _GAME_ID.fullmatch(gid):
        raise ValueError(f"unsafe game_id: {gid!r}")
    return gid


def _checked_abbr(abbr: str) -> str:
    """Canonical abbreviation only; ``require_abbr`` also upper-cases, so compare back."""
    if require_abbr(abbr) != abbr:
        raise ValueError(f"non-canonical team abbreviation: {abbr!r}")
    return abbr


def explainer_html(acc) -> str:
    """The model explainer (markdown bullets in render.explainer_md) as a <ul>."""
    items: list[str] = []
    for line in render.explainer_md(acc).strip().splitlines():
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif items:
            items[-1] += " " + line.strip()
    bold = re.compile(r"[*][*](.+?)[*][*]")
    lis = "".join("<li>" + bold.sub(r"<b>\1</b>", t) + "</li>" for t in items)
    return "<ul>" + lis + "</ul>"


def check_site(root: Path) -> list[str]:
    """Return a list of problems: missing index, absolute or dangling links."""
    problems: list[str] = []
    if not (root / "index.html").exists():
        problems.append("index.html is missing")
    for page in sorted(root.rglob("*.html")):
        text = page.read_text(encoding="utf-8")
        for url in _ATTR.findall(text):
            if not url or url.startswith("#") or url.lower().startswith(_EXTERNAL):
                continue
            if url.startswith("/"):
                if page.name != "404.html":
                    problems.append(f"{page.relative_to(root)}: absolute path {url}")
                continue
            if url.startswith("?"):
                problems.append(f"{page.relative_to(root)}: query-only link {url}")
                continue
            target = url.split("#", 1)[0].split("?", 1)[0]
            if target and not (page.parent / target).resolve().exists():
                problems.append(f"{page.relative_to(root)}: broken link {url}")
    return problems


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _page(title: str, chunks: list[str], now: dt.datetime, data_at) -> str:
    return render.static_page(title, "\n".join(chunks), now, data_at)


def _team_list_html() -> str:
    items = "".join(
        f'<li><a href="team/{a}.html">{render.esc(TEAM_NAME_CH[a])}（{a}）</a></li>'
        for a in sorted(TEAM_NAME_CH))
    return f'<div class="nb-panel"><ul class="nb-teamlist">{items}</ul></div>'


def _index_page(games: pd.DataFrame, tr: dict, acc, season: str, now: dt.datetime,
                data_at) -> str:
    chunks = render.board_chunks(games, tr, acc, now, StaticLinks(0))
    if games.empty:
        chunks.append(f'<div class="nb-honest">{render.EMPTY_BOARD_TEXT}</div>')
    chunks += [
        render.team_section_head(season, with_game_hint=False),
        _team_list_html(),
        '<details class="nb-explainer"><summary>ℹ️ 這個模型怎麼運作？</summary>'
        f'{explainer_html(acc)}</details>',
        render.DISCLAIMER_HTML,
    ]
    return _page(SITE_NAME, chunks, now, data_at)


def _game_page(run: Run, gid: str, tr: dict, acc, now: dt.datetime, data_at) -> str:
    links = StaticLinks(1)
    df = queries.game(run, gid)
    if df is None or df.empty:
        return _page(SITE_NAME, render.not_found_chunks(acc, links), now, data_at)
    g = df.iloc[0]
    home, away = _checked_abbr(g["home_abbr"]), _checked_abbr(g["away_abbr"])
    gdate, season = g["game_date_et"], g["season"]
    chunks = render.detail_chunks(
        g, tr,
        queries.injuries(run, away, gdate), queries.injuries(run, home, gdate),
        queries.player_recent(run, away, season), queries.player_recent(run, home, season),
        acc, links)
    return _page(f"{away} @ {home} · {SITE_NAME}", chunks, now, data_at)


def _team_page(run: Run, abbr: str, season: str, acc, now: dt.datetime, data_at) -> str:
    chunks = [
        render.topbar_html(acc, back=True, links=StaticLinks(1)),
        render.team_section_head(season, with_game_hint=False),
        render.team_tables_html(abbr, queries.team_recent(run, abbr),
                                queries.player_recent(run, abbr, season)),
        render.DISCLAIMER_HTML,
    ]
    return _page(f"{TEAM_NAME_CH[abbr]} · {SITE_NAME}", chunks, now, data_at)


def _not_found_page(now: dt.datetime, data_at) -> str:
    # Pages serves this for any missing path without changing the URL, so a
    # relative link would break on deep URLs: build the home URL in JS instead.
    body = ('<div class="nb-honest">找不到這個頁面。'
            '<a id="home" class="nb-static-link" href="#">回到首頁</a></div>'
            "<script>var h=document.getElementById('home');"
            "h.setAttribute('href','/'+location.pathname.split('/')[1]+'/');</script>")
    return render.static_page(f"找不到頁面 · {SITE_NAME}", body, now, data_at)


def _generate(run: Run, root: Path, now: dt.datetime) -> None:
    games = queries.recent_games(run, 30)
    tr = queries.track_record(run)
    acc = tr.get("wf_acc")
    season = queries.latest_season(run)
    data_at = queries.data_freshness(run)

    game_ids: list[str] = []
    for _, row in games.iterrows():
        game_ids.append(safe_game_id(row["game_id"]))
        _checked_abbr(row["home_abbr"])
        _checked_abbr(row["away_abbr"])

    _write(root / "index.html", _index_page(games, tr, acc, season, now, data_at))
    for gid in game_ids:
        _write(root / "game" / f"{gid}.html", _game_page(run, gid, tr, acc, now, data_at))
    for abbr in sorted(TEAM_NAME_CH):
        _write(root / "team" / f"{abbr}.html", _team_page(run, abbr, season, acc, now, data_at))
    _write(root / "404.html", _not_found_page(now, data_at))
    _write(root / ".nojekyll", "")


def build(run: Run, out_dir: Path, now: dt.datetime) -> None:
    """Generate the site into ``out_dir``; the old site survives any failure."""
    out_dir = Path(out_dir)
    tmp = out_dir.with_name(out_dir.name + ".tmp")
    old = out_dir.with_name(out_dir.name + ".old")
    shutil.rmtree(tmp, ignore_errors=True)
    if old.exists():
        if out_dir.exists():
            shutil.rmtree(old, ignore_errors=True)
        else:  # an interrupted swap left the only good copy here
            old.rename(out_dir)
    try:
        _generate(run, tmp, now)
        problems = check_site(tmp)
        if problems:
            raise RuntimeError("site check failed: " + "; ".join(problems[:5]))
        if out_dir.exists():
            out_dir.rename(old)
        try:
            tmp.rename(out_dir)
        except Exception:
            if old.exists() and not out_dir.exists():
                old.rename(out_dir)
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def connect_readonly():
    """One read-only connection with connect and statement timeouts."""
    os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
    conn = db_connect()
    conn.set_session(readonly=True)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 30000")
    return conn


def make_run(conn) -> Run:
    def run(sql: str, params: tuple = ()) -> pd.DataFrame:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static NBA prediction site.")
    parser.add_argument("--out", default="site", help="output directory (default: site)")
    args = parser.parse_args(argv)
    try:
        conn = connect_readonly()
    except Exception as exc:  # message may contain a DSN: print the class only
        print(f"DB connection failed ({type(exc).__name__}); check the DB credentials.")
        return 1
    try:
        build(make_run(conn), Path(args.out), dt.datetime.now(dt.timezone.utc))
    except Exception as exc:
        print(f"Site build failed: {type(exc).__name__}: {exc}")
        return 1
    finally:
        conn.close()
    print(f"Site written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
