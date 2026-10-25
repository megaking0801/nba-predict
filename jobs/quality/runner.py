"""Run the checks, compare them with the known-problem baseline, print and exit.

    python -m jobs.quality [--all | --seasons 2025-26,2026-27] [--since 7d] [--with-espn]
                           [--fail-on error|warn|none] [--json PATH] [--baseline PATH]
                           [--write-baseline PATH] [--verify-flags]

Read-only: the connection is a read-only session and --write-baseline only writes a
local JSON file. jobs -> webui import below is deliberate (re-use connect_readonly)
so jobs/db_utils.py, which other jobs depend on, stays untouched.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from jobs.quality import (checks_box, checks_games, checks_lines, checks_shots)
from jobs.quality.checks_espn import check_a1_espn_event_coverage
from jobs.quality.core import (Finding, Scope, baseline_key, classify, exit_code, load_baseline,
                               scope_where)
from jobs.quality.game_quality import game_quality, verify_flags
from webui.build_site import connect_readonly, make_run

DEFAULT_BASELINE = Path(__file__).resolve().parent / "baseline.json"
D2_DROP_ALLOWED = 3.0     # percentage points of exact-match share a season may lose

REGISTRY: List[tuple] = [
    ("A2", checks_games.check_a2_team_game_counts), ("A3", checks_games.check_a3_duplicate_games),
    ("A4", checks_games.check_a4_team_twice_same_day), ("A5", checks_games.check_a5_stuck_not_final),
    ("A6", checks_games.check_a6_final_without_score), ("A7", checks_games.check_a7_suspicious_tipoff),
    ("B1", checks_box.check_b1_team_box_complete), ("B2", checks_box.check_b2_player_box_not_placeholder),
    ("B3", checks_box.check_b3_player_points_match_team), ("B4", checks_box.check_b4_team_minutes_240),
    ("B5", checks_box.check_b5_impossible_lines), ("B6", checks_box.check_b6_team_points_equal_score),
    ("C1", checks_games.check_c1_status_rows_present), ("C2", checks_games.check_c2_starters_are_ten),
    ("C3", checks_games.check_c3_officials_at_least_three),
    ("D1", checks_shots.check_d1_shots_present), ("D2", checks_shots.check_d2_shots_class_share),
    ("D3", checks_shots.check_d3_shots_3pa_match), ("D4", checks_shots.check_d4_shots_unknown_share),
    ("D5", checks_shots.check_d5_shots_impossible_combo),
    ("E1", checks_lines.check_e1_line_coverage), ("E2", checks_lines.check_e2_spread_range),
    ("E3", checks_lines.check_e3_spread_health), ("E4", checks_lines.check_e4_total_range),
    ("E5", checks_lines.check_e5_total_health), ("E6", checks_lines.check_e6_price_point_provenance),
    ("E7", checks_lines.check_e7_line_flip),
]


def _today() -> date:
    return date.today()


def classify_d2(f: Finding, entry: Optional[dict]) -> Finding:
    """D2 is judged on the exact-match share, not on a count: it may drop D2_DROP_ALLOWED points."""
    out = dataclasses.replace(f, detail=dict(f.detail))
    out.baseline = entry.get("n_bad") if entry else None
    if f.severity == "info":
        out.status = "info"
    elif f.detail.get("n_comparable", f.n_total) == 0:
        out.status = "ok"
    elif entry is None:
        out.status = "ok" if f.n_bad == 0 else "new"
    elif entry.get("exact_pct") is not None and \
            entry["exact_pct"] - f.detail["exact_pct"] > D2_DROP_ALLOWED + 1e-9:
        out.status = "worse"
    else:
        out.status = "known"
    return out


def run(run_sql, scope: Scope, baseline: dict, *, with_espn: bool = False,
        fetch_scoreboard: Optional[Callable] = None) -> List[Finding]:
    since_mode = scope.since is not None
    findings: List[Finding] = []
    checks = list(REGISTRY)
    for _, fn in checks:
        findings.extend(fn(run_sql, scope))
    if with_espn:
        findings.extend(check_a1_espn_event_coverage(run_sql, scope, fetch_scoreboard))
    out = []
    for f in findings:
        if since_mode:
            rate = baseline["rates"].get(f"{f.check_id}|{f.scope.split('/', 1)[-1]}")
            entry = {"recent_rate": rate} if rate else None
        else:
            entry = baseline["entries"].get(baseline_key(f.check_id, f.scope))
        if f.check_id == "D2" and not since_mode:
            out.append(classify_d2(f, entry))
        else:
            out.append(classify(f, entry, since_mode=since_mode))
    return out


def since_date(since: str, as_of: Optional[str], today: date) -> date:
    """Window start: the later of the baseline date and today minus N days."""
    m = re.fullmatch(r"(\d+)d", since or "")
    if not m:
        raise ValueError(f"--since must look like 7d, got {since!r}")
    window = today - timedelta(days=int(m.group(1)))
    return max(window, date.fromisoformat(as_of)) if as_of else window


def build_baseline(findings: List[Finding], as_of: date) -> dict:
    """Known problems measured now. Entries only for problems that exist (info is never
    baselined); rates hold the latest complete season's bad-rate per (check, season type)."""
    entries, groups = {}, {}
    for f in findings:
        if f.severity == "info" or "/" not in f.scope:
            continue
        if f.n_bad > 0 or (f.check_id == "D2" and f.detail.get("n_comparable", 0) >= checks_shots.MIN_COMPARABLE):
            entry = {"n_bad": f.n_bad, "why": f"measured {as_of.isoformat()}"}
            if f.check_id == "D2":
                entry["exact_pct"] = f.detail["exact_pct"]
            entries[baseline_key(f.check_id, f.scope)] = entry
        season, stype = f.scope.split("/", 1)
        if re.fullmatch(r"\d{4}-\d{2}", season):
            groups.setdefault((f.check_id, stype), []).append((season, f))
    rates = {}
    for (check, stype), items in groups.items():
        biggest = max(f.n_total for _, f in items)
        complete = [(s, f) for s, f in items if f.n_total >= 0.9 * biggest and f.n_total > 0]
        if complete:
            _, latest = max(complete, key=lambda x: x[0])
            rate = latest.n_bad / latest.n_total
            if rate > 0:
                rates[f"{check}|{stype}"] = rate
    return {"as_of_date": as_of.isoformat(), "entries": entries, "rates": rates}


