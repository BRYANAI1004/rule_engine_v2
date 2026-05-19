#!/usr/bin/env python3
"""Step 2D: MCG definitions capture → shared condition definitions (deterministic subset)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from definition_rule_tree_types import SCHEMA_SHARED_CONDITION_DEFINITIONS

from mcg_m190_named_definition_bundles import collect_m190_extra_bundles_and_provenance


def _rel_posix(root: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p.as_posix())


def _normalize_body(_title: str, raw_text: str) -> tuple[str, str]:
    """Return (opening_line_used_for_checks, canonical_body_without_ui_noise)."""
    t = raw_text.strip()
    t = re.sub(r"\s+", " ", t)
    tl = raw_text.strip().splitlines()
    body_lines: list[str] = []
    for ln in tl[1:] if tl else []:
        s = ln.strip()
        if not s:
            continue
        if s == "Close":
            continue
        body_lines.append(s)
    canonical = "\n".join(body_lines).strip()
    opening = tl[0].strip() if tl else ""
    return opening, canonical


def _all_lines_matching_in_order(canon: str, prefixes: tuple[str, ...]) -> str:
    picked: list[str] = []
    want_i = 0
    for ln in canon.splitlines():
        if want_i >= len(prefixes):
            break
        s = ln.strip()
        pref = prefixes[want_i]
        if s.startswith(pref):
            picked.append(s)
            want_i += 1

    if want_i != len(prefixes):
        raise ValueError(
            "Sequential line capture failed while extracting pediatric excerpts:\n"
            f"- wanted {len(prefixes)} lines with prefixes:\n"
            + "\n".join(f"  - {p!r}" for p in prefixes)
            + "\n"
            f"- matched {want_i} lines ({picked[:10]}{'...' if len(picked) > 10 else ''})\n"
        )

    return "\n".join(picked)


def _popup_meta(pop: dict[str, Any]) -> dict[str, Any]:
    srcs = pop.get("trigger_sources") or []
    first = srcs[0] if isinstance(srcs, list) and srcs else {}
    quote = ""
    ctx = first.get("source_text_context") if isinstance(first, dict) else ""
    if isinstance(ctx, str) and ctx.strip():
        quote = ctx.strip()
    return {
        "source_popup_title": str(pop.get("title") or "").strip(),
        "source_popup_id": str(pop.get("popup_id") or "").strip(),
        "source_text_hash": str(pop.get("text_hash") or "").strip(),
        "source_quote": quote,
        "popup_definition_hint": str(pop.get("definition_id") or ""),
    }


def _tag(md: dict[str, Any]) -> dict[str, Any]:
    """Apply shared provenance fields to composites/atomics."""
    return {
        "source_popup_title": md["source_popup_title"],
        "source_popup_id": md["source_popup_id"],
        "popup_text_hash": md["source_text_hash"] or None,
        "source_quote": md["source_quote"] or None,
    }


def _iter_definition_records(defs_raw: dict[str, Any]):
    for key in ("definitions", "popups"):
        for d in defs_raw.get(key) or []:
            if isinstance(d, dict):
                yield d


def _find_definition(defs_raw: dict[str, Any], title: str) -> dict[str, Any] | None:
    for d in _iter_definition_records(defs_raw):
        if str(d.get("title") or "") == title:
            return d if isinstance(d, dict) else None
    return None


def _stable_deduplicate_defs(rows: list[dict[str, Any]], id_key: str = "id") -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        rid = str(r.get(id_key) or "")
        if not rid:
            continue
        if rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


def _gather_domain_admission_candidate_text(domain_doc: dict[str, Any]) -> str:
    """Join admission-linked bullets from domain_rule_tree (original + normalized)."""
    chunks: list[str] = []
    mcg = str(domain_doc.get("mcg_code") or "").strip()
    pref = f"{mcg}.admission."
    for n in domain_doc.get("logic_nodes") or []:
        if not isinstance(n, dict):
            continue
        dn = str(n.get("linked_domain_node_id") or "")
        if not dn.startswith(pref):
            continue
        for k in ("original_text", "normalized_text"):
            v = n.get(k)
            if isinstance(v, str) and v.strip():
                chunks.append(v.strip())
    return "\n".join(chunks)


def _norm_ws_lower(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()


def admission_requires_dangerous_arrhythmia_subtree(domain_admission_text: str) -> bool:
    """True when admission prose names Dangerous arrhythmia (distinct from CAI-only headings)."""
    return "dangerous arrhythmia" in _norm_ws_lower(domain_admission_text)


def admission_requires_prolonged_telemetry_dangerous_clause(domain_admission_text: str) -> bool:
    t = _norm_ws_lower(domain_admission_text)
    return "dangerous arrhythmia" in t and ("prolonged" in t or "beyond observation care time frame" in t) and "telemetry" in t


def build_tachycardia(*, canon: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Tachycardia popup → tachycardia_condition_present (OR of adult + pediatric HR thresholds)."""

    required_lines = (
        "Tachycardia,[A][B] as indicated by 1 or more of the following:",
        "Heart rate greater than 100 beats per minute in adult[A][B](1)",
        "Heart rate greater than 85 beats per minute in child 13 to 17 years of age[A][C](2)",
        "Heart rate greater than 95 beats per minute in child 6 to 12 years of age[A][C](2)",
        "Heart rate greater than 110 beats per minute in child 1 to 5 years of age[A][C](2)",
        "Heart rate greater than 120 beats per minute in infant 3 to 11 months of age[A][C](2)",
        "Heart rate greater than 150 beats per minute in infant 1 or 2 months of age[A][C](2)",
    )
    canon_lc = canon.casefold()
    missing = [ln for ln in required_lines if ln.casefold() not in canon_lc]
    if missing:
        raise ValueError(
            "Tachycardia definition capture missing required excerpts; refusing to synthesize:\n"
            + "\n".join(f"- {m}" for m in missing)
        )

    prov = _tag(meta)
    atomic_rules: list[dict[str, Any]] = []
    atom_by_id: dict[str, dict[str, Any]] = {}

    def _atom(row: dict[str, Any]) -> None:
        atom_by_id[row["id"]] = row
        atomic_rules.append(row)

    _atom(
        {
            **prov,
            "id": "atom.tachycardia.adult.hr_gt_100",
            "condition_key": "heart_rate_bpm",
            "definition_type": "atomic_numeric",
            "measurement": "heart_rate",
            "operator": ">",
            "value": 100,
            "unit": "bpm",
            "original_text": "Heart rate greater than 100 beats per minute in adult[A][B](1)",
            "review_status": "ok",
        }
    )

    ped_specs: tuple[tuple[str, str, int, str], ...] = (
        (
            "atom.tachycardia.pediatric.hr_gt_85_age_13_17",
            "Heart rate greater than 85 beats per minute in child 13 to 17 years of age[A][C](2)",
            85,
            "13_to_17_years",
        ),
        (
            "atom.tachycardia.pediatric.hr_gt_95_age_6_12",
            "Heart rate greater than 95 beats per minute in child 6 to 12 years of age[A][C](2)",
            95,
            "6_to_12_years",
        ),
        (
            "atom.tachycardia.pediatric.hr_gt_110_age_1_5",
            "Heart rate greater than 110 beats per minute in child 1 to 5 years of age[A][C](2)",
            110,
            "1_to_5_years",
        ),
        (
            "atom.tachycardia.pediatric.hr_gt_120_infant_3_11_months",
            "Heart rate greater than 120 beats per minute in infant 3 to 11 months of age[A][C](2)",
            120,
            "3_to_11_months",
        ),
        (
            "atom.tachycardia.pediatric.hr_gt_150_infant_1_2_months",
            "Heart rate greater than 150 beats per minute in infant 1 or 2 months of age[A][C](2)",
            150,
            "1_to_2_months",
        ),
    )

    for aid, otext, val, age_range in ped_specs:
        _atom(
            {
                **prov,
                "id": aid,
                "condition_key": "heart_rate_bpm",
                "definition_type": "atomic_numeric",
                "measurement": "heart_rate",
                "operator": ">",
                "value": val,
                "unit": "bpm",
                "age_range": age_range,
                "original_text": otext,
                "review_status": "needs_review",
                "review_notes": "Pediatric age stratum threshold; evaluator must apply age handling.",
            }
        )

    composite_defs: list[dict[str, Any]] = [
        {
            **prov,
            "id": "comp.tachycardia.root",
            "condition_key": "tachycardia_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [str(a["id"]) for a in atomic_rules],
            "original_text": "Tachycardia,[A][B] as indicated by 1 or more of the following:",
            "review_status": "ok",
        }
    ]

    return dict(
        atomic_rules=atomic_rules,
        composite_definitions=composite_defs,
        condition_stub=dict(
            condition_key="tachycardia_condition_present",
            display_name="Tachycardia",
            definition_type="shared_composite_condition",
            root_composite_id="comp.tachycardia.root",
            source_original_text=canon,
            review_status="draft",
        ),
    )


