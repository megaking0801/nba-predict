"""Taiwan lottery 21:00 prices against the international close (read-only: files only, no network, no DB writes).

Pre-registered test of docs/superpowers/specs/2026-10-29-tw-snapshot-design.md §7: each game's best Taiwan
spread/total option by EV at the 21:00 Taipei snapshot (21:00 information only), scored by its EV against the
closing reference price (CLV). The constants below are pre-registered and locked by tests; do not change them
once snapshots accumulate.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from statistics import NormalDist
from typing import List, Optional

import numpy as np
import pandas as pd

from jobs import tw_parse
from jobs.feature_ablation import block_bootstrap_ci
from jobs.tw_price import devig, ev, implied_mean, outcome_probs
from jobs.tz import et_date_of
from webui.build_site import connect_readonly, make_run

ANALYSIS_DATE_TPE = "2026-11-30"
BET_WINDOW_TPE = ("20:45", "21:30")
CLOSE_MAX_MIN = 60
DELAY_SPREAD_PTS = 1.0
DELAY_TOTAL_PTS = 1.5
MIN_BETS = 150
MIN_DAYS = 20
MIN_BETS_PER_WEEK = 3
LEVEL = 0.99
BLOCK_DAYS = 1
REPS = 10000
SEED = 0
SIGMAS_SPREAD = (11.0, 12.5, 14.0)
SIGMAS_TOTAL = (17.0, 19.0, 21.0)
SIGMA_SPREAD = 12.5
SIGMA_TOTAL = 19.0
NO_CONVERSION_PTS = 0.5

TPE = "Asia/Taipei"
MATCH_HOURS = 3.0
_REF_COLS = ["home", "away", "tipoff_utc", "spread_line", "p_home_cover", "ref_source", "total_line", "p_over",
             "ref_source_total"]
CAND_COLS = ["game", "et_date", "home", "away", "tipoff_utc", "stamp_21", "market", "line", "side", "dec", "m",
             "ev_21", "ev_close", "close_gap_min", "close_ref_line", "ref_source", "ref_source_21", "primary"]


@lru_cache(maxsize=None)
def _parsed(path: str, kind: str) -> pd.DataFrame:
    raw = Path(path).read_bytes()
    return {"tw": tw_parse.parse_tw, "espn": tw_parse.parse_espn, "oddsapi": tw_parse.parse_oddsapi}[kind](raw)


def _file(snaps: pd.DataFrame, stamp, kind: str) -> Optional[pd.DataFrame]:
    hit = snaps[(snaps["stamp_utc"] == stamp) & (snaps["kind"] == kind)]
    return _parsed(str(hit["path"].iloc[0]), kind) if len(hit) else None


def _two_way(o: pd.DataFrame, market: str, high: str, low: str) -> pd.DataFrame:
    """Per (game, book, line): de-vigged probability of the high side (home cover / over)."""
    d = o[o["market"] == market]
    keys = ["home", "away", "tipoff_utc", "book", "line"]
    m = d[d["side"] == high][keys + ["dec"]].merge(d[d["side"] == low][keys + ["dec"]], on=keys,
                                                   suffixes=("_hi", "_lo"))
    m["p"] = [devig(a, b) for a, b in zip(m["dec_hi"], m["dec_lo"])]
    return m


def _pick_ref(books: pd.DataFrame):
    """(line, p, source) from one game's Odds API books: Pinnacle, else the median over the books quoting the
    most-quoted line (ties: the lower line)."""
    pin = books[books["book"] == "pinnacle"]
    if len(pin):
        return float(pin["line"].iloc[0]), float(pin["p"].iloc[0]), "pinnacle"
    counts = books.groupby("line").size()
    line = float(counts[counts == counts.max()].index.min())
    return line, float(np.median(books.loc[books["line"] == line, "p"])), "median"


def reference(stamp_utc, snaps: pd.DataFrame) -> pd.DataFrame:
    """Reference prices per game at one snapshot time, each market by the priority Pinnacle -> median of the
    Odds API books -> ESPN DraftKings. Games are matched on (home, away) and tip times within 3 hours; the ESPN
    tip time wins."""
    games = []                                   # dicts with home, away, tipoff_utc and the chosen references
    e, o = _file(snaps, stamp_utc, "espn"), _file(snaps, stamp_utc, "oddsapi")

    def find(home, away, tip):
        for g in games:
            if g["home"] == home and g["away"] == away and abs(g["tipoff_utc"] - tip) <= pd.Timedelta(hours=MATCH_HOURS):
                return g
        g = {"home": home, "away": away, "tipoff_utc": tip}
        games.append(g)
        return g

    if e is not None:
        for r in e.itertuples(index=False):
            g = find(r.home, r.away, r.tipoff_utc)
            g["espn"] = r
    if o is not None and len(o):
        for market, high, low, prefix in (("spread", "home", "away", "s"), ("total", "over", "under", "t")):
            tw_ = _two_way(o, market, high, low)
            for (home, away, tip), books in tw_.groupby(["home", "away", "tipoff_utc"]):
                find(home, away, tip)[prefix] = _pick_ref(books)
    rows = []
    for g in games:
        r = g.get("espn")
        if r is not None:
            g["tipoff_utc"] = r.tipoff_utc
            if "s" not in g and pd.notna(r.spread_line) and r.spread_home_dec and r.spread_away_dec:
                g["s"] = (float(r.spread_line), devig(r.spread_home_dec, r.spread_away_dec), "draftkings")
            if "t" not in g and pd.notna(r.total_line) and r.over_dec and r.under_dec:
                g["t"] = (float(r.total_line), devig(r.over_dec, r.under_dec), "draftkings")
        s, t = g.get("s", (np.nan, np.nan, None)), g.get("t", (np.nan, np.nan, None))
        if s[2] is None and t[2] is None:
            continue
        rows.append({"home": g["home"], "away": g["away"], "tipoff_utc": g["tipoff_utc"], "spread_line": s[0],
                     "p_home_cover": s[1], "ref_source": s[2], "total_line": t[0], "p_over": t[1],
                     "ref_source_total": t[2]})
    return pd.DataFrame(rows, columns=_REF_COLS)


def _in_window(ts_utc: pd.Timestamp) -> bool:
    hm = ts_utc.tz_convert(TPE).strftime("%H:%M")
    return BET_WINDOW_TPE[0] <= hm <= BET_WINDOW_TPE[1]


def _all_refs(snaps: pd.DataFrame) -> pd.DataFrame:
    stamps = sorted(snaps.loc[snaps["kind"].isin(["espn", "oddsapi"]), "stamp_utc"].unique())
    parts = [reference(s, snaps).assign(stamp_utc=s) for s in stamps]
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=_REF_COLS + ["stamp_utc"])


def _option_ev(ref, market, line, side, dec, sigma):
    """EV of one Taiwan option against one reference row; NaN when that market has no reference. The moneyline
    (exploratory) is priced from the spread reference: home wins when the Normal(mu, sigma) margin is > 0."""
    spread_ref = market in ("spread", "ml")
    ref_line, p = (ref["spread_line"], ref["p_home_cover"]) if spread_ref else (ref["total_line"], ref["p_over"])
    if pd.isna(ref_line) or pd.isna(p):
        return np.nan, np.nan, None
    src = ref["ref_source"] if spread_ref else ref["ref_source_total"]
    if market == "ml":
        p_home = 1.0 - NormalDist(implied_mean(ref_line, p, sigma, "spread"), sigma).cdf(0.0)
        return ev(dec, p_home if side == "home" else 1.0 - p_home, 0.0), float(ref_line), src
    w, push, _ = outcome_probs(line, implied_mean(ref_line, p, sigma, market), sigma, market, side)
    return ev(dec, w, push), float(ref_line), src


def _prep(snaps: pd.DataFrame) -> pd.DataFrame:
    snaps = snaps.copy()
    snaps["stamp_utc"] = pd.to_datetime(snaps["stamp_utc"], utc=True)
    return snaps


def _slot_rows(snaps, refs, s_tw, sigma: dict, markets, ref_ok):
    """Rows for every option of `markets` in the Taiwan snapshot at s_tw, for the games of its ET day that have
    not started. The 'now' reference is the matching reference stamp accepted by ref_ok nearest to s_tw."""
    t = _file(snaps, s_tw, "tw")
    t = t[t["market"].isin(markets)]
    day = et_date_of(s_tw.to_pydatetime())
    rows, postponed, unpriced = [], [], []
    for (home, away, kick), opts in t.groupby(["home", "away", "kickoff_utc"]):
        if et_date_of(kick.to_pydatetime()) != day or kick <= s_tw:
            continue
        game = f"{day.isoformat()} {away}@{home}"
        mine = refs[(refs["home"] == home) & (refs["away"] == away)
                    & ((refs["tipoff_utc"] - kick).abs() <= pd.Timedelta(hours=MATCH_HOURS))]
        now = mine[mine["stamp_utc"].map(ref_ok)]
        if now.empty:
            unpriced.append(game)
            continue
        r_now = now.loc[(now["stamp_utc"] - s_tw).abs().idxmin()]
        if mine[mine["stamp_utc"] > r_now["stamp_utc"]].empty:
            postponed.append(game)
            continue
        tip = mine.sort_values("stamp_utc")["tipoff_utc"].iloc[-1]
        before = mine[mine["stamp_utc"] < tip].sort_values("stamp_utc")
        close = before.iloc[-1] if len(before) else None
        gap = (tip - close["stamp_utc"]).total_seconds() / 60.0 if close is not None else np.nan
        for o in opts.itertuples(index=False):
            e_now, _, src_now = _option_ev(r_now, o.market, o.line, o.side, o.dec, sigma[o.market])
            ec, cline, src = (_option_ev(close, o.market, o.line, o.side, o.dec, sigma[o.market])
                              if close is not None else (np.nan, np.nan, None))
            rows.append({"game": game, "et_date": day, "home": home, "away": away, "tipoff_utc": tip,
                         "stamp_21": s_tw, "market": o.market, "line": o.line, "side": o.side, "dec": o.dec,
                         "m": o.m, "ev_21": e_now, "ev_close": ec, "close_gap_min": gap, "close_ref_line": cline,
                         "ref_source": src, "ref_source_21": src_now,
                         "primary": bool(pd.notna(ec) and gap <= CLOSE_MAX_MIN)})
    return rows, postponed, unpriced


def _bet_stamps(snaps: pd.DataFrame) -> pd.Series:
    """The 21:00 Taiwan snapshot of each Taipei date: inside BET_WINDOW_TPE, the one closest to 21:00."""
    tws = snaps[(snaps["kind"] == "tw") & snaps["stamp_utc"].map(_in_window)].copy()
    if tws.empty:
        return pd.Series([], dtype="datetime64[ns, UTC]")
    tws["tpe_date"] = tws["stamp_utc"].map(lambda s: s.tz_convert(TPE).date())
    tws["off"] = tws["stamp_utc"].map(lambda s: abs((s.tz_convert(TPE) - s.tz_convert(TPE).normalize())
                                                    - pd.Timedelta(hours=21)))
    return tws.sort_values(["tpe_date", "off", "stamp_utc"]).drop_duplicates("tpe_date")["stamp_utc"]


def candidates(snaps: pd.DataFrame, sigma_spread: float, sigma_total: float, markets=("spread", "total"),
               refs: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """One row per Taiwan spread/total option of each game in the 21:00 Taipei snapshot of its ET game day.
    ev_21 uses the reference in the same 21:00 window; ev_close the last reference before tip. Games with no
    reference after the 21:00 snapshot (postponed, or every later slot missed) are dropped and listed in
    .attrs["postponed"]; games with no 21:00 reference are listed in .attrs["unpriced"]."""
    snaps = _prep(snaps)
    stamps = _bet_stamps(snaps)
    if refs is None:
        refs = _all_refs(snaps) if len(stamps) else pd.DataFrame(columns=_REF_COLS + ["stamp_utc"])
    rows, postponed, unpriced = [], [], []
    sigma = {"spread": sigma_spread, "total": sigma_total, "ml": sigma_spread}
    for s21 in stamps:
        d21 = s21.tz_convert(TPE).date()
        r, p, u = _slot_rows(snaps, refs, s21, sigma, markets,
                             lambda s: _in_window(s) and s.tz_convert(TPE).date() == d21)
        rows += r
        postponed += p
        unpriced += u
    out = pd.DataFrame(rows, columns=CAND_COLS)
    out["primary"] = out["primary"].astype(bool)          # an empty frame would otherwise index by column
    out.attrs.update(postponed=postponed, unpriced=unpriced)
    return out


def pick_bets(cands: pd.DataFrame) -> pd.DataFrame:
    """The highest-ev_21 option per game, kept only when ev_21 > 0 (ties: spread before total, lower line)."""
    c = cands.dropna(subset=["ev_21"])
    c = c.sort_values(["game", "ev_21", "market", "line", "side"], ascending=[True, False, True, True, True])
    best = c.drop_duplicates("game")
    return best[best["ev_21"] > 0].reset_index(drop=True)


# ----- verdict (spec §7) -----

PAIRS = list(zip(SIGMAS_SPREAD, SIGMAS_TOTAL))
MAIN_PAIR = (SIGMA_SPREAD, SIGMA_TOTAL)


def _key(pair) -> str:
    return f"{pair[0]}/{pair[1]}"


def _t_quantile(q: float, df: int) -> float:
    """Student t quantile by the Cornish-Fisher expansion around the normal quantile (no scipy; good to ~1e-3
    for df >= 10). Only used for the comparison t interval, never for the verdict."""
    z = NormalDist().inv_cdf(q)
    g = [(z ** 3 + z) / 4, (5 * z ** 5 + 16 * z ** 3 + 3 * z) / 96,
         (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z) / 384,
         (79 * z ** 9 + 776 * z ** 7 + 1482 * z ** 5 - 1920 * z ** 3 - 945 * z) / 92160]
    return z + sum(gi / df ** (i + 1) for i, gi in enumerate(g))


def _counts(bets: pd.DataFrame) -> dict:
    n = int(len(bets))
    if not n:
        return {"n_bets": 0, "n_days": 0, "bets_per_week": 0.0}
    dates = pd.to_datetime(pd.Series(bets["et_date"]))
    span_days = (dates.max() - dates.min()).days + 1
    return {"n_bets": n, "n_days": int(dates.nunique()), "bets_per_week": n / (span_days / 7.0)}


def _ci(bets: pd.DataFrame):
    if bets.empty:
        return np.nan, np.nan, np.nan
    d = pd.DataFrame({"date": pd.to_datetime(pd.Series(bets["et_date"])).to_numpy(),
                      "d": bets["ev_close"].to_numpy(dtype=float)})
    return block_bootstrap_ci(d, reps=REPS, seed=SEED, level=LEVEL, block_days=BLOCK_DAYS)


def verdict(bets: pd.DataFrame, by_sigma: dict) -> dict:
    """The pre-registered 'yes' rule on the primary bets (EV_close of each bet). Every failed condition is listed
    in reasons; passes is True only when there is none."""
    out = _counts(bets)
    mean, lo, hi = _ci(bets)
    by_day = bets.groupby("et_date")["ev_close"].mean() if len(bets) else pd.Series(dtype=float)
    if len(by_day) >= 2:
        half = _t_quantile(1 - (1 - LEVEL) / 2, len(by_day) - 1) * by_day.std(ddof=1) / np.sqrt(len(by_day))
        t_lo, t_hi = float(by_day.mean() - half), float(by_day.mean() + half)
    else:
        t_lo = t_hi = np.nan
    lows = {_key(p): _ci(b)[1] for p, b in by_sigma.items()}
    conv = bets[(bets["line"] - bets["close_ref_line"]).abs() <= NO_CONVERSION_PTS + 1e-9] if len(bets) else bets
    no_conv = float(conv["ev_close"].mean()) if len(conv) else np.nan
    reasons = []
    if out["n_bets"] < MIN_BETS:
        reasons.append(f"fewer than {MIN_BETS} bets ({out['n_bets']})")
    if out["n_days"] < MIN_DAYS:
        reasons.append(f"fewer than {MIN_DAYS} ET game days ({out['n_days']})")
    if out["bets_per_week"] < MIN_BETS_PER_WEEK:
        reasons.append(f"fewer than {MIN_BETS_PER_WEEK} bets per week ({out['bets_per_week']:.2f})")
    if not lo > 0:
        reasons.append(f"block bootstrap {LEVEL:.0%} lower bound not above 0 ({lo:.4f})")
    for k, v in lows.items():
        if not v > 0:
            reasons.append(f"sigma {k}: lower bound not above 0 ({v:.4f})")
    if not no_conv > 0:
        reasons.append(f"no-conversion subset (|Taiwan line - close line| <= {NO_CONVERSION_PTS}) mean not above 0"
                       f" ({no_conv:.4f})")
    out.update(mean_ev_close=mean, ci_lo=lo, ci_hi=hi, t_lo=t_lo, t_hi=t_hi, sigma_lows=lows,
               no_conversion_mean=no_conv, passes=not reasons, reasons=reasons)
    return out


# ----- controls and exploratory tables -----

def _summary(x: pd.Series) -> dict:
    x = pd.Series(x, dtype=float).dropna()
    return {"n": int(len(x)), "mean_ev_close": float(x.mean()) if len(x) else np.nan}


def controls(cands: pd.DataFrame) -> dict:
    """Sanity controls over the primary candidates: one random spread/total option per game (seed SEED), and
    both sides of the Taiwan main line (the most balanced prices; should sit near the ~-14% vig)."""
    c = cands[cands["primary"]].sort_values(["game", "market", "line", "side"])
    rng = np.random.default_rng(SEED)
    rand = [g["ev_close"].iloc[int(rng.integers(0, len(g)))] for _, g in c.groupby("game", sort=True)]
    main = []
    for _, g in c.groupby(["game", "market"], sort=True):
        hi, lo = ("home", "away") if g["market"].iloc[0] == "spread" else ("over", "under")
        two = g[g["side"] == hi][["line", "dec", "close_ref_line"]].merge(
            g[g["side"] == lo][["line", "dec"]], on="line", suffixes=("_hi", "_lo"))
        if two.empty:
            continue
        two["bal"] = (two["dec_hi"] - two["dec_lo"]).abs()
        two["dist"] = (two["line"] - two["close_ref_line"]).abs()
        line = two.sort_values(["bal", "dist", "line"])["line"].iloc[0]
        main += g.loc[g["line"] == line, "ev_close"].tolist()
    return {"random_pick": _summary(pd.Series(rand)), "main_line_both_sides": _summary(pd.Series(main))}


def _gap_bucket(g) -> str:
    if pd.isna(g):
        return "none"
    return "0-30" if g <= 30 else "30-60" if g <= 60 else "60-120" if g <= 120 else ">120"


def _book_lines(snaps, stamp) -> pd.DataFrame:
    """Pinnacle and DraftKings main lines per game and market at one stamp (DraftKings from ESPN, else the
    Odds API draftkings book)."""
    rows = []
    o, e = _file(snaps, stamp, "oddsapi"), _file(snaps, stamp, "espn")
    if o is not None:
        for book in ("pinnacle", "draftkings"):
            for market, side in (("spread", "home"), ("total", "over")):
                d = o[(o["book"] == book) & (o["market"] == market) & (o["side"] == side)]
                rows += [{"home": r.home, "away": r.away, "tip": r.tipoff_utc, "market": market, "book": book,
                          "line": r.line, "rank": 1} for r in d.itertuples(index=False)]
    if e is not None:
        for r in e.itertuples(index=False):
            rows += [{"home": r.home, "away": r.away, "tip": r.tipoff_utc, "market": m, "book": "draftkings",
                      "line": v, "rank": 0} for m, v in (("spread", r.spread_line), ("total", r.total_line))
                     if pd.notna(v)]
    d = pd.DataFrame(rows, columns=["home", "away", "tip", "market", "book", "line", "rank"])
    return d.sort_values("rank").drop_duplicates(["home", "away", "market", "book"])


def delay_events(snaps: pd.DataFrame) -> list:
    """Exploratory: consecutive stamps (both with a Taiwan file) where Pinnacle and DraftKings main lines both
    move the same way by >= DELAY_*_PTS while the Taiwan option nearest the earlier Pinnacle line keeps the same
    update time."""
    snaps = _prep(snaps)
    kinds = snaps.groupby("stamp_utc")["kind"].agg(set)
    stamps = sorted(s for s, k in kinds.items() if "tw" in k and k & {"oddsapi", "espn"})
    thr = {"spread": DELAY_SPREAD_PTS, "total": DELAY_TOTAL_PTS}
    out = []
    for s0, s1 in zip(stamps, stamps[1:]):
        b0, b1 = _book_lines(snaps, s0), _book_lines(snaps, s1)
        t0, t1 = _file(snaps, s0, "tw"), _file(snaps, s1, "tw")
        for (home, away, market), g0 in b0.groupby(["home", "away", "market"]):
            g1 = b1[(b1["home"] == home) & (b1["away"] == away) & (b1["market"] == market)]
            l0, l1 = g0.set_index("book")["line"], g1.set_index("book")["line"]
            if not {"pinnacle", "draftkings"} <= set(l0.index) & set(l1.index):
                continue
            mp, md = l1["pinnacle"] - l0["pinnacle"], l1["draftkings"] - l0["draftkings"]
            if min(abs(mp), abs(md)) < thr[market] or mp * md <= 0:
                continue
            tip = g0["tip"].iloc[0]

            def opts(t):
                return t[(t["home"] == home) & (t["away"] == away) & (t["market"] == market)
                         & ((t["kickoff_utc"] - tip).abs() <= pd.Timedelta(hours=MATCH_HOURS))]
            o0, o1 = opts(t0), opts(t1)
            if o0.empty:
                continue
            lines = sorted(o0["line"].unique(), key=lambda x: (abs(x - l0["pinnacle"]), x))
            line = lines[0]
            u0 = o0.loc[o0["line"] == line, "updated_utc"].iloc[0]
            u1 = o1.loc[o1["line"] == line, "updated_utc"]
            if len(u1) and u1.iloc[0] == u0:
                out.append({"game": f"{et_date_of(tip.to_pydatetime()).isoformat()} {away}@{home}",
                            "market": market, "from_utc": s0, "to_utc": s1, "pinnacle_move": float(mp),
                            "draftkings_move": float(md), "tw_line": float(line), "tw_updated_utc": u0})
    return out


def _other_slots(snaps, refs) -> dict:
    """Exploratory: every non-21:00 Taiwan snapshot, best option per game by EV against the same-stamp
    reference, grouped by Taipei hour."""
    sigma = {"spread": SIGMA_SPREAD, "total": SIGMA_TOTAL}
    by = {}
    for s in snaps.loc[(snaps["kind"] == "tw") & ~snaps["stamp_utc"].map(_in_window), "stamp_utc"]:
        rows, _, _ = _slot_rows(snaps, refs, s, sigma, ("spread", "total"), lambda r, s=s: r == s)
        if rows:
            by.setdefault(s.tz_convert(TPE).strftime("%H:00"), []).append(pd.DataFrame(rows))
    out = {}
    for hour, parts in sorted(by.items()):
        best = pick_bets(pd.concat(parts, ignore_index=True).assign(
            game=lambda d: d["game"] + " " + d["stamp_21"].astype(str)))
        out[hour] = {"n_positive_best": int(len(best)), "mean_ev_now": float(best["ev_21"].mean()) if len(best)
                     else np.nan, **_summary(best.loc[best["primary"], "ev_close"])}
    return out


# ----- secondary P&L (read-only DB) -----

_SCORES = """/* tw:SCORES */
SELECT g.game_date_et AS date, g.home_abbr, g.away_abbr, g.home_score, g.away_score
  FROM public.games_v2 g
 WHERE g.status = 'final' AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
   AND g.game_date_et = ANY(%s)
