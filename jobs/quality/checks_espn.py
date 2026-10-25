"""A1: compare ESPN's scoreboards with the games in the DB (read-only GETs, SELECT only).

Only runs with --with-espn because it needs the network. It checks that
(1) every NBA-vs-NBA finished game ESPN lists is in the DB, per season and type, and
(2) the events we skip (a team abbreviation we cannot map) are all of an allowed kind.
"""
from __future__ import annotations

import collections
import datetime as dt
from typing import Callable, List, Optional

from jobs import ingest_espn
from jobs.quality.core import Finding, Scope
from jobs.teams import normalize_espn_abbr

MAX_EXAMPLES = 20

_DB_COUNTS = """/* qc:A1 */
SELECT g.season, g.season_type, count(*) AS n
  FROM public.games_v2 g
 WHERE g.status = 'final' AND g.game_date_et BETWEEN %s AND %s
 GROUP BY g.season, g.season_type
"""


def _is_nba(abbr: str) -> bool:
    try:
        normalize_espn_abbr(abbr)
        return True
    except ValueError:
        return False


def classify_event(e: dict) -> dict:
    """What an ESPN scoreboard event is: an NBA game, an allowed skip, or a bad skip."""
    comp = e["competitions"][0]
    abbrs = [c["team"]["abbreviation"] for c in comp["competitors"]]
    year = (e.get("season") or {}).get("year")
    stype = ingest_espn.SEASON_TYPE.get((e.get("season") or {}).get("type"), "regular")
    final = comp["status"]["type"]["name"] == "STATUS_FINAL"
    n_nba = sum(1 for a in abbrs if _is_nba(a))
    info = {"id": e.get("id"), "abbrs": abbrs, "type": stype, "final": final,
            "season": ingest_espn._season_str(year) if year else None}
    if n_nba == 2:
        info["kind"] = "nba"
    elif stype == "preseason" or "TBD" in abbrs or (n_nba == 0 and stype in ("regular", "allstar")):
        info["kind"] = "skip_ok"          # international preseason, undecided opponent, all-star game
    else:
        info["kind"] = "skip_bad"         # e.g. a real NBA team against an abbreviation we do not know
    return info


def _dates(scope: Scope, today: dt.date) -> List[dt.date]:
    if scope.since is not None:
        lo, hi = scope.since + dt.timedelta(days=1), today
        return [lo + dt.timedelta(days=i) for i in range((hi - lo).days + 1)]
    seasons = scope.seasons or sorted(ingest_espn.SEASON_WINDOWS)
    out: List[dt.date] = []
    for s in seasons:
        lo, hi = ingest_espn.SEASON_WINDOWS[s]
        out += [lo + dt.timedelta(days=i) for i in range((min(hi, today) - lo).days + 1)]
    return out


def check_a1_espn_event_coverage(run, scope: Scope, fetch_scoreboard: Optional[Callable] = None,
                                 today: Optional[dt.date] = None) -> List[Finding]:
    if fetch_scoreboard is None:
        fetch_scoreboard = ingest_espn.scoreboard
    today = today or dt.date.today()
    dates = _dates(scope, today)
    if not dates:
        return [Finding("A1", "error", "all/none", 0, 0, {})]
    espn = collections.Counter()
    bad, failed, skipped_total = [], [], 0
    for d in dates:
        sb = fetch_scoreboard(d.strftime("%Y%m%d"))
        if sb is None:
            failed.append(d.isoformat())
            continue
        for e in sb.get("events", []):
            try:
                info = classify_event(e)
            except (KeyError, IndexError, TypeError):
                bad.append({"id": e.get("id"), "abbrs": None, "type": None, "why": "unparseable"})
                continue
            if info["kind"] == "nba":
                if info["final"] and info["season"]:
                    espn[(info["season"], info["type"])] += 1
            else:
                skipped_total += 1
                if info["kind"] == "skip_bad":
                    bad.append({"id": info["id"], "abbrs": info["abbrs"], "type": info["type"]})
    db = run(_DB_COUNTS, (dates[0], dates[-1]))
    db_counts = {(r["season"], r["season_type"]): int(r["n"]) for _, r in db.iterrows()} if len(db) else {}
    out = [Finding("A1", "error", f"{s}/{t}", abs(n - db_counts.get((s, t), 0)), n,
                   {"espn": n, "db": db_counts.get((s, t), 0)})
           for (s, t), n in sorted(espn.items())]
    for key in sorted(set(db_counts) - set(espn)):       # in the DB but ESPN lists nothing
        out.append(Finding("A1", "error", f"{key[0]}/{key[1]}", db_counts[key], 0,
                           {"espn": 0, "db": db_counts[key]}))
    out.append(Finding("A1", "error", "events/skipped", len(bad), skipped_total,
                       {"examples": bad[:MAX_EXAMPLES]}))
    out.append(Finding("A1", "error", "events/fetch", len(failed), len(dates),
                       {"failed_dates": failed[:MAX_EXAMPLES]}))
    return out
