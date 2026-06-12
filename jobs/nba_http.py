"""HTTP client for stats.nba.com — lifted from the proven cache_nba.py pattern.

Browser-masquerading headers, exponential backoff with jitter, and a circuit
breaker so a throttled day degrades gracefully instead of hammering the API.
Read timeout defaults to 90s because full-season leaguegamelog payloads are
5-20 MB.
"""
from __future__ import annotations

import os
import random
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

NBA_STATS_BASE = "https://stats.nba.com/stats"

NBA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}


def make_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(NBA_HEADERS)
    return s


def _sleep_backoff(attempt: int, base: float, cap: float) -> None:
    expo = base * (2 ** attempt)
    jitter = random.uniform(0.0, base)
    time.sleep(min(cap, expo + jitter))


class CircuitBreaker:
    def __init__(self, fail_threshold: int = 6):
        self.fail_threshold = fail_threshold
        self.consecutive_failures = 0
        self.opened = False

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.fail_threshold:
            self.opened = True

    def allow(self) -> bool:
        return not self.opened


def nba_stats_get_json(
    session: requests.Session,
    endpoint: str,
    params: Dict[str, Any],
    *,
    timeout: Tuple[float, float] = (10, 90),
    retries: Optional[int] = None,
    backoff_base: float = 1.0,
    backoff_cap: float = 30.0,
    cb: Optional[CircuitBreaker] = None,
) -> Optional[dict]:
    """Robust GET for stats.nba.com. Returns JSON dict or None on failure."""
    if cb and not cb.allow():
        print(f"[WARN] circuit breaker OPEN; skip stats endpoint={endpoint}", flush=True)
        return None

    if retries is None:
        retries = int(os.environ.get("NBA_STATS_RETRIES") or "3")

    url = f"{NBA_STATS_BASE}/{endpoint}"
    last_err: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            if random.random() < 0.15:
                time.sleep(random.uniform(0.1, 0.6))

            r = session.get(url, params=params, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if cb:
                cb.record_success()
            return data
        except Exception as e:
            last_err = e
            if attempt < retries:
                print(f"[WARN] retry {attempt+1}/{retries} endpoint={endpoint} err={e}", flush=True)
                _sleep_backoff(attempt, backoff_base, backoff_cap)
            else:
                print(f"[WARN] FAILED endpoint={endpoint} err={e}", flush=True)

    if cb:
        cb.record_failure()
        if not cb.allow():
            print(f"[WARN] circuit breaker OPEN after err={last_err}", flush=True)

    return None


def resultset_to_df(data: dict, idx: int = 0) -> pd.DataFrame:
    """{"resultSets": [{"headers": [...], "rowSet": [...]}]} -> DataFrame."""
    try:
        rs = (data.get("resultSets") or [])[idx]
        headers = rs.get("headers") or []
        rows = rs.get("rowSet") or []
        return pd.DataFrame(rows, columns=headers)
    except Exception as e:
        print(f"[WARN] resultset_to_df failed idx={idx} err={e}", flush=True)
        return pd.DataFrame()
