#!/usr/bin/env python3
"""Admission definition linkage QA: named keys vs hashed leaves + shared-definition embed readiness."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _norm_headline(txt: str) -> str:
    t = txt or ""
    t = re.sub(r"\[\s*[A-Za-z]\s*\]", "", t)
    t = re.sub(r"\(\s*\d", " (", t)
    t = " ".join(t.split()).strip().lower()
    return t


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", type=Path, required=True)
    ap.add_argument("--mcg-code", default="M190")
    ap.add_argument("--domain-rule-tree", type=Path)
    ap.add_argument("--definitions-raw-json", type=Path)
    ap.add_argument("--shared-definitions", type=Path)
    ap.add_argument("--linked-condition-refs-jsonl", type=Path)
    ap.add_argument("--prior-hashed-leaf-count", type=int, default=-1, help="If >=0, include as before count")
    ap.add_argument("--out-json", type=Path, required=True)
    ap.add_argument("--out-md", type=Path, required=True)
    args = ap.parse_args()

    repo = Path(args.repo_root).resolve()
    mc = args.mcg_code.strip().upper()
    dom_path = Path(args.domain_rule_tree).resolve()
    defs_path = Path(args.definitions_raw_json).resolve()
    sh_path = Path(args.shared_definitions).resolve()
    lk_path = Path(args.linked_condition_refs_jsonl).resolve()

    dom = load_json(dom_path)
    defs = load_json(defs_path)
    shared = load_json(sh_path)

    pref = f"{mc}.admission."
    admissions: list[dict] = []
    for ln in dom.get("logic_nodes") or []:
        if not isinstance(ln, dict):
            continue
        dn = str(ln.get("linked_domain_node_id") or "")
        if dn.startswith(pref) and str(ln.get("node_kind") or "") == "atomic":
            ck = ln.get("condition_key")
            if ck is None:
                continue
            admissions.append(ln)

    admission_leaf_count = len(admissions)
    hashed_rows = [
        n
        for n in admissions
        if isinstance(n.get("condition_key"), str) and str(n.get("condition_key")).startswith(f"leaf_{mc.lower()}_")
    ]
    hashed_leaf_count = len(hashed_rows)
    hashed_leaf_texts = sorted({str(n.get("original_text") or "")[:220] for n in hashed_rows if n.get("original_text")})

    named_rows = [
        n
        for n in admissions
        if isinstance(n.get("condition_key"), str)
        and not str(n.get("condition_key")).startswith(f"leaf_{mc.lower()}_")
    ]
    named_condition_link_count = len(named_rows)

    shared_cks = {
        str(c.get("condition_key") or "")
        for c in (shared.get("conditions") or [])
        if str(c.get("condition_key") or "")
    }

    lk_rows: list[dict] = []
    for line in lk_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        lk_rows.append(json.loads(line))

    adm_refs = [
        r
        for r in lk_rows
        if str(r.get("mcg_code") or "").strip().upper() == mc and str(r.get("domain_node_id") or "").startswith(pref)
    ]

    linked_def = sum(1 for r in adm_refs if str(r.get("definition_link_status") or "") == "linked_shared_definition")
    linked_atomic = sum(
        1 for r in adm_refs if str(r.get("definition_link_status") or "") == "linked_shared_atomic_definition"
    )
    evaluator_scope_unlinked_count = sum(1 for r in adm_refs if str(r.get("definition_link_status") or "") == "unlinked")

    def headline_for_match(n: dict) -> str:
        ot = str(n.get("original_text") or "").strip()
        h = ot.split(":")[0]
        return h.split("( ")[0].strip()

    label_map: dict[str, str] = {}
    for key in ("definitions", "popups"):
        for d in defs.get(key) or []:
            if not isinstance(d, dict):
                continue
            ttl = str(d.get("title") or "").strip()
            if ttl.startswith("Definition - "):
                lbl = ttl[len("Definition - ") :].strip()
                if lbl:
                    label_map[_norm_headline(lbl)] = ttl

    expected_named_links_missing: list[dict[str, str]] = []
    for n in hashed_rows:
        head = _norm_headline(headline_for_match(n))
        ttl_hit = label_map.get(head)
        if ttl_hit:
            expected_named_links_missing.append(
                dict(
                    popup_title=ttl_hit,
                    headline=headline_for_match(n),
                    condition_key=str(n.get("condition_key")),
                    normalized_head=head,
                )
            )

    fail_hemo = False
    fail_cai = False
    fail_ams = False
    for n in hashed_rows:
        ot = _norm_headline(str(n.get("original_text") or ""))
        if "hemodynamic instability" in ot:
            fail_hemo = True
        if "cardiac arrhythmias of immediate concern" in ot:
            fail_cai = True
        if "altered mental status that is severe or persistent" in ot:
            fail_ams = True

    ready_for_runtime = not (fail_hemo or fail_cai or fail_ams or bool(expected_named_links_missing))

    hemo_linked = False
    cai_linked = False
    ams_named = False
    for r in adm_refs:
        ck = str(r.get("condition_key") or "")
        st = str(r.get("definition_link_status") or "")
        if ck == "hemodynamic_instability_condition_present" and st == "linked_shared_definition":
            hemo_linked = True
        if ck == "cardiac_arrhythmia_of_immediate_concern_condition_present" and st == "linked_shared_definition":
            cai_linked = True
        if ck == "altered_mental_status_severe_or_persistent_condition_present" and (
            st == "linked_shared_definition" or st == "linked_shared_atomic_definition"
        ):
            ams_named = True

    out = dict(
        mcg_code=mc,
        domain_rule_tree=str(dom_path.relative_to(repo)) if str(dom_path).startswith(str(repo)) else str(dom_path),
        admission_leaf_count=admission_leaf_count,
        named_condition_link_count=named_condition_link_count,
        hashed_leaf_count=hashed_leaf_count,
        hashed_leaf_texts=sorted(set(hashed_leaf_texts)),
        expected_named_links_missing=expected_named_links_missing,
        linked_shared_definition_count=linked_def,
        linked_shared_atomic_definition_count=linked_atomic,
        evaluator_scope_unlinked_count=evaluator_scope_unlinked_count,
        linkage_flags=dict(
            hemodynamic_instability_still_hashed_leaf=fail_hemo,
            cardiac_arrhythmias_immediate_concern_still_hashed_leaf=fail_cai,
            altered_mental_status_severe_persistent_still_hashed_leaf=fail_ams,
            definition_title_overlap_unresolved_leaf=bool(expected_named_links_missing),
        ),
        linkage_verification=dict(
            hemodynamic_linked_shared_definition_row=hemo_linked,
            cardiac_arrhythmias_linked_shared_definition_row=cai_linked,
            altered_mental_status_severe_persistent_named_link=ams_named,
        ),
        ready_for_runtime=ready_for_runtime,
        prior_hashed_leaf_count=args.prior_hashed_leaf_count,
        shared_conditions_count=len(shared_cks),
    )

    md_lines = [
        f"# Definition linkage QA — `{mc}`",
        "",
        f"- **ready_for_runtime**: `{out['ready_for_runtime']}`",
        f"- **admission_leaf_count**: {admission_leaf_count}",
        f"- **named_condition_link_count**: {named_condition_link_count}",
        f"- **hashed_leaf_count** (after): {hashed_leaf_count}",
        f"- **prior_hashed_leaf_count** (before regeneration): {_fmt_prior(args.prior_hashed_leaf_count)}",
        f"- **linked_shared_definition**: {linked_def}",
        f"- **linked_shared_atomic_definition**: {linked_atomic}",
        f"- **evaluator_scope_unlinked** (admission paths): {evaluator_scope_unlinked_count}",
        "",
        "## Flags",
        "",
    ]
    for k, v in out["linkage_flags"].items():
        md_lines.append(f"- **{k}**: `{v}`")
    md_lines.extend(
        [
            "",
            "## Verification rows (linked refs)",
            "",
            f"- **hemodynamic → shared-definition row**: `{hemo_linked}`",
            f"- **cardiac arrhythmias immediate → shared-definition row**: `{cai_linked}`",
            f"- **AMS severe/persistent named link**: `{ams_named}`",
            "",
            "## Admission hashed leaf excerpts (truncated)",
            "",
        ]
    )
    for t in hashed_leaf_texts[:40]:
        md_lines.append(f"- {t}")

    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(out, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    Path(args.out_md).write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")
    return 0


def _fmt_prior(v: int) -> str:
    if v is None or v < 0:
        return "not_provided"
    return str(v)


if __name__ == "__main__":
    raise SystemExit(main())
