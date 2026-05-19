#!/usr/bin/env python3
"""Step 2E: project domain rule tree logic nodes onto shared condition definitions (linkage only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


SCHEMA_LINKED_RULE_TREE = "mcg_linked_rule_tree.v1"


def _rel_posix(root: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p.as_posix())


def _norm_ck(node: dict[str, Any]) -> str | None:
    ck = node.get("condition_key")
    if ck is None:
        return None
    s = str(ck).strip()
    return s if s else None


def _shared_conditions_index(conditions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in conditions:
        if not isinstance(row, dict):
            continue
        ck = str(row.get("condition_key") or "").strip()
        if not ck:
            continue
        out[ck] = {
            "condition_key": ck,
            "definition_type": str(row.get("definition_type") or ""),
            "root_composite_id": str(row.get("root_composite_id") or "").strip(),
            "root_atomic_id": str(row.get("root_atomic_id") or "").strip(),
            "source_popup_title": row.get("source_popup_title"),
            "source_popup_id": row.get("source_popup_id"),
            "source_strategy": row.get("source_strategy"),
            "evaluator_ready": row.get("evaluator_ready"),
            "review_status": row.get("review_status"),
        }
    return out


def _link_fields_for_node(
    node: dict[str, Any],
    shared_by_ck: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return new dict: original logic node + definition_link_status (+ optional ref)."""
    out = deepcopy(node)
    nk = str(node.get("node_kind") or "")
    ck = _norm_ck(node)

    if nk == "composite" and ck is None:
        out["definition_link_status"] = "not_applicable"
        return out

    if ck is None:
        out["definition_link_status"] = "not_applicable"
        return out

    if ck not in shared_by_ck:
        out["definition_link_status"] = "unlinked"
        return out

    meta = shared_by_ck[ck]
    dt = str(meta.get("definition_type") or "")
    if dt == "shared_composite_condition":
        rcid = str(meta.get("root_composite_id") or "").strip()
        if not rcid:
            out["definition_link_status"] = "unlinked"
            return out
        out["definition_link_status"] = "linked_shared_definition"
        out["shared_definition_ref"] = {
            "condition_key": meta["condition_key"],
            "root_composite_id": rcid,
            "definition_type": dt,
            "source_popup_title": meta.get("source_popup_title"),
            "source_popup_id": meta.get("source_popup_id"),
        }
    elif dt == "shared_atomic_condition":
        raid = str(meta.get("root_atomic_id") or "").strip()
        if not raid:
            out["definition_link_status"] = "unlinked"
            return out
        out["definition_link_status"] = "linked_shared_atomic_definition"
        out["shared_atomic_definition_ref"] = {
            "condition_key": meta["condition_key"],
            "root_atomic_id": raid,
            "definition_type": dt,
            "source_popup_title": meta.get("source_popup_title"),
            "source_popup_id": meta.get("source_popup_id"),
            "source_strategy": meta.get("source_strategy"),
            "review_status": meta.get("review_status"),
            "evaluator_ready": meta.get("evaluator_ready"),
        }
    else:
        out["definition_link_status"] = "unlinked"

    return out


def _ref_jsonl_row(
    *,
    mcg_code: str,
    node: dict[str, Any],
) -> dict[str, Any]:
    ck = _norm_ck(node)
    if ck is None:
        raise ValueError("internal: ref row requires condition_key")
    st = str(node.get("definition_link_status") or "")
    row: dict[str, Any] = {
        "mcg_code": mcg_code,
        "domain_node_id": str(node.get("linked_domain_node_id") or ""),
        "logic_node_id": str(node.get("logic_node_id") or ""),
        "condition_key": ck,
        "definition_link_status": st,
        "domain_original_text": str(node.get("original_text") or ""),
    }
    row["root_composite_id"] = None
    row["root_atomic_id"] = None
    row["source_popup_title"] = None
    row["linked_definition_condition_key"] = None

    if st == "linked_shared_definition":
        ref = node.get("shared_definition_ref") or {}
        row["root_composite_id"] = str(ref.get("root_composite_id") or "") or None
        row["source_popup_title"] = ref.get("source_popup_title")
        row["linked_definition_condition_key"] = str(ref.get("condition_key") or ck)
    elif st == "linked_shared_atomic_definition":
        ref = node.get("shared_atomic_definition_ref") or {}
        row["root_atomic_id"] = str(ref.get("root_atomic_id") or "") or None
        row["linked_definition_condition_key"] = str(ref.get("condition_key") or ck)
        row["source_popup_title"] = ref.get("source_popup_title")
    return row


