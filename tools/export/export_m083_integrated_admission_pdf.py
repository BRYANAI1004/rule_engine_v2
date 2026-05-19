from __future__ import annotations

"""Backward-compatible shim: M083 integrated admission PDF via the generic MCG exporter."""

import argparse

from export_mcg_integrated_admission_pdf import run_export


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain-rule-tree", required=True)
    ap.add_argument("--shared-definitions", required=True)
    ap.add_argument("--linked-condition-refs", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--title", default="M083 Integrated Admission Rule Hierarchy")
    args = ap.parse_args()

    run_export(
        mcg_code="M083",
        mcg_title="Stroke: Ischemic",
        domain_rule_tree=args.domain_rule_tree,
        shared_definitions=args.shared_definitions,
        linked_condition_refs=args.linked_condition_refs,
        output=args.output,
        scope="admission",
        document_title=args.title,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
