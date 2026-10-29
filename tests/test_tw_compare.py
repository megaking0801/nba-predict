"""jobs.tw_compare on synthetic snapshot directories: reference priority, the 21:00 window, the close, the pick."""
import datetime as dt
import json
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from jobs import tw_compare as tc
from jobs import tw_price as pr

TIP = "2026-10-30T00:00:00Z"                 # 08:00 Taipei on 10-30, ET game day 2026-10-29


def _p(dec):
    return {"pu": f"{(dec - 1) * 100:.0f}", "pd": "100"}


def tw(spreads=((-5.5, 1.72, 1.72),), total=(230.5, 1.72, 1.72), kt="2026-10-30T08:00:00+08:00", m="2",
       ss="2026-10-29T20:30:00+08:00", ml=None):
    ms = [{"name": "不讓分", "ss": ss, "m": m, "cs": [{"name": "沙加緬度國王", "hv": "", **_p(ml[1])},
                                                    {"name": "紐奧良鵜鶘", "hv": "", **_p(ml[0])}]}] if ml else []
    ms += [{"name": f"[總分]讓分 {hv:+g}", "ss": ss, "m": m, "cs": [
        {"name": f"沙加緬度國王 {-hv:+g}", "hv": str(hv), **_p(a)},
        {"name": f"紐奧良鵜鶘 {hv:+g}", "hv": str(hv), **_p(h)}]} for hv, h, a in spreads]
    if total:
        line, o, u = total
        ms.append({"name": f"[總分]大小 {line:g}", "ss": ss, "m": m, "cs": [
            {"name": f"大 {line:g}", "hv": str(line), **_p(o)}, {"name": f"小 {line:g}", "hv": str(line), **_p(u)}]})
    return [{"id": "g1", "no": "1", "an": "沙加緬度國王", "hn": "紐奧良鵜鶘", "kt": kt, "tn": "美國職籃",
             "er": "sr:match:1", "ms": ms}]


def espn(spread=-5.5, sp=(-110, -110), total=230.5, tp=(-110, -110), date="2026-10-30T00:00Z"):
    def side(line, odds):
        return {"close": {"line": line, "odds": f"{odds:+d}"}, "open": {"line": line, "odds": f"{odds:+d}"}}
    odds = {"provider": {"name": "Draft Kings"},
            "pointSpread": {"home": side(f"{spread:+g}", sp[0]), "away": side(f"{-spread:+g}", sp[1])},
            "total": {"over": side(f"o{total:g}", tp[0]), "under": side(f"u{total:g}", tp[1])}}
    comp = {"competitors": [{"homeAway": "home", "team": {"abbreviation": "NO"}},
                            {"homeAway": "away", "team": {"abbreviation": "SAC"}}], "odds": [odds]}
    return {"events": [{"date": date, "competitions": [comp]}]}


def oddsapi(books, commence=TIP):
    bms = [{"key": k, "markets": [
        {"key": "spreads", "outcomes": [{"name": "New Orleans Pelicans", "price": h, "point": sl},
                                        {"name": "Sacramento Kings", "price": a, "point": -sl}]},
        {"key": "totals", "outcomes": [{"name": "Over", "price": o, "point": tl},
                                       {"name": "Under", "price": u, "point": tl}]}]}
        for k, (sl, h, a, tl, o, u) in books.items()]
    return [{"home_team": "New Orleans Pelicans", "away_team": "Sacramento Kings", "commence_time": commence,
             "bookmakers": bms}]


def put(root, stamp, kind, obj):
    day = root / stamp[:8]
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{stamp}+0800-{kind}.json").write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def snaps(root):
    return tc.tw_parse.list_snapshots(root)


def even(line, tline=230.5):
    return {"pinnacle": (line, 1.95, 1.95, tline, 1.95, 1.95)}


def test_constants_are_preregistered():
    assert tc.ANALYSIS_DATE_TPE == "2026-11-30"
    assert tc.BET_WINDOW_TPE == ("20:45", "21:30")
    assert tc.CLOSE_MAX_MIN == 60
    assert tc.DELAY_SPREAD_PTS == 1.0 and tc.DELAY_TOTAL_PTS == 1.5
    assert tc.MIN_BETS == 150 and tc.MIN_DAYS == 20 and tc.MIN_BETS_PER_WEEK == 3
    assert tc.LEVEL == 0.99 and tc.BLOCK_DAYS == 1 and tc.REPS == 10000 and tc.SEED == 0
    assert tc.SIGMAS_SPREAD == (11.0, 12.5, 14.0) and tc.SIGMA_SPREAD == 12.5
    assert tc.SIGMAS_TOTAL == (17.0, 19.0, 21.0) and tc.SIGMA_TOTAL == 19.0
    assert tc.NO_CONVERSION_PTS == 0.5


