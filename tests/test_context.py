"""P4.1: static team geography and date windows used by the context features."""
from __future__ import annotations

from datetime import date

import pytest

from jobs import context
from jobs.quality import game_quality
from jobs.teams import CANONICAL_ABBRS


def test_every_team_has_a_venue_and_nothing_else():
    assert set(context.TEAM_VENUE) == set(CANONICAL_ABBRS)


def test_venue_values_are_plausible():
    for abbr, (lat, lon, tz, elev) in context.TEAM_VENUE.items():
        assert 25 < lat < 46 and -125 < lon < -70, abbr
        assert -5 <= elev <= 1700, abbr


@pytest.mark.parametrize("a,b,km,tol", [
    ("LAL", "BOS", 4170, 0.03), ("GSW", "LAL", 560, 0.05), ("BOS", "MIA", 2020, 0.03),
    ("NYK", "BKN", 6, 0.6), ("DEN", "UTA", 600, 0.05), ("MIN", "MIA", 2430, 0.03),
])
def test_distance_matches_known_city_pairs(a, b, km, tol):
    assert abs(context.distance_km(a, b) - km) <= km * tol


def test_distance_is_symmetric_and_zero_to_self():
    assert context.distance_km("DEN", "CHI") == pytest.approx(context.distance_km("CHI", "DEN"))
    assert context.distance_km("DEN", "DEN") == 0.0


def test_utc_offset_follows_daylight_saving():
    assert context.utc_offset_hours("BOS", date(2025, 1, 15)) == -5
    assert context.utc_offset_hours("BOS", date(2025, 7, 15)) == -4
    assert context.utc_offset_hours("LAL", date(2025, 1, 15)) == -8
    assert context.utc_offset_hours("DEN", date(2025, 1, 15)) == -7
    assert context.utc_offset_hours("PHX", date(2025, 1, 15)) == -7
    assert context.utc_offset_hours("PHX", date(2025, 7, 15)) == -7        # Arizona has no DST
    assert context.utc_offset_hours("CHI", date(2025, 7, 15)) == -5


def test_tz_shift_is_positive_when_travelling_east():
    d = date(2025, 1, 15)
    assert context.tz_shift_hours("LAL", "BOS", d) == 3
    assert context.tz_shift_hours("BOS", "LAL", d) == -3
    assert context.tz_shift_hours("BOS", "BOS", d) == 0


def test_elevation_edge_only_for_the_mountain_teams():
    assert context.elevation_m("DEN") > 1500 and context.elevation_m("UTA") > 1200
    assert context.elevation_m("MIA") < 20


def test_date_windows_match_the_quality_package():
    assert (context.BUBBLE_START, context.BUBBLE_END) == (game_quality.BUBBLE_START, game_quality.BUBBLE_END)
    assert context.CROWD_RESTRICTED == game_quality.CROWD_RESTRICTED
    assert context.is_bubble(date(2020, 8, 15)) and not context.is_bubble(date(2020, 7, 29))
    assert context.is_crowd_restricted(date(2021, 1, 15)) and not context.is_crowd_restricted(date(2021, 6, 1))
