#!/usr/bin/env python3
"""
Local smoke evaluator for M083 admission paths (Step 2H).
Reads domain tree + shared definitions + linkage; evaluates patient facts with tri-state logic.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

Trilean = Literal["TRUE", "FALSE", "UNKNOWN"]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def as_trace_val(x: Any) -> str | None:
    if x is None:
        return None
    if isinstance(x, bool):
        return "true" if x else "false"
    return str(x)


def combine_or(children: Sequence[Trilean]) -> Trilean:
    if "TRUE" in children:
        return "TRUE"
    if "UNKNOWN" in children:
        return "UNKNOWN"
    return "FALSE"


def combine_and(children: Sequence[Trilean]) -> Trilean:
    if "FALSE" in children:
        return "FALSE"
    if "UNKNOWN" in children:
        return "UNKNOWN"
    return "TRUE"


@dataclass
class EvalContext:
    facts: Mapping[str, Any]
    atoms_by_id: dict[str, dict[str, Any]]
    composites_by_id: dict[str, dict[str, Any]]
    links_by_logic: dict[str, dict[str, Any]]
    logic_by_id: dict[str, dict[str, Any]]
    trace: list[dict[str, Any]] = field(default_factory=list)

    def append_trace(
        self,
        *,
        node_id: str,
        condition_key: str | None,
        operator: str | None,
        expected_value: str | None,
        actual_value: str | None,
        result: Trilean,
        reason: str,
        source: Literal["domain", "shared_composite", "shared_atomic"],
    ) -> None:
        self.trace.append(
            {
                "node_id": node_id,
                "condition_key": condition_key,
                "operator": operator,
                "expected_value": expected_value,
                "actual_value": actual_value,
                "result": result,
                "reason": reason,
                "source": source,
            }
        )

    def fact_value(self, key: str) -> Any | None:
        if key not in self.facts:
            return None
        return self.facts[key]

    def compare_numeric(self, op: str, lhs: float, rhs: float) -> bool:
        if op in (">",):
            return lhs > rhs
        if op in (">=",):
            return lhs >= rhs
        if op in ("<",):
            return lhs < rhs
        if op in ("<=",):
            return lhs <= rhs
        if op in ("==", "="):
            return lhs == rhs
        raise ValueError(f"unsupported numeric operator {op!r}")

    def eval_atomic_rule(self, atom: dict[str, Any]) -> tuple[Trilean, str]:
        a_id = atom["id"]
        ck = atom.get("condition_key")
        op = atom.get("operator")
        if atom.get("review_status") == "needs_review" or atom.get("evaluator_ready") is False:
            return "UNKNOWN", "needs_review or evaluator_ready=false"

        dtype = atom.get("definition_type")
        if dtype == "atomic_flag":
            if op != "IS_TRUE":
                return "UNKNOWN", f"unsupported flag operator {op!r}"
            v = self.fact_value(str(ck))
            if v is True:
                return "TRUE", "fact is true"
            if v is False:
                return "FALSE", "fact is false"
            return "FALSE", "fact missing"

        if dtype == "atomic_numeric":
            threshold = atom.get("value")
            exp_unit = atom.get("unit")
            raw = self.fact_value(str(ck))
            if raw is None:
                return "FALSE", "numeric fact missing"
            try:
                lhs = float(raw)
                rhs = float(threshold)
            except (TypeError, ValueError):
                return "UNKNOWN", "non-numeric fact or threshold"
            ok = self.compare_numeric(str(op), lhs, rhs)
            if exp_unit:
                reason = f"compare {lhs} {op} {rhs} {exp_unit}"
            else:
                reason = f"compare {lhs} {op} {rhs}"
            return ("TRUE" if ok else "FALSE"), reason

        return "UNKNOWN", f"unsupported definition_type {dtype!r}"

    def eval_shared_atomic(self, atomic_id: str, reason_prefix: str = "") -> tuple[Trilean, set[str]]:
        atom = self.atoms_by_id.get(atomic_id)
        if not atom:
            self.append_trace(
                node_id=atomic_id,
                condition_key=None,
                operator=None,
                expected_value=None,
                actual_value=None,
                result="UNKNOWN",
                reason=reason_prefix + "atomic id not found in shared definitions",
                source="shared_atomic",
            )
            return "UNKNOWN", set()

        tri, why = self.eval_atomic_rule(atom)
        exp_val = as_trace_val(atom.get("value"))
        if atom.get("definition_type") == "atomic_flag":
            exp_val = as_trace_val(True)
        act = as_trace_val(self.fact_value(str(atom["condition_key"])))
        self.append_trace(
            node_id=atomic_id,
            condition_key=atom.get("condition_key"),
            operator=atom.get("operator"),
            expected_value=exp_val,
            actual_value=act,
            result=tri,
            reason=reason_prefix + why,
            source="shared_atomic",
        )
        keys: set[str] = set()
        if tri == "TRUE" and atom.get("condition_key"):
            keys.add(str(atom["condition_key"]))
        return tri, keys

    def eval_shared_composite(self, comp_id: str) -> tuple[Trilean, set[str]]:
        comp = self.composites_by_id.get(comp_id)
        if not comp:
            self.append_trace(
                node_id=comp_id,
                condition_key=None,
                operator=None,
                expected_value=None,
                actual_value=None,
                result="UNKNOWN",
                reason="composite id not found in shared definitions",
                source="shared_composite",
            )
            return "UNKNOWN", set()

        if comp.get("review_status") == "needs_review":
            ck = comp.get("condition_key")
            self.append_trace(
                node_id=comp_id,
                condition_key=ck,
                operator=comp.get("operator"),
                expected_value=None,
                actual_value=None,
                result="UNKNOWN",
                reason="composite review_status needs_review",
                source="shared_composite",
            )
            return "UNKNOWN", set()

        children_ids: Sequence[str] = comp.get("children") or []
        op = str(comp.get("operator") or "AND").upper()
        child_tris: list[Trilean] = []
        child_keys: list[set[str]] = []
        for cid in children_ids:
            if cid in self.composites_by_id:
                t, ks = self.eval_shared_composite(cid)
            elif cid in self.atoms_by_id:
                t, ks = self.eval_shared_atomic(cid)
            else:
                self.append_trace(
                    node_id=cid,
                    condition_key=None,
                    operator=None,
                    expected_value=None,
                    actual_value=None,
                    result="UNKNOWN",
                    reason="child id not found as composite or atomic",
                    source="shared_composite",
                )
                t, ks = "UNKNOWN", set()
            child_tris.append(t)
            child_keys.append(ks)

        if op == "OR":
            tri = combine_or(child_tris)
        elif op == "AND":
            tri = combine_and(child_tris)
        else:
            self.append_trace(
                node_id=comp_id,
                condition_key=comp.get("condition_key"),
                operator=op,
                expected_value=None,
                actual_value=None,
                result="UNKNOWN",
                reason=f"unsupported composite operator {op!r}",
                source="shared_composite",
            )
            return "UNKNOWN", set()

        matched: set[str] = set()
        if tri == "TRUE":
            if comp.get("condition_key"):
                matched.add(str(comp["condition_key"]))
            for t, ks in zip(child_tris, child_keys):
                if t == "TRUE":
                    matched |= ks

        reason = f"composite {op} of {child_tris}"
        self.append_trace(
            node_id=comp_id,
            condition_key=comp.get("condition_key"),
            operator=op,
            expected_value=None,
            actual_value=None,
            result=tri,
            reason=reason,
            source="shared_composite",
        )
        return tri, matched

    def eval_domain_logic_node(self, logic_node_id: str) -> tuple[Trilean, set[str]]:
        node = self.logic_by_id[logic_node_id]
        kind = node.get("node_kind")
        node_op = str(node.get("operator") or "").upper()

        if kind == "composite":
            children = node.get("child_logic_node_ids") or []
            child_tris: list[Trilean] = []
            key_sets: list[set[str]] = []
            for cid in children:
                t, ks = self.eval_domain_logic_node(cid)
                child_tris.append(t)
                key_sets.append(ks)
            if node_op == "OR":
                tri = combine_or(child_tris)
            elif node_op == "AND":
                tri = combine_and(child_tris)
            else:
                tri = "UNKNOWN"
                self.append_trace(
                    node_id=logic_node_id,
                    condition_key=node.get("condition_key"),
                    operator=node_op,
                    expected_value=None,
                    actual_value=None,
                    result=tri,
                    reason=f"unsupported domain composite operator {node_op!r}",
                    source="domain",
                )
                return tri, set()

            matched: set[str] = set()
            if tri == "TRUE":
                for t, ks in zip(child_tris, key_sets):
                    if t == "TRUE":
                        matched |= ks
            reason = f"domain composite {node_op} children={child_tris}"
            self.append_trace(
                node_id=logic_node_id,
                condition_key=node.get("condition_key"),
                operator=node_op,
                expected_value=None,
                actual_value=None,
                result=tri,
                reason=reason,
                source="domain",
            )
            return tri, matched

        # atomic or context: resolve via linkage
        link = self.links_by_logic.get(logic_node_id)
        if not link:
            self.append_trace(
                node_id=logic_node_id,
                condition_key=node.get("condition_key"),
                operator=node.get("operator"),
                expected_value=as_trace_val(node.get("value")),
                actual_value=as_trace_val(self.fact_value(str(node.get("condition_key") or ""))),
                result="UNKNOWN",
                reason="no linked_condition_refs entry for logic_node_id",
                source="domain",
            )
            return "UNKNOWN", set()

        status = link.get("definition_link_status")
        if status == "unlinked":
            self.append_trace(
                node_id=logic_node_id,
                condition_key=node.get("condition_key"),
                operator=node.get("operator"),
                expected_value=as_trace_val(node.get("value")),
                actual_value=as_trace_val(self.fact_value(str(node.get("condition_key") or ""))),
                result="UNKNOWN",
                reason="definition_link_status=unlinked",
                source="domain",
            )
            return "UNKNOWN", set()

        if status == "linked_shared_atomic_definition":
            aid = link.get("root_atomic_id")
            if not aid:
                self.append_trace(
                    node_id=logic_node_id,
                    condition_key=node.get("condition_key"),
                    operator=None,
                    expected_value=None,
                    actual_value=None,
                    result="UNKNOWN",
                    reason="linked_shared_atomic_definition but root_atomic_id missing",
                    source="domain",
                )
                return "UNKNOWN", set()
            t, ks = self.eval_shared_atomic(str(aid))
            return t, ks

        if status == "linked_shared_definition":
            cid = link.get("root_composite_id")
            if not cid:
                self.append_trace(
                    node_id=logic_node_id,
                    condition_key=node.get("condition_key"),
                    operator=None,
                    expected_value=None,
                    actual_value=None,
                    result="UNKNOWN",
                    reason="linked_shared_definition but root_composite_id missing",
                    source="domain",
                )
                return "UNKNOWN", set()
            t, ks = self.eval_shared_composite(str(cid))
            return t, ks

        self.append_trace(
            node_id=logic_node_id,
            condition_key=node.get("condition_key"),
            operator=node.get("operator"),
            expected_value=None,
            actual_value=None,
            result="UNKNOWN",
            reason=f"unsupported definition_link_status {status!r}",
            source="domain",
        )
        return "UNKNOWN", set()


def evaluate_admission_paths(
    *,
    domain: dict[str, Any],
    shared: dict[str, Any],
    links_rows: list[dict[str, Any]],
    facts: Mapping[str, Any],
) -> tuple[bool, list[str], list[str], list[dict[str, Any]]]:
    atoms = {a["id"]: a for a in shared.get("atomic_rules") or []}
    comps = {c["id"]: c for c in shared.get("composite_definitions") or []}
    logic_nodes = domain.get("logic_nodes") or []
    logic_by_id = {ln["logic_node_id"]: ln for ln in logic_nodes}
    links_by_logic = {r["logic_node_id"]: r for r in links_rows if r.get("logic_node_id")}

    admission_root = (domain.get("domain_roots") or {}).get("admission")
    if not admission_root:
        raise ValueError("domain_roots.admission missing")

    path_nodes = [
        n
        for n in domain.get("domain_nodes") or []
        if n.get("parent_node_id") == admission_root
        and n.get("node_type") == "admission_path"
        and n.get("domain") == "admission"
    ]
    path_nodes.sort(key=lambda x: x.get("sort_order", 0))

    ctx = EvalContext(
        facts=facts,
        atoms_by_id=atoms,
        composites_by_id=comps,
        links_by_logic=links_by_logic,
        logic_by_id=logic_by_id,
    )

    matched_paths: list[str] = []
    all_keys: set[str] = set()

    for pn in path_nodes:
        root_id = pn.get("logic_root_id")
        nid = pn.get("node_id")
        if not root_id or not nid:
            continue
        tri, keys = ctx.eval_domain_logic_node(str(root_id))
        if tri == "TRUE":
            matched_paths.append(str(nid))
            all_keys |= keys

    admission_recommended = len(matched_paths) > 0
    matched_conditions = sorted(all_keys)
    return admission_recommended, matched_paths, matched_conditions, ctx.trace


# --- smoke cases ---

SMOKE_CASES: list[dict[str, Any]] = [
    {
        "case_id": "acute_only_negative",
        "facts": {"acute_ischemic_stroke_condition_present": True},
        "expected": {"admission_recommended": False, "matched_paths": []},
    },
    {
        "case_id": "path1_nihss_positive",
        "facts": {"acute_ischemic_stroke_condition_present": True, "nihss_score": 4},
        "expected": {
            "admission_recommended": True,
            "matched_paths_contains": ["M083.admission.acute_ischemic_stroke_neurologic_findings"],
        },
    },
    {
        "case_id": "path1_aphasia_positive",
        "facts": {"acute_ischemic_stroke_condition_present": True, "aphasia_condition_present": True},
        "expected": {
            "admission_recommended": True,
            "matched_paths_contains": ["M083.admission.acute_ischemic_stroke_neurologic_findings"],
            "matched_conditions_contains": ["aphasia_condition_present"],
        },
    },
    {
        "case_id": "path2_hemodynamic_sbp_low_positive",
        "facts": {"acute_ischemic_stroke_condition_present": True, "systolic_blood_pressure_mmhg": 76},
        "expected": {
            "admission_recommended": True,
            "matched_paths_contains": ["M083.admission.acute_ischemic_stroke_clinical_need_monitoring"],
            "matched_conditions_contains": [
                "hemodynamic_instability_condition_present",
                "systolic_blood_pressure_mmhg",
            ],
        },
    },
    {
        "case_id": "path2_hypoxemia_positive",
        "facts": {"acute_ischemic_stroke_condition_present": True, "spo2_percent": 88},
        "expected": {
            "admission_recommended": True,
            "matched_paths_contains": ["M083.admission.acute_ischemic_stroke_clinical_need_monitoring"],
            "matched_conditions_contains": [
                "respiratory_abnormality_condition_present",
                "hypoxemia_condition_present",
                "spo2_percent",
            ],
        },
    },
    {
        "case_id": "path2_arrhythmia_positive",
        "facts": {
            "acute_ischemic_stroke_condition_present": True,
            "sustained_ventricular_tachycardia_condition_present": True,
        },
        "expected": {
            "admission_recommended": True,
            "matched_paths_contains": ["M083.admission.acute_ischemic_stroke_clinical_need_monitoring"],
            "matched_conditions_contains": [
                "cardiac_arrhythmia_of_immediate_concern_condition_present",
                "dangerous_arrhythmia_condition_present",
                "sustained_ventricular_tachycardia_condition_present",
            ],
        },
    },
    {
        "case_id": "path2_severe_hypertension_positive",
        "facts": {"acute_ischemic_stroke_condition_present": True, "systolic_blood_pressure_mmhg": 190},
        "expected": {
            "admission_recommended": True,
            "matched_paths_contains": ["M083.admission.acute_ischemic_stroke_clinical_need_monitoring"],
            "matched_conditions_contains": ["systolic_blood_pressure_mmhg"],
        },
    },
    {
        "case_id": "path3_thrombolysis_positive",
        "facts": {"thrombolysis_or_thrombectomy_performed_or_planned_condition_present": True},
        "expected": {
            "admission_recommended": True,
            "matched_paths_contains": [
                "M083.admission.thrombolysis_or_thrombectomy_performed_or_planned"
            ],
        },
    },
    {
        "case_id": "negative_wrong_context",
        "facts": {"nihss_score": 4},
        "expected": {"admission_recommended": False, "matched_paths": []},
    },
]


def check_case(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> tuple[bool, list[str]]:
    mismatches: list[str] = []
    if actual["admission_recommended"] != expected["admission_recommended"]:
        mismatches.append(
            f"admission_recommended: expected {expected['admission_recommended']!r}, got {actual['admission_recommended']!r}"
        )
    if "matched_paths" in expected:
        if actual["matched_paths"] != expected["matched_paths"]:
            mismatches.append(
                f"matched_paths: expected {expected['matched_paths']!r}, got {actual['matched_paths']!r}"
            )
    if "matched_paths_contains" in expected:
        for p in expected["matched_paths_contains"]:
            if p not in actual["matched_paths"]:
                mismatches.append(f"matched_paths missing {p!r}; got {actual['matched_paths']!r}")
    if "matched_conditions_contains" in expected:
        mc = set(actual["matched_conditions"])
        for k in expected["matched_conditions_contains"]:
            if k not in mc:
                mismatches.append(
                    f"matched_conditions missing {k!r}; got {actual['matched_conditions']!r}"
                )
    return (len(mismatches) == 0), mismatches


def write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    lines: list[str] = [
        "# M083 admission smoke evaluation",
        "",
        "## Summary",
        "",
        f"- **Cases:** {report['case_count']}",
        f"- **Passed:** {report['passed_count']}",
        f"- **Failed:** {report['failed_count']}",
        "",
        "| case_id | passed | admission_recommended | matched_paths |",
        "|---------|--------|------------------------|---------------|",
    ]
    for c in report["cases"]:
        mp = ", ".join(c["actual"]["matched_paths"]) if c["actual"]["matched_paths"] else "—"
        lines.append(
            f"| {c['case_id']} | {'PASS' if c['passed'] else 'FAIL'} | {c['actual']['admission_recommended']} | {mp} |"
        )
    lines.extend(["", "## Cases", ""])

    for c in report["cases"]:
        lines.append(f"### {c['case_id']}")
        st = "PASS" if c["passed"] else "FAIL"
        lines.append(f"- **Status:** {st}")
        lines.append(f"- **Facts:** `{json.dumps(c['facts'], sort_keys=True)}`")
        if not c["passed"] and c.get("mismatches"):
            lines.append("- **Mismatches:**")
            for m in c["mismatches"]:
                lines.append(f"  - {m}")
        lines.append(
            f"- **Matched conditions:** `{json.dumps(c['actual']['matched_conditions'], sort_keys=True)}`"
        )
        trace = c.get("trace") or []
        excerpt = trace[:12]
        lines.append(f"- **Trace excerpt (first {len(excerpt)} rows):**")
        lines.append("```json")
        lines.append(json.dumps(excerpt, indent=2, sort_keys=False))
        lines.append("```")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    root = repo_root()
    domain_path = root / "rules/mcg/domain-trees/M083.domain-rule-tree.v1.json"
    shared_path = root / "rules/mcg/shared-condition-definitions/M083.shared-condition-definitions.v1.json"
    links_path = root / "rules/mcg/linked-rule-trees/M083.linked-condition-refs.jsonl"
    out_dir = root / "rules/mcg/audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_out = out_dir / "M083.admission-smoke-eval.report.json"
    md_out = out_dir / "M083.admission-smoke-eval.report.md"

    domain = load_json(domain_path)
    shared = load_json(shared_path)
    links = load_jsonl(links_path)

    cases_out: list[dict[str, Any]] = []
    passed_n = 0
    for case in SMOKE_CASES:
        facts = case["facts"]
        exp = case["expected"]
        adm, paths, conds, trace = evaluate_admission_paths(
            domain=domain, shared=shared, links_rows=links, facts=facts
        )
        actual = {
            "admission_recommended": adm,
            "matched_paths": paths,
            "matched_conditions": conds,
        }
        ok, mismatches = check_case(actual, exp)
        if ok:
            passed_n += 1
        cases_out.append(
            {
                "case_id": case["case_id"],
                "passed": ok,
                "facts": dict(facts),
                "expected": dict(exp),
                "actual": actual,
                "trace": trace,
                **({} if ok else {"mismatches": mismatches}),
            }
        )

    report = {
        "mcg_code": "M083",
        "case_count": len(SMOKE_CASES),
        "passed_count": passed_n,
        "failed_count": len(SMOKE_CASES) - passed_n,
        "cases": cases_out,
    }
    json_out.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    write_markdown_report(md_out, report)

    print(f"Wrote {json_out.relative_to(root)}")
    print(f"Wrote {md_out.relative_to(root)}")
    print(f"Passed {passed_n} / {len(SMOKE_CASES)}")
    if report["failed_count"]:
        for c in cases_out:
            if not c["passed"]:
                print(f"FAILED {c['case_id']}: {c.get('mismatches')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
