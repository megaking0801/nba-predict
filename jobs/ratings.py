"""Elo ratings with FiveThirtyEight-style margin-of-victory multiplier and
preseason carryover. Kept separate from features.py for unit-testability."""
from __future__ import annotations

from typing import Dict

from jobs.config import CONFIG


class Elo:
    def __init__(self, k: float = CONFIG.ELO_K, hca: float = CONFIG.ELO_HCA,
                 mean: float = CONFIG.ELO_MEAN, carryover: float = CONFIG.ELO_CARRYOVER,
                 init: float = 1500.0):
        self.k = k
        self.hca = hca
        self.mean = mean
        self.carryover = carryover
        self.init = init
        self.ratings: Dict[str, float] = {}

    def get(self, abbr: str) -> float:
        return self.ratings.get(abbr, self.init)

    def diff(self, home_abbr: str, away_abbr: str) -> float:
        return self.get(home_abbr) - self.get(away_abbr)

    def expected_home_win(self, home_abbr: str, away_abbr: str) -> float:
        gap = self.get(home_abbr) + self.hca - self.get(away_abbr)
        return 1.0 / (1.0 + 10.0 ** (-gap / 400.0))

    def update(self, home_abbr: str, away_abbr: str, margin: int) -> None:
        """margin = home_score - away_score (ties impossible in the NBA)."""
        e_home = self.expected_home_win(home_abbr, away_abbr)
        s_home = 1.0 if margin > 0 else 0.0

        # 538 MOV multiplier, damped by the winner's pregame Elo edge (incl. HCA)
        if margin > 0:
            winner_gap = self.get(home_abbr) + self.hca - self.get(away_abbr)
        else:
            winner_gap = self.get(away_abbr) - (self.get(home_abbr) + self.hca)
        mov_mult = ((abs(margin) + 3.0) ** 0.8) / (7.5 + 0.006 * winner_gap)

        delta = self.k * mov_mult * (s_home - e_home)
        self.ratings[home_abbr] = self.get(home_abbr) + delta
        self.ratings[away_abbr] = self.get(away_abbr) - delta

    def new_season(self) -> None:
        for abbr in list(self.ratings):
            self.ratings[abbr] = (self.carryover * self.ratings[abbr]
                                  + (1.0 - self.carryover) * self.mean)
