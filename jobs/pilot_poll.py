"""Phase-0 pilot for the price-lag observation (local, read-only, no DB).

    python -m jobs.pilot_poll --once                      # one tick, to check that everything is reachable
    python -m jobs.pilot_poll --interval 300 --max-hours 12
    ODDS_API_KEY=... python -m jobs.pilot_poll --interval 300   # also samples The Odds API (free key, set by the user)

Every tick it GETs the ESPN injury report and diffs it with the previous tick (jobs.injury_events). When a
key player-type entry turns Out / Day-To-Day for a team with a game in the next 14 h, it
  * watches that game's ESPN line every tick for 60 minutes, and
  * schedules Odds API samples at +0, +5, +15, +30, +60 minutes (merged, one call returns every game).
Everything is appended to data/pilot/poll-<UTC date>.jsonl.gz (gzip members, readable with gzip.open). Only
CHANGES are stored (injury events, changed ESPN lines) plus one small heartbeat per tick; the Odds API samples are
always stored, with each bookmaker's own last_update next to OUR fetch time, so our polling delay is never
mistaken for a bookmaker's delay. Nothing is written to the database.

Rules:
- ODDS_API_KEY is read from the environment only; it is never printed, logged or saved (errors log the class and
  HTTP status, never the URL). The free plan is 500 credits a month; a call with markets=spreads, regions=us costs 1.
  Hard caps (default 16 a day, 450 a month, and a reserve left on the provider's remaining-credits header) stop calls.
- The ESPN endpoints are the ones the project already uses. Disney's general terms forbid automated access and
  collecting data (see docs/handoff/2026-10-28-data-source-survey.md): running this is the user's decision.
- Minimum interval 60 s. Stop with Ctrl+C; it is safe to restart (state: data/pilot/pilot-state.json).
- This script does not run itself. Design: docs/superpowers/specs/2026-10-28-injury-events-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from jobs.injury_events import detect
from jobs.teams import abbr_from_team_name, normalize_espn_abbr
from jobs.tz import ET, UTC

OUT_DIR = Path("data/pilot")
ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
OFFSETS_MIN = (0, 5, 15, 30, 60)
WATCH_MIN = 60                      # watch an affected game's ESPN line this long after an event
GAME_HORIZON_H = 14                 # only games tipping off within this many hours matter
SCHEDULE_REFRESH_S = 1800
TRIGGER_STATUSES = ("Out", "Day-To-Day")
TRIGGER_KINDS = ("new", "status_change", "update")
MIN_INTERVAL_S = 60


# ----- budget and schedule (pure, injected clock) -----

class Budget:
    """Hard caps on Odds API calls: per UTC day, per UTC month, and a reserve on the provider's own counter."""

    def __init__(self, path: Path, daily_cap: int = 16, monthly_cap: int = 450, reserve: int = 30):
        self.path, self.daily_cap, self.monthly_cap, self.reserve = path, daily_cap, monthly_cap, reserve
        self.state = {"day": "", "day_used": 0, "month": "", "month_used": 0, "remaining": None}
        if path.exists():
            try:
                self.state.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def _roll(self, now: dt.datetime) -> None:
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        if self.state["day"] != day:
            self.state["day"], self.state["day_used"] = day, 0
        if self.state["month"] != month:
            self.state["month"], self.state["month_used"] = month, 0

    def can_spend(self, now: dt.datetime, cost: int = 1) -> bool:
        self._roll(now)
        rem = self.state.get("remaining")
        return (self.state["day_used"] + cost <= self.daily_cap
                and self.state["month_used"] + cost <= self.monthly_cap
                and (rem is None or rem - cost >= self.reserve))

    def spend(self, now: dt.datetime, cost: int = 1, remaining: Optional[int] = None) -> None:
        self._roll(now)
        self.state["day_used"] += cost
        self.state["month_used"] += cost
        if remaining is not None:
            self.state["remaining"] = remaining
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state), encoding="utf-8")


class OddsSchedule:
    """Times at which an Odds API sample is due. Nearby requests merge: one call returns every game."""

    def __init__(self, merge_s: float = 120.0):
        self.times: List[float] = []
        self.merge_s = merge_s

    def add_event(self, t: float, offsets_min=OFFSETS_MIN) -> None:
        for m in offsets_min:
            due = t + 60.0 * m
            if not any(abs(due - x) < self.merge_s for x in self.times):
                self.times.append(due)

    def pop_due(self, now: float) -> bool:
        due = [x for x in self.times if x <= now]
        if due:
            self.times = [x for x in self.times if x > now]
        return bool(due)


# ----- parsing -----