def build_hypotension(*, canon: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Hypotension popup → hypotension_condition_present."""

    required_frags = (
        "Hypotension, as indicated by 1 or more of the following:",
        "Hypotension in adult patient, as indicated by 1 or more of the following:",
        "Systolic blood pressure less than 90 mm Hg[A][B](1)",
        "Mean arterial pressure[C] less than 70 mm Hg[A][B]",
        "Decrease in baseline systolic blood pressure of 30 mm Hg or more, with significant signs or symptoms due to lower blood pressure",
        "Hypotension in pediatric patient, as indicated by 1 or more of the following:",
        "Systolic blood pressure less than 110 mm Hg in child 13 to 17 years of age[A][D](3)",
        "Systolic blood pressure less than 100 mm Hg in child 6 to 12 years of age[A][D](3)",
        "Systolic blood pressure less than 95 mm Hg in child 3 to 5 years of age[A][D](3)",
        "Systolic blood pressure less than 90 mm Hg in child 1 or 2 years of age[A][D](3)",
        "Systolic blood pressure less than 80 mm Hg in infant 6 to 11 months of age[A][D](3)",
        "Systolic blood pressure less than 70 mm Hg in infant 3 to 5 months of age[A][D](3)",
        "Systolic blood pressure less than 65 mm Hg in infant 1 or 2 months of age[A][D](3)",
    )
    canon_norm_lc = re.sub(r"\s+", " ", canon).casefold()
    missing = []
    for frag in required_frags:
        if frag.casefold() not in canon_norm_lc:
            missing.append(frag)
    if missing:
        raise ValueError(
            "Hypotension definition capture missing required excerpts; refusing to synthesize:\n"
            + "\n".join(f"- {m}" for m in missing[:40])
            + ("\n..." if len(missing) > 40 else "")
        )

    # Deterministic MAP line (UI noise varies around the MAP calculator link).
    adult_map_line = ""
    for ln in canon.splitlines():
        s = ln.strip()
        if "Mean arterial pressure" in s and "less than 70 mm Hg" in s:
            adult_map_line = s
            break
    if not adult_map_line:
        raise ValueError("Could not extract adult MAP < 70 line from Hypotension definition text.")

    prov = _tag(meta)
    atomic_rules: list[dict[str, Any]] = []

    def _atom(row: dict[str, Any]) -> None:
        atomic_rules.append(row)

    _atom(
        {
            **prov,
            "id": "atom.hypotension.adult.sbp_lt_90",
            "condition_key": "systolic_blood_pressure_mmhg",
            "definition_type": "atomic_numeric",
            "measurement": "SBP",
            "operator": "<",
            "value": 90,
            "unit": "mmHg",
            "original_text": "Systolic blood pressure less than 90 mm Hg[A][B](1)",
            "review_status": "ok",
        }
    )
    _atom(
        {
            **prov,
            "id": "atom.hypotension.adult.map_lt_70",
            "condition_key": "mean_arterial_pressure_mmhg",
            "definition_type": "atomic_numeric",
            "measurement": "MAP",
            "operator": "<",
            "value": 70,
            "unit": "mmHg",
            "original_text": adult_map_line,
            "review_status": "ok",
        }
    )
    _atom(
        {
            **prov,
            "id": "atom.hypotension.adult.baseline_sbp_drop_ge_30_with_symptoms",
            "condition_key": "baseline_sbp_decrease_with_symptoms_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": (
                "Decrease in baseline systolic blood pressure of 30 mm Hg or more, with significant signs or symptoms "
                "due to lower blood pressure (eg, near syncope, syncope, chest pain)[A][B](1)"
            ),
            "review_status": "ok",
        }
    )

    ped_specs: tuple[tuple[str, str, int, str], ...] = (
        (
            "atom.hypotension.pediatric.sbp_lt_110_age_13_17",
            "Systolic blood pressure less than 110 mm Hg in child 13 to 17 years of age[A][D](3)",
            110,
            "13_to_17_years",
        ),
        (
            "atom.hypotension.pediatric.sbp_lt_100_age_6_12",
            "Systolic blood pressure less than 100 mm Hg in child 6 to 12 years of age[A][D](3)",
            100,
            "6_to_12_years",
        ),
        (
            "atom.hypotension.pediatric.sbp_lt_95_age_3_5",
            "Systolic blood pressure less than 95 mm Hg in child 3 to 5 years of age[A][D](3)",
            95,
            "3_to_5_years",
        ),
        (
            "atom.hypotension.pediatric.sbp_lt_90_age_1_2",
            "Systolic blood pressure less than 90 mm Hg in child 1 or 2 years of age[A][D](3)",
            90,
            "1_to_2_years",
        ),
        (
            "atom.hypotension.pediatric.sbp_lt_80_infant_6_11_months",
            "Systolic blood pressure less than 80 mm Hg in infant 6 to 11 months of age[A][D](3)",
            80,
            "6_to_11_months",
        ),
        (
            "atom.hypotension.pediatric.sbp_lt_70_infant_3_5_months",
            "Systolic blood pressure less than 70 mm Hg in infant 3 to 5 months of age[A][D](3)",
            70,
            "3_to_5_months",
        ),
        (
            "atom.hypotension.pediatric.sbp_lt_65_infant_1_2_months",
            "Systolic blood pressure less than 65 mm Hg in infant 1 or 2 months of age[A][D](3)",
            65,
            "1_to_2_months",
        ),
    )
    for aid, otext, val, age_range in ped_specs:
        _atom(
            {
                **prov,
                "id": aid,
                "condition_key": "systolic_blood_pressure_mmhg",
                "definition_type": "atomic_numeric",
                "measurement": "SBP",
                "operator": "<",
                "value": val,
                "unit": "mmHg",
                "age_range": age_range,
                "original_text": otext,
                "review_status": "needs_review",
                "review_notes": "Pediatric age stratum SBP threshold; evaluator must apply age handling.",
            }
        )

    composite_defs: list[dict[str, Any]] = [
        {
            **prov,
            "id": "comp.hypotension.adult_bucket",
            "condition_key": "hypotension_in_adult_patient_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.hypotension.adult.sbp_lt_90",
                "atom.hypotension.adult.map_lt_70",
                "atom.hypotension.adult.baseline_sbp_drop_ge_30_with_symptoms",
            ],
            "original_text": "Hypotension in adult patient, as indicated by 1 or more of the following:",
            "review_status": "ok",
        },
        {
            **prov,
            "id": "comp.hypotension.pediatric_bucket",
            "condition_key": "hypotension_in_pediatric_patient_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [pid for pid, _, _, _ in ped_specs],
            "original_text": "Hypotension in pediatric patient, as indicated by 1 or more of the following:",
            "review_status": "ok",
        },
        {
            **prov,
            "id": "comp.hypotension.root",
            "condition_key": "hypotension_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": ["comp.hypotension.adult_bucket", "comp.hypotension.pediatric_bucket"],
            "original_text": "Hypotension, as indicated by 1 or more of the following:",
            "review_status": "ok",
        },
    ]

    return dict(
        atomic_rules=atomic_rules,
        composite_definitions=composite_defs,
        condition_stub=dict(
            condition_key="hypotension_condition_present",
            display_name="Hypotension",
            definition_type="shared_composite_condition",
            root_composite_id="comp.hypotension.root",
            source_original_text=canon,
            review_status="draft",
        ),
    )


def build_orthostatic_hypotension(*, canon: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Orthostatic hypotension popup → orthostatic_hypotension_condition_present."""

    required = (
        "Orthostatic hypotension,[A][B] as indicated by 1 or more of the following(1)(2)(3):",
        "Fall in SBP of 20 mm Hg or more 1 to 3 minutes after patient sits or stands from recumbent position",
        "Fall in DBP of 10 mm Hg or more 1 to 3 minutes after patient sits or stands from recumbent position",
    )
    canon_lc = canon.casefold()
    missing = [x for x in required if x.casefold() not in canon_lc]
    if missing:
        raise ValueError(
            "Orthostatic hypotension definition capture missing required excerpts; refusing to synthesize:\n"
            + "\n".join(f"- {m}" for m in missing)
        )

    prov = _tag(meta)
    atomic_rules: list[dict[str, Any]] = [
        {
            **prov,
            "id": "atom.orthostatic_hypotension.sbp_drop_ge_20",
            "condition_key": "orthostatic_systolic_blood_pressure_drop_mmhg",
            "definition_type": "atomic_numeric",
            "measurement": "orthostatic_SBP_drop",
            "operator": ">=",
            "value": 20,
            "unit": "mmHg",
            "original_text": (
                "Fall in SBP of 20 mm Hg or more 1 to 3 minutes after patient sits or stands from recumbent position"
            ),
            "review_status": "ok",
        },
        {
            **prov,
            "id": "atom.orthostatic_hypotension.dbp_drop_ge_10",
            "condition_key": "orthostatic_diastolic_blood_pressure_drop_mmhg",
            "definition_type": "atomic_numeric",
            "measurement": "orthostatic_DBP_drop",
            "operator": ">=",
            "value": 10,
            "unit": "mmHg",
            "original_text": (
                "Fall in DBP of 10 mm Hg or more 1 to 3 minutes after patient sits or stands from recumbent position"
            ),
            "review_status": "ok",
        },
    ]

    composite_defs: list[dict[str, Any]] = [
        {
            **prov,
            "id": "comp.orthostatic_hypotension.root",
            "condition_key": "orthostatic_hypotension_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": ["atom.orthostatic_hypotension.sbp_drop_ge_20", "atom.orthostatic_hypotension.dbp_drop_ge_10"],
            "original_text": "Orthostatic hypotension,[A][B] as indicated by 1 or more of the following(1)(2)(3):",
            "review_status": "ok",
        }
    ]

    return dict(
        atomic_rules=atomic_rules,
        composite_definitions=composite_defs,
        condition_stub=dict(
            condition_key="orthostatic_hypotension_condition_present",
            display_name="Orthostatic hypotension",
            definition_type="shared_composite_condition",
            root_composite_id="comp.orthostatic_hypotension.root",
            source_original_text=canon,
            review_status="draft",
        ),
    )


def build_altered_mental_status(*, canon: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Altered mental status popup → altered_mental_status_condition_present (OR of subcriteria)."""

    required = (
        "Altered mental status (ie, different from baseline), as indicated by 1 or more of the following(1)(2)(3)(4):",
        "Confusional state (eg, disorientation, difficulty following commands, deficit in attention)",
        "Lethargy (eg, awake or arousable, but with drowsiness; reduced awareness of self and environment)",
        "Obtundation (ie, arousable only with strong stimuli, lessened interest in environment, slowed responses to stimulation)",
        "Stupor (ie, may be arousable but patient does not return to normal baseline level of awareness)",
        "Coma (ie, not arousable)",
    )
    canon_lc = canon.casefold()
    missing = [x for x in required if x.casefold() not in canon_lc]
    if missing:
        raise ValueError(
            "Altered mental status definition capture missing required excerpts; refusing to synthesize:\n"
            + "\n".join(f"- {m}" for m in missing)
        )

    prov = _tag(meta)
    specs: tuple[tuple[str, str, str], ...] = (
        (
            "atom.altered_mental_status.confusional_state",
            "confusional_state_condition_present",
            "Confusional state (eg, disorientation, difficulty following commands, deficit in attention)",
        ),
        (
            "atom.altered_mental_status.lethargy",
            "lethargy_condition_present",
            "Lethargy (eg, awake or arousable, but with drowsiness; reduced awareness of self and environment)",
        ),
        (
            "atom.altered_mental_status.obtundation",
            "obtundation_condition_present",
            "Obtundation (ie, arousable only with strong stimuli, lessened interest in environment, slowed responses to stimulation)",
        ),
        (
            "atom.altered_mental_status.stupor",
            "stupor_condition_present",
            "Stupor (ie, may be arousable but patient does not return to normal baseline level of awareness)",
        ),
        (
            "atom.altered_mental_status.coma",
            "coma_condition_present",
            "Coma (ie, not arousable)",
        ),
    )

    atomic_rules: list[dict[str, Any]] = []
    for aid, ck, otext in specs:
        atomic_rules.append(
            {
                **prov,
                "id": aid,
                "condition_key": ck,
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": otext,
                "review_status": "ok",
            }
        )

    composite_defs: list[dict[str, Any]] = [
        {
            **prov,
            "id": "comp.altered_mental_status.root",
            "condition_key": "altered_mental_status_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [str(a["id"]) for a in atomic_rules],
            "original_text": (
                "Altered mental status (ie, different from baseline), as indicated by 1 or more of the following"
            ),
            "review_status": "ok",
        }
    ]

    return dict(
        atomic_rules=atomic_rules,
        composite_definitions=composite_defs,
        condition_stub=dict(
            condition_key="altered_mental_status_condition_present",
            display_name="Altered mental status",
            definition_type="shared_composite_condition",
            root_composite_id="comp.altered_mental_status.root",
            source_original_text=canon,
            review_status="draft",
        ),
    )


def build_hemodynamic_instability(
    *,
    canon: str,
    meta: dict[str, Any],
    tachycardia_root_id: str,
    hypotension_root_id: str,
    orthostatic_hypotension_root_id: str,
) -> dict[str, Any]:
    """Return dict with atomic_rules + composite_definitions for hemodynamic instability.

    Validates required source substrings deterministically against `canon`.
    """

    required = [
        "Hemodynamic instability, as indicated by 1 or more of the following:",
        (
            "Vital sign abnormality not corrected by appropriate treatment, as indicated "
            "by 1 or more of the following"
        ),
        (
            "Tachycardia that persists despite appropriate treatment (eg, treatment of underlying cause, "
            "despite observation care)"
        ),
        (
            "Hypotension that persists despite appropriate treatment (eg, treatment of underlying cause, "
            "despite observation care)"
        ),
        (
            "Orthostatic hypotension that persists despite appropriate treatment (eg, treatment of underlying cause, "
            "despite observation care)"
        ),
        "Severe hypotension in adult patient, as indicated by 1 or more of the following:",
        "Systolic blood pressure less than 80 mm Hg",
        "Shock index greater than 1.0 (shock index is the heart rate divided by systolic blood pressure)",
        "Mean arterial pressure",
        "less than 65 mm Hg",
        "IV inotropic or vasopressor medication required to maintain adequate blood pressure or perfusion",
        "Hypoperfusion due to hypotension, as indicated by ALL of the following:",
        "Hypotension",
        "Evidence of hypoperfusion, as indicated by 1 or more of the following:",
        "Lactate of 2.0 mmol/L (18 mg/dL) or more secondary to hypotension (ie, hypoperfusion)",
        "Metabolic acidosis (arterial or venous",
        "pH less than 7.35) not otherwise explained",
        "Significant end organ dysfunction due to hypotension (eg, Altered mental status",
        "Severe hypotension in pediatric patient, as indicated by 1 or more of the following:",
        "Shock index greater than 1.2 in child 4 to 6 years of age (shock index is the heart rate divided by systolic blood pressure)",
    ]

    canon_norm = re.sub(r"\s+", " ", canon)
    canon_norm_lc = canon_norm.casefold()

    missing: list[str] = []
    for frag in required:
        if frag.casefold() not in canon_norm_lc:
            missing.append(frag)

    if missing:
        raise ValueError(
            "Hemodynamic instability definition capture missing required excerpts; refusing to synthesize:\n"
            + "\n".join(f"- {m}" for m in missing[:25])
            + ("\n..." if len(missing) > 25 else "")
        )

    pediatric_section_m = re.search(
        r"(Severe hypotension in pediatric patient,[^\n]*\n)([\s\S]+)\Z",
        canon,
        flags=re.MULTILINE,
    )
    if not pediatric_section_m:
        raise ValueError("Could not isolate pediatric hypotension subsection from Hemodynamic instability text.")
    pediatric_section_tail = pediatric_section_m.group(2)

    ped_hblocks = []
    start_key = "Hypoperfusion due to hypotension, as indicated by ALL of the following:"
    ix = 0
    while True:
        pos = pediatric_section_tail.find(start_key, ix)
        if pos == -1:
            break
        ped_hblocks.append(pos)
        ix = pos + len(start_key)
    if len(ped_hblocks) < 1:
        raise ValueError("Expected pediatric hypoperfusion block under pediatric severe hypotension section.")
    pediatric_hypo_original = pediatric_section_tail[ped_hblocks[-1] :].strip()

    pediatric_sbp_original = _all_lines_matching_in_order(
        canon,
        (
            "Systolic blood pressure less than 80 mm Hg in child 6 to 17 years of age",
            "Systolic blood pressure less than 75 mm Hg in child 6 months to 5 years of age",
        ),
    )
    pediatric_shock_original = _all_lines_matching_in_order(
        canon,
        (
            "Shock index greater than 0.9 in child 13 to 17 years of age "
            "(shock index is the heart rate divided by systolic blood pressure)",
            "Shock index greater than 1.0 in child 7 to 12 years of age "
            "(shock index is the heart rate divided by systolic blood pressure)",
            "Shock index greater than 1.2 in child 4 to 6 years of age "
            "(shock index is the heart rate divided by systolic blood pressure)",
        ),
    )

    adult_map_original = ""
    adult_map_candidates: list[str] = []
    for ln in canon.splitlines():
        s = ln.strip()
        if ("Mean arterial pressure" in s) and ("less than 65 mm Hg" in s):
            adult_map_candidates.append(s)
            break  # deterministic: first occurrence is adult subsection
    if not adult_map_candidates:
        raise ValueError("Could not extract adult MAP threshold line from captured canon text.")
    adult_map_original = adult_map_candidates[0]

    prov = _tag(meta)

    atomic_rules: list[dict[str, Any]] = []

    atom_by_id = {}

    def _atom(row: dict[str, Any]) -> None:
        atom_by_id[row["id"]] = row
        atomic_rules.append(row)

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.context.persistence_despite_appropriate_treatment",
            "condition_key": "persistence_despite_appropriate_treatment_context_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "persists despite appropriate treatment",
            "review_status": "ok",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.adult.sbp_lt_80",
            "condition_key": "systolic_blood_pressure_mmhg",
            "definition_type": "atomic_numeric",
            "measurement": "SBP",
            "operator": "<",
            "value": 80,
            "unit": "mmHg",
            "original_text": "Systolic blood pressure less than 80 mm Hg",
            "review_status": "ok",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.adult.shock_index_gt_1",
            "condition_key": "shock_index",
            "definition_type": "atomic_numeric",
            "measurement": "shock_index",
            "operator": ">",
            "value": 1.0,
            "unit": "ratio",
            "original_text": (
                "Shock index greater than 1.0 (shock index is the heart rate divided by systolic blood pressure)"
            ),
            "review_status": "ok",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.adult.map_lt_65",
            "condition_key": "mean_arterial_pressure_mmhg",
            "definition_type": "atomic_numeric",
            "measurement": "MAP",
            "operator": "<",
            "value": 65,
            "unit": "mmHg",
            "original_text": adult_map_original,
            "review_status": "ok",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.adult.vasopressor_or_inotrope_required",
            "condition_key": "vasopressor_or_inotrope_required",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": ("IV inotropic or vasopressor medication required to maintain adequate blood pressure or perfusion"),
            "review_status": "ok",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.hypoperfusion.lactate_gte_2",
            "condition_key": "lactate_mmol_l",
            "definition_type": "atomic_numeric",
            "measurement": "lactate",
            "operator": ">=",
            "value": 2.0,
            "unit": "mmol/L",
            "original_text": ("Lactate of 2.0 mmol/L (18 mg/dL) or more secondary to hypotension (ie, hypoperfusion)"),
            "review_status": "ok",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.hypoperfusion.ph_lt_7_35",
            "condition_key": "arterial_or_venous_ph",
            "definition_type": "atomic_numeric",
            "measurement": "pH",
            "operator": "<",
            "value": 7.35,
            "unit": "pH",
            "original_text": ("Metabolic acidosis (arterial or venous pH less than 7.35) not otherwise explained"),
            "review_status": "ok",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.hypoperfusion.end_organ_dysfunction",
            "condition_key": "end_organ_dysfunction_due_to_hypotension_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": (
                "Significant end organ dysfunction due to hypotension "
                "(eg, Altered mental status, myocardial ischemia, 2-fold or higher acute increase in serum creatinine)"
            ),
            "review_status": "ok",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.pediatric.sbp_table_threshold_flag",
            "condition_key": "pediatric_systolic_blood_pressure_threshold_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": pediatric_sbp_original,
            "review_status": "needs_review",
            "review_notes": "Requires pediatric age strata / tables; thresholds are present in guideline text.",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.pediatric.shock_index_table_threshold_flag",
            "condition_key": "pediatric_shock_index_threshold_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": pediatric_shock_original,
            "review_status": "needs_review",
            "review_notes": "Requires pediatric age strata; thresholds are present in guideline text.",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.pediatric.vasopressor_or_inotrope_required",
            "condition_key": "pediatric_vasopressor_or_inotrope_required",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": ("IV inotropic or vasopressor medication required to maintain adequate blood pressure or perfusion"),
            "review_status": "needs_review",
            "review_notes": "Captured from pediatric guideline section but should be differentiated from adult dosing thresholds if needed.",
        }
    )

    _atom(
        {
            **prov,
            "id": "atom.hemodynamic_instability.pediatric.hypoperfusion_due_to_hypotension",
            "condition_key": "pediatric_hypoperfusion_due_to_hypotension_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": pediatric_hypo_original,
            "review_status": "needs_review",
            "review_notes": "Pediatric hypoperfusion block mirrors adult decomposition; fuller numeric modeling deferred.",
        }
    )

    composite_defs: list[dict[str, Any]] = []

    def _comp(row: dict[str, Any]) -> dict[str, Any]:
        composite_defs.append(row)
        return row

    _comp(
        {
            **prov,
            "id": "comp.hemodynamic_instability.evidence_hypoperfusion",
            "condition_key": "hypoperfusion_evidence_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.hemodynamic_instability.hypoperfusion.lactate_gte_2",
                "atom.hemodynamic_instability.hypoperfusion.ph_lt_7_35",
                "atom.hemodynamic_instability.hypoperfusion.end_organ_dysfunction",
            ],
            "original_text": "Evidence of hypoperfusion, as indicated by 1 or more of the following:",
            "review_status": "ok",
        }
    )

    _comp(
        {
            **prov,
            "id": "comp.hemodynamic_instability.hypoperfusion_bundle",
            "condition_key": "hypoperfusion_due_to_hypotension_condition_present",
            "definition_type": "composite",
            "operator": "AND",
            "children": [
                hypotension_root_id,
                "comp.hemodynamic_instability.evidence_hypoperfusion",
            ],
            "original_text": ("Hypoperfusion due to hypotension, as indicated by ALL of the following:"),
            "review_status": "ok",
        }
    )

    _comp(
        {
            **prov,
            "id": "comp.hemodynamic_instability.vitals.persistent_tachycardia",
            "condition_key": "persistent_tachycardia_condition_present",
            "definition_type": "composite",
            "operator": "AND",
            "children": [
                tachycardia_root_id,
                "atom.hemodynamic_instability.context.persistence_despite_appropriate_treatment",
            ],
            "original_text": (
                "Tachycardia that persists despite appropriate treatment (eg, treatment of underlying cause, despite observation care)"
            ),
            "review_status": "ok",
        }
    )

    _comp(
        {
            **prov,
            "id": "comp.hemodynamic_instability.vitals.persistent_hypotension",
            "condition_key": "persistent_hypotension_condition_present",
            "definition_type": "composite",
            "operator": "AND",
            "children": [
                hypotension_root_id,
                "atom.hemodynamic_instability.context.persistence_despite_appropriate_treatment",
            ],
            "original_text": (
                "Hypotension that persists despite appropriate treatment (eg, treatment of underlying cause, despite observation care)"
            ),
            "review_status": "ok",
        }
    )

    _comp(
        {
            **prov,
            "id": "comp.hemodynamic_instability.vitals.persistent_orthostatic_hypotension",
            "condition_key": "persistent_orthostatic_hypotension_condition_present",
            "definition_type": "composite",
            "operator": "AND",
            "children": [
                orthostatic_hypotension_root_id,
                "atom.hemodynamic_instability.context.persistence_despite_appropriate_treatment",
            ],
            "original_text": (
                "Orthostatic hypotension that persists despite appropriate treatment (eg, treatment of underlying cause, "
                "despite observation care)"
            ),
            "review_status": "ok",
        }
    )

    _comp(
        {
            **prov,
            "id": "comp.hemodynamic_instability.severe_adult_hypotension",
            "condition_key": "severe_adult_hypotension_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.hemodynamic_instability.adult.sbp_lt_80",
                "atom.hemodynamic_instability.adult.shock_index_gt_1",
                "atom.hemodynamic_instability.adult.map_lt_65",
                "atom.hemodynamic_instability.adult.vasopressor_or_inotrope_required",
                "comp.hemodynamic_instability.hypoperfusion_bundle",
            ],
            "original_text": ("Severe hypotension in adult patient, as indicated by 1 or more of the following:"),
            "review_status": "ok",
        }
    )

    _comp(
        {
            **prov,
            "id": "comp.hemodynamic_instability.vital_sign_abnormality_not_corrected",
            "condition_key": "vital_sign_abnormality_not_corrected_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "comp.hemodynamic_instability.vitals.persistent_tachycardia",
                "comp.hemodynamic_instability.vitals.persistent_hypotension",
                "comp.hemodynamic_instability.vitals.persistent_orthostatic_hypotension",
            ],
            "original_text": (
                "Vital sign abnormality not corrected by appropriate treatment, as indicated "
                "by 1 or more of the following:"
            ),
            "review_status": "ok",
        }
    )

    _comp(
        {
            **prov,
            "id": "comp.hemodynamic_instability.severe_pediatric_hypotension",
            "condition_key": "severe_pediatric_hypotension_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.hemodynamic_instability.pediatric.sbp_table_threshold_flag",
                "atom.hemodynamic_instability.pediatric.shock_index_table_threshold_flag",
                "atom.hemodynamic_instability.pediatric.vasopressor_or_inotrope_required",
                "atom.hemodynamic_instability.pediatric.hypoperfusion_due_to_hypotension",
            ],
            "original_text": ("Severe hypotension in pediatric patient, as indicated by 1 or more of the following:"),
            "review_status": "ok",
        }
    )

    _comp(
        {
            **prov,
            "id": "comp.hemodynamic_instability.root",
            "condition_key": "hemodynamic_instability_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "comp.hemodynamic_instability.vital_sign_abnormality_not_corrected",
                "comp.hemodynamic_instability.severe_adult_hypotension",
                "comp.hemodynamic_instability.severe_pediatric_hypotension",
            ],
            "original_text": ("Hemodynamic instability, as indicated by 1 or more of the following"),
            "review_status": "ok",
        }
    )

    # Deterministic composite ordering mirrors intended clinical hierarchy already.

    composite_by_id = {c["id"]: c for c in composite_defs}

    def _referenced_ids(cid: str, seen: set[str] | None = None) -> set[str]:
        if seen is None:
            seen = set()
        if cid in seen:
            return seen
        seen.add(cid)
        node = composite_by_id.get(cid)
        if not node:
            return seen
        for ch in node.get("children") or []:
            if str(ch).startswith("comp.") and str(ch) in composite_by_id:
                _referenced_ids(str(ch), seen)
            else:
                seen.add(str(ch))
        return seen

    referenced_atoms = sorted(
        x for x in _referenced_ids("comp.hemodynamic_instability.root") if str(x).startswith("atom.")
    )
    defined_atoms = sorted(atom_by_id.keys())
    orphaned_atoms = sorted(set(defined_atoms) - set(referenced_atoms))
    if orphaned_atoms:
        raise ValueError(f"Internal error: orphaned atomic_rules not reachable from root: {orphaned_atoms}")

    return dict(
        atomic_rules=atomic_rules,
        composite_definitions=composite_defs,
        condition_stub=dict(
            condition_key="hemodynamic_instability_condition_present",
            display_name="Hemodynamic instability",
            definition_type="shared_composite_condition",
            root_composite_id="comp.hemodynamic_instability.root",
            source_original_text=canon,
            review_status="draft",
        ),
    )


def build_hypoxemia(*, canon: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Definition - Hypoxemia → hypoxemia_condition_present."""

    required = (
        "Hypoxemia, as indicated by 1 or more of the following",
        "oxygen saturation (SpO2) of less than 90%",
        "PaO2) of less than 60 mm Hg (8.0 kPa)",
        "supplemental oxygen to keep SpO2 greater than 89%",
        "increased supplemental oxygen",
    )
    cn = re.sub(r"\s+", " ", canon).casefold()
    missing = [x for x in required if x.casefold() not in cn]
    if missing:
        raise ValueError(
            "Hypoxemia definition missing required excerpts; refusing to synthesize:\n"
            + "\n".join(f"- {m}" for m in missing)
        )

    prov = _tag(meta)
    atoms: list[dict[str, Any]] = []

    def _a(row: dict[str, Any]) -> None:
        atoms.append(row)

    line1 = (
        "Patient without baseline need for supplemental oxygen with oxygen saturation (SpO2) of less than 90% "
        "or arterial blood gas partial pressure of oxygen (PaO2) of less than 60 mm Hg (8.0 kPa) on room air[A][B]"
    )
    line2 = (
        "Patient without baseline need for supplemental oxygen who now requires supplemental oxygen to keep "
        "SpO2 greater than 89% or PaO2 greater than 59 mm Hg (7.9 kPa)[A]"
    )
    line3 = (
        "Patient with baseline need for supplemental oxygen who now requires increased supplemental oxygen "
        "to maintain oxygenation at baseline or acceptable level"
    )

    _a(
        {
            **prov,
            "id": "atom.hypoxemia.spo2_lt_90_percent_room_air",
            "condition_key": "spo2_percent",
            "definition_type": "atomic_numeric",
            "measurement": "SpO2",
            "operator": "<",
            "value": 90,
            "unit": "%",
            "original_text": line1,
            "review_status": "ok",
            "review_notes": "Room-air / baseline oxygen context is in the source line; evaluator applies setting.",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.hypoxemia.pao2_lt_60_mmhg_room_air",
            "condition_key": "arterial_po2_mmhg",
            "definition_type": "atomic_numeric",
            "measurement": "PaO2",
            "operator": "<",
            "value": 60,
            "unit": "mmHg",
            "original_text": line1,
            "review_status": "ok",
            "review_notes": "Paired SpO2/PaO2 threshold appears on the same source line as an OR; MCG pairs these on room air.",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.hypoxemia.supplemental_o2_required_to_maintain_targets",
            "condition_key": "supplemental_oxygen_required_to_maintain_targets_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": line2,
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.hypoxemia.increased_supplemental_oxygen_from_baseline",
            "condition_key": "increased_supplemental_oxygen_from_baseline_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": line3,
            "review_status": "ok",
        }
    )

    comps: list[dict[str, Any]] = [
        {
            **prov,
            "id": "comp.hypoxemia.room_air_hypoxemia_criteria",
            "condition_key": "hypoxemia_room_air_saturation_or_pao2_criteria_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.hypoxemia.spo2_lt_90_percent_room_air",
                "atom.hypoxemia.pao2_lt_60_mmhg_room_air",
            ],
            "original_text": line1,
            "review_status": "ok",
        },
        {
            **prov,
            "id": "comp.hypoxemia.root",
            "condition_key": "hypoxemia_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "comp.hypoxemia.room_air_hypoxemia_criteria",
                "atom.hypoxemia.supplemental_o2_required_to_maintain_targets",
                "atom.hypoxemia.increased_supplemental_oxygen_from_baseline",
            ],
            "original_text": "Hypoxemia, as indicated by 1 or more of the following(1):",
            "review_status": "ok",
        },
    ]

    return dict(
        atomic_rules=atoms,
        composite_definitions=comps,
        condition_stub=dict(
            condition_key="hypoxemia_condition_present",
            display_name="Hypoxemia",
            definition_type="shared_composite_condition",
            root_composite_id="comp.hypoxemia.root",
            source_original_text=canon,
            review_status="draft",
        ),
    )


