from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


def load_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_jsonl(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def e(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.split())


def fmt_atom(rule: dict[str, Any]) -> str:
    original = clean_text(rule.get("original_text"))
    measurement = rule.get("measurement")
    op = rule.get("operator")
    value = rule.get("value")
    unit = rule.get("unit")
    condition_key = rule.get("condition_key", "")

    if measurement and op and value not in (None, ""):
        label = f"{measurement} {op} {value}"
        if unit:
            label += f" {unit}"
        return label

    if condition_key:
        return condition_key

    return original or rule.get("id", "")


class Renderer:
    def __init__(
        self,
        domain_tree: dict[str, Any],
        shared_defs: dict[str, Any],
        linked_refs: list[dict[str, Any]],
        *,
        scope: str,
        mcg_code: str,
        mcg_title: str,
    ) -> None:
        self.scope = scope
        self.mcg_code = mcg_code
        self.mcg_title = mcg_title
        self.domain_tree = domain_tree
        self.shared_defs = shared_defs
        self.domain_nodes = domain_tree.get("domain_nodes", [])
        self.logic_nodes = {
            n["logic_node_id"]: n
            for n in domain_tree.get("logic_nodes", [])
            if n.get("logic_node_id")
        }

        self.shared_conditions = {
            c["condition_key"]: c
            for c in shared_defs.get("conditions", [])
            if c.get("condition_key")
        }
        self.shared_composites = {
            c["id"]: c
            for c in shared_defs.get("composite_definitions", [])
            if c.get("id")
        }
        self.shared_atoms = {
            a["id"]: a
            for a in shared_defs.get("atomic_rules", [])
            if a.get("id")
        }

        self.linked_by_logic_id = {
            r.get("logic_node_id"): r
            for r in linked_refs
            if r.get("logic_node_id")
        }

    def _scope_domains(self) -> tuple[str, ...]:
        """Resolve domain filter for Level‑3 exported paths."""

        raw = str(self.scope or "admission").strip()
        if raw == "admission_discharge":
            return ("admission", "discharge")
        return (raw,)

    def main_heading_title(self) -> str:
        if str(self.scope) == "admission_discharge":
            return (
                f"{self.mcg_code} {self.mcg_title} - Integrated Admission & Discharge Rule Hierarchy"
            )
        return f"{self.mcg_code} {self.mcg_title} - Integrated Admission Rule Hierarchy"

    def scoped_paths(self, domain: str | None = None) -> list[dict[str, Any]]:
        doms = self._scope_domains() if domain is None else (domain,)
        paths = [
            n
            for n in self.domain_nodes
            if n.get("domain") in doms and n.get("level") == 3 and n.get("logic_root_id")
        ]
        path_order_admission_first = {"admission": 0, "discharge": 1}
        return sorted(
            paths,
            key=lambda n: (
                path_order_admission_first.get(str(n.get("domain") or ""), 99),
                n.get("sort_order", 9999),
                n.get("node_id", ""),
            ),
        )

    def node_label(self, node: dict[str, Any]) -> str:
        kind = node.get("node_kind", "")
        op = node.get("operator", "")
        condition_key = node.get("condition_key", "")
        text = clean_text(node.get("original_text"))

        bits = []
        if kind:
            bits.append(kind)
        if op:
            bits.append(op)
        if condition_key:
            bits.append(condition_key)

        prefix = " / ".join(bits)
        if prefix:
            return f"[{prefix}] {text}"
        return text

    def render_domain_logic(self, logic_node_id: str, depth: int = 0, seen: set[str] | None = None) -> str:
        if seen is None:
            seen = set()
        if logic_node_id in seen:
            return f"<li class='cycle'>Cycle skipped: {e(logic_node_id)}</li>"
        seen.add(logic_node_id)

        node = self.logic_nodes.get(logic_node_id)
        if not node:
            return f"<li class='missing'>Missing logic node: {e(logic_node_id)}</li>"

        condition_key = node.get("condition_key")
        linked = self.linked_by_logic_id.get(logic_node_id)
        link_status = linked.get("definition_link_status") if linked else None
        css = "domain-node"
        if link_status == "linked_shared_definition":
            css += " linked"
        elif link_status == "linked_shared_atomic_definition":
            css += " linked linked-atomic"
        elif condition_key:
            css += " unlinked"

        label = self.node_label(node)
        out = [f"<li class='{css}'><div class='node-line'>{e(label)}</div>"]

        if condition_key:
            if link_status == "linked_shared_definition" and condition_key in self.shared_conditions:
                cond = self.shared_conditions[condition_key]
                root_id = cond.get("root_composite_id")
                out.append("<div class='shared-box'>")
                out.append(
                    f"<div class='shared-title'>Linked shared definition: "
                    f"{e(condition_key)} -> {e(root_id)}</div>"
                )
                out.append(f"<div class='source-title'>Source: {e(cond.get('source_popup_title'))}</div>")
                if root_id:
                    out.append("<ul class='shared-tree'>")
                    out.append(self.render_shared_node(root_id, seen=set()))
                    out.append("</ul>")
                out.append("</div>")
            elif link_status == "linked_shared_atomic_definition":
                linked_row = linked or {}
                raid = str(linked_row.get("root_atomic_id") or "")
                atom = self.shared_atoms.get(raid)
                cond = self.shared_conditions.get(condition_key, {})
                out.append("<div class='shared-box'>")
                out.append(
                    f"<div class='shared-title'>Linked shared atomic definition: "
                    f"{e(condition_key)} -> {e(raid)}</div>"
                )
                if atom:
                    adt = atom.get("definition_type")
                    meta_ln = ""
                    if adt == "atomic_numeric":
                        mv = atom.get("measurement")
                        op = atom.get("operator")
                        val = atom.get("value")
                        unit = atom.get("unit")
                        if mv and op and val is not None:
                            meta_ln = f"{mv} {op} {val}"
                            if unit:
                                meta_ln += f" {unit}"
                    elif adt == "atomic_flag":
                        meta_ln = "IS_TRUE"
                    if meta_ln:
                        out.append(f"<div class='atomic-meta'>{e(meta_ln)}</div>")
                    ot = clean_text(atom.get("original_text"))
                    if len(ot) > 280:
                        ot = ot[:277].rstrip() + "..."
                    out.append(f"<div class='orig'>{e(ot)}</div>")
                    rv = atom.get("review_status")
                    ss = atom.get("source_strategy") or cond.get("source_strategy")
                    out.append(
                        f"<div class='atomic-foot'>review_status={e(rv)} source_strategy={e(ss)}</div>"
                    )
                else:
                    out.append("<div class='missing'>Missing atomic definition row.</div>")
                out.append("</div>")
            else:
                out.append(
                    "<div class='unlinked-note'>No shared definition linked yet - treated as current direct leaf / extractor target.</div>"
                )

        children = node.get("child_logic_node_ids") or []
        if children:
            out.append("<ul>")
            for child in children:
                out.append(self.render_domain_logic(child, depth + 1, seen=set(seen)))
            out.append("</ul>")

        out.append("</li>")
        return "\n".join(out)

    def render_shared_node(self, item_id: str, seen: set[str] | None = None) -> str:
        if seen is None:
            seen = set()
        if item_id in seen:
            return f"<li class='cycle'>Shared cycle skipped: {e(item_id)}</li>"
        seen.add(item_id)

        if item_id in self.shared_atoms:
            atom = self.shared_atoms[item_id]
            review = atom.get("review_status")
            review_html = f" <span class='review'>({e(review)})</span>" if review and review != "ok" else ""
            text = fmt_atom(atom)
            original = clean_text(atom.get("original_text"))
            return (
                f"<li class='shared-atom'><span class='atom-label'>{e(text)}</span>"
                f"{review_html}"
                f"<div class='orig'>{e(original)}</div></li>"
            )

        comp = self.shared_composites.get(item_id)
        if not comp:
            return f"<li class='missing'>Missing shared child: {e(item_id)}</li>"

        op = comp.get("operator", "")
        key = comp.get("condition_key", "")
        text = clean_text(comp.get("original_text"))
        review = comp.get("review_status")
        review_html = f" <span class='review'>({e(review)})</span>" if review and review != "ok" else ""

        out = [
            "<li class='shared-comp'>",
            f"<div><span class='op'>{e(op)}</span> "
            f"<span class='condition-key'>{e(key)}</span>{review_html}</div>",
            f"<div class='orig'>{e(text)}</div>",
        ]

        children = comp.get("children") or []
        if children:
            out.append("<ul>")
            for child in children:
                out.append(self.render_shared_node(child, seen=set(seen)))
            out.append("</ul>")
        out.append("</li>")
        return "\n".join(out)

    def render_definition_appendix(self) -> str:
        """Minimal Definition appendix: grouped condition_key lists only (no trees or original text)."""

        def needs_review_atom(a: dict[str, Any]) -> bool:
            if str(a.get("review_status") or "") == "needs_review":
                return True
            if a.get("evaluator_ready") is False:
                return True
            ss = str(a.get("source_strategy") or "")
            if ss in ("pediatric_placeholder", "manual_review_placeholder"):
                return True
            return False

        def needs_review_condition(c: dict[str, Any]) -> bool:
            if str(c.get("review_status") or "") == "needs_review":
                return True
            if c.get("evaluator_ready") is False:
                return True
            if str(c.get("definition_type") or "") == "shared_atomic_condition":
                aid = str(c.get("root_atomic_id") or "")
                sub_a = self.shared_atoms.get(aid)
                if sub_a and needs_review_atom(sub_a):
                    return True
            return False

        conditions = [c for c in (self.shared_defs.get("conditions") or []) if isinstance(c, dict)]
        atoms = [a for a in (self.shared_defs.get("atomic_rules") or []) if isinstance(a, dict)]

        needs_keys: set[str] = set()
        for c in conditions:
            ck = str(c.get("condition_key") or "").strip()
            if ck and needs_review_condition(c):
                needs_keys.add(ck)
        for a in atoms:
            ck = str(a.get("condition_key") or "").strip()
            if ck and needs_review_atom(a):
                needs_keys.add(ck)

        composite_keys: set[str] = set()
        numeric_keys: set[str] = set()
        boolean_keys: set[str] = set()

        for c in conditions:
            ck = str(c.get("condition_key") or "").strip()
            if not ck or ck in needs_keys:
                continue
            dt = str(c.get("definition_type") or "")
            if dt == "shared_composite_condition":
                composite_keys.add(ck)
            elif dt == "shared_atomic_condition":
                aid = str(c.get("root_atomic_id") or "")
                at = self.shared_atoms.get(aid)
                if not at:
                    continue
                adt = str(at.get("definition_type") or "")
                if adt == "atomic_numeric":
                    numeric_keys.add(ck)
                elif adt == "atomic_flag":
                    boolean_keys.add(ck)

        for a in atoms:
            ck = str(a.get("condition_key") or "").strip()
            if not ck or ck in needs_keys:
                continue
            adt = str(a.get("definition_type") or "")
            if adt == "atomic_numeric":
                numeric_keys.add(ck)
            elif adt == "atomic_flag":
                boolean_keys.add(ck)

        numeric_keys -= composite_keys
        boolean_keys -= composite_keys

        lines: list[str] = []
        lines.append("<section class='definition-appendix'>")
        lines.append("<h1 class='def-root'>Definition</h1>")

        def sub(title: str) -> None:
            lines.append(f"<h2 class='def-section'>{e(title)}</h2>")

        def bullet_list(keys: set[str]) -> None:
            if not keys:
                lines.append("<p class='def-none'>_None._</p>")
                return
            lines.append("<ul class='def-key-list'>")
            for ck in sorted(keys):
                lines.append(f"<li>{e(ck)}</li>")
            lines.append("</ul>")

        sub("Composite Definitions")
        bullet_list(composite_keys)

        sub("Atomic Numeric Definitions")
        bullet_list(numeric_keys)

        sub("Atomic Boolean / Leaf Definitions")
        bullet_list(boolean_keys)

        sub("Needs Review / Pediatric Placeholders")
        bullet_list(needs_keys)

        lines.append("</section>")
        return "\n".join(lines)

    def html_body(self) -> str:
        out = []
        out.append("<section class='top'>")
        out.append(f"<h1>{e(self.main_heading_title())}</h1>")
        scope_label = (
            self.scope.replace("_", " ").title()
            if self.scope
            else "Admission"
        )
        out.append(
            f"<div class='subtitle'>{e(scope_label)}. Domain Level‑3 paths are shown with linked "
            f"definitions embedded under each rule.</div>"
        )
        out.append("</section>")

        out.append(self.render_definition_appendix())

        out.append("<section class='rule-root'>")

        if str(self.scope) in ("admission", "admission_discharge"):
            adm_paths = self.scoped_paths("admission")
            out.append(f"<h2>{e(self.mcg_code)} — Admission</h2>")
            out.append(
                "<div class='main-admission'><b>Admission is indicated for 1 or more of the "
                "following</b></div>"
            )
            out.append("<ul class='path-list'>")

            for idx, path in enumerate(adm_paths, start=1):
                path_text = clean_text(path.get("original_text"))
                out.append("<li class='path-block'>")
                out.append(f"<div class='path-title'><b>Path {idx}: {e(path_text)}</b></div>")
                out.append("<ul class='domain-tree'>")
                out.append(self.render_domain_logic(path["logic_root_id"]))
                out.append("</ul>")
                out.append("</li>")

            out.append("</ul>")

        if str(self.scope) in ("discharge", "admission_discharge"):
            dis_paths = self.scoped_paths("discharge")
            if str(self.scope) == "admission_discharge":
                out.append("<hr style='margin:16px 0;' />")

            out.append(f"<h2>{e(self.mcg_code)} — Discharge</h2>")
            out.append(
                "<div class='main-discharge'><b>Discharge domain (planning + destination checklist "
                "items)</b></div>"
            )
            out.append("<ul class='path-list'>")

            for idx, path in enumerate(dis_paths, start=1):
                path_hdr = clean_text(path.get("description") or path.get("original_text"))
                path_text = clean_text(path.get("original_text"))
                title = path_hdr if path_hdr else path_text
                out.append("<li class='path-block'>")
                out.append(f"<div class='path-title'><b>Section {idx}: {e(title)}</b></div>")
                out.append("<ul class='domain-tree'>")
                out.append(self.render_domain_logic(path["logic_root_id"]))
                out.append("</ul>")
                out.append("</li>")

            out.append("</ul>")

        out.append("</section>")
        return "\n".join(out)


_CSS = """
@page {
  size: A4 landscape;
  margin: 8mm 7mm 9mm 7mm;
}
body {
  font-family: Arial, sans-serif;
  color: #111;
  font-size: 9px;
  line-height: 1.25;
}
h1 {
  font-size: 18px;
  margin: 0 0 4px 0;
  border-bottom: 2px solid #222;
  padding-bottom: 5px;
}
h2 {
  font-size: 15px;
  margin: 12px 0 6px 0;
  color: #0b3d69;
}
.subtitle {
  font-size: 10px;
  color: #555;
}
.main-admission {
  font-size: 13px;
  background: #eaf2fb;
  border-left: 5px solid #1f5f99;
  padding: 7px 9px;
  margin-bottom: 8px;
}
.main-discharge {
  font-size: 13px;
  background: #f4fbea;
  border-left: 5px solid #3d7316;
  padding: 7px 9px;
  margin-bottom: 8px;
}
ul {
  margin-top: 3px;
  margin-bottom: 3px;
  padding-left: 18px;
}
li {
  margin: 2px 0;
}
.path-block {
  page-break-inside: avoid;
  margin: 8px 0 10px 0;
  padding: 7px;
  border: 1px solid #c9d6e2;
  border-radius: 5px;
}
.path-title {
  font-size: 12px;
  color: #0b3d69;
  background: #eef5ff;
  padding: 5px 7px;
  border-left: 4px solid #1f5f99;
  margin-bottom: 5px;
}
.node-line {
  font-family: Menlo, Monaco, Consolas, "Courier New", monospace;
  white-space: pre-wrap;
}
.domain-node.linked > .node-line {
  font-weight: 700;
  color: #083b63;
}
.domain-node.linked-atomic > .node-line {
  font-weight: 700;
  color: #083b63;
}
.atomic-meta {
  font-family: Menlo, Monaco, Consolas, "Courier New", monospace;
  font-size: 8px;
  color: #333;
  margin: 2px 0;
}
.atomic-foot {
  font-size: 7px;
  color: #666;
  margin-top: 2px;
}
.definition-appendix {
  margin-top: 10px;
  margin-bottom: 12px;
}
.admission-root {
  margin-top: 14px;
}
h1.def-root {
  font-size: 16px;
  margin: 6px 0 4px 0;
}
h2.def-section {
  font-size: 12px;
  margin: 10px 0 4px 0;
  color: #0b3d69;
}
.definition-appendix ul.def-key-list {
  margin-top: 4px;
  margin-bottom: 8px;
}
.definition-appendix ul.def-key-list li {
  font-family: Menlo, Monaco, Consolas, "Courier New", monospace;
  font-size: 9px;
}
.definition-appendix .def-none {
  color: #777;
  font-size: 9px;
  margin: 2px 0 6px 0;
}
.unlinked-note {
  color: #777;
  font-size: 8px;
  margin: 2px 0 4px 0;
}
.shared-box {
  margin: 4px 0 5px 0;
  padding: 6px 7px;
  border-left: 4px solid #6b3fa0;
  background: #f7f3fb;
}
.shared-title {
  font-weight: 700;
  color: #4b217d;
}
.source-title {
  color: #555;
  font-size: 8px;
  margin-bottom: 4px;
}
.op {
  font-weight: 800;
  color: #5b2aa0;
}
.condition-key {
  font-weight: 700;
  color: #123f5d;
}
.atom-label {
  font-family: Menlo, Monaco, Consolas, "Courier New", monospace;
  font-weight: 700;
}
.orig {
  color: #555;
  font-size: 8px;
  margin-left: 2px;
}
.review {
  color: #9a5b00;
  font-weight: 700;
}
.missing, .cycle {
  color: #b00020;
}
"""


def generate_integrated_admission_pdf_html(
    domain_tree: dict[str, Any],
    shared_defs: dict[str, Any],
    linked_refs: list[dict[str, Any]],
    *,
    mcg_code: str,
    mcg_title: str,
    scope: str = "admission",
    document_title: str | None = None,
) -> tuple[str, str]:
    """
    Returns (html_document, escaped_title_tag_value).
    document_title overrides the <title> tag; when None, matches the rendered h1.
    """
    renderer = Renderer(
        domain_tree,
        shared_defs,
        linked_refs,
        scope=scope,
        mcg_code=mcg_code,
        mcg_title=mcg_title,
    )
    body = renderer.html_body()
    heading = renderer.main_heading_title()
    doc_title = document_title if document_title is not None else heading
    title = html.escape(doc_title)
    html_doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""
    return html_doc, title


def run_export(
    *,
    mcg_code: str,
    mcg_title: str,
    domain_rule_tree: str,
    shared_definitions: str,
    linked_condition_refs: str,
    output: str,
    scope: str = "admission",
    document_title: str | None = None,
) -> None:
    domain_tree = load_json(domain_rule_tree)
    shared_defs = load_json(shared_definitions)
    linked_refs = load_jsonl(linked_condition_refs)

    html_doc, _ = generate_integrated_admission_pdf_html(
        domain_tree,
        shared_defs,
        linked_refs,
        mcg_code=mcg_code,
        mcg_title=mcg_title,
        scope=scope,
        document_title=document_title,
    )

    out_pdf = Path(output)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    out_html = out_pdf.with_suffix(".html")
    out_html.write_text(html_doc, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1754, "height": 1240})
        page.set_content(html_doc, wait_until="load")
        page.pdf(
            path=str(out_pdf),
            format="A4",
            landscape=True,
            print_background=True,
            margin={"top": "8mm", "right": "7mm", "bottom": "9mm", "left": "7mm"},
        )
        browser.close()

    print(f"Wrote PDF: {out_pdf}")
    print(f"Wrote HTML: {out_html}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Export integrated admission rule hierarchy to PDF/HTML.")
    ap.add_argument("--mcg-code", required=True)
    ap.add_argument("--mcg-title", required=True)
    ap.add_argument("--domain-rule-tree", required=True)
    ap.add_argument("--shared-definitions", required=True)
    ap.add_argument("--linked-condition-refs", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument(
        "--scope",
        default="admission",
        help="Domain scope for Level 3 paths: admission, discharge, admission_discharge (default: admission).",
    )
    ap.add_argument(
        "--document-title",
        default=None,
        help="Overrides the HTML <title> tag only (defaults to the same string as the main heading).",
    )
    args = ap.parse_args()

    run_export(
        mcg_code=args.mcg_code.strip(),
        mcg_title=args.mcg_title.strip(),
        domain_rule_tree=args.domain_rule_tree,
        shared_definitions=args.shared_definitions,
        linked_condition_refs=args.linked_condition_refs,
        output=args.output,
        scope=args.scope.strip(),
        document_title=args.document_title,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
