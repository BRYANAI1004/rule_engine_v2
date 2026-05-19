import csv
import json
from pathlib import Path

input_path = Path("rules/mcg/domain-trees/M083.domain-rule-tree.v1.json")
out_path = Path("rules/mcg/domain-trees/M083.admission.atomic-clean.csv")

with input_path.open("r", encoding="utf-8") as f:
    d = json.load(f)

logic = {x["logic_node_id"]: x for x in d["logic_nodes"]}
source_refs = {x["source_ref_id"]: x for x in d["source_refs"]}

rows = []


def first_source_quote(node):
    ref_ids = node.get("source_ref_ids") or []
    if not ref_ids:
        return ""
    ref = source_refs.get(ref_ids[0])
    return ref.get("source_quote", "") if ref else ""


def fmt_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def walk(node_id, path_node, current_group="", path_logic="", context_key="", context_text=""):
    node = logic[node_id]
    kind = node.get("node_kind")
    op = node.get("operator")

    if kind == "context":
        context_key = node.get("condition_key", "")
        context_text = node.get("original_text", "")

    if kind == "composite":
        if op in ("AND", "OR", "CHECKLIST", "OPTIONS", "EXAMPLE_SET"):
            if not path_logic:
                path_logic = op
            current_group = node.get("original_text", "") or current_group

    if kind == "atomic":
        rows.append({
            "admission_path_id": path_node.get("node_id", ""),
            "admission_path_text": path_node.get("original_text", ""),
            "path_root_logic": path_logic,
            "context_condition_key": context_key,
            "context_text": context_text,
            "criteria_group": current_group,
            "atomic_condition_key": node.get("condition_key", ""),
            "atomic_operator": op or "",
            "measurement": node.get("measurement", ""),
            "value": fmt_value(node.get("value")),
            "unit": node.get("unit", ""),
            "atomic_original_text": node.get("original_text", ""),
            "source_quote": first_source_quote(node),
            "review_status": node.get("review_status", ""),
        })

    for child_id in node.get("child_logic_node_ids", []):
        walk(
            child_id,
            path_node=path_node,
            current_group=current_group,
            path_logic=path_logic,
            context_key=context_key,
            context_text=context_text,
        )


for path_node in d["domain_nodes"]:
    if path_node.get("domain") == "admission" and path_node.get("level") == 3:
        root_id = path_node.get("logic_root_id")
        if root_id:
            walk(root_id, path_node)

fieldnames = [
    "admission_path_id",
    "admission_path_text",
    "path_root_logic",
    "context_condition_key",
    "context_text",
    "criteria_group",
    "atomic_condition_key",
    "atomic_operator",
    "measurement",
    "value",
    "unit",
    "atomic_original_text",
    "source_quote",
    "review_status",
]

with out_path.open("w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

print(f"Wrote {len(rows)} atomic rows to {out_path}")
