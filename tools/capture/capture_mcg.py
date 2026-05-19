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


def _truthy_optional_flag(value: str) -> bool:
    """Parse CLI boolean strings permissively."""

    v = str(value).strip().lower()

    return v not in {"", "false", "0", "no", "n", "off"}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Capture MCG CareWeb HTML into rules/mcg/ (authorized use only).")
    p.add_argument("--url", required=True, help="Target guideline URL")
    p.add_argument("--mcg-code", required=True, dest="mcg_code", help="e.g. M083")
    p.add_argument("--title", required=True, help='Human title, e.g. "Stroke: Ischemic"')
    p.add_argument("--out-prefix", required=True, dest="out_prefix", help="Output file prefix, e.g. M083")
    p.add_argument(
        "--capture-definitions",
        default="true",
        help="Whether to click glossary/definition triggers after Expand All (true/false). Default: true",
    )
    p.add_argument("--max-definition-triggers", type=int, default=300, help="Max trigger queue pops (default 300)")
    p.add_argument(
        "--definition-recursion-depth",
        type=int,
        default=2,
        help="Nested definition recursion depth (default 2)",
    )
    p.add_argument(
        "--definition-click-timeout-ms",
        type=int,
        default=3000,
        help="Popup detection budget per click (ms)",
    )
    p.add_argument(
        "--allow-needs-review-exit-zero",
        default="false",
        help="If true, exit 0 when capture_status is needs_review (failed always exits non-zero). Default: false",
    )
    p.add_argument(
        "--progress",
        default="true",
        help="Enable terminal progress UX (structured phase lines + live meters). true/false. Default: true",
    )
    p.add_argument(
        "--progress-style",
        default="bar",
        choices=("bar", "line", "silent"),
        help=(
            "Live progress rendering when --progress true: bar (TTY carriage-return), "
            "line (periodic lines; also auto when stdout is not a TTY), silent (final summary only). "
            "Default: bar"
        ),
    )
    p.add_argument(
        "--url-status",
        default="canonical",
        choices=("canonical", "search_page"),
        dest="url_status",
        help='How the URL is used: canonical (direct /isc/*.htm) or search_page (CareWeb shell + Quick Search)',
    )
    p.add_argument(
        "--search-code",
        default="",
        dest="search_code",
        help='Quick Search token (e.g. m180) when --url-status search_page',
    )
    p.add_argument(
        "--resolver-only",
        default="false",
        help="Open browser, verify target (canonical navigation or Quick Search), then exit without capturing. true/false.",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return run_capture(
        mcg_code=args.mcg_code,
        mcg_title=args.title,
        url=args.url,
        out_prefix=args.out_prefix,
        capture_definitions=_truthy_optional_flag(args.capture_definitions),
        max_definition_triggers=max(1, int(args.max_definition_triggers)),
        definition_recursion_depth=max(0, int(args.definition_recursion_depth)),
        definition_click_timeout_ms=max(250, int(args.definition_click_timeout_ms)),
        allow_needs_review_exit_zero=_truthy_optional_flag(args.allow_needs_review_exit_zero),
        progress_enabled=_truthy_optional_flag(args.progress),
        progress_style=str(args.progress_style or "bar").strip().lower(),
        url_status=str(args.url_status or "canonical").strip().lower(),
        search_code=str(args.search_code or "").strip() or None,
        resolver_only=_truthy_optional_flag(args.resolver_only),
    )


if __name__ == "__main__":
    raise SystemExit(main())
