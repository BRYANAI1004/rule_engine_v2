#!/usr/bin/env python3
"""
Offline audit of MCG expanded HTML for definition/glossary popup signals.

Reads saved expanded HTML (+ optional manifest) and summarizes how clinical
definitions are likely surfaced (onclick/href overlays) vs absent from DOM.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _repo_root(script_dir: Path) -> Path:
    return script_dir.parents[1]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Audit M083-style popup/definition signals in captured HTML (+ completeness preflight).")
    p.add_argument("--mcg-code", required=True, dest="mcg_code", help="e.g. M083")
    p.add_argument(
        "--expanded-html",
        required=True,
        dest="expanded_html",
        help="Path to *.full.expanded.html",
    )
    p.add_argument("--out-dir", required=True, dest="out_dir", help="e.g. rules/mcg/audits")
    p.add_argument(
        "--definitions-json",
        dest="definitions_json",
        default="",
        help="Optional path to *.definitions.raw.json (defaults under rules/mcg/raw-html/)",
    )
    p.add_argument(
        "--popups-json",
        dest="popups_json",
        default="",
        help="Optional path to *.popups.raw.json",
    )
    p.add_argument(
        "--manifest",
        dest="manifest",
        default="",
        help="Optional path to *.capture-manifest.json",
    )
    p.add_argument(
        "--skip-completeness-preflight",
        action="store_true",
        help="Only run BeautifulSoup expanded-HTML heuristic audit",
    )
    return p.parse_args(argv)


_POPUP_HINT_RE = re.compile(
    r"javascript:|void\s*\(|onclick|glossary|definition|popover|tooltip|(?:^|\/)mcm|context|dlg|dialog|modal|popup",
    re.I,
)
_EXPAND_UI_RE = re.compile(
    r"expand\s*all|collapse\s*all|print\s+view|benchmark\s|return\s+to\s+top|link\s+to\s+codes|show\s+all\s+code",
    re.I,
)


def _read_text_maybe(path: Path) -> tuple[Optional[str], Optional[str]]:
    if not path.exists():
        return None, f"missing:{path.relative_to(Path.cwd()) if path.is_absolute() else path}"
    try:
        return path.read_text(encoding="utf-8"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"read_failed:{exc}"


def _collect_candidate_triggers(html_text: str) -> list[dict[str, Any]]:
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("beautifulsoup4 is required (see requirements.txt)") from exc

    soup = BeautifulSoup(html_text, "lxml")

    triggers: list[dict[str, Any]] = []

    elems = soup.select(
        "a,"
        "[role=\"link\"],"
        "span[onclick],"
        "font[onclick],"
        "button[onclick],"
        "td[onclick],"
        "[title][onclick]",
    )

    role_link_elems = soup.select("[role=\"link\"]")

    elems = list(dict.fromkeys(list(elems) + list(role_link_elems)))

    seen = set()
    for node in elems:
        try:
            name = getattr(node, "name", "").lower()
        except Exception:
            continue

        attrs = getattr(node, "attrs", {}) or {}

        allowed_name = (
            name in {"a", "span", "font", "button", "td"}
            or attrs.get("role") == "link"
        )
        if not allowed_name:
            continue

        raw_text = ""
        try:
            raw_text = " ".join((node.get_text(" ", strip=True) or "").split())
        except Exception:
            raw_text = ""

        tl = raw_text.strip().lower()

        href = attrs.get("href") or ""
        onclick = attrs.get("onclick") or ""

        ttl = attrs.get("title") or attrs.get("aria-label") or ""
        elem_id = attrs.get("id") or ""
        class_names = attrs.get("class")

        cls = ""
        if isinstance(class_names, list):
            cls = " ".join(class_names)
        elif isinstance(class_names, str):
            cls = class_names

        if _EXPAND_UI_RE.search(tl) or _EXPAND_UI_RE.search((ttl or "").lower()):
            continue

        data_attrs: dict[str, str] = {}
        if isinstance(attrs, dict):
            for k, v in attrs.items():
                ks = str(k)
                if isinstance(v, list):
                    vv = " ".join(str(x) for x in v)
                else:
                    vv = str(v) if v is not None else ""
                if ks.startswith("data-"):
                    data_attrs[ks] = vv

        blob_attrs = html.unescape(onclick + " " + href + " " + cls + " " + elem_id + " " + ttl)
        suspicious = False
        if onclick.strip():
            suspicious = True
        if isinstance(href, str) and _POPUP_HINT_RE.search(href):
            suspicious = True
        if _POPUP_HINT_RE.search(blob_attrs):
            suspicious = True
        if data_attrs:
            suspicious = True
        if ttl and _POPUP_HINT_RE.search(html.unescape(ttl)):
            suspicious = True

        if not suspicious:
            continue

        if name == "button" and "expand" in tl.lower() and not _POPUP_HINT_RE.search(blob_attrs):
            continue

        snippet = ""
        try:
            snippet = str(node)[:500]
        except Exception:
            snippet = ""

        key = "|".join(
            (
                tl[:120],
                href[:200],
                onclick[:240],
                cls[:240],
                elem_id[:120],
            ),
        )

        if key in seen:
            continue
        seen.add(key)

        triggers.append(
            {
                "tag": name or "?",
                "text": tl[:260],
                "href": html.unescape(href)[:260] if isinstance(href, str) else "",
                "onclick": html.unescape(onclick)[:400] if isinstance(onclick, str) else "",
                "class": html.unescape(cls)[:260] if cls else "",
                "id": elem_id[:120] if elem_id else "",
                "title_or_aria": html.unescape(str(ttl))[:260],
                "data_attr_keys_sample": sorted(data_attrs.keys())[:12],
                "outer_html_truncated": snippet,
            },
        )

    return triggers


def _hidden_definition_signals(html_text: str) -> dict[str, Any]:
    hints = []
    lowered = html_text.lower()
    for needle in ["definition", "definitions", "tooltip", "popover", "glossary"]:
        hints.append({"needle": needle, "substring_count_in_html": lowered.count(needle)})

    # Very rough indication of visually hidden blobs (often used for glossary content)
    style_hidden = lowered.count("display:none") + lowered.count("visibility:hidden")

    dialogish = lowered.count('role="dialog"') + lowered.count("aria-modal")
    iframeish = lowered.count("<iframe")

    # Check for obvious definition title pattern in raw HTML text
    has_def_dash_pattern = bool(re.search(r"definition\s*-\s*hemodynamic", lowered))

    return {
        "needle_stats": hints,
        "approx_style_hidden_signals": style_hidden,
        "dialog_role_or_aria_modal_mentions_approx": dialogish,
        "iframe_mentions_approx": iframeish,
        "substring_definition_dash_hemodynamic": has_def_dash_pattern,
    }


def _read_capture_manifest(manifest_path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    txt, err = _read_text_maybe(manifest_path)
    if txt is None:
        return None, err
    try:
        return json.loads(txt), None
    except Exception as exc:  # noqa: BLE001
        return None, f"manifest_json_invalid:{exc}"


def _grepped_scripts(script_dir: Path) -> dict[str, Any]:
    core_path = script_dir / "capture_core.py"
    mcg_path = script_dir / "capture_mcg.py"
    out: dict[str, Any] = {"files": {}, "notes": []}

    for fname, path in (("capture_core.py", core_path), ("capture_mcg.py", mcg_path)):
        t, err = _read_text_maybe(path)
        if t is None:
            out["files"][fname] = {"present": False, "read_error": err}
            if err:
                out["notes"].append(f"{fname}:{err}")
            continue

        lc = t.lower()

        clues: list[str] = []
        if "run_expand_passes" in t:
            clues.append("implements_expand_click_loop(run_expand_passes)")
        if "page.content()" in t:
            clues.append("persists_DOM_via_page_content()")
        lc = t.lower()
        if "run_definition_capture" in t or "definition_capture" in lc or "capture_definitions" in lc:
            clues.append("includes_definition_popup_capture_symbols")

        out["files"][fname] = {
            "present": True,
            "bytes": len(t.encode("utf-8")),
            "has_run_expand_passes": "run_expand_passes" in t,
            "has_page_content_persist": "page.content()" in t,
            "has_definition_capture_hook": ("run_definition_capture" in t) or ("definition_capture" in lc),
            "lightweight_clues": clues,
        }

    return out


def audit(*, expanded_html_path: Path, manifest_path: Path, script_dir: Path, mcg_code: str) -> dict[str, Any]:
    txt, html_err = _read_text_maybe(expanded_html_path)
    lowered = (txt or "").lower()

    def _has(pat: str) -> bool:
        return pat.lower() in lowered if txt is not None else False

    exp_def_hi = bool(re.search(r"definition\s*-\s*hemodynamic\b", lowered)) if txt else False
    exp_tachy = ("tachycardia" in lowered) if txt else False
    exp_shock = ("shock index" in lowered) if txt else False
    exp_map = ("mean arterial pressure" in lowered) if txt else False
    exp_vaso = ("vasopressor" in lowered) if txt else False
    exp_lac = ("lactate" in lowered) if txt else False

    manifest, man_err = _read_capture_manifest(manifest_path)
    triggers: list[dict[str, Any]] = []
    trigger_err = None

    hidden = {}

    warnings: list[str] = []

    if html_err:
        warnings.append(html_err)

    if man_err:
        warnings.append(str(man_err))

    candidate_trigger_examples: list[dict[str, Any]] = []

    hemodynamic_trigger_like = False
    hemodynamic_plain_in_html = False
    nested_hint = ""

    if txt:
        triggers = _collect_candidate_triggers(txt)
        hidden = _hidden_definition_signals(txt)
        candidate_trigger_examples = triggers[:40]

        for tr in triggers:
            ttxt = str(tr.get("text") or "")
            blob = html.unescape(
                (
                    str(tr.get("href") or "")
                    + " "
                    + str(tr.get("onclick") or "")
                    + " "
                    + str(tr.get("class") or "")
                    + str(tr.get("title_or_aria") or ""),
                ).lower(),
            )
            tl = ttxt.lower()
            if ("hemodynamic" in tl or "instability" in tl) and _POPUP_HINT_RE.search(blob):
                hemodynamic_trigger_like = True

        hemodynamic_plain_in_html = "hemodynamic" in lowered or "instability" in lowered
        nested_hint = "Likely YES if glossary popovers allow nested clickable terms after first open."
    else:
        warnings.append(
            "expanded_html_missing:_cannot_audit_triggers_via_beautifulsoup;_run_capture_then_re_audit",
        )

    hemodynamic_trigger_found = bool(hemodynamic_trigger_like)

    likely_mechanism = (
        "If expanded HTML lacks 'Definition - …' verbatim but includes many javascript:/onclick glossary-style "
        "controls, definitions are injected only after transient UI interaction (mouseover/click) into overlays "
        "(often role=dialog/aria-modal, fixed DIVs). Capturing innerHTML alone will miss popup-only text."
        if triggers and not exp_def_hi
        else (
            "If even suspicious triggers are scarce, glossary content may rely on scripted HTTP fetch or iframe "
            "content not inlined in DOM until interaction."
            if txt and not triggers
            else ("Cannot infer mechanism confidently without expanded HTML." if txt is None else "Inspect triggers list.")
        )
    )

    missing_why = (
        "Historic note: early captures saved expanded DOM without opening glossary overlays. "
        "Current `definition_capture` persists popup text/HTML into `*.definitions.raw.json` / `*.popups.raw.json`; "
        "expanded HTML may still lack verbatim `Definition - …` when those strings exist only in dialogs."
    )

    audit_obj: dict[str, Any] = {
        # Required keys requested by ops:
        "expanded_html_contains_definition_hemodynamic_instability": bool(exp_def_hi),
        "expanded_html_contains_tachycardia": bool(exp_tachy),
        "expanded_html_contains_shock_index": bool(exp_shock),
        "candidate_trigger_count": int(len(triggers)),
        "candidate_trigger_examples": candidate_trigger_examples,
        "hemodynamic_trigger_found": bool(hemodynamic_trigger_found),
        "likely_popup_mechanism": likely_mechanism,
        "recommended_capture_fix": (
            "After Expand All finishes, systematically click glossary/definition candidates, detect transient "
            "modals/overlays containing 'Definition -', serialize popup inner HTML/text, dedupe + recurse safely, "
            "then persist parallel definition artifacts without mutating baseline expanded DOM snapshot."
        ),
        "warnings": warnings,
        # Extra investigative fields:
        "mcg_code": mcg_code,
        "expanded_html_path": str(expanded_html_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_present": manifest is not None,
        "expanded_html_contains_plain_hemodynamic_or_instability": bool(hemodynamic_plain_in_html),
        "expanded_html_contains_hidden_style_signals": hidden,
        "why_current_capture_missed_popup_definitions": missing_why,
        "nested_definition_links_inside_popup_likely_clickable_hint": nested_hint,
        "scripts_review_stub": _grepped_scripts(script_dir),
        "presence_checks": {
            "mean_arterial_pressure": bool(exp_map),
            "vasopressor": bool(exp_vaso),
            "lactate": bool(exp_lac),
            "onclick_common": bool(lowered.count("onclick")) if txt else False,
            "iframe_count_approx": lowered.count("<iframe") if txt else False,
        },
        "requested_markers_secondary": {
            "Contains 'Clinical Indications' marker": bool(_has("clinical indications")),
        },
    }
    return audit_obj


def _render_md(audit_obj: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Popup / glossary capture audit — `{audit_obj.get('mcg_code','?')}`")
    lines.append("")
    lines.append("## Questions (answers)")
    lines.append("")
    lines.append(
        f"- **Does expanded HTML contain popup/definition verbatim content?** "
        f"Definition-Hemodynamic: **{audit_obj.get('expanded_html_contains_definition_hemodynamic_instability')}**; "
        f"Tachycardia substring: **{audit_obj.get('expanded_html_contains_tachycardia')}**.",
    )
    lines.append("")
    lines.append("- **Hidden definition containers?** See `expanded_html_contains_hidden_style_signals` in JSON.")
    lines.append("")
    lines.append(f"- **Clickable triggers (BS4 heuristic)**: **{audit_obj.get('candidate_trigger_count')}** candidates.")
    lines.append("")
    lines.append("- **Trigger attribute examples**: see `candidate_trigger_examples` array in JSON (href/onclick/class/id/title/data-*).")
    lines.append("")
    lines.append("- **Loaded only after click?** Typically yes when expanded HTML lacks 'Definition - …' verbatim while triggers exist.")
    lines.append("")
    lines.append("- **Modal vs iframe vs dynamic DOM?** See `likely_popup_mechanism` in JSON.")
    lines.append("")
    lines.append("- **Nested glossary links inside popups clickable?** See `nested_definition_links_inside_popup_likely_clickable_hint` in JSON.")
    lines.append("")
    lines.append(f"- **'Hemodynamic instability' clickable trigger heuristic match?** **{audit_obj.get('hemodynamic_trigger_found')}**")
    lines.append("")
    lines.append(f"- **Why did capture miss it?** {audit_obj.get('why_current_capture_missed_popup_definitions','')}")
    lines.append("")
    lines.append("## Recommended capture fix")
    lines.append("")
    lines.append(str(audit_obj.get("recommended_capture_fix", "")))
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    sd = _script_dir()
    root = _repo_root(sd)
    expanded = Path(args.expanded_html).expanduser().resolve()

    manifest = root / "rules" / "mcg" / "raw-html" / f"{args.mcg_code}.capture-manifest.json"
    try:
        # prefer manifest beside expanded when user uses custom layout
        m_side = expanded.parent / f"{args.mcg_code}.capture-manifest.json"
        if m_side.exists():
            manifest = m_side
    except Exception:
        pass

    if str(getattr(args, "manifest", "") or "").strip():
        manifest = Path(args.manifest).expanduser().resolve()

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    exit_code = 0

    def _run_completeness() -> None:
        nonlocal exit_code
        if getattr(args, "skip_completeness_preflight", False):
            return
        import importlib.util

        comp_path = sd / "audit_capture_completeness.py"
        spec = importlib.util.spec_from_file_location("_audit_capture_completeness", comp_path)
        if spec is None or spec.loader is None:
            print("[preflight] skipped: audit_capture_completeness.py not loadable", flush=True)
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        mcg = str(args.mcg_code).strip()
        raw_dir = root / "rules/mcg/raw-html"
        defs_path = Path(args.definitions_json).expanduser().resolve() if str(args.definitions_json).strip() else raw_dir / f"{mcg}.definitions.raw.json"
        pops_path = Path(args.popups_json).expanduser().resolve() if str(args.popups_json).strip() else raw_dir / f"{mcg}.popups.raw.json"
        man_path = Path(args.manifest).expanduser().resolve() if str(args.manifest).strip() else manifest

        comp = mod.run_capture_completeness_audit(
            mcg_code=mcg,
            expanded_html=expanded,
            definitions_json=defs_path,
            popups_json=pops_path,
            manifest_path=man_path,
            out_dir=out_dir,
            repo_root=root,
            write_files=True,
        )
        print(f"[preflight] completeness_verdict={comp.get('verdict')}", flush=True)
        if comp.get("verdict") != "PASS":
            exit_code = 1

    _run_completeness()

    try:
        ao = audit(
            expanded_html_path=expanded,
            manifest_path=manifest,
            script_dir=sd,
            mcg_code=str(args.mcg_code).strip(),
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "beautifulsoup4" not in msg.lower() and "beautifulsoup" not in msg.lower():
            raise
        ao = {
            "mcg_code": str(args.mcg_code).strip(),
            "candidate_trigger_count": 0,
            "candidate_trigger_examples": [],
            "warnings": [msg],
            "expanded_html_path": str(expanded.resolve()),
            "manifest_path": str(manifest.resolve()),
            "manifest_present": manifest.exists(),
            "likely_popup_mechanism": "Skipped HTML trigger scrape (beautifulsoup4 not installed). Run completeness preflight output instead.",
            "why_current_capture_missed_popup_definitions": "",
            "recommended_capture_fix": str(exc),
        }

    stem = f"{args.mcg_code.strip()}.popup-capture.audit"
    jp = out_dir / f"{stem}.json"
    mp = out_dir / f"{stem}.md"

    jp.write_text(json.dumps(ao, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mp.write_text(_render_md(ao), encoding="utf-8")

    print(f"[audit] wrote: {jp}", flush=True)
    print(f"[audit] wrote: {mp}", flush=True)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
