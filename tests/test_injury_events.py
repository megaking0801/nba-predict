"""jobs.injury_events: change detection for the injury report (pure, no network)."""
from __future__ import annotations

import datetime as dt

from jobs.injury_events import detect

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
T1 = T0 + dt.timedelta(minutes=5)


def row(team="ATL", name="A. Player", status="Day-To-Day", rep=dt.datetime(2026, 10, 28, 13, 0, tzinfo=UTC)):
    return {"team_abbr": team, "player_name": name, "status": status, "detail": "Knee",
            "reported_at": rep, "espn_player_id": None}


def test_first_fetch_is_a_baseline_not_news():
    ev, known = detect({}, [row(), row(name="B")], T0, baseline=True)
    assert {e["kind"] for e in ev} == {"baseline"} and len(known) == 2


def test_new_status_change_update_and_cleared():
    _, known = detect({}, [row(), row(name="B"), row(name="C")], T0, baseline=True)
    later = dt.datetime(2026, 10, 28, 14, 2, tzinfo=UTC)
    rows = [row(status="Out"),                                    # changed
            row(name="B", rep=later),                             # same status, newer report
            row(name="D")]                                        # new; C is gone
    ev, known2 = detect(known, rows, T1)
    kinds = {(e["player_name"], e["kind"]) for e in ev}
    assert kinds == {("A. Player", "status_change"), ("B", "update"), ("D", "new"), ("C", "cleared")}
    ch = next(e for e in ev if e["kind"] == "status_change")
    assert ch["prev_status"] == "Day-To-Day" and ch["status"] == "Out"
    assert ("ATL", "C") not in known2 and ("ATL", "D") in known2


def test_identical_fetch_is_idempotent():
    _, known = detect({}, [row()], T0, baseline=True)
    ev, known2 = detect(known, [row()], T1)
    assert ev == [] and known2 == known


def test_missing_reported_at_does_not_cause_repeated_updates():
    _, known = detect({}, [row(rep=None)], T0, baseline=True)
    assert detect(known, [row(rep=None)], T1)[0] == []
