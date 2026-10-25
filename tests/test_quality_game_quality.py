"""Tests for jobs.quality.game_quality: derive_flags, verify_flags and the read-only SELECT."""
from __future__ import annotations

import datetime as dt
from datetime import date
from pathlib import Path

import pandas as pd

from jobs.quality import game_quality as gq
from jobs.quality.core import Scope
from tests.quality_fakes import assert_select_only, fake_run

UTC = dt.timezone.utc


def _row(**kw) -> dict:
    """A clean regular-season game: full box, exact shots, a closing line."""
    base = dict(
        game_id="0022500001", season="2025-26", season_type="regular", game_date_et=date(2025, 12, 1),
        raw_status="final", raw_team_rows=2, raw_team_nonnull_n=2, raw_player_min_nonnull_min=9,
        raw_player_pts_diff=0, raw_box_fga_nonnull=True, raw_bucket_diff_max=0, raw_bucket_rows=2,
        raw_status_rows=26, raw_starters_n=10, raw_officials_n=3, raw_spread=-3.5, raw_total=224.5,
        raw_line_price_point="close", raw_line_total_price_point="close",
        raw_line_captured_at=dt.datetime(2025, 12, 2, 0, 0, tzinfo=UTC),
        raw_tipoff_utc=dt.datetime(2025, 12, 2, 0, 30, tzinfo=UTC))
    base.update(kw)
    return base


def test_clean_game_is_usable_everywhere():
    f = gq.derive_flags(_row())
    assert f["team_box_ok"] and f["player_box_ok"] and f["points_consistent"] is True
    assert f["shots_class"] == "exact" and f["status_ok"]
    assert all(f[k] for k in ("usable_team_features", "usable_player_features", "usable_shot_features",
                              "usable_spread_line", "usable_total_line"))
    assert f["spread_provenance"] == "close" and f["total_provenance"] == "close"
    assert not (f["is_cup_final"] or f["is_bubble"] or f["crowd_restricted"] or f["suspicious_tipoff"])


def test_derive_flags_placeholder_box():
    f = gq.derive_flags(_row(raw_team_nonnull_n=0, raw_player_min_nonnull_min=0, raw_player_pts_diff=108,
                             raw_box_fga_nonnull=False, raw_bucket_diff_max=None))
    assert f["team_box_ok"] is False and f["player_box_ok"] is False
    assert f["points_consistent"] is None            # not judged without a real player box
    assert f["shots_class"] == "no_box"
    assert not f["usable_team_features"] and not f["usable_player_features"] and not f["usable_shot_features"]


def test_derive_flags_missing_team_row():
    f = gq.derive_flags(_row(raw_team_rows=1, raw_team_nonnull_n=1))
    assert f["team_box_ok"] is False and not f["usable_team_features"]


def test_derive_flags_points_mismatch_blocks_player_features_only():
    f = gq.derive_flags(_row(raw_player_pts_diff=2))
    assert f["points_consistent"] is False
    assert f["usable_team_features"] and not f["usable_player_features"]


def test_derive_flags_shot_classes():
    assert gq.derive_flags(_row(raw_bucket_diff_max=2))["shots_class"] == "minor"
    assert gq.derive_flags(_row(raw_bucket_diff_max=2))["usable_shot_features"]
    f = gq.derive_flags(_row(raw_bucket_diff_max=3))
    assert f["shots_class"] == "major" and not f["usable_shot_features"]
    f = gq.derive_flags(_row(raw_bucket_diff_max=None, raw_bucket_rows=0))
    assert f["shots_class"] == "none" and not f["usable_shot_features"]


def test_derive_flags_lines():
    f = gq.derive_flags(_row(raw_total=2001.0))
    assert f["has_total"] and not f["total_ok"] and not f["usable_total_line"] and f["usable_spread_line"]
    f = gq.derive_flags(_row(raw_spread=None, raw_total=None, raw_line_price_point=None,
                             raw_line_total_price_point=None, raw_line_captured_at=None))
    assert f["spread_provenance"] == "none" and f["total_provenance"] == "none"
    assert not f["has_spread"] and not f["usable_spread_line"]
    tip = dt.datetime(2024, 2, 1, 1, 0, tzinfo=UTC)
    f = gq.derive_flags(_row(raw_line_price_point=None, raw_line_total_price_point=None,
                             raw_line_captured_at=tip, raw_tipoff_utc=tip))
    assert f["spread_provenance"] == "backfill_unlabeled" and f["total_provenance"] == "backfill_unlabeled"
    f = gq.derive_flags(_row(raw_line_price_point=None, raw_line_total_price_point="current",
                             raw_line_captured_at=tip, raw_tipoff_utc=tip + dt.timedelta(hours=1)))
    assert f["spread_provenance"] == "unlabeled" and f["total_provenance"] == "current"
    assert gq.derive_flags(_row(raw_spread=30.0))["spread_ok"] is False


