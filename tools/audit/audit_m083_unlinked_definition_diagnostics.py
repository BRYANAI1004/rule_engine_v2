#!/usr/bin/env python3
"""Admission unlinked definition diagnostics: capture vs shared JSON vs linker (audit only)."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

ADMISSION_PREFIX = "M083.admission"

KNOWN_ALIASES: dict[str, list[str]] = {
    "respiratory_abnormality_condition_present": [
        "respiratory abnormalities",
        "hypoxemia",
        "impaired airway protection",
        "respiratory distress",
    ],
    "cardiac_arrhythmia_of_immediate_concern_condition_present": [
        "cardiac arrhythmias of immediate concern",
        "dangerous arrhythmia",
    ],
    "prolonged_cardiac_telemetry_monitoring_required": [
        "prolonged cardiac telemetry",
        "dangerous arrhythmia",
        "atrial fibrillation",
    ],
    "hypoxemia_condition_present": ["hypoxemia"],
    "dangerous_arrhythmia_condition_present": ["dangerous arrhythmia"],
}

RECOMMENDED_FIX = {
    "captured_but_not_parsed": "build_shared_definition",
    "parsed_but_not_linked": "rerun_linker",
    "not_captured": "manual_review",
    "source_only": "no_action",
    "already_atomic_minimal": "no_action",
    "acceptable_boolean_leaf": "no_action",
    "manual_review": "manual_review",
}


def _norm(s: str) -> str:
    s = s.casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


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


def _index_logic(domain_path: Path) -> dict[str, dict[str, Any]]:
    d = _load_json(domain_path)
    return {str(n["logic_node_id"]): n for n in (d.get("logic_nodes") or []) if n.get("logic_node_id")}


def _collect_popup_records(defs: dict[str, Any] | None, pops: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if defs:
        for r in defs.get("definitions") or []:
            out.append(r)
    if pops:
        for r in pops.get("popups") or []:
            out.append(r)
    return out


def _def_match_for_key(
    condition_key: str,
    domain_text: str,
    records: list[dict[str, Any]],
    aliases: list[str],
) -> tuple[bool, bool, str, str]:
    """Match captured popups using domain text + aliases against popup titles (not bodies — avoids 'following' noise)."""
    blob = _norm(f"{condition_key} {domain_text} {' '.join(aliases)}")
    best_def: tuple[int, str, str] | None = None
    best_pop: tuple[str, str] | None = None

    for rec in records:
        ptype = str(rec.get("popup_type") or "")
        title = str(rec.get("title") or "")
        pid = str(rec.get("popup_id") or "")
        title_n = _norm(title.replace("definition -", "").replace("reference -", ""))

        hit = bool(title_n and len(title_n) >= 6 and title_n in blob)
        if not hit:
            for phrase in aliases:
                pn = _norm(phrase)
                if not pn or len(pn) < 8:
                    continue
                if pn == title_n or pn in title_n or title_n in pn:
                    hit = True
                    break
        if not hit:
            ck_phrase = _norm(condition_key.replace("_condition_present", "").replace("_required", "").replace("_", " "))
            if ck_phrase and len(ck_phrase) >= 10 and (ck_phrase in title_n or title_n in ck_phrase):
                hit = True

        if not hit:
            continue

        if ptype == "definition":
            score = len(title_n)
            if best_def is None or score > best_def[0]:
                best_def = (score, title, pid)
        elif best_pop is None:
            best_pop = (title, pid)

    if best_def:
        _, best_title, best_id = best_def
        return True, True, best_title, best_id
    if best_pop:
        t, i = best_pop
        return False, True, t, i
    return False, False, "", ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mcg-code", default="M083")
    ap.add_argument("--domain-rule-tree", required=True, type=Path)
    ap.add_argument("--linked-condition-refs", required=True, type=Path)
    ap.add_argument("--shared-definitions", required=True, type=Path)
    ap.add_argument("--definitions-json", required=True, type=Path)
    ap.add_argument("--popups-json", type=Path)
    ap.add_argument("--prior-coverage-audit", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args()

    mcg = args.mcg_code
    logic_by_id = _index_logic(args.domain_rule_tree)
    refs = _iter_jsonl(args.linked_condition_refs)
    shared = _load_json(args.shared_definitions)
    shared_cks = {str(c.get("condition_key")) for c in (shared.get("conditions") or [])}

    prior = _load_json(args.prior_coverage_audit)
    cov_by_ck: dict[str, str] = {}
    for row in prior.get("conditions") or []:
        ck = str(row.get("condition_key") or "")
        if ck:
            cov_by_ck[ck] = str(row.get("coverage_category") or "")

    defs_doc = _load_json(args.definitions_json)
    pops_doc = _load_json(args.popups_json) if args.popups_json and args.popups_json.is_file() else None
    records = _collect_popup_records(defs_doc, pops_doc)

    admission_unlinked = [
        r
        for r in refs
        if str(r.get("mcg_code")) == mcg
        and str(r.get("definition_link_status")) == "unlinked"
        and str(r.get("domain_node_id") or "").startswith(ADMISSION_PREFIX)
    ]

    rows: list[dict[str, Any]] = []
    diag_counts: dict[str, int] = {}

    for ref in admission_unlinked:
        ck = str(ref.get("condition_key") or "")
        domain_text = str(ref.get("domain_original_text") or "")
        logic_id = str(ref.get("logic_node_id") or "")
        domain_node = str(ref.get("domain_node_id") or "")
        logic = logic_by_id.get(logic_id)
        op = str(logic.get("operator") or "") if logic else ""
        val = logic.get("value") if logic else ""
        unit = str(logic.get("unit") or "") if logic and logic.get("unit") is not None else ""

        prior_cat = cov_by_ck.get(ck, "")
        aliases = KNOWN_ALIASES.get(ck, [])
        cap_def, cap_pop, mtitle, mid = _def_match_for_key(ck, domain_text, records, aliases)
        in_shared = ck in shared_cks

        diagnosis = "manual_review"
        if prior_cat == "already_atomic_minimal":
            diagnosis = "already_atomic_minimal"
        elif prior_cat == "direct_boolean_leaf_acceptable":
            diagnosis = "acceptable_boolean_leaf"
        elif prior_cat == "needs_manual_review":
            diagnosis = "manual_review"
        elif in_shared:
            diagnosis = "parsed_but_not_linked"
        elif cap_def:
            diagnosis = "captured_but_not_parsed"
        elif prior_cat in ("needs_shared_definition_source_only",) and not cap_def:
            diagnosis = "source_only"
        elif not cap_def and not cap_pop:
            diagnosis = "not_captured"
        elif not cap_def and cap_pop:
            diagnosis = "not_captured"
        else:
            diagnosis = "source_only"

        diag_counts[diagnosis] = diag_counts.get(diagnosis, 0) + 1

        rows.append(
            {
                "condition_key": ck,
                "domain_original_text": domain_text,
                "logic_node_id": logic_id,
                "domain_node_id": domain_node,
                "operator": op,
                "value": val if val is not None else "",
                "unit": unit,
                "coverage_category_from_prior_audit": prior_cat,
                "captured_definition_match": cap_def,
                "captured_popup_match": cap_pop,
                "matched_popup_title": mtitle,
                "matched_popup_id": mid,
                "present_in_shared_definitions": in_shared,
                "present_in_linked_tree": False,
                "diagnosis": diagnosis,
                "recommended_fix": RECOMMENDED_FIX.get(diagnosis, "manual_review"),
            }
        )

    summary = {
        "mcg_code": mcg,
        "admission_unlinked_ref_count": len(rows),
        "diagnosis_counts": dict(sorted(diag_counts.items(), key=lambda x: x[0])),
    }

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{mcg}.unlinked-definition-diagnostics.audit"
    doc = {"summary": summary, "rows": rows}

    (out_dir / f"{stem}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    md = [
        f"# {mcg} unlinked admission definition diagnostics",
        "",
        "## Summary",
        "",
        json.dumps(summary, indent=2),
        "",
        "## By diagnosis",
        "",
    ]
    by_d: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_d.setdefault(r["diagnosis"], []).append(r)
    for k in sorted(by_d.keys()):
        md.append(f"### {k}")
        md.append("")
        for r in sorted(by_d[k], key=lambda x: x["condition_key"]):
            md.append(
                f"- `{r['condition_key']}` — def={r['captured_definition_match']} "
                f"pop={r['captured_popup_match']} — {r['matched_popup_title'] or '—'}"
            )
        md.append("")

    (out_dir / f"{stem}.md").write_text("\n".join(md), encoding="utf-8")

    fields = list(rows[0].keys()) if rows else []
    if rows:
        with (out_dir / f"{stem}.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
