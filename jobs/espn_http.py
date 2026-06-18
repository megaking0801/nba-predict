"""HTTP client for ESPN's public NBA API — the data source after stats.nba.com
proved unreachable (Akamai geo/IP-blocks both GitHub runners and non-US homes).

ESPN endpoints used:
  scoreboard?dates=YYYYMMDD  -> every game that day (teams, scores, status, season)
  summary?event=<id>         -> one game's team + player box score

No API key, generally not geo-blocked, tolerant of modest concurrency.
"""
from __future__ import annotations

import json
import random
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
           "Accept": "application/json, text/plain, */*"}


def get_json(url: str, *, timeout: float = 20.0, retries: int = 3,
             backoff_base: float = 1.0, backoff_cap: float = 20.0) -> Optional[dict]:
    """GET JSON with exponential backoff + jitter. Returns dict or None."""
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # no such event/date — not an error worth retrying
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < retries:
            time.sleep(min(backoff_cap, backoff_base * (2 ** attempt) + random.uniform(0, backoff_base)))
    print(f"[WARN] ESPN GET failed url={url} err={last_err}", flush=True)
    return None


def scoreboard(date_yyyymmdd: str, **kw) -> Optional[dict]:
    return get_json(f"{ESPN_BASE}/scoreboard?dates={date_yyyymmdd}&limit=100", **kw)


def summary(event_id: str, **kw) -> Optional[dict]:
    return get_json(f"{ESPN_BASE}/summary?event={event_id}", **kw)
