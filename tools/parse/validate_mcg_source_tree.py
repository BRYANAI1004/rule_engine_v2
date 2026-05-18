#!/usr/bin/env python3
"""Validate M083.source-tree.v1.json structure and integrity checks."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

UI_NOISE_SUBSTRINGS = (
    "Return to top",
    "Benchmark Statistics",
    "Link to Codes",
    "Print View",
    "Click here to preview",
    "View abstract",
    "Context Link",
    "Expand All",
    "Collapse All",
    "Show All",
    "Hide All",
)


def _fail(msgs: list[str], msg: str) -> None:
    msgs.append(msg)


def validate(doc: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if doc.get("schema_version") != "mcg_source_tree.v1":
        _fail(errors, f"schema_version mismatch: {doc.get('schema_version')}")

    for key in (
        "source_document",
        "sections",
        "source_nodes",
        "tables",
        "footnotes",
        "references",
        "codes",
        "collapse_expand_map",
        "audit",
    ):
        if key not in doc:
            _fail(errors, f"missing top-level key: {key}")

    nodes = doc.get("source_nodes") or []
    sections = doc.get("sections") or []
    mcg = doc.get("source_document", {}).get("mcg_code") or ""

    section_ids = {s.get("section_id") for s in sections if s.get("section_id")}
    node_ids = [n.get("source_node_id") for n in nodes]
    if len(node_ids) != len(set(node_ids)):
        _fail(errors, "duplicate source_node_id values")

    parents: dict[str | None, list[dict[str, Any]]] = {}
    for n in nodes:
        pid = n.get("parent_source_node_id")
        parents.setdefault(pid, []).append(n)

    id_set = set(node_ids)
    for n in nodes:
        sid = n.get("source_node_id")
        pid = n.get("parent_source_node_id")
        if not sid:
            _fail(errors, "node missing source_node_id")
        if pid is not None and pid not in id_set:
            _fail(errors, f"parent_source_node_id not found: {pid} (child {sid})")
        sec_ref = n.get("section_id")
        if sec_ref and sec_ref not in section_ids:
            _fail(errors, f"section_id not found for node {sid}: {sec_ref}")
        if "sort_order" not in n:
            _fail(errors, f"sort_order missing on {sid}")
        d = n.get("source_depth")
        if d is None:
            _fail(errors, f"source_depth missing on {sid}")
        elif pid is None:
            if d != 1:
                _fail(errors, f"root node depth should be 1: {sid} depth={d}")
        else:
            parent = next((x for x in nodes if x.get("source_node_id") == pid), None)
            if parent:
                pd = parent.get("source_depth")
                if pd is not None and d != pd + 1:
                    _fail(
                        errors,
                        f"source_depth mismatch: {sid} depth={d} parent {pid} depth={pd}",
                    )

    admission_root = any(
        "Admission is indicated for" in (n.get("original_text") or "")
        and "1 or more" in (n.get("original_text") or "")
        for n in nodes
    )
    if not admission_root:
        _fail(errors, "admission root text not found in source_nodes")

    has_dp = any(
        "Discharge planning includes" in (n.get("original_text") or "") for n in nodes
    )
    if not has_dp:
        _fail(errors, "discharge planning root not found")

    has_dest = any(
        "Post-hospital levels" in (n.get("original_text") or "") for n in nodes
    )
    if not has_dest:
        _fail(errors, "discharge destination root not found")

    tables = doc.get("tables") or []
    orc_ok = any(
        (t.get("table_id") or "").endswith("orc.001") or t.get("title") == "Optimal Recovery Course"
        for t in tables
    )
    if not orc_ok:
        _fail(errors, "ORC table not found")

    for n in nodes:
        t = n.get("original_text") or ""
        for s in UI_NOISE_SUBSTRINGS:
            if s in t:
                _fail(errors, f"UI noise in node {n.get('source_node_id')}: {s}")

    crit_residual = ("Expand Acute", "Expand Patient", "Expand Medication", "[Expand All")
    for n in nodes:
        t = n.get("original_text") or ""
        for m in crit_residual:
            if m in t:
                _fail(errors, f"residual expand text in node {n.get('source_node_id')}: {m}")

    # Roundtrip / md file not loaded here; audit file lists residual_expand_text from build

    return len(errors) == 0, errors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    args = ap.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    ok, errs = validate(data)
    if ok:
        print("PASS — MCG source tree validation OK")
        print(f"  sections: {len(data.get('sections') or [])}")
        print(f"  source_nodes: {len(data.get('source_nodes') or [])}")
        print(f"  tables: {len(data.get('tables') or [])}")
        sys.exit(0)
    print("FAIL — MCG source tree validation")
    for e in errs:
        print(f"  - {e}")
    sys.exit(1)


if __name__ == "__main__":
    main()
