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
    for cron in ("0 7 * * *", "30 22 * * *", "30 0 * * *", "0 2 * * *", "0 12 * * 1"):
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
    assert "'0 7 * * *'" in cond


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


def test_manual_backfill_uses_espn_seasons():
    step = _block(r"- name: v2 historical backfill")
    assert "jobs.ingest_espn --seasons" in step
    assert "jobs.backfill" not in step
    seasons = _block(r"^      seasons:")
    assert "(ESPN)" in seasons and 'default: "2026-27"' in seasons


# ---------- P3b: read-only data-quality steps ----------

def _cond(step: str) -> str:
    return next(ln for ln in step.splitlines() if ln.strip().startswith("if:"))


def test_quality_step_runs_after_daily_pipeline_even_on_failure():
    text = _text()
    pipeline = text.index("- name: v2 daily pipeline")
    quality = text.index("- name: v2 quality (read-only data checks)")
    assert quality > pipeline
    cond = _cond(_block(r"- name: v2 quality \(read-only data checks\)"))
    assert "!cancelled()" in cond and "always()" not in cond
    assert "github.event.schedule == '0 7 * * *'" in cond


def test_quality_step_command_and_fail_on():
    step = _block(r"- name: v2 quality \(read-only data checks\)")
    assert "python -u -m jobs.quality --since 7d --fail-on warn --ignore-types preseason" in step
    assert "--write-baseline" not in step and "--with-espn" not in step


def test_quality_not_on_close_or_train_schedules():
    for name in (r"- name: v2 quality \(read-only data checks\)", r"- name: v2 quality \(manual, full run\)"):
        cond = _cond(_block(name))
        for cron in ("'30 22 * * *'", "'30 0 * * *'", "'0 2 * * *'", "'0 12 * * 1'"):
            assert cron not in cond


def test_quality_manual_dispatch():
    text = _text()
    desc = text[text.index("description: >-"):text.index('required: true')]
    assert "v2_quality" in desc
    step = _block(r"- name: v2 quality \(manual, full run\)")
    assert "inputs.run == 'v2_quality'" in step
    assert "python -u -m jobs.quality --all --fail-on warn" in step
    assert "--write-baseline" not in step


def test_quality_is_not_part_of_run_daily():
    src = (Path(__file__).resolve().parents[1] / "jobs" / "run_daily.py").read_text(encoding="utf-8")
    assert "quality" not in src
