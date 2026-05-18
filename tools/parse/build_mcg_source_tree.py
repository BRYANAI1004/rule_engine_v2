#!/usr/bin/env python3
"""Deterministic MCG expanded-HTML -> source tree JSON (mcg_source_tree.v1)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

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

UI_SKIP_EXACT = frozenset(
    {
        "[ Expand All / Collapse All ]",
        "Expand All / Collapse All",
    }
)

RESIDUAL_EXPAND_MARKERS = (
    "Expand Acute",
    "Expand Patient",
    "Expand Medication",
    "[Expand All",
    "Collapse All",
    "Show All",
)

ADMISSION_EXPECTED_TEXTS = {
    "admission_root": "Admission is indicated for 1 or more of the following",
    "stroke_neuro_following": "neurologic findings that warrant inpatient care",
    "nihss": "NIHSS) score greater than 2",
    "hemorrhagic_transformation": "Evidence of hemorrhagic transformation",
    "altered_ms": "Altered mental status",
    "stroke_monitoring_following": "clinical need for inpatient care or monitoring",
    "hemodynamic": "Hemodynamic instability",
    "thrombolysis": "Thrombolysis or thrombectomy performed or planned",
}

DISCHARGE_EXPECTED_TEXTS = {
    "planning_root": "Discharge planning includes",
    "assessment": "Assessment of needs and planning for care",
    "early_destination": "Early identification of anticipated discharge destination",
    "patient_safe": "Patient safe to go home",
    "med_rec": "Medication reconciliation completion includes",
    "post_hospital": "Post-hospital levels of admission may include",
    "home_hc": "Home healthcare",
}

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_ws(text: str) -> str:
    return " ".join(text.split()).strip()


def _extract_collapse_map(raw_html: str) -> dict[str, Any]:
    m = re.search(
        r"var\s+collapseExpandMap\s*=\s*\{([^}]*)\}\s*;",
        raw_html[:500000],
        re.DOTALL,
    )
    raw_block = m.group(0) if m else ""
    parsed: dict[str, list[str]] = {}
    if m:
        inner = m.group(1)
        for pair in re.finditer(r"([A-Z0-9]+)\s*:\s*\[([^\]]*)\]", inner):
            key = pair.group(1)
            vals_raw = pair.group(2)
            parts = [p.strip().strip('"').strip("'") for p in vals_raw.split(",")]
            parts = [p for p in parts if p]
            parsed[key] = parts
    return {"raw": raw_block.strip(), "parsed": parsed}


def _edition_from_soup(soup: BeautifulSoup) -> str | None:
    for font in soup.find_all("font", class_=re.compile(r"logo_ed", re.I)):
        t = _normalize_ws(font.get_text())
        if t:
            return t
    return None


def _heading_anchor(h: Tag) -> str | None:
    a = h.find("a", attrs={"name": True})
    if a and a.get("name"):
        return str(a.get("name"))
    prev = h.find_previous_sibling()
    if prev and getattr(prev, "name", None) == "a" and prev.get("name"):
        return str(prev.get("name"))
    return None


def _domain_for_section(section_key: str, title: str, data_role: str | None) -> str:
    t = title
    dr = data_role or ""
    if section_key in {"admission"} or dr == "Clinical Indications for Admission to Inpatient Care":
        return "admission"
    if section_key in {"alternatives"} or "Alternatives to Admission" in t:
        return "alternatives"
    if section_key == "optimal_recovery_course" or "Optimal Recovery Course" in t:
        return "optimal_recovery_course"
    if section_key == "extended_stay":
        return "extended_stay"
    if section_key in {"discharge", "discharge_planning", "discharge_destination"}:
        return "discharge"
    if section_key == "references" or t == "References":
        return "references"
    if section_key == "footnotes" or t == "Footnotes":
        return "footnotes"
    if section_key == "codes" or t == "Codes":
        return "codes"
    if section_key.startswith("evidence_") or section_key == "evidence_summary" or t == "Evidence Summary":
        return "evidence_summary"
    if section_key == "related_cms":
        return "evidence_summary"
    if section_key == "care_planning":
        return "care_planning"
    if section_key == "hospitalization":
        return "hospitalization"
    if section_key == "supplemental_medicare":
        return "supplemental_medicare"
    return section_key


def _classify_section_heading(h: Tag) -> tuple[str, str, str | None] | None:
    """Return (section_key, title, data_role) or None if not a tracked section."""
    title = _normalize_ws(h.get_text())
    data_role = h.get("data-role")
    if isinstance(data_role, list):
        data_role = data_role[0] if data_role else None
    anchor = (_heading_anchor(h) or "").lower()

    if title == "Care Planning - Inpatient Admission and Alternatives":
        return "care_planning", title, data_role
    if data_role == "Clinical Indications for Admission to Inpatient Care":
        return "admission", title, data_role
    if data_role == "Supplemental Medicare Criteria":
        return "supplemental_medicare", title, data_role
    if data_role == "Alternatives to Admission":
        return "alternatives", title, data_role
    if data_role == "Optimal Recovery Course":
        return "optimal_recovery_course", title, data_role
    if data_role == "Extended Stay":
        return "extended_stay", title, data_role
    if data_role == "DischargePlanning":
        return "discharge_planning", title, data_role
    if data_role == "Discharge Destination":
        return "discharge_destination", title, data_role
    if title == "Hospitalization":
        if h.name == "h3" or "annotation" in anchor:
            return "evidence_hospitalization_ann", title, data_role
        if h.name == "h2":
            return "hospitalization", title, data_role
        return "evidence_hospitalization_ann", title, data_role
    if title == "Discharge" and h.name == "h2":
        return "discharge", title, data_role
    if title == "Evidence Summary" and h.name == "h2":
        return "evidence_summary", title, data_role
    if title == "Criteria" and h.name == "h3":
        return "evidence_criteria", title, data_role
    if title == "Alternatives" and h.name == "h3" and "alternativesannotation" in anchor:
        return "evidence_alternatives_ann", title, data_role
    if title == "Length of Stay" and h.name == "h3":
        return "evidence_length_of_stay", title, data_role
    if title == "Rationale" and h.name == "h3":
        return "evidence_rationale", title, data_role
    if title == "Related CMS Coverage Guidance":
        return "related_cms", title, data_role
    if title == "References" and h.name == "h2":
        return "references", title, data_role
    if title == "Footnotes" and h.name == "h2":
        return "footnotes", title, data_role
    if title == "Codes" and h.name == "h2":
        return "codes", title, data_role
    return None


def _build_sections(
    soup: BeautifulSoup,
    mcg_code: str,
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    sort_order = 0

    for h in soup.find_all(["h2", "h3"]):
        hit = _classify_section_heading(h)
        if not hit:
            continue
        section_key, title, data_role = hit
        sort_order += 1
        anchor = _heading_anchor(h)
        domain = _domain_for_section(section_key, title, data_role)
        payload = {
            "section_id": f"{mcg_code}.section.{section_key}",
            "mcg_code": mcg_code,
            "section_key": section_key,
            "domain": domain,
            "title": title,
            "html_anchor": anchor,
            "html_tag": h.name,
            "logictype": h.get("logictype"),
            "data_role": data_role,
            "sort_order": sort_order,
            "text_hash": _sha256_text(f"{section_key}|{anchor}|{sort_order}|{title}"),
        }
        sections.append(payload)
    return sections


def _find_expand_group_before(start: Tag | None) -> str | None:
    if not start:
        return None
    sib = start.previous_sibling
    steps = 0
    while sib is not None and steps < 30:
        steps += 1
        if isinstance(sib, NavigableString):
            sib = sib.previous_sibling
            continue
        if isinstance(sib, Tag):
            html = str(sib)
            m = re.search(r"expandall\('([^']+)'\)", html, re.I)
            if m:
                return m.group(1)
        sib = sib.previous_sibling
    return None


def _collect_footnote_refs(el: Tag) -> list[str]:
    keys: list[str] = []
    for a in el.find_all("a", onclick=True):
        oc = a.get("onclick", "")
        m = re.search(r"popup_footnote\([^,]*,\s*'([^']*)'\)", str(oc), re.I)
        if m:
            keys.append(m.group(1))
    # also <sup>[<a>A</a>]</sup>
    for a in el.find_all("a", string=re.compile(r"^[A-Z]$")):
        t = a.get_text(strip=True)
        if len(t) == 1 and t.isupper():
            keys.append(t)
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _collect_reference_refs(el: Tag) -> list[str]:
    nums: list[str] = []
    for a in el.find_all("a", onclick=True):
        oc = str(a.get("onclick", ""))
        m = re.search(r"popup_citation\([^,]*,\s*'([^']*)'\s*\)", oc)
        if m:
            nums.append(m.group(1))
    seen: set[str] = set()
    out: list[str] = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _clone_for_text(li: Tag) -> Tag:
    c = BeautifulSoup(str(li), "lxml")
    root = c.find("li")
    assert root is not None
    return root


def _strip_noise_from_tree(el: Tag) -> None:
    for img in el.find_all("img"):
        img.decompose()
    for a in el.find_all("a", title=True):
        title = str(a.get("title", ""))
        if "annotation" in title.lower() or "Supporting evidence" in title:
            a.decompose()


def _strip_collapsed_expand_divs(root: Tag) -> None:
    """Prefer expanded (e-*) divs: remove paired collapsed (c-*) divs and collapse controls."""
    for div in list(root.find_all("div", id=True)):
        did = str(div.get("id") or "")
        if did.startswith("c-"):
            div.decompose()
    for a in root.find_all("a", onclick=re.compile(r"\bcollapse\s*\(", re.I)):
        a.decompose()
    for a in root.find_all("a", onclick=re.compile(r"\bexpand\s*\(", re.I)):
        a.decompose()


def _li_visible_text(li: Tag) -> str:
    c = _clone_for_text(li)
    for ul in c.find_all("ul"):
        ul.decompose()
    _strip_collapsed_expand_divs(c)
    _strip_noise_from_tree(c)
    for a in c.find_all("a", onclick=re.compile(r"popup_annotation", re.I)):
        a.decompose()
    text = c.get_text(separator=" ", strip=True)
    for sub in UI_NOISE_SUBSTRINGS:
        text = text.replace(sub, "")
    text = _normalize_ws(text)
    # trim trailing citation parens clutter — keep readable single line
    return text


def _infer_node_type(text: str, depth: int, has_child_ul: bool) -> str:
    tl = text.lower()
    if tl.startswith("note:"):
        return "note"
    if "may include" in tl:
        return "option"
    if ("includes" in tl and tl.endswith(":")) or "includes:" in tl:
        if "may include" not in tl:
            return "checklist_item"
    if depth == 1 and has_child_ul:
        return "criterion_root"
    return "criterion_item"


def _logic_hint(text: str) -> dict[str, Any] | None:
    tl = text.lower()
    hints: list[tuple[str, str, str]] = []

    def add(phrase: str, op: str, conf: str = "high", kind: str = ""):
        hints.append((phrase, op, conf, kind))

    if re.search(r"\b1 or more of the following\b", tl) or re.search(
        r"\bone or more of the following\b", tl
    ):
        add("1 or more of the following", "OR")
    if re.search(r"\ball of the following\b", tl):
        add("all of the following", "AND")
    if "examples include" in tl or re.search(r"\beg,\b", tl) or " e.g." in tl:
        add("examples include or eg", "EXAMPLE_SET", "medium", "example_only")
    if "may include" in tl:
        add("may include", "OPTIONS", "high")
    if re.search(r"\bincludes\b", tl):
        add("includes", "CHECKLIST", "medium")
    if "as indicated by" in tl:
        add("as indicated by", "criteria_intro", "medium")

    if not hints:
        return None
    raw_phrase = hints[0][0]
    op = hints[0][1]
    kind = hints[0][3] if len(hints[0]) > 3 else ""
    out: dict[str, Any] = {
        "raw_phrase": raw_phrase,
        "inferred_operator": op,
        "confidence": "high" if op in {"OR", "AND"} else "medium",
    }
    if kind:
        out["hint_kind"] = kind
    return out


def _html_path(tag: Tag) -> str:
    parts: list[str] = []
    cur: Any = tag
    while cur and isinstance(cur, Tag) and cur.name and cur.name != "[document]":
        parent = cur.parent
        if not isinstance(parent, Tag):
            parts.append(cur.name)
            break
        idx = 1 + sum(1 for prev in cur.previous_siblings if getattr(prev, "name", None) == cur.name)
        parts.append(f"{cur.name}[{idx}]")
        cur = parent
    return "/".join(reversed(parts))


def _first_anchor_in(li: Tag) -> str | None:
    a = li.find("a", attrs={"name": True})
    if a and a.get("name"):
        return str(a.get("name"))
    return None


def _map_collapse_ids(
    expand_group: str | None,
    cmap: dict[str, list[str]],
) -> tuple[str | None, str | None, str | None]:
    if not expand_group or expand_group not in cmap:
        return None, None, None
    pair = cmap.get(expand_group) or []
    # map: expanded first, collapsed second when two elements
    exp_id = pair[0] if len(pair) > 0 else None
    col_id = pair[1] if len(pair) > 1 else None
    if exp_id == "":
        exp_id = None
    if col_id == "":
        col_id = None
    return expand_group, exp_id, col_id


class _PathIds:
    def __init__(self, mcg_code: str, section_key: str):
        self.mcg_code = mcg_code
        self.section_key = section_key
        self.path: list[int] = []

    def bump(self, depth: int) -> tuple[str, str | None]:
        while len(self.path) < depth:
            self.path.append(0)
        self.path = self.path[:depth]
        self.path[depth - 1] += 1
        parts = ".".join(f"{p:03d}" for p in self.path)
        sid = f"{self.mcg_code}.source.{self.section_key}.{parts}"
        if len(self.path) <= 1:
            return sid, None
        parent_parts = ".".join(f"{p:03d}" for p in self.path[:-1])
        pid = f"{self.mcg_code}.source.{self.section_key}.{parent_parts}"
        return sid, pid


def _uls_immediate_under_li(container_li: Tag) -> list[Tag]:
    """UL descendants whose first LI ancestor is container_li (not a nested list item)."""
    out: list[Tag] = []
    for cand in container_li.find_all("ul"):
        cur = cand.parent
        first_li = None
        while cur is not None:
            if getattr(cur, "name", None) == "li":
                first_li = cur
                break
            cur = cur.parent
        if first_li is container_li:
            out.append(cand)
    return out


def _parse_ul_into_nodes(
    ul: Tag,
    section: dict[str, Any],
    mcg_code: str,
    cmap: dict[str, list[str]],
    expand_group: str | None,
    nodes: list[dict[str, Any]],
    path_ids: _PathIds,
    sort_order_global: list[int],
    depth_base: int = 0,
) -> None:
    for li in ul.find_all("li", recursive=False):
        depth = depth_base + 1
        sid, pid = path_ids.bump(depth)
        sort_order_global[0] += 1
        child_uls = _uls_immediate_under_li(li)
        has_child_ul = bool(child_uls)
        otext = _li_visible_text(li)
        if not otext or otext in UI_SKIP_EXACT:
            continue
        if _is_pure_noise_li(otext):
            continue
        nt = _infer_node_type(otext, depth, has_child_ul)
        fn = _collect_footnote_refs(li)
        rn = _collect_reference_refs(li)
        ah = _first_anchor_in(li)
        cg, eid, cid = _map_collapse_ids(expand_group, cmap)
        node: dict[str, Any] = {
            "source_node_id": sid,
            "mcg_code": mcg_code,
            "section_id": section["section_id"],
            "domain": section["domain"],
            "parent_source_node_id": pid,
            "source_depth": depth,
            "sort_order": sort_order_global[0],
            "node_type": nt,
            "original_text": otext,
            "normalized_text": _normalize_ws(otext),
            "html_tag": "li",
            "html_anchor": ah,
            "html_path": _html_path(li),
            "collapse_id": cg,
            "expanded_div_id": eid,
            "collapsed_div_id": cid,
            "expand_group_parent": expand_group,
            "expanded": True,
            "logic_hint": _logic_hint(otext),
            "footnote_refs": fn,
            "reference_refs": rn,
            "text_hash": _sha256_text(otext),
            "warnings": [],
        }
        if not otext.strip():
            node["warnings"].append("empty_original_text")
        nodes.append(node)
        for cu in child_uls:
            _parse_ul_into_nodes(
                cu,
                section,
                mcg_code,
                cmap,
                expand_group,
                nodes,
                path_ids,
                sort_order_global,
                depth_base=depth,
            )


def _is_pure_noise_li(text: str) -> bool:
    t = _normalize_ws(text)
    if not t:
        return True
    for s in UI_NOISE_SUBSTRINGS:
        if t == s or t.startswith(s):
            return True
    return False


def _append_paragraph_node(
    cur: Tag,
    section: dict[str, Any],
    mcg_code: str,
    nodes: list[dict[str, Any]],
    path_ids: _PathIds,
    sort_order_global: list[int],
) -> None:
    text = _normalize_ws(cur.get_text(separator=" ", strip=True))
    if not text:
        return
    sid, pid = path_ids.bump(1)
    sort_order_global[0] += 1
    a = cur.find("a", attrs={"name": True})
    ah = str(a.get("name")) if a and a.get("name") else None
    nodes.append(
        {
            "source_node_id": sid,
            "mcg_code": mcg_code,
            "section_id": section["section_id"],
            "domain": section["domain"],
            "parent_source_node_id": pid,
            "source_depth": 1,
            "sort_order": sort_order_global[0],
            "node_type": "source_only",
            "original_text": text,
            "normalized_text": text,
            "html_tag": "p",
            "html_anchor": ah,
            "html_path": _html_path(cur),
            "collapse_id": None,
            "expanded_div_id": None,
            "collapsed_div_id": None,
            "expand_group_parent": None,
            "expanded": True,
            "logic_hint": _logic_hint(text),
            "footnote_refs": _collect_footnote_refs(cur),
            "reference_refs": _collect_reference_refs(cur),
            "text_hash": _sha256_text(text),
            "warnings": [],
        }
    )


def _prose_section(sec: dict[str, Any]) -> bool:
    sk = sec["section_key"]
    return sk.startswith("evidence_") or sk in {"related_cms", "evidence_summary"}


def _build_source_nodes(
    soup: BeautifulSoup,
    sections: list[dict[str, Any]],
    mcg_code: str,
    cmap: dict[str, list[str]],
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    heads = _ordered_section_heads(soup, sections)
    skip_parse_keys = {"references", "footnotes", "codes"}
    for i, (h, sec) in enumerate(heads):
        if sec["section_key"] in skip_parse_keys:
            continue
        stop = heads[i + 1][0] if i + 1 < len(heads) else None
        cur: Any = h.next_sibling
        path_ids = _PathIds(mcg_code, sec["section_key"])
        sort_g = [0]
        while cur is not None and cur != stop:
            if isinstance(cur, Tag):
                if cur.name == "table" and cur.get("id") == "orc":
                    cur = cur.next_sibling
                    continue
                if cur.name == "ul":
                    eg = _find_expand_group_before(cur)
                    _parse_ul_into_nodes(
                        cur, sec, mcg_code, cmap, eg, nodes, path_ids, sort_g, depth_base=0
                    )
                elif cur.name == "p" and _prose_section(sec):
                    c2 = cur
                    while c2 is not None and c2 != stop:
                        if isinstance(c2, Tag) and c2.name == "p":
                            _append_paragraph_node(c2, sec, mcg_code, nodes, path_ids, sort_g)
                        c2 = c2.next_sibling
                    cur = c2
                    continue
            cur = cur.next_sibling
    return nodes


def _ordered_section_heads(
    soup: BeautifulSoup, sections: list[dict[str, Any]]
) -> list[tuple[Tag, dict[str, Any]]]:
    out: list[tuple[Tag, dict[str, Any]]] = []
    for h in soup.find_all(["h2", "h3"]):
        cl = _classify_section_heading(h)
        if not cl:
            continue
        sk, _title, _dr = cl
        anchor = _heading_anchor(h)
        sec = next(
            (s for s in sections if s["section_key"] == sk and s.get("html_anchor") == anchor),
            None,
        )
        if not sec:
            sec = next((s for s in sections if s["section_key"] == sk), None)
        if sec:
            out.append((h, sec))
    return out


def _parse_orc_table(
    soup: BeautifulSoup,
    mcg_code: str,
    section: dict[str, Any],
) -> dict[str, Any] | None:
    tbl = soup.find("table", id="orc")
    if not tbl:
        return None
    headers: list[str] = []
    hr = tbl.find("tr")
    if not hr:
        return None
    for th in hr.find_all(["th", "td"]):
        headers.append(_normalize_ws(th.get_text()))
    rows_out: list[dict[str, Any]] = []
    for ri, tr in enumerate(tbl.find_all("tr")[1:], start=1):
        cells = tr.find_all(["td", "th"])
        row_cells: dict[str, Any] = {}
        for hi, hname in enumerate(headers):
            if hi >= len(cells):
                continue
            raw = cells[hi]
            pieces = [_normalize_ws(x) for x in raw.get_text("\n", strip=True).split("\n")]
            pieces = [p for p in pieces if p]
            if len(pieces) <= 1:
                row_cells[hname] = pieces[0] if pieces else ""
            else:
                row_cells[hname] = pieces
        rows_out.append({"row_index": ri, "cells": row_cells})
    rec = {
        "table_id": f"{mcg_code}.table.orc.001",
        "mcg_code": mcg_code,
        "section_id": section["section_id"],
        "domain": section["domain"],
        "title": "Optimal Recovery Course",
        "columns": headers,
        "rows": rows_out,
        "source_ref_ids": [],
        "text_hash": _sha256_text(json.dumps(rows_out, sort_keys=True, ensure_ascii=False)),
    }
    return rec


def _extract_references(soup: BeautifulSoup, mcg_code: str, section: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    h2 = None
    for cand in soup.find_all("h2"):
        if _normalize_ws(cand.get_text()) == "References":
            h2 = cand
            break
    if not h2:
        return refs
    cur = h2.next_sibling
    while cur is not None:
        if isinstance(cur, Tag) and cur.name in {"h2", "h3"}:
            break
        if isinstance(cur, Tag) and cur.name == "ol":
            for i, li in enumerate(cur.find_all("li", recursive=False), start=1):
                c = BeautifulSoup(str(li), "lxml")
                root = c.find("li")
                assert root is not None
                for a in root.find_all("a", string=re.compile(r"View abstract", re.I)):
                    a.decompose()
                for a in root.find_all("a", title=re.compile(r"abstract", re.I)):
                    a.decompose()
                text = _normalize_ws(root.get_text(separator=" ", strip=True))
                doi_m = re.search(r"DOI:\s*(\S+)", text, re.I)
                doi = doi_m.group(1).rstrip(".") if doi_m else None
                ctx: list[str] = []
                cm = re.search(r"\[\s*Context Link\s*([^\]]+)\]", text, re.I)
                if cm:
                    ctx = [x.strip() for x in cm.group(1).split(",") if x.strip()]
                refs.append(
                    {
                        "reference_id": f"{mcg_code}.reference.{i:03d}",
                        "mcg_code": mcg_code,
                        "reference_number": str(i),
                        "text": text,
                        "doi": doi,
                        "context_links": ctx,
                        "text_hash": _sha256_text(text),
                    }
                )
            break
        cur = cur.next_sibling
    if refs and section:
        for r in refs:
            r["section_id"] = section["section_id"]
    return refs


def _extract_footnotes(soup: BeautifulSoup, mcg_code: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    h2 = None
    for cand in soup.find_all("h2"):
        if _normalize_ws(cand.get_text()) == "Footnotes":
            h2 = cand
            break
    if not h2:
        return out
    cur = h2.next_sibling
    keys_seen: set[str] = set()
    while cur is not None:
        if isinstance(cur, Tag) and cur.name == "h2":
            break
        if isinstance(cur, Tag) and cur.name == "p" and "MsoFootnoteText" in " ".join(cur.get("class", [])):
            text = _normalize_ws(cur.get_text(separator=" ", strip=True))
            mk = re.match(r"^\[\s*([A-Z0-9]+)\s*\]\s*(.*)$", text, re.DOTALL)
            key = mk.group(1) if mk else ""
            body = mk.group(2) if mk else text
            if key and key not in keys_seen:
                keys_seen.add(key)
                ref_nums = re.findall(r"\((\d+)\)", body)
                out.append(
                    {
                        "footnote_id": f"{mcg_code}.footnote.{key}",
                        "mcg_code": mcg_code,
                        "footnote_key": key,
                        "text": _normalize_ws(body),
                        "reference_refs": ref_nums,
                        "text_hash": _sha256_text(body),
                    }
                )
        cur = cur.next_sibling
    return out


def _extract_codes_from_meta(meta: dict[str, str], mcg_code: str) -> list[dict[str, Any]]:
    raw = meta.get("ICD-10 Diagnosis", "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    # also include root I63 from user example
    content = ",".join(parts)
    return [
        {
            "code_group_id": f"{mcg_code}.codes.icd10_diagnosis",
            "mcg_code": mcg_code,
            "code_system": "ICD-10 Diagnosis",
            "codes": parts,
            "descriptions": [],
            "text_hash": _sha256_text(content),
        }
    ]


def _detect_ui_noise(nodes: list[dict[str, Any]]) -> list[str]:
    hits: list[str] = []
    for n in nodes:
        t = n.get("original_text", "")
        for s in UI_NOISE_SUBSTRINGS:
            if s in t:
                hits.append(f"{n.get('source_node_id')}: {s}")
    return hits


def _residual_expand(nodes: list[dict[str, Any]], md_text: str) -> list[str]:
    hits: list[str] = []
    for n in nodes:
        t = n.get("original_text", "")
        for m in RESIDUAL_EXPAND_MARKERS:
            if m in t:
                hits.append(f"node:{n.get('source_node_id')}:{m}")
    for m in RESIDUAL_EXPAND_MARKERS:
        if m in md_text:
            hits.append(f"md:{m}")
    return hits


def _run_audit(
    mcg_code: str,
    sections: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    footnotes: list[dict[str, Any]],
    references: list[dict[str, Any]],
    codes: list[dict[str, Any]],
    md_text: str,
) -> dict[str, Any]:
    by_dom: dict[str, int] = {}
    by_sec: dict[str, int] = {}
    for n in nodes:
        d = n.get("domain", "")
        by_dom[d] = by_dom.get(d, 0) + 1
        s = n.get("section_id", "")
        by_sec[s] = by_sec.get(s, 0) + 1

    texts = " ".join(n.get("original_text", "") for n in nodes)

    def has_sub(s: str) -> bool:
        return s.lower() in texts.lower()

    admission_root = has_sub(ADMISSION_EXPECTED_TEXTS["admission_root"])
    root_hint = None
    for n in nodes:
        if ADMISSION_EXPECTED_TEXTS["admission_root"].lower() in n.get("original_text", "").lower():
            lh = n.get("logic_hint") or {}
            root_hint = lh.get("inferred_operator")
            break

    expected_admission = {k: has_sub(v) for k, v in ADMISSION_EXPECTED_TEXTS.items()}

    discharge_planning = any(
        n.get("section_id") == f"{mcg_code}.section.discharge_planning" for n in nodes
    )
    discharge_dest = any(
        n.get("section_id") == f"{mcg_code}.section.discharge_destination" for n in nodes
    )

    return {
        "source_node_count": len(nodes),
        "section_count": len(sections),
        "table_count": len(tables),
        "footnote_count": len(footnotes),
        "reference_count": len(references),
        "code_group_count": len(codes),
        "node_count_by_domain": by_dom,
        "node_count_by_section": by_sec,
        "admission": {
            "found": any(s["section_key"] == "admission" for s in sections),
            "root_found": admission_root,
            "root_logic_hint": root_hint,
            "expected_path_texts_found": expected_admission,
        },
        "discharge": {
            "found": any(s["section_key"] == "discharge" for s in sections),
            "planning_found": discharge_planning,
            "destination_found": discharge_dest,
            "patient_safe_to_go_home_found": has_sub(DISCHARGE_EXPECTED_TEXTS["patient_safe"]),
            "medication_reconciliation_found": has_sub(DISCHARGE_EXPECTED_TEXTS["med_rec"]),
        },
        "orc_table_found": any(t.get("table_id", "").endswith("orc.001") for t in tables),
        "nodes_missing_original_text": [
            n["source_node_id"] for n in nodes if not (n.get("original_text") or "").strip()
        ],
        "nodes_missing_source_id": [
            n["source_node_id"] for n in nodes if not n.get("source_node_id")
        ],
        "duplicate_source_node_ids": _dup_ids(nodes),
        "ui_noise_detected": _detect_ui_noise(nodes),
        "residual_expand_text": _residual_expand(nodes, md_text),
        "warnings": [],
    }


def _dup_ids(nodes: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    dups: set[str] = set()
    for n in nodes:
        sid = n.get("source_node_id", "")
        if sid in seen:
            dups.add(sid)
        seen.add(sid)
    return sorted(dups)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _roundtrip_markdown(
    mcg_code: str,
    sections: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    tables: list[dict[str, Any]],
) -> str:
    def children_of(pid: str | None) -> list[dict[str, Any]]:
        return [n for n in nodes if n.get("parent_source_node_id") == pid]

    def tree_md(parent_id: str | None, indent: int) -> list[str]:
        lines: list[str] = []
        kids = sorted(children_of(parent_id), key=lambda x: x.get("sort_order", 0))
        for ch in kids:
            pad = "  " * indent
            lines.append(f"{pad}- {ch.get('original_text', '')}")
            lines.extend(tree_md(ch["source_node_id"], indent + 1))
        return lines

    sec_admission = next((s for s in sections if s["section_key"] == "admission"), None)
    sec_dp = next((s for s in sections if s["section_key"] == "discharge_planning"), None)
    sec_dd = next((s for s in sections if s["section_key"] == "discharge_destination"), None)

    lines = [f"# {mcg_code} Source Tree Roundtrip", ""]
    lines.append("## Admission")
    lines.append("")
    if sec_admission:
        roots = [
            n
            for n in nodes
            if n.get("section_id") == sec_admission["section_id"] and not n.get("parent_source_node_id")
        ]
        roots = sorted(roots, key=lambda x: x.get("sort_order", 0))
        for r in roots:
            lines.append(f"- {r.get('original_text', '')}")
            lines.extend(tree_md(r["source_node_id"], 1))
    lines.append("")
    lines.append("## Discharge")
    lines.append("")
    lines.append("### Discharge Planning")
    lines.append("")
    if sec_dp:
        roots = [
            n
            for n in nodes
            if n.get("section_id") == sec_dp["section_id"] and not n.get("parent_source_node_id")
        ]
        roots = sorted(roots, key=lambda x: x.get("sort_order", 0))
        for r in roots:
            lines.append(f"- {r.get('original_text', '')}")
            lines.extend(tree_md(r["source_node_id"], 1))
    lines.append("")
    lines.append("### Discharge Destination")
    lines.append("")
    if sec_dd:
        roots = [
            n
            for n in nodes
            if n.get("section_id") == sec_dd["section_id"] and not n.get("parent_source_node_id")
        ]
        roots = sorted(roots, key=lambda x: x.get("sort_order", 0))
        for r in roots:
            lines.append(f"- {r.get('original_text', '')}")
            lines.extend(tree_md(r["source_node_id"], 1))

    lines.append("")
    lines.append("## Optimal Recovery Course")
    lines.append("")
    orc = next((t for t in tables if "orc" in t.get("table_id", "")), None)
    if orc:
        cols = orc.get("columns", [])
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
        for row in orc.get("rows", [])[:10]:
            cells = row.get("cells", {})
            def cell_str(c: Any) -> str:
                if isinstance(c, list):
                    return "; ".join(c)
                return str(c or "")
            lines.append("| " + " | ".join(cell_str(cells.get(c, "")) for c in cols) + " |")
        if len(orc.get("rows", [])) > 10:
            lines.append("")
            lines.append("(Table truncated in roundtrip preview; see JSON for full rows.)")
    lines.append("")
    return "\n".join(lines)


def _meta_dict(soup: BeautifulSoup) -> dict[str, str]:
    meta: dict[str, str] = {}
    for m in soup.find_all("meta"):
        name = m.get("name")
        if name:
            meta[name] = str(m.get("content") or "")
    return meta


def build_tree(
    *,
    mcg_code: str,
    mcg_title: str,
    expanded_html_path: Path,
    out_dir: Path,
) -> None:
    raw_html = expanded_html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw_html, "lxml")
    meta = _meta_dict(soup)
    sha = _sha256_file(expanded_html_path)
    extracted = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    edition = meta.get("Edition") or _edition_from_soup(soup)
    title_el = soup.find("title")
    product_name = _normalize_ws(title_el.get_text()) if title_el else None

    source_document: dict[str, Any] = {
        "mcg_code": mcg_code,
        "mcg_title": mcg_title,
        "org": meta.get("ORG", "").strip() or None,
        "product": meta.get("Product", "").strip() or None,
        "product_name": product_name,
        "edition": edition,
        "file": meta.get("File", "").strip() or None,
        "goal_length_of_stay": meta.get("Goal Length of Stay", "").strip() or None,
        "source_html_path": str(expanded_html_path.as_posix()),
        "source_html_sha256": sha,
        "extracted_at": extracted,
        "condition": meta.get("Condition"),
        "code_description": meta.get("Code Description"),
        "icd_10_diagnosis_meta": meta.get("ICD-10 Diagnosis"),
        "keywords": meta.get("Keywords"),
    }

    collapse_raw = _extract_collapse_map(raw_html)
    sections = _build_sections(soup, mcg_code)

    orc_section = next((s for s in sections if s["section_key"] == "optimal_recovery_course"), None)
    tables: list[dict[str, Any]] = []
    if orc_section:
        t = _parse_orc_table(soup, mcg_code, orc_section)
        if t:
            tables.append(t)

    ref_section = next((s for s in sections if s["section_key"] == "references"), None)
    references = _extract_references(soup, mcg_code, ref_section or {})

    footnotes = _extract_footnotes(soup, mcg_code)
    codes = _extract_codes_from_meta(meta, mcg_code)

    cmap = collapse_raw.get("parsed") or {}
    nodes = _build_source_nodes(soup, sections, mcg_code, cmap)

    md = _roundtrip_markdown(mcg_code, sections, nodes, tables)
    audit = _run_audit(
        mcg_code, sections, nodes, tables, footnotes, references, codes, md
    )

    doc: dict[str, Any] = {
        "schema_version": "mcg_source_tree.v1",
        "source_document": source_document,
        "sections": sections,
        "source_nodes": nodes,
        "tables": tables,
        "footnotes": footnotes,
        "references": references,
        "codes": codes,
        "collapse_expand_map": collapse_raw,
        "audit": audit,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{mcg_code}.source-tree"
    main_path = out_dir / f"{mcg_code}.source-tree.v1.json"
    main_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    _write_jsonl(out_dir / f"{mcg_code}.source-nodes.jsonl", nodes)
    _write_jsonl(out_dir / f"{mcg_code}.sections.jsonl", sections)
    _write_jsonl(out_dir / f"{mcg_code}.tables.jsonl", tables)
    _write_jsonl(out_dir / f"{mcg_code}.footnotes.jsonl", footnotes)
    _write_jsonl(out_dir / f"{mcg_code}.references.jsonl", references)
    _write_jsonl(out_dir / f"{mcg_code}.codes.jsonl", codes)
    (out_dir / f"{mcg_code}.source-tree.audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / f"{mcg_code}.source-tree.roundtrip.md").write_text(md, encoding="utf-8")
    print(f"Wrote {main_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build MCG source tree JSON from expanded HTML.")
    ap.add_argument("--mcg-code", required=True)
    ap.add_argument("--title", required=True, dest="mcg_title")
    ap.add_argument("--expanded-html", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    if not args.expanded_html.is_file():
        print(f"Missing input: {args.expanded_html}", file=sys.stderr)
        sys.exit(1)
    build_tree(
        mcg_code=args.mcg_code,
        mcg_title=args.mcg_title,
        expanded_html_path=args.expanded_html,
        out_dir=args.out_dir,
    )


if __name__ == "__main__":
    main()