def _fmt_atomic_preview(atom: dict[str, Any]) -> str:
    rv = str(atom.get("review_status") or "")
    suffix = ""
    if rv == "needs_review":
        suffix = " _(needs_review)_"
    if atom.get("definition_type") == "atomic_numeric":
        mv = str(atom.get("measurement") or atom.get("condition_key") or "")
        op = str(atom.get("operator") or "")
        vl = atom.get("value")
        um = str(atom.get("unit") or "").strip()
        base = f"{mv} {op} {vl}".strip()
        mv_l = mv.casefold()
        um_l = um.casefold()
        if um and not (mv_l == um_l):
            base = f"{base} {um}".strip()
        return (base + suffix).strip()
    return (str(atom.get("condition_key") or "") + suffix).strip()


def _hemodynamic_preview_lines(shared: dict[str, Any]) -> list[str]:
    atoms = {str(a["id"]): a for a in (shared.get("atomic_rules") or []) if str(a.get("id") or "")}
    preview_ids = [
        "atom.hemodynamic_instability.adult.sbp_lt_80",
        "atom.hemodynamic_instability.adult.shock_index_gt_1",
        "atom.hemodynamic_instability.adult.map_lt_65",
        "atom.hemodynamic_instability.adult.vasopressor_or_inotrope_required",
        "atom.hemodynamic_instability.hypoperfusion.lactate_gte_2",
        "atom.hemodynamic_instability.hypoperfusion.ph_lt_7_35",
    ]
    lines: list[str] = []
    for aid in preview_ids:
        a = atoms.get(aid)
        if a:
            lines.append(_fmt_atomic_preview(a))
    return lines


def _admission_path_title(desc: str, original: str) -> str:
    for src in (desc, original):
        if not src:
            continue
        if ", as indicated" in src:
            return src.split(", as indicated", 1)[0].strip()
        return str(src).strip()
    return ""


