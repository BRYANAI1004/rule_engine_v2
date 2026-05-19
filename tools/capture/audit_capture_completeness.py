#!/usr/bin/env python3
"""
Preflight / regression audit for one MCG capture artifact bundle.

Reads expanded HTML (optional), manifest, definitions/popups JSON, and JSONL logs.
Emits PASS vs NEEDS_REVIEW with structured reasons — no browser, no Supabase.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _repo_root(script_dir: Path) -> Path:
    return script_dir.parents[1]


def _remaining_expand_looks_critical(previews: list[str]) -> bool:
    """True if leftover expand control label sits next to clinical section vocabulary."""

    for t in previews:
        if _warn_if_section_terms(t):
            return True
    return False


def _warn_if_section_terms(text: str) -> bool:
    low = text.lower()
    keys = ["admission", "clinical indications", "discharge", "discharge planning", "discharge destination"]
    return any(k in low for k in keys)


def _read_json(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, f"missing:{path}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid_json:{exc}"


def _count_nonempty_lines(path: Path) -> tuple[int, Optional[str]]:
    if not path.exists():
        return 0, f"missing:{path}"
    try:
        n = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        return n, None
    except Exception as exc:  # noqa: BLE001
        return 0, f"read_failed:{exc}"


def _file_nonempty(path: Path) -> tuple[bool, int, Optional[str]]:
    if not path.exists():
        return False, 0, "missing"
    try:
        raw = path.read_bytes()
        return len(raw) > 0, len(raw), None
    except Exception as exc:  # noqa: BLE001
        return False, 0, str(exc)


def _norm_blob(*parts: str) -> str:
    return " ".join(p.lower() for p in parts if p)


def run_capture_completeness_audit(
    *,
    mcg_code: str,
    expanded_html: Path,
    definitions_json: Path,
    popups_json: Path,
    manifest_path: Path,
    out_dir: Path,
    repo_root: Path,
    failed_trigger_ratio_warn: float = 0.40,
    failed_trigger_abs_warn: int = 120,
    write_files: bool = True,
) -> dict[str, Any]:
    """Return audit dict; optionally write JSON + Markdown next to other audits."""

    mcg = str(mcg_code).strip()
    reasons_pass: list[str] = []
    reasons_review: list[str] = []
    checks: dict[str, Any] = {}

    manifest, man_err = _read_json(manifest_path)
    defs_payload, defs_err = _read_json(definitions_json)
    pops_payload, pops_err = _read_json(popups_json)

    prefix = mcg
    triggers_path = manifest_path.parent / f"{prefix}.popup-triggers.jsonl"
    failures_path = manifest_path.parent / f"{prefix}.popup-failures.jsonl"
    def_audit_path = out_dir / f"{prefix}.definition-capture.audit.json"
    expanded_txt_path = manifest_path.parent / f"{prefix}.full.expanded.txt"
    screenshot_path_guess = repo_root / "rules" / "mcg" / "audits" / f"{prefix}.capture-full-page.png"

    html_ok, html_sz, html_err = _file_nonempty(expanded_html)
    txt_ok, txt_sz, txt_err = _file_nonempty(expanded_txt_path)

    checks["expanded_html"] = {"path": str(expanded_html), "nonempty": html_ok, "bytes": html_sz, "error": html_err}
    checks["expanded_txt"] = {"path": str(expanded_txt_path), "nonempty": txt_ok, "bytes": txt_sz, "error": txt_err}

    if not html_ok:
        reasons_review.append("expanded_html_missing_or_empty")
    else:
        reasons_pass.append("expanded_html_present")

    if not txt_ok:
        reasons_review.append("expanded_txt_missing_or_empty")
    else:
        reasons_pass.append("expanded_txt_present")

    if man_err:
        reasons_review.append(f"manifest:{man_err}")
        checks["manifest"] = {"error": man_err}
    else:
        checks["manifest"] = {"present": True, "path": str(manifest_path)}
        reasons_pass.append("manifest_present")

    expand_outcome = ""
    expand_summary: dict[str, Any] = {}
    remaining_expand = 0
    warnings_manifest: list[str] = []
    section_probe: dict[str, Any] = {}
    def_cap: dict[str, Any] = {}

    if manifest:
        expand_outcome = str(manifest.get("expand_phase_outcome") or "")
        expand_summary = manifest.get("expand_phase_summary") if isinstance(manifest.get("expand_phase_summary"), dict) else {}
        remaining_expand = int(manifest.get("remaining_expand_control_count") or 0)
        w = manifest.get("warnings")
        warnings_manifest = [str(x) for x in w] if isinstance(w, list) else []
        sp = manifest.get("section_probe")
        section_probe = sp if isinstance(sp, dict) else {}
        dc = manifest.get("definition_capture")
        def_cap = dc if isinstance(dc, dict) else {}

        ms_raw = manifest.get("capture_status")
        manifest_capture_status = str(ms_raw).strip().lower() if ms_raw is not None else ""
        mcsr = manifest.get("capture_status_reasons")
        manifest_capture_reasons: list[str] = [str(x) for x in mcsr] if isinstance(mcsr, list) else []
        checks["manifest_capture_status"] = {
            "capture_status": manifest_capture_status or "(absent)",
            "capture_status_reasons": manifest_capture_reasons,
        }
        if manifest_capture_status == "failed":
            reasons_review.append("manifest_capture_status_failed")
        elif manifest_capture_status == "needs_review":
            reasons_review.append("manifest_capture_status_needs_review")

    checks["expand_phase"] = {
        "expand_phase_outcome": expand_outcome,
        "expand_phase_summary": expand_summary,
        "remaining_expand_control_count": remaining_expand,
    }

    aborted_outcomes = ("aborted_unexpected_navigation", "aborted_consecutive_timeouts")
    ok_outcomes = ("ok", "stopped_no_progress", "stopped_stable_body", "stopped_max_passes")

    if expand_outcome in aborted_outcomes:
        reasons_review.append(f"expand_phase_aborted:{expand_outcome}")
    elif expand_outcome not in ok_outcomes and expand_outcome:
        reasons_review.append(f"expand_phase_unknown_outcome:{expand_outcome}")
    else:
        reasons_pass.append("expand_phase_bounded_ok")

    nav_hints = ("unexpected navigation", "navigation", "restore navigation failed")
    low_warns = " ".join(w.lower() for w in warnings_manifest)
    if any(h in low_warns for h in nav_hints):
        reasons_review.append("manifest_warnings_suggest_navigation_issues")

    # Residual expand controls (informational; critical if adjacent to admission — partial signal only)
    rem_controls = manifest.get("remaining_expand_controls") if manifest else []
    rem_texts = [str(x.get("text") or "") for x in rem_controls] if isinstance(rem_controls, list) else []
    checks["remaining_expand_controls_preview"] = rem_texts[:12]
    checks["remaining_expand_controls_count"] = remaining_expand
    if remaining_expand > 0 and _remaining_expand_looks_critical(rem_texts):
        reasons_review.append(f"residual_expand_controls_near_critical_section:{remaining_expand}")

    has_admission = bool(section_probe.get("has_admission")) if section_probe else False
    checks["admission_root"] = {"section_probe_has_admission": has_admission}
    if not has_admission:
        reasons_review.append("admission_section_marker_missing")

    if defs_err:
        reasons_review.append(f"definitions_json:{defs_err}")
        def_audit = {}
    else:
        reasons_pass.append("definitions_json_readable")

    if pops_err:
        reasons_review.append(f"popups_json:{pops_err}")
    else:
        reasons_pass.append("popups_json_readable")

    schema_version = str((defs_payload or {}).get("schema_version") or "")
    definition_count = int((defs_payload or {}).get("definition_count") or 0)
    popup_count = int((defs_payload or {}).get("popup_count") or (pops_payload or {}).get("popup_count") or 0)
    popup_def_triggers = int((defs_payload or {}).get("audit", {}).get("popup_definition_trigger_count", 0)) if isinstance((defs_payload or {}).get("audit"), dict) else 0

    trigger_lines, tl_err = _count_nonempty_lines(triggers_path)
    failure_lines, fl_err = _count_nonempty_lines(failures_path)

    checks["jsonl"] = {
        "popup_triggers": {"path": str(triggers_path), "lines": trigger_lines, "error": tl_err},
        "popup_failures": {"path": str(failures_path), "lines": failure_lines, "error": fl_err},
    }

    if tl_err:
        reasons_review.append(f"popup_triggers_jsonl:{tl_err}")
    else:
        reasons_pass.append("popup_triggers_jsonl_exists")

    if fl_err:
        reasons_review.append(f"popup_failures_jsonl:{fl_err}")
    else:
        reasons_pass.append("popup_failures_jsonl_exists")

    failed_trigger_count = 0
    candidate_trigger_count = 0
    clicked_tc = 0
    direct_tc = 0
    popup_detected_tc = 0
    def_audit: dict[str, Any] = {}

    da_disk, da_err = _read_json(def_audit_path)
    if isinstance(da_disk, dict):
        def_audit = da_disk
        failed_trigger_count = int(def_audit.get("failed_trigger_count") or 0)
        candidate_trigger_count = int(def_audit.get("candidate_trigger_count") or 0)
        clicked_tc = int(def_audit.get("clicked_trigger_count") or 0)
        direct_tc = int(def_audit.get("direct_invocation_count") or 0)
        popup_detected_tc = int(def_audit.get("popup_detected_count") or 0)
    elif defs_payload and isinstance(defs_payload.get("audit"), dict):
        def_audit = defs_payload["audit"]
        failed_trigger_count = int(def_audit.get("failed_trigger_count") or 0)
        candidate_trigger_count = int(def_audit.get("candidate_trigger_count") or 0)
        clicked_tc = int(def_audit.get("clicked_trigger_count") or 0)
        direct_tc = int(def_audit.get("direct_invocation_count") or 0)
        popup_detected_tc = int(def_audit.get("popup_detected_count") or 0)

    manifest_trigger_count = int(def_cap.get("trigger_count") or 0)
    processed_denom = manifest_trigger_count if manifest_trigger_count > 0 else max(
        1,
        failed_trigger_count + clicked_tc + direct_tc,
    )

    checks["definition_capture_counts"] = {
        "schema_version": schema_version,
        "definition_count": definition_count,
        "popup_count": popup_count,
        "popup_definition_trigger_count_seed": popup_def_triggers,
        "failed_trigger_count": failed_trigger_count,
        "candidate_trigger_count": candidate_trigger_count,
        "clicked_trigger_count": clicked_tc,
        "direct_invocation_count": direct_tc,
        "popup_detected_count": popup_detected_tc,
        "manifest_trigger_count_processed": manifest_trigger_count,
    }

    checks["manifest_definition_capture"] = {**def_cap, "note_trigger_count_is_processed_fingerprints": True}

    if def_cap.get("enabled") is False:
        reasons_review.append("definition_capture_disabled_in_manifest")

    if popup_def_triggers > 0 and definition_count <= 0:
        reasons_review.append("expected_definitions_but_count_zero")

    if schema_version.startswith("mcg_popup_capture.v2") and popup_count < definition_count:
        reasons_review.append("popup_count_lt_definition_count_schema_v2")

    fail_ratio_processed = failed_trigger_count / max(1, processed_denom)
    checks["failure_pressure"] = {
        "ratio_vs_processed_attempts": round(fail_ratio_processed, 4),
        "processed_attempt_denominator": processed_denom,
        "ratio_vs_gathered_candidates": round(failed_trigger_count / max(1, candidate_trigger_count), 4),
        "threshold_ratio": failed_trigger_ratio_warn,
    }

    if failed_trigger_count >= failed_trigger_abs_warn or fail_ratio_processed >= failed_trigger_ratio_warn:
        reasons_review.append(
            f"high_failed_trigger_pressure:count={failed_trigger_count},ratio_processed={fail_ratio_processed:.3f}",
        )

    if manifest_trigger_count <= 0 and def_cap.get("enabled") is True:
        reasons_review.append("zero_triggers_processed_while_definition_capture_enabled")

    # Screenshot (manifest path preferred)
    shot_rel = str(manifest.get("screenshot_path") or "") if manifest else ""
    shot_path = repo_root / shot_rel if shot_rel else screenshot_path_guess
    shot_ok, shot_sz, shot_err = _file_nonempty(Path(shot_path))
    checks["screenshot"] = {"path": str(shot_path), "nonempty": shot_ok, "bytes": shot_sz, "error": shot_err}
    if not shot_ok:
        reasons_review.append("screenshot_missing_or_empty")

    # M083-oriented regression sentinels (safe for any MCG: check presence in captured popup corpus)
    defs_blob = json.dumps(defs_payload or {}, ensure_ascii=False)
    pops_blob = json.dumps(pops_payload or {}, ensure_ascii=False)
    combined_lower = _norm_blob(defs_blob, pops_blob)

    sentinel_definitions = [
        "hemodynamic instability",
        "tachycardia",
        "hypotension",
        "orthostatic hypotension",
        "altered mental status",
        "hypoxemia",
        "dangerous arrhythmia",
        "cardiac arrhythmias of immediate concern",
    ]
    sentinel_terms = [
        "shock index",
        "mean arterial pressure",
        "vasopressor",
        "lactate",
        "spo2",
        "pao2",
        "ventricular tachycardia",
        "atrioventricular",
    ]

    sent_def_hits = {k: (k in combined_lower) for k in sentinel_definitions}
    sent_term_hits = {k: (k in combined_lower) for k in sentinel_terms}

    checks["regression_sentinels"] = {
        "mcg_code": mcg,
        "definitions": sent_def_hits,
        "terms": sent_term_hits,
        "definitions_all_true": all(sent_def_hits.values()),
        "terms_all_true": all(sent_term_hits.values()),
    }

    if mcg.upper() == "M083":
        if not all(sent_def_hits.values()):
            missing = [k for k, v in sent_def_hits.items() if not v]
            reasons_review.append(f"m083_regression_definition_sentinels_missing:{missing}")
        else:
            reasons_pass.append("m083_regression_definition_sentinels_ok")

        if not all(sent_term_hits.values()):
            missing_t = [k for k, v in sent_term_hits.items() if not v]
            reasons_review.append(f"m083_regression_term_sentinels_missing:{missing_t}")
        else:
            reasons_pass.append("m083_regression_term_sentinels_ok")

    verdict = "NEEDS_REVIEW" if reasons_review else "PASS"

    out = {
        "schema_version": "mcg_capture_completeness_preflight.v1",
        "mcg_code": mcg,
        "verdict": verdict,
        "reasons_pass": reasons_pass,
        "reasons_review": reasons_review,
        "checks": checks,
        "criteria_reference": {
            "PASS": [
                "expanded_html exists and non-empty",
                "expanded_txt exists and non-empty",
                "capture manifest exists",
                "admission root marker OR explicit waiver not implemented — uses Clinical Indications probe",
                "expand phase outcome ok / stopped_no_progress / stopped_stable_body / stopped_max_passes",
                "popup trigger JSONL exists",
                "popup failures JSONL exists (may be empty)",
                "definition_count > 0 when popup_definition seeds exist",
                "popup_count >= definition_count for schema v2",
                "failed_trigger_count below ratio/abs thresholds",
                "screenshot present",
                "M083: sentinel strings present in definitions+popups JSON",
            ],
            "NEEDS_REVIEW": [
                "definition_count = 0 with expected seeds",
                "trigger_count = 0",
                "many failed triggers",
                "admission marker missing",
                "residual Expand control labels adjacent to Admission / Clinical Indications / Discharge vocabulary",
                "unexpected navigation warnings",
                "key sentinel terms absent (M083 regression)",
            ],
            "failed_trigger_thresholds": {
                "ratio_processed_warn_if_gte": failed_trigger_ratio_warn,
                "failed_count_warn_if_gte": failed_trigger_abs_warn,
                "ratio_denominator_note": "manifest.definition_capture.trigger_count if > 0 else failed+clicked+direct",
            },
        },
    }

    if write_files:
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{mcg}.capture-completeness.preflight"
        jp = out_dir / f"{stem}.json"
        mp = out_dir / f"{stem}.md"
        jp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        mp.write_text(_render_completeness_md(out), encoding="utf-8")

    return out


def _render_completeness_md(audit: dict[str, Any]) -> str:
    lines = [
        f"# Capture completeness preflight — `{audit.get('mcg_code', '?')}`",
        "",
        f"**Verdict**: **{audit.get('verdict')}**",
        "",
        "## Reasons (PASS)",
        "",
    ]
    for r in audit.get("reasons_pass") or []:
        lines.append(f"- {r}")
    lines.extend(["", "## Reasons (NEEDS_REVIEW)", ""])
    rr = audit.get("reasons_review") or []
    if not rr:
        lines.append("- (none)")
    else:
        for r in rr:
            lines.append(f"- {r}")
    lines.extend(["", "## Key checks", "", "```json"])
    lines.append(json.dumps(audit.get("checks"), ensure_ascii=False, indent=2))
    lines.extend(["```", ""])
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    sd = _script_dir()
    root = _repo_root(sd)
    p = argparse.ArgumentParser(description="Preflight audit for one MCG capture artifact bundle.")
    p.add_argument("--mcg-code", required=True, dest="mcg_code")
    p.add_argument(
        "--expanded-html",
        default="",
        help=f"Path to *.full.expanded.html (default: rules/mcg/raw-html/{{mcg}}.full.expanded.html under repo)",
    )
    p.add_argument("--definitions-json", dest="definitions_json", default="")
    p.add_argument("--popups-json", dest="popups_json", default="")
    p.add_argument("--manifest", dest="manifest", default="")
    p.add_argument("--out-dir", dest="out_dir", default=str(root / "rules/mcg/audits"))
    _ = sd, root  # repo_root resolved again in main()
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    sd = _script_dir()
    root = _repo_root(sd)
    mcg = str(args.mcg_code).strip()
    raw_dir = root / "rules/mcg/raw-html"
    exp = (
        Path(args.expanded_html).expanduser().resolve()
        if str(args.expanded_html).strip()
        else (raw_dir / f"{mcg}.full.expanded.html")
    )
    defs_path = Path(args.definitions_json).expanduser().resolve() if args.definitions_json else (raw_dir / f"{mcg}.definitions.raw.json")
    pops_path = Path(args.popups_json).expanduser().resolve() if args.popups_json else (raw_dir / f"{mcg}.popups.raw.json")
    man_path = Path(args.manifest).expanduser().resolve() if args.manifest else (raw_dir / f"{mcg}.capture-manifest.json")
    out_dir = Path(args.out_dir).expanduser().resolve()

    audit = run_capture_completeness_audit(
        mcg_code=mcg,
        expanded_html=exp,
        definitions_json=defs_path,
        popups_json=pops_path,
        manifest_path=man_path,
        out_dir=out_dir,
        repo_root=root,
        write_files=True,
    )
    stem = f"{mcg}.capture-completeness.preflight"
    print(f"[preflight] verdict={audit.get('verdict')}", flush=True)
    print(f"[preflight] wrote: {out_dir / (stem + '.json')}", flush=True)
    print(f"[preflight] wrote: {out_dir / (stem + '.md')}", flush=True)
    return 0 if audit.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
