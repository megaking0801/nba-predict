"""jobs.tw_parse on synthetic snapshots shaped like the real Taiwan, ESPN and Odds API responses."""
import json
from pathlib import Path

import pandas as pd
import pytest

from jobs import tw_parse as tp

FIX = Path(__file__).parent / "fixtures" / "tw"
TW = (FIX / "20261029T2100+0800-tw.json").read_bytes()
ESPN = (FIX / "20261029T2100+0800-espn.json").read_bytes()
ODDS = (FIX / "20261029T2100+0800-oddsapi.json").read_bytes()


def test_list_snapshots_parses_offset_and_skips_others(tmp_path):
    day = tmp_path / "20261029"
    day.mkdir()
    (day / "20261029T2100+0800-tw.json").write_bytes(TW)
    (day / "20261029T2100+0800-espn.json").write_bytes(ESPN)
    (day / "notes.txt").write_text("x")
    (day / ".DS_Store").write_text("x")
    (tmp_path / "_samples").mkdir()
    (tmp_path / "_samples" / "20261029T1635+0800-tw.json").write_bytes(TW)
    s = tp.list_snapshots(tmp_path)
    assert sorted(s["kind"]) == ["espn", "tw"]
    assert (s["stamp_utc"] == pd.Timestamp("2026-10-29 13:00", tz="UTC")).all()


def test_parse_tw_strips_bom_and_converts_prices():
    t = tp.parse_tw(TW)
    ml = t[(t["market"] == "ml") & (t["side"] == "away")]
    assert ml["dec"].iloc[0] == pytest.approx(2.3)
    assert set(t["home"]) == {"NOP", "POR"}                         # the CBA game is dropped
    sac = t[(t["home"] == "NOP") & (t["market"] == "spread")]
    assert sorted(sac["line"].unique()) == [-6.5, -5.5, -4.5]
    assert set(t["market"]) == {"ml", "spread", "total"}             # team totals and half markets are dropped
    assert t[(t["home"] == "NOP") & (t["market"] == "total")]["line"].unique().tolist() == [237.5]


def test_parse_tw_spread_sign_is_home_handicap():
    t = tp.parse_tw(TW)
    s = t[(t["home"] == "NOP") & (t["market"] == "spread") & (t["line"] == -5.5)].set_index("side")
    assert s.loc["home", "dec"] == pytest.approx(1.72) and s.loc["away", "dec"] == pytest.approx(1.72)
    assert s.loc["home", "away"] == "SAC"
    assert s.loc["home", "kickoff_utc"] == pd.Timestamp("2026-10-30 00:00", tz="UTC")
    assert s.loc["home", "updated_utc"] == pd.Timestamp("2026-10-29 12:30", tz="UTC")
    assert s.loc["home", "m"] == 2


def test_parse_tw_integer_line_and_totals_sides():
    t = tp.parse_tw(TW)
    por = t[t["home"] == "POR"]
    assert por[por["market"] == "spread"]["line"].unique().tolist() == [-7.0]
    assert set(por[por["market"] == "total"]["side"]) == {"over", "under"}
    assert (por["m"] == 1).all()


def _mutate(fn):
    games = json.loads(TW.decode("utf-8-sig"))
    fn(games)
    return json.dumps(games, ensure_ascii=False).encode("utf-8")


def test_parse_tw_raises_when_name_sign_disagrees_with_hv():
    def flip(games):
        games[0]["ms"][2]["cs"][1]["name"] = "紐奧良鵜鶘 +5.5"       # home option, hv says the home team gives 5.5
    with pytest.raises(ValueError):
        tp.parse_tw(_mutate(flip))


def test_parse_tw_unknown_team_raises():
    def rename(games):
        games[0]["hn"] = "火星隊"
    with pytest.raises(ValueError):
        tp.parse_tw(_mutate(rename))


def test_parse_espn_reads_close_and_open_and_checks_provider():
    e = tp.parse_espn(ESPN).set_index("home")
    assert set(e.index) == {"NOP", "POR"}                             # the game without odds is dropped
    nop = e.loc["NOP"]
    assert nop["away"] == "SAC" and nop["tipoff_utc"] == pd.Timestamp("2026-10-30 00:00", tz="UTC")
    assert nop["spread_line"] == -5.5 and nop["spread_line_open"] == -5.0
    assert nop["spread_home_dec"] == pytest.approx(1 + 100 / 110)
    assert nop["total_line"] == 237.5 and nop["total_line_open"] == 236.5
    assert nop["over_dec"] == pytest.approx(1 + 100 / 105)
    assert e.loc["POR", "spread_home_dec"] == pytest.approx(2.0)
    bad = json.loads(ESPN)
    bad["events"][0]["competitions"][0]["odds"][0]["provider"]["name"] = "Caesars"
    with pytest.raises(ValueError):
        tp.parse_espn(json.dumps(bad).encode())


def test_parse_oddsapi_rows():
    o = tp.parse_oddsapi(ODDS)
    pin = o[o["book"] == "pinnacle"].set_index(["market", "side"])
    assert pin.loc[("spread", "home"), "line"] == -5.5 and pin.loc[("spread", "away"), "line"] == -5.5
    assert pin.loc[("spread", "home"), "dec"] == pytest.approx(1.95)
    assert pin.loc[("total", "over"), "line"] == 237.5 and pin.loc[("total", "over"), "dec"] == pytest.approx(1.91)
    assert set(o["home"]) == {"NOP"} and set(o["away"]) == {"SAC"}
    assert set(o["book"]) == {"pinnacle", "draftkings"}
    bad = json.loads(ODDS)
    bad[0]["home_team"] = "Paris Basketball"
    with pytest.raises(ValueError):
        tp.parse_oddsapi(json.dumps(bad).encode())