def test_reference_prefers_pinnacle_then_median_then_dk(tmp_path):
    put(tmp_path, "20261029T2100", "espn", espn(spread=-6.5, sp=(-120, +100)))
    put(tmp_path, "20261029T2100", "oddsapi", oddsapi({
        "pinnacle": (-5.5, 1.90, 2.00, 230.5, 1.95, 1.95),
        "draftkings": (-5.5, 1.80, 2.00, 230.5, 1.90, 1.90),
        "fanduel": (-5.5, 1.85, 1.95, 230.5, 1.90, 1.90)}))
    stamp = pd.Timestamp("2026-10-29 13:00", tz="UTC")
    r = tc.reference(stamp, snaps(tmp_path)).iloc[0]
    assert (r["home"], r["away"], r["ref_source"], r["ref_source_total"]) == ("NOP", "SAC", "pinnacle", "pinnacle")
    assert r["spread_line"] == -5.5 and r["p_home_cover"] == pytest.approx(pr.devig(1.90, 2.00))
    assert r["tipoff_utc"] == pd.Timestamp(TIP)

    put(tmp_path, "20261029T2200", "espn", espn(spread=-6.5, sp=(-120, +100)))
    put(tmp_path, "20261029T2200", "oddsapi", oddsapi({
        "draftkings": (-5.5, 1.80, 2.00, 230.5, 1.90, 1.90),
        "fanduel": (-5.5, 1.85, 1.95, 230.5, 1.90, 1.90),
        "betmgm": (-5.5, 1.90, 1.90, 230.5, 1.90, 1.90)}))
    r = tc.reference(pd.Timestamp("2026-10-29 14:00", tz="UTC"), snaps(tmp_path)).iloc[0]
    assert r["ref_source"] == "median"
    assert r["p_home_cover"] == pytest.approx(pr.devig(1.85, 1.95))

    put(tmp_path, "20261029T2300", "espn", espn(spread=-6.5, sp=(-120, +100)))
    r = tc.reference(pd.Timestamp("2026-10-29 15:00", tz="UTC"), snaps(tmp_path)).iloc[0]
    assert r["ref_source"] == "draftkings" and r["spread_line"] == -6.5
    assert r["p_home_cover"] == pytest.approx(pr.devig(1 + 100 / 120, 2.0))


def test_reference_median_uses_the_most_quoted_line(tmp_path):
    put(tmp_path, "20261029T2100", "oddsapi", oddsapi({
        "draftkings": (-5.5, 1.80, 2.00, 230.5, 1.90, 1.90),
        "fanduel": (-5.5, 1.90, 1.90, 230.5, 1.90, 1.90),
        "betmgm": (-6.5, 2.10, 1.70, 230.5, 1.90, 1.90)}))
    r = tc.reference(pd.Timestamp("2026-10-29 13:00", tz="UTC"), snaps(tmp_path)).iloc[0]
    assert r["spread_line"] == -5.5
    assert r["p_home_cover"] == pytest.approx((pr.devig(1.80, 2.00) + pr.devig(1.90, 1.90)) / 2)


def _day(root, tw_stamp="20261029T2100", closes=(("20261030T0740", -5.5),)):
    put(root, tw_stamp, "tw", tw())
    put(root, tw_stamp, "oddsapi", oddsapi(even(-5.5)))
    for stamp, line in closes:
        put(root, stamp, "oddsapi", oddsapi(even(line)))


