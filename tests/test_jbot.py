"""Jbot (Taiwan sports lottery odds) fetcher and comparison maths. No network: the opener is faked."""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from jobs import jbot

TOKEN = "SECRET-TOKEN-123"


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _payload(used=1, limit=20):
    return {"user": {"day_limit": limit, "day_used": used, "expiry": "2026-12-31"},
            "data": [{"id": "g1", "time": "2026-10-20T08:00", "home": "H", "away": "A", "league": "NBA",
                      "odds": [{"normal": {"x": 1}, "handi": {"y": [1, 2]}}]}]}


class _Opener:
    def __init__(self, payloads):
        self.payloads, self.requests = list(payloads), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        p = self.payloads.pop(0)
        if isinstance(p, Exception):
            raise p
        return _Resp(p)


def _clock_sleeper():
    t = {"now": 1000.0}
    sleeps = []

    def clock():
        return t["now"]

    def sleeper(s):
        sleeps.append(s)
        t["now"] += s
    return clock, sleeper, sleeps


def test_token_comes_from_the_environment_only(monkeypatch):
    monkeypatch.delenv("JBOT_TOKEN", raising=False)
    with pytest.raises(jbot.JbotError) as e:
        jbot.token_from_env()
    assert "JBOT_TOKEN" in str(e.value)
    monkeypatch.setenv("JBOT_TOKEN", TOKEN)
    assert jbot.token_from_env() == TOKEN


def test_request_sends_the_documented_header_and_params():
    clock, sleeper, _ = _clock_sleeper()
    op = _Opener([_payload()])
    data = jbot.request_odds("2026-10-20", "close", TOKEN, opener=op, sleeper=sleeper, clock=clock)
    req = op.requests[0]
    assert req.get_header("X-jbot-token") == TOKEN
    assert req.full_url.startswith("https://api.sportsbot.tech/v2/odds?")
    assert "sport=NBA" in req.full_url and "date=2026-10-20" in req.full_url and "mode=close" in req.full_url
    assert data["user"]["day_used"] == 1


def test_consecutive_calls_are_spaced_more_than_five_seconds():
    clock, sleeper, sleeps = _clock_sleeper()
    state = jbot.RateLimiter()
    op = _Opener([_payload(1), _payload(2)])
    jbot.request_odds("2026-10-20", "close", TOKEN, opener=op, sleeper=sleeper, clock=clock, limiter=state)
    jbot.request_odds("2026-10-21", "close", TOKEN, opener=op, sleeper=sleeper, clock=clock, limiter=state)
    assert sleeps and min(sleeps) > 5.0


def test_errors_never_contain_the_token():
    clock, sleeper, _ = _clock_sleeper()
    err = urllib.error.HTTPError("https://api.sportsbot.tech/v2/odds?x", 429, f"limit {TOKEN}", {}, io.BytesIO(b"{}"))
    op = _Opener([err])
    with pytest.raises(jbot.JbotError) as e:
        jbot.request_odds("2026-10-20", "close", TOKEN, opener=op, sleeper=sleeper, clock=clock)
    assert TOKEN not in str(e.value) and "429" in str(e.value)
    op = _Opener([RuntimeError(f"boom {TOKEN}")])
    with pytest.raises(jbot.JbotError) as e2:
        jbot.request_odds("2026-10-20", "close", TOKEN, opener=op, sleeper=sleeper, clock=clock)
    assert TOKEN not in str(e2.value)


def test_fetch_days_saves_raw_stops_at_the_quota_and_the_call_cap(tmp_path):
    clock, sleeper, _ = _clock_sleeper()
    op = _Opener([_payload(18, 20), _payload(19, 20), _payload(20, 20), _payload(20, 20)])
    out = jbot.fetch_days(["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"], "close", tmp_path, TOKEN,
                          max_calls=10, opener=op, sleeper=sleeper, clock=clock)
    assert out["calls"] == 3 and out["stopped"] == "daily quota used up"      # stops once used >= limit
    assert (tmp_path / "2026-10-01_close.json").exists()
    saved = json.loads((tmp_path / "2026-10-01_close.json").read_text(encoding="utf-8"))
    assert saved["data"][0]["id"] == "g1"
    assert TOKEN not in (tmp_path / "2026-10-01_close.json").read_text(encoding="utf-8")


