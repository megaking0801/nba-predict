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
            "games": [{"id": "g1", "time": "2026-10-20 08:00", "home": "H", "away": "A", "league": "NBA",
                       "odds": {"normal": {"x": 1}, "handi": {"y": [1, 2]}}}]}


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
    assert saved["games"][0]["id"] == "g1"
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
    g = s["games"][0]
    assert g["odds"]["normal"] == {"x": "int"} and g["odds"]["handi"] == {"y": ["int"]}
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
