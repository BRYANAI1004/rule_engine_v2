#!/usr/bin/env python3
"""Step 2B: source-tree (.v1) → domain rule tree (deterministic)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict, deque
from pathlib import Path as Pth
from typing import Any

from domain_rule_tree_types import SCHEMA_DOMAIN_RULE_TREE, SCHEMA_SOURCE_TREE


def strip_txt(t: str) -> str:
    t = re.sub(r"\[\s*[A-Za-z]\s*\]", "", t or "")
    t = re.sub(r"\(\s*\d[^\)]*", "", t)
    return " ".join(t.split()).strip(" :")


def ref_hash(mcg: str, sid: str) -> str:
    return f"sr.{mcg}.{hashlib.sha256(str(sid).encode()).hexdigest()[:14]}"


def leaf_condition_key(mcg: str, sid: str) -> str:
    """Stable non–source-id condition key for auto-generated atomic leaves (no src_ / source_ fragments)."""
    h = hashlib.sha256(str(sid).encode()).hexdigest()[:16]
    return f"leaf_{mcg}_{h}"


class Idx:
    def __init__(self, src_nodes: list[dict]):
        self.by = {str(n["source_node_id"]): n for n in src_nodes}
        self.ch: dict[str | None, list[dict]] = defaultdict(list)
        for n in src_nodes:
            p = str(n["parent_source_node_id"]) if n.get("parent_source_node_id") else None
            self.ch[p].append(n)
        for k in self.ch:
            self.ch[k].sort(key=lambda x: int(x["sort_order"]))

    def __getitem__(self, sid: str) -> dict:
        return self.by[sid]

    def C(self, p: str | None) -> list[dict]:
        return list(self.ch.get(p, []))


class Refs:
    def __init__(self, mc: str):
        self.mc = mc
        self.R: dict[str, dict[str, Any]] = {}

    def put(self, ix: Idx, df: str | None, sid: str) -> str:
        sid = str(sid)
        n = ix[sid]
        rid = ref_hash(self.mc, sid)
        if rid not in self.R:
            self.R[rid] = dict(
                source_ref_id=rid,
                mcg_code=self.mc,
                source_node_id=sid,
                source_section_id=n.get("section_id"),
                domain=df if df is None else str(n.get("domain") or df),
                source_quote=str(n.get("original_text")),
                footnote_refs=list(n.get("footnote_refs") or []),
                reference_refs=list(n.get("reference_refs") or []),
                text_hash=str(n.get("text_hash") or ""),
            )
        return rid

    def tag(self, b: dict, ix: Idx, df: str | None, sids: list[str]) -> None:
        xs = list(dict.fromkeys(map(str, sids)))
        b["source_node_ids"] = xs
        b["source_ref_ids"] = [self.put(ix, df, x) for x in xs]


class Forge:

    _sort_seq = 0

    def __init__(self, L: list[dict], refs: Refs, ix: Idx, link_dn: str, dm: str):
        self.L, self.r, self.idx, self.link_dn, self.dm = L, refs, ix, link_dn, dm

    def seq(self) -> int:
        Forge._sort_seq += 1
        return Forge._sort_seq

    def A(
        self,
        nid: str,
        parent,
        ck: str | None,
        op: str | None,
        mv,
        vv,
        um,
        txt: str,
        ev: str,
        strict: bool,
        xo: bool,
        sids: list[str],
        *,
        kind: str = "atomic",
        rev: str = "auto_extracted",
        wr=None,
        lb=None,
    ) -> str:
        self.L.append(
            dict(
                logic_node_id=nid,
                level=4,
                logic_depth=0,
                node_kind=kind,
                linked_domain_node_id=self.link_dn,
                parent_logic_node_id=parent,
                child_logic_node_ids=[],
                sort_order=self.seq(),
                operator=op,
                condition_key=ck,
                measurement=mv,
                value=vv,
                unit=um,
                evaluation_mode=ev,
                strict_boolean_evaluation=strict,
                example_only=xo,
                original_text=txt,
                normalized_text=strip_txt(txt),
                logic_basis=lb,
                review_status=rev,
                warnings=list(wr or []),
            )

        )

        self.r.tag(self.L[-1], self.idx, self.dm, sids)

        return nid


    def C(
        self,
        nid,
        parent,
        op,
        ck,
        txt,
        ev,
        strict,
        xo,
        kids,
        sids,
        lb=None,
        display_label=None,
    ):
        row = dict(
            logic_node_id=nid,
            level=4,
            logic_depth=0,
            node_kind="composite",
            linked_domain_node_id=self.link_dn,
            parent_logic_node_id=parent,
            child_logic_node_ids=kids,
            sort_order=self.seq(),
            operator=op,
            condition_key=ck,
            measurement=None,
            value=None,
            unit=None,
            evaluation_mode=ev,
            strict_boolean_evaluation=strict,
            example_only=xo,
            original_text=txt,
            normalized_text=strip_txt(txt),
            logic_basis=lb,
            review_status="auto_extracted",
            warnings=[],
        )
        if display_label is not None:
            row["display_label"] = display_label
        self.L.append(row)
        self.r.tag(self.L[-1], self.idx, self.dm, sids)
        return nid




def wire(L: list[dict]) -> None:


    ix = {n["logic_node_id"]: n for n in L}


    for p in L:


        for c in p.get("child_logic_node_ids") or []:


            if c in ix and ix[c]["parent_logic_node_id"] is None:


                ix[c]["parent_logic_node_id"] = p["logic_node_id"]


    for r in [n for n in L if n["parent_logic_node_id"] is None]:


        dq = deque([(r["logic_node_id"], 0)])


        seen = set()


        while dq:


            nid, d = dq.popleft()


            if nid in seen:


                continue


            seen.add(nid)


            ix[nid]["logic_depth"] = d


            for c in ix[nid].get("child_logic_node_ids") or []:


                if c in ix:


                    dq.append((c, d + 1))


def emit_source_logic(ix: Idx, F: Forge, root_sid: str, root_logic_id: str, id_prefix: str) -> None:
    ctr = [0]

    def gen_id() -> str:
        ctr[0] += 1
        return f"{id_prefix}.{ctr[0]}"

    def lb_from_hint(n: dict) -> dict[str, Any] | None:
        lh = n.get("logic_hint")
        if not lh or lh.get("raw_phrase") is None:
            return None
        cf = lh.get("confidence")
        conf = cf if cf in ("high", "medium", "low") else "medium"
        return {"raw_phrase": str(lh["raw_phrase"]), "confidence": conf}

    def walk(sid: str, inherited_xo: bool, forced_id: str | None = None) -> str:
        n = ix[sid]
        ch = ix.C(sid)
        my_id = forced_id or gen_id()
        ot = n["original_text"]
        mc = F.r.mc
        if not ch:
            ck = leaf_condition_key(mc, sid)
            strict_leaf = False if inherited_xo else True
            return F.A(
                my_id,
                None,
                ck,
                "IS_TRUE",
                None,
                True,
                None,
                ot,
                "evaluate_atomic_fact",
                strict_leaf,
                inherited_xo,
                [sid],
            )
        lh = n.get("logic_hint") or {}
        op = lh.get("inferred_operator")
        ol = str(ot).lower()
        if not op:
            op = "OPTIONS" if "options include" in ol else "CHECKLIST"
        lb = lb_from_hint(n)
        composite_xo = op == "EXAMPLE_SET"
        child_xo = inherited_xo or composite_xo
        kid_ids = [walk(c["source_node_id"], child_xo, None) for c in ch]
        strict_comp = False if composite_xo else True
        return F.C(
            my_id,
            None,
            op,
            None,
            ot,
            "evaluate_children",
            strict_comp,
            composite_xo,
            kid_ids,
            [sid],
            lb=lb,
        )

    walk(root_sid, False, root_logic_id)


def cond_dict(mc: str, lg: list[dict]) -> list[dict]:
    done = set()
    ix = {n["logic_node_id"]: n for n in lg}

    def role(lid: str) -> str:
        nd = ix[lid]
        pn = nd.get("parent_logic_node_id")
        pk = ix[pn]["operator"] if pn and pn in ix else ""

        nk = nd["node_kind"]

        if nk == "composite":

            return "composite"

        if nk == "context":

            return "context"

        dom = ix[lid]["linked_domain_node_id"] or ""

        if "discharge" in dom:

            return "option" if pk == "OPTIONS" else "checklist_item"

        return "atomic_fact"


    rows = []

    for nd in sorted(lg, key=lambda x: x["logic_node_id"]):

        lid = nd["logic_node_id"]

        if lid in done:

            continue

        done.add(lid)

        kk = nd.get("condition_key")

        fallback = kk or (

            lid.replace(".", "_") + "_composite" if nd["node_kind"] == "composite" else lid.replace(".", "_")

        )


        rows.append(

            {

                "condition_key": kk or fallback,

                "condition_role": role(lid),

                "mcg_code": mc,

                "domain": "admission" if "admission" in (ix[lid]["linked_domain_node_id"] or "") else "discharge",

                "linked_domain_node_id": ix[lid]["linked_domain_node_id"],

                "linked_logic_node_id": lid,

                "node_kind": nd["node_kind"],

                "operator": nd.get("operator"),

                "measurement": nd.get("measurement"),

                "value": nd.get("value"),

                "unit": nd.get("unit"),

                "original_text": nd.get("original_text"),

                "source_ref_ids": nd.get("source_ref_ids") or [],

                "definition_scope": "guideline_local",

                "llm_extractable": True,

                "review_status": nd.get("review_status"),

            }

        )

    return rows


def render_md(dom: list[dict], lg: list[dict]) -> str:


    ix = {n["logic_node_id"]: n for n in lg}


    def wl(nid, ind=""):


        n = ix[nid]

        op = n.get("operator") or ""

        if n["node_kind"] == "context":


            pref = "[context]"

        elif n["node_kind"] == "atomic":

            pref = ("[atomic]" if op not in ("<", "<=", ">", ">=")


                    else f"[atomic {op} {n.get('measurement','')}]")

        else:

            pref = f"[{op}]"

        ln = [f"{ind}{pref} {strip_txt(str(n.get('original_text','')))[:172]}"]

        for c in n.get("child_logic_node_ids") or []:

            ln.extend(wl(c, ind + "  "))

        return ln

    mc = next(z["mcg_code"] for z in dom if z["node_type"] == "guideline")

    m = ["# " + mc + " Domain Rule Tree", "", "## Admission", ""]

    m.append("- [OR] Admission is indicated for 1 or more of the following")

    for pid in ("M083.admission.acute_ischemic_stroke_neurologic_findings", "M083.admission.acute_ischemic_stroke_clinical_need_monitoring", "M083.admission.thrombolysis_or_thrombectomy_performed_or_planned"):

        dn = next(d for d in dom if d["node_id"] == pid)

        rid = dn["logic_root_id"]

        m.append("  - Path: " + dn["original_text"])

        m.extend(["    " + x for x in wl(rid, "")])

    m += ["", "## Discharge", "", "- [CHECKLIST] Discharge planning includes"]

    pl = next(z for z in dom if z["node_type"] == "discharge_planning_section")

    m += ["  " + x for x in wl(pl["logic_root_id"], "")]

    m += ["", "- Discharge destination"]

    dst = next(z for z in dom if z["node_type"] == "discharge_destination_section")

    m += ["  " + x for x in wl(dst["logic_root_id"], "")]

    return "\n".join(m).rstrip() + "\n"


def build_m083(ix: Idx, ttl: str) -> tuple[list[dict], list[dict], Refs]:
    Forge._sort_seq = 0
    mc = "M083"
    rs = Refs(mc)
    dom: list[dict] = []
    lg: list[dict] = []

    rn = sorted(ix.C(None), key=lambda z: z["sort_order"])[0]
    bx: dict[str, Any] = {}
    rs.tag(bx, ix, None, [rn["source_node_id"]])
    dom.append(
        dict(
            node_id="MCG",
            level=0,
            node_type="guideline_library_root",
            name="MCG Guideline Library",
            mcg_code=None,
            mcg_title=None,
            domain=None,
            parent_node_id=None,
            child_node_ids=[mc],
            sort_order=1,
            description="MCG Guideline Library",
            original_text="",
            normalized_text="",
            evaluation_mode="evaluate_children",
            logic_operator=None,
            logic_basis=None,
            logic_root_id=None,
            source_node_ids=bx["source_node_ids"],
            source_ref_ids=bx["source_ref_ids"],
            review_status="auto_extracted",
            warnings=[],
        )

    )

    asn = "M083.source.admission.001"

    dn1 = dict(
        node_id=mc,
        level=1,
        node_type="guideline",
        mcg_code=mc,
        mcg_title=ttl,
        domain=None,
        parent_node_id="MCG",
        child_node_ids=[f"{mc}.admission", f"{mc}.discharge"],
        sort_order=2,
        description=ttl,
        original_text=strip_txt(ix[asn]["original_text"]),
        normalized_text=strip_txt(ix[asn]["original_text"]),
        evaluation_mode="evaluate_children",
        logic_operator=None,
        logic_basis=None,
        logic_root_id=None,
        review_status="auto_extracted",
        warnings=[],
    )
    rs.tag(dn1, ix, None, [asn])

    dn_ad = dict(
        node_id=f"{mc}.admission",
        level=2,
        node_type="guideline_domain_root",
        mcg_code=mc,
        mcg_title=ttl,
        domain="admission",
        parent_node_id=mc,
        child_node_ids=[
            f"{mc}.admission.acute_ischemic_stroke_neurologic_findings",
            f"{mc}.admission.acute_ischemic_stroke_clinical_need_monitoring",
            f"{mc}.admission.thrombolysis_or_thrombectomy_performed_or_planned",
        ],
        sort_order=3,
        description="Clinical Indications for Admission to Inpatient Care",
        original_text=strip_txt(ix[asn]["original_text"]),
        normalized_text=strip_txt(ix[asn]["original_text"]),
        evaluation_mode="evaluate_children",
        logic_operator="OR",
        logic_basis={"raw_phrase": ix[asn]["logic_hint"]["raw_phrase"], "confidence": ix[asn]["logic_hint"]["confidence"]},
        logic_root_id=None,
        review_status="auto_extracted",
        warnings=[],
    )
    rs.tag(dn_ad, ix, "admission", [asn])

    dp1 = "M083.source.discharge_planning.001"
    ds1 = "M083.source.discharge_destination.001"

    dn_dis = dict(
        node_id=f"{mc}.discharge",
        level=2,
        node_type="guideline_domain_root",
        mcg_code=mc,
        mcg_title=ttl,
        domain="discharge",
        parent_node_id=mc,
        child_node_ids=[f"{mc}.discharge.planning", f"{mc}.discharge.destination"],
        sort_order=4,
        description="Discharge",
        original_text="",
        normalized_text="",
        evaluation_mode="checklist_support",
        logic_operator="CHECKLIST",
        logic_basis={"raw_phrase": "Discharge", "confidence": "high"},
        logic_root_id=None,
        review_status="auto_extracted",
        warnings=[],
    )
    rs.tag(dn_dis, ix, "discharge", [dp1])

    lr_plan = f"logic.{mc}.discharge.planning.root"
    lr_dst = f"logic.{mc}.discharge.destination.root"

    dn_pl = dict(
        node_id=f"{mc}.discharge.planning",
        level=3,
        node_type="discharge_planning_section",
        mcg_code=mc,
        mcg_title=ttl,
        domain="discharge",
        parent_node_id=f"{mc}.discharge",
        child_node_ids=[],
        sort_order=5,
        description="Discharge planning includes",
        original_text="Discharge planning includes",
        normalized_text="Discharge planning includes",
        evaluation_mode="evaluate_rule_logic",
        logic_operator=None,
        logic_basis=None,
        logic_root_id=lr_plan,
        review_status="auto_extracted",
        warnings=[],
    )
    rs.tag(dn_pl, ix, "discharge", [dp1])

    dn_dst = dict(
        node_id=f"{mc}.discharge.destination",
        level=3,
        node_type="discharge_destination_section",
        mcg_code=mc,
        mcg_title=ttl,
        domain="discharge",
        parent_node_id=f"{mc}.discharge",
        child_node_ids=[],
        sort_order=6,
        description="Discharge Destination",
        original_text="Post-hospital levels of admission may include",
        normalized_text="Post-hospital levels of admission may include",
        evaluation_mode="evaluate_rule_logic",
        logic_operator=None,
        logic_basis=None,
        logic_root_id=lr_dst,
        review_status="auto_extracted",
        warnings=[],
    )
    rs.tag(dn_dst, ix, "discharge", [ds1])

    dom.extend([dn1, dn_ad, dn_dis, dn_pl, dn_dst])

    lr1, lr2, lr3 = f"logic.{mc}.p1.root", f"logic.{mc}.p2.root", f"logic.{mc}.p3.root"

    path_rows = [
        (
            f"{mc}.admission.acute_ischemic_stroke_neurologic_findings",
            "M083.source.admission.001.001",
            ("Acute ischemic stroke with neurologic findings that warrant inpatient care, as indicated "
             "by 1 or more of the following"),
            lr1,
        ),

        (

            f"{mc}.admission.acute_ischemic_stroke_clinical_need_monitoring",

            "M083.source.admission.001.002",

            ("Acute ischemic stroke with clinical need for inpatient care or monitoring, as indicated by "

             "1 or more of the following"),

            lr2,

        ),

        (

            f"{mc}.admission.thrombolysis_or_thrombectomy_performed_or_planned",

            "M083.source.admission.001.003",

            "Thrombolysis or thrombectomy performed or planned",

            lr3,

        ),

    ]

    for i, (nid, sid, otxt, lr) in enumerate(path_rows):

        dnp = dict(node_id=nid, level=3, node_type="admission_path",

                   mcg_code=mc, mcg_title=ttl, domain="admission",

                   parent_node_id=f"{mc}.admission",

                   child_node_ids=[],

                   sort_order=10 + i,

                   description=otxt[:96],

                   original_text=otxt,

                   normalized_text=otxt,

                   evaluation_mode="evaluate_rule_logic",

                   logic_operator=None,

                   logic_basis=None,

                   logic_root_id=lr,

                   review_status="auto_extracted",

                   warnings=[])

        rs.tag(dnp, ix, "admission", [sid])

        dom.append(dnp)

    p1_id, p2_id, p3_id = path_rows[0][0], path_rows[1][0], path_rows[2][0]

    F3 = Forge(lg, rs, ix, p3_id, "admission")

    F3.A(lr3,

         None,

         "thrombolysis_or_thrombectomy_performed_or_planned_condition_present",

         "IS_TRUE",

         None,

         True,

         None,

         ix["M083.source.admission.001.003"]["original_text"],

         "evaluate_atomic_fact",

         True,

         False,

         ["M083.source.admission.001.003"])

    F1 = Forge(lg, rs, ix, p1_id, "admission")

    srows = [

        ("nihss_score", ">", "NIHSS", 2, "score", "M083.source.admission.001.001.001"),

        ("hemorrhagic_transformation_condition_present", "IS_TRUE", None, True, None, "M083.source.admission.001.001.002"),

        ("altered_mental_status_condition_present", "IS_TRUE", None, True, None, "M083.source.admission.001.001.003"),

        ("dysphagia_evaluation_required", "IS_TRUE", None, True, None, "M083.source.admission.001.001.004"),

        ("significant_limb_weakness_condition_present", "IS_TRUE", None, True, None, "M083.source.admission.001.001.005"),

        ("aphasia_condition_present", "IS_TRUE", None, True, None, "M083.source.admission.001.001.006"),

        ("gait_impairment_condition_present", "IS_TRUE", None, True, None, "M083.source.admission.001.001.007"),

        ("brain_imaging_finding_requiring_inpatient_care_condition_present",

         "IS_TRUE", None, True, None,

         "M083.source.admission.001.001.008"),

        ("neurologic_worsening_monitoring_required",

         "IS_TRUE", None, True, None,

         "M083.source.admission.001.001.009"),

        ("clinical_stability_unclear_condition_present",

         "IS_TRUE", None, True, None,

         "M083.source.admission.001.001.010"),

    ]

    or_c = []

    for k, (ck, oo, mv, vv, um, sid) in enumerate(srows):

        or_c.append(

            F1.A(f"log.{mc}.ad1.{k}", None, ck, oo, mv, vv, um, ix[sid]["original_text"],

                 "evaluate_atomic_fact", True, False, [sid]))

    lh_1 = ix["M083.source.admission.001.001"]

    or_n = F1.C(f"log.{mc}.ad1.or",

                None,

                "OR",

                None,

                lh_1["original_text"],

                "evaluate_children",

                True,

                False,

                or_c,

                [lh_1["source_node_id"]],

                lb={"raw_phrase": lh_1["logic_hint"]["raw_phrase"],

                    "confidence": lh_1["logic_hint"]["confidence"]})

    ctx1 = F1.A(f"log.{mc}.ad1.cx",

                None,

                "acute_ischemic_stroke_condition_present",

                "IS_TRUE",

                None,

                True,

                None,

                "Acute ischemic stroke",

                "evaluate_extracted_fact",

                True,

                False,

                [lh_1["source_node_id"]],

                kind="context")

    p1_root_txt = path_rows[0][2]
    F1.C(
        lr1,
        None,
        "AND",
        None,
        p1_root_txt,
        "evaluate_children",
        True,
        False,
        [ctx1, or_n],
        [lh_1["source_node_id"]],
        lb={"raw_phrase": "with neurologic inpatient criteria", "confidence": "high"},
        display_label="Stroke neurologic inpatient bundle",
    )

    F2 = Forge(lg, rs, ix, p2_id, "admission")

    lh_2 = ix["M083.source.admission.001.002"]

    s_htn = "M083.source.admission.001.002.006"

    ad2_simple = [
        "M083.source.admission.001.002.001",
        "M083.source.admission.001.002.002",
        "M083.source.admission.001.002.003",
        "M083.source.admission.001.002.004",
        "M083.source.admission.001.002.005",
        "M083.source.admission.001.002.007",
        "M083.source.admission.001.002.008",
    ]

    ad2_condition_key_by_sid = {
        "M083.source.admission.001.002.001": "hemodynamic_instability_condition_present",
        "M083.source.admission.001.002.002": "cardiac_arrhythmia_of_immediate_concern_condition_present",
        "M083.source.admission.001.002.003": "clinically_significant_cardiac_or_vascular_disorder_condition_present",
        "M083.source.admission.001.002.004": "cerebral_venous_thrombosis_condition_present",
        "M083.source.admission.001.002.005": "respiratory_abnormality_condition_present",
        "M083.source.admission.001.002.007": "prolonged_cardiac_telemetry_monitoring_required",
        "M083.source.admission.001.002.008": "suspected_vasculitis_condition_present",
    }

    grp: list[str] = []

    for j, sid in enumerate(ad2_simple):

        grp.append(
            F2.A(
                f"log.{mc}.ad2.{j}",
                None,
                ad2_condition_key_by_sid[sid],
                "IS_TRUE",
                None,
                True,
                None,
                ix[sid]["original_text"],
                "evaluate_atomic_fact",
                True,
                False,
                [sid],
            )
        )

    sbp = F2.A(
        f"log.{mc}.ad2.htn.sbp",
        None,
        "systolic_blood_pressure_mmhg",
        ">",
        "SBP",
        180,
        "mmHg",
        "Systolic blood pressure greater than 180 mm Hg",
        "evaluate_atomic_fact",
        True,
        False,
        [s_htn],
    )

    dbp = F2.A(
        f"log.{mc}.ad2.htn.dbp",
        None,
        "diastolic_blood_pressure_mmhg",
        ">",
        "DBP",
        120,
        "mmHg",
        "Diastolic blood pressure greater than 120 mm Hg",
        "evaluate_atomic_fact",
        True,
        False,
        [s_htn],
    )

    ped = F2.A(
        f"log.{mc}.ad2.htn.ped",
        None,
        "pediatric_severe_hypertension_threshold_condition_present",
        "IS_TRUE",
        None,
        True,
        None,
        ix[s_htn]["original_text"],
        "evaluate_atomic_fact",
        True,
        False,
        [s_htn],
        rev="needs_review",
        wr=["Apply age-sex-height 95th percentile + 30 mm Hg where pediatric criteria apply."],
    )

    htn_or = F2.C(
        f"log.{mc}.ad2.htn.or",
        None,
        "OR",
        None,
        ix[s_htn]["original_text"],
        "evaluate_children",
        True,
        False,
        [sbp, dbp, ped],
        [s_htn],
    )

    grp.append(htn_or)

    or2 = F2.C(
        f"log.{mc}.ad2.or",
        None,
        "OR",
        None,
        lh_2["original_text"],
        "evaluate_children",
        True,
        False,
        grp,
        [lh_2["source_node_id"]],
        lb={"raw_phrase": lh_2["logic_hint"]["raw_phrase"], "confidence": lh_2["logic_hint"]["confidence"]},
    )

    ctx2 = F2.A(
        f"log.{mc}.ad2.cx",
        None,
        "acute_ischemic_stroke_condition_present",
        "IS_TRUE",
        None,
        True,
        None,
        "Acute ischemic stroke",
        "evaluate_extracted_fact",
        True,
        False,
        [lh_2["source_node_id"]],
        kind="context",
    )

    p2_root_txt = path_rows[1][2]
    F2.C(
        lr2,
        None,
        "AND",
        None,
        p2_root_txt,
        "evaluate_children",
        True,
        False,
        [ctx2, or2],
        [lh_2["source_node_id"]],
        lb={"raw_phrase": "with clinical inpatient or monitoring criteria", "confidence": "high"},
        display_label="Stroke clinical monitoring inpatient bundle",
    )

    emit_source_logic(ix, Forge(lg, rs, ix, f"{mc}.discharge.planning", "discharge"), dp1, lr_plan, f"log.{mc}.dplan")

    emit_source_logic(ix, Forge(lg, rs, ix, f"{mc}.discharge.destination", "discharge"), ds1, lr_dst, f"log.{mc}.ddst")

    wire(lg)

    return dom, lg, rs


KNOWN_SYNTHETIC_ORIGINAL_LABELS = frozenset(
    {
        "Stroke neurologic inpatient bundle",
        "Stroke clinical monitoring inpatient bundle",
    }
)


def collect_audit_logic(lg: list[dict]) -> dict[str, list[str]]:
    bad_condition_keys: list[str] = []
    example_strict: list[str] = []
    synthetic_ot: list[str] = []
    seen_bad: set[str] = set()

    for n in lg:
        lid = n["logic_node_id"]
        ck = n.get("condition_key")
        if isinstance(ck, str):
            bad_reason = None
            if ck.startswith("src_"):
                bad_reason = "src_prefix"
            elif "source_admission" in ck or "source_discharge" in ck or "source_" in ck:
                bad_reason = "source_fragment"
            if bad_reason:
                entry = f"{lid}: {ck}"
                if entry not in seen_bad:
                    seen_bad.add(entry)
                    bad_condition_keys.append(entry)
        ot = (n.get("original_text") or "").strip()
        if ot in KNOWN_SYNTHETIC_ORIGINAL_LABELS:
            synthetic_ot.append(lid)
        op = n.get("operator")
        xo = n.get("example_only")
        strict = n.get("strict_boolean_evaluation")
        if (op == "EXAMPLE_SET" or xo is True) and strict is not False:
            example_strict.append(lid)

    bad_condition_keys.sort()
    example_strict.sort()
    synthetic_ot.sort()
    return {
        "bad_condition_keys": bad_condition_keys,
        "example_nodes_with_strict_boolean_true": example_strict,
        "synthetic_original_text_nodes": synthetic_ot,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Step 2B: build domain rule tree from validated source-tree JSON.")

    ap.add_argument(
        "--input",
        "--source-tree",
        dest="input",
        required=True,
        help="Validated mcg_source_tree.v1 JSON path",
    )

    ap.add_argument("--mcg-code", default=None, help="Optional; must match source document if set")

    ap.add_argument("--title", default=None, help="Optional override for mcg_title in outputs")

    ap.add_argument("--out-dir", required=True, help="Output directory for domain rule tree artifacts")

    args = ap.parse_args()

    in_path = Pth(args.input)

    doc = json.loads(in_path.read_text(encoding="utf-8"))

    if doc.get("schema_version") != SCHEMA_SOURCE_TREE:

        raise SystemExit(f"unsupported source schema: {doc.get('schema_version')}")

    sd = doc["source_document"]

    mcg = sd["mcg_code"]

    ttl = str(sd.get("mcg_title") or sd.get("title") or sd.get("product_name") or "MCG")

    if args.mcg_code is not None and args.mcg_code != mcg:
        raise SystemExit(f"--mcg-code {args.mcg_code} does not match source document {mcg}")

    if args.title is not None:
        ttl = args.title

    ix = Idx(doc["source_nodes"])

    if mcg != "M083":

        raise SystemExit(f"unsupported mcg_code {mcg}")
    dom, lg, rs = build_m083(ix, ttl)
    out = Pth(args.out_dir)

    out.mkdir(parents=True, exist_ok=True)

    jpath = out / f"{mcg}.domain-rule-tree.v1.json"

    refs_sorted = sorted(rs.R.values(), key=lambda r: str(r["source_ref_id"]))

    cond = cond_dict(mcg, lg)

    logic_audit = collect_audit_logic(lg)

    audit = dict(
        mcg_code=mcg,
        mc_title=ttl,
        source_tree=str(in_path),
        domain_node_count=len(dom),
        logic_node_count=len(lg),
        source_ref_count=len(rs.R),
        condition_dictionary_count=len(cond),
        source_tree_audit=doc.get("audit"),
        logic=logic_audit,
    )
    payload = dict(
        schema_version=SCHEMA_DOMAIN_RULE_TREE,
        source_tree_schema_version=SCHEMA_SOURCE_TREE,
        mcg_code=mcg,
        mcg_title=ttl,
        source_tree_path=str(in_path),
        source_document=dict(mcg_code=mcg, mcg_title=ttl),
        domain_roots={"admission": f"{mcg}.admission", "discharge": f"{mcg}.discharge"},
        domain_nodes=dom,
        logic_nodes=lg,
        source_refs=refs_sorted,
        condition_dictionary=cond,
        audit=audit,
    )

    jpath.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    (out / f"{mcg}.domain-nodes.jsonl").write_text(

        "".join(json.dumps(z, ensure_ascii=False) + "\n" for z in dom),

        encoding="utf-8",

    )

    (out / f"{mcg}.logic-nodes.jsonl").write_text(

        "".join(json.dumps(z, ensure_ascii=False) + "\n" for z in lg),

        encoding="utf-8",

    )

    (out / f"{mcg}.source-refs.jsonl").write_text(

        "".join(json.dumps(z, ensure_ascii=False) + "\n" for z in refs_sorted),

        encoding="utf-8",

    )

    (out / f"{mcg}.condition-dictionary.jsonl").write_text(

        "".join(json.dumps(z, ensure_ascii=False) + "\n" for z in cond),

        encoding="utf-8",

    )

    (out / f"{mcg}.domain-rule-tree.audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    (out / f"{mcg}.domain-rule-tree.roundtrip.md").write_text(render_md(dom, lg), encoding="utf-8")

    print("Wrote", jpath)


if __name__ == "__main__":
    main()