def parse_scoreboard(payload: Optional[dict]) -> List[dict]:
    """Games from an ESPN scoreboard payload: id, home/away tricode, tipoff (UTC)."""
    out = []
    for ev in (payload or {}).get("events") or []:
        try:
            comp = ev["competitions"][0]
            teams = {c["homeAway"]: normalize_espn_abbr(c["team"]["abbreviation"]) for c in comp["competitors"]}
            tip = dt.datetime.fromisoformat(str(ev["date"]).replace("Z", "+00:00"))
            state = (ev.get("status") or {}).get("type", {}).get("state")
            out.append({"game_id": str(ev["id"]), "home": teams["home"], "away": teams["away"],
                        "tipoff": tip if tip.tzinfo else tip.replace(tzinfo=UTC), "state": state})
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return out


def parse_oddsapi(games: List[dict]) -> List[dict]:
    """Compact rows: per game, per bookmaker the home spread/price with the bookmaker's own last_update."""
    out = []
    for g in games or []:
        books = []
        for b in g.get("bookmakers") or []:
            for m in b.get("markets") or []:
                if m.get("key") != "spreads":
                    continue
                by_team = {o.get("name"): o for o in m.get("outcomes") or []}
                home, away = by_team.get(g.get("home_team")), by_team.get(g.get("away_team"))
                if not home:
                    continue
                books.append({"book": b.get("key"), "last_update": m.get("last_update") or b.get("last_update"),
                              "home_point": home.get("point"), "home_price": home.get("price"),
                              "away_price": away.get("price") if away else None})
        out.append({"home": abbr_from_team_name(g.get("home_team")) or g.get("home_team"),
                    "away": abbr_from_team_name(g.get("away_team")) or g.get("away_team"),
                    "commence_time": g.get("commence_time"), "books": books})
    return out


def espn_line_key(parsed: Optional[dict]) -> Optional[tuple]:
    if not parsed:
        return None
    return (parsed.get("home_spread"), parsed.get("home_price"), parsed.get("away_price"), parsed.get("total_line"))


# ----- output -----

