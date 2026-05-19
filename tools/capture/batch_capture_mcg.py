#!/usr/bin/env python3
"""
Batch MCG capture + completeness preflight for multiple guidelines.

Runs capture_mcg.py per manifest entry, then audit_capture_completeness.py,
classifies PASS / NEEDS_REVIEW / FAILED, and writes a batch summary JSON + MD.

No parsing, Tree JSON, DB, frontend, evaluator, or LLM — capture + audit only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _repo_root(script_dir: Path) -> Path:
    return script_dir.parents[1]


def _truthy_optional_flag(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    v = str(value).strip().lower()
    return v not in {"", "false", "0", "no", "n", "off"}


def _norm_mcg_code(code: str) -> str:
    return str(code or "").strip().upper()


def _load_manifest_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"manifest must be a JSON array, got {type(data).__name__}")
    entries: list[dict[str, Any]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"manifest[{i}] must be an object")
        mc = _norm_mcg_code(str(item.get("mcg_code", "")))
        if not mc:
            raise ValueError(f"manifest[{i}] missing mcg_code")
        url_status_raw = str(item.get("url_status", "canonical")).strip().lower()
        if url_status_raw not in ("canonical", "search_page"):
            url_status_raw = "canonical"
        search_code_v = str(item.get("search_code", "")).strip()
        if search_code_v and url_status_raw == "canonical":
            url_status_raw = "search_page"
        if url_status_raw == "search_page" and not search_code_v:
            raise ValueError(
                f"manifest[{mc}] search_page capture requires non-empty search_code "
                f"(set search_code or use url_status canonical with an /isc/*.htm url)",
            )
        entries.append(
            {
                "mcg_code": mc,
                "title": str(item.get("title", "")).strip() or mc,
                "url": str(item.get("url", "")).strip(),
                "url_status": url_status_raw,
                "search_code": search_code_v,
            },
        )
        if not entries[-1]["url"]:
            raise ValueError(f"manifest[{mc}] missing url")
    return entries


def _parse_only_csv(raw: str) -> Optional[set[str]]:
    if not str(raw).strip():
        return None
    out: set[str] = set()
    for part in str(raw).split(","):
        c = _norm_mcg_code(part)
        if c:
            out.add(c)
    return out if out else None


def _read_json_optional(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, "missing_file"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"invalid_json:{exc.msg}"
    if isinstance(payload, dict):
        return payload, None
    return None, f"expected_object_got:{type(payload).__name__}"


def _summarize_need_review(preflight: Optional[dict[str, Any]]) -> str:
    if not preflight:
        return "preflight_missing_or_unreadable"
    reasons = preflight.get("reasons_review")
    if isinstance(reasons, list) and reasons:
        return str(reasons[0])
    denom = (((preflight.get("checks") or {}).get("definition_capture_counts")) or {}).get(
        "definition_count",
        None,
    )
    if isinstance(denom, int) and denom == 0:
        return "definition_count=0"
    return "needs_review_unknown"


def _failed_ratio_pct(preflight: Optional[dict[str, Any]]) -> Optional[float]:
    if not preflight:
        return None
    fp = (((preflight.get("checks") or {}).get("failure_pressure")) or {}).get("ratio_vs_processed_attempts")
    if fp is None:
        return None
    try:
        return round(float(fp) * 100.0, 1)
    except (TypeError, ValueError):
        return None


def _counts_from_manifest(manifest: Optional[dict[str, Any]]) -> tuple[int, int]:
    if not manifest:
        return 0, 0
    dc = manifest.get("definition_capture")
    if isinstance(dc, dict):
        defs = int(dc.get("definition_count") or 0)
        pops = int(dc.get("popup_count") or 0)
        return defs, pops
    return 0, 0


def _classify(
    *,
    manifest: Optional[dict[str, Any]],
    manifest_err: Optional[str],
    preflight: Optional[dict[str, Any]],
    preflight_err: Optional[str],
) -> tuple[str, str]:
    """
    Return (status_class, rationale_one_liner).

    pass: manifest capture_status pass AND preflight verdict PASS.
    failed: manifest missing / unreadable capture_status failed.
    needs_review: all other coherent outcomes.
    """
    if manifest is None:
        tip = manifest_err or "manifest_missing_after_capture"
        return "failed", tip

    ms_raw = manifest.get("capture_status")
    ms = str(ms_raw).strip().lower() if ms_raw is not None else ""

    if ms == "failed":
        reasons = manifest.get("capture_status_reasons")
        r0 = str(reasons[0]) if isinstance(reasons, list) and reasons else "capture_failed"
        return "failed", r0

    if preflight is None:
        pe = preflight_err or "preflight_missing_after_audit"
        if ms == "pass":
            return "needs_review", pe
        if ms == "needs_review":
            return "needs_review", pe
        return "needs_review", pe

    pv = str(preflight.get("verdict") or "").strip().upper()

    if ms == "needs_review":
        csr = manifest.get("capture_status_reasons")
        if isinstance(csr, list) and csr:
            return "needs_review", str(csr[0])
        return "needs_review", _summarize_need_review(preflight)

    if ms == "pass" and pv == "PASS":
        return "pass", ""

    if pv == "NEEDS_REVIEW":
        return "needs_review", _summarize_need_review(preflight)

    return "needs_review", f"manifest_status={ms or 'unset'} verdict={pv or 'unset'}"


def _capture_cmd(repo_root: Path, entry: dict[str, Any]) -> list[str]:
    script = repo_root / "tools/capture/capture_mcg.py"
    cmd: list[str] = [
        sys.executable,
        str(script),
        "--url",
        entry["url"],
        "--mcg-code",
        entry["mcg_code"],
        "--title",
        entry["title"],
        "--out-prefix",
        entry["mcg_code"],
        "--url-status",
        str(entry.get("url_status", "canonical")),
        "--capture-definitions",
        "true",
        "--max-definition-triggers",
        "500",
        "--definition-recursion-depth",
        "3",
        "--definition-click-timeout-ms",
        "3000",
        "--progress",
        "true",
        "--progress-style",
        "bar",
    ]
    if str(entry.get("url_status", "canonical")) == "search_page":
        cmd.extend(["--search-code", str(entry.get("search_code", "")).strip()])
    return cmd


def _audit_cmd(repo_root: Path, mcg: str) -> list[str]:
    script = repo_root / "tools/capture/audit_capture_completeness.py"
    return [sys.executable, str(script), "--mcg-code", mcg]


def _banner_label(entry: dict[str, Any]) -> str:
    t = str(entry.get("title", "")).strip()
    mc = entry["mcg_code"]
    if not t:
        return mc
    return f"{mc} {t}"


def _run_subprocess(repo_root: Path, cmd: list[str]) -> int:
    proc = subprocess.run(cmd, cwd=str(repo_root), check=False)
    return int(proc.returncode)


@dataclass
class RowResult:
    mcg_code: str
    title: str
    url: str
    status: str
    capture_exit_code: int
    audit_exit_code: int
    manifest_capture_status: str = ""
    preflight_verdict: str = ""
    rationale: str = ""
    manifest_path: str = ""
    preflight_path: str = ""
    definition_count: int = 0
    popup_count: int = 0
    failed_trigger_ratio_pct: Optional[float] = None


def _write_summary_md(
    repo_root: Path,
    paths: tuple[Path, Path],
    blob_head: dict[str, Any],
    rows: list[RowResult],
) -> None:
    def rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(repo_root.resolve()))
        except ValueError:
            return str(p)

    out_json, out_md = paths
    total = blob_head["total"]
    counts = blob_head["counts"]
    md_lines = [
        "# Batch capture summary",
        "",
        f"- **Generated**: `{blob_head['generated_at']}`",
        f"- **Manifest**: `{blob_head['manifest_path']}`",
        "",
        "## Per guideline",
        "",
        "| MCG | Status | defs | popups | fail ratio | rationale |",
        "| --- | ------ | ----- | ------ | ---------- | --------- |",
    ]
    for r in rows:
        fr = ""
        if r.failed_trigger_ratio_pct is not None:
            fr = f"{r.failed_trigger_ratio_pct}%"
        rat = (r.rationale or "").replace("|", "/")
        md_lines.append(
            f"| `{r.mcg_code}` | **{str(r.status).upper()}** | {r.definition_count} | {r.popup_count} | "
            f"{fr} | {rat[:240]} |",
        )
    md_lines.extend(
        [
            "",
            "## Aggregate",
            "",
            f"- Total: **{total}**",
            f"- PASS: **{counts['pass']}**",
            f"- NEEDS_REVIEW: **{counts['needs_review']}**",
            f"- FAILED: **{counts['failed']}**",
            "",
            f"- Ready for parse: `{blob_head['ready']}`",
            f"- Needs review: `{blob_head['review']}`",
            f"- Failed: `{blob_head['fail']}`",
            "",
            f"- Summary JSON: `{rel(out_json)}`",
            f"- Summary MD: `{rel(out_md)}`",
            "",
        ],
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    sd = _script_dir()
    root = _repo_root(sd)
    default_manifest = root / "rules/mcg/batch/mcg_capture_manifest.remaining.json"

    p = argparse.ArgumentParser(description="Batch capture + preflight audit for multiple MCG guidelines.")
    p.add_argument(
        "--manifest",
        default=str(default_manifest),
        help="JSON array manifest path (default: rules/mcg/batch/mcg_capture_manifest.remaining.json)",
    )
    p.add_argument("--dry-run", default="false", help="Print planned commands only; do not capture or write outputs.")
    p.add_argument(
        "--stop-on-failure",
        default="false",
        help="If true, stop the batch after the first FAILED classification.",
    )
    p.add_argument("--limit", type=int, default=0, help="Process at most N guidelines after filtering (0 = no cap).")
    p.add_argument(
        "--only",
        default="",
        help='Comma-separated subset of mcg codes, e.g. "M083,M282". Empty = full filtered manifest.',
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    sd = _script_dir()
    repo_root = _repo_root(sd)

    manifest_path = Path(args.manifest).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = (repo_root / manifest_path).resolve()
    manifest_rel_for_report = ""
    try:
        manifest_rel_for_report = str(manifest_path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        manifest_rel_for_report = str(manifest_path)

    dry_run = _truthy_optional_flag(args.dry_run)
    stop_on_failure = _truthy_optional_flag(args.stop_on_failure)
    limit = max(0, int(args.limit or 0))
    only = _parse_only_csv(str(args.only or ""))

    try:
        entries = _load_manifest_entries(manifest_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"[Batch] fatal: manifest: {exc}", flush=True)
        return 2

    if only is not None:
        entries = [e for e in entries if e["mcg_code"] in only]
    if limit > 0:
        entries = entries[:limit]

    total_batch = len(entries)
    audit_dir = repo_root / "rules/mcg/audits"
    raw_html_dir = repo_root / "rules/mcg/raw-html"
    summary_json_path = audit_dir / "batch-capture-summary.json"
    summary_md_path = audit_dir / "batch-capture-summary.md"

    rows: list[RowResult] = []

    if total_batch == 0:
        print("[Batch] no entries after filters (--only / empty manifest)", flush=True)
        if dry_run:
            print("[Batch] dry-run complete (no captures, no summary files)", flush=True)
            return 0
        counts = {"pass": 0, "needs_review": 0, "failed": 0}
        slim = {
            "schema_version": "mcg_batch_capture_summary.v1",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "manifest_path": manifest_rel_for_report,
            "cli": {
                "dry_run": dry_run,
                "stop_on_failure": stop_on_failure,
                "limit": limit or None,
                "only": str(args.only or ""),
                "manifest": str(args.manifest),
            },
            "counts": {"total": 0, **counts},
            "ready_for_parse": [],
            "needs_review": [],
            "failed": [],
            "results": [],
        }
        summary_json_path.parent.mkdir(parents=True, exist_ok=True)
        summary_json_path.write_text(json.dumps(slim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary_md_path.write_text("# Batch capture summary\n\nNo entries processed.\n", encoding="utf-8")
        _print_footer(0, counts, [], [], [], summary_json_path, summary_md_path, repo_root)
        return 0

    for idx, entry in enumerate(entries, start=1):
        mc = entry["mcg_code"]
        label = _banner_label(entry)
        cap_cmd = _capture_cmd(repo_root, entry)
        aud_cmd = _audit_cmd(repo_root, mc)

        if dry_run:
            print(f"[Batch] {idx}/{total_batch} {label} | capture running", flush=True)
            print(f"          capture: {' '.join(cap_cmd)}", flush=True)
            print(f"          audit: {' '.join(aud_cmd)}", flush=True)
            continue

        print(f"[Batch] {idx}/{total_batch} {label} | capture running", flush=True)
        cap_ec = _run_subprocess(repo_root, cap_cmd)

        if cap_ec != 0:
            print(f"[Batch] capture exit {cap_ec} (continuing to audit)", flush=True)
        print(f"[Batch] {idx}/{total_batch} {label} | audit running", flush=True)
        aud_ec = _run_subprocess(repo_root, aud_cmd)

        manifest_path_actual = raw_html_dir / f"{mc}.capture-manifest.json"
        preflight_path_actual = audit_dir / f"{mc}.capture-completeness.preflight.json"

        manifest, man_err = _read_json_optional(manifest_path_actual)
        preflight, pre_err = _read_json_optional(preflight_path_actual)

        status, rationale = _classify(
            manifest=manifest,
            manifest_err=man_err,
            preflight=preflight,
            preflight_err=pre_err,
        )

        defs_c, pops_c = _counts_from_manifest(manifest)
        ratio_pct = _failed_ratio_pct(preflight)

        ms_disp = ""
        if manifest:
            ms_disp = str(manifest.get("capture_status") or "")
        pv_disp = ""
        if preflight:
            pv_disp = str(preflight.get("verdict") or "")

        row = RowResult(
            mcg_code=mc,
            title=str(entry["title"]),
            url=str(entry["url"]),
            status=status,
            capture_exit_code=cap_ec,
            audit_exit_code=aud_ec,
            manifest_capture_status=ms_disp,
            preflight_verdict=pv_disp,
            rationale=rationale,
            manifest_path=str(manifest_path_actual.relative_to(repo_root)),
            preflight_path=str(preflight_path_actual.relative_to(repo_root)),
            definition_count=defs_c,
            popup_count=pops_c,
            failed_trigger_ratio_pct=ratio_pct,
        )
        rows.append(row)

        detail = ""
        if status == "pass":
            fr = ratio_pct if ratio_pct is not None else 0.0
            detail = (
                f"PASS | definitions {defs_c} | popups {pops_c} | "
                f"failed ratio {'{:.1f}'.format(fr)}%"
            )
        elif status == "needs_review":
            detail = f"NEEDS_REVIEW | reason: {rationale}"
        else:
            detail = f"FAILED | reason: {rationale}"

        print(f"[Batch] {idx}/{total_batch} {label} | {detail}", flush=True)

        if stop_on_failure and status == "failed":
            print("[Batch] stop-on-failure: halting remaining guidelines", flush=True)
            break

    if dry_run:
        print("", flush=True)
        print("[Batch] dry-run complete (no captures, no summary files)", flush=True)
        return 0

    agg_total = len(rows)
    counts = {"pass": 0, "needs_review": 0, "failed": 0}
    ready: list[str] = []
    review_l: list[str] = []
    failed_l: list[str] = []
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status == "pass":
            ready.append(r.mcg_code)
        elif r.status == "needs_review":
            review_l.append(r.mcg_code)
        else:
            failed_l.append(r.mcg_code)

    blob: dict[str, Any] = {
        "schema_version": "mcg_batch_capture_summary.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "manifest_path": manifest_rel_for_report,
        "cli": {
            "dry_run": dry_run,
            "stop_on_failure": stop_on_failure,
            "limit": limit or None,
            "only": str(args.only or ""),
            "manifest": str(args.manifest),
        },
        "criteria": {
            "ready_for_parse": "manifest.capture_status pass AND preflight.verdict PASS",
            "exclude_parse": "needs_review or failed — do not proceed to JSON conversion",
        },
        "counts": {"total": agg_total, **counts},
        "ready_for_parse": sorted(ready),
        "needs_review": sorted(review_l),
        "failed": sorted(failed_l),
        "results": [asdict(r) for r in rows],
    }
    summary_json_path.parent.mkdir(parents=True, exist_ok=True)
    summary_json_path.write_text(json.dumps(blob, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    head = {
        "generated_at": blob["generated_at"],
        "manifest_path": manifest_rel_for_report,
        "total": agg_total,
        "counts": counts,
        "ready": sorted(ready),
        "review": sorted(review_l),
        "fail": sorted(failed_l),
    }
    _write_summary_md(repo_root, (summary_json_path, summary_md_path), head, rows)

    _print_footer(agg_total, counts, sorted(ready), sorted(review_l), sorted(failed_l), summary_json_path, summary_md_path, repo_root)
    return 0


def _print_footer(
    total: int,
    counts: dict[str, int],
    ready: list[str],
    review_l: list[str],
    failed_l: list[str],
    summary_json: Path,
    summary_md: Path,
    repo_root: Path,
) -> None:
    def rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(repo_root.resolve()))
        except ValueError:
            return str(p)

    print("", flush=True)
    print("================ BATCH CAPTURE SUMMARY ================", flush=True)
    print(f"Total: {total}", flush=True)
    print(f"PASS: {counts.get('pass', 0)}", flush=True)
    print(f"NEEDS_REVIEW: {counts.get('needs_review', 0)}", flush=True)
    print(f"FAILED: {counts.get('failed', 0)}", flush=True)
    print(f"Ready for parse: {ready}", flush=True)
    print(f"Needs review: {review_l}", flush=True)
    print(f"Failed: {failed_l}", flush=True)
    print(f"Summary JSON: {rel(summary_json)}", flush=True)
    print(f"Summary MD: {rel(summary_md)}", flush=True)
    print("=======================================================", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