"""


def settle(bets: pd.DataFrame, scores: pd.DataFrame) -> dict:
    """Profit per unit of each bet from final scores (pushes refund). Spread lines are the home handicap."""
    s = scores.rename(columns={"date": "et_date", "home_abbr": "home", "away_abbr": "away"})
    s = s.assign(et_date=pd.to_datetime(s["et_date"]).dt.date)
    b = bets.assign(et_date=pd.to_datetime(pd.Series(bets["et_date"])).dt.date.to_numpy())
    m = b.merge(s[["et_date", "home", "away", "home_score", "away_score"]], on=["et_date", "home", "away"],
                how="left")
    done = m.dropna(subset=["home_score"])
    profit, pushes = [], 0
    for r in done.itertuples(index=False):
        if r.market == "spread":
            x = (r.home_score - r.away_score) + r.line
            x = x if r.side == "home" else -x
        else:
            x = (r.home_score + r.away_score) - r.line
            x = x if r.side == "over" else -x
        pushes += x == 0
        profit.append(0.0 if x == 0 else (r.dec - 1.0) if x > 0 else -1.0)
    p = np.array(profit, dtype=float)
    return {"n": int(len(p)), "n_unsettled": int(len(m) - len(done)), "n_push": int(pushes),
            "roi": float(p.mean()) if len(p) else np.nan, "profit": profit}


# ----- report and CLI -----

NOTES = [
    "「有」只代表值得進入預先登記的前瞻追蹤（先記帳、可小額），不代表已證實能獲利；要宣稱能獲利仍須達到 jobs.playbook.passes() 的標準。",
    "目前沒有 21:00 即時計算工具，結論無法直接照著下注。",
    "單注 EV 不等於串關 EV：m >= 2 的場次推測必須串關，另一關的抽水會吃掉優勢。",
]


def _clean(x):
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (float, np.floating)):
        return None if np.isnan(x) else float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (pd.Timestamp, dt.date)):
        return x.isoformat()
    return x


def report(root, as_of_tpe: str, conn=None) -> dict:
    """The analysis report on every snapshot up to the Taipei date as_of_tpe. Before ANALYSIS_DATE_TPE it is
    progress only: counts and coverage, no EV of the picks and no interval (spec §7)."""
    snaps = _prep(tw_parse.list_snapshots(root))
    snaps = snaps[snaps["stamp_utc"].dt.tz_convert(TPE).dt.date.astype(object) <= dt.date.fromisoformat(as_of_tpe)]
    progress_only = as_of_tpe < ANALYSIS_DATE_TPE
    refs = _all_refs(snaps)
    cands = {p: candidates(snaps, *p, refs=refs) for p in PAIRS}
    picks = {p: pick_bets(c) for p, c in cands.items()}
    primary = {p: b[b["primary"]] for p, b in picks.items()}
    main_c, main_b = cands[MAIN_PAIR], picks[MAIN_PAIR]
    tw_dates = set(snaps.loc[snaps["kind"] == "tw", "stamp_utc"].map(lambda s: s.tz_convert(TPE).date()))
    bet_dates = {s.tz_convert(TPE).date() for s in _bet_stamps(snaps)}
    games = [{"game": g, "reason": "no reference after the 21:00 snapshot (postponed, or every later slot missed)"}
             for g in main_c.attrs["postponed"]]
    games += [{"game": g, "reason": "no reference in the 21:00 window"} for g in main_c.attrs["unpriced"]]
    games += [{"game": r.game, "reason": "no reference before tip" if pd.isna(r.close_gap_min)
               else f"close reference {r.close_gap_min:.0f} min before tip (> {CLOSE_MAX_MIN})"}
              for r in main_b[~main_b["primary"]].itertuples(index=False)]
    out = {
        "as_of_tpe": as_of_tpe, "analysis_date_tpe": ANALYSIS_DATE_TPE, "progress_only": progress_only,
        "coverage": {"n_tw_snapshots": int((snaps["kind"] == "tw").sum()),
                     "n_ref_snapshots": int(snaps.loc[snaps["kind"] != "tw", "stamp_utc"].nunique()),
                     "n_bet_days": len(bet_dates), "n_candidate_games": int(main_c["game"].nunique())},
        "excluded": {"games": games, "days_without_21": sorted(d.isoformat() for d in tw_dates - bet_dates)},
        "controls": controls(main_c),
        "delay_events": delay_events(snaps),
        "notes": NOTES,
    }
    if progress_only:
        out.update(verdict=_counts(primary[MAIN_PAIR]), by_close_gap=None, by_m=None, other_slots=None,
                   moneyline=None, bets=None)
        return _clean(out)
    b = primary[MAIN_PAIR]
    ml = pick_bets(candidates(snaps, *MAIN_PAIR, markets=("ml",), refs=refs))
    out.update(
        verdict=verdict(b, primary),
        by_close_gap={k: _summary(g["ev_close"]) for k, g in
                      main_b.assign(bucket=main_b["close_gap_min"].map(_gap_bucket)).groupby("bucket")},
        by_m={"m=1": _summary(b.loc[b["m"] == 1, "ev_close"]), "m>=2": _summary(b.loc[b["m"] >= 2, "ev_close"])},
        other_slots=_other_slots(snaps, refs),
        moneyline=_summary(ml.loc[ml["primary"], "ev_close"]),
        bets=b[["game", "market", "line", "side", "dec", "m", "ev_21", "ev_close", "close_gap_min",
                "close_ref_line", "ref_source", "ref_source_21"]].to_dict("records"),
    )
    if conn is not None:
        dates = sorted({d for d in b["et_date"]})
        scores = make_run(conn)(_SCORES, (dates,)) if dates else pd.DataFrame(
            columns=["date", "home_abbr", "away_abbr", "home_score", "away_score"])
        out["results"] = {k: v for k, v in settle(b, scores).items() if k != "profit"}
    return _clean(out)


def _scrub(text: str) -> str:
    return re.sub(r"postgres(?:ql)?://\S+", "[redacted]", text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobs.tw_compare",
                                 description="Taiwan 21:00 prices against the international close (read-only)")
    ap.add_argument("--root", default="data/tw_snapshots")
    ap.add_argument("--out", required=True)
    ap.add_argument("--as-of", dest="as_of", default=pd.Timestamp.now(tz=TPE).date().isoformat(),
                    help="Taipei date YYYY-MM-DD (default: today)")
    ap.add_argument("--with-results", action="store_true", help="add the secondary P&L from a read-only DB read")
    args = ap.parse_args(argv)
    conn = None
    if args.with_results:
        try:
            conn = connect_readonly()
        except Exception as exc:  # the message may carry a connection string: print the class only
            print(f"DB connection failed ({type(exc).__name__}); check the DB credentials.")
            return 2
    try:
        r = report(Path(args.root), args.as_of, conn=conn)
    except Exception as exc:
        print(f"tw_compare failed: {type(exc).__name__}: {_scrub(str(exc))}")
        return 2
    finally:
        if conn is not None:
            conn.close()
    Path(args.out).write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    v = r["verdict"]
    print(f"as of {r['as_of_tpe']} (analysis {r['analysis_date_tpe']}): {v['n_bets']} primary bets over "
          f"{v['n_days']} ET days, {v['bets_per_week']:.1f}/week; {r['coverage']['n_tw_snapshots']} Taiwan snapshots")
    if r["progress_only"]:
        print("progress only: no EV or interval before the analysis date")
    else:
        print(f"passes={v['passes']} mean EV_close={v['mean_ev_close']} 99% CI [{v['ci_lo']}, {v['ci_hi']}]")
        for reason in v["reasons"]:
            print(f"  - {reason}")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