def test_bet_window(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _day(a, "20261029T2044")
    _day(b, "20261029T2130")
    _day(c, "20261030T0600")
    assert tc.candidates(snaps(a), 12.5, 19.0).empty
    assert len(tc.candidates(snaps(b), 12.5, 19.0)) == 4             # one spread line and one total, two sides each
    assert tc.candidates(snaps(c), 12.5, 19.0).empty


def test_close_is_last_reference_before_tip_within_60_min(tmp_path):
    _day(tmp_path, closes=(("20261030T0630", -6.5), ("20261030T0740", -4.5)))
    c = tc.candidates(snaps(tmp_path), 12.5, 19.0)
    s = c[c["market"] == "spread"]
    assert (s["close_gap_min"] == 20).all() and (s["close_ref_line"] == -4.5).all() and s["primary"].all()
    home = s[s["side"] == "home"].iloc[0]
    mu = pr.implied_mean(-4.5, 0.5, 12.5, "spread")
    w, push, _ = pr.outcome_probs(-5.5, mu, 12.5, "spread", "home")
    assert home["ev_close"] == pytest.approx(pr.ev(1.72, w, push))

    only90 = tmp_path / "only90"
    _day(only90, closes=(("20261030T0630", -6.5),))
    c = tc.candidates(snaps(only90), 12.5, 19.0)
    assert (c["close_gap_min"] == 90).all() and not c["primary"].any()


def test_pick_uses_only_21_info(tmp_path):
    put(tmp_path, "20261029T2100", "tw", tw(spreads=((-5.5, 1.95, 1.80),), total=(230.5, 1.80, 1.80)))
    put(tmp_path, "20261029T2100", "oddsapi", oddsapi(even(-7.5, 230.5)))
    put(tmp_path, "20261030T0740", "oddsapi", oddsapi(even(-3.5, 220.5)))
    c = tc.candidates(snaps(tmp_path), 12.5, 19.0)
    assert c.loc[c["ev_close"].idxmax(), ["market", "side"]].tolist() == ["total", "under"]
    b = tc.pick_bets(c)
    assert len(b) == 1 and b.iloc[0][["market", "side"]].tolist() == ["spread", "home"]
    assert b.iloc[0]["ev_21"] > 0 > b.iloc[0]["ev_close"]


def test_pick_skips_games_without_positive_ev(tmp_path):
    _day(tmp_path)
    c = tc.candidates(snaps(tmp_path), 12.5, 19.0)
    assert (c["ev_21"] < 0).all() and tc.pick_bets(c).empty


def test_et_date_from_tipoff(tmp_path):
    _day(tmp_path)
    c = tc.candidates(snaps(tmp_path), 12.5, 19.0)
    assert (c["et_date"] == dt.date(2026, 10, 29)).all()
    assert (c["tipoff_utc"] == pd.Timestamp(TIP)).all()


def test_game_without_later_reference_is_dropped_and_listed(tmp_path):
    _day(tmp_path, closes=())
    c = tc.candidates(snaps(tmp_path), 12.5, 19.0)
    assert c.empty and c.attrs["postponed"] == ["2026-10-29 SAC@NOP"]


def test_espn_tip_time_wins(tmp_path):
    _day(tmp_path)
    put(tmp_path, "20261030T0740", "espn", espn(date="2026-10-30T00:30Z"))
    c = tc.candidates(snaps(tmp_path), 12.5, 19.0)
    assert (c["tipoff_utc"] == pd.Timestamp("2026-10-30 00:30", tz="UTC")).all()
    assert (c["close_gap_min"] == 50).all()


# ----- Task 5: verdict, controls, report, CLI -----

PAIRS = [(11.0, 17.0), (12.5, 19.0), (14.0, 21.0)]


def bets_frame(n, days, mean, sd=0.02, span_days=None, conv=0.0, seed=1):
    rng = np.random.default_rng(seed)
    start = dt.date(2026, 10, 29)
    span = span_days or days
    day_offsets = np.linspace(0, span - 1, days).round().astype(int)
    dates = [start + dt.timedelta(days=int(day_offsets[i % days])) for i in range(n)]
    return pd.DataFrame({"et_date": dates, "ev_close": rng.normal(mean, sd, n), "market": "spread",
                         "line": -5.5 + conv, "close_ref_line": -5.5, "m": 2})


def by_sigma_of(b):
    return {p: b for p in PAIRS}


def test_verdict_passes_on_clear_edge():
    b = bets_frame(200, 40, 0.05)
    v = tc.verdict(b, by_sigma_of(b))
    assert v["passes"] is True and v["reasons"] == []
    assert v["n_bets"] == 200 and v["n_days"] == 40 and v["bets_per_week"] == pytest.approx(200 / (40 / 7))
    assert 0 < v["ci_lo"] < v["mean_ev_close"] < v["ci_hi"] and v["t_lo"] > 0
    assert set(v["sigma_lows"]) == {"11.0/17.0", "12.5/19.0", "14.0/21.0"}
    assert v["no_conversion_mean"] == pytest.approx(b["ev_close"].mean())


def test_verdict_fails_below_150_bets():
    b = bets_frame(149, 40, 0.05)
    v = tc.verdict(b, by_sigma_of(b))
    assert v["passes"] is False and any("150" in r for r in v["reasons"])


def test_verdict_fails_below_3_per_week():
    b = bets_frame(150, 25, 0.05, span_days=401)
    v = tc.verdict(b, by_sigma_of(b))
    assert v["passes"] is False and any("per week" in r for r in v["reasons"])


def test_verdict_fails_below_20_days():
    b = bets_frame(200, 19, 0.05)
    v = tc.verdict(b, by_sigma_of(b))
    assert v["passes"] is False and any("20" in r for r in v["reasons"])


def test_verdict_requires_every_sigma_low_above_zero():
    b = bets_frame(200, 40, 0.05)
    bs = by_sigma_of(b)
    bs[(14.0, 21.0)] = bets_frame(200, 40, -0.05)
    v = tc.verdict(b, bs)
    assert v["passes"] is False and v["sigma_lows"]["14.0/21.0"] < 0
    assert any("14.0/21.0" in r for r in v["reasons"])


def test_verdict_requires_no_conversion_subset_positive():
    good = bets_frame(200, 40, 0.10, conv=1.0)
    bad = bets_frame(50, 40, -0.01, sd=0.001, seed=2)
    b = pd.concat([good, bad], ignore_index=True)
    v = tc.verdict(b, by_sigma_of(b))
    assert v["no_conversion_mean"] < 0 and v["passes"] is False
    assert any("no-conversion" in r for r in v["reasons"])


def test_verdict_on_no_bets():
    b = bets_frame(0, 1, 0.0)
    v = tc.verdict(b, by_sigma_of(b))
    assert v["passes"] is False and v["n_bets"] == 0


def test_t_quantile():
    assert tc._t_quantile(0.995, 20) == pytest.approx(2.845, abs=2e-3)
    assert tc._t_quantile(0.995, 5) == pytest.approx(4.032, abs=3e-2)


def test_main_line_control_is_near_minus_vig(tmp_path):
    _day(tmp_path)
    c = tc.candidates(snaps(tmp_path), 12.5, 19.0)
    ctl = tc.controls(c)
    assert ctl["main_line_both_sides"]["mean_ev_close"] == pytest.approx(1.72 * 0.5 - 1, abs=1e-9)
    assert ctl["main_line_both_sides"]["n"] == 4 and ctl["random_pick"]["n"] == 1


def test_main_line_is_the_most_balanced_taiwan_line(tmp_path):
    put(tmp_path, "20261029T2100", "tw", tw(spreads=((-6.5, 1.62, 1.82), (-5.5, 1.72, 1.72), (-4.5, 1.82, 1.62))))
    put(tmp_path, "20261029T2100", "oddsapi", oddsapi(even(-5.5)))
    put(tmp_path, "20261030T0740", "oddsapi", oddsapi(even(-5.5)))
    ctl = tc.controls(tc.candidates(snaps(tmp_path), 12.5, 19.0))
    assert ctl["main_line_both_sides"]["mean_ev_close"] == pytest.approx(-0.14, abs=1e-9)


def test_report_is_progress_only_before_analysis_date(tmp_path):
    _day(tmp_path)
    r = tc.report(tmp_path, "2026-11-15")
    assert r["progress_only"] is True
    assert "ci_lo" not in r["verdict"] and "mean_ev_close" not in r["verdict"]
    assert r["verdict"]["n_bets"] == 0
    assert r["by_m"] is None and r["by_close_gap"] is None
    assert len(r["notes"]) == 3


def test_report_full_on_analysis_date(tmp_path):
    put(tmp_path, "20261029T2100", "tw", tw(spreads=((-5.5, 1.95, 1.80),), total=(230.5, 1.80, 1.80)))
    put(tmp_path, "20261029T2100", "oddsapi", oddsapi(even(-7.5, 230.5)))
    put(tmp_path, "20261030T0740", "oddsapi", oddsapi(even(-7.5, 230.5)))
    put(tmp_path, "20261030T1500", "tw", tw())                 # a later Taipei date without a 21:00 file
    r = tc.report(tmp_path, tc.ANALYSIS_DATE_TPE)
    v = r["verdict"]
    assert r["progress_only"] is False and v["n_bets"] == 1 and v["passes"] is False
    assert r["by_m"]["m>=2"]["n"] == 1 and r["by_m"]["m=1"]["n"] == 0
    assert r["by_close_gap"]["0-30"]["n"] == 1
    assert r["excluded"]["days_without_21"] == ["2026-10-30"]
    json.dumps(r, default=str)


def test_report_ignores_snapshots_after_as_of(tmp_path):
    _day(tmp_path)
    r = tc.report(tmp_path, "2026-10-28")
    assert r["coverage"]["n_tw_snapshots"] == 0


def test_delay_event_needs_both_books_to_move_and_taiwan_to_stand_still(tmp_path):
    def stamp(root, s, pin, dk, ss="2026-10-29T20:30:00+08:00"):
        put(root, s, "tw", tw(ss=ss))
        put(root, s, "oddsapi", oddsapi(even(pin)))
        put(root, s, "espn", espn(spread=dk))
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    stamp(a, "20261030T0630", -5.5, -5.5)
    stamp(a, "20261030T0730", -7.0, -7.0)
    ev_ = tc.delay_events(snaps(a))
    assert len(ev_) == 1 and ev_[0]["market"] == "spread" and ev_[0]["tw_line"] == -5.5
    stamp(b, "20261030T0630", -5.5, -5.5)
    stamp(b, "20261030T0730", -7.0, -6.0)                      # DraftKings moved only 0.5
    assert tc.delay_events(snaps(b)) == []
    stamp(c, "20261030T0630", -5.5, -5.5)
    stamp(c, "20261030T0730", -7.0, -7.0, ss="2026-10-30T07:20:00+08:00")   # Taiwan updated
    assert tc.delay_events(snaps(c)) == []


def test_moneyline_is_priced_from_the_spread_reference(tmp_path):
    put(tmp_path, "20261029T2100", "tw", tw(ml=(1.50, 2.70)))
    put(tmp_path, "20261029T2100", "oddsapi", oddsapi(even(-5.5)))
    put(tmp_path, "20261030T0740", "oddsapi", oddsapi(even(-5.5)))
    ml = tc.candidates(snaps(tmp_path), 12.5, 19.0, markets=("ml",))
    home = ml[ml["side"] == "home"].iloc[0]
    p_home = 1 - NormalDist(5.5, 12.5).cdf(0)
    assert home["ev_21"] == pytest.approx(1.50 * p_home - 1)


def test_settle_bets():
    b = pd.DataFrame({"et_date": [dt.date(2026, 10, 29)] * 4, "home": "NOP", "away": "SAC",
                      "market": ["spread", "spread", "total", "total"], "line": [-5.5, -5.0, 230.5, 230.5],
                      "side": ["home", "away", "over", "under"], "dec": [1.72] * 4})
    scores = pd.DataFrame({"date": [dt.date(2026, 10, 29)], "home_abbr": ["NOP"], "away_abbr": ["SAC"],
                           "home_score": [120], "away_score": [115]})
    s = tc.settle(b, scores)
    assert s["n"] == 4 and s["n_push"] == 1
    assert s["profit"] == pytest.approx([-1.0, 0.0, 0.72, -1.0])     # home wins by 5: -5.5 loses, +5 pushes


def test_module_is_read_only():
    for mod in ("tw_compare", "tw_parse", "tw_price"):
        src = (Path(tc.__file__).parent / f"{mod}.py").read_text(encoding="utf-8")
        for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "commit()", "executemany", "ensure_schema"):
            assert word not in src, (mod, word)


