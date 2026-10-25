"""Findings, the known-problem baseline state machine and shared SQL helpers.

Everything in jobs/quality is read-only: checks only SELECT, and the CLI never
writes to the database (--write-baseline writes a local JSON file).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import List, NamedTuple, Optional

import pandas as pd

SEVERITIES = ("error", "warn", "info")


@dataclass
class Finding:
    check_id: str
    severity: str            # "error" | "warn" | "info"
    scope: str               # e.g. "2025-26/regular"
    n_bad: int
    n_total: int
    detail: dict = field(default_factory=dict)
    baseline: Optional[int] = None
    status: str = "ok"       # ok | known | new | worse | info


class Scope(NamedTuple):
    seasons: Optional[List[str]]   # None = every season
    since: Optional[date]          # strictly after this date (game_date_et > since)


def scope_where(scope: Scope, alias: str = "g") -> tuple:
    """(extra WHERE fragment, params) restricting games to the scope. Always starts
    with ' AND ' so it can be appended to an existing WHERE."""
    parts, params = [], []
    if scope.seasons:
        parts.append(f" AND {alias}.season = ANY(%s)")
        params.append(list(scope.seasons))
    if scope.since is not None:
        parts.append(f" AND {alias}.game_date_et > %s")
        params.append(scope.since)
    return "".join(parts), tuple(params)


def allowed_in_window(n_total: int, recent_rate: float) -> int:
    """How many bad rows a window may show before a chronic, low-rate problem is 'worse'."""
    return math.ceil(n_total * recent_rate * 3) + 2


def classify(f: Finding, entry: Optional[dict], *, since_mode: bool = False) -> Finding:
    """Compare a Finding with its baseline entry. Returns a new Finding."""
    out = replace(f, detail=dict(f.detail))
    out.baseline = entry.get("n_bad") if entry else None
    if f.severity == "info":
        out.status = "info"
        return out
    if f.n_bad == 0:
        out.status = "ok"
        if not since_mode and entry and entry.get("n_bad", 0) > 0:
            out.detail["improved"] = True
        return out
    if since_mode:
        if entry is None:
            out.status = "new"            # zero at baseline time: any bad row is news
        elif entry.get("recent_rate", 0) > 0:
            out.status = ("known" if f.n_bad <= allowed_in_window(f.n_total, entry["recent_rate"])
                          else "worse")
        else:
            out.status = "worse"
        return out
    if entry is None:
        out.status = "new"
    elif f.n_bad <= entry.get("n_bad", 0):
        out.status = "known"
    else:
        out.status = "worse"
    return out


def baseline_key(check_id: str, scope: str) -> str:
    return f"{check_id}|{scope}"


def load_baseline(path: Optional[Path]) -> dict:
    empty = {"as_of_date": None, "entries": {}, "rates": {}}
    if path is None or not Path(path).exists():
        return empty
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"as_of_date": data.get("as_of_date"),
            "entries": data.get("entries") or {},
            "rates": data.get("rates") or {}}


def exit_code(findings: List[Finding], fail_on: str) -> int:
    """Non-zero only for new or worse problems at or above the --fail-on level."""
    if fail_on == "none":
        return 0
    levels = {"error"} if fail_on == "error" else {"error", "warn"}
    return 1 if any(f.status in ("new", "worse") and f.severity in levels for f in findings) else 0


def findings_from_frame(df: pd.DataFrame, check_id: str, severity: str, bad_col: str,
                        total_col: str, detail_cols: tuple = ()) -> List[Finding]:
    """One Finding per (season, season_type) row. An empty frame yields a single
    zero Finding so a check always reports something and never divides by zero."""
    if df is None or df.empty:
        return [Finding(check_id, severity, "all/none", 0, 0, {})]
    out = []
    for _, row in df.iterrows():
        scope = f"{row['season']}/{row['season_type']}"
        out.append(Finding(check_id, severity, scope, int(row[bad_col] or 0),
                           int(row[total_col] or 0),
                           {c: _py(row[c]) for c in detail_cols}))
    return out


def _py(v):
    """numpy / pandas scalars -> plain Python (JSON friendly)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v.item() if hasattr(v, "item") else v
