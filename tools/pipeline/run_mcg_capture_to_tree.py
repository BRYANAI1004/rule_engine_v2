#!/usr/bin/env python3
"""
MCG orchestration: CareWeb canonical capture → validated nested rule-tree JSON.

- Canonical URLs only (--url-status forced to canonical inside capture invocation).
- No Supabase seed, no evaluator/frontend/LLM in this workflow.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# Forbidden phrases wrongly promoted onto admission path titles (beyond source-tree skips).
_BAD_ADMISSION_PATH_SUBSTRINGS: tuple[str, ...] = (
    "extended stay",
    "goal length of stay",
    "optimal recovery course",
    "failure to meet discharge",
    "inpatient admission and alternatives",
)

_EVIDENCE_TEXT_MARKERS = (
    "view abstract",
    "pubmed abstract",
    "doi:",
    "[context link]",
)

_BAD_CK_HINTS_EVIDENCE = (
    "footnote",
    "reference",
    "citation",
    "pubmed",
    "abstract_only",
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for base in [here.parent, *here.parents]:
        if (base / "tools" / "capture" / "capture_mcg.py").is_file():
            return base
    return here.parents[2]


def _truthy_optional_flag(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    v = str(value).strip().lower()
    return v not in {"", "false", "0", "no", "n", "off"}


def _norm_mcg_code(code: str) -> str:
    return str(code or "").strip().upper()


def _read_json_optional(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, f"missing:{path.name}"
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


def _classify_capture(
    *,
    manifest: Optional[dict[str, Any]],
    manifest_err: Optional[str],
    preflight: Optional[dict[str, Any]],
    preflight_err: Optional[str],
) -> tuple[str, str]:
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


def _load_manifest(path: Path) -> list[dict[str, Any]]:
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
        url = str(item.get("url", "")).strip()
        if not url:
            raise ValueError(f"manifest[{mc}] missing url")
        # Quick Search forbidden for this runner; normalize non-canonical to canonical semantics.
        us = str(item.get("url_status", "canonical")).strip().lower()
        if us not in ("canonical", "search_page"):
            us = "canonical"
        if us == "search_page":
            raise ValueError(f"manifest[{mc}]: search_page capture is disabled in this runner (canonical URL only)")
        entries.append({"mcg_code": mc, "title": str(item.get("title", "") or mc).strip() or mc, "url": url})
    return entries


def _mirror_audit(src: Path, audit_dir: Path) -> Optional[Path]:
    if not src.is_file():
        return None
    audit_dir.mkdir(parents=True, exist_ok=True)
    dst = audit_dir / src.name
    dst.write_bytes(src.read_bytes())
    return dst


def _prior_hashed_admission_leaf_count(domain_json_path: Path, mcg_code: str) -> int:
    """Count hashed atomic admission leaves immediately before rebuilding that domain artifact."""
    if not domain_json_path.is_file():
        return -1
    try:
        dj = json.loads(domain_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return -1
    mc = _norm_mcg_code(str(dj.get("mcg_code") or mcg_code))
    pref = f"{mc}.admission."
    leaf_pfx = f"leaf_{mc.lower()}_"
    n = 0
    for row in dj.get("logic_nodes") or []:
        if not isinstance(row, dict):
            continue
        dn = str(row.get("linked_domain_node_id") or "")
        if not dn.startswith(pref):
            continue
        if str(row.get("node_kind") or "") != "atomic":
            continue
        ck = str(row.get("condition_key") or "")
        if ck.startswith(leaf_pfx):
            n += 1
    return n


def _run(repo: Path, cmd: list[str]) -> int:
    proc = subprocess.run(cmd, cwd=str(repo), check=False)
    return int(proc.returncode)


def _semantic_domain_errors(domain: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    mcg = str(domain.get("mcg_code") or "")
    for d in domain.get("domain_nodes") or []:
        if not isinstance(d, dict):
            continue
        if str(d.get("node_type") or "") != "admission_path" or str(d.get("domain") or "") != "admission":
            continue
        blob = (
            str(d.get("original_text") or "") + " " + str(d.get("description") or "") + " " + str(d.get("name") or "")
        ).casefold()
        for frag in _BAD_ADMISSION_PATH_SUBSTRINGS:
            if frag in blob:
                errs.append(f"Admission path {_bad_path_label(d)} contains forbidden substring {frag!r}")
                break
        if "complications documented" in blob and ("inpatient complication" in blob or "complication milestones" in blob):
            errs.append(f"Admission path {_bad_path_label(d)} looks complications / milestone scaffolding")
        if ("hospitalization" in blob or "hospitalisation" in blob) and "admission is indicated" not in blob:
            if (
                any(
                    s in blob
                    for s in (
                        "milestone",
                        "level of bed",
                        "alternatives",
                        "optimal ",
                    )
                )
                or blob.count("hospitalization") >= 2
            ):
                errs.append(f"Admission path {_bad_path_label(d)} mentions hospitalization in a non‑admission‑indicator way")
    for ln in domain.get("logic_nodes") or []:
        if not isinstance(ln, dict):
            continue
        dn = str(ln.get("linked_domain_node_id") or "")
        if not dn.startswith(f"{mcg}.admission."):
            continue
        ck = str(ln.get("condition_key") or "").strip()
        if ck and any(h in ck for h in _BAD_CK_HINTS_EVIDENCE):
            errs.append(
                f"Admission logic {ln.get('logic_node_id')}: evidence-like condition_key derived from citations: {ck!r}",
            )
    return errs


_ck_pattern = re.compile(r"^[a-z][a-z0-9_]*$")


def _semantic_linked_leaf_issues(linked: dict[str, Any]) -> list[str]:
    errs: list[str] = []

    allowed_status = frozenset(
        {
            "linked_shared_definition",
            "linked_shared_atomic_definition",
            "unlinked",
            "not_applicable",
        },
    )

    for n in linked.get("linked_logic_nodes") or []:
        if not isinstance(n, dict):
            continue
        nk = str(n.get("node_kind") or "")
        ck = str(n.get("condition_key") or "").strip()
        st = str(n.get("definition_link_status") or "")
        lid = str(n.get("logic_node_id") or "")

        if nk != "atomic":
            continue

        if not ck:
            errs.append(f"Atomic logic {lid}: missing condition_key")
            continue

        if not _ck_pattern.fullmatch(ck):
            errs.append(f"Leaf logic {lid}: invalid condition_key shape {ck!r}")

        if st not in allowed_status:
            errs.append(f"Leaf logic {lid}: unclassified linkage status {st!r}")
            continue

        if st == "not_applicable":
            errs.append(f"Leaf logic {lid}: atomic node tagged not_applicable")

        if ck and any(h in ck for h in _BAD_CK_HINTS_EVIDENCE):
            errs.append(f"Leaf logic {lid}: evidence/meta token leaked into condition_key {ck!r}")

    return errs


def _classification_dashboard(linked_doc: dict[str, Any], shared_doc: dict[str, Any]) -> dict[str, Any]:
    la = linked_doc.get("audit") if isinstance(linked_doc.get("audit"), dict) else {}

    lk_by_id = {
        str(n.get("logic_node_id")): n
        for n in (linked_doc.get("linked_logic_nodes") or [])
        if isinstance(n, dict) and n.get("logic_node_id")
    }

    evid = 0
    details = la.get("admission_unlinked_details") if isinstance(la.get("admission_unlinked_details"), list) else []
    for row in details:
        if not isinstance(row, dict):
            continue
        nid = str(row.get("logic_node_id") or "")
        n = lk_by_id.get(nid)
        ot = str((n or {}).get("original_text") or "").casefold()
        if any(m.casefold() in ot for m in _EVIDENCE_TEXT_MARKERS):
            evid += 1

    admission_unlinked = int(la.get("admission_unlinked_condition_ref_count") or 0)
    atoms = shared_doc.get("atomic_rules") or []
    needs_review_atoms = sum(
        1 for a in atoms if isinstance(a, dict) and str(a.get("review_status") or "") == "needs_review"
    )
    conds_need = sum(
        1 for c in (shared_doc.get("conditions") or []) if isinstance(c, dict) and str(c.get("review_status") or "") == "needs_review"
    )

    dangling = la.get("dangling_shared_definition_refs") or []

    src_only_approx = max(0, admission_unlinked - evid)

    return dict(
        linked_shared_definition_count=int(la.get("linked_shared_definition_ref_count") or 0),
        linked_shared_atomic_definition_count=int(la.get("linked_shared_atomic_definition_ref_count") or 0),
        unlinked_condition_ref_count=int(la.get("unlinked_condition_ref_count") or 0),
        admission_unlinked_condition_ref_count=admission_unlinked,
        source_only_approx_count=src_only_approx,
        intentionally_unlinked_evidence_only_count=evid,
        needs_review_atom_instances=needs_review_atoms,
        needs_review_condition_stub_instances=conds_need,
        dangling_ref_count=len(dangling) if isinstance(dangling, list) else int(bool(dangling)),
    )


def _bad_path_label(d: dict[str, Any]) -> str:
    return str(d.get("node_id") or d.get("description") or "?")


def _domain_metrics(domain: dict[str, Any], mcg: str) -> tuple[bool, bool, int, int]:
    dom_nodes = domain.get("domain_nodes") or []
    adm_root = False
    dis_root = False
    for dn in dom_nodes:
        if not isinstance(dn, dict):
            continue
        nid = str(dn.get("node_id") or "")
        if nid == f"{mcg}.admission":
            adm_root = True
        if nid == f"{mcg}.discharge":
            dis_root = True

    admission_paths = sum(
        1
        for dn in dom_nodes
        if isinstance(dn, dict)
        and str(dn.get("node_type") or "") == "admission_path"
        and str(dn.get("domain") or "") == "admission"
    )
    discharge_paths = sum(
        1
        for dn in dom_nodes
        if isinstance(dn, dict)
        and str(dn.get("domain") or "") == "discharge"
        and dn.get("level") == 3
        and dn.get("logic_root_id")
    )
    return adm_root, dis_root, admission_paths, discharge_paths


def _aggregate_counts(shared: dict[str, Any], linked_audit: dict[str, Any]) -> dict[str, Any]:
    conds = shared.get("conditions") or []
    atoms = shared.get("atomic_rules") or []
    comps = shared.get("composite_definitions") or []
    dangling = linked_audit.get("dangling_shared_definition_refs") or []
    return dict(
        condition_count=len([c for c in conds if isinstance(c, dict)]),
        atomic_rule_count=len([a for a in atoms if isinstance(a, dict)]),
        composite_count=len([c for c in comps if isinstance(c, dict)]),
        linked_shared_definition_ref_count=int(linked_audit.get("linked_shared_definition_ref_count") or 0),
        linked_shared_atomic_definition_ref_count=int(linked_audit.get("linked_shared_atomic_definition_ref_count") or 0),
        unlinked_count=int(linked_audit.get("unlinked_condition_ref_count") or 0),
        dangling_refs=list(dangling) if isinstance(dangling, list) else [],
    )


@dataclass
class RowResult:
    mcg_code: str
    title: str
    url: str
    capture_classification: str
    capture_skip_reason: str
    capture_ran_browser: bool
    capture_ec: Optional[int]
    audit_preflight_ec: Optional[int]

    validate_source_ec: Optional[int] = None
    validate_domain_ec: Optional[int] = None
    validate_shared_ec: Optional[int] = None
    validate_linked_ec: Optional[int] = None

    semantic_errors: list[str] = field(default_factory=list)

    admission_root_found: bool = False
    discharge_root_found: bool = False
    admission_path_count: int = 0
    discharge_path_count: int = 0
    admission_unlinked_condition_ref_count: int = 0

    aggregate: dict[str, Any] = field(default_factory=dict)
    classification_counts: dict[str, Any] = field(default_factory=dict)

    ready_for_parse: bool = False
    ready_for_db: bool = False

    export_pdf_requested: bool = False
    export_pdf_ec: Optional[int] = None
    pdf_rel: Optional[str] = None
    html_rel: Optional[str] = None

    failed_step: Optional[str] = None
    defs_count: int = 0
    popups_count: int = 0

    def unlinked_count_for_table(self) -> int:
        return int(self.aggregate.get("unlinked_count") or 0)


def _build_row_for_step_failure(
    base: RowResult,
    *,
    failed_step: str,
    errs: Iterable[str],
) -> RowResult:
    base.failed_step = failed_step
    base.semantic_errors = list(base.semantic_errors) + list(errs)
    base.ready_for_parse = False
    base.ready_for_db = False
    return base


def _vz(ec: Optional[int]) -> bool:
    """Treat unset validator exits (None) as N/A/pass for earlier pipeline aborts."""

    return ec is None or ec == 0


def _finalize_ready_flags(rr: RowResult) -> RowResult:
    rr.ready_for_parse = (
        rr.capture_classification == "pass"
        and _vz(rr.validate_source_ec)
        and _vz(rr.validate_domain_ec)
        and _vz(rr.validate_shared_ec)
        and _vz(rr.validate_linked_ec)
        and not rr.semantic_errors
        and rr.failed_step is None
    )
    rr.ready_for_db = rr.ready_for_parse
    return rr


def process_entry(
    repo: Path,
    *,
    mcg_code: str,
    title: str,
    url: Optional[str],
    skip_capture_flag: bool,
    force_capture: bool,
    export_pdf: bool,
    scope_for_export: str,
    stop_on_failure: bool,
    py: Path,
    raw_dir: Path,
    audits_dir: Path,
    tree_dir: Path,
    domain_dir: Path,
    shared_dir: Path,
    linked_dir: Path,
    preview_dir: Path,
) -> tuple[RowResult, bool]:
    mc = _norm_mcg_code(mcg_code)
    ttl = title or mc

    rr = RowResult(
        mcg_code=mc,
        title=ttl,
        url=str(url or ""),
        capture_classification="",
        capture_skip_reason="",
        capture_ran_browser=False,
        capture_ec=None,
        audit_preflight_ec=None,
        export_pdf_requested=export_pdf,
    )

    cap_manifest_p = raw_dir / f"{mc}.capture-manifest.json"
    pref_p = audits_dir / f"{mc}.capture-completeness.preflight.json"

    should_skip_browser = skip_capture_flag
    if skip_capture_flag:
        rr.capture_skip_reason = "cli_skip_capture"
    elif not force_capture:
        mf, mf_err = _read_json_optional(cap_manifest_p)
        pf, pf_err = _read_json_optional(pref_p)
        cls, rationale = _classify_capture(manifest=mf, manifest_err=mf_err, preflight=pf, preflight_err=pf_err)
        if mf is not None and cls == "pass":
            should_skip_browser = True
            rr.capture_skip_reason = "existing_bundle_pass_without_force_capture"

    if not should_skip_browser:
        if not url:
            rr = _build_row_for_step_failure(
                rr,
                failed_step="capture_missing_url",
                errs=["capture requires manifest url or --url when not skipping capture"],
            )
            rr.capture_classification = "failed"
            return _finalize_ready_flags(rr), stop_on_failure

        cap_cmd = [
            str(py),
            str(repo / "tools/capture/capture_mcg.py"),
            "--url",
            str(url),
            "--mcg-code",
            mc,
            "--title",
            ttl,
            "--out-prefix",
            mc,
            "--url-status",
            "canonical",
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
            "line",
        ]
        rr.capture_ran_browser = True
        rr.capture_ec = _run(repo, cap_cmd)

    mf, mf_err = _read_json_optional(cap_manifest_p)
    rr.defs_count, rr.popups_count = _counts_from_manifest(mf)

    aud_cmd = [str(py), str(repo / "tools/capture/audit_capture_completeness.py"), "--mcg-code", mc]
    rr.audit_preflight_ec = _run(repo, aud_cmd)

    pf, pf_err = _read_json_optional(pref_p)
    cls, rat = _classify_capture(manifest=mf, manifest_err=mf_err, preflight=pf, preflight_err=pf_err)
    rr.capture_classification = cls
    if cls != "pass":
        rr = _build_row_for_step_failure(rr, failed_step="capture_or_preflight", errs=[rat or cls])
        return _finalize_ready_flags(rr), stop_on_failure

    exp_html = raw_dir / f"{mc}.full.expanded.html"
    if not exp_html.is_file():
        rr = _build_row_for_step_failure(rr, failed_step="missing_expanded_html", errs=[str(exp_html)])
        return _finalize_ready_flags(rr), stop_on_failure

    st_cmd = [
        str(py),
        str(repo / "tools/parse/build_mcg_source_tree.py"),
        "--mcg-code",
        mc,
        "--title",
        ttl,
        "--expanded-html",
        str(exp_html),
        "--out-dir",
        str(tree_dir),
    ]
    if _run(repo, st_cmd) != 0:
        rr = _build_row_for_step_failure(rr, failed_step="build_source_tree", errs=["exit non‑zero"])
        return _finalize_ready_flags(rr), stop_on_failure

    st_json = tree_dir / f"{mc}.source-tree.v1.json"
    _mirror_audit(tree_dir / f"{mc}.source-tree.audit.json", audits_dir)
    vv_st = [
        str(py),
        str(repo / "tools/parse/validate_mcg_source_tree.py"),
        "--input",
        str(st_json),
    ]
    rr.validate_source_ec = _run(repo, vv_st)
    if rr.validate_source_ec != 0:
        rr.failed_step = rr.failed_step or "validate_source_tree"
        return _finalize_ready_flags(rr), stop_on_failure

    dom_json = domain_dir / f"{mc}.domain-rule-tree.v1.json"
    prior_hashed_leaf_count = _prior_hashed_admission_leaf_count(dom_json, mc)

    dom_cmd = [
        str(py),
        str(repo / "tools/parse/build_mcg_domain_rule_tree.py"),
        "--input",
        str(st_json),
        "--mcg-code",
        mc,
        "--title",
        ttl,
        "--out-dir",
        str(domain_dir),
    ]
    if _run(repo, dom_cmd) != 0:
        rr = _build_row_for_step_failure(rr, failed_step="build_domain_rule_tree", errs=["exit non‑zero"])
        return _finalize_ready_flags(rr), stop_on_failure

    dom_doc = json.loads(dom_json.read_text(encoding="utf-8"))
    _mirror_audit(domain_dir / f"{mc}.domain-rule-tree.audit.json", audits_dir)
    vv_dom = [
        str(py),
        str(repo / "tools/parse/validate_domain_rule_tree.py"),
        "--input",
        str(dom_json),
    ]
    rr.validate_domain_ec = _run(repo, vv_dom)
    if rr.validate_domain_ec != 0:
        rr.failed_step = rr.failed_step or "validate_domain_rule_tree"
        sem = _semantic_domain_errors(dom_doc)
        rr.semantic_errors.extend(sem)
        return _finalize_ready_flags(rr), stop_on_failure

    sem_dom = _semantic_domain_errors(dom_doc)
    rr.semantic_errors.extend(sem_dom)

    defs_json = raw_dir / f"{mc}.definitions.raw.json"
    if not defs_json.is_file():
        rr = _build_row_for_step_failure(rr, failed_step="missing_definitions_json", errs=[str(defs_json)])
        return _finalize_ready_flags(rr), stop_on_failure

    sh_cmd = [
        str(py),
        str(repo / "tools/parse/build_mcg_definition_rule_tree.py"),
        "--mcg-code",
        mc,
        "--title",
        ttl,
        "--definitions-json",
        str(defs_json),
        "--domain-rule-tree",
        str(dom_json),
        "--out-dir",
        str(shared_dir),
    ]
    if _run(repo, sh_cmd) != 0:
        rr = _build_row_for_step_failure(rr, failed_step="build_shared_definitions", errs=["exit non‑zero"])
        return _finalize_ready_flags(rr), stop_on_failure

    sh_json = shared_dir / f"{mc}.shared-condition-definitions.v1.json"
    sh_doc = json.loads(sh_json.read_text(encoding="utf-8"))
    _mirror_audit(shared_dir / f"{mc}.shared-condition-definitions.audit.json", audits_dir)

    vv_sh = [str(py), str(repo / "tools/parse/validate_mcg_definition_rule_tree.py"), "--input", str(sh_json)]
    rr.validate_shared_ec = _run(repo, vv_sh)
    if rr.validate_shared_ec != 0:
        rr.failed_step = rr.failed_step or "validate_shared_definitions"
        return _finalize_ready_flags(rr), stop_on_failure

    lk_cmd = [
        str(py),
        str(repo / "tools/parse/build_mcg_linked_rule_tree.py"),
        "--mcg-code",
        mc,
        "--domain-rule-tree",
        str(dom_json),
        "--shared-definitions",
        str(sh_json),
        "--out-dir",
        str(linked_dir),
    ]
    if _run(repo, lk_cmd) != 0:
        rr = _build_row_for_step_failure(rr, failed_step="build_linked_rule_tree", errs=["exit non‑zero"])
        return _finalize_ready_flags(rr), stop_on_failure

    lk_json = linked_dir / f"{mc}.linked-rule-tree.v1.json"
    lk_doc = json.loads(lk_json.read_text(encoding="utf-8"))

    rr.semantic_errors.extend(_semantic_linked_leaf_issues(lk_doc))

    lk_audit = lk_doc.get("audit") or {}
    rr.admission_unlinked_condition_ref_count = int(lk_audit.get("admission_unlinked_condition_ref_count") or 0)
    vv_lk = [
        str(py),
        str(repo / "tools/parse/validate_mcg_linked_rule_tree.py"),
        "--input",
        str(lk_json),
        "--shared-definitions",
        str(sh_json),
        "--repo-root",
        str(repo),
    ]
    rr.validate_linked_ec = _run(repo, vv_lk)
    if rr.validate_linked_ec != 0:
        rr.failed_step = rr.failed_step or "validate_linked_rule_tree"

    rr.admission_root_found, rr.discharge_root_found, rr.admission_path_count, rr.discharge_path_count = _domain_metrics(
        dom_doc,
        mc,
    )
    rr.aggregate = _aggregate_counts(sh_doc, lk_audit)
    rr.classification_counts = _classification_dashboard(lk_doc, sh_doc)

    linkage_audit_ec: int | None = None
    if _norm_mcg_code(mc) == "M190" and rr.validate_linked_ec == 0:
        aq_out = audits_dir / f"{mc}.definition-linkage-quality.audit.json"
        aq_cmd = [
            str(py),
            str(repo / "tools/audit/audit_definition_linkage_quality.py"),
            "--repo-root",
            str(repo),
            "--mcg-code",
            mc,
            "--domain-rule-tree",
            str(dom_json),
            "--definitions-raw-json",
            str(defs_json),
            "--shared-definitions",
            str(sh_json),
            "--linked-condition-refs-jsonl",
            str(linked_dir / f"{mc}.linked-condition-refs.jsonl"),
            "--prior-hashed-leaf-count",
            str(int(prior_hashed_leaf_count)),
            "--out-json",
            str(aq_out),
            "--out-md",
            str(aq_out.with_suffix(".md")),
        ]
        linkage_audit_ec = _run(repo, aq_cmd)
        _mirror_audit(aq_out, audits_dir)
        if linkage_audit_ec != 0:
            rr.failed_step = rr.failed_step or "definition_linkage_quality_audit"

    if rr.semantic_errors:
        rr.failed_step = rr.failed_step or "semantic_policy"

    if export_pdf:
        pdf_out = preview_dir / f"{mc}.integrated-rule-hierarchy.pdf"
        lk_refs = linked_dir / f"{mc}.linked-condition-refs.jsonl"
        ex_cmd = [
            str(py),
            str(repo / "tools/export/export_mcg_integrated_admission_pdf.py"),
            "--mcg-code",
            mc,
            "--mcg-title",
            ttl,
            "--domain-rule-tree",
            str(dom_json),
            "--shared-definitions",
            str(sh_json),
            "--linked-condition-refs",
            str(lk_refs),
            "--output",
            str(pdf_out),
            "--scope",
            scope_for_export,
        ]
        rr.export_pdf_ec = _run(repo, ex_cmd)

        rr.pdf_rel = str(pdf_out.resolve().relative_to(repo.resolve()))
        rr.html_rel = str(pdf_out.with_suffix(".html").resolve().relative_to(repo.resolve()))
        if rr.export_pdf_ec != 0:
            rr.failed_step = rr.failed_step or "export_pdf"
    else:
        rr.export_pdf_ec = None

    rr = _finalize_ready_flags(rr)
    halt = stop_on_failure and not rr.ready_for_db
    return rr, halt


def _write_pipeline_artifacts(rr: RowResult, repo: Path, audits_dir: Path) -> None:
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    dangling = rr.aggregate.get("dangling_refs") or []

    metrics: dict[str, Any] = {
        "admission_root_found": rr.admission_root_found,
        "discharge_root_found": rr.discharge_root_found,
        "admission_path_count": rr.admission_path_count,
        "discharge_path_count": rr.discharge_path_count,
        "ready_for_parse": rr.ready_for_parse,
        "ready_for_db": rr.ready_for_db,
        "dangling_refs": dangling,
        "classification": rr.classification_counts,
        **dict(rr.aggregate or {}),
    }

    doc = dict(
        schema_version="mcg.capture_to_tree.pipeline.v1",
        generated_at_iso=iso,
        **asdict(rr),
        metrics=metrics,
    )

    mj = audits_dir / f"{rr.mcg_code}.capture-to-tree.pipeline.json"
    audits_dir.mkdir(parents=True, exist_ok=True)
    mj.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# {rr.mcg_code} capture → tree pipeline",
        "",
        f"- **Generated**: `{iso}`",
        f"- Capture classification: **{rr.capture_classification}**"
        + (f" (_{rr.capture_skip_reason}_)" if rr.capture_skip_reason else ""),
        f"- **Ready for DB**: `{rr.ready_for_db}`",
        f"- Admission paths: `{rr.admission_path_count}`, discharge sections: `{rr.discharge_path_count}`",
        f"- Semantic issues: `{len(rr.semantic_errors)}`",
        "",
    ]

    cc = rr.classification_counts or {}
    if cc:
        lines.extend(
            [
                "## Classification / linkage rollup",
                "",
                f"- `linked_shared_definition_count`: {cc.get('linked_shared_definition_count')}",
                f"- `linked_shared_atomic_definition_count`: {cc.get('linked_shared_atomic_definition_count')}",
                f"- `unlinked_condition_ref_count`: {cc.get('unlinked_condition_ref_count')}",
                f"- `admission_unlinked_condition_ref_count`: {cc.get('admission_unlinked_condition_ref_count')}",
                f"- `source_only_approx_count`: {cc.get('source_only_approx_count')}",
                f"- `intentionally_unlinked_evidence_only_count`: {cc.get('intentionally_unlinked_evidence_only_count')}",
                f"- Review atoms (`needs_review`): {cc.get('needs_review_atom_instances')}",
                f"- Review condition stubs: {cc.get('needs_review_condition_stub_instances')}",
                "",
            ],
        )

    if rr.semantic_errors:
        lines.append("## Semantic / policy violations")
        lines.extend(f"- `{e}`" for e in rr.semantic_errors)
        lines.append("")

    agg = rr.aggregate
    lines.extend(
        [
            "## Aggregate metrics",
            "",
            f"- `condition_count`: {agg.get('condition_count')}",
            f"- `atomic_rule_count`: {agg.get('atomic_rule_count')}",
            f"- `composite_count`: {agg.get('composite_count')}",
            f"- Linked shared defs: `{agg.get('linked_shared_definition_ref_count')}`, "
            f"linked atomics: `{agg.get('linked_shared_atomic_definition_ref_count')}`, "
            f"unlinked: `{agg.get('unlinked_count')}`",
            f"- Dangling shared refs in linkage audit: `{len(agg.get('dangling_refs') or [])}`",
            "",
        ],
    )

    mj.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_scope_for_export(scope: str) -> str:
    raw = scope.strip().lower().replace("-", "_")
    if raw in ("admission", "discharge", "admission_discharge"):
        return raw
    if raw == "admission discharge":
        return "admission_discharge"
    return raw


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--manifest",
        dest="manifest",
        default=None,
        help='JSON manifest path (canonical URLs only); e.g. rules/mcg/batch/mcg_capture_manifest.top11_canonical.json"',
    )
    p.add_argument("--mcg-code", dest="single_mcg", default=None, help='Single‑MCG mode, e.g. "M085"')
    p.add_argument("--mcg-title", dest="single_title", default=None, help="Title for single‑MCG mode")
    p.add_argument(
        "--url",
        dest="single_url",
        default=None,
        help="Canonical CareWeb URL (required when not using --manifest/--skip‑capture)",
    )
    p.add_argument(
        "--skip-capture",
        default="false",
        dest="skip_capture",
        help="Skip Playwright capture; reuse existing bundle under raw-html/",
    )
    p.add_argument(
        "--force-capture",
        default="false",
        dest="force_capture",
        help="Always run capture (ignore existing passing bundle shortcuts)",
    )
    p.add_argument(
        "--export-pdf",
        default="true",
        dest="export_pdf",
        help="Emit integrated hierarchy PDF/HTML (true/false, default true)",
    )
    p.add_argument(
        "--stop-on-failure",
        default="false",
        dest="stop_on_failure",
        help="Stop batch immediately when the first guideline hard‑fails a step",
    )
    p.add_argument(
        "--scope",
        default="admission_discharge",
        dest="scope",
        help="Export renderer scope (admission_discharge | admission | discharge)",
    )
    p.add_argument(
        "--audit-aggregate-prefix",
        default="top11",
        dest="audit_prefix",
        help="Stem for aggregated summary files under audits/<prefix>.capture-to-tree.summary.*",
    )

    ns = p.parse_args(argv)
    manifest = getattr(ns, "manifest", None)
    if ns.single_mcg and manifest:
        p.error("--mcg-code and --manifest cannot be combined")

    skip_cap = _truthy_optional_flag(ns.skip_capture)

    need_url = manifest is None and ns.single_mcg and not skip_cap and getattr(ns, "single_url", None) in (
        None,
        "",
    )

    need_title = manifest is None and ns.single_mcg is not None and not getattr(ns, "single_title", None)

    if need_url:
        p.error("--url is required in single‑MCG mode unless --skip-capture is true")

    if need_title:
        p.error("--mcg-title required for single‑MCG mode")

    if not manifest and not ns.single_mcg:
        p.error("Provide --manifest … or (--mcg-code and --mcg-title)")

    return ns


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_cli(sys.argv[1:] if argv is None else argv)
    repo = _repo_root()
    py = Path(sys.executable)
    audits_dir = repo / "rules/mcg/audits"
    raw_dir = repo / "rules/mcg/raw-html"
    tree_dir = repo / "rules/mcg/source-trees"
    domain_dir = repo / "rules/mcg/domain-trees"
    shared_dir = repo / "rules/mcg/shared-condition-definitions"
    linked_dir = repo / "rules/mcg/linked-rule-trees"
    preview_dir = repo / "rules/mcg/previews"

    manifest_path_opt: Optional[Path] = None
    entries: list[dict[str, Any]] = []
    audit_prefix = str(args.audit_prefix or "top11")

    skip_capture_flag = _truthy_optional_flag(args.skip_capture)
    force_capture = _truthy_optional_flag(args.force_capture)
    export_pdf = _truthy_optional_flag(args.export_pdf)
    stop_on_failure = _truthy_optional_flag(args.stop_on_failure)
    scope_export = _parse_scope_for_export(args.scope.strip())

    if args.manifest:
        manifest_path_opt = Path(args.manifest).expanduser()
        if not manifest_path_opt.is_absolute():
            manifest_path_opt = (repo / manifest_path_opt).resolve()
        entries = _load_manifest(manifest_path_opt)
    else:
        mc = _norm_mcg_code(str(args.single_mcg))
        entries = [
            dict(
                mcg_code=mc,
                title=str(args.single_title or mc),
                url=str(args.single_url or "").strip(),
            ),
        ]

    aggregate_rows: list[RowResult] = []
    hard_stop = False

    for et in entries:
        rr, halt_batch = process_entry(
            repo,
            mcg_code=et["mcg_code"],
            title=et["title"],
            url=et.get("url") or "",
            skip_capture_flag=skip_capture_flag,
            force_capture=force_capture,
            export_pdf=export_pdf,
            scope_for_export=scope_export,
            stop_on_failure=stop_on_failure,
            py=py,
            raw_dir=raw_dir,
            audits_dir=audits_dir,
            tree_dir=tree_dir,
            domain_dir=domain_dir,
            shared_dir=shared_dir,
            linked_dir=linked_dir,
            preview_dir=preview_dir,
        )
        _write_pipeline_artifacts(rr, repo, audits_dir)
        aggregate_rows.append(rr)
        if stop_on_failure and halt_batch:
            hard_stop = True
            break

    ready_db = sorted({r.mcg_code for r in aggregate_rows if r.ready_for_db})
    needs_review = sorted({r.mcg_code for r in aggregate_rows if not r.ready_for_db})

    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary_blob: dict[str, Any] = {
        "schema_version": "mcg.capture_to_tree.batch_summary.v1",
        "generated_at_iso": iso,
        "manifest_path": str(manifest_path_opt.relative_to(repo)) if manifest_path_opt else "",
        "entries": [asdict(r) for r in aggregate_rows],
        "ready_for_db": ready_db,
        "needs_review": needs_review,
    }
    summaries_dir = audits_dir
    summaries_dir.mkdir(parents=True, exist_ok=True)
    sjson = summaries_dir / f"{audit_prefix}.capture-to-tree.summary.json"
    smd = summaries_dir / f"{audit_prefix}.capture-to-tree.summary.md"
    sjson.write_text(json.dumps(summary_blob, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tbl = "| MCG | capture | defs | admit paths | discharge L3 | linked | unlinked | PDF | DB ready |\n|---|---|---:|---:|---:|---:|---:|---|---|\n"
    for r in aggregate_rows:
        cap = ("✓ skip" if r.capture_skip_reason else ("✗" if r.capture_classification != "pass" else "✓"))
        pdf_cell = "`" + str(r.pdf_rel or "—") + "`" if export_pdf else "—"
        tbl += (
            f"| {r.mcg_code} | {cap} | {r.defs_count} | {r.admission_path_count} | "
            f"{r.discharge_path_count} | {r.aggregate.get('linked_shared_definition_ref_count')} + "
            f"{r.aggregate.get('linked_shared_atomic_definition_ref_count')} | {r.unlinked_count_for_table()} | "
            f"{pdf_cell} | {r.ready_for_db} |\n"
        )

    smd_body = ["# Capture → tree aggregate", "", f"- `_generated_: {iso}`", "", "## Table", "", tbl, ""]
    smd.write_text("\n".join(smd_body), encoding="utf-8")

    print(f"[Pipeline] Summary JSON → {sjson.relative_to(repo)}")
    if hard_stop:
        return 3
    if any(not r.ready_for_db for r in aggregate_rows):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