def test_no_network_imports():
    for mod in ("tw_compare", "tw_parse", "tw_price"):
        src = (Path(tc.__file__).parent / f"{mod}.py").read_text(encoding="utf-8")
        for word in ("requests", "urllib.request", "jobs.espn_http"):
            assert word not in src, (mod, word)


def test_cli_error_does_not_leak_a_dsn(monkeypatch, capsys, tmp_path):
    def boom():
        raise RuntimeError("postgresql://user:secret@host/db failed")
    monkeypatch.setattr(tc, "connect_readonly", boom)
    assert tc.main(["--root", str(tmp_path), "--out", str(tmp_path / "r.json"), "--with-results"]) == 2
    assert "secret" not in capsys.readouterr().out


def test_cli_writes_the_report(tmp_path, capsys):
    _day(tmp_path / "snaps")
    out = tmp_path / "r.json"
    assert tc.main(["--root", str(tmp_path / "snaps"), "--out", str(out), "--as-of", "2026-11-01"]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["progress_only"] is True


def test_report_on_an_empty_root(tmp_path):
    r = tc.report(tmp_path, tc.ANALYSIS_DATE_TPE)
    assert r["verdict"]["n_bets"] == 0 and r["verdict"]["passes"] is False
    assert r["coverage"]["n_tw_snapshots"] == 0
