"""Demand-driven shared-definition bundles for M190 admission-named condition keys."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable


def _iter_definitions(defs_raw: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for key in ("definitions", "popups"):
        for d in defs_raw.get(key) or []:
            if isinstance(d, dict):
                yield d


def _find_definition(defs_raw: dict[str, Any], title: str) -> dict[str, Any] | None:
    for d in _iter_definitions(defs_raw):
        if str(d.get("title") or "") == title:
            return d
    return None


def _normalize_body(_title: str, raw_text: str) -> tuple[str, str]:
    tl = raw_text.strip().splitlines()
    body_lines: list[str] = []
    for ln in tl[1:] if tl else []:
        s = ln.strip()
        if not s or s == "Close":
            continue
        body_lines.append(s)
    return ((tl[0].strip() if tl else "").strip(), "\n".join(body_lines).strip())


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
    return {
        "source_popup_title": md["source_popup_title"],
        "source_popup_id": md["source_popup_id"],
        "popup_text_hash": md["source_text_hash"] or None,
        "source_quote": md["source_quote"] or None,
    }


def collect_m190_admission_named_condition_keys(domain_doc: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for n in domain_doc.get("logic_nodes") or []:
        if not isinstance(n, dict):
            continue
        dn = str(n.get("linked_domain_node_id") or "")
        if not dn.startswith("M190.admission."):
            continue
        ck = str(n.get("condition_key") or "").strip()
        if ck and not ck.startswith("leaf_"):
            out.add(ck)
    return out


def build_tachypnea_bundle(*, canon: str, meta: dict[str, Any]) -> dict[str, Any]:
    required_lines = (
        "Tachypnea,[A][B] as indicated by 1 or more of the following:",
        "Respiratory rate greater than 20 breaths per minute in adult[A][B](1)",
        "Respiratory rate greater than 18 breaths per minute in child 13 to 17 years of age[A][B](2)",
        "Respiratory rate greater than 22 breaths per minute in child 6 to 12 years of age[A][B](2)",
        "Respiratory rate greater than 25 breaths per minute in child 3 to 5 years of age[A][B](2)",
        "Respiratory rate greater than 30 breaths per minute in child 1 or 2 years of age[A][B](2)",
        "Respiratory rate greater than 40 breaths per minute in infant 6 to 11 months of age[A][B](2)",
        "Respiratory rate greater than 45 breaths per minute in infant 3 to 5 months of age[A][B](2)",
        "Respiratory rate greater than 55 breaths per minute in infant 1 or 2 months of age[A][B](2)",
    )
    canon_lc = canon.casefold()
    missing = [ln for ln in required_lines if ln.casefold() not in canon_lc]
    if missing:
        raise ValueError(
            "Tachypnea definition capture missing required excerpts; refusing to synthesize:\n"
            + "\n".join(f"- {m}" for m in missing)
        )
    prov = _tag(meta)
    atoms: list[dict[str, Any]] = [
        {
            **prov,
            "id": "atom.tachypnea.adult.rr_gt_20",
            "condition_key": "respiratory_rate_bpm",
            "definition_type": "atomic_numeric",
            "measurement": "respiratory_rate",
            "operator": ">",
            "value": 20,
            "unit": "breaths/min",
            "original_text": "Respiratory rate greater than 20 breaths per minute in adult[A][B](1)",
            "review_status": "ok",
        },
    ]
    ped_specs: tuple[tuple[str, str, int, str], ...] = (
        (
            "atom.tachypnea.pediatric.rr_gt_18_13_17",
            "Respiratory rate greater than 18 breaths per minute in child 13 to 17 years of age[A][B](2)",
            18,
            "13_to_17_years",
        ),
        (
            "atom.tachypnea.pediatric.rr_gt_22_6_12",
            "Respiratory rate greater than 22 breaths per minute in child 6 to 12 years of age[A][B](2)",
            22,
            "6_to_12_years",
        ),
        (
            "atom.tachypnea.pediatric.rr_gt_25_3_5",
            "Respiratory rate greater than 25 breaths per minute in child 3 to 5 years of age[A][B](2)",
            25,
            "3_to_5_years",
        ),
        (
            "atom.tachypnea.pediatric.rr_gt_30_1_2y",
            "Respiratory rate greater than 30 breaths per minute in child 1 or 2 years of age[A][B](2)",
            30,
            "1_or_2_years",
        ),
        (
            "atom.tachypnea.pediatric.rr_gt_40_infant_6_11m",
            "Respiratory rate greater than 40 breaths per minute in infant 6 to 11 months of age[A][B](2)",
            40,
            "6_to_11_months",
        ),
        (
            "atom.tachypnea.pediatric.rr_gt_45_infant_3_5m",
            "Respiratory rate greater than 45 breaths per minute in infant 3 to 5 months of age[A][B](2)",
            45,
            "3_to_5_months",
        ),
        (
            "atom.tachypnea.pediatric.rr_gt_55_infant_1_2m",
            "Respiratory rate greater than 55 breaths per minute in infant 1 or 2 months of age[A][B](2)",
            55,
            "1_or_2_months",
        ),
    )
    for aid, otext, val, age_range in ped_specs:
        atoms.append(
            {
                **prov,
                "id": aid,
                "condition_key": "respiratory_rate_bpm",
                "definition_type": "atomic_numeric",
                "measurement": "respiratory_rate",
                "operator": ">",
                "value": val,
                "unit": "breaths/min",
                "age_range": age_range,
                "original_text": otext,
                "review_status": "needs_review",
                "review_notes": "Pediatric age stratum threshold; evaluator must apply age handling.",
            }
        )

    comps: list[dict[str, Any]] = [
        {
            **prov,
            "id": "comp.tachypnea.root",
            "condition_key": "tachypnea_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [a["id"] for a in atoms],
            "original_text": "Tachypnea,[A][B] as indicated by 1 or more of the following:",
            "review_status": "ok",
        }
    ]

    return dict(
        atomic_rules=atoms,
        composite_definitions=comps,
        condition_stub=dict(
            condition_key="tachypnea_condition_present",
            display_name="Tachypnea",
            definition_type="shared_composite_condition",
            root_composite_id="comp.tachypnea.root",
            source_original_text=canon,
            review_status="draft",
        ),
    )


def build_altered_mental_status_severe_persistent_bundle(meta: dict[str, Any]) -> dict[str, Any]:
    prov = _tag(meta)

    atom_specs = (
        (
            "atom.amssevere.confusional_persists",
            "Confusional state (eg, disorientation, difficulty following commands, deficit in attention) that persists despite appropriate treatment (eg, of underlying cause, despite observation care)",
        ),
        (
            "atom.amssevere.lethargy_persists",
            "Lethargy (eg, awake or arousable, but with drowsiness; reduced awareness of self and environment) that persists despite appropriate treatment (eg, of underlying cause, despite observation care)",
        ),
        (
            "atom.amssevere.obtundation",
            "Obtundation (ie, arousable only with strong stimuli, lessened interest in environment, slowed responses to stimulation)",
        ),
        ("atom.amssevere.stupor", "Stupor (ie, may be arousable but patient does not return to normal baseline level of awareness)"),
        ("atom.amssevere.coma", "Coma (ie, not arousable)"),
    )
    atoms = [
        {
            **prov,
            "id": aid,
            "condition_key": "altered_mental_status_severe_persistent_clause_condition_present",
            "definition_type": "atomic_flag",
            "operator": "IS_TRUE",
            "original_text": txt,
            "review_status": "ok",
        }
        for aid, txt in atom_specs
    ]

    comps = [
        {
            **prov,
            "id": "comp.altered_mental_status.severe_persistent.root",
            "condition_key": "altered_mental_status_severe_or_persistent_condition_present",
            "definition_type": "composite",
            "operator": "OR",
            "children": [spec[0] for spec in atom_specs],
            "original_text": (
                "Altered mental status (ie, different from baseline) that is severe or persistent, "
                "as indicated by 1 or more of the following(1)(2)(3)(4):"
            ),
            "review_status": "ok",
        }
    ]
    return dict(
        atomic_rules=atoms,
        composite_definitions=comps,
        condition_stub=dict(
            condition_key="altered_mental_status_severe_or_persistent_condition_present",
            display_name="Altered mental status (severe or persistent)",
            definition_type="shared_composite_condition",
            root_composite_id="comp.altered_mental_status.severe_persistent.root",
            source_original_text="Definition - Altered mental status that is severe or persistent",
            review_status="draft",
        ),
    )


def collect_m190_extra_bundles_and_provenance(
    defs_raw: dict[str, Any],
    domain_doc: dict[str, Any],
    existing_condition_keys: set[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    req = collect_m190_admission_named_condition_keys(domain_doc)
    want = req - existing_condition_keys
    bundles: list[dict[str, Any]] = []
    prov_meta: dict[str, dict[str, Any]] = {}

    def _need(title: str) -> tuple[dict[str, Any], str]:
        pop = _find_definition(defs_raw, title)
        if pop is None:
            raise ValueError(f"Missing M190 definition popup: {title!r}")
        m = _popup_meta(pop)
        _, c = _normalize_body(str(pop.get("title") or ""), str(pop.get("text") or ""))
        return m, c

    def _synthetic_meta(note: str) -> dict[str, Any]:
        return {
            "source_popup_title": "M190 Admission (synthetic linkage)",
            "source_popup_id": "",
            "source_text_hash": "",
            "source_quote": note.strip(),
            "popup_definition_hint": "",
        }

    def _verbatim_composite_bundle(
        *,
        ck: str,
        display: str,
        prose: str,
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        prov = _tag(meta)
        h = hashlib.sha256(ck.encode()).hexdigest()[:14]
        aid = f"atom.m190 verbatim.{h}"
        rid = f"comp.m190 verbatim.{h}"
        atoms = [
            {
                **prov,
                "id": aid,
                "condition_key": ck + "_verbatim_fact_condition_present",
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": prose.strip(),
                "review_status": "needs_review",
                "review_notes": "Admission-linked composite; verbatim bullet pending richer modeling.",
            }
        ]
        comps = [
            {
                **prov,
                "id": rid,
                "condition_key": ck,
                "definition_type": "composite",
                "operator": "OR",
                "children": [aid],
                "original_text": prose.strip(),
                "review_status": "needs_review",
                "structural_grouping_note": (
                    "Singleton OR placeholder grouping one admission-bullet atom; expandable when modeled."
                ),
            }
        ]
        return dict(
            atomic_rules=atoms,
            composite_definitions=comps,
            condition_stub=dict(
                condition_key=ck,
                display_name=display,
                definition_type="shared_composite_condition",
                root_composite_id=rid,
                source_original_text=prose.strip(),
                review_status="needs_review",
            ),
        )

    tach_want_standalone = "tachypnea_condition_present" in want
    tach_want_persistent = "persistent_tachypnea_despite_observation_care_condition_present" in want

    if tach_want_standalone or tach_want_persistent:
        tm, tc = _need("Definition - Tachypnea")
        t_bundle = build_tachypnea_bundle(canon=tc, meta=tm)
        prov_meta["tachypnea_condition_present"] = tm

        if tach_want_standalone:
            bundles.append(t_bundle)

        if tach_want_persistent:
            prov_pt = dict(tm)
            prov_pt["source_quote"] = str(prov_pt.get("source_quote") or "") + " | admission tachypnea persistence clause"
            prov_meta["persistent_tachypnea_despite_observation_care_condition_present"] = prov_pt
            prov = _tag(prov_pt)
            atoms_pt = [
                {
                    **prov,
                    "id": "atom.persistent_tachypnea.observation_failure_context",
                    "condition_key": "tachypnea_persistence_despite_observation_care_condition_present",
                    "definition_type": "atomic_flag",
                    "operator": "IS_TRUE",
                    "original_text": "Tachypnea that persists despite observation care.",
                    "review_status": "ok",
                }
            ]
            comps_pt = [
                {
                    **prov,
                    "id": "comp.persistent_tachypnea_observation.root",
                    "condition_key": "persistent_tachypnea_despite_observation_care_condition_present",
                    "definition_type": "composite",
                    "operator": "AND",
                    "children": ["comp.tachypnea.root", "atom.persistent_tachypnea.observation_failure_context"],
                    "original_text": ("Definition - Tachypnea + admission persistence-after-observation clause."),
                    "review_status": "ok",
                }
            ]
            bundles.append(
                dict(
                    atomic_rules=t_bundle["atomic_rules"] + atoms_pt,
                    composite_definitions=t_bundle["composite_definitions"] + comps_pt,
                    condition_stub=dict(
                        condition_key="persistent_tachypnea_despite_observation_care_condition_present",
                        display_name="Persistent tachypnea despite observation care",
                        definition_type="shared_composite_condition",
                        root_composite_id="comp.persistent_tachypnea_observation.root",
                        source_original_text="M190 admission + Definition - Tachypnea",
                        review_status="draft",
                    ),
                )
            )

    if "persistent_dyspnea_above_baseline_despite_observation_care_condition_present" in want:
        m_dx, _ = _need("Definition - Respiratory distress")
        pm = dict(m_dx)
        pm["source_quote"] = str(pm.get("source_quote") or "") + " | admission dyspnea persistence clause"
        prov_meta["persistent_dyspnea_above_baseline_despite_observation_care_condition_present"] = pm
        prov = _tag(pm)
        atoms = [
            {
                **prov,
                "id": "atom.dyspnea_persistent.admission_clause",
                "condition_key": "persistent_dyspnea_above_baseline_admission_clause_condition_present",
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": (
                    "Dyspnea (above baseline) that persists despite observation care "
                    "(admission bullet; fuller respiratory-distress decomposition deferred)."
                ),
                "review_status": "needs_review",
                "review_notes": "Admission persistence clause with respiratory-distress popup provenance anchor.",
            }
        ]
        comps = [
            {
                **prov,
                "id": "comp.dyspnea_persistent_placeholder.root",
                "condition_key": "persistent_dyspnea_above_baseline_despite_observation_care_condition_present",
                "definition_type": "composite",
                "operator": "OR",
                "children": ["atom.dyspnea_persistent.admission_clause"],
                "original_text": "Dyspnea (above baseline) that persists despite observation care",
                "review_status": "needs_review",
                "structural_grouping_note": (
                    "Singleton OR placeholder linking admission persistence clause to respiratory-distress modeling."
                ),
            }
        ]
        bundles.append(
            dict(
                atomic_rules=atoms,
                composite_definitions=comps,
                condition_stub=dict(
                    condition_key="persistent_dyspnea_above_baseline_despite_observation_care_condition_present",
                    display_name="Persistent dyspnea above baseline",
                    definition_type="shared_composite_condition",
                    root_composite_id="comp.dyspnea_persistent_placeholder.root",
                    source_original_text="M190 admission + Definition - Respiratory distress (placeholder)",
                    review_status="needs_review",
                ),
            )
        )

    if "altered_mental_status_severe_or_persistent_condition_present" in want:
        m_am, canon = _need("Definition - Altered mental status that is severe or persistent")
        b = build_altered_mental_status_severe_persistent_bundle(m_am)
        opening = canon.splitlines()[0] if canon else ""
        if opening:
            b["composite_definitions"][0]["original_text"] = opening
        bundles.append(b)
        prov_meta["altered_mental_status_severe_or_persistent_condition_present"] = m_am

    if "severe_electrolyte_abnormality_requiring_inpatient_care_condition_present" in want:
        m_el, canon = _need("Definition - Severe electrolyte abnormalities requiring inpatient care")
        prov = _tag(m_el)
        lines = [ln.strip() for ln in canon.splitlines() if ln.strip()]
        baseline_txt = next(
            (ln for ln in lines if ln.lower().startswith("electrolyte abnormality is not")),
            lines[0] if lines else "",
        )
        lab_lines = [
            ln
            for ln in lines
            if re.match(r"^(Sodium|Potassium|Calcium|Phosphorus|Magnesium|Uric acid)\b", ln)
            or (
                ("not corrected to near normal" in ln.casefold())
                or ("severe finding requiring inpatient management" in ln.casefold())
            )
        ]
        atoms_el: list[dict[str, Any]] = [
            {
                **prov,
                "id": "atom.severe_el.baseline_shift",
                "condition_key": "electrolyte_abnormality_not_at_acceptable_baseline_condition_present",
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": baseline_txt,
                "review_status": "ok",
            }
        ]
        or_children: list[str] = []
        seen: set[str] = set()
        if not lab_lines:
            lab_lines.append(
                "Severe abnormality, as indicated by 1 or more of the laboratory lines in Definition - Severe electrolyte abnormalities requiring inpatient care"
            )
        for ln in lab_lines:
            if ln in seen or len(ln) < 24:
                continue
            seen.add(ln)
            h = hashlib.sha256(ln.encode()).hexdigest()[:12]
            aid = f"atom.severe_el.lab_{h}"
            if len(or_children) >= 80:
                break
            or_children.append(aid)
            atoms_el.append(
                {
                    **prov,
                    "id": aid,
                    "condition_key": "severe_electrolyte_lab_line_candidate_condition_present",
                    "definition_type": "atomic_flag",
                    "operator": "IS_TRUE",
                    "original_text": ln[:2400],
                    "review_status": "needs_review",
                    "review_notes": "Laboratory line extracted verbatim from electrolyte guideline definition.",
                }
            )

        comps_el: list[dict[str, Any]] = [
            {
                **prov,
                "id": "comp.severe_el.severity_or_bucket",
                "condition_key": "severe_electrolyte_lab_criteria_or_branch_condition_present",
                "definition_type": "composite",
                "operator": "OR",
                "children": or_children,
                "original_text": "Severe abnormality, as indicated by 1 or more of the following:",
                "review_status": "needs_review",
            },
            {
                **prov,
                "id": "comp.severe_electrolyte_inpatient.root",
                "condition_key": "severe_electrolyte_abnormality_requiring_inpatient_care_condition_present",
                "definition_type": "composite",
                "operator": "AND",
                "children": ["atom.severe_el.baseline_shift", "comp.severe_el.severity_or_bucket"],
                "original_text": "Severe electrolyte abnormalities requiring inpatient care (ALL prerequisites + severity OR)",
                "review_status": "needs_review",
                "review_notes": "Laboratory subtree is verbatim-heavy; evaluator modeling still flagged needs_review.",
            },
        ]
        bundles.append(
            dict(
                atomic_rules=atoms_el,
                composite_definitions=comps_el,
                condition_stub=dict(
                    condition_key="severe_electrolyte_abnormality_requiring_inpatient_care_condition_present",
                    display_name="Severe electrolyte abnormalities requiring inpatient care",
                    definition_type="shared_composite_condition",
                    root_composite_id="comp.severe_electrolyte_inpatient.root",
                    source_original_text=canon,
                    review_status="needs_review",
                ),
            )
        )
        prov_meta["severe_electrolyte_abnormality_requiring_inpatient_care_condition_present"] = m_el

    if "severe_anasarca_or_peripheral_edema_condition_present" in want:
        m_an, canon_an = _need("Definition - Anasarca")
        meta = dict(m_an)
        meta["source_quote"] = str(meta.get("source_quote") or "") + " | M190 admission severe peripheral edema"
        prov_meta["severe_anasarca_or_peripheral_edema_condition_present"] = meta
        prov = _tag(meta)
        atoms_a = [
            {
                **prov,
                "id": "atom.anasarca_severe.anasarca_def",
                "condition_key": "anasarca_definition_generalized_edema_condition_present",
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": canon_an[:1200],
                "review_status": "ok",
            },
            {
                **prov,
                "id": "atom.anasarca_severe.peripheral_manifestation",
                "condition_key": "severe_peripheral_edema_manifestation_clause_condition_present",
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": (
                    "Anasarca or peripheral edema that is severe "
                    "(eg, significant paroxysmal dyspnea, inability to ambulate or void, "
                    "skin breakdown, concomitant ascites)"
                ),
                "review_status": "ok",
            },
        ]
        comps_a = [
            {
                **prov,
                "id": "comp.anasarca_severe_peripheral.root",
                "condition_key": "severe_anasarca_or_peripheral_edema_condition_present",
                "definition_type": "composite",
                "operator": "AND",
                "children": ["atom.anasarca_severe.anasarca_def", "atom.anasarca_severe.peripheral_manifestation"],
                "original_text": "Severe anasarca/peripheral edema bundle (definition + admission clause).",
                "review_status": "ok",
            }
        ]
        bundles.append(
            dict(
                atomic_rules=atoms_a,
                composite_definitions=comps_a,
                condition_stub=dict(
                    condition_key="severe_anasarca_or_peripheral_edema_condition_present",
                    display_name="Severe anasarca or peripheral edema",
                    definition_type="shared_composite_condition",
                    root_composite_id="comp.anasarca_severe_peripheral.root",
                    source_original_text="Definition - Anasarca + admission clause",
                    review_status="draft",
                ),
            )
        )

    if "pulmonary_edema_with_oxygen_need_and_observation_failure_condition_present" in want:
        m_hx, _ = _need("Definition - Hypoxemia")
        prov_meta["pulmonary_edema_with_oxygen_need_and_observation_failure_condition_present"] = m_hx
        prov = _tag(m_hx)
        atoms_pe = [
            {
                **prov,
                "id": "atom.pe_combo.new_o2_need",
                "condition_key": "pulmonary_edema_new_or_increased_oxygen_need_for_spo2_target_condition_present",
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": (
                    "New need for oxygen therapy to keep oxygen saturation above 90% "
                    "(or increased FiO2 need from baseline)."
                ),
                "review_status": "needs_review",
                "review_notes": "Admission sub-bullet mirrored into shared definition (Hypoxemia provenance anchor).",
            },
            {
                **prov,
                "id": "atom.pe_combo.observation_fu",
                "condition_key": "pulmonary_edema_insufficient_improvement_with_observation_care_diuretics_condition_present",
                "definition_type": "atomic_flag",
                "operator": "IS_TRUE",
                "original_text": (
                    "Has not improved sufficiently with observation care (eg, appropriately dosed IV diuretics)."
                ),
                "review_status": "needs_review",
                "review_notes": "Admission sub-bullet mirrored into shared definition.",
            },
        ]
        comps_pe = [
            {
                **prov,
                "id": "comp.pe_with_o2_observation_bundle.root",
                "condition_key": "pulmonary_edema_with_oxygen_need_and_observation_failure_condition_present",
                "definition_type": "composite",
                "operator": "AND",
                "children": ["atom.pe_combo.new_o2_need", "atom.pe_combo.observation_fu"],
                "original_text": "Pulmonary edema and ALL of the following (compound admission bullets).",
                "review_status": "needs_review",
            }
        ]
        bundles.append(
            dict(
                atomic_rules=atoms_pe,
                composite_definitions=comps_pe,
                condition_stub=dict(
                    condition_key="pulmonary_edema_with_oxygen_need_and_observation_failure_condition_present",
                    display_name="Pulmonary edema with oxygen need + observation failure",
                    definition_type="shared_composite_condition",
                    root_composite_id="comp.pe_with_o2_observation_bundle.root",
                    source_original_text="Admission AND bundle + Hypoxemia provenance",
                    review_status="needs_review",
                ),
            )
        )

    if "very_severe_pulmonary_edema_condition_present" in want:
        m_rd, _ = _need("Definition - Respiratory distress")
        prov_meta["very_severe_pulmonary_edema_condition_present"] = m_rd
        bundles.append(
            _verbatim_composite_bundle(
                ck="very_severe_pulmonary_edema_condition_present",
                display="Very severe pulmonary edema",
                prose=(
                    "Pulmonary edema that is very severe (eg, invasive or noninvasive assisted ventilation needed, "
                    "imminent or likely; or need for 100% oxygen to keep oxygen saturation above 90%)."
                ),
                meta=m_rd,
            )
        )

    if "acute_myocardial_ischemia_with_heart_failure_admission_condition_present" in want:
        meta = _synthetic_meta("No single dedicated AMI+HF popup; linkage uses admission verbatim stub.")
        prov_meta["acute_myocardial_ischemia_with_heart_failure_admission_condition_present"] = meta
        bundles.append(
            _verbatim_composite_bundle(
                ck="acute_myocardial_ischemia_with_heart_failure_admission_condition_present",
                display="Acute myocardial ischemia associated with HF (admission)",
                prose=(
                    "Acute myocardial ischemia causing or associated with failure "
                    "(see Angina ISC or Myocardial Infarction ISC guideline as appropriate)."
                ),
                meta=meta,
            )
        )

    if "acute_renal_insufficiency_evidence_gt_50pct_egfr_reduction_from_baseline_condition_present" in want:
        m_rf, canon_rf = _need("Definition - Severe renal failure")
        mx = dict(m_rf)
        mx["source_quote"] = str(mx.get("source_quote") or "") + " | admission creatinine/eGFR 50% criterion"
        prov_meta["acute_renal_insufficiency_evidence_gt_50pct_egfr_reduction_from_baseline_condition_present"] = mx
        bundles.append(
            _verbatim_composite_bundle(
                ck="acute_renal_insufficiency_evidence_gt_50pct_egfr_reduction_from_baseline_condition_present",
                display="Creatinine ↑ with >50% eGFR drop from baseline",
                prose=(
                    "Increased creatinine with reduction of more than 50% in estimated glomerular filtration rate "
                    "from baseline."
                ),
                meta=mx,
            )
        )

    if "progressive_renal_insufficiency_evidence_gt_25pct_egfr_reduction_condition_present" in want:
        m_rf, _ = _need("Definition - Severe renal failure")
        mx = dict(m_rf)
        mx["source_quote"] = str(mx.get("source_quote") or "") + " | admission progressive eGFR 25% criterion"
        prov_meta["progressive_renal_insufficiency_evidence_gt_25pct_egfr_reduction_condition_present"] = mx
        bundles.append(
            _verbatim_composite_bundle(
                ck="progressive_renal_insufficiency_evidence_gt_25pct_egfr_reduction_condition_present",
                display="Progressive creatinine with >25% eGFR decline",
                prose=(
                    "Progressively (ongoing) rising creatinine with reduction of more than 25% in estimated "
                    "glomerular filtration rate from baseline."
                ),
                meta=mx,
            )
        )

    if "pulmonary_artery_catheter_monitoring_needed_condition_present" in want:
        m_ob, _ = _need("Definition - Observation care")
        mx = dict(m_ob)
        mx["source_quote"] = str(mx.get("source_quote") or "") + " | PA catheter admission bullet"
        prov_meta["pulmonary_artery_catheter_monitoring_needed_condition_present"] = mx
        bundles.append(
            _verbatim_composite_bundle(
                ck="pulmonary_artery_catheter_monitoring_needed_condition_present",
                display="Pulmonary artery catheter monitoring needed",
                prose="Pulmonary artery catheter monitoring needed.",
                meta=mx,
            )
        )

    return bundles, prov_meta


__all__ = [
    "collect_m190_extra_bundles_and_provenance",
    "collect_m190_admission_named_condition_keys",
    "build_tachypnea_bundle",
]