def test_fetch_days_respects_max_calls_and_skips_saved_days(tmp_path):
    clock, sleeper, _ = _clock_sleeper()
    (tmp_path / "2026-10-01_close.json").write_text("{}", encoding="utf-8")
    op = _Opener([_payload(1), _payload(2), _payload(3)])
    out = jbot.fetch_days(["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"], "close", tmp_path, TOKEN,
                          max_calls=2, opener=op, sleeper=sleeper, clock=clock)
    assert out["calls"] == 2 and out["stopped"] == "max calls"
    assert len(op.requests) == 2 and "2026-10-02" in op.requests[0].full_url


def test_schema_of_records_structure_without_values():
    s = jbot.schema_of(_payload())
    assert s["user"] == {"day_limit": "int", "day_used": "int", "expiry": "str"}
    g = s["data"][0]
    assert g["odds"][0]["normal"] == {"x": "int"} and g["odds"][0]["handi"] == {"y": ["int"]}
    assert "g1" not in json.dumps(s)


def test_quota_of():
    assert jbot.quota_of(_payload(3, 20)) == (20, 3)
    assert jbot.quota_of({}) == (None, None)


# ---------- comparison maths on a normalised table ----------

def _norm(n=400, seed=0, tw_edge=0.0):
    """Taiwan prices built from a true probability with a 12% overround; consensus probability is the truth."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.2, 0.8, n)
    over = 1.12
    return pd.DataFrame({"p_consensus": p,
                         "tw_ml_home_dec": 1 / (np.clip(p + tw_edge, 0.01, 0.99) * over),
                         "tw_ml_away_dec": 1 / ((1 - np.clip(p + tw_edge, 0.01, 0.99)) * over),
                         "espn_ml_home_dec": 1 / (p * 1.045), "espn_ml_away_dec": 1 / ((1 - p) * 1.045),
                         "tw_spread_line": -3.5, "espn_home_spread": -3.5})


def test_overround():
    d = pd.DataFrame({"a": [1.9, 2.0], "b": [1.9, 2.0]})
    r = jbot.overround(d["a"], d["b"])
    assert r.iloc[0] == pytest.approx(2 / 1.9 - 1) and r.iloc[1] == pytest.approx(0.0)


def test_ev_vs_consensus_is_negative_when_prices_are_fair_plus_vig():
    out = jbot.ev_vs_consensus(_norm(4000), reps=200, seed=1)
    assert out["tw"]["mean_best_side_ev"] < 0 and out["espn"]["mean_best_side_ev"] < 0
    assert out["tw"]["mean_best_side_ev"] < out["espn"]["mean_best_side_ev"]        # 12% vs 4.5% overround
    assert out["tw"]["share_positive_ev"] < 0.5


def test_ev_vs_consensus_finds_a_stale_taiwan_price():
    out = jbot.ev_vs_consensus(_norm(4000, tw_edge=0.15), reps=200, seed=1)
    assert out["tw"]["mean_best_side_ev"] > 0.02 and out["tw"]["share_positive_ev"] > 0.3


def test_line_difference():
    d = pd.DataFrame({"tw_spread_line": [-3.5, -4.0, -2.0], "espn_home_spread": [-3.5, -3.0, -3.0]})
    out = jbot.line_difference(d, "tw_spread_line", "espn_home_spread")
    assert out["n"] == 3 and out["mean_abs_diff"] == pytest.approx(2 / 3) and out["share_same_line"] == pytest.approx(1 / 3)


def test_hint_of_value_needs_a_positive_interval_and_enough_games():
    assert jbot.hint_of_value({"n": 150, "lo": 0.01}) is True
    assert jbot.hint_of_value({"n": 150, "lo": -0.01}) is False
    assert jbot.hint_of_value({"n": 40, "lo": 0.05}) is False


def test_module_never_prints_or_stores_the_token_in_source():
    src = Path(jbot.__file__).read_text(encoding="utf-8")
    assert "print(token" not in src and "JBOT_TOKEN" in src
    for word in ("INSERT ", "UPDATE ", "DELETE ", "commit()", "executemany", "ensure_schema"):
        assert word not in src
    assert "cron" not in src.lower() and "schedule" not in src.lower().replace("scheduling", "")


# ---------- the documented response layout ----------

def _raw():
    return {"status": "OK", "user": {"day_limit": 20, "day_used": 1, "email": "x", "expiry": "2026-12-31"},
            "data": [
                {"id": "A1", "time": "2026-10-21T08:00", "away": "克里夫蘭騎士", "home": "奧蘭多魔術", "league": "NBA",
                 "odds": [{"update": "2026-10-20T20:00",
                           "normal": {"a": 1.62, "h": 2.05, "s": 1},
                           "handi": {"-5.5": {"a": 1.76, "h": 1.74, "m": True, "s": 1},
                                     "-4.5": {"a": 1.6, "h": 1.95, "m": False, "s": 1}},
                           "total": {"221.5": {"o": 1.75, "u": 1.75, "m": True, "s": 1}}}]},
                {"id": "A2", "time": "2026-10-21T10:00", "away": "洛杉磯快艇", "home": "費城76人", "league": "NBA",
                 "odds": [{"update": "t1", "normal": {"a": 1.5, "h": 2.3}},
                          {"update": "t2", "normal": {"a": 1.55, "h": 2.2}}]},
                {"id": "A3", "time": "2026-10-21T11:00", "away": "不明球隊", "home": "洛杉磯湖人", "league": "NBA", "odds": []}]}


def test_team_names_map_by_nickname_suffix():
    assert jbot.team_abbr_from_cn("奧蘭多魔術") == "ORL" and jbot.team_abbr_from_cn("費城76人") == "PHI"
    assert jbot.team_abbr_from_cn("洛杉磯快艇") == "LAC" and jbot.team_abbr_from_cn("洛杉磯湖人") == "LAL"
    assert jbot.team_abbr_from_cn("克里夫蘭騎士") == "CLE"
    assert jbot.team_abbr_from_cn("不明球隊") is None and jbot.team_abbr_from_cn(None) is None


def test_normalize_day_reads_the_main_lines():
    n = jbot.normalize_day(_raw())
    a = n.iloc[0]
    assert (a["home_abbr"], a["away_abbr"]) == ("ORL", "CLE")
    assert a["tw_ml_home_dec"] == 2.05 and a["tw_ml_away_dec"] == 1.62
    assert a["tw_spread_line"] == -5.5 and a["tw_spread_home_dec"] == 1.74 and a["tw_spread_away_dec"] == 1.76
    assert a["tw_total_line"] == 221.5 and a["tw_over_dec"] == 1.75
    assert a["n_odds"] == 1


def test_normalize_day_uses_the_newest_odds_entry_and_tolerates_missing_blocks():
    n = jbot.normalize_day(_raw())
    b = n.iloc[1]
    assert b["tw_ml_home_dec"] == 2.2 and b["n_odds"] == 2 and b["tw_spread_line"] is None or pd.isna(b["tw_spread_line"])
    c = n.iloc[2]
    assert c["away_abbr"] is None and pd.isna(c["tw_ml_home_dec"])
    assert len(jbot.normalize_day({})) == 0


def test_handicap_sign_matches_espn_home_spread():
    """'-5.5' is the HOME handicap: the home team gives 5.5 points, like ESPN's home_spread of -5.5."""
    n = jbot.normalize_day(_raw())
    diff = jbot.line_difference(pd.DataFrame({"tw_spread_line": [n.iloc[0]["tw_spread_line"]],
                                              "espn_home_spread": [-5.5]}), "tw_spread_line", "espn_home_spread")
    assert diff["mean_abs_diff"] == 0 and diff["share_same_line"] == 1.0
    flipped = jbot.line_difference(pd.DataFrame({"tw_spread_line": [n.iloc[0]["tw_spread_line"]],
                                                 "espn_home_spread": [5.5]}), "tw_spread_line", "espn_home_spread")
    assert flipped["mean_abs_diff"] == 11.0


