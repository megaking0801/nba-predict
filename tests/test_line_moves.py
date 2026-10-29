"""C2 step 1: fixed-seed sample, opening / closing spread parsing, the continue checks, no DB writes."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import re
from pathlib import Path

import pandas as pd

from jobs import line_moves as lm


def _games():
    rows = []
    for s in ("2015-16", "2018-19", "2022-23", "2023-24", "2024-25", "2025-26"):
        for i in range(40):
            rows.append({"game_id": f"{s[:4]}{i:04d}", "season": s, "date": dt.date(int(s[:4]), 11, 1)})
    return pd.DataFrame(rows)


def test_sample_is_fixed_and_follows_the_plan():
    a, b = lm.sample_games(_games()), lm.sample_games(_games())
    assert list(a["game_id"]) == list(b["game_id"]) and len(a) == 50
    assert (a["season"] == "2023-24").sum() == 15 and (a["season"] == "2024-25").sum() == 15
    older = a[a["era"] == "older"]
    assert len(older) == 20 and older["season"].between("2015-16", "2022-23").all()
    assert "2025-26" not in set(a["season"])                       # the confirmation season is never sampled
    assert list(lm.sample_games(_games(), seed=1)["game_id"]) != list(a["game_id"])


def _prov(name, priority, home_blocks, extra=None):
    p = {"provider": {"id": "1", "name": name, "priority": priority},
         "homeTeamOdds": {k: {"pointSpread": {"american": v}} for k, v in home_blocks.items()},
         "awayTeamOdds": {}}
    p.update(extra or {})
    return p


def test_recent_games_use_the_providers_own_open_and_close_blocks():
    items = [_prov("ESPN BET", 0, {"open": "-3.5", "close": "-5"})]
    s = lm.spreads(lm.trim_items({"items": items}))
    assert s["open_spread"] == -3.5 and s["close_spread"] == -5.0 and s["move"] == -1.5
    assert s["open_source"] == "ESPN BET:open" and s["close_point"] == "close"


def test_older_games_take_the_opening_provider_and_never_close_from_it():
    items = [_prov("Opening", 0, {"current": "+2"}), _prov("consensus", 1, {"current": "+1"}),
             _prov("Live Odds", 0, {"open": "+9", "close": "+9"})]
    s = lm.spreads(lm.trim_items({"items": items}))
    assert s["open_spread"] == 2.0 and s["open_source"] == "Opening:current"
    assert s["close_spread"] == 1.0 and s["move"] == -1.0               # live provider ignored


def test_missing_opening_means_no_move():
    s = lm.spreads(lm.trim_items({"items": [_prov("consensus", 0, {"current": "-4"})]}))
    assert s["open_spread"] is None and s["close_spread"] == -4.0 and s["move"] is None
    assert lm.spreads(lm.trim_items(None))["move"] is None


def test_open_and_close_come_from_the_same_book():
    items = [_prov("A", 0, {"current": "-6"}), _prov("B", 1, {"open": "-3", "close": "-4"})]
    s = lm.spreads(lm.trim_items({"items": items}))
    assert s["open_source"] == "B:open" and s["close_source"] == "B" and s["move"] == -1.0   # not -6 - (-3)


def test_a_close_that_is_only_an_open_block_is_no_close():
    items = [_prov("Opening", 0, {"open": "-3"}), _prov("A", 1, {"open": "-3"})]
    s = lm.spreads(lm.trim_items({"items": items}))
    assert s["close_spread"] is None and s["move"] is None


def test_pick_even_and_odd_priorities():
    items = [_prov("A", None, {"open": "PK", "close": "EVEN"}), _prov("B", "x", {"open": "-1", "close": "-2"})]
    s = lm.spreads(lm.trim_items({"items": items}))
    assert s["move"] in (0.0, -1.0)                                     # no crash on a missing / odd priority
    assert lm.spreads(lm.trim_items({"items": items[:1]}))["move"] == 0.0


def test_a_failed_fetch_is_recorded_and_counts_against_coverage(tmp_path):
    sample = lm.sample_games(_games()).head(2)
    recs = lm.fetch_sample(sample, tmp_path / "f.jsonl.gz", fetch=lambda gid: None, sleep=0)
    assert [r["fetched"] for r in recs] == [False, False] and all(r["move"] is None for r in recs)
    assert lm.summarize(recs)["eras"]["recent"]["open_coverage"] == 0.0


def _rec(era, move, fetched=True):
    return {"era": era, "fetched": fetched, "move": move, "open_source": "x:open" if move is not None else None}


def test_continue_needs_every_check_in_every_era():
    good = [_rec("recent", m) for m in (0, -1, 1, 0.5, -0.5, 0, 1.5, -2, 0, 1)]
    out = lm.summarize(good + [_rec("older", m) for m in (0, 1, -1, 2, 0, -0.5, 0.5, 0, 1, -1)])
    assert out["continue_to_full_fetch"] is True
    sparse = [_rec("older", None)] * 3 + [_rec("older", m) for m in (1, -1, 0.5, 0, 2, -2, 1)]   # 70% coverage
    out = lm.summarize(good + sparse)
    assert out["eras"]["older"]["checks"]["open_coverage"] is False and out["continue_to_full_fetch"] is False
    flat = [_rec("recent", 0.0)] * 10
    assert lm.summarize(flat)["eras"]["recent"]["checks"]["share_moved"] is False


def test_fetch_writes_one_gzip_line_per_game_with_the_trimmed_items(tmp_path):
    sample = lm.sample_games(_games()).head(3)
    calls = []

    def fake(gid):
        calls.append(gid)
        return {"items": [_prov("ESPN BET", 0, {"open": "-1", "close": "-2"}, {"links": ["big"]})]}
    out = tmp_path / "s.jsonl.gz"
    recs = lm.fetch_sample(sample, out, fetch=fake, sleep=0)
    assert calls == list(sample["game_id"]) and len(recs) == 3
    lines = [json.loads(x) for x in gzip.open(out, "rt", encoding="utf-8")]
    assert [x["move"] for x in lines] == [-1.0] * 3 and "links" not in lines[0]["items"][0]


def test_the_module_only_selects_and_only_gets():
    src = Path(lm.__file__).read_text(encoding="utf-8")
    sql = re.findall(r'"""/\* c2:.*?"""', src, re.S)
    assert len(sql) == 1 and not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b", sql[0], re.I)
    assert "ensure_schema" not in src and "connect_readonly" in src
    assert "urlopen" not in src and "requests" not in src             # network only through espn_http.odds (GET)
