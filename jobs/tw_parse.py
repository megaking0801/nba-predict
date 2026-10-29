"""Parse the raw snapshots the phone Shortcut uploads (read-only, no network).

Files live in the private data repo as tw_snapshots/<Taipei yyyyMMdd>/<yyyyMMddTHHmm±zzzz>-<kind>.json, kind in
tw (Taiwan lottery pre-match basketball feed), espn (ESPN scoreboard, DraftKings) and oddsapi (The Odds API).
Design: docs/superpowers/specs/2026-10-29-tw-snapshot-design.md. Team names go through jobs.teams and raise
when unknown.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import List

import pandas as pd

from jobs.playbook import american_to_decimal
from jobs.teams import abbr_from_cn, normalize_espn_abbr, require_team_name

SNAPSHOT_RE = r"^(\d{8}T\d{4}[+-]\d{4})-(tw|espn|oddsapi)\.json$"
NBA_TN = "美國職籃"
_SPREAD, _TOTAL, _ML = "[總分]讓分", "[總分]大小", "不讓分"


def list_snapshots(root) -> pd.DataFrame:
    """Every snapshot file under root (skipping _samples/ and anything not named like a snapshot)."""
    rows = []
    root = Path(root)
    for p in sorted(root.rglob("*.json")):
        if "_samples" in p.relative_to(root).parts:
            continue
        m = re.match(SNAPSHOT_RE, p.name)
        if not m:
            continue
        stamp = dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%z")
        rows.append({"stamp_utc": pd.Timestamp(stamp).tz_convert("UTC"), "kind": m.group(2), "path": p})
    return pd.DataFrame(rows, columns=["stamp_utc", "kind", "path"])


def _utc(text) -> pd.Timestamp:
    return pd.Timestamp(text).tz_convert("UTC")


def _tw_market(name: str):
    if name == _ML:
        return "ml"
    if name.startswith(_SPREAD):
        return "spread"
    if name.startswith(_TOTAL):
        return "total"
    return None


def _tw_rows(game: dict) -> List[dict]:
    home, away = abbr_from_cn(game.get("hn")), abbr_from_cn(game.get("an"))
    names = {home: game.get("hn").strip(), away: game.get("an").strip()}
    base = {"home": home, "away": away, "kickoff_utc": _utc(game["kt"]), "er": game.get("er")}
    rows = []
    for mk in game.get("ms") or []:
        market = _tw_market((mk.get("name") or "").strip())
        if market is None:
            continue
        for c in mk.get("cs") or []:
            name = (c.get("name") or "").strip()
            row = dict(base, market=market, dec=1.0 + float(c["pu"]) / float(c["pd"]),
                       updated_utc=_utc(mk["ss"]), m=int(mk["m"]))
            if market == "total":
                if not (name.startswith("大") or name.startswith("小")):
                    raise ValueError(f"unexpected total option: {name!r}")
                row.update(side="over" if name.startswith("大") else "under", line=float(c["hv"]))
            else:
                team, _, num = name.rpartition(" ") if market == "spread" else (name, "", "")
                side = "home" if team == names[home] else "away" if team == names[away] else None
                if side is None:
                    raise ValueError(f"option {name!r} names neither {names[home]!r} nor {names[away]!r}")
                row["side"] = side
                if market == "spread":
                    hv = float(c["hv"])                    # always the home handicap
                    own = hv if side == "home" else -hv
                    if abs(float(num) - own) > 1e-9:
                        raise ValueError(f"option {name!r} disagrees with home handicap hv={c['hv']!r}")
                    row["line"] = hv
                else:
                    row["line"] = float("nan")
            rows.append(row)
    return rows


def parse_tw(raw: bytes) -> pd.DataFrame:
    """One row per NBA selection of the main markets (moneyline, full-game spread lines, full-game total)."""
    games = json.loads(raw.decode("utf-8-sig"))
    rows = [r for g in games if g.get("tn") == NBA_TN for r in _tw_rows(g)]
    return pd.DataFrame(rows, columns=["home", "away", "kickoff_utc", "er", "market", "line", "side", "dec",
                                       "updated_utc", "m"])


def _num(text) -> float:
    return float(str(text).lstrip("ou").replace("+", ""))


def _espn_point(odds: dict, point: str) -> dict:
    sp, tot = odds.get("pointSpread") or {}, odds.get("total") or {}
    sfx = "" if point == "close" else "_open"
    h, a = (sp.get("home") or {}).get(point) or {}, (sp.get("away") or {}).get(point) or {}
    o, u = (tot.get("over") or {}).get(point) or {}, (tot.get("under") or {}).get(point) or {}
    return {f"spread_line{sfx}": _num(h["line"]) if h.get("line") is not None else float("nan"),
            f"spread_home_dec{sfx}": american_to_decimal(h.get("odds")),
            f"spread_away_dec{sfx}": american_to_decimal(a.get("odds")),
            f"total_line{sfx}": _num(o["line"]) if o.get("line") is not None else float("nan"),
            f"over_dec{sfx}": american_to_decimal(o.get("odds")),
            f"under_dec{sfx}": american_to_decimal(u.get("odds"))}


def parse_espn(raw: bytes) -> pd.DataFrame:
    """One row per game with DraftKings odds: current ('close') and opening spread and total."""
    rows = []
    for ev in json.loads(raw).get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        odds = (comp.get("odds") or [None])[0]
        if not odds:
            continue
        provider = ((odds.get("provider") or {}).get("name") or "").replace(" ", "").lower()
        if provider != "draftkings":
            raise ValueError(f"ESPN odds provider is {odds.get('provider')!r}, expected DraftKings")
        side = {c["homeAway"]: normalize_espn_abbr(c["team"]["abbreviation"]) for c in comp["competitors"]}
        rows.append({"home": side["home"], "away": side["away"], "tipoff_utc": _utc(ev["date"]),
                     **_espn_point(odds, "close"), **_espn_point(odds, "open")})
    return pd.DataFrame(rows)


def parse_oddsapi(raw: bytes) -> pd.DataFrame:
    """One row per (game, book, market, side). Spread lines are the home handicap, total lines the total."""
    rows = []
    for ev in json.loads(raw):
        home, away = require_team_name(ev["home_team"]), require_team_name(ev["away_team"])
        base = {"home": home, "away": away, "tipoff_utc": _utc(ev["commence_time"])}
        for bk in ev.get("bookmakers") or []:
            for mk in bk.get("markets") or []:
                for o in mk.get("outcomes") or []:
                    if mk["key"] == "spreads":
                        side = "home" if require_team_name(o["name"]) == home else "away"
                        line = float(o["point"]) if side == "home" else -float(o["point"])
                        market = "spread"
                    elif mk["key"] == "totals":
                        side, line, market = o["name"].lower(), float(o["point"]), "total"
                    else:
                        continue
                    rows.append(dict(base, book=bk["key"], market=market, line=line, side=side,
                                     dec=float(o["price"])))
    return pd.DataFrame(rows, columns=["home", "away", "tipoff_utc", "book", "market", "line", "side", "dec"])