def build_respiratory_abnormality(
    *,
    canon_hint: str,
    hypox_meta: dict[str, Any],
    impaired_meta: dict[str, Any],
    hypoxemia_root_id: str,
) -> dict[str, Any]:
    """Admission respiratory abnormalities bucket: hypoxemia composite OR impaired airway protection (source phrase)."""

    prov_root = _tag(hypox_meta)
    prov = _tag(impaired_meta)
    atoms = [
        {
            **prov,
            "id": "atom.respiratory_abnormality.impaired_airway_protection",
            "condition_key": "impaired_airway_protection_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "impaired airway protection",
            "review_status": "needs_review",
            "review_notes": (
                "Admission text lists impaired airway protection as an example without a dedicated MCG definition "
                "popup for this exact phrase; provenance uses closest respiratory distress capture for audit trail."
            ),
        }
    ]
    comps = [
        {
            **prov_root,
            "id": "comp.respiratory_abnormality.root",
            "condition_key": "respiratory_abnormality_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [hypoxemia_root_id, "atom.respiratory_abnormality.impaired_airway_protection"],
            "original_text": canon_hint,
            "review_status": "ok",
        }
    ]
    return dict(
        atomic_rules=atoms,
        composite_definitions=comps,
        condition_stub=dict(
            condition_key="respiratory_abnormality_condition_present",
            display_name="Respiratory abnormalities (admission)",
            definition_type="shared_composite_condition",
            root_composite_id="comp.respiratory_abnormality.root",
            source_original_text=canon_hint,
            review_status="draft",
        ),
    )


