"""Change detection for the injury report: which entries are new, changed, updated or cleared.

Pure functions, no network, no DB. Players are keyed by (team_abbr, player_name) because
espn_player_id is NULL on rows seen before the 2026-10-28 parser fix.
Design: docs/superpowers/specs/2026-10-28-injury-events-design.md
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, Iterable, List, Optional, Tuple

Key = Tuple[str, str]


def _iso(ts: Optional[dt.datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


def detect(known: Dict[Key, dict], rows: Iterable[dict], now: dt.datetime,
           baseline: bool = False) -> Tuple[List[dict], Dict[Key, dict]]:
    """Compare one fetch (``rows`` as returned by snapshot_injuries.parse_injuries) with ``known``.

    Returns (events, new_known). kind is 'new' (first time seen), 'status_change', 'update' (same status,
    newer reported_at) or 'cleared' (was listed, now gone). With baseline=True the very first fetch marks
    everything 'baseline' instead of 'new': those are not news, only the starting state."""
    events: List[dict] = []
    current: Dict[Key, dict] = {}
    for r in rows:
        key = (r["team_abbr"], r["player_name"])
        current[key] = {"status": r.get("status"), "reported_at": _iso(r.get("reported_at")),
                        "detail": r.get("detail"), "espn_player_id": r.get("espn_player_id")}
    for key, cur in current.items():
        old = known.get(key)
        if old is None:
            kind, prev = ("baseline" if baseline else "new"), None
        elif old["status"] != cur["status"]:
            kind, prev = "status_change", old["status"]
        elif cur["reported_at"] and cur["reported_at"] != old["reported_at"]:
            kind, prev = "update", old["status"]
        else:
            continue
        events.append({"kind": kind, "first_seen_at": now.isoformat(), "team_abbr": key[0],
                       "player_name": key[1], "espn_player_id": cur["espn_player_id"],
                       "status": cur["status"], "prev_status": prev, "detail": cur["detail"],
                       "reported_at": cur["reported_at"]})
    for key, old in known.items():
        if key not in current:
            events.append({"kind": "cleared", "first_seen_at": now.isoformat(), "team_abbr": key[0],
                           "player_name": key[1], "espn_player_id": old.get("espn_player_id"),
                           "status": None, "prev_status": old["status"], "detail": None,
                           "reported_at": None})
    return events, current
