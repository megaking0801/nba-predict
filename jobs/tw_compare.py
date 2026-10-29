"""Taiwan lottery 21:00 prices against the international close (read-only: files only, no network, no DB writes).

Pre-registered test of docs/superpowers/specs/2026-10-29-tw-snapshot-design.md §7: each game's best Taiwan
spread/total option by EV at the 21:00 Taipei snapshot (21:00 information only), scored by its EV against the
closing reference price (CLV). The constants below are pre-registered and locked by tests; do not change them
once snapshots accumulate.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from jobs import tw_parse
from jobs.tw_price import devig, ev, implied_mean, outcome_probs
from jobs.tz import et_date_of

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
    """EV of one Taiwan option against one reference row; NaN when that market has no reference."""
    ref_line, p = (ref["spread_line"], ref["p_home_cover"]) if market == "spread" else (ref["total_line"], ref["p_over"])
    if pd.isna(ref_line) or pd.isna(p):
        return np.nan, np.nan, None
    w, push, _ = outcome_probs(line, implied_mean(ref_line, p, sigma, market), sigma, market, side)
    src = ref["ref_source"] if market == "spread" else ref["ref_source_total"]
    return ev(dec, w, push), float(ref_line), src


def candidates(snaps: pd.DataFrame, sigma_spread: float, sigma_total: float) -> pd.DataFrame:
    """One row per Taiwan spread/total option of each game in the 21:00 Taipei snapshot of its ET game day.
    ev_21 uses the reference in the same 21:00 window; ev_close the last reference before tip. Games with no
    reference after the 21:00 snapshot (postponed, or every later slot missed) are dropped and listed in
    .attrs["postponed"]; games with no 21:00 reference are listed in .attrs["unpriced"]."""
    snaps = snaps.copy()
    snaps["stamp_utc"] = pd.to_datetime(snaps["stamp_utc"], utc=True)
    tws = snaps[(snaps["kind"] == "tw") & snaps["stamp_utc"].map(_in_window)].copy()
    tws["tpe_date"] = tws["stamp_utc"].map(lambda s: s.tz_convert(TPE).date())
    tws["off"] = tws["stamp_utc"].map(lambda s: abs((s.tz_convert(TPE) - s.tz_convert(TPE).normalize())
                                                    - pd.Timedelta(hours=21)))
    tws = tws.sort_values(["tpe_date", "off", "stamp_utc"]).drop_duplicates("tpe_date")
    refs = _all_refs(snaps) if len(tws) else pd.DataFrame(columns=_REF_COLS + ["stamp_utc"])
    rows, postponed, unpriced = [], [], []
    sigma = {"spread": sigma_spread, "total": sigma_total}
    for s21 in tws["stamp_utc"]:
        t = _file(snaps, s21, "tw")
        t = t[t["market"].isin(["spread", "total"])]
        day = et_date_of(s21.to_pydatetime())
        for (home, away, kick), opts in t.groupby(["home", "away", "kickoff_utc"]):
            if et_date_of(kick.to_pydatetime()) != day or kick <= s21:
                continue
            game = f"{day.isoformat()} {away}@{home}"
            mine = refs[(refs["home"] == home) & (refs["away"] == away)
                        & ((refs["tipoff_utc"] - kick).abs() <= pd.Timedelta(hours=MATCH_HOURS))]
            at21 = mine[mine["stamp_utc"].map(lambda s: _in_window(s) and s.tz_convert(TPE).date()
                                              == s21.tz_convert(TPE).date())]
            if at21.empty:
                unpriced.append(game)
                continue
            r21 = at21.loc[(at21["stamp_utc"] - s21).abs().idxmin()]
            later = mine[mine["stamp_utc"] > r21["stamp_utc"]]
            if later.empty:
                postponed.append(game)
                continue
            tip = mine.sort_values("stamp_utc")["tipoff_utc"].iloc[-1]
            before = mine[mine["stamp_utc"] < tip].sort_values("stamp_utc")
            close = before.iloc[-1] if len(before) else None
            gap = (tip - close["stamp_utc"]).total_seconds() / 60.0 if close is not None else np.nan
            for o in opts.itertuples(index=False):
                e21, _, src21 = _option_ev(r21, o.market, o.line, o.side, o.dec, sigma[o.market])
                ec, cline, src = (_option_ev(close, o.market, o.line, o.side, o.dec, sigma[o.market])
                                  if close is not None else (np.nan, np.nan, None))
                rows.append({"game": game, "et_date": day, "home": home, "away": away, "tipoff_utc": tip,
                             "stamp_21": s21, "market": o.market, "line": o.line, "side": o.side, "dec": o.dec,
                             "m": o.m, "ev_21": e21, "ev_close": ec, "close_gap_min": gap, "close_ref_line": cline,
                             "ref_source": src, "ref_source_21": src21,
                             "primary": bool(pd.notna(ec) and gap <= CLOSE_MAX_MIN)})
    out = pd.DataFrame(rows, columns=CAND_COLS)
    out.attrs.update(postponed=postponed, unpriced=unpriced)
    return out


def pick_bets(cands: pd.DataFrame) -> pd.DataFrame:
    """The highest-ev_21 option per game, kept only when ev_21 > 0 (ties: spread before total, lower line)."""
    c = cands.dropna(subset=["ev_21"])
    c = c.sort_values(["game", "ev_21", "market", "line", "side"], ascending=[True, False, True, True, True])
    best = c.drop_duplicates("game")
    return best[best["ev_21"] > 0].reset_index(drop=True)