def _render_roundtrip(
    *,
    mcg_code: str,
    domain: dict[str, Any],
    linked_nodes: list[dict[str, Any]],
    shared: dict[str, Any],
) -> str:
    domain_nodes = domain.get("domain_nodes") or []
    admission_paths = [
        n
        for n in domain_nodes
        if isinstance(n, dict)
        and str(n.get("node_type") or "") == "admission_path"
        and str(n.get("domain") or "") == "admission"
    ]
    admission_paths.sort(key=lambda x: int(x.get("sort_order") or 0))

    by_domain_id: dict[str, list[dict[str, Any]]] = {}
    for ln in linked_nodes:
        if str(ln.get("definition_link_status") or "") != "linked_shared_definition":
            continue
        dn = str(ln.get("linked_domain_node_id") or "")
        by_domain_id.setdefault(dn, []).append(ln)

    lines: list[str] = [f"# {mcg_code} Linked Rule Tree", ""]

    for path in admission_paths:
        dnid = str(path.get("node_id") or "")
        title = _admission_path_title(
            str(path.get("description") or ""),
            str(path.get("original_text") or ""),
        )
        lines.append(f"## Admission path: {title}")
        lines.append("")
        rows = sorted(by_domain_id.get(dnid, []), key=lambda r: str(r.get("logic_node_id")))
        if not rows:
            lines.append("_No linked shared definitions on this path._")
            lines.append("")
            continue
        for r in rows:
            ck = _norm_ck(r)
            if not ck:
                continue
            ref = r.get("shared_definition_ref") or {}
            rcid = str(ref.get("root_composite_id") or "")
            sttl = str(ref.get("source_popup_title") or "")
            lines.append(f"- {ck}")
            lines.append(f"  - linked shared definition: {rcid}")
            lines.append(f"  - source: {sttl}")
            if ck == "hemodynamic_instability_condition_present":
                lines.append("  - preview (subset):")
                for pv in _hemodynamic_preview_lines(shared):
                    lines.append(f"    - {pv}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mcg-code", required=True)
    ap.add_argument("--domain-rule-tree", required=True, type=Path)
    ap.add_argument("--shared-definitions", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args()

    root = Path(".").resolve()
    dom_path = args.domain_rule_tree
    shared_path = args.shared_definitions
    out_dir: Path = args.out_dir

    domain = json.loads(dom_path.read_text(encoding="utf-8"))
    shared = json.loads(shared_path.read_text(encoding="utf-8"))

    mcg_code = str(args.mcg_code)
    if str(domain.get("mcg_code") or "") != mcg_code:
        print(f"ERROR: mcg-code {mcg_code!r} does not match domain tree", file=sys.stderr)
        return 2
    if str(shared.get("mcg_code") or "") != mcg_code:
        print(f"ERROR: mcg-code {mcg_code!r} does not match shared definitions", file=sys.stderr)
        return 2

    shared_conditions = shared.get("conditions") or []
    shared_by_ck = _shared_conditions_index(shared_conditions)

    raw_logic = domain.get("logic_nodes") or []
    linked_logic_nodes = [_link_fields_for_node(dict(n), shared_by_ck) for n in raw_logic if isinstance(n, dict)]

    ref_rows: list[dict[str, Any]] = []
    for n in linked_logic_nodes:
        if _norm_ck(n) is None:
            continue
        ref_rows.append(_ref_jsonl_row(mcg_code=mcg_code, node=n))
    ref_rows.sort(key=lambda r: str(r.get("logic_node_id")))

    domain_nodes = domain.get("domain_nodes") or []
    admission_paths = [
        n
        for n in domain_nodes
        if isinstance(n, dict)
        and str(n.get("node_type") or "") == "admission_path"
        and str(n.get("domain") or "") == "admission"
    ]
    admission_paths.sort(key=lambda x: int(x.get("sort_order") or 0))
    path2_id = str(admission_paths[1]["node_id"]) if len(admission_paths) >= 2 else ""

    keys_in_domain: set[str] = set()
    for n in raw_logic:
        if isinstance(n, dict):
            ck = _norm_ck(n)
            if ck:
                keys_in_domain.add(ck)

    keys_linked: set[str] = set()
    keys_unlinked: set[str] = set()
    linked_composite_n = 0
    linked_atomic_n = 0
    unlinked_n = 0
    hemo_id: str | None = None
    for n in linked_logic_nodes:
        if _norm_ck(n) == "hemodynamic_instability_condition_present":
            hemo_id = str(n.get("logic_node_id") or "")
            break

    hemo_linked = False
    for n in linked_logic_nodes:
        ck = _norm_ck(n)
        if ck is None:
            continue
        st = str(n.get("definition_link_status") or "")
        if st == "linked_shared_definition":
            linked_composite_n += 1
            keys_linked.add(ck)
            if ck == "hemodynamic_instability_condition_present":
                hemo_linked = True
        elif st == "linked_shared_atomic_definition":
            linked_atomic_n += 1
            keys_linked.add(ck)
        elif st == "unlinked":
            unlinked_n += 1
            keys_unlinked.add(ck)

    path2_hemo = False
    if path2_id:
        for n in linked_logic_nodes:
            if _norm_ck(n) != "hemodynamic_instability_condition_present":
                continue
            if str(n.get("linked_domain_node_id") or "") != path2_id:
                continue
            if str(n.get("definition_link_status") or "") == "linked_shared_definition":
                path2_hemo = True
                break

    shared_ck_set = set(shared_by_ck.keys())
    dangling = sorted(shared_ck_set - keys_in_domain)

    hemo_node_admission = False
    if hemo_id:
        for n in linked_logic_nodes:
            if str(n.get("logic_node_id") or "") != hemo_id:
                continue
            dn = str(n.get("linked_domain_node_id") or "")
            for path in admission_paths:
                if str(path.get("node_id")) == dn:
                    hemo_node_admission = True
                    break
            break

    linked_total_n = linked_composite_n + linked_atomic_n
    admission_unlinked_details: list[dict[str, str]] = []
    for n in linked_logic_nodes:
        ck = _norm_ck(n)
        if ck is None:
            continue
        dn = str(n.get("linked_domain_node_id") or "")
        if not dn.startswith(f"{mcg_code}.admission."):
            continue
        st = str(n.get("definition_link_status") or "")
        if st == "unlinked":
            admission_unlinked_details.append(
                {"condition_key": ck, "logic_node_id": str(n.get("logic_node_id") or ""), "domain_node_id": dn}
            )

    audit: dict[str, Any] = {
        "mcg_code": mcg_code,
        "domain_logic_node_count": len(raw_logic),
        "shared_condition_count": len(shared_conditions),
        "linked_condition_ref_count": linked_total_n,
        "linked_shared_definition_ref_count": linked_composite_n,
        "linked_shared_atomic_definition_ref_count": linked_atomic_n,
        "unlinked_condition_ref_count": unlinked_n,
        "admission_unlinked_condition_ref_count": len(admission_unlinked_details),
        "admission_unlinked_details": admission_unlinked_details,
        "hemodynamic_instability_linked": hemo_linked,
        "hemodynamic_instability_logic_node_id": hemo_id,
        "hemodynamic_instability_root_composite_id": "comp.hemodynamic_instability.root",
        "admission_path_2_has_hemodynamic_link": path2_hemo,
        "linked_condition_keys": sorted(keys_linked),
        "unlinked_condition_keys": sorted(keys_unlinked),
        "dangling_shared_definition_refs": dangling,
        "warnings": [],
    }
    if hemo_linked and not hemo_node_admission:
        audit["warnings"].append("hemodynamic_instability logic node not under an admission_path domain node")

    doc: dict[str, Any] = {
        "schema_version": SCHEMA_LINKED_RULE_TREE,
        "mcg_code": mcg_code,
        "mcg_title": domain.get("mcg_title"),
        "source_files": {
            "domain_rule_tree": _rel_posix(root, dom_path.resolve()),
            "shared_condition_definitions": _rel_posix(root, shared_path.resolve()),
            "domain_rule_tree_sha256": hashlib.sha256(dom_path.read_bytes()).hexdigest(),
            "shared_condition_definitions_sha256": hashlib.sha256(shared_path.read_bytes()).hexdigest(),
        },
        "linked_logic_nodes": sorted(linked_logic_nodes, key=lambda r: str(r.get("logic_node_id"))),
        "audit": audit,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{mcg_code}.linked-rule-tree"
    json_path = out_dir / f"{stem}.v1.json"
    audit_path = out_dir / f"{stem}.audit.json"
    refs_path = out_dir / f"{mcg_code}.linked-condition-refs.jsonl"
    logic_path = out_dir / f"{mcg_code}.linked-logic-nodes.jsonl"
    md_path = out_dir / f"{stem}.roundtrip.md"

    json_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    refs_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in ref_rows), encoding="utf-8")
    logic_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in doc["linked_logic_nodes"]), encoding="utf-8")
    md_path.write_text(
        _render_roundtrip(
            mcg_code=mcg_code,
            domain=domain,
            linked_nodes=linked_logic_nodes,
            shared=shared,
        ),
        encoding="utf-8",
    )

    print(f"OK wrote: {json_path}")
    print(
        f"OK linked_condition_ref_count={linked_total_n} "
        f"(composite={linked_composite_n} atomic={linked_atomic_n}) "
        f"unlinked_condition_ref_count={unlinked_n} "
        f"logic_nodes_jsonl={len(doc['linked_logic_nodes'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
