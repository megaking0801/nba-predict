"""Read-only data quality checks (P3a). See docs/superpowers/specs/2026-10-25-p3-data-quality-design.md."""
from jobs.quality.core import Finding, Scope, classify, exit_code, load_baseline

__all__ = ["Finding", "Scope", "classify", "exit_code", "load_baseline"]
