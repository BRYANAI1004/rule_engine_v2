#!/usr/bin/env python3
"""Structural validation for mcg_shared_condition_definitions.v1."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from definition_rule_tree_types import SCHEMA_SHARED_CONDITION_DEFINITIONS


def _fail(xs: list[str], msg: str) -> None:
    xs.append(msg)


_RE_CK = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_condition_key(ck: str) -> tuple[bool, str]:
    """Lightweight heuristic checks for snake_case identifiers + semantic suffix cues."""
    ck = ck.strip()
    if not ck or not _RE_CK.fullmatch(ck):
        return False, "must be lowercase snake_case ascii"
    tokens = ck.split("_")
    if ck.startswith("_") or ck.endswith("_"):
        return False, "no leading/trailing underscores"

    if (
        ck.endswith("_condition_present")
        or ck.endswith("_context_present")
        or ck.endswith("_required")
        or ck.endswith("_absent")
        or ck.endswith("_score")
    ):
        return True, ""
    # numeric-ish measurement identifiers (non-exhaustive, extensible step-by-step).
    numeric_ok = ck in frozenset(
        {
            "systolic_blood_pressure_mmhg",
            "diastolic_blood_pressure_mmhg",
            "mean_arterial_pressure_mmhg",
            "lactate_mmol_l",
            "arterial_or_venous_ph",
            "shock_index",
            "pulse_pressure_mmhg",
            "heart_rate_bpm",
            "respiratory_rate_bpm",
            "orthostatic_systolic_blood_pressure_drop_mmhg",
            "orthostatic_diastolic_blood_pressure_drop_mmhg",
            "spo2_percent",
            "arterial_po2_mmhg",
        }
    )
    if numeric_ok:
        return True, ""

    if ck.startswith("leaf_") and re.fullmatch(r"leaf_[a-z0-9]+_[a-f0-9]{16}", ck):
        return True, ""

    if tokens and tokens[-1] in frozenset({"increased", "decreased"}):
        return True, ""

    return False, "missing expected semantic suffix/type pattern for this vocabulary"


def _has_src_pop(a: dict[str, Any]) -> bool:
    t = str(a.get("source_popup_title") or "").strip()
    i = str(a.get("source_popup_id") or "").strip()
    return bool(t or i)


_SOURCE_STRATEGIES_POPUP_EXEMPT = frozenset(
    {
        "domain_source_atomic_leaf",
        "domain_source_numeric_leaf",
        "manual_review_placeholder",
        "pediatric_placeholder",
    }
)


def _atomic_has_popup_or_exempt(a: dict[str, Any]) -> bool:
    if str(a.get("source_strategy") or "").strip() in _SOURCE_STRATEGIES_POPUP_EXEMPT:
        return True
    return _has_src_pop(a)


def _warn(xs: list[str], msg: str) -> None:
    xs.append(msg)


PEDIATRIC_REVIEW_CONDITION_KEYS = frozenset(
    {
        "pediatric_systolic_blood_pressure_threshold_condition_present",
        "pediatric_shock_index_threshold_condition_present",
        "pediatric_hypoperfusion_due_to_hypotension_condition_present",
        "pediatric_vasopressor_or_inotrope_required",
    }
)


ADULT_ASSERTION_ATOMS = {
    # Canonical adult severe hypotension thresholds + vasopressor + hypoperfusion evidence numerics.
    "atom.hemodynamic_instability.adult.sbp_lt_80": {
        "condition_key": "systolic_blood_pressure_mmhg",
        "definition_type": "atomic_numeric",
        "operator": "<",
        "value": 80,
    },
    "atom.hemodynamic_instability.adult.shock_index_gt_1": {
        "condition_key": "shock_index",
        "definition_type": "atomic_numeric",
        "operator": ">",
        "value": 1.0,
    },
    "atom.hemodynamic_instability.adult.map_lt_65": {
        "condition_key": "mean_arterial_pressure_mmhg",
        "definition_type": "atomic_numeric",
        "operator": "<",
        "value": 65,
    },
    "atom.hemodynamic_instability.adult.vasopressor_or_inotrope_required": {
        "condition_key": "vasopressor_or_inotrope_required",
        "definition_type": "atomic_flag",
        "operator": "IS_TRUE",
    },
    "atom.hemodynamic_instability.hypoperfusion.lactate_gte_2": {
        "condition_key": "lactate_mmol_l",
        "definition_type": "atomic_numeric",
        "operator": ">=",
        "value": 2.0,
    },
    "atom.hemodynamic_instability.hypoperfusion.ph_lt_7_35": {
        "condition_key": "arterial_or_venous_ph",
        "definition_type": "atomic_numeric",
        "operator": "<",
        "value": 7.35,
    },
}


def validate(doc: dict[str, Any]) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []

    if doc.get("schema_version") != SCHEMA_SHARED_CONDITION_DEFINITIONS:
        _fail(
            errs,
            f"schema_version mismatch expected {SCHEMA_SHARED_CONDITION_DEFINITIONS!r}: {doc.get('schema_version')!r}",
        )

    comps = doc.get("composite_definitions") or []
    atoms = doc.get("atomic_rules") or []
    conds = doc.get("conditions") or []

    comp_by_id = {}
    dup_comp: set[str] = set()
    for c in comps:
        cid = str(c.get("id") or "")
        if not cid:
            _fail(errs, "composite missing id")
            continue
        if cid in comp_by_id:
            dup_comp.add(cid)
        comp_by_id[cid] = c

    if dup_comp:
        _fail(errs, f"duplicate composite ids: {sorted(dup_comp)}")

    atom_by_id = {}
    dup_atom: set[str] = set()
    for a in atoms:
        aid = str(a.get("id") or "")
        if not aid:
            _fail(errs, "atomic rule missing id")
            continue
        if aid in atom_by_id:
            dup_atom.add(aid)
        atom_by_id[aid] = a
    if dup_atom:
        _fail(errs, f"duplicate atomic ids: {sorted(dup_atom)}")

    known = set(comp_by_id) | set(atom_by_id)

    dangling: list[str] = []

    OPS = frozenset({"AND", "OR"})

    for c in comps:
        cid = str(c.get("id"))
        ck = str(c.get("condition_key") or "").strip()
        ok_ck, ck_msg = _validate_condition_key(ck)
        if not ok_ck:
            _fail(errs, f"{cid} composite condition_key invalid: {ck_msg} ({ck!r})")

        op = str(c.get("operator") or "")
        if op not in OPS:
            _fail(errs, f"{cid} composite operator invalid: {op!r}")

        ch = list(c.get("children") or [])
        if len(ch) == 1:
            review = str(c.get("review_status") or "")
            note = str(c.get("structural_grouping_note") or "")
            ot = str(c.get("original_text") or "").casefold()

            grouping_ok = review == "needs_review" and bool(note.strip())
            text_ok_for_singleton = ("all of the following" in ot) or ("1 or more" in ot)

            if not (grouping_ok or text_ok_for_singleton):
                _fail(
                    errs,
                    (
                        f"{cid} composite has exactly one child; either avoid singleton composites or document "
                        f"explicit grouping need + review rationale (needs_review + structural_grouping_note), "
                        f"OR ensure original_text clearly indicates grouped checklist/OR semantics ({cid})"
                    ),
                )

        for kid in ch:
            if str(kid) not in known:
                dangling.append(f"{cid} -> {kid}")

        dt = str(c.get("definition_type") or "")
        if dt != "composite":
            _fail(errs, f"{cid} composite definition_type unexpected: {dt!r}")

        ot = str(c.get("original_text") or "").strip()
        if not ot:
            _fail(errs, f"{cid} composite missing original_text")

        src_ok = bool(str(c.get("source_popup_title") or "").strip() or str(c.get("source_popup_id") or "").strip())
        if not src_ok:
            _fail(errs, f"{cid} composite missing source_popup_title and source_popup_id")

    pediatric_threshold_keys = frozenset(PEDIATRIC_REVIEW_CONDITION_KEYS)

    for a in atoms:
        aid = str(a.get("id") or "").strip()
        ck = str(a.get("condition_key") or "").strip()
        dt = str(a.get("definition_type") or "").strip()
        op = str(a.get("operator") or "").strip()

        ok_ck, ck_msg = _validate_condition_key(ck)
        if not ok_ck:
            _fail(errs, f"{aid} atomic condition_key invalid: {ck_msg} ({ck!r})")

        if not aid:
            continue

        if not dt:
            _fail(errs, f"{aid} missing definition_type")
        if not op:
            _fail(errs, f"{aid} missing operator")

        otp = str(a.get("original_text") or "").strip()
        if not otp:
            _fail(errs, f"{aid} missing original_text")

        if not _atomic_has_popup_or_exempt(a):
            _fail(errs, f"{aid} missing source_popup_title and source_popup_id")

        if dt == "atomic_flag":
            if op != "IS_TRUE":
                _fail(errs, f"{aid} atomic_flag must use operator IS_TRUE (got {op!r})")
        elif dt == "atomic_numeric":
            for k in ("measurement", "value", "unit"):
                if k not in a or a[k] is None or str(a.get(k)).strip() == "":
                    _fail(errs, f"{aid} numeric atomic missing required field: {k}")
        else:
            _fail(errs, f"{aid} unknown definition_type: {dt!r}")

        if ck in pediatric_threshold_keys:
            if str(a.get("review_status")) != "needs_review":
                _fail(errs, f"{aid} pediatric table-driven rule must use review_status=needs_review ({ck})")

        if str(a.get("source_strategy") or "") == "pediatric_placeholder":
            if str(a.get("review_status")) != "needs_review":
                _fail(errs, f"{aid} pediatric_placeholder atoms must use review_status=needs_review ({ck})")
            if a.get("evaluator_ready") is not False:
                _fail(errs, f"{aid} pediatric_placeholder atoms must set evaluator_ready=false ({ck})")

        if str(a.get("source_strategy") or "") == "manual_review_placeholder":
            if str(a.get("review_status")) != "needs_review":
                _fail(errs, f"{aid} manual_review_placeholder atoms must use review_status=needs_review ({ck})")
            if a.get("evaluator_ready") is not False:
                _fail(errs, f"{aid} manual_review_placeholder atoms must set evaluator_ready=false ({ck})")

        if dt == "atomic_numeric":
            age_r = a.get("age_range")
            if age_r is not None and str(age_r).strip():
                if str(a.get("review_status")) != "needs_review":
                    _fail(
                        errs,
                        f"{aid} numeric rule with age_range must use review_status=needs_review (age_range={age_r!r})",
                    )

        if ck in frozenset(
            {"systolic_blood_pressure_mmhg", "shock_index", "mean_arterial_pressure_mmhg", "lactate_mmol_l", "arterial_or_venous_ph"}
        ) and aid in ADULT_ASSERTION_ATOMS and str(a.get("review_status")) != "ok":
            # Step 2D baseline: adult numerics are fully extracted and should be stable at review_status ok.
            _warn(warns, f"{aid} adult numeric thresholds expected review_status ok (got {a.get('review_status')!r})")

    for eid, spec in ADULT_ASSERTION_ATOMS.items():
        row = atom_by_id.get(eid)
        if not row:
            _fail(errs, f"missing expected adult atomic id: {eid}")
            continue

        if str(row.get("condition_key")) != str(spec["condition_key"]):
            _fail(errs, f"{eid} condition_key mismatch: got {row.get('condition_key')!r}, want {spec['condition_key']!r}")
        if str(row.get("definition_type")) != str(spec["definition_type"]):
            _fail(errs, f"{eid} definition_type mismatch: got {row.get('definition_type')!r}, want {spec['definition_type']!r}")
        if str(row.get("operator")) != str(spec["operator"]):
            _fail(errs, f"{eid} operator mismatch: got {row.get('operator')!r}, want {spec['operator']!r}")

        if "value" in spec:
            gv = row.get("value")
            ev = spec["value"]
            try:
                gvf = float(gv)  # type: ignore[arg-type]
                evf = float(ev)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                _fail(errs, f"{eid} value not numeric/coercible: got {gv!r}")
                gvf = evf = 0.0
            if gvf != evf:
                _fail(errs, f"{eid} value mismatch: got {gv!r}, want {ev!r}")

    for cond in conds:
        ck = str(cond.get("condition_key") or "").strip()
        ok_ck, ck_msg = _validate_condition_key(ck)
        if not ok_ck:
            _fail(errs, f"condition entry condition_key invalid: {ck_msg} ({ck!r})")

        sot = str(cond.get("source_original_text") or "").strip()
        if not sot:
            _fail(errs, f"condition {ck} missing source_original_text")

        if str(cond.get("review_status")) not in frozenset({"draft", "ok", "needs_review"}):
            _warn(warns, f"condition {ck} unexpected review_status: {cond.get('review_status')!r}")

        dt_cond = str(cond.get("definition_type") or "").strip()
        rc = str(cond.get("root_composite_id") or "").strip()
        ra = str(cond.get("root_atomic_id") or "").strip()

        if dt_cond == "shared_composite_condition":
            if not rc:
                _fail(errs, f"condition {ck} missing root_composite_id")
            elif rc not in comp_by_id:
                _fail(errs, f"condition {ck} root_composite_id not found among composites: {rc!r}")
            if ra:
                _fail(errs, f"condition {ck} shared_composite_condition must not set root_atomic_id")
            if ck != comp_by_id[rc].get("condition_key"):
                _fail(errs, f"condition {ck} root composite condition_key mismatch: {comp_by_id[rc].get('condition_key')!r}")
        elif dt_cond == "shared_atomic_condition":
            if not ra:
                _fail(errs, f"condition {ck} shared_atomic_condition missing root_atomic_id")
            elif ra not in atom_by_id:
                _fail(errs, f"condition {ck} root_atomic_id not found among atomic_rules: {ra!r}")
            if rc:
                _fail(errs, f"condition {ck} shared_atomic_condition must not set root_composite_id")
            atom_root = atom_by_id[ra]
            if str(atom_root.get("condition_key") or "").strip() != ck:
                _fail(
                    errs,
                    f"condition {ck} root atomic condition_key mismatch: {atom_root.get('condition_key')!r}",
                )
            strat = str(cond.get("source_strategy") or "").strip()
            if strat in _SOURCE_STRATEGIES_POPUP_EXEMPT:
                pass
            elif not _has_src_pop(cond):
                _warn(warns, f"condition {ck} missing source_popup provenance on shared_atomic_condition row")
        else:
            _fail(errs, f"condition {ck} unexpected definition_type: {cond.get('definition_type')!r}")

        if dt_cond == "shared_composite_condition" and ck == "hemodynamic_instability_condition_present":
            if str(cond.get("source_popup_title") or "").strip().casefold() != "definition - hemodynamic instability":
                _warn(warns, f"unexpected source_popup_title on condition {ck}: {cond.get('source_popup_title')!r}")

    adult_flags_expected = dict(
        adult_sbp_lt_80_found=False,
        adult_shock_index_gt_1_found=False,
        adult_map_lt_65_found=False,
        vasopressor_or_inotrope_required_found=False,
        lactate_gte_2_found=False,
        ph_lt_7_35_found=False,
    )
    for a in atoms:
        ck = str(a.get("condition_key"))
        op = str(a.get("operator"))
        val = a.get("value")
        if ck == "systolic_blood_pressure_mmhg" and op == "<" and val == 80:
            adult_flags_expected["adult_sbp_lt_80_found"] = True
        if ck == "shock_index" and op == ">" and val == 1.0:
            adult_flags_expected["adult_shock_index_gt_1_found"] = True
        if ck == "mean_arterial_pressure_mmhg" and op == "<" and val == 65:
            adult_flags_expected["adult_map_lt_65_found"] = True
        if ck == "vasopressor_or_inotrope_required" and op == "IS_TRUE":
            adult_flags_expected["vasopressor_or_inotrope_required_found"] = True
        if ck == "lactate_mmol_l" and op == ">=" and val == 2.0:
            adult_flags_expected["lactate_gte_2_found"] = True
        if ck == "arterial_or_venous_ph" and op == "<" and val == 7.35:
            adult_flags_expected["ph_lt_7_35_found"] = True

    missing_aud = [k for k, ok in adult_flags_expected.items() if not ok]
    computed_complete = len(missing_aud) == 0
    audit_blob = dict(doc.get("audit") or {})
    stated = audit_blob.get("hemodynamic_instability_complete")
    if computed_complete != bool(stated):
        _fail(
            errs,
            (
                "audit hemodynamic_instability_complete inconsistent with deterministic adult completeness scan: "
                f"computed={computed_complete}, stated={stated!r}, missing_scan={missing_aud}"
            ),
        )

    pediatric_keys = frozenset(PEDIATRIC_REVIEW_CONDITION_KEYS)
    ped_rows = [a for a in atoms if str(a.get("condition_key")) in pediatric_keys]
    seen_ped = {str(a.get("condition_key")) for a in ped_rows}
    hemo = any(str(c.get("condition_key")) == "hemodynamic_instability_condition_present" for c in conds)
    ped_ok = (
        ped_rows and seen_ped == pediatric_keys and all(str(a.get("review_status")) == "needs_review" for a in ped_rows)
    )
    if hemo and not ped_ok:
        _fail(
            errs,
            "pediatric_* rules expectation not met for hemodynamic instability export "
            "(expected all four pediatric flags present with review_status needs_review)",
        )

    # Cross-check dangling child refs parity with composites scan.
    if dangling:
        _fail(errs, "dangling composite child refs: " + "; ".join(dangling[:40]) + (" ..." if len(dangling) > 40 else ""))

    tach_ok = any(
        str(a.get("condition_key")) == "heart_rate_bpm"
        and str(a.get("definition_type")) == "atomic_numeric"
        and str(a.get("operator")) == ">"
        and _numeric_eq(a.get("value"), 100)
        for a in atoms
    )
    if not tach_ok:
        _fail(errs, "expected tachycardia adult atomic: heart_rate_bpm numeric operator '>' value 100 (review_status ok)")

    persistent_ck_atom_flags = []
    for want_ck in (
        "persistent_tachycardia_condition_present",
        "persistent_hypotension_condition_present",
        "persistent_orthostatic_hypotension_condition_present",
    ):
        for a in atoms:
            if str(a.get("condition_key")) == want_ck and str(a.get("definition_type")) == "atomic_flag":
                persistent_ck_atom_flags.append(str(a.get("id")))
        found_comp = False
        for c in comps:
            if str(c.get("condition_key")) == want_ck and str(c.get("definition_type")) == "composite":
                if str(c.get("operator")) != "AND":
                    _fail(errs, f"persistent composite {want_ck!r} expected operator AND")
                ch = list(c.get("children") or [])
                if len(ch) < 2:
                    _fail(errs, f"persistent composite {want_ck!r} expected at least 2 children")
                found_comp = True
        if not found_comp:
            _fail(errs, f"missing composite definition for {want_ck!r} (Step 2D.2 linkage)")

    if persistent_ck_atom_flags:
        _fail(
            errs,
            "persistent tachycardia/hypotension/orthostatic must not remain plain atomic_flag rows: "
            + ", ".join(persistent_ck_atom_flags),
        )

    if str(doc.get("mcg_code")) == "M083":
        cond_ck_set = {str(c.get("condition_key")) for c in conds}
        if "hypoxemia_condition_present" in cond_ck_set:
            if "comp.hypoxemia.root" not in comp_by_id:
                _fail(errs, "M083: hypoxemia condition present but comp.hypoxemia.root missing")
            spo2_ok = any(
                str(a.get("condition_key")) == "spo2_percent"
                and str(a.get("definition_type")) == "atomic_numeric"
                and str(a.get("operator")) == "<"
                and _numeric_eq(a.get("value"), 90)
                for a in atoms
            )
            if not spo2_ok:
                _fail(errs, "M083: expected SpO2 < 90 atomic from Hypoxemia definition capture")
            pao2_ok = any(
                str(a.get("condition_key")) == "arterial_po2_mmhg"
                and str(a.get("definition_type")) == "atomic_numeric"
                and str(a.get("operator")) == "<"
                and _numeric_eq(a.get("value"), 60)
                for a in atoms
            )
            if not pao2_ok:
                _fail(errs, "M083: expected PaO2 < 60 mmHg atomic from Hypoxemia definition capture")
            o2_flag_cks = (
                "supplemental_oxygen_required_to_maintain_targets_condition_present",
                "increased_supplemental_oxygen_from_baseline_condition_present",
            )
            if (
                sum(
                    1
                    for a in atoms
                    if str(a.get("condition_key")) in o2_flag_cks and str(a.get("definition_type")) == "atomic_flag"
                )
                < 2
            ):
                _fail(errs, "M083: expected supplemental / increased oxygen requirement flag atomics from Hypoxemia text")
        if "respiratory_abnormality_condition_present" in cond_ck_set:
            r0 = comp_by_id.get("comp.respiratory_abnormality.root")
            if not r0:
                _fail(errs, "M083: comp.respiratory_abnormality.root missing")
            elif len(list(r0.get("children") or [])) < 2:
                _fail(errs, "M083: respiratory abnormalities root expected at least two OR branches")
        da_ck = "dangerous_arrhythmia_condition_present" in cond_ck_set
        if da_ck:
            if "comp.dangerous_arrhythmia.root" not in comp_by_id:
                _fail(errs, "M083: comp.dangerous_arrhythmia.root missing")
        if "cardiac_arrhythmia_of_immediate_concern_condition_present" in cond_ck_set:
            croot = comp_by_id.get("comp.cardiac_arrhythmia_immediate.root")
            if not croot:
                _fail(errs, "M083: comp.cardiac_arrhythmia_immediate.root missing")
            elif da_ck and "comp.dangerous_arrhythmia.root" not in list(croot.get("children") or []):
                _fail(
                    errs,
                    "M083: cardiac arrhythmia immediate root must reference comp.dangerous_arrhythmia.root "
                    "when dangerous arrhythmia is exported",
                )
        if "prolonged_cardiac_telemetry_monitoring_required" in cond_ck_set:
            tel = comp_by_id.get("comp.prolonged_cardiac_telemetry_monitoring.root")
            if not tel:
                _fail(errs, "M083: comp.prolonged_cardiac_telemetry_monitoring.root missing")
            elif str(tel.get("operator")) != "AND":
                _fail(errs, "M083: prolonged telemetry root expected AND")
            elif len(list(tel.get("children") or [])) < 3:
                _fail(errs, "M083: prolonged telemetry root expected three AND children")
            elif "comp.dangerous_arrhythmia.root" not in list(tel.get("children") or []):
                _fail(errs, "M083: prolonged telemetry must embed comp.dangerous_arrhythmia.root")

        dom_rel = (doc.get("source_files") or {}).get("domain_rule_tree")
        if isinstance(dom_rel, str) and dom_rel.strip():
            dom_path = Path(dom_rel)
            if dom_path.is_file():
                domain_doc = json.loads(dom_path.read_text(encoding="utf-8"))
                admission_required: set[str] = set()
                mcg_d = str(domain_doc.get("mcg_code") or "")
                adm_p = f"{mcg_d}.admission."
                for n in domain_doc.get("logic_nodes") or []:
                    if not isinstance(n, dict):
                        continue
                    dn = str(n.get("linked_domain_node_id") or "")
                    if not dn.startswith(adm_p):
                        continue
                    ck_ad = str(n.get("condition_key") or "").strip()
                    if ck_ad:
                        admission_required.add(ck_ad)
                missing_adm = sorted(admission_required - cond_ck_set)
                if missing_adm:
                    _fail(
                        errs,
                        f"{mcg_d} admission domain logic keys missing shared `conditions` rows: " + ", ".join(missing_adm),
                    )

    return errs, warns


def _numeric_eq(got: Any, expected: float | int) -> bool:
    try:
        return float(got) == float(expected)
    except (TypeError, ValueError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, type=Path)
    args = ap.parse_args()

    doc = json.loads(args.input.read_text(encoding="utf-8"))
    errs, warns = validate(doc)

    for w in warns:
        print(f"WARN: {w}", file=sys.stderr)

    if errs:
        print("INVALID shared condition definitions artifact:", file=sys.stderr)
        for e in errs:
            print(f"- {e}", file=sys.stderr)
        return 1

    print("OK: shared condition definitions validate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
