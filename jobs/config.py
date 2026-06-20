"""Single source of truth for every tunable parameter in the rebuilt system.

Every model version and pick row records CONFIG_HASH so results are always
traceable to the exact parameter set that produced them.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Config:
    # --- features ---
    WINDOW_SHORT: int = 10            # form / four-factors window
    WINDOW_LONG: int = 30             # season-scale quality
    WINDOW_TALENT_GAMES: int = 5      # realized-talent lookback
    SHRINK_K_SHORT: int = 5           # prior weight vs 10-game window
    SHRINK_K_LONG: int = 10
    PREV_SEASON_PRIOR_W: float = 0.6  # regression-to-mean on prev-season net rating
    PLAYER_EWMA_LAMBDA: float = 0.15  # ~13-game effective memory
    PLAYER_MIN_MINUTES: float = 8.0   # below this, per-minute rates are noise
    PLAYER_SHRINK_GAMES: int = 3      # v_used shrink denominator
    PLAYER_PRIOR_VALUE: float = 0.35  # conservative GameScore/min prior for unknowns
    ROTATION_TOP_N: int = 8           # full-strength talent definition
    REST_CLIP_DAYS: int = 3           # 4+ days rest treated the same
    ELO_K: float = 20.0
    ELO_HCA: float = 65.0             # Elo points of home advantage
    ELO_CARRYOVER: float = 0.75       # preseason regression; rest to mean 1505
    ELO_MEAN: float = 1505.0
    ENABLE_ALTITUDE_FLAG: bool = False

    # --- training-row eligibility ---
    WARMUP_GP_FIRST_SEASON: int = 15  # no priors exist in season 1 of backfill
    MIN_GP_OTHER_SEASONS: int = 5
    MIN_TRAIN_ROWS: int = 800         # below this, training refuses to ship

    # --- model ---
    RIDGE_ALPHA: float = 10.0         # default; walk-forward picks from RIDGE_ALPHA_GRID
    RIDGE_ALPHA_GRID: tuple = (1.0, 3.0, 10.0, 30.0, 100.0)
    HGB_LEARNING_RATE: float = 0.05
    HGB_MAX_DEPTH: int = 3
    HGB_MAX_LEAF_NODES: int = 15
    HGB_MIN_SAMPLES_LEAF: int = 40
    HGB_L2: float = 1.0
    HGB_MAX_ITER: int = 300           # walk-forward picks from HGB_ITER_GRID; no early stopping
    HGB_ITER_GRID: tuple = (150, 300, 450)
    MODEL_CANDIDATES: tuple = ("ridge", "hgb", "blend")
    SAMPLE_WEIGHT_SCHEME: str = "uniform"   # alt: "season_decay" {1.0, 0.85, 0.7}
    SEASON_DECAY_WEIGHTS: tuple = (1.0, 0.85, 0.7)
    RANDOM_STATE: int = 42

    # --- probability head (Stage 2) ---
    RESIDUAL_DIST: str = "normal"     # alt: "t8"
    SIGMA_ESTIMATOR: str = "std"      # alt: "mad"
    HETERO_SIGMA: bool = False        # v1.1 flag; needs walk-forward Brier win >= 0.0005

    # --- forward calibration (Stage 3) ---
    CAL_MIN_N_PLATT: int = 150
    CAL_MIN_N_ISOTONIC: int = 600
    USE_LEGACY_CALIBRATION: bool = False    # enable only after legacy-line sanity audit
    LEGACY_CALIBRATION_WEIGHT: float = 0.5
    CAL_SLOPE_ALARM: tuple = (0.5, 1.5)
    CAL_INTERCEPT_ALARM: float = 0.3

    # --- walk-forward validation ---
    WF_MIN_TRAIN_ROWS: int = 800
    WF_STEP_DAYS: int = 14
    GATE_MAE_ABS: float = 10.5
    GATE_TOTAL_MAE_ABS: float = 16.0   # totals model: NBA total stdev ~18–20, looser than margin
    GATE_MAE_VS_BASELINE: float = 0.25
    GATE_BRIER_MAX: float = 0.2500
    GATE_CAL_SLOPE: tuple = (0.7, 1.3)
    GATE_BIAS_ABS: float = 0.5
    PROMOTION_BRIER_TOL: float = 0.002

    # --- picks ---
    MIN_EDGE: float = 0.04
    MIN_EDGE_ALARM_MODE: float = 0.06
    MAX_PICKS_PER_DAY: int = 4
    LINE_MAX_AGE_HOURS: float = 12.0
    DISAGREEMENT_GUARD_PTS: float = 12.0
    INJURY_VETO_TALENT_RANK: int = 3
    KELLY_FRACTION: float = 0.0       # flat stake
    PAPER_MODE_WEEKS: int = 4

    # --- retraining ---
    RETRAIN_EVERY_DAYS: int = 7
    RETRAIN_MIN_NEW_GAMES: int = 50

    # --- bookmakers (preference order for line selection) ---
    BOOK_PREFERENCE: tuple = (
        "pinnacle", "draftkings", "fanduel", "betmgm", "caesars", "pointsbetus",
    )

    # --- feature set version tag (bump when the feature list changes) ---
    FEATURE_SET: str = "fs1"
    FEATURE_SET_TOTAL: str = "ts1"   # totals model feature set


CONFIG = Config()


def config_hash(cfg: Config = CONFIG) -> str:
    payload = json.dumps(asdict(cfg), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


CONFIG_HASH = config_hash()


if __name__ == "__main__":
    print(f"config_hash={CONFIG_HASH}")
    for k, v in asdict(CONFIG).items():
        print(f"  {k} = {v!r}")