class Out:
    def __init__(self, directory: Path):
        self.dir = directory

    def write(self, record: dict, when: dt.datetime) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"poll-{when.astimezone(UTC).strftime('%Y-%m-%d')}.jsonl.gz"
        with gzip.open(path, "ab") as f:
            f.write((json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8"))


# ----- one tick -----

def watched_games(event: dict, games: List[dict], now: dt.datetime) -> List[dict]:
    horizon = now + dt.timedelta(hours=GAME_HORIZON_H)
    return [g for g in games if event["team_abbr"] in (g["home"], g["away"])
            and g["state"] in (None, "pre") and now < g["tipoff"] <= horizon]


class Poller:
    def __init__(self, out: Out, budget: Budget, fetch: Dict[str, Callable], api_key: Optional[str],
                 odds_base_s: float = 10800.0):
        self.out, self.budget, self.fetch, self.api_key = out, budget, fetch, api_key
        self.known: Dict[tuple, dict] = {}
        self.first = True
        self.games: List[dict] = []
        self.games_at = -1e18
        self.watch: Dict[str, float] = {}          # game_id -> watch until (epoch s)
        self.last_line: Dict[str, Optional[tuple]] = {}
        self.sched = OddsSchedule()
        self.odds_base_s, self.last_base = odds_base_s, -1e18
        self.last_odds_base = -1e18                # last Odds API call of any kind

    def _log(self, now: dt.datetime, src: str, **kw) -> None:
        self.out.write({"t": now.isoformat(), "src": src, **kw}, now)

    def tick(self, now: dt.datetime) -> dict:
        t = now.timestamp()
        stats = {"events": 0, "espn_lines": 0, "odds_calls": 0, "errors": 0}
        if t - self.games_at >= SCHEDULE_REFRESH_S:
            games = []
            for d in (now.astimezone(ET).date(), now.astimezone(ET).date() + dt.timedelta(days=1)):
                games += parse_scoreboard(self.fetch["scoreboard"](d.strftime("%Y%m%d")))
            if games:
                self.games, self.games_at = games, t
        rows = self.fetch["injuries"]()
        if rows is None:
            stats["errors"] += 1
            self._log(now, "error", what="injuries")
        else:
            events, self.known = detect(self.known, rows, now, baseline=self.first)
            self.first = False
            for ev in events:
                self._log(now, "injury_event", **ev)
                stats["events"] += ev["kind"] != "baseline"
                if ev["kind"] in TRIGGER_KINDS and ev["status"] in TRIGGER_STATUSES:
                    gs = watched_games(ev, self.games, now)
                    for g in gs:
                        self.watch[g["game_id"]] = t + 60 * WATCH_MIN
                    if gs:
                        self.sched.add_event(t)
        # ESPN lines of watched games (every tick) and a slow baseline of all upcoming games
        horizon = now + dt.timedelta(hours=GAME_HORIZON_H)
        upcoming = [g for g in self.games if g["state"] in (None, "pre") and now < g["tipoff"] <= horizon]
        base_due = t - self.last_base >= 1800
        if base_due:
            self.last_base = t
        for g in upcoming:
            if self.watch.get(g["game_id"], 0) < t and not base_due:
                continue
            parsed = self.fetch["espn_odds"](g["game_id"])
            if parsed is None:
                continue
            key = espn_line_key(parsed)
            if key != self.last_line.get(g["game_id"]):
                self.last_line[g["game_id"]] = key
                self._log(now, "espn_line", game_id=g["game_id"], home=g["home"], away=g["away"],
                          tipoff=g["tipoff"].isoformat(), **{k: parsed.get(k) for k in (
                              "home_spread", "home_price", "away_price", "total_line", "price_point")})
                stats["espn_lines"] += 1
        # The Odds API: event-triggered samples, plus a slow base sample while games are upcoming
        if self.api_key and upcoming:
            due = self.sched.pop_due(t)
            base = (t - self.last_odds_base >= self.odds_base_s
                    and self.budget.state.get("day_used", 0) < self.budget.daily_cap // 2)
            if (due or base) and self.budget.can_spend(now):
                res = self.fetch["odds_api"](self.api_key)
                if res is None:
                    stats["errors"] += 1
                    self._log(now, "error", what="odds_api")
                else:
                    games, remaining = res
                    self.budget.spend(now, 1, remaining)
                    self.last_odds_base = t
                    self._log(now, "oddsapi", fetched_at=now.isoformat(), remaining=remaining,
                              games=parse_oddsapi(games), reason="event" if due else "base")
                    stats["odds_calls"] += 1
        self._log(now, "heartbeat", injury_rows=None if rows is None else len(rows),
                  watching=sum(1 for v in self.watch.values() if v >= t), **stats)
        return stats


# ----- real network fetchers (not used in tests) -----

def _fetchers() -> Dict[str, Callable]:
    from jobs import espn_http
    from jobs import snapshot_injuries
    from jobs.ingest_espn import parse_close_line
    from jobs.tz import et_today

    def injuries():
        payload = espn_http.get_json(snapshot_injuries.INJURIES_URL, retries=1)
        return None if payload is None else snapshot_injuries.parse_injuries(payload, et_today())

    def espn_odds(game_id):
        d = espn_http.odds(game_id, retries=1)
        return parse_close_line((d or {}).get("items", [])) if d else None

    def odds_api(key):
        q = urllib.parse.urlencode({"apiKey": key, "regions": "us", "markets": "spreads", "oddsFormat": "decimal"})
        try:
            with urllib.request.urlopen(f"{ODDS_URL}?{q}", timeout=25) as r:
                rem = r.headers.get("x-requests-remaining")
                return json.loads(r.read()), int(float(rem)) if rem is not None else None
        except urllib.error.HTTPError as e:                  # the URL carries the key: never print it
            print(f"[WARN] odds api HTTP {e.code}", flush=True)
        except Exception as e:                                # noqa: BLE001
            print(f"[WARN] odds api {type(e).__name__}", flush=True)
        return None

    return {"scoreboard": espn_http.scoreboard, "injuries": injuries, "espn_odds": espn_odds, "odds_api": odds_api}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobs.pilot_poll", description="Phase-0 price-lag pilot (no DB)")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--interval", type=int, default=300, help="seconds between ticks (min 60)")
    ap.add_argument("--max-hours", type=float, default=0, help="stop after this many hours (0 = until Ctrl+C)")
    ap.add_argument("--once", action="store_true", help="run one tick and exit")
    ap.add_argument("--odds-daily-cap", type=int, default=16)
    ap.add_argument("--odds-monthly-cap", type=int, default=450)
    ap.add_argument("--odds-base-minutes", type=int, default=180, help="slow Odds API sample while games are upcoming")
    args = ap.parse_args(argv)
    interval = max(MIN_INTERVAL_S, args.interval)
    key = os.environ.get("ODDS_API_KEY") or None
    out_dir = Path(args.out_dir)
    poller = Poller(Out(out_dir), Budget(out_dir / "pilot-state.json", args.odds_daily_cap, args.odds_monthly_cap),
                    _fetchers(), key, odds_base_s=60.0 * args.odds_base_minutes)
    print(f"[pilot] odds api: {'on' if key else 'off (ODDS_API_KEY not set)'}; interval {interval}s; out {out_dir}", flush=True)
    start = time.time()
    while True:
        now = dt.datetime.now(UTC)
        s = poller.tick(now)
        print(f"[pilot] {now:%H:%M:%S}Z {s}", flush=True)
        if args.once or (args.max_hours and time.time() - start >= args.max_hours * 3600):
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
