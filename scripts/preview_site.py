"""Render the static site from the test fakes (no DB) for a visual check.

    python scripts/preview_site.py [--out data_local/site-preview]

Writes <out>/light (as served) and <out>/dark (same pages with <html data-theme="dark">, which the CSS
treats like a dark system setting). Open <out>/light/index.html in a browser; use a narrow window or
the device toolbar for the phone width. data_local/ is git-ignored.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import fakes  # noqa: E402
from webui import build_site  # noqa: E402

NOW = dt.datetime(2026, 10, 23, 1, 0, tzinfo=dt.timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data_local" / "site-preview"))
    args = ap.parse_args(argv)
    out = Path(args.out)
    light, dark = out / "light", out / "dark"
    for p in (light, dark):
        if p.exists():
            shutil.rmtree(p)
    build_site.build(fakes.scenario_board().run, light, NOW)
    shutil.copytree(light, dark)
    for page in dark.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        page.write_text(text.replace('<html lang="zh-Hant">', '<html lang="zh-Hant" data-theme="dark">', 1),
                        encoding="utf-8")
    print(f"light: {light / 'index.html'}\ndark:  {dark / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