def write_baseline(path: Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8",
                          newline="\n")


def _print(findings: List[Finding]) -> None:
    shown = [f for f in findings if f.status not in ("ok", "info") or f.n_bad]
    print(f"{'check':<6}{'scope':<24}{'bad/total':>16}  status")
    for f in sorted(shown, key=lambda x: (x.check_id, x.scope)):
        print(f"{f.check_id:<6}{f.scope:<24}{f'{f.n_bad}/{f.n_total}':>16}  {f.status}"
              f"{'' if f.baseline is None else f' (baseline {f.baseline})'}")
    bad = [f for f in findings if f.status in ("new", "worse")]
    print(f"{len(findings)} findings, {len(bad)} new or worse")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m jobs.quality", description="Read-only data quality checks")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="every season (default)")
    g.add_argument("--seasons", help="comma-separated, e.g. 2025-26,2026-27")
    ap.add_argument("--since", help="only games after max(baseline date, today - N days), e.g. 7d")
    ap.add_argument("--with-espn", action="store_true", help="also run A1 (needs the network)")
    ap.add_argument("--fail-on", choices=("error", "warn", "none"), default="warn")
    ap.add_argument("--json", help="write findings to this file")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--write-baseline", help="write the measured problems to this local file")
    ap.add_argument("--verify-flags", action="store_true",
                    help="check that game_quality()'s SQL flags equal derive_flags() on the same counts")
    return ap


def _execute(args) -> int:
    baseline = load_baseline(Path(args.baseline))
    conn = connect_readonly()
    try:
        run_sql = make_run(conn)
        seasons = [s.strip() for s in args.seasons.split(",") if s.strip()] if args.seasons else None
        since = since_date(args.since, baseline["as_of_date"], _today()) if args.since else None
        scope = Scope(seasons, since)
        if args.verify_flags:
            bad = verify_flags(game_quality(run_sql, scope))
            print(f"verify-flags: {len(bad)} games differ" + (f": {', '.join(bad[:20])}" if bad else ""))
            return 1 if bad else 0
        if since is not None:
            extra, params = scope_where(scope)
            n_new = run_sql("/* qc:NEW */ SELECT count(*) AS n FROM public.games_v2 g "
                            "WHERE g.status = 'final'" + extra, params)
            if int(n_new.iloc[0]["n"]) == 0:
                print(f"無新資料 (no finished games after {since.isoformat()})")
                return 0
        findings = run(run_sql, scope, baseline, with_espn=args.with_espn)
        _print(findings)
        if args.json:
            Path(args.json).write_text(json.dumps([dataclasses.asdict(f) for f in findings], indent=1,
                                                  default=str, ensure_ascii=False), encoding="utf-8")
        if args.write_baseline:
            asof = run_sql("/* qc:ASOF */ SELECT max(game_date_et) AS d FROM public.games_v2 "
                           "WHERE status = 'final'", ()).iloc[0]["d"]
            write_baseline(Path(args.write_baseline), build_baseline(findings, asof))
            return 0          # measuring a baseline accepts the current state
        return exit_code(findings, args.fail_on)
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.write_baseline and args.since:
        print("--write-baseline needs a full run (no --since): a window would record partial counts.")
        return 2
    try:
        return _execute(args)
    except Exception as exc:  # message may carry a connection string: print the class only
        print(f"Quality check failed ({type(exc).__name__}); see the job log.")
        return 2
