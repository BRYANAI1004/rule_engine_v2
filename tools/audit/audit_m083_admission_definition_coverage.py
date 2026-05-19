#!/usr/bin/env python3
"""Audit: M083 admission definition coverage (inventory only; does not change pipeline artifacts)."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Admission-only scope -------------------------------------------------
ADMISSION_ROOT = "M083.admission"


def _is_admission_domain_node(domain_node_id: str) -> bool:
    return domain_node_id == ADMISSION_ROOT or domain_node_id.startswith(ADMISSION_ROOT + ".")


NUMERIC_OPS = {">", "<", ">=", "<=", "=="}

# Boolean clinical leaves acceptable as extractor targets (controlled list).
DIRECT_BOOLEAN_LEAF_KEYS = frozenset(
    {
        "aphasia_condition_present",
        "gait_impairment_condition_present",
        "significant_limb_weakness_condition_present",
        "thrombolysis_or_thrombectomy_performed_or_planned_condition_present",
        "hemorrhagic_transformation_condition_present",
    }
)

# Recommended shared-definition build order (condition_key prefixes / keys).
NEXT_BUILD_PRIORITY: list[tuple[str, list[str]]] = [
    (
        "respiratory_abnormality / hypoxemia",
        ["respiratory_abnormality_condition_present", "hypoxemia_condition_present"],
    ),
    (
        "cardiac_arrhythmia_of_immediate_concern / dangerous_arrhythmia",
        [
            "cardiac_arrhythmia_of_immediate_concern_condition_present",
            "dangerous_arrhythmia_condition_present",
            "prolonged_cardiac_telemetry_monitoring_required",
        ],
    ),
    (
        "severe_hypertension",
        [
            "pediatric_severe_hypertension_threshold_condition_present",
            "severe_hypertension_condition_present",
        ],
    ),
    ("dysphagia_evaluation_required", ["dysphagia_evaluation_required"]),
    (
        "brain_imaging_finding_requiring_inpatient_care",
        ["brain_imaging_finding_requiring_inpatient_care_condition_present"],
    ),
    (
        "neurologic_worsening_monitoring_required",
        ["neurologic_worsening_monitoring_required"],
    ),
    (
        "clinically_significant_cardiac_or_vascular_disorder",
        ["clinically_significant_cardiac_or_vascular_disorder_condition_present"],
    ),
    (
        "cerebral_venous_thrombosis",
        ["cerebral_venous_thrombosis_condition_present"],
    ),
    ("suspected_vasculitis", ["suspected_vasculitis_condition_present"]),
]

NEXT_BUILD_CATEGORIES = frozenset(
    {
        "needs_shared_definition_popup_available",
        "needs_shared_definition_source_only",
    }
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _build_domain_index(
    domain_nodes: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    by_id: dict[str, dict[str, Any]] = {}
    admission_root: str | None = None
    for n in domain_nodes:
        nid = str(n.get("node_id") or "")
        if nid:
            by_id[nid] = n
        if n.get("node_type") == "guideline_domain_root" and n.get("domain") == "admission":
            admission_root = nid
    return by_id, admission_root


def admission_path_text(domain_node_id: str, by_id: dict[str, dict[str, Any]]) -> str:
    parts: list[str] = []
    cur: str | None = domain_node_id
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        node = by_id.get(cur)
        if not node:
            parts.append(cur)
            break
        label = str(node.get("description") or cur)
        parts.append(label)
        if cur == ADMISSION_ROOT:
            break
        cur = node.get("parent_node_id")
        if not cur:
            break
    parts.reverse()
    return " > ".join(parts)


def _norm_core_title(title: str) -> str:
    t = title.strip()
    if t.lower().startswith("definition -"):
        t = t[12:].strip()
    return t.lower()


def _collect_definition_records(
    definitions_json: dict[str, Any] | None, popups_json: dict[str, Any] | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if definitions_json:
        for d in definitions_json.get("definitions") or []:
            if str(d.get("popup_type") or "") == "definition":
                out.append(d)
    if popups_json:
        for p in popups_json.get("popups") or []:
            if str(p.get("popup_type") or "") == "definition":
                out.append(p)
    return out


# Tokens too generic to align a condition_key to a definition title by overlap alone.
_CK_STOPWORDS = frozenset(
    {
        "clinically",
        "significant",
        "severe",
        "acute",
        "chronic",
        "clinical",
        "finding",
        "present",
        "required",
    }
)


def _condition_meaning_tokens(condition_key: str) -> set[str]:
    base = condition_key
    for suf in ("_condition_present", "_required"):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    return {t for t in base.split("_") if len(t) >= 4}


def _find_matching_definition(
    condition_key: str,
    domain_text: str,
    def_records: list[dict[str, Any]],
) -> tuple[str | None, bool]:
    domain_lower = domain_text.lower()
    blob = f"{condition_key} {domain_text}".lower()
    blob_compact = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", blob)).strip()
    ck_tokens = _condition_meaning_tokens(condition_key)

    best_title: str | None = None

    for rec in def_records:
        title = str(rec.get("title") or "")
        if not title:
            continue
        core = _norm_core_title(title)
        if len(core) < 4:
            continue
        if core in domain_lower or core in blob:
            best_title = title
            break
        cw = [w for w in core.split() if len(w) >= 4]
        if len(cw) < 2:
            continue
        hits = sum(1 for w in cw if w in blob_compact)
        if hits < max(2, int(0.6 * len(cw))):
            continue
        core_word_set = set(core.split())
        aligned = (ck_tokens - _CK_STOPWORDS) & core_word_set
        if not aligned:
            continue
        best_title = title
        break

    return (best_title, best_title is not None)


def _is_pediatric_row(condition_key: str, domain_text: str, orig_text: str) -> bool:
    t = f"{condition_key} {domain_text} {orig_text}".lower()
    return "pediatric" in t or condition_key.startswith("pediatric_")


def _is_example_only_logic(logic: dict[str, Any] | None) -> bool:
    if not logic:
        return False
    return bool(logic.get("example_only"))


def _format_val(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _already_atomic_minimal(logic: dict[str, Any] | None) -> bool:
    if not logic or logic.get("node_kind") != "atomic":
        return False
    op = logic.get("operator")
    if op not in NUMERIC_OPS:
        return False
    val = logic.get("value")
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return True
    return False


def _classify_unlinked(
    *,
    condition_key: str,
    domain_original_text: str,
    logic: dict[str, Any] | None,
    captured_popup_title: str | None,
    captured_popup_available: bool,
    def_match_title: str | None,
    def_match_available: bool,
) -> tuple[str, str, str]:
    """Return (coverage_category, reason, recommended_next_action)."""

    orig = domain_original_text
    logic_orig = str(logic.get("original_text") or "") if logic else ""
    combined_example = _is_example_only_logic(logic)

    if combined_example:
        return (
            "needs_manual_review",
            "Logic node marked example_only; avoid strict boolean without review.",
            "manual_review_required",
        )

    if _is_pediatric_row(condition_key, orig, logic_orig):
        return (
            "needs_manual_review",
            "Pediatric threshold or wording present; needs explicit age modeling or policy.",
            "manual_review_required",
        )

    if condition_key in DIRECT_BOOLEAN_LEAF_KEYS:
        return (
            "direct_boolean_leaf_acceptable",
            "Direct clinical fact leaf; acceptable as boolean extractor target for now.",
            "keep_as_boolean_extractor_leaf",
        )

    if _already_atomic_minimal(logic):
        return (
            "already_atomic_minimal",
            "Numeric threshold atomic; rule-ready without shared composite.",
            "none_atomic_ready",
        )

    # Broad / contextual anchors
    if condition_key in (
        "acute_ischemic_stroke_condition_present",
        "clinical_stability_unclear_condition_present",
    ):
        return (
            "needs_manual_review",
            "Broad or stability-judgment phrase; insufficient deterministic structure for strict boolean.",
            "manual_review_required",
        )

    title_for_row = captured_popup_title or def_match_title
    popup_ok = bool(captured_popup_title) or def_match_available

    if popup_ok:
        return (
            "needs_shared_definition_popup_available",
            f"MCG definition popup text available ({title_for_row or 'matched title'}).",
            "build_shared_definition_next",
        )

    return (
        "needs_shared_definition_source_only",
        "Concept likely needs decomposition; no matching captured definition popup in inputs.",
        "inspect_popup_capture",
    )


def _shared_roots_by_condition(shared_doc: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in shared_doc.get("conditions") or []:
        ck = row.get("condition_key")
        rid = row.get("root_composite_id")
        if ck and rid:
            out[str(ck)] = str(rid)
    return out


def _shared_atomic_roots_by_condition(shared_doc: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in shared_doc.get("conditions") or []:
        if str(row.get("definition_type") or "") != "shared_atomic_condition":
            continue
        ck = row.get("condition_key")
        rid = row.get("root_atomic_id")
        if ck and rid:
            out[str(ck)] = str(rid)
    return out


def _linked_atomic_row_classification(
    ref: dict[str, Any],
    shared_atomic_roots: dict[str, str],
) -> tuple[str, str, str, str | None]:
    lck = ref.get("linked_definition_condition_key")
    root: str | None = ref.get("root_atomic_id")
    if not root and lck:
        root = shared_atomic_roots.get(str(lck))
    msg = "Linked to shared atomic definition."
    if str(ref.get("definition_link_status")) == "linked_shared_atomic_definition" and not root:
        msg = "linked_shared_atomic_definition status but root_atomic_id missing in ref; check shared JSON."
    return ("linked_shared_atomic_definition", msg, "none_already_linked", root)


def _linked_row_classification(
    ref: dict[str, Any],
    shared_roots: dict[str, str],
) -> tuple[str, str, str, str | None]:
    """For linked_shared_definition rows."""
    lck = ref.get("linked_definition_condition_key")
    root: str | None = ref.get("root_composite_id")
    if not root and lck:
        root = shared_roots.get(str(lck))
    msg = "Linked to shared composite definition."
    if str(ref.get("definition_link_status")) == "linked_shared_definition" and not root:
        msg = "linked_shared_definition status but root_composite_id missing in ref; check shared JSON."
    return ("linked_shared_definition", msg, "none_already_linked", root)


def run_audit(args: argparse.Namespace) -> int:
    mcg = args.mcg_code
    domain_path = Path(args.domain_rule_tree)
    refs_path = Path(args.linked_condition_refs)
    shared_path = Path(args.shared_definitions)
    defs_path = Path(args.definitions_json)
    popups_path = Path(args.popups_json) if args.popups_json else None

    domain_doc = _load_json(domain_path)
    logic_by_id: dict[str, dict[str, Any]] = {
        str(n["logic_node_id"]): n for n in (domain_doc.get("logic_nodes") or [])
    }
    by_domain_id, _adm = _build_domain_index(domain_doc.get("domain_nodes") or [])

    shared_doc = _load_json(shared_path)
    shared_roots = _shared_roots_by_condition(shared_doc)
    shared_atomic_roots = _shared_atomic_roots_by_condition(shared_doc)

    defs_doc = _load_json(defs_path) if defs_path.is_file() else None
    pops_doc = _load_json(popups_path) if popups_path and popups_path.is_file() else None
    def_records = _collect_definition_records(defs_doc, pops_doc)

    all_refs = _iter_jsonl(refs_path)
    admission_refs = [r for r in all_refs if _is_admission_domain_node(str(r.get("domain_node_id") or ""))]

    dangling: list[dict[str, str]] = []
    warnings: list[str] = []

    non_admission_m083 = sum(
        1
        for r in all_refs
        if str(r.get("mcg_code")) == mcg and not _is_admission_domain_node(str(r.get("domain_node_id") or ""))
    )
    if non_admission_m083:
        warnings.append(
            f"Excluded {non_admission_m083} linked-condition-ref rows (non-admission domain) from this admission audit."
        )

    rows_out: list[dict[str, Any]] = []

    for ref in admission_refs:
        domain_node_id = str(ref.get("domain_node_id") or "")
        logic_node_id = str(ref.get("logic_node_id") or "")
        condition_key = str(ref.get("condition_key") or "")
        domain_original_text = str(ref.get("domain_original_text") or "")
        link_status = str(ref.get("definition_link_status") or "")

        logic = logic_by_id.get(logic_node_id)
        if logic is None:
            dangling.append(
                {
                    "logic_node_id": logic_node_id,
                    "condition_key": condition_key,
                    "domain_node_id": domain_node_id,
                }
            )
            warnings.append(
                f"Dangling logic_node_id {logic_node_id} for condition_key={condition_key} (not in domain rule tree)."
            )

        op = _format_val(logic.get("operator") if logic else None)
        val = _format_val(logic.get("value") if logic else None)
        unit = _format_val(logic.get("unit") if logic else None)
        if logic and (not op and logic.get("operator") is not None):
            op = _format_val(logic.get("operator"))

        path_txt = admission_path_text(domain_node_id, by_domain_id)
        ref_popup_title = ref.get("source_popup_title")
        cap_title = str(ref_popup_title) if ref_popup_title else ""
        cap_available = bool(ref_popup_title)

        def_match_title, def_match_available = _find_matching_definition(
            condition_key, domain_original_text, def_records
        )

        shared_root = ref.get("root_composite_id")
        shared_atomic_root = ref.get("root_atomic_id")
        if link_status == "linked_shared_definition":
            cat, reason, action, root_resolved = _linked_row_classification(ref, shared_roots)
            shared_root = root_resolved or shared_root
            cap_available = cap_available or bool(cap_title)
            # Augment popup fields from shared definitions metadata when missing on ref.
            if not cap_title and (lck := ref.get("linked_definition_condition_key")):
                for c in shared_doc.get("conditions") or []:
                    if c.get("condition_key") == lck and c.get("source_popup_title"):
                        cap_title = str(c["source_popup_title"])
                        cap_available = True
                        break
        elif link_status == "linked_shared_atomic_definition":
            cat, reason, action, root_resolved = _linked_atomic_row_classification(ref, shared_atomic_roots)
            shared_atomic_root = root_resolved or shared_atomic_root
            if not cap_title and (lck := ref.get("linked_definition_condition_key")):
                for c in shared_doc.get("conditions") or []:
                    if c.get("condition_key") == lck and c.get("source_popup_title"):
                        cap_title = str(c["source_popup_title"])
                        cap_available = True
                        break
        else:
            cat, reason, action = _classify_unlinked(
                condition_key=condition_key,
                domain_original_text=domain_original_text,
                logic=logic,
                captured_popup_title=cap_title if cap_title else None,
                captured_popup_available=cap_available,
                def_match_title=def_match_title,
                def_match_available=def_match_available,
            )

        captured_popup_display = cap_title or def_match_title or ""

        rows_out.append(
            {
                "mcg_code": mcg,
                "domain_node_id": domain_node_id,
                "admission_path_text": path_txt,
                "logic_node_id": logic_node_id,
                "condition_key": condition_key,
                "domain_original_text": domain_original_text,
                "operator": op,
                "value": val,
                "unit": unit,
                "definition_link_status": link_status,
                "coverage_category": cat,
                "shared_definition_root_composite_id": shared_root or "",
                "shared_definition_root_atomic_id": shared_atomic_root or "",
                "captured_popup_title": captured_popup_display,
                "captured_popup_available": bool(cap_title) or def_match_available,
                "reason": reason,
                "recommended_next_action": action,
            }
        )

    # Post-pass: out-of-scope check (should not happen if filter is correct)
    for r in rows_out:
        if not _is_admission_domain_node(r["domain_node_id"]):
            r["coverage_category"] = "out_of_scope_non_admission"
            r["reason"] = "Non-admission domain_node_id in admission audit row (unexpected)."
            r["recommended_next_action"] = "manual_review_required"
            warnings.append(f"out_of_scope_non_admission: {r['condition_key']} ({r['domain_node_id']})")

    counts = Counter(r["coverage_category"] for r in rows_out)

    admission_keys = {str(r.get("condition_key")) for r in admission_refs}
    top_candidates: list[dict[str, Any]] = []
    seen_prio: set[str] = set()
    for label, keys in NEXT_BUILD_PRIORITY:
        present = [k for k in keys if k in admission_keys]
        if not present:
            continue
        for ck in present:
            rec = next((x for x in rows_out if x["condition_key"] == ck), None)
            if not rec:
                continue
            if rec["coverage_category"] not in NEXT_BUILD_CATEGORIES:
                continue
            if ck in seen_prio:
                continue
            seen_prio.add(ck)
            top_candidates.append(
                {
                    "priority_label": label,
                    "condition_key": ck,
                    "coverage_category": rec["coverage_category"],
                    "recommended_next_action": rec["recommended_next_action"],
                }
            )

    summary = {
        "mcg_code": mcg,
        "admission_condition_ref_count": len(rows_out),
        "linked_shared_definition_count": int(counts.get("linked_shared_definition", 0)),
        "linked_shared_atomic_definition_count": int(counts.get("linked_shared_atomic_definition", 0)),
        "already_atomic_minimal_count": int(counts.get("already_atomic_minimal", 0)),
        "direct_boolean_leaf_acceptable_count": int(counts.get("direct_boolean_leaf_acceptable", 0)),
        "needs_shared_definition_popup_available_count": int(
            counts.get("needs_shared_definition_popup_available", 0)
        ),
        "needs_shared_definition_source_only_count": int(
            counts.get("needs_shared_definition_source_only", 0)
        ),
        "needs_manual_review_count": int(counts.get("needs_manual_review", 0)),
        "top_next_shared_definition_candidates": top_candidates,
        "warnings": warnings,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{mcg}.admission-definition-coverage.audit"

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "domain_rule_tree": str(domain_path),
            "linked_condition_refs": str(refs_path),
            "shared_definitions": str(shared_path),
            "definitions_json": str(defs_path),
            "popups_json": str(popups_path) if popups_path else None,
        },
        "dangling_linked_refs": dangling,
        "non_admission_excluded_ref_count": non_admission_m083,
    }

    audit_doc = {"summary": summary, "conditions": rows_out, "meta": meta}
    (out_dir / f"{base}.json").write_text(
        json.dumps(audit_doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Markdown
    md_lines = [
        "# M083 Admission Definition Coverage Audit",
        "",
        "## Summary",
        "",
        f"- **Admission condition refs:** {summary['admission_condition_ref_count']}",
        f"- **linked_shared_definition:** {summary['linked_shared_definition_count']}",
        f"- **linked_shared_atomic_definition:** {summary['linked_shared_atomic_definition_count']}",
        f"- **already_atomic_minimal:** {summary['already_atomic_minimal_count']}",
        f"- **direct_boolean_leaf_acceptable:** {summary['direct_boolean_leaf_acceptable_count']}",
        f"- **needs_shared_definition — popup available:** {summary['needs_shared_definition_popup_available_count']}",
        f"- **needs_shared_definition — source only:** {summary['needs_shared_definition_source_only_count']}",
        f"- **needs_manual_review:** {summary['needs_manual_review_count']}",
        f"- **Dangling linked refs (missing logic node):** {len(dangling)}",
        "",
    ]
    if warnings:
        md_lines.extend(["### Warnings", ""])
        for w in warnings:
            md_lines.append(f"- {w}")
        md_lines.append("")

    def section(title: str, cat: str) -> None:
        md_lines.extend([f"## {title}", ""])
        sub = [r for r in rows_out if r["coverage_category"] == cat]
        if not sub:
            md_lines.append("_None._")
        else:
            for r in sorted(sub, key=lambda x: (x["domain_node_id"], x["condition_key"])):
                md_lines.append(
                    f"- **{r['condition_key']}** — `{r['logic_node_id']}` — {r['reason']}"
                )
        md_lines.append("")

    section("Linked shared definitions", "linked_shared_definition")
    section("Linked shared atomic definitions", "linked_shared_atomic_definition")
    section("Already atomic minimal", "already_atomic_minimal")
    section("Direct boolean leaves acceptable for now", "direct_boolean_leaf_acceptable")
    section("Needs shared definition — popup available", "needs_shared_definition_popup_available")
    section("Needs shared definition — source only", "needs_shared_definition_source_only")
    section("Needs manual review", "needs_manual_review")
    oor = [r for r in rows_out if r["coverage_category"] == "out_of_scope_non_admission"]
    if oor:
        md_lines.extend(["## Out of scope (non-admission)", ""])
        for r in oor:
            md_lines.append(f"- **{r['condition_key']}** — {r['domain_node_id']}")
        md_lines.append("")

    md_lines.extend(
        [
            "## Recommended next build order",
            "",
            "Ordered priorities (only keys present in admission refs and still needing shared work):",
            "",
        ]
    )
    if not top_candidates:
        md_lines.append("_No prioritized candidates in `needs_shared_definition_*` categories._")
    else:
        for i, c in enumerate(top_candidates, 1):
            md_lines.append(
                f"{i}. **{c['priority_label']}** — `{c['condition_key']}` "
                f"({c['coverage_category']}; {c['recommended_next_action']})"
            )
    md_lines.append("")

    (out_dir / f"{base}.md").write_text("\n".join(md_lines), encoding="utf-8")

    # CSV
    fieldnames = list(rows_out[0].keys()) if rows_out else [
        "mcg_code",
        "domain_node_id",
        "admission_path_text",
        "logic_node_id",
        "condition_key",
        "domain_original_text",
        "operator",
        "value",
        "unit",
        "definition_link_status",
        "coverage_category",
        "shared_definition_root_composite_id",
        "captured_popup_title",
        "captured_popup_available",
        "reason",
        "recommended_next_action",
    ]
    csv_path = out_dir / f"{base}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows_out:
            w.writerow(r)

    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mcg-code", default="M083")
    p.add_argument("--domain-rule-tree", required=True)
    p.add_argument("--linked-condition-refs", required=True)
    p.add_argument("--shared-definitions", required=True)
    p.add_argument("--definitions-json", required=True)
    p.add_argument("--popups-json", default="")
    p.add_argument("--out-dir", required=True)
    args = p.parse_args()
    if not args.popups_json:
        args.popups_json = None  # type: ignore[assignment]
    return run_audit(args)


if __name__ == "__main__":
    sys.exit(main())
