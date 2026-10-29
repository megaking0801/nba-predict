"""Chinese (Taiwan lottery) and Odds API team names -> canonical abbr; unknown names must raise."""
import pytest

from jobs import teams


def test_abbr_from_cn_matches_by_nickname():
    assert teams.abbr_from_cn("克里夫蘭騎士") == "CLE"
    assert teams.abbr_from_cn(" 紐奧良鵜鶘 ") == "NOP"
    assert teams.abbr_from_cn("洛杉磯快艇") == "LAC" and teams.abbr_from_cn("洛杉磯湖人") == "LAL"


@pytest.mark.parametrize("bad", ["不明球隊", "", None])
def test_abbr_from_cn_raises_on_unknown(bad):
    with pytest.raises(ValueError):
        teams.abbr_from_cn(bad)


def test_require_team_name():
    assert teams.require_team_name("Portland Trail Blazers") == "POR"
    with pytest.raises(ValueError):
        teams.require_team_name("Paris Basketball")