def build_dangerous_arrhythmia(*, canon: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Definition - Dangerous arrhythmia → dangerous_arrhythmia_condition_present."""

    required_frags = (
        "Resuscitated ventricular fibrillation or cardiac arrest",
        "Ventricular escape rhythm",
        "Sustained ventricular tachycardia (30 seconds or more",
        "Nonsustained ventricular tachycardia and 1 or more of the following",
        "Acute myocarditis",
        "Myocardial ischemia",
        "Type II second-degree atrioventricular block",
        "Third-degree atrioventricular block",
        "New-onset left bundle branch block with suspected myocardial ischemia",
        "Hypotension",
        "Respiratory distress",
        "Association with other significant symptoms (eg, syncope)",
    )
    cn = re.sub(r"\s+", " ", canon).casefold()
    missing = [x for x in required_frags if x.casefold() not in cn]
    absent_alt = (
        "resuscitated ventricular fibrillation or cardiac arrest",
        "ventricular escape rhythm",
        "nonsustained ventricular tachycardia",
        "type ii second-degree atrioventricular block",
        "third-degree atrioventricular block",
    )
    if missing and all(x in cn for x in absent_alt):
        missing = []
    if missing:
        raise ValueError(
            "Dangerous arrhythmia definition missing required excerpts; refusing to synthesize:\n"
            + "\n".join(f"- {m}" for m in missing)
        )

    prov = _tag(meta)
    atoms: list[dict[str, Any]] = []
    comps: list[dict[str, Any]] = []

    def _a(row: dict[str, Any]) -> None:
        atoms.append(row)

    def _c(row: dict[str, Any]) -> None:
        comps.append(row)

    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.vf_resuscitated_or_cardiac_arrest",
            "condition_key": "resuscitated_ventricular_fibrillation_or_cardiac_arrest_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Resuscitated ventricular fibrillation or cardiac arrest",
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.ventricular_escape_rhythm",
            "condition_key": "ventricular_escape_rhythm_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Ventricular escape rhythm",
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.sustained_vt",
            "condition_key": "sustained_ventricular_tachycardia_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": (
                "Sustained ventricular tachycardia (30 seconds or more of ventricular rhythm at greater than 100 beats per minute)"
            ),
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.nonsustained_vt",
            "condition_key": "nonsustained_ventricular_tachycardia_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Nonsustained ventricular tachycardia and 1 or more of the following:",
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.acute_myocarditis",
            "condition_key": "acute_myocarditis_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Acute myocarditis",
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.myocardial_ischemia",
            "condition_key": "myocardial_ischemia_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Myocardial ischemia",
            "review_status": "ok",
        }
    )
    _c(
        {
            **prov,
            "id": "comp.dangerous_arrhythmia.nsvt_comorbidities",
            "condition_key": "nsvt_with_acute_myocarditis_or_myocardial_ischemia_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.dangerous_arrhythmia.acute_myocarditis",
                "atom.dangerous_arrhythmia.myocardial_ischemia",
            ],
            "original_text": "Nonsustained ventricular tachycardia and 1 or more of the following:",
            "review_status": "ok",
        }
    )
    _c(
        {
            **prov,
            "id": "comp.dangerous_arrhythmia.nsvt_with_comorbidity",
            "condition_key": "nonsustained_vt_with_comorbidity_condition_present",
            "definition_type": "composite",
            "operator": "AND",
            "children": [
                "atom.dangerous_arrhythmia.nonsustained_vt",
                "comp.dangerous_arrhythmia.nsvt_comorbidities",
            ],
            "original_text": "Nonsustained ventricular tachycardia and 1 or more of the following:",
            "structural_grouping_note": (
                "MCG nests NSVT with a following OR-list; AND captures NSVT plus at least one comorbidity line."
            ),
            "review_status": "needs_review",
        }
    )

    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.av_block_type_2",
            "condition_key": "type_ii_second_degree_av_block_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Type II second-degree atrioventricular block",
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.av_block_third_degree",
            "condition_key": "third_degree_av_block_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Third-degree atrioventricular block",
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.lbbb_suspected_ischemia",
            "condition_key": "new_onset_lbbb_with_suspected_myocardial_ischemia_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "New-onset left bundle branch block with suspected myocardial ischemia",
            "review_status": "ok",
        }
    )
    _c(
        {
            **prov,
            "id": "comp.dangerous_arrhythmia.unstable_conduction_defects",
            "condition_key": "unstable_cardiac_conduction_defects_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.dangerous_arrhythmia.av_block_type_2",
                "atom.dangerous_arrhythmia.av_block_third_degree",
                "atom.dangerous_arrhythmia.lbbb_suspected_ischemia",
            ],
            "original_text": "Unstable cardiac conduction defects, as indicated by 1 or more of the following(7):",
            "review_status": "ok",
        }
    )

    _c(
        {
            **prov,
            "id": "comp.dangerous_arrhythmia.inherently_dangerous_rhythms",
            "condition_key": "dangerous_inherently_unstable_rhythm_mechanisms_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.dangerous_arrhythmia.vf_resuscitated_or_cardiac_arrest",
                "atom.dangerous_arrhythmia.ventricular_escape_rhythm",
                "atom.dangerous_arrhythmia.sustained_vt",
                "comp.dangerous_arrhythmia.nsvt_with_comorbidity",
                "comp.dangerous_arrhythmia.unstable_conduction_defects",
            ],
            "original_text": (
                "Heart rhythms that are inherently dangerous or unstable, as indicated by 1 or more of the following(5)(6):"
            ),
            "review_status": "ok",
        }
    )

    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.hypotension_concern",
            "condition_key": "hypotension_in_dangerous_arrhythmia_context_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Hypotension",
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.respiratory_distress_concern",
            "condition_key": "respiratory_distress_in_dangerous_arrhythmia_context_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Respiratory distress",
            "review_status": "ok",
        }
    )
    _a(
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.significant_symptoms_syncope",
            "condition_key": "dangerous_arrhythmia_associated_significant_symptoms_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Association with other significant symptoms (eg, syncope)(7)(9)(10)(11)",
            "review_status": "ok",
        }
    )
    _c(
        {
            **prov,
            "id": "comp.dangerous_arrhythmia.hemodynamic_symptom_concerns",
            "condition_key": "dangerous_rhythm_hemodynamic_or_symptom_concern_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.dangerous_arrhythmia.hypotension_concern",
                "atom.dangerous_arrhythmia.respiratory_distress_concern",
                "atom.dangerous_arrhythmia.significant_symptoms_syncope",
            ],
            "original_text": "Heart rhythms of concern due to 1 or more of the following(8):",
            "review_status": "ok",
        }
    )

    _c(
        {
            **prov,
            "id": "comp.dangerous_arrhythmia.root",
            "condition_key": "dangerous_arrhythmia_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "comp.dangerous_arrhythmia.inherently_dangerous_rhythms",
                "comp.dangerous_arrhythmia.hemodynamic_symptom_concerns",
            ],
            "original_text": "Dangerous arrhythmia, as indicated by 1 or more of the following(1)(2)(3)(4):",
            "review_status": "ok",
        }
    )

    return dict(
        atomic_rules=atoms,
        composite_definitions=comps,
        condition_stub=dict(
            condition_key="dangerous_arrhythmia_condition_present",
            display_name="Dangerous arrhythmia",
            definition_type="shared_composite_condition",
            root_composite_id="comp.dangerous_arrhythmia.root",
            source_original_text=canon,
            review_status="draft",
        ),
    )


def build_dangerous_arrhythmia_needs_review_placeholder(
    *,
    meta: dict[str, Any],
    review_notes: str,
) -> dict[str, Any]:
    """Minimal dangerous-arrhythmia shape when admission needs the concept but full expansion is unavailable."""
    prov = _tag(meta)
    atoms: list[dict[str, Any]] = [
        {
            **prov,
            "id": "atom.dangerous_arrhythmia.unexpanded_needs_review",
            "condition_key": "dangerous_arrhythmia_unexpanded_needs_review_placeholder",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Dangerous arrhythmia (definition capture incomplete or excerpts did not validate — placeholder).",
            "review_status": "needs_review",
            "review_notes": review_notes,
        }
    ]
    comps: list[dict[str, Any]] = [
        {
            **prov,
            "id": "comp.dangerous_arrhythmia.root",
            "condition_key": "dangerous_arrhythmia_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": ["atom.dangerous_arrhythmia.unexpanded_needs_review"],
            "original_text": "Dangerous arrhythmia — placeholder subtree pending definition capture/modeling.",
            "review_status": "needs_review",
            "review_notes": review_notes,
        }
    ]
    return dict(
        atomic_rules=atoms,
        composite_definitions=comps,
        condition_stub=dict(
            condition_key="dangerous_arrhythmia_condition_present",
            display_name="Dangerous arrhythmia",
            definition_type="shared_composite_condition",
            root_composite_id="comp.dangerous_arrhythmia.root",
            source_original_text="",
            review_status="needs_review",
            review_notes=review_notes,
        ),
    )


def build_cardiac_arrhythmia_immediate_concern(
    *,
    canon: str,
    meta: dict[str, Any],
    suspected_ischemia_line: str,
    dangerous_root_id: str | None,
    afib_meta: dict[str, Any] | None = None,
    afib_canon: str | None = None,
) -> dict[str, Any]:
    """Definition - Cardiac arrhythmias of immediate concern → reuse dangerous arrhythmia + AFib + admission-only clauses."""

    required = (
        "Cardiac arrhythmias or findings of immediate concern",
        "Continuous long-term ECG monitoring needed",
        "automatic implantable cardioverter-defibrillator",
    )
    cn = re.sub(r"\s+", " ", canon).casefold()
    missing = [x for x in required if x.casefold() not in cn]
    if missing:
        raise ValueError(
            "Cardiac arrhythmias of immediate concern definition missing required excerpts:\n"
            + "\n".join(f"- {m}" for m in missing)
        )

    prov = _tag(meta)
    afib_meta_use = afib_meta or meta
    afib_prov = _tag(afib_meta_use)
    atoms: list[dict[str, Any]] = []
    comps: list[dict[str, Any]] = []

    afib_text = (afib_canon or "").strip()
    afib_atom: dict[str, Any] = {
        **afib_prov,
        "id": "atom.cardiac_arrhythmia_immediate.clinically_active_atrial_fibrillation",
        "condition_key": "clinically_active_atrial_fibrillation_condition_present",
        "definition_type": "atomic_flag",
        "operator": "IS_TRUE",
        "original_text": afib_text,
        "review_status": "ok",
    }
    if not afib_text:
        afib_atom["original_text"] = (
            "Clinically active atrial fibrillation with rapid ventricular response requiring emergent management."
        )
        afib_atom["review_status"] = "needs_review"
        afib_atom["review_notes"] = (
            "Separate 'Definition - Clinically active atrial fibrillation' popup was not present in this MCG capture; "
            "placeholder text retained for composite linkage."
        )
    atoms.append(afib_atom)
    atoms.append(
        {
            **prov,
            "id": "atom.cardiac_arrhythmia_immediate.suspected_ischemia_nsivt",
            "condition_key": "suspected_cardiac_ischemia_as_cause_or_consequence_of_vt_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": suspected_ischemia_line.strip(),
            "review_status": "ok",
        }
    )
    atoms.append(
        {
            **prov,
            "id": "atom.cardiac_arrhythmia_immediate.continuous_long_term_ecg_monitoring",
            "condition_key": "continuous_long_term_ecg_monitoring_needed_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": (
                "Continuous long-term ECG monitoring needed (eg, initiation of drug requiring monitoring beyond observation care)"
            ),
            "review_status": "ok",
        }
    )
    atoms.append(
        {
            **prov,
            "id": "atom.cardiac_arrhythmia_immediate.aicd_firing_or_malfunction",
            "condition_key": "automatic_icd_firing_malfunction_or_setting_adjustment_needed_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": (
                "Patient has automatic implantable cardioverter-defibrillator that is repeatedly firing, malfunctioning, "
                "or in need of immediate adjustment of settings beyond scope of observation care."
            ),
            "review_status": "ok",
        }
    )

    comps.append(
        {
            **prov,
            "id": "comp.cardiac_arrhythmia_immediate.supplemental_concerns",
            "condition_key": "other_immediate_cardiac_arrhythmia_concern_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [
                "atom.cardiac_arrhythmia_immediate.suspected_ischemia_nsivt",
                "atom.cardiac_arrhythmia_immediate.continuous_long_term_ecg_monitoring",
                "atom.cardiac_arrhythmia_immediate.aicd_firing_or_malfunction",
            ],
            "original_text": (
                "Any heart rhythm and 1 or more of the following(5)(8)(10)(11)(12)(13):"
            ),
            "review_status": "ok",
        }
    )
    c_root_children: list[str] = []
    if dangerous_root_id:
        c_root_children.append(dangerous_root_id)
    c_root_children.extend(
        [
            "atom.cardiac_arrhythmia_immediate.clinically_active_atrial_fibrillation",
            "comp.cardiac_arrhythmia_immediate.supplemental_concerns",
        ]
    )
    comps.append(
        {
            **prov,
            "id": "comp.cardiac_arrhythmia_immediate.root",
            "condition_key": "cardiac_arrhythmia_of_immediate_concern_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": c_root_children,
            "original_text": "Cardiac arrhythmias or findings of immediate concern, as indicated by 1 or more of the following(1)(2)(3):",
            "review_status": "ok",
        }
    )

    return dict(
        atomic_rules=atoms,
        composite_definitions=comps,
        condition_stub=dict(
            condition_key="cardiac_arrhythmia_of_immediate_concern_condition_present",
            display_name="Cardiac arrhythmias of immediate concern",
            definition_type="shared_composite_condition",
            root_composite_id="comp.cardiac_arrhythmia_immediate.root",
            source_original_text=canon,
            review_status="draft",
        ),
    )


def build_prolonged_cardiac_telemetry_monitoring(
    *,
    admission_original_text: str,
    meta: dict[str, Any],
    dangerous_root_id: str | None,
) -> dict[str, Any]:
    """Admission bullet: prolonged telemetry for dangerous arrhythmia (not AF-only detection)."""

    prov = _tag(meta)
    atoms = [
        {
            **prov,
            "id": "atom.prolonged_telemetry.beyond_observation_timeframe",
            "condition_key": "prolonged_telemetry_beyond_observation_required",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": "Prolonged cardiac telemetry monitoring needed (eg, beyond observation care time frame)",
            "review_status": "ok",
        },
        {
            **prov,
            "id": "atom.prolonged_telemetry.not_for_atrial_fibrillation_detection_only",
            "condition_key": "atrial_fibrillation_detection_only_context_absent",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": (
                "due to concern for Dangerous arrhythmia (ie, not in effort to detect atrial fibrillation)"
            ),
            "review_status": "needs_review",
            "review_notes": (
                "Guideline uses parenthetical exclusion of AF-detection-only intent; evaluator maps to this flag."
            ),
        },
    ]

    comps = [
        {
            **prov,
            "id": "comp.prolonged_cardiac_telemetry_monitoring.root",
            "condition_key": "prolonged_cardiac_telemetry_monitoring_required",
            "definition_type": "composite",
            "operator": "AND",
            "children": [
                "atom.prolonged_telemetry.beyond_observation_timeframe",
                dangerous_root_id,
                "atom.prolonged_telemetry.not_for_atrial_fibrillation_detection_only",
            ],
            "original_text": admission_original_text.strip(),
            "review_status": "ok",
        }
    ]
    return dict(
        atomic_rules=atoms,
        composite_definitions=comps,
        condition_stub=dict(
            condition_key="prolonged_cardiac_telemetry_monitoring_required",
            display_name="Prolonged cardiac telemetry monitoring",
            definition_type="shared_composite_condition",
            root_composite_id="comp.prolonged_cardiac_telemetry_monitoring.root",
            source_original_text=admission_original_text.strip(),
            review_status="draft",
        ),
    )


def _main_audit_flags(doc: dict[str, Any]) -> dict[str, Any]:
    atoms = doc.get("atomic_rules") or []
    comp = doc.get("composite_definitions") or []
    pediatric_keys = (
        "pediatric_systolic_blood_pressure_threshold_condition_present",
        "pediatric_shock_index_threshold_condition_present",
        "pediatric_vasopressor_or_inotrope_required",
        "pediatric_hypoperfusion_due_to_hypotension_condition_present",
    )

    comp_by_id = {str(c["id"]): c for c in comp}
    dangling: list[str] = []
    singles: list[str] = []

    known = set(comp_by_id) | {str(a["id"]) for a in atoms}

    for c in comp:
        cid = str(c.get("id") or "")
        kids = list(c.get("children") or [])
        for kid in kids:
            if str(kid) not in known:
                dangling.append(f"{cid} -> missing child {kid!r}")

        # Builder sanity (validator is authoritative): composites should avoid singleton wrappers.
        if len(kids) == 1:
            singles.append(cid)

    cond_keys = {str(c.get("condition_key") or "") for c in (doc.get("conditions") or [])}

    def _persistent_is_composite(ck: str) -> bool:
        for c in comp:
            if str(c.get("condition_key") or "") != ck:
                continue
            if str(c.get("definition_type") or "") != "composite":
                continue
            if str(c.get("operator") or "") != "AND":
                continue
            kids = list(c.get("children") or [])
            return len(kids) >= 2
        return False

    tach_adult = any(
        str(a.get("condition_key")) == "heart_rate_bpm"
        and str(a.get("operator")) == ">"
        and a.get("value") == 100
        for a in atoms
    )

    flags = dict(
        mcg_code=str(doc.get("mcg_code") or ""),
        condition_count=len(doc.get("conditions") or []),
        atomic_rule_count=len(atoms),
        composite_count=len(comp),
        hemodynamic_instability_found="hemodynamic_instability_condition_present" in cond_keys,
        tachycardia_definition_found="tachycardia_condition_present" in cond_keys,
        tachycardia_adult_hr_gt_100_found=tach_adult,
        hypotension_definition_found="hypotension_condition_present" in cond_keys,
        orthostatic_hypotension_definition_found="orthostatic_hypotension_condition_present" in cond_keys,
        altered_mental_status_definition_found="altered_mental_status_condition_present" in cond_keys,
        hemodynamic_persistent_tachycardia_is_composite=_persistent_is_composite("persistent_tachycardia_condition_present"),
        hemodynamic_persistent_hypotension_is_composite=_persistent_is_composite("persistent_hypotension_condition_present"),
        hemodynamic_persistent_orthostatic_hypotension_is_composite=_persistent_is_composite(
            "persistent_orthostatic_hypotension_condition_present"
        ),
        adult_sbp_lt_80_found=False,
        adult_shock_index_gt_1_found=False,
        adult_map_lt_65_found=False,
        vasopressor_or_inotrope_required_found=False,
        lactate_gte_2_found=False,
        ph_lt_7_35_found=False,
        pediatric_rules_marked_needs_review=False,
        missing_required_rules=[],  # type: ignore[dict-item]
        dangling_child_refs=list(dangling),
        one_child_composites=list(singles),
        warnings=[],  # type: ignore[dict-item]
    )

    for row in atoms:
        ck = str(row.get("condition_key"))
        op = str(row.get("operator"))
        val = row.get("value")
        if ck == "systolic_blood_pressure_mmhg" and op == "<" and val == 80:
            flags["adult_sbp_lt_80_found"] = True
        if ck == "shock_index" and op == ">" and val == 1.0:
            flags["adult_shock_index_gt_1_found"] = True
        if ck == "mean_arterial_pressure_mmhg" and op == "<" and val == 65:
            flags["adult_map_lt_65_found"] = True
        if ck == "vasopressor_or_inotrope_required" and op == "IS_TRUE":
            flags["vasopressor_or_inotrope_required_found"] = True
        if ck == "lactate_mmol_l" and op == ">=" and val == 2.0:
            flags["lactate_gte_2_found"] = True
        if ck == "arterial_or_venous_ph" and op == "<" and val == 7.35:
            flags["ph_lt_7_35_found"] = True

    ped_rows = [a for a in atoms if str(a.get("condition_key")) in pediatric_keys]
    seen_peds = {str(a.get("condition_key")) for a in ped_rows}
    flags["pediatric_rules_marked_needs_review"] = bool(ped_rows) and seen_peds == set(
        pediatric_keys
    ) and all(str(a.get("review_status")) == "needs_review" for a in ped_rows)

    flags["missing_required_rules"] = []
    req_checks = [
        ("adult_sbp_lt_80_found", flags["adult_sbp_lt_80_found"]),
        ("adult_shock_index_gt_1_found", flags["adult_shock_index_gt_1_found"]),
        ("adult_map_lt_65_found", flags["adult_map_lt_65_found"]),
        ("vasopressor_or_inotrope_required_found", flags["vasopressor_or_inotrope_required_found"]),
        ("lactate_gte_2_found", flags["lactate_gte_2_found"]),
        ("ph_lt_7_35_found", flags["ph_lt_7_35_found"]),
    ]
    for k, ok in req_checks:
        if not ok:
            flags["missing_required_rules"].append(k)

    return flags


def _render_roundtrip(doc: dict[str, Any]) -> str:
    comp_by_id = {str(c["id"]): c for c in (doc.get("composite_definitions") or [])}
    atom_by_id = {str(a["id"]): a for a in (doc.get("atomic_rules") or [])}

    def fmt_atomic_markdown(atom: dict[str, Any]) -> str:
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

    def render_composite_innards(cid: str, inner_indent: str) -> list[str]:
        c = comp_by_id[cid]
        lines: list[str] = [f"{inner_indent}{c.get('operator')}"]
        item_indent = inner_indent + "  "
        for kid in list(c.get("children") or []):
            kid = str(kid)
            if kid.startswith("comp."):
                sub = comp_by_id[kid]
                title = str(sub.get("original_text") or sub.get("condition_key") or kid).strip().rstrip(":")
                lines.append(f"{item_indent}- {title}")
                lines.extend(render_composite_innards(kid, item_indent + "  "))
            else:
                a = atom_by_id[kid]
                lines.append(f"{item_indent}- {fmt_atomic_markdown(a)}")
        return lines

    out_lines: list[str] = []
    out_lines.append(f"# {str(doc.get('mcg_code') or '')} Shared Condition Definitions")

    conditions = doc.get("conditions") or []

    for ccond in conditions:
        ck = str(ccond.get("condition_key"))
        ttl = str(ccond.get("source_popup_title") or "")
        def_type = str(ccond.get("definition_type") or "")
        root_id = str(ccond.get("root_composite_id") or "")
        atomic_id = str(ccond.get("root_atomic_id") or "")

        out_lines.append("")
        out_lines.append(f"## {ck}")
        out_lines.append("")
        src_line = ttl if ttl else str(ccond.get("source_strategy") or "domain-sourced")
        out_lines.append(f"Source: {src_line}")
        out_lines.append("")

        if def_type == "shared_atomic_condition":
            if not atomic_id or atomic_id not in atom_by_id:
                out_lines.append("_Missing root atomic in render output._")
                out_lines.append("")
                continue
            a = atom_by_id[atomic_id]
            out_lines.append(
                f"_Atomic ({str(a.get('definition_type') or '')})_ — {fmt_atomic_markdown(a)} — "
                f"review={str(a.get('review_status') or '')}"
            )
            out_lines.append("")
            out_lines.append(str(a.get("original_text") or "").strip())
            out_lines.append("")
            continue

        if not root_id or root_id not in comp_by_id:
            out_lines.append("_Missing root composite in render output._")
            continue

        root = comp_by_id[root_id]
        out_lines.append(str(root.get("operator") or "").strip())

        indent = ""
        for kid in list(root.get("children") or []):
            kid = str(kid)
            if kid.startswith("comp."):
                sub = comp_by_id[kid]
                title = str(sub.get("original_text") or sub.get("condition_key") or kid).strip().rstrip(":")
                out_lines.append(f"{indent}- {title}")
                out_lines.extend(render_composite_innards(kid, indent + "  "))
            else:
                a = atom_by_id[kid]
                out_lines.append(f"{indent}- {fmt_atomic_markdown(a)}")

        out_lines.append("")

    return "\n".join(out_lines).rstrip() + "\n"


def _collect_admission_condition_sources(domain: dict[str, Any], mcg_code: str) -> dict[str, dict[str, Any]]:
    """First logic-node occurrence per admission-scoped condition_key (canonical domain text)."""
    prefix = f"{mcg_code}.admission."
    out: dict[str, dict[str, Any]] = {}
    for n in domain.get("logic_nodes") or []:
        if not isinstance(n, dict):
            continue
        dn = str(n.get("linked_domain_node_id") or "")
        if not dn.startswith(prefix):
            continue
        ck = str(n.get("condition_key") or "").strip()
        if not ck:
            continue
        if ck not in out:
            out[ck] = n
    return out


def _extend_m083_admission_atomic_conditions(
    *,
    domain: dict[str, Any],
    atom_rows: list[dict[str, Any]],
    composite_condition_keys: set[str],
    mcg_code: str,
) -> list[dict[str, Any]]:
    """Add domain-sourced shared_atomic_condition rows + atoms for admission leaves not covered by composites."""

    def _norm_txt(value: Any) -> str:
        return str(value or "").strip()

    existing_atom_ids = {str(a.get("id") or "") for a in atom_rows if str(a.get("id") or "")}
    by_ck = _collect_admission_condition_sources(domain, mcg_code)
    new_conditions: list[dict[str, Any]] = []

    for ck in sorted(by_ck.keys()):
        if ck.startswith("leaf_"):
            continue
        if ck in composite_condition_keys:
            continue
        node = by_ck[ck]
        atom_id = f"atom.{mcg_code}.admission.{ck}"
        if atom_id in existing_atom_ids:
            raise ValueError(f"Admission atomic id collision: {atom_id}")

        original_text = _norm_txt(node.get("original_text"))
        if not original_text:
            raise ValueError(f"Admission condition {ck!r} missing original_text on domain logic node")

        op = str(node.get("operator") or "").strip()
        atom_row: dict[str, Any]
        review_status = "ok"
        evaluator_ready = True
        source_strategy = "domain_source_atomic_leaf"
        reason: str | None = None

        if ck == "pediatric_severe_hypertension_threshold_condition_present":
            atom_row = {
                "id": atom_id,
                "condition_key": ck,
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": original_text,
                "review_status": "needs_review",
                "evaluator_ready": False,
                "source_strategy": "pediatric_placeholder",
                "reason": (
                    "Pediatric age/sex/height percentile thresholds are out of scope for detailed modeling in this pass; "
                    "preserve MCG source text only."
                ),
                "source_popup_title": None,
                "source_popup_id": None,
                "source_quote": None,
                "popup_text_hash": None,
            }
            review_status = "needs_review"
            evaluator_ready = False
            source_strategy = "pediatric_placeholder"
            reason = str(atom_row["reason"])
        elif ck == "clinical_stability_unclear_condition_present":
            atom_row = {
                "id": atom_id,
                "condition_key": ck,
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": original_text,
                "review_status": "needs_review",
                "evaluator_ready": False,
                "source_strategy": "manual_review_placeholder",
                "reason": "Broad / stability-judgment guideline wording; not expanded into subcriteria in this pass.",
                "source_popup_title": None,
                "source_popup_id": None,
                "source_quote": None,
                "popup_text_hash": None,
            }
            review_status = "needs_review"
            evaluator_ready = False
            source_strategy = "manual_review_placeholder"
            reason = str(atom_row["reason"])
        elif op and op != "IS_TRUE" and node.get("value") is not None:
            measurement = _norm_txt(node.get("measurement")) or ck
            unit = _norm_txt(node.get("unit"))
            atom_row = {
                "id": atom_id,
                "condition_key": ck,
                "definition_type": "atomic_numeric",
                "measurement": measurement,
                "operator": op,
                "value": node.get("value"),
                "unit": unit,
                "original_text": original_text,
                "review_status": "ok",
                "evaluator_ready": True,
                "source_strategy": "domain_source_numeric_leaf",
                "source_popup_title": None,
                "source_popup_id": None,
                "source_quote": None,
                "popup_text_hash": None,
            }
            source_strategy = "domain_source_numeric_leaf"
        else:
            atom_row = {
                "id": atom_id,
                "condition_key": ck,
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": original_text,
                "review_status": "ok",
                "evaluator_ready": True,
                "source_strategy": "domain_source_atomic_leaf",
                "source_popup_title": None,
                "source_popup_id": None,
                "source_quote": None,
                "popup_text_hash": None,
            }

        cond_row: dict[str, Any] = {
            "condition_key": ck,
            "definition_type": "shared_atomic_condition",
            "root_atomic_id": atom_id,
            "root_composite_id": None,
            "source_original_text": original_text,
            "review_status": review_status,
            "evaluator_ready": evaluator_ready,
            "source_strategy": source_strategy,
            "source_popup_title": None,
            "source_popup_id": None,
            "source_quote": None,
        }
        if reason:
            cond_row["reason"] = reason

        atom_rows.append(atom_row)
        existing_atom_ids.add(atom_id)
        new_conditions.append(cond_row)

    return new_conditions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mcg-code", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--definitions-json", required=True, type=Path)
    ap.add_argument("--domain-rule-tree", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args()

    root = Path(".").resolve()
    defs_path: Path = args.definitions_json
    dom_path: Path = args.domain_rule_tree
    out_dir: Path = args.out_dir

    defs_raw = json.loads(defs_path.read_text(encoding="utf-8"))
    domain_full_pre = json.loads(dom_path.read_text(encoding="utf-8"))
    admission_scan = _gather_domain_admission_candidate_text(domain_full_pre)
    need_domain_da_subtree = admission_requires_dangerous_arrhythmia_subtree(admission_scan)
    want_telemetry_bullet = admission_requires_prolonged_telemetry_dangerous_clause(admission_scan)

    def _need_popup(title: str) -> tuple[dict[str, Any], str]:
        pop = _find_definition(defs_raw, title)
        if pop is None:
            raise ValueError(f"Missing required definition popup titled exactly: {title!r}")
        raw_text = str(pop.get("text") or "")
        body_meta = _popup_meta(pop)
        _, c = _normalize_body(str(pop.get("title") or ""), raw_text)
        return body_meta, c

    da_meta_export: dict[str, Any] | None = None

    try:
        t_meta, t_canon = _need_popup("Definition - Tachycardia")
        h_meta, h_canon = _need_popup("Definition - Hypotension")
        o_meta, o_canon = _need_popup("Definition - Orthostatic hypotension")
        a_meta, a_canon = _need_popup("Definition - Altered mental status")
        hx_meta, hx_canon = _need_popup("Definition - Hypoxemia")
        rd_meta, rd_canon = _need_popup("Definition - Respiratory distress")
        ca_meta, ca_canon = _need_popup("Definition - Cardiac arrhythmias of immediate concern")
        caf_pop = _find_definition(defs_raw, "Definition - Clinically active atrial fibrillation")
        if caf_pop:
            caf_meta = _popup_meta(caf_pop)
            _, caf_canon = _normalize_body(str(caf_pop.get("title") or ""), str(caf_pop.get("text") or ""))
        else:
            caf_meta = None
            caf_canon = None
        hemo_pop = _find_definition(defs_raw, "Definition - Hemodynamic instability")
        if hemo_pop is None:
            raise ValueError("Missing required definition popup titled exactly: 'Definition - Hemodynamic instability'")
        hemo_raw = str(hemo_pop.get("text") or "")
        hemo_meta = _popup_meta(hemo_pop)
        _, hemo_canon = _normalize_body(str(hemo_pop.get("title") or ""), hemo_raw)

        tach_bundle = build_tachycardia(canon=t_canon, meta=t_meta)
        hyp_bundle = build_hypotension(canon=h_canon, meta=h_meta)
        orth_bundle = build_orthostatic_hypotension(canon=o_canon, meta=o_meta)
        alt_bundle = build_altered_mental_status(canon=a_canon, meta=a_meta)
        hemo_bundle = build_hemodynamic_instability(
            canon=hemo_canon,
            meta=hemo_meta,
            tachycardia_root_id="comp.tachycardia.root",
            hypotension_root_id="comp.hypotension.root",
            orthostatic_hypotension_root_id="comp.orthostatic_hypotension.root",
        )
        hypox_bundle = build_hypoxemia(canon=hx_canon, meta=hx_meta)
        admission_respiratory = "Respiratory abnormalities (eg, Hypoxemia, impaired airway protection)"
        resp_bundle = build_respiratory_abnormality(
            canon_hint=admission_respiratory,
            hypox_meta=hx_meta,
            impaired_meta=rd_meta,
            hypoxemia_root_id="comp.hypoxemia.root",
        )
        dangerous_root_id: str | None = None
        da_bundle: dict[str, Any] | None = None
        da_meta_export: dict[str, Any] | None = None

        if need_domain_da_subtree:
            da_pop = _find_definition(defs_raw, "Definition - Dangerous arrhythmia")
            if da_pop is None:
                da_pop = _find_definition(defs_raw, "Definition - Dangerous arrhythmia absent")
            flat_adm_preview = admission_scan.strip().replace("\n", " ")
            snippet = flat_adm_preview[:420] + ("…" if len(flat_adm_preview) > 420 else "")

            synth_meta = {
                "source_popup_title": "Definition - Dangerous arrhythmia",
                "source_popup_id": "",
                "source_text_hash": "",
                "source_quote": f"(admission excerpt) {snippet}" if snippet else "",
                "popup_definition_hint": "",
            }

            if da_pop is not None:
                da_raw = str(da_pop.get("text") or "")
                da_meta_export = _popup_meta(da_pop)
                _, da_canon = _normalize_body(str(da_pop.get("title") or ""), da_raw)
                try:
                    da_bundle = build_dangerous_arrhythmia(canon=da_canon, meta=da_meta_export)
                except ValueError as err:
                    da_bundle = build_dangerous_arrhythmia_needs_review_placeholder(
                        meta=da_meta_export,
                        review_notes=(
                            "Dangerous arrhythmia capture did not pass excerpt validation — placeholder subtree only "
                            f"({type(err).__name__}: {err})"
                        ),
                    )
            else:
                da_meta_export = synth_meta
                da_bundle = build_dangerous_arrhythmia_needs_review_placeholder(
                    meta=synth_meta,
                    review_notes=(
                        "Admission names Dangerous arrhythmia but no matching definition popup was captured "
                        "(tried 'Definition - Dangerous arrhythmia' and 'Definition - Dangerous arrhythmia absent')."
                    ),
                )

            dangerous_root_id = "comp.dangerous_arrhythmia.root"

        suspected_line = ""
        for ln in ca_canon.splitlines():
            s = ln.strip()
            if s.startswith("Suspected cardiac ischemia as cause or consequence"):
                suspected_line = s
                break
        if not suspected_line:
            raise ValueError(
                "Could not extract 'Suspected cardiac ischemia as cause or consequence...' line from cardiac arrhythmias definition."
            )
        cai_bundle = build_cardiac_arrhythmia_immediate_concern(
            canon=ca_canon,
            meta=ca_meta,
            suspected_ischemia_line=suspected_line,
            dangerous_root_id=dangerous_root_id,
            afib_meta=caf_meta,
            afib_canon=caf_canon,
        )
        admission_telemetry = (
            "Prolonged cardiac telemetry monitoring needed (eg, beyond observation care time frame) due to concern for "
            "Dangerous arrhythmia (ie, not in effort to detect atrial fibrillation) [ G ]"
        )
        tel_bundle = None
        if want_telemetry_bullet and dangerous_root_id:
            tel_bundle = build_prolonged_cardiac_telemetry_monitoring(
                admission_original_text=admission_telemetry,
                meta=ca_meta,
                dangerous_root_id=dangerous_root_id,
            )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    code = args.mcg_code
    ttl = args.title

    atom_rows: list[dict[str, Any]] = []
    comp_rows: list[dict[str, Any]] = []
    cond_rows: list[dict[str, Any]] = []
    for b in (
        tach_bundle,
        hyp_bundle,
        orth_bundle,
        alt_bundle,
        hemo_bundle,
        hypox_bundle,
        resp_bundle,
        da_bundle,
        cai_bundle,
        tel_bundle,
    ):
        if b is None:
            continue
        atom_rows.extend(b["atomic_rules"])
        comp_rows.extend(b["composite_definitions"])
        cond_rows.append(b["condition_stub"])

    m190_prov: dict[str, dict[str, Any]] = {}
    if str(code).upper() == "M190":
        _ck_have = {str(s.get("condition_key") or "") for s in cond_rows if str(s.get("condition_key") or "").strip()}
        try:
            m190_extra, m190_prov = collect_m190_extra_bundles_and_provenance(
                defs_raw, domain_full_pre, _ck_have
            )
        except ValueError as e:
            print(f"ERROR building M190 demand-driven definitions: {e}", file=sys.stderr)
            return 4

        seen_a = {str(a.get("id") or "") for a in atom_rows if str(a.get("id") or "")}
        seen_c = {str(c.get("id") or "") for c in comp_rows if str(c.get("id") or "")}
        for b in m190_extra:
            for row in b.get("atomic_rules") or []:
                rid = str(row.get("id") or "")
                if rid and rid not in seen_a:
                    seen_a.add(rid)
                    atom_rows.append(row)
            for row in b.get("composite_definitions") or []:
                cid = str(row.get("id") or "")
                if cid and cid not in seen_c:
                    seen_c.add(cid)
                    comp_rows.append(row)
            stub = b.get("condition_stub")
            if isinstance(stub, dict) and str(stub.get("condition_key") or "").strip():
                cond_rows.append(stub)
    # Per-popup provenance on condition rows (stubs do not include popup fields).
    prov_by_stub_ck: dict[str, dict[str, Any]] = {
        "tachycardia_condition_present": t_meta,
        "hypotension_condition_present": h_meta,
        "orthostatic_hypotension_condition_present": o_meta,
        "altered_mental_status_condition_present": a_meta,
        "hemodynamic_instability_condition_present": hemo_meta,
        "hypoxemia_condition_present": hx_meta,
        "respiratory_abnormality_condition_present": hx_meta,
        "cardiac_arrhythmia_of_immediate_concern_condition_present": ca_meta,
    }
    if da_meta_export is not None:
        prov_by_stub_ck["dangerous_arrhythmia_condition_present"] = da_meta_export
    if tel_bundle is not None:
        prov_by_stub_ck["prolonged_cardiac_telemetry_monitoring_required"] = ca_meta
    if m190_prov:
        prov_by_stub_ck.update(m190_prov)
    conditions_out: list[dict[str, Any]] = []
    for stub in cond_rows:
        ck = str(stub.get("condition_key") or "")
        pm = prov_by_stub_ck.get(ck, {})
        conditions_out.append({**pm, **stub})

    domain_full = domain_full_pre
    composite_cks = {str(st.get("condition_key") or "") for st in cond_rows}
    conditions_out.extend(
        _extend_m083_admission_atomic_conditions(
            domain=domain_full,
            atom_rows=atom_rows,
            composite_condition_keys=composite_cks,
            mcg_code=code,
        )
    )
    conditions_out.sort(key=lambda r: str(r["condition_key"]))

    atom_rows = _stable_deduplicate_defs(atom_rows, "id")
    comp_rows = _stable_deduplicate_defs(comp_rows, "id")

    doc: dict[str, Any] = dict(
        schema_version=SCHEMA_SHARED_CONDITION_DEFINITIONS,
        mcg_code=code,
        mcg_title=ttl,
        source_files=dict(
            definitions_raw_json=_rel_posix(root, defs_path.resolve()),
            domain_rule_tree=_rel_posix(root, dom_path.resolve()),
            definitions_json_sha256=hashlib.sha256(defs_path.read_bytes()).hexdigest(),
        ),
        conditions=sorted(conditions_out, key=lambda r: str(r["condition_key"])),
        atomic_rules=sorted(atom_rows, key=lambda r: str(r["id"])),
        composite_definitions=sorted(comp_rows, key=lambda r: str(r["id"])),
        audit={},
    )

    audit_compute = _main_audit_flags(doc)
    doc_audit = dict(
        hemodynamic_instability_complete=len(audit_compute.get("missing_required_rules") or []) == 0,
        hemodynamic_audit_flags=dict(
            adult_sbp_lt_80_found=audit_compute["adult_sbp_lt_80_found"],
            adult_shock_index_gt_1_found=audit_compute["adult_shock_index_gt_1_found"],
            adult_map_lt_65_found=audit_compute["adult_map_lt_65_found"],
            vasopressor_or_inotrope_required_found=audit_compute["vasopressor_or_inotrope_required_found"],
            lactate_gte_2_found=audit_compute["lactate_gte_2_found"],
            ph_lt_7_35_found=audit_compute["ph_lt_7_35_found"],
        ),
        built_definition_keys=sorted(prov_by_stub_ck.keys()),
    )
    doc["audit"] = doc_audit

    out_dir.mkdir(parents=True, exist_ok=True)

    stem = f"{code}.shared-condition-definitions"
    json_path = out_dir / f"{stem}.v1.json"
    audit_path = out_dir / f"{stem}.audit.json"
    md_path = out_dir / f"{stem}.roundtrip.md"
    atom_jl = out_dir / f"{stem}.atomic-rules.jsonl"
    comp_jl = out_dir / f"{stem}.composites.jsonl"
    cond_jl = out_dir / f"{stem}.conditions.jsonl"

    audit_path.write_text(json.dumps({**audit_compute, "warnings": audit_compute["warnings"]}, indent=2) + "\n", encoding="utf-8")

    json_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    atoms = doc["atomic_rules"]
    composites = doc["composite_definitions"]
    conditions = doc["conditions"]
    atom_jl.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in atoms), encoding="utf-8")
    comp_jl.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in composites), encoding="utf-8")
    cond_jl.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in conditions), encoding="utf-8")

    md_path.write_text(_render_roundtrip(doc), encoding="utf-8")

    # Helpful UX: summarize to stdout after success.
    print(f"OK wrote: {json_path}")
    print(f"OK atomic_rule_count={len(atoms)} composite_count={len(composites)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
