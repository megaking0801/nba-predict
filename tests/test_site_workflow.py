"""Text-level checks for .github/workflows/site.yml.

PyYAML is not in requirements.txt (CI installs only that), so no yaml import.
"""
from __future__ import annotations

import re
from pathlib import Path

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "site.yml"


def _text() -> str:
    return WF.read_text(encoding="utf-8")


def test_secrets_only_in_build_site_step():
    lines = _text().splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(r"\s*- name:\s*Build site\s*$", ln)]
    assert len(starts) == 1
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*- ", lines[i])),
               len(lines))
    secret_lines = [i for i, ln in enumerate(lines) if "secrets." in ln]
    assert secret_lines, "build step needs the DB secrets"
    assert all(start < i < end for i in secret_lines), "secrets must stay in the Build site step"


def test_no_ref_on_checkout():
    text = _text()
    for forbidden in ("head_sha", "head_branch", "ref:"):
        assert forbidden not in text


def test_least_privilege():
    text = _text()
    assert re.search(r"^permissions:\s*\n\s+contents:\s*read", text, re.M)
    assert text.count("pages: write") == 1
    assert text.index("pages: write") > text.index("\n  deploy:")
    assert text.count("id-token: write") == 1


def test_build_timeout():
    assert "timeout-minutes: 15" in _text()


def test_triggers():
    text = _text()
    assert "workflow_run:" in text and 'workflows: ["daily_nba"]' in text
    assert "types: [completed]" in text
    assert "fail_build" in text
    for path in ("webui/**", ".github/workflows/site.yml", "jobs/teams.py", "jobs/model.py",
                 "jobs/db_utils.py", "requirements.txt", "tests/**"):
        assert path in text


def test_deploy_needs_build_and_has_id():
    text = _text()
    assert "needs: build" in text
    assert "id: deployment" in text
    assert "name: github-pages" in text


def test_concurrency_does_not_cancel_deploys():
    text = _text()
    assert "group: pages" in text and "cancel-in-progress: false" in text


def test_builds_with_python_311_and_pinned_requirements():
    text = _text()
    assert 'python-version: "3.11"' in text
    assert "pip install -r requirements.txt" in text
