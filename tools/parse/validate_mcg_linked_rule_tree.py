#!/usr/bin/env python3
"""Validate mcg_linked_rule_tree.v1 linkage artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCHEMA_LINKED_RULE_TREE = "mcg_linked_rule_tree.v1"


def _fail(errs: list[str], msg: str) -> None:
    errs.append(msg)


def validate(
    doc: dict[str, Any],
    shared: dict[str, Any],
    *,
    domain: dict[str, Any] | None,
    repo_root: Path,
) -> list[str]:
    errs: list[str] = []

    if doc.get("schema_version") != SCHEMA_LINKED_RULE_TREE:
        _fail(
            errs,
            f"schema_version expected {SCHEMA_LINKED_RULE_TREE!r} got {doc.get('schema_version')!r}",
        )

    nodes = doc.get("linked_logic_nodes")
    if not isinstance(nodes, list) or not nodes:
        _fail(errs, "linked_logic_nodes missing or empty")

    shared_conds = shared.get("conditions") or []
    shared_ck = {str(c.get("condition_key") or "") for c in shared_conds if isinstance(c, dict)}
    comp_ids = {str(c.get("id") or "") for c in (shared.get("composite_definitions") or []) if isinstance(c, dict)}

    atom_ids = {str(c.get("id") or "") for c in (shared.get("atomic_rules") or []) if isinstance(c, dict)}

    dangling_ptrs: list[str] = []

    for n in nodes:
        if not isinstance(n, dict):
            continue
        st = str(n.get("definition_link_status") or "")
        if st == "linked_shared_definition":
            ref = n.get("shared_definition_ref") or {}
            rck = str(ref.get("condition_key") or "")
            if rck not in shared_ck:
                dangling_ptrs.append(f"{n.get('logic_node_id')}: bad shared condition_key {rck!r}")
            rcid = str(ref.get("root_composite_id") or "")
            if rcid and rcid not in comp_ids:
                dangling_ptrs.append(f"{n.get('logic_node_id')}: root_composite_id {rcid!r} not in shared composites")
        elif st == "linked_shared_atomic_definition":
            ref = n.get("shared_atomic_definition_ref") or {}
            rck = str(ref.get("condition_key") or "")
            if rck not in shared_ck:
                dangling_ptrs.append(f"{n.get('logic_node_id')}: bad shared_atomic condition_key {rck!r}")
            raid = str(ref.get("root_atomic_id") or "")
            if raid and raid not in atom_ids:
                dangling_ptrs.append(f"{n.get('logic_node_id')}: root_atomic_id {raid!r} not in shared atomic_rules")

    if dangling_ptrs:
        _fail(errs, "dangling definition refs: " + "; ".join(dangling_ptrs[:30]))

    mcg_code = str(doc.get("mcg_code") or shared.get("mcg_code") or "")
    admission_prefix = f"{mcg_code}.admission."

    admission_bad: list[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        ck = str(n.get("condition_key") or "").strip()
        if not ck:
            continue
        dn = str(n.get("linked_domain_node_id") or "")
        if not dn.startswith(admission_prefix):
            continue
        st = str(n.get("definition_link_status") or "")
        if mcg_code == "M083":
            allowed = ("linked_shared_definition", "linked_shared_atomic_definition")
        else:
            allowed = (
                "linked_shared_definition",
                "linked_shared_atomic_definition",
                "unlinked",
            )
        if st not in allowed:
            admission_bad.append(f"{ck} logic_node={n.get('logic_node_id')} status={st!r} domain_node={dn}")

    if admission_bad:
        if mcg_code == "M083":
            msg = (
                f"{mcg_code} admission condition leaves must be linked_shared_definition or "
                f"linked_shared_atomic_definition; remaining: "
            )
        else:
            msg = (
                f"{mcg_code} admission condition leaves must be linked_shared_definition, "
                "linked_shared_atomic_definition, or unlinked (source_only); remaining: "
            )
        _fail(errs, msg + "; ".join(admission_bad[:50]))

    if mcg_code == "M083":
        hemo_nodes = [
            n
            for n in nodes
            if isinstance(n, dict) and str(n.get("condition_key") or "") == "hemodynamic_instability_condition_present"
        ]
        if not hemo_nodes:
            _fail(errs, "no logic node with condition_key hemodynamic_instability_condition_present")
        hemo_linked = [
            n
            for n in hemo_nodes
            if str(n.get("definition_link_status") or "") == "linked_shared_definition"
        ]
        if not hemo_linked:
            _fail(errs, "hemodynamic_instability_condition_present not linked_shared_definition")
        ref0 = (hemo_linked[0].get("shared_definition_ref") or {}) if hemo_linked else {}
        if str(ref0.get("root_composite_id") or "") != "comp.hemodynamic_instability.root":
            _fail(
                errs,
                f"hemodynamic link root_composite_id expected comp.hemodynamic_instability.root got {ref0.get('root_composite_id')!r}",
            )

        if domain:
            path_ids = {
                str(n.get("node_id"))
                for n in (domain.get("domain_nodes") or [])
                if isinstance(n, dict) and str(n.get("node_type") or "") == "admission_path"
            }
            hn = hemo_linked[0]
            dn_h = str(hn.get("linked_domain_node_id") or "")
            if dn_h not in path_ids:
                _fail(errs, f"hemodynamic logic node domain {dn_h!r} is not an admission_path in domain tree")
        else:
            _fail(errs, "domain rule tree not loaded; cannot verify hemodynamic node is on an admission_path")

        audit = doc.get("audit") or {}
        if not audit.get("hemodynamic_instability_linked"):
            _fail(errs, "audit.hemodynamic_instability_linked must be true")
        if not audit.get("admission_path_2_has_hemodynamic_link"):
            _fail(errs, "audit.admission_path_2_has_hemodynamic_link must be true")

    expected_sc = len(shared_conds)
    audit = doc.get("audit") or {}
    if int(audit.get("shared_condition_count") or 0) != expected_sc:
        _fail(
            errs,
            f"audit.shared_condition_count expected {expected_sc} got {audit.get('shared_condition_count')!r}",
        )

    # Cross-check audit flags against doc (avoid stale hand-edited audits).
    linked_composite_n = sum(
        1
        for n in nodes
        if isinstance(n, dict)
        and str(n.get("definition_link_status") or "") == "linked_shared_definition"
        and str(n.get("condition_key") or "").strip()
    )
    linked_atomic_n = sum(
        1
        for n in nodes
        if isinstance(n, dict)
        and str(n.get("definition_link_status") or "") == "linked_shared_atomic_definition"
        and str(n.get("condition_key") or "").strip()
    )
    linked_total = linked_composite_n + linked_atomic_n
    if int(audit.get("linked_condition_ref_count") or -1) != linked_total:
        _fail(
            errs,
            (
                f"audit.linked_condition_ref_count mismatch computed={linked_total} "
                f"(composite={linked_composite_n} atomic={linked_atomic_n}) "
                f"audit={audit.get('linked_condition_ref_count')!r}"
            ),
        )
    ac_sp = audit.get("linked_shared_definition_ref_count")
    if ac_sp is not None and int(ac_sp) != linked_composite_n:
        _fail(errs, f"audit.linked_shared_definition_ref_count mismatch computed={linked_composite_n} audit={ac_sp!r}")
    ac_at = audit.get("linked_shared_atomic_definition_ref_count")
    if ac_at is not None and int(ac_at) != linked_atomic_n:
        _fail(errs, f"audit.linked_shared_atomic_definition_ref_count mismatch computed={linked_atomic_n} audit={ac_at!r}")

    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--shared-definitions", required=True, type=Path)
    ap.add_argument("--repo-root", type=Path, default=Path("."))
    args = ap.parse_args()

    repo_root = args.repo_root.resolve()
    doc = json.loads(args.input.read_text(encoding="utf-8"))
    shared = json.loads(args.shared_definitions.read_text(encoding="utf-8"))

    dom_rel = (doc.get("source_files") or {}).get("domain_rule_tree")
    domain: dict[str, Any] | None = None
    if isinstance(dom_rel, str) and dom_rel:
        dom_path = (repo_root / dom_rel).resolve()
        if dom_path.is_file():
            domain = json.loads(dom_path.read_text(encoding="utf-8"))

    errs = validate(doc, shared, domain=domain, repo_root=repo_root)
    if errs:
        print("INVALID linked rule tree:", file=sys.stderr)
        for e in errs:
            print(f"- {e}", file=sys.stderr)
        return 1

    print("OK: linked rule tree validates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