def test_the_documented_sample_overround_is_about_fourteen_percent():
    n = jbot.normalize_day(_raw()).iloc[0]
    o = jbot.overround(pd.Series([n["tw_spread_away_dec"]]), pd.Series([n["tw_spread_home_dec"]]))
    assert o.iloc[0] == pytest.approx(1 / 1.76 + 1 / 1.74 - 1) and 0.14 < o.iloc[0] < 0.15


def test_spread_ev_at_the_same_line_is_minus_the_margin():
    e = jbot.spread_ev(-5.5, 1.74, 1.76, market_home_spread=-5.5)
    assert e["p_home_cover"] == pytest.approx(0.5) and e["ev_home"] == pytest.approx(0.87 - 1)         and e["ev_away"] == pytest.approx(0.88 - 1)


def test_spread_ev_rewards_a_cheaper_taiwan_line_on_the_right_side():
    # market says the home team wins by 5.5; Taiwan lets the home team give only 3.5: home side is worth more
    e = jbot.spread_ev(-3.5, 1.80, 1.80, market_home_spread=-5.5)
    assert e["p_home_cover"] > 0.5 and e["ev_home"] > e["ev_away"]
    t = jbot.total_ev(215.5, 1.8, 1.8, market_total=221.5)
    assert t["p_over"] > 0.5 and t["ev_over"] > t["ev_under"]


