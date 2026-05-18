#!/usr/bin/env python3
"""
MCG CareWeb HTML capture — user-facing CLI.

Uses a persistent Chromium profile (manual login). Outputs raw HTML, expanded
HTML/text, manifest, and summary under rules/mcg/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

# Same-directory imports when run as `python tools/capture/capture_mcg.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_core import run_capture  # noqa: E402


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Capture MCG CareWeb HTML into rules/mcg/ (authorized use only).")
    p.add_argument("--url", required=True, help="Target guideline URL")
    p.add_argument("--mcg-code", required=True, dest="mcg_code", help="e.g. M083")
    p.add_argument("--title", required=True, help='Human title, e.g. "Stroke: Ischemic"')
    p.add_argument("--out-prefix", required=True, dest="out_prefix", help="Output file prefix, e.g. M083")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return run_capture(
        mcg_code=args.mcg_code,
        mcg_title=args.title,
        url=args.url,
        out_prefix=args.out_prefix,
    )


if __name__ == "__main__":
    raise SystemExit(main())
