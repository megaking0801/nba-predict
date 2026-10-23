"""Text-level checks for .github/workflows/daily_nba.yml (no yaml import: CI lacks PyYAML)."""
from __future__ import annotations

import re
from pathlib import Path

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily_nba.yml"


def _text() -> str:
    return WF.read_text(encoding="utf-8")


def _block(start_regex: str) -> str:
    """Lines from the first line matching start_regex up to (not including) the next
    non-blank line indented no deeper than it."""
    lines = _text().splitlines()
    idx = next(i for i, ln in enumerate(lines) if re.search(start_regex, ln))
    indent = len(lines[idx]) - len(lines[idx].lstrip())
    out = [lines[idx]]
    for ln in lines[idx + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        out.append(ln)
    return "\n".join(out)


def test_crons():
    text = _text()
    for cron in ("30 10 * * *", "30 22 * * *", "30 0 * * *", "0 2 * * *", "0 12 * * 1"):
        assert f'cron: "{cron}"' in text
    assert '"0 22 * * *"' not in text


def test_legacy_preload_not_scheduled():
    step = _block(r"- name: Legacy daily preload")
    cond = next(ln for ln in step.splitlines() if ln.strip().startswith("if:"))
    assert "github.event.schedule" not in cond
    assert "workflow_dispatch" in cond


def test_daily_pipeline_still_scheduled_at_1030():
    cond = next(ln for ln in _block(r"- name: v2 daily pipeline").splitlines()
                if ln.strip().startswith("if:"))
    assert "'30 10 * * *'" in cond


def test_close_step_runs_close_mode_without_predict():
    step = _block(r"- name: v2 close snapshot")
    assert "jobs.run_daily --close" in step
    assert "predict_daily" not in step
    cond = next(ln for ln in step.splitlines() if ln.strip().startswith("if:"))
    for cron in ("'30 22 * * *'", "'30 0 * * *'", "'0 2 * * *'"):
        assert cron in cond
    assert "v2_lines" in cond


def test_no_other_step_predicts_on_schedule():
    for name in re.findall(r"- name: (.+)", _text()):
        if "predict" in name.lower() and "only" not in name.lower():
            raise AssertionError(f"unexpected predicting step: {name}")


def test_paper_mode_on():
    assert 'PAPER_MODE: "1"' in _text()


def test_season_default():
    text = _text()
    assert "2026-27" in text
    assert 'default: "2025-26"' not in text
    assert "|| '2025-26'" not in text


def test_keepalive_job():
    job = _block(r"^  keepalive:")
    assert "actions: write" in job
    assert "/actions/workflows/daily_nba.yml/enable" in job
    assert "secrets." not in job
    assert "'0 12 * * 1'" in job


def test_manual_ingest_never_writes_nba_format_ids():
    step = _block(r"- name: v2 ingest only")
    assert "jobs.ingest_espn" in step
    assert "ingest_games" not in step and "ingest_stats" not in step