def _games_df():
    return pd.DataFrame({"game_id": ["e1", "e2", "e3"], "home_abbr": ["ORL", "PHI", "ORL"],
                         "away_abbr": ["CLE", "LAC", "CLE"],
                         "tipoff_utc": pd.to_datetime(["2026-10-21 00:00", "2026-10-21 02:00", "2026-10-30 00:00"], utc=True),
                         "date": pd.to_datetime(["2026-10-20", "2026-10-20", "2026-10-29"]).date,
                         "home_spread": [-5.5, -2.0, -1.0], "total_line": [221.5, 218.0, 210.0]})


def test_match_games_measures_the_time_offset_instead_of_assuming_it():
    matched, info = jbot.match_games(jbot.normalize_day(_raw()), _games_df())
    # Taiwan 08:00 vs UTC 00:00 -> +8 hours, measured from the two unambiguous matches
    assert info["offset_hours_median"] == pytest.approx(8.0)
    assert sorted(matched["game_id"]) == ["e1", "e2"] and info["matched"] == 2
    assert "e3" not in set(matched["game_id"])                       # same teams ten days later: not within a day


def test_comparison_table_joins_the_espn_consensus():
    matched, _ = jbot.match_games(jbot.normalize_day(_raw()), _games_df())
    espn = pd.DataFrame({"game_id": ["e1"], "p_home": [0.7], "home_dec": [1.45], "away_dec": [2.8]})
    d = jbot.comparison_table(matched, espn)
    assert len(d) == 1 and d.iloc[0]["p_consensus"] == 0.7 and d.iloc[0]["espn_home_spread"] == -5.5


def test_compare_report_end_to_end_on_synthetic_games():
    raw = _raw()
    norm = jbot.normalize_day(raw)
    espn = pd.DataFrame({"game_id": ["e1", "e2"], "p_home": [0.66, 0.40], "home_dec": [1.5, 2.4], "away_dec": [2.6, 1.6]})
    out = jbot.compare_report(norm, _games_df(), espn, reps=100, seed=1)
    assert out["compared_games"] == 2 and out["matching"]["offset_hours_median"] == pytest.approx(8.0)
    assert 0.12 < out["overround"]["spread"]["mean"] < 0.16
    assert out["spread_line_vs_espn"]["share_same_line"] == 1.0 or out["spread_line_vs_espn"]["n"] >= 1
    assert out["moneyline_ev_vs_consensus"]["tw"]["mean_best_side_ev"] < out["moneyline_ev_vs_consensus"]["espn"]["mean_best_side_ev"] + 0.2
    assert "verdict_hint" in out
    json.dumps(out, default=str)


def test_compare_report_with_nothing_matched():
    norm = jbot.normalize_day(_raw())
    out = jbot.compare_report(norm, _games_df().iloc[0:0], pd.DataFrame(columns=["game_id", "p_home", "home_dec", "away_dec"]))
    assert out["compared_games"] == 0
