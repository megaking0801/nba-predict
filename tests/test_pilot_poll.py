"""jobs.pilot_poll: budget, schedule, parsing and one tick with fake fetchers (no network, no DB)."""
from __future__ import annotations

import datetime as dt
import gzip
import json

from jobs import pilot_poll as pp

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 28, 14, 0, tzinfo=UTC)          # 10:00 ET


def injuries_row(status, name="A. Player", team="ATL", rep=None):
    return {"team_abbr": team, "player_name": name, "status": status, "detail": "Knee",
            "reported_at": rep, "espn_player_id": 1}


def scoreboard(hours_ahead=8):
    tip = (NOW + dt.timedelta(hours=hours_ahead)).strftime("%Y-%m-%dT%H:%MZ")
    return {"events": [{"id": "401", "date": tip, "status": {"type": {"state": "pre"}}, "competitions": [{
        "competitors": [{"homeAway": "home", "team": {"abbreviation": "ATL"}},
                        {"homeAway": "away", "team": {"abbreviation": "MIA"}}]}]}]}


ODDS_GAMES = [{"home_team": "Atlanta Hawks", "away_team": "Miami Heat", "commence_time": "2026-10-28T23:00:00Z",
               "bookmakers": [{"key": "fanduel", "last_update": "x", "markets": [{
                   "key": "spreads", "last_update": "2026-10-28T14:01:10Z", "outcomes": [
                       {"name": "Atlanta Hawks", "price": 1.91, "point": -3.5},
                       {"name": "Miami Heat", "price": 1.91, "point": 3.5}]}]}]}]


def make(tmp_path, rows_seq, key="SECRETKEY123", daily_cap=16):
    calls = {"odds_api": 0, "espn_odds": 0}
    seq = iter(rows_seq)

    def odds_api(k):
        calls["odds_api"] += 1
        return ODDS_GAMES, 480 - calls["odds_api"]

    def espn_odds(gid):
        calls["espn_odds"] += 1
        return {"home_spread": -3.5, "home_price": 1.91, "away_price": 1.91, "total_line": 220.5, "price_point": "current"}

    fetch = {"scoreboard": lambda d: scoreboard(), "injuries": lambda: next(seq),
             "espn_odds": espn_odds, "odds_api": odds_api}
    poller = pp.Poller(pp.Out(tmp_path), pp.Budget(tmp_path / "state.json", daily_cap=daily_cap), fetch, key,
                       odds_base_s=1e12)
    return poller, calls


def records(tmp_path):
    out = []
    for f in sorted(tmp_path.glob("poll-*.jsonl.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            out += [json.loads(line) for line in fh]
    return out


def test_budget_caps_and_reserve(tmp_path):
    b = pp.Budget(tmp_path / "s.json", daily_cap=2, monthly_cap=10, reserve=5)
    assert b.can_spend(NOW)
    b.spend(NOW, 1, remaining=100)
    b.spend(NOW, 1, remaining=99)
    assert not b.can_spend(NOW)                                     # daily cap
    assert b.can_spend(NOW + dt.timedelta(days=1))                  # new UTC day
    b2 = pp.Budget(tmp_path / "s.json", daily_cap=9, monthly_cap=10, reserve=5)   # state survives a restart
    assert b2.state["day_used"] == 2 and b2.state["remaining"] == 99
    b2.state["remaining"] = 5
    assert not b2.can_spend(NOW)                                    # provider's own counter hits the reserve


def test_schedule_merges_nearby_requests():
    s = pp.OddsSchedule()
    s.add_event(1000.0)
    s.add_event(1060.0)                                             # a minute later: merged into the same samples
    assert len(s.times) == len(pp.OFFSETS_MIN)
    assert not s.pop_due(999.0) and s.pop_due(1000.0) and not s.pop_due(1001.0)


def test_parse_oddsapi_keeps_each_books_own_last_update():
    g = pp.parse_oddsapi(ODDS_GAMES)[0]
    assert (g["home"], g["away"]) == ("ATL", "MIA")
    assert g["books"][0] == {"book": "fanduel", "last_update": "2026-10-28T14:01:10Z", "home_point": -3.5,
                             "home_price": 1.91, "away_price": 1.91}


def test_event_triggers_watch_and_odds_sample(tmp_path):
    p, calls = make(tmp_path, [[injuries_row("Day-To-Day")], [injuries_row("Out")]])
    s0 = p.tick(NOW)                       # baseline: no events; one slow 'base' sample = the pre-event reference
    assert s0["events"] == 0 and calls["odds_api"] == 1
    s1 = p.tick(NOW + dt.timedelta(minutes=5))                      # status change to Out, game in 8 h
    assert s1["events"] == 1 and s1["odds_calls"] == 1 and s1["espn_lines"] == 0   # ESPN line unchanged
    recs = records(tmp_path)
    assert [r["src"] for r in recs].count("injury_event") == 2      # baseline entry + the change
    assert [r["reason"] for r in recs if r["src"] == "oddsapi"] == ["base", "event"]
    od = next(r for r in recs if r["src"] == "oddsapi")
    assert od["games"][0]["books"][0]["last_update"] == "2026-10-28T14:01:10Z" and "fetched_at" in od
    assert "SECRETKEY123" not in json.dumps(recs) + (tmp_path / "state.json").read_text()   # the key is never written
    assert p.budget.state["day_used"] == 2


def test_no_key_means_no_odds_api_calls(tmp_path):
    p, calls = make(tmp_path, [[injuries_row("Day-To-Day")], [injuries_row("Out")]], key=None)
    p.tick(NOW)
    p.tick(NOW + dt.timedelta(minutes=5))
    assert calls["odds_api"] == 0 and calls["espn_odds"] >= 1


def test_daily_cap_stops_calls(tmp_path):
    rows = [[injuries_row("Day-To-Day")], [injuries_row("Out")], [injuries_row("Out", rep=NOW)],
            [injuries_row("Out", rep=NOW + dt.timedelta(minutes=1))]]
    p, calls = make(tmp_path, rows, daily_cap=1)
    for i in range(4):
        p.tick(NOW + dt.timedelta(minutes=5 * i))
    assert calls["odds_api"] == 1                                  # cap 1: only the first (base) sample


def test_fetch_failure_is_logged_and_does_not_crash(tmp_path):
    p, _ = make(tmp_path, [None])
    assert p.tick(NOW)["errors"] == 1
    assert any(r["src"] == "error" for r in records(tmp_path))
