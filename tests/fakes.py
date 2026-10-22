"""Fake DB for characterization and build tests (no real connection needed).

Fixtures are stored as plain Python values ``(cols, rows)`` and turned into a
DataFrame exactly like ``auto_nba_predict.q`` does, so dtype inference (e.g.
float scores when a row has None) matches production.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Callable

import pandas as pd

from jobs.model import MODEL_NAME

UTC = dt.timezone.utc

GAME_UPCOMING = "0022600001"
GAME_FINAL = "0022600002"
GAME_NOLINE = "0022600003"
GAME_MISSING = "9999999999"

# Column order of public.v_app_board (jobs/schema.py v_app_board).
BOARD_VIEW_COLS = [
    "game_id", "season", "season_type", "game_date_et", "tipoff_utc", "status",
    "home_abbr", "away_abbr", "home_score", "away_score", "margin",
    "home_spread", "home_price", "away_price", "line_source", "book",
    "line_captured_at",
    "model_name", "model_version", "predicted_at", "home_spread_used",
    "pred_margin", "p_home_cover", "p_home_win", "edge_prob", "ev_home", "pick_side",
    "abstain_reason", "is_paper", "cover_result", "win_result",
    "total_line", "over_price", "under_price",
    "pred_total", "total_line_used", "p_over", "over_result",
]

RECENT_COLS = ["game_id", "game_date_et", "tipoff_utc", "status", "home_abbr", "away_abbr",
               "home_score", "away_score", "pred_margin", "p_home_win", "win_result"]
TEAM_RECENT_COLS = ["日期", "主", "客", "主分", "客分", "勝負", "得分", "籃板", "助攻", "正負值"]
PLAYER_COLS = ["球員", "場次", "分鐘", "得分", "籃板", "助攻", "正負值"]
INJURY_COLS = ["player_name", "status", "detail", "snap_date"]


def _board_row(**kw) -> dict:
    row = {c: None for c in BOARD_VIEW_COLS}
    row.update(season="2025-26", season_type="Regular Season", model_name=MODEL_NAME,
               model_version="v1", is_paper=False)
    row.update(kw)
    return row


def _board_rows() -> list[dict]:
    return [
        _board_row(
            game_id=GAME_UPCOMING, game_date_et=dt.date(2026, 10, 24),
            tipoff_utc=dt.datetime(2026, 10, 24, 23, 30, tzinfo=UTC), status="scheduled",
            home_abbr="LAL", away_abbr="GSW", home_spread=Decimal("-3.5"),
            line_source="espn", total_line=Decimal("228.5"), pred_margin=4.1,
            p_home_cover=0.56, p_home_win=0.66, pred_total=231.2, p_over=0.55),
        _board_row(
            game_id=GAME_FINAL, game_date_et=dt.date(2026, 10, 23),
            tipoff_utc=dt.datetime(2026, 10, 23, 23, 0, tzinfo=UTC), status="final",
            home_abbr="BOS", away_abbr="NYK", home_score=110, away_score=105, margin=5,
            home_spread=Decimal("-6.5"), line_source="espn", total_line=Decimal("215.5"),
            pred_margin=7.2, p_home_cover=0.52, p_home_win=0.74, cover_result=0,
            win_result=1, pred_total=211.0, p_over=0.4, over_result=0),
        _board_row(
            game_id=GAME_NOLINE, game_date_et=dt.date(2026, 10, 22),
            tipoff_utc=dt.datetime(2026, 10, 22, 23, 0, tzinfo=UTC), status="final",
            home_abbr="DEN", away_abbr="PHX", home_score=None, away_score=None,
            pred_margin=-1.0, p_home_win=0.46, win_result=None),
    ]


def _recent_from(rows: list[dict]) -> tuple[list[str], list[tuple]]:
    return RECENT_COLS, [tuple(r[c] for c in RECENT_COLS) for r in rows]


def _frame(cols: list[str], rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=cols)


class FakeDb:
    """Routes normalized SQL to fixtures by substring; exactly one key must hit."""

    def __init__(self, routes: dict[str, pd.DataFrame | Callable[[tuple], pd.DataFrame]]):
        self.routes = routes
        self.calls: list[tuple[str, tuple]] = []
        self.settings: list[str] = []
        self.session: dict = {}
        self.closed = False

    def run(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        norm = " ".join(sql.split())
        self.calls.append((norm, tuple(params)))
        hits = [k for k in self.routes if k in norm]
        if len(hits) != 1:
            raise AssertionError(f"{len(hits)} routes matched SQL: {norm}")
        val = self.routes[hits[0]]
        return val(tuple(params)) if callable(val) else val.copy()

    def connect(self, *a, **kw):
        return _FakeConn(self)


class _FakeCursor:
    def __init__(self, db: FakeDb):
        self._db = db
        self._df: pd.DataFrame | None = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if " ".join(sql.split()).upper().startswith("SET "):
            self._db.settings.append(" ".join(sql.split()))
            return
        self._df = self._db.run(sql, tuple(params or ()))
        self.description = [(c,) for c in self._df.columns]

    def fetchall(self):
        return [tuple(r) for r in self._df.itertuples(index=False, name=None)]


class _FakeConn:
    def __init__(self, db: FakeDb):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._db)

    def set_session(self, **kw):
        self._db.session.update(kw)

    def close(self):
        self._db.closed = True


def _metrics_for(params: tuple) -> pd.DataFrame:
    name = params[0]
    if name == "total_model":
        m = {"report_over": {"accuracy": 0.482, "n": 900}}
    elif name == MODEL_NAME:
        m = {"report_betting": {"straight_up": {"winner_accuracy": 0.655, "n": 3000}}}
    else:
        raise AssertionError(f"unexpected model_name {name!r}")
    return pd.DataFrame([(m,)], columns=["metrics"])


def _game_by_id(rows: list[dict]) -> Callable[[tuple], pd.DataFrame]:
    def fn(params: tuple) -> pd.DataFrame:
        sel = [r for r in rows if r["game_id"] == params[0]]
        return pd.DataFrame(sel, columns=BOARD_VIEW_COLS)
    return fn


def _injuries(params: tuple) -> pd.DataFrame:
    abbr = params[0]
    if abbr in ("LAL", "BOS"):
        rows = [("Alpha One", "Out", "Knee Surgery", dt.date(2026, 10, 23)),
                ("Beta Two", "Day-To-Day", "Not Specified", dt.date(2026, 10, 23))]
        return pd.DataFrame(rows, columns=INJURY_COLS)
    return pd.DataFrame([], columns=INJURY_COLS)


def _team_recent(params: tuple) -> pd.DataFrame:
    abbr = params[0]
    if abbr in ("LAL", "BOS"):
        rows = [(dt.date(2026, 10, 22), abbr, "NYK", 101, 99, "W", 101, 44, 25, 2)]
        return pd.DataFrame(rows, columns=TEAM_RECENT_COLS)
    return pd.DataFrame([], columns=TEAM_RECENT_COLS)


def _players(params: tuple) -> pd.DataFrame:
    abbr = params[0]
    if abbr in ("LAL", "BOS", "GSW", "NYK"):
        rows = [("Gamma Three", 5, Decimal("30.5"), Decimal("24.0"), Decimal("5.5"),
                 Decimal("6.0"), Decimal("1.5"))]
        return pd.DataFrame(rows, columns=PLAYER_COLS)
    return pd.DataFrame([], columns=PLAYER_COLS)


def _db(board_rows: list[dict], *, hit: int, n: int, ohit: int, on: int,
        metrics: bool = True) -> FakeDb:
    recent_cols, recent_rows = _recent_from(board_rows)
    empty_metrics = pd.DataFrame([], columns=["metrics"])
    return FakeDb({
        "FROM public.v_app_board WHERE p_home_win IS NOT NULL": _frame(recent_cols, recent_rows),
        "FROM public.v_app_board WHERE game_id": _game_by_id(board_rows),
        "FROM public.model_registry_v2": _metrics_for if metrics else (lambda p: empty_metrics),
        "(p_home_win>=0.5)": _frame(["hit", "n"], [(hit, n)]),
        "(p_over>=0.5)": _frame(["hit", "n"], [(ohit, on)]),
        "FROM public.team_game_stats": _team_recent,
        "FROM public.player_game_stats": _players,
        "SELECT season FROM public.games_v2": _frame(["season"], [("2025-26",)]),
        "FROM public.injury_snapshots": _injuries,
        "max(predicted_at)": _frame(["predicted_at"],
                                    [(dt.datetime(2026, 10, 22, 20, 15, tzinfo=UTC),)]),
    })


def scenario_board() -> FakeDb:
    return _db(_board_rows(), hit=40, n=60, ohit=20, on=41)


def scenario_offseason() -> FakeDb:
    return _db(_board_rows()[1:], hit=40, n=60, ohit=0, on=0)


def scenario_empty() -> FakeDb:
    recent_cols = RECENT_COLS
    return FakeDb({
        "FROM public.v_app_board WHERE p_home_win IS NOT NULL": _frame(recent_cols, []),
        "FROM public.v_app_board WHERE game_id": lambda p: pd.DataFrame([], columns=BOARD_VIEW_COLS),
        "FROM public.model_registry_v2": pd.DataFrame([], columns=["metrics"]),
        "(p_home_win>=0.5)": _frame(["hit", "n"], [(0, 0)]),
        "(p_over>=0.5)": _frame(["hit", "n"], [(0, 0)]),
        "FROM public.team_game_stats": pd.DataFrame([], columns=TEAM_RECENT_COLS),
        "FROM public.player_game_stats": pd.DataFrame([], columns=PLAYER_COLS),
        "SELECT season FROM public.games_v2": pd.DataFrame([], columns=["season"]),
        "FROM public.injury_snapshots": pd.DataFrame([], columns=INJURY_COLS),
        "max(predicted_at)": _frame(["predicted_at"], [(None,)]),
    })
