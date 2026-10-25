"""P4.0: data-scope switches on the feature builder. Defaults must leave fs1 untouched."""
from __future__ import annotations

import copy
import dataclasses

import pandas as pd

from jobs.config import CONFIG
from jobs.features import FEATURE_NAMES, TOTAL_FEATURE_NAMES, build_feature_table, scope_bundles
from tests.test_features_point_in_time import make_bundles

FS1_FROZEN_HASH = 1509810820937614110      # numeric columns of make_bundles() rounded to 9 decimals


def _frozen_hash(table: pd.DataFrame) -> int:
    """Hash of the fs1/ts1 columns only (new context columns may be added without touching it)."""
    fs1_cols = {"margin", *FEATURE_NAMES, *TOTAL_FEATURE_NAMES}
    num = table[[c for c in table.columns if c in fs1_cols]].select_dtypes("number").round(9)
    return int(pd.util.hash_pandas_object(num, index=True).sum() % (2 ** 61))


def test_fs1_output_frozen_with_default_flags():
    assert _frozen_hash(build_feature_table(make_bundles())) == FS1_FROZEN_HASH


def test_flag_defaults_keep_current_behaviour():
    assert CONFIG.EXCLUDE_PRESEASON is False and CONFIG.TRAIN_FROM_SEASON == ""
    assert len(FEATURE_NAMES) == 17 and len(TOTAL_FEATURE_NAMES) == 18


def _with_preseason():
    """Two preseason games in front of the regular games, in the same season."""
    bundles = make_bundles()
    pre = []
    for i, src in enumerate(bundles[:2]):
        b = copy.deepcopy(src)
        b["game_id"] = f"pre{i}"
        b["season_type"] = "preseason"
        b["date"] = b["date"].replace(month=10, day=1 + i)
        pre.append(b)
    return pre, bundles


def test_scope_bundles_drops_preseason_only_when_asked():
    pre, reg = _with_preseason()
    allb = pre + reg
    assert scope_bundles(allb, CONFIG) == allb
    cfg = dataclasses.replace(CONFIG, EXCLUDE_PRESEASON=True)
    assert scope_bundles(allb, cfg) == reg


def test_excluding_preseason_equals_never_having_seen_it():
    pre, reg = _with_preseason()
    cfg = dataclasses.replace(CONFIG, EXCLUDE_PRESEASON=True)
    with_pre = build_feature_table(pre + reg, cfg)
    clean = build_feature_table(reg, cfg)
    assert list(with_pre["game_id"]) == list(clean["game_id"])
    assert _frozen_hash(with_pre) == _frozen_hash(clean) == FS1_FROZEN_HASH


def test_including_preseason_changes_later_rows():
    pre, reg = _with_preseason()
    with_pre = build_feature_table(pre + reg)
    assert len(with_pre) == len(reg) + 2
    later = with_pre[~with_pre["game_id"].str.startswith("pre")].reset_index(drop=True)
    assert _frozen_hash(later) != FS1_FROZEN_HASH      # state absorbed the preseason games


def test_train_from_season_only_changes_eligibility():
    bundles = make_bundles()
    for b in bundles[30:]:
        b["season"] = "2025-26"
    base = build_feature_table(bundles)
    cfg = dataclasses.replace(CONFIG, TRAIN_FROM_SEASON="2025-26")
    late = build_feature_table(bundles, cfg)
    feature_cols = [c for c in base.columns if c != "eligible"]
    pd.testing.assert_frame_equal(base[feature_cols], late[feature_cols])
    assert not late.loc[late["season"] == "2024-25", "eligible"].any()
    assert (late["eligible"] <= base["eligible"]).all()
