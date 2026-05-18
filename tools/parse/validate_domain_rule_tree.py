#!/usr/bin/env python3
"""Integrity checks for mcg_domain_rule_tree.v1 domain rule tree JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from domain_rule_tree_types import SCHEMA_DOMAIN_RULE_TREE, SCHEMA_SOURCE_TREE


def _fail(msgs: list[str], msg: str) -> None:
    msgs.append(msg)


OPS = frozenset(("AND", "OR", "CHECKLIST", "OPTIONS", "EXAMPLE_SET"))

KNOWN_SYNTHETIC_ORIGINAL_LABELS = frozenset(
    {
        "Stroke neurologic inpatient bundle",
        "Stroke clinical monitoring inpatient bundle",
    }
)


def _bad_condition_key_fragments(ck: str) -> bool:
    if ck.startswith("src_"):
        return True
    return "source_admission" in ck or "source_discharge" in ck or "source_" in ck


def validate(doc: dict[str, Any]) -> tuple[bool, list[str]]:
    errs: list[str] = []

    if doc.get("schema_version") != SCHEMA_DOMAIN_RULE_TREE:
        _fail(errs, f"schema_version mismatch (expected {SCHEMA_DOMAIN_RULE_TREE}): {doc.get('schema_version')}")

    if doc.get("source_tree_schema_version") != SCHEMA_SOURCE_TREE:
        _fail(
            errs,
            "source_tree_schema_version mismatch "
            f"(expected {SCHEMA_SOURCE_TREE}): {doc.get('source_tree_schema_version')}",
        )

    for key in ("mcg_code", "mcg_title", "domain_roots", "domain_nodes", "logic_nodes", "source_refs", "condition_dictionary", "audit"):
        if key not in doc:
            _fail(errs, f"missing top-level key: {key}")

    dom = doc.get("domain_nodes") or []
    lg = doc.get("logic_nodes") or []
    srefs = doc.get("source_refs") or []

    dom_by_id = {d["node_id"]: d for d in dom}

    lids = [n["logic_node_id"] for n in lg]
    if len(lids) != len(set(lids)):
        _fail(errs, "duplicate logic_node_id values")

    lgx = {n["logic_node_id"]: n for n in lg}

    for d in dom:
        nid = d.get("node_id")
        if not nid:
            _fail(errs, "domain node missing node_id")
            continue
        lv = d.get("level")
        if lv is None:
            _fail(errs, f"domain node {nid} missing level")
        elif not (0 <= int(lv) <= 3):
            _fail(errs, f"domain node {nid} level out of range 0–3: {lv}")
        cid = str(d["parent_node_id"]) if d.get("parent_node_id") is not None else None
        if cid is not None and cid not in dom_by_id:
            _fail(errs, f"domain parent_node_id missing: {nid} -> {cid}")
        lr = d.get("logic_root_id")
        if lr is not None and lr not in lgx:
            _fail(errs, f"domain node {nid} logic_root_id not found among logic_nodes: {lr}")
        source_ref_ids = d.get("source_ref_ids") or []
        sid_list = d.get("source_node_ids") or []

        # Source provenance tags are required for all domain nodes touched by this pipeline.
        if not source_ref_ids or not sid_list:
            _fail(errs, f"domain node {nid} missing source_ref_ids/source_node_ids")

    for nid, lst in [(n["node_id"], n.get("child_node_ids")) for n in dom]:
        if not lst:
            continue
        if not isinstance(lst, list):
            _fail(errs, f"domain node {nid} child_node_ids not a list")
            continue

        if len(lst) != len(set(lst)):
            _fail(errs, f"domain node {nid} duplicate child ids")

        for c in lst:
            if str(c) not in dom_by_id:
                _fail(errs, f"domain node {nid} refers to unknown child {c}")

    ref_by_id = {r["source_ref_id"]: r for r in srefs}
    for r in srefs:
        if not r.get("source_ref_id"):
            _fail(errs, "source_ref missing source_ref_id")

    sr_needed: set[str] = set()

    for d in dom:
        sr_needed.update(d.get("source_ref_ids") or [])

    for ln in lg:
        sr_needed.update(ln.get("source_ref_ids") or [])

    missing_sr = sr_needed.difference(ref_by_id)

    if missing_sr:
        _fail(errs, f"missing source_ref records for refs: {sorted(missing_sr)[:12]}{'...' if len(missing_sr)>12 else ''}")

    lone_roots = [ln["logic_node_id"] for ln in lg if ln.get("parent_logic_node_id") is None]

    for ln in lg:
        lid = ln["logic_node_id"]
        if ln.get("level") != 4:
            _fail(errs, f"logic node {lid} level must be 4, got {ln.get('level')}")
        lk = ln.get("linked_domain_node_id")
        if lk and lk not in dom_by_id:
            _fail(errs, f"logic node {lid} linked_domain_node_id unknown: {lk}")
        srs = ln.get("source_ref_ids") or []

        sns = ln.get("source_node_ids") or []

        if not srs or not sns:

            _fail(errs, f"logic node {lid} missing source_ref_ids/source_node_ids")

        kids = ln.get("child_logic_node_ids") or []

        for c in kids:

            if c not in lgx:

                _fail(errs, f"logic node {lid} refers to unknown child {c}")

        op = ln.get("operator")

        nk = ln.get("node_kind")

        if nk == "composite" and op:

            if op not in OPS:

                _fail(errs, f"logic node {lid} unsupported operator {op}")

            # Keep AND/OR/OR-like structures meaningful — each branch should be observable.
            if op in ("AND", "OR") and len(kids) < 2:

                _fail(errs, f"logic composite {lid} ({op}) has fewer than 2 children ({len(kids)})")

    ck_rows = doc.get("condition_dictionary") or []

    for row in ck_rows:

        lk = row.get("linked_logic_node_id")

        if lk not in lgx:

            _fail(errs, f"condition_dictionary row linked_logic_node_id missing: {lk}")

    reachable: set[str] = set()

    for rid in lone_roots:

        stk = [rid]

        while stk:

            u = stk.pop()

            if u in reachable:

                continue

            reachable.add(u)

            for c in lgx[u].get("child_logic_node_ids") or []:

                stk.append(str(c))

    if len(reachable) != len(lgx):

        _fail(errs, "not all logic nodes reachable from lone parents (cycle or orphan?)")

    for lid, ln in lgx.items():

        p = ln.get("parent_logic_node_id")

        if p is None:

            continue

        parent = lgx.get(p)

        if not parent:

            _fail(errs, f"logic node {lid} parent not found {p}")

        ch = parent.get("child_logic_node_ids") or []

        if lid not in ch:

            _fail(errs, f"logic node {lid} parent {p} does not list it as child")

    for ln in lg:
        lid = ln["logic_node_id"]
        ck = ln.get("condition_key")
        if isinstance(ck, str) and _bad_condition_key_fragments(ck):
            _fail(errs, f"logic node {lid} condition_key must not use source-id style keys: {ck!r}")
        op = ln.get("operator")
        strict = ln.get("strict_boolean_evaluation")
        xo = ln.get("example_only")
        if op == "EXAMPLE_SET" and strict is not False:
            _fail(errs, f"logic node {lid} EXAMPLE_SET must have strict_boolean_evaluation false")
        if xo is True and strict is not False:
            _fail(errs, f"logic node {lid} example_only requires strict_boolean_evaluation false")
        ot = (ln.get("original_text") or "").strip()
        if ot in KNOWN_SYNTHETIC_ORIGINAL_LABELS:
            _fail(
                errs,
                f"logic node {lid} original_text must not be only the synthetic bundle label ({ot!r}); "
                "use MCG source wording as original_text and put the short title in display_label",
            )

    return len(errs) == 0, errs


def main() -> None:

    ap = argparse.ArgumentParser(description=f"Validate {SCHEMA_DOMAIN_RULE_TREE}")

    ap.add_argument("--input", required=True)

    ns = ap.parse_args()

    p = Path(ns.input)

    doc = json.loads(p.read_text(encoding="utf-8"))

    ok, msgs = validate(doc)

    print("PASS" if ok else "FAIL")

    for m in msgs:

        print(m, file=sys.stderr)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