def test_derive_flags_status_and_officials():
    assert gq.derive_flags(_row(raw_starters_n=9))["status_ok"] is False
    assert gq.derive_flags(_row(raw_status_rows=0, raw_starters_n=0))["status_ok"] is False
    assert gq.derive_flags(_row(raw_officials_n=2))["officials_n"] == 2


def test_cup_final_bubble_crowd_and_tipoff_flags():
    assert gq.derive_flags(_row(game_id="401607495"))["is_cup_final"] is True
    assert gq.derive_flags(_row(game_id="401734908"))["is_cup_final"] is True
    assert gq.derive_flags(_row(game_id="401809839"))["is_cup_final"] is True
    assert gq.derive_flags(_row(game_date_et=date(2020, 8, 15)))["is_bubble"] is True
    assert gq.derive_flags(_row(game_date_et=date(2020, 7, 29)))["is_bubble"] is False
    assert gq.derive_flags(_row(game_date_et=date(2020, 10, 12)))["is_bubble"] is True
    assert gq.derive_flags(_row(game_date_et=date(2021, 1, 15)))["crowd_restricted"] is True
    assert gq.derive_flags(_row(game_date_et=date(2021, 6, 1)))["crowd_restricted"] is False
    early = dt.datetime(2020, 3, 1, 12, 30, tzinfo=UTC)         # 07:30 ET
    assert gq.derive_flags(_row(raw_tipoff_utc=early, game_date_et=date(2020, 3, 1)))["suspicious_tipoff"] is True
    assert gq.derive_flags(_row(raw_tipoff_utc=None))["suspicious_tipoff"] is True
    mismatch = dt.datetime(2025, 12, 2, 0, 30, tzinfo=UTC)      # 19:30 ET on 12-01
    assert gq.derive_flags(_row(raw_tipoff_utc=mismatch, game_date_et=date(2025, 12, 5)))["suspicious_tipoff"] is True
    assert gq.derive_flags(_row(raw_tipoff_utc=mismatch, game_date_et=date(2025, 12, 1)))["suspicious_tipoff"] is False


def test_non_final_game_is_never_usable():
    f = gq.derive_flags(_row(raw_status="scheduled"))
    assert not f["usable_team_features"] and not f["usable_player_features"]


def _frame_from_rows(rows) -> pd.DataFrame:
    recs = []
    for r in rows:
        rec = dict(r)
        rec.update(gq.derive_flags(r))
        recs.append(rec)
    return pd.DataFrame(recs)


def test_game_quality_returns_frame_with_all_columns():
    df = _frame_from_rows([_row(), _row(game_id="0022500002", raw_team_nonnull_n=0)])
    run, db = fake_run({"qc:GQ": df})
    got = gq.game_quality(run, Scope(["2025-26"], None))
    assert set(got.columns) == set(gq.GAME_QUALITY_COLUMNS)
    assert len(got) == 2 and db.calls[0][1] == (["2025-26"],)


def test_game_quality_sql_has_every_column_and_is_select_only():
    assert_select_only(Path(gq.__file__))
    for col in gq.GAME_QUALITY_COLUMNS:
        assert f"AS {col}" in gq.SQL or f" {col}," in gq.SQL or f"\n  {col}" in gq.SQL or col in gq.SQL, col
    assert gq.SQL.lstrip().startswith("/* qc:GQ */")


def test_verify_flags_clean_and_mismatch():
    df = _frame_from_rows([_row(), _row(game_id="0022500002", raw_player_pts_diff=4)])
    assert gq.verify_flags(df) == []
    broken = df.copy()
    broken.loc[1, "usable_player_features"] = True                  # SQL disagrees with derive_flags
    broken.loc[0, "shots_class"] = "major"
    assert gq.verify_flags(broken) == ["0022500001", "0022500002"]


def test_verify_flags_treats_null_like_none():
    df = _frame_from_rows([_row(raw_player_min_nonnull_min=0, raw_player_pts_diff=50)])
    assert df.loc[0, "points_consistent"] is None or pd.isna(df.loc[0, "points_consistent"])
    assert gq.verify_flags(df) == []


def test_game_quality_sql_does_not_treat_a_null_team_score_as_consistent():
    assert "IS NULL OR pl.pp IS NULL" in gq.SQL            # a NULL score makes pts_diff NULL, not 0
