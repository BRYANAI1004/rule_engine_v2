#!/usr/bin/env python3
"""
Step 1B: capture glossary/definition overlays after main guideline expansion.

Uses heuristic trigger discovery + resilient popup probing. Recursive depth is bounded.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from playwright.sync_api import ElementHandle, Frame, Locator, Page, TimeoutError

from progress_logger import CaptureTerminalReporter, ProgressLogger, update_elapsed

ContentRoot = Union[Page, Frame]


def locator_for_xpath(root: ContentRoot, xpath: str) -> Locator:
    """
    Playwright XPath selectors must look like xpath=/absolute/path — do not wrap the
    expression in JSON quotes (e.g. xpath="/html..." breaks engine evaluation).
    """
    if not xpath or not xpath.strip():
        raise ValueError("missing xpath")
    expr = xpath.strip()
    if expr.startswith("xpath="):
        return root.locator(expr)
    return root.locator(f"xpath={expr}")


_NOISE_TRIGGER_TEXT_RES = (
    r"^\s*$",
    r"print\s+view\b",
    r"benchmark\s+statistics\b",
    r"link\s+to\s+codes\b",
    r"return\s+to\s+top\b",
    r"expand\s+all\b",
    r"collapse\s+all\b",
    r"show\s+all\s+code\b",
    r"\bfootnotes?\b",
    r"\breferences\b",
    r"\bview\s+abstract\b",
    r"click\s+here\s+to\s+preview\b",
)


def _compile_noise_patterns() -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for pat in _NOISE_TRIGGER_TEXT_RES:
        try:
            out.append(re.compile(pat, flags=re.I))
        except re.error:
            continue
    return out


_NOISE_PATTERNS = _compile_noise_patterns()

_POPUP_DEFINITION_HEAD_RE = re.compile(r"popup_definition\s*\(", re.I)

_POPUP_DEFINITION_ARGS_RE = re.compile(
    r"popup_definition\s*\(\s*"
    r"(['\"])(?P<id>[^'\"]*)\1\s*,\s*"
    r"(['\"])(?P<title>[^'\"]*)\3\s*\)",
    re.I,
)

_POPUP_CITATION_HEAD_RE = re.compile(r"popup_citation\s*\(", re.I)
_POPUP_FOOTNOTE_HEAD_RE = re.compile(r"popup_footnote\s*\(", re.I)
_CONTEXT_POPUP_HEAD_RE = re.compile(
    r"(?:popup_context|popup_mcm_context|\w*[Ii]n_?[Cc]ontext(?:\w+)?|context\s*link|contextlink\d*)\s*\(",
    re.I,
)


def _strip_js_quotes(arg: str) -> Optional[str]:
    """If arg is a single-quoted JS string literal, return unescaped inner value."""
    a = (arg or "").strip()
    if len(a) < 2:
        return None
    quote = a[0]
    if quote not in ("'", '"'):
        return None
    bs = chr(92)
    if a[-1] != quote:
        return None
    inner: list[str] = []
    escape = False
    for ch in a[1:-1]:
        if escape:
            inner.append(ch)
            escape = False
        elif ch == bs:
            escape = True
        else:
            inner.append(ch)
    return "".join(inner)


def _matching_close_paren(s: str, open_paren_idx: int) -> Optional[int]:
    """If s[open_paren_idx] == '(', return index of matching ')' respecting quoted strings."""

    if open_paren_idx < 0 or open_paren_idx >= len(s) or s[open_paren_idx] != "(":
        return None

    bs = chr(92)
    depth = 0
    quote: Optional[str] = None
    i = open_paren_idx

    while i < len(s):
        ch = s[i]
        if quote:
            if ch == bs and i + 1 < len(s):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _extract_call_inner_content(onclick: str, callee: str) -> Optional[str]:
    """Return inner content inside the first (...) of callee(...) for clickable JS."""

    if not onclick or not callee:
        return None
    low = onclick.lower()
    nf = callee.lower()
    suf = "("
    pos = 0
    while True:
        ix = low.find(nf + suf, pos)
        if ix < 0:
            return None
        open_paren = ix + len(nf)
        close = _matching_close_paren(onclick, open_paren)
        if close is not None:
            return onclick[open_paren + 1 : close]
        pos = ix + len(nf) + 1


def _split_js_top_level_commas(inner: str) -> list[str]:
    """Rough split of JS call argument list respecting quotes and nested parentheses."""

    inner_st = inner.strip()
    if not inner_st:
        return []
    parts: list[str] = []
    buf: list[str] = []
    bs = chr(92)
    quote: Optional[str] = None
    depth_paren = 0
    i = 0
    length = len(inner_st)

    def flush_buf() -> None:
        txt = "".join(buf).strip()
        buf.clear()
        if txt:
            parts.append(txt)

    while i < length:
        ch = inner_st[i]
        if quote:
            buf.append(ch)
            if ch == bs and i + 1 < length:
                buf.append(inner_st[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if quote is None and ch == "(":
            depth_paren += 1
            buf.append(ch)
            i += 1
            continue
        if quote is None and ch == ")" and depth_paren > 0:
            depth_paren -= 1
            buf.append(ch)
            i += 1
            continue
        if quote is None and ch == "," and depth_paren == 0:
            flush_buf()
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush_buf()
    return parts


def _string_args_raw_for_invoke(callee: str, onclick: str) -> tuple[Optional[list[str]], str]:
    """
    Produce string arguments for callable window.<callee>.

    Prefer quoted JS string literals converted to UTF-8; fall back to entire trimmed token.
    """

    inner = _extract_call_inner_content(onclick, callee)
    if inner is None:
        return None, ""

    chunks = _split_js_top_level_commas(inner)
    if not chunks:
        return [], inner

    out: list[str] = []
    for ch in chunks:
        sq = _strip_js_quotes(ch)
        out.append(ch if sq is None else sq)
    return out, inner


_OTHER_POPUP_HEAD_RE = re.compile(r"\b(popup_[a-z0-9_]+)\s*\(", re.I)


def classify_popup_invoke(onclick: str) -> tuple[str, str, Optional[list[str]]]:
    """Return popup_type (domain string), javascript handler key, invoke args list or None."""

    oc = onclick or ""

    parsed_def = _parse_popup_definition_args(oc)
    if parsed_def:
        d_id, t = parsed_def
        return ("definition", "popup_definition", [d_id, t])

    cit_head = bool(_POPUP_CITATION_HEAD_RE.search(oc))
    if cit_head:
        args, _ = _string_args_raw_for_invoke("popup_citation", oc)
        if args is None:
            return ("citation", "popup_citation", None)
        return ("citation", "popup_citation", args)

    foot_head = bool(_POPUP_FOOTNOTE_HEAD_RE.search(oc))
    if foot_head:
        args, _ = _string_args_raw_for_invoke("popup_footnote", oc)
        if args is None:
            return ("footnote", "popup_footnote", None)
        return ("footnote", "popup_footnote", args)

    _CTX_CALLEE_EXACT = (
        "popup_context",
        "popup_mcm_context",
    )
    for callee in _CTX_CALLEE_EXACT:
        parsed_args, _inner = _string_args_raw_for_invoke(callee, oc)
        if parsed_args is not None:
            return ("context", callee, parsed_args)

    for mx in re.finditer(r"\b(popup_[a-z0-9_]*context[a-z0-9_]*)\s*\(", oc, flags=re.I):
        callee2 = mx.group(1)
        if callee2.lower() in {"popup_definition", "popup_citation", "popup_footnote"}:
            continue
        parsed_args2, _i2 = _string_args_raw_for_invoke(callee2, oc)
        if parsed_args2 is not None:
            return ("context", callee2, parsed_args2)

    for ox in _OTHER_POPUP_HEAD_RE.finditer(oc):
        callee_o = ox.group(1)
        lk = callee_o.lower()
        if lk in {"popup_definition", "popup_citation", "popup_footnote"}:
            continue
        parsed_args_o, _i3 = _string_args_raw_for_invoke(callee_o, oc)
        if parsed_args_o is not None:
            return ("other_popup", callee_o, parsed_args_o)
        return ("other_popup", callee_o, None)

    return ("other_popup", "", None)


def _popup_arg_identity_for_queue(handler: str, invoke_args: Optional[list[str]]) -> str:
    if invoke_args:
        return "::".join(str(a).strip().lower()[:320] for a in invoke_args)
    return ""



def _enqueue_dedupe_key(
    handler: str,
    arg_identity: str,
    trigger_title_hint: str,
    parent_popup_id: Optional[str],
    depth: int,
) -> str:
    bits = "|".join(
        (
            (handler or "").strip().lower()[:160],
            (arg_identity or "")[:780],
            _normalize_text_for_hash(trigger_title_hint)[:260],
            parent_popup_id or "",
            str(int(depth)),
        ),
    )
    return bits


def _invoke_window_popup_fn(root: ContentRoot, fn_key: str, parts: Optional[list[str]]) -> tuple[bool, str]:
    plist = parts if parts is not None else []
    if not fn_key:
        return False, "missing_fn_key"
    try:
        root.evaluate(
            """ ({ fnKey, parts }) => {
              try {
                const f = window[fnKey];
                if (typeof f !== 'function') {
                  throw new Error('popup_fn_missing_'+fnKey);
                }
                f.apply(window, parts);
              } catch (e) {
                throw new Error(String(e && e.stack || e));
              }
            } """,
            {"fnKey": fn_key, "parts": plist},
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


_POP_HINT_RE = re.compile(
    r"javascript:|void\s*\(|glossary|definition|popover|tooltip|dlg|dialog|modal|popup|hint|tool\s*tips|(?:^|[/?#])(mcm|context)\b|\bpopover\b|\btooltip\b",
    re.I,
)


def _sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _normalize_text_for_hash(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _normalize_title_slug(title: str) -> str:
    t = _normalize_text_for_hash(title)
    t = re.sub(r"^definition\s*-\s*", "", t)
    t = re.sub(r"[^a-z0-9]+", "_", t)
    t = t.strip("_")
    return t[:120] or "definition"


def _trigger_fingerprint(trigger: dict[str, Any]) -> str:
    parts = "|".join(
        (
            _normalize_text_for_hash(trigger.get("text") or ""),
            _normalize_text_for_hash((trigger.get("href") or "")[:400]),
            _normalize_text_for_hash((trigger.get("onclick") or "")[:400]),
            _normalize_text_for_hash((trigger.get("outer_html") or trigger.get("outerHTML") or "")[:800]),
        ),
    )


    return hashlib.sha1(parts.encode("utf-8", errors="replace")).hexdigest()


def _popup_merge_key(popup_type: str, normalized_title: str, text_hash: str) -> str:
    return f"{popup_type}::{normalized_title}::{text_hash}"


def _def_merge_key(normalized_title: str, text_hash: str) -> str:
    return _popup_merge_key("definition", normalized_title, text_hash)


def _is_noise_trigger(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return True
    for rp in _NOISE_PATTERNS:
        try:
            if rp.search(t):
                return True
        except Exception:
            continue
    return False


def _parse_popup_definition_args(onclick: str) -> Optional[tuple[str, str]]:
    if not onclick:
        return None
    m = _POPUP_DEFINITION_ARGS_RE.search(onclick)
    if not m:
        return None
    return m.group("id"), (m.group("title") or "")


def _is_disallowed_anchor_text(
    txt: str,
    *,
    has_popup_definition_onclick: bool,
    relax_for_popup_refs: bool = False,
) -> bool:
    """Reject numeric-only / single-letter link text unless this is an explicit glossary or popup-ish onclick."""

    if has_popup_definition_onclick or relax_for_popup_refs:
        return False
    t = (txt or "").strip()
    if not t:
        return False
    if len(t) <= 3 and re.fullmatch(r"[0-9]+", t):
        return True
    if re.fullmatch(r"[A-Za-z]", t):
        return True
    if re.fullmatch(r"[0-9]+(?:\s*[-–,]\s*[0-9]+)?", t) and len(t) <= 8:
        return True
    return False


def _should_skip_navigation_href(href: Optional[str]) -> tuple[bool, str]:
    """
    Prefer in-page / script-driven definitions; skip likely real navigations.
    """
    if not href:
        return False, ""
    h = href.strip()
    low = h.lower()
    if low.startswith("mailto:"):
        return True, "mailto"
    if low.startswith("tel:"):
        return True, "tel"
    if low.startswith("http:") or low.startswith("https:"):
        return True, "absolute_http_https"
    if low.endswith(".pdf") or low.endswith(".doc") or low.endswith(".htm") and "javascript:" not in low:
        # allow relative .htm? might navigate away - heuristic: skip obvious pages
        if re.match(r"^https?:", low):
            return True, "http_like"
        if re.match(r"^/|\\", low) and "." in low and "javascript:" not in low:
            # keep conservative: *.htm anchors without js might navigate
            pass
        if low.startswith("javascript:"):
            return False, ""
        if re.match(r"^[^:?#]+\.(?:htm|html|asp|aspx|php)(\?|$|#)", low):
            return True, "document_like_href"
    return False, ""


def _trigger_is_suspicious(tag: str, href: Optional[str], onclick: Optional[str], cls: Optional[str], tid: Optional[str], title: Optional[str], data_attrs: dict[str, str]) -> tuple[bool, str]:
    onclick_s = onclick or ""
    href_s = href or ""
    cls_s = cls or ""
    tid_s = tid or ""
    title_s = title or ""

    blob = f"{onclick_s} {href_s} {cls_s} {tid_s} {title_s} ".lower()
    for k, v in (data_attrs or {}).items():
        blob += f" {k}:{v}"

    oc_low = onclick_s.lower()

    # exclude obvious structural expand loops (handled elsewhere, but still)
    if re.search(r"\bexpand\s+all\b|\bcollapse\s+all\b", (title_s + " ").lower()):
        return False, ""

    reason = ""

    # Strong signals
    if onclick_s.strip():
        if "expandsection" not in oc_low and not re.search(r"expand\s*criteria|expand\s*footnote", oc_low):
            if _POP_HINT_RE.search(onclick_s) or "void(" in onclick_s.lower() or "return false" in oc_low:
                reason = "onclick_popup_like"
                return True, reason

    if href_s.strip() and _POP_HINT_RE.search(href_s):
        return True, "href_popup_like"

    if _POP_HINT_RE.search(blob):
        return True, "attribute_blob_popup_like"

    # weak: clinical-looking anchor tags with short text and javascript-ish behavior (but no onclick)
    if tag == "a" and href_s.strip().startswith("#"):
        txt_bits = "".join(blob)
        if "context" in txt_bits or "glossary" in txt_bits:
            return True, "fragment_context_like"

    return False, ""


_TRIGGERS_JS = r"""
({rootSel, maxN}) => {
  function visible(el){
    try {
      if (!el || !(el.nodeType === 1)) return false;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity||'1') === 0) return false;
      const r = el.getClientRects();
      return !!(r && r.length > 0);
    } catch (e) {
      return false;
    }
  }

  function buildXPath(el){
    try {
      if (!el || el.nodeType !== 1) return '';
      const segs = [];
      let cur = el;
      for (let depth = 0; depth < 40 && cur && cur.nodeType === 1; depth++) {
        const nm = cur.nodeName.toLowerCase();
        if (nm === 'html') { segs.unshift('html'); break; }
        let ix = 1;
        let sib = cur.previousSibling;
        while (sib) {
          if (sib.nodeType === 1 && sib.nodeName === cur.nodeName) ix++;
          sib = sib.previousSibling;
        }
        segs.unshift(nm + '[' + ix + ']');
        cur = cur.parentElement;
        if (!cur) break;
      }
      return '/' + segs.join('/');
    } catch (e) {
      return '';
    }
  }

  const root = rootSel ? document.querySelector(rootSel) : document.body;
  if (!root) return [];

  const nodes = [];
  nodes.push(...root.querySelectorAll('a'));
  nodes.push(...root.querySelectorAll('[role="link"]'));
  nodes.push(...root.querySelectorAll('span[onclick], font[onclick], button[onclick], td[onclick]'));

  const out = [];

  outer: for (const el of nodes) {
    if (!visible(el)) continue;
    let text = '';
    try { text = (el.innerText || '').replace(/\s+/g,' ').trim(); } catch (e) { text = ''; }
    let href = el.getAttribute('href') || '';
    let onclick = el.getAttribute('onclick') || '';
    const cls = el.getAttribute('class') || '';
    const id = el.getAttribute('id') || '';
    let title = el.getAttribute('title') || el.getAttribute('aria-label') || '';
    let tag = (el.tagName || '').toLowerCase();
    if (tag === '') tag = '?';

    const data = {};
    try {
      for (const attr of el.attributes || []) {
        const n = attr.name || '';
        if (n && n.startsWith('data-')) data[n] = attr.value || '';
      }
    } catch (e) {}

    try {
      if (onclick && /\b(?:expand\s*section|collapse)\b/i.test(onclick)) {
        continue;
      }
    } catch (e) {}

    out.push({
      xpath: buildXPath(el),
      text,
      tag,
      href,
      onclick,
      className: cls,
      idName: id,
      titleAria: title,
      data_attrs: data,
      outerHTML: String(el.outerHTML || '').slice(0, 1200),
      page_section_hint: '',
      source_text_context: '',
    });
    if (out.length >= maxN) break outer;
  }
  return out;
}
"""


_PROBE_POPUP_JS = r"""
() => {
  function visible(el){
    try {
      if (!el || !(el.nodeType === 1)) return false;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity||'1') === 0) return false;
      const r = el.getClientRects();
      return !!(r && r.length > 0);
    } catch (e) {
      return false;
    }
  }

  function z(el){
    try {
      const v = parseInt(window.getComputedStyle(el).zIndex || '0', 10);
      return Number.isFinite(v) ? v : 0;
    } catch (e) {
      return 0;
    }
  }

  const dialogs = Array.from(document.querySelectorAll('[role="dialog"],[aria-modal="true"]')).filter(visible);
  if (dialogs.length) {
    const d = dialogs[dialogs.length - 1];
    return { kind: 'dialog', outerHTML: d.outerHTML, text: String(d.innerText || ''), elemKey: '__last_dialog__'};
  }

  function overlayLikely(txt){
    const t = String(txt || '');
    if (t.length < 24) return false;
    const defLike = /definition\s*-|^definition\b/i.test(t);
    const citLike = /\bcitations?\b|^\s*[(\[]?\s*[0-9]{1,3}\s*[)\]]?[.\s]|^\(?\s*\d{1,3}\s*\)?\.\s|^[\[{]\s*[A-Za-z]{1,3}\s*[}\]]/im.test(t);
    const ftLike = /\bfootnotes?\b|^\s*\[\s*[A-Za-z]\s*\]/im.test(t);
    const cxLike = /in\s+context|context\s+link/i.test(t);
    return defLike || citLike || ftLike || cxLike;
  }

  const fixed = Array.from(document.querySelectorAll('body div')).filter((d) => {
    if (!visible(d)) return false;
    const st = window.getComputedStyle(d);
    const pos = st.position;
    if (!(pos === 'fixed' || pos === 'absolute')) return false;
    const t = String(d.innerText || '');
    if (!overlayLikely(t)) return false;
    if (t.length < 36) return false;
    return true;
  });

  fixed.sort((a,b) => z(b) - z(a));
  if (fixed.length) {
    const d = fixed[0];
    return { kind: 'fixed_overlay', outerHTML: d.outerHTML, text: String(d.innerText || ''), elemKey: '__fixed_pick__'};
  }

  const anyTxt = Array.from(document.querySelectorAll('body div,body section,article')).filter((d) => visible(d)).filter((d) => overlayLikely(String(d.innerText||''))).sort((a,b) => String(b.innerText||'').length - String(a.innerText||'').length);
  if (anyTxt.length) {
    const d = anyTxt[0];
    return { kind: 'text_blob', outerHTML: d.outerHTML, text: String(d.innerText || ''), elemKey: '__text_pick__'};
  }

  return null;
}
"""


_GATHER_PT_ORDER = {"definition": 0, "citation": 1, "footnote": 2, "context": 3, "other_popup": 7}


def _gather_sort_tuple(row: dict[str, Any]) -> tuple[int, str]:
    oc = str(row.get("onclick") or "")
    pt, _, _ = classify_popup_invoke(oc)
    return (_GATHER_PT_ORDER.get(pt, 30), str(row.get("text") or "").lower())


def _gather_trigger_candidates(
    root: ContentRoot,
    *,
    root_sel: Optional[str],
    max_gather: int,
    inside_popup: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stats: dict[str, Any] = {"inside_popup": int(bool(inside_popup))}
    try:
        raw = root.evaluate(_TRIGGERS_JS, {"rootSel": root_sel or "", "maxN": int(max_gather)})
    except Exception:
        return [], stats
    if not isinstance(raw, list):
        return [], stats

    refined: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue

        onclick = row.get("onclick")
        oc_raw = onclick if isinstance(onclick, str) else ""
        txt = str(row.get("text") or "").strip()
        if not txt:
            continue

        has_popup_def = bool(_POPUP_DEFINITION_HEAD_RE.search(oc_raw))
        _pt, handler_key, _args = classify_popup_invoke(oc_raw)
        relax_refs = bool(inside_popup or (handler_key or "").strip())

        if not inside_popup and not relax_refs and _is_noise_trigger(txt):
            continue
        if _is_disallowed_anchor_text(
            txt,
            has_popup_definition_onclick=has_popup_def,
            relax_for_popup_refs=relax_refs,
        ):
            continue

        tag = str(row.get("tag") or "")
        href = row.get("href")

        cls = str(row.get("className") or "")
        elem_id = str(row.get("idName") or "")
        title_ar = str(row.get("titleAria") or "")
        da = row.get("data_attrs")
        data_attrs = da if isinstance(da, dict) else {}

        skip_nav, _skip_why = _should_skip_navigation_href(href if isinstance(href, str) else "")
        if skip_nav:
            continue

        if (handler_key or "").strip():
            ok, why = True, "known_popup_handler"
        elif has_popup_def:
            ok, why = True, "onclick_popup_definition"
        else:
            ok, why = _trigger_is_suspicious(
                tag,
                href if isinstance(href, str) else None,
                onclick if isinstance(onclick, str) else None,
                cls,
                elem_id,
                title_ar,
                data_attrs,
            )

        oc = oc_raw
        if not ok:
            if isinstance(href, str) and href.strip().lower().startswith("javascript:"):
                ok, why = True, "href_javascript_always"
            elif tag == "a" and oc.strip():
                ok, why = True, "anchor_with_onclick"
            elif tag == "a" and (title_ar or "").strip():
                txt_l = txt.lower()
                if len(txt) <= 80 and txt_l and "definition" not in txt_l:
                    maybe = False
                    for hint in [
                        "hemodynamic",
                        "hypotension",
                        "tachycardia",
                        "orthostatic",
                        "altered mental",
                        "shock index",
                        "mean arterial",
                        "map ",
                        "map calculator",
                        "lactate",
                        "vasopressor",
                        "inotropic",
                    ]:
                        if hint in txt_l:
                            maybe = True
                            break
                    if maybe and _POP_HINT_RE.search(title_ar.lower()):
                        ok, why = True, "clinical_anchor_title_hint"

        if not ok:
            continue

        xp = str(row.get("xpath") or "")
        if not xp.startswith("/"):
            continue

        section_hint = ""
        try:
            section_hint = str(
                root.evaluate(
                    """(xp) => {
              function visible(el){ try{
                const cs = window.getComputedStyle(el); if(cs.display==='none'||cs.visibility==='hidden') return false;
                const r = el.getClientRects(); return !!(r&&r.length);
              }catch(e){ return false;}}

              try {
                const res = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                const el = res.singleNodeValue;
                if (!(el instanceof Element)) return '';
                let p = el;
                for (let i=0;i<12;i++) {
                  if (!p) break;
                  const t = ((p.innerText||'').split('\n')[0]||'').trim();
                  const looks = /\\bAdmission\\b|\\bClinical Indications\\b|\\bEvidence\\b|\\bDischarge\\b/i.test((p.innerText||''));
                  if (looks && t.length < 240) return t.slice(0, 240);
                  p = p.parentElement;
                }
                return '';
              } catch(e) {
                return '';
              }
            }""",
                    xp,
                ),
            )
        except Exception:
            section_hint = ""

        ctx = txt[:520]
        refined.append(
            {
                **row,
                "page_section_hint": section_hint[:240],
                "source_text_context": ctx,
                "_suspicious_reason": why,
                "_planned_popup_type": _pt,
            },
        )

    refined.sort(key=_gather_sort_tuple)
    return refined[:max_gather], stats


def _probe_popup(root: ContentRoot) -> Optional[dict[str, Any]]:
    try:
        v = root.evaluate(_PROBE_POPUP_JS)
        if isinstance(v, dict):
            return v
    except Exception:
        return None
    return None


def _popup_is_valid(popup: Optional[dict[str, Any]]) -> bool:
    if not popup or not isinstance(popup.get("outerHTML"), str):
        return False
    return bool(str(popup.get("outerHTML") or "").strip())


def _probe_popup_best(dom: ContentRoot, page: Page) -> Optional[dict[str, Any]]:
    p = _probe_popup(dom)
    if _popup_is_valid(p):
        return p
    if dom is not page:
        p2 = _probe_popup(page)
        if _popup_is_valid(p2):
            return p2
    return None


def _wait_and_probe_popup(dom: ContentRoot, page: Page, budget_ms: int) -> Optional[dict[str, Any]]:
    t_end = time.time() + (max(budget_ms, 250) / 1000.0)
    try:
        page.wait_for_timeout(120)
    except Exception:
        pass
    popup: Optional[dict[str, Any]] = None
    while time.time() < t_end:
        try:
            popup = _probe_popup_best(dom, page)
        except Exception:
            popup = None
        if _popup_is_valid(popup):
            break
        try:
            page.wait_for_timeout(90)
        except Exception:
            time.sleep(0.09)
    return popup if _popup_is_valid(popup) else None


def _close_popup(dom: ContentRoot, page: Page, log: ProgressLogger) -> None:
    # Try common chrome within dialog first
    close_selectors = [
        '[role="dialog"] [aria-label*="close" i]',
        '[aria-modal="true"] [aria-label*="close" i]',
        '[role="dialog"] button[class*="close" i]',
        '[role="dialog"] .ui-dialog-titlebar-close',
        '[aria-modal="true"] img[alt*="close" i]',
        '[role="dialog"] button:has-text("×")',
        '[aria-modal="true"] button:has-text("×")',
        'button[title*="Close" i]',
        'button:has-text("Close")',
    ]
    roots: list[ContentRoot] = [dom]
    if dom is not page:
        roots.append(page)
    for sel in close_selectors:
        for root in roots:
            loc = root.locator(sel)
            try:
                n = loc.count()
                if not n:
                    continue
                for i in range(n - 1, -1, -1):
                    el = loc.nth(i)
                    try:
                        if el.is_visible(timeout=120):
                            el.click(timeout=1500)
                            page.wait_for_timeout(120)
                    except Exception:
                        continue
            except Exception:
                continue

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(140)
    except Exception:
        pass

    still = _probe_popup_best(dom, page)
    if _popup_is_valid(still):
        log.warn("Popup appears still present after close attempts")
    return


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


_GRAB_POPUP_ELEMENT_JS = r"""
() => {
  function visible(el){
    try {
      if (!el || !(el.nodeType === 1)) return false;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity||'1') === 0) return false;
      const r = el.getClientRects();
      return !!(r && r.length > 0);
    } catch (e) {
      return false;
    }
  }

  function z(el){
    try {
      const v = parseInt(window.getComputedStyle(el).zIndex || '0', 10);
      return Number.isFinite(v) ? v : 0;
    } catch (e) {
      return 0;
    }
  }

  const dialogs = Array.from(document.querySelectorAll('[role="dialog"],[aria-modal="true"]')).filter(visible);
  if (dialogs.length) return dialogs[dialogs.length - 1];

  function overlayLikely(txt){
    const t = String(txt || '');
    if (t.length < 20) return false;
    const defLike = /definition\s*-|^definition\b/i.test(t);
    const citLike = /\bcitations?\b|^\s*[(\[]?\s*[0-9]{1,3}\s*[)\]]?[.\s]|^\(?\s*\d{1,3}\s*\)?\.\s|^[\[{]\s*[A-Za-z]{1,3}\s*[}\]]/im.test(t);
    const ftLike = /\bfootnotes?\b|^\s*\[\s*[A-Za-z]\s*\]/im.test(t);
    const cxLike = /in\s+context|context\s+link/i.test(t);
    return defLike || citLike || ftLike || cxLike;
  }

  const fixed = Array.from(document.querySelectorAll('body div')).filter((d) => {
    if (!visible(d)) return false;
    const st = window.getComputedStyle(d);
    const pos = st.position;
    if (!(pos === 'fixed' || pos === 'absolute')) return false;
    const t = String(d.innerText || '');
    return overlayLikely(t) && t.length >= 32;
  });
  fixed.sort((a,b) => z(b) - z(a));
  if (fixed.length) return fixed[0];

  const blobs = Array.from(document.querySelectorAll('body div,body section')).filter((d) => visible(d)).filter((d) => overlayLikely(String(d.innerText||'')));
  blobs.sort((a,b) => String(b.innerText||'').length - String(a.innerText||'').length);
  if (blobs.length) return blobs[0];

  return null;
}
"""


def _popup_element_handle(dom: ContentRoot, page: Page) -> Optional[ElementHandle]:
    def _try_root(r: ContentRoot) -> Optional[ElementHandle]:
        h = None
        try:
            h = r.evaluate_handle(_GRAB_POPUP_ELEMENT_JS)
            el = h.as_element()
            if el is None and h:
                try:
                    h.dispose()
                except Exception:
                    pass
            return el
        except Exception:
            if h:
                try:
                    h.dispose()
                except Exception:
                    pass
            return None

    el = _try_root(dom)
    if el is not None:
        return el
    if dom is not page:
        return _try_root(page)
    return None


def _extract_popup_title(popup_inner_text: str, popup_type: str) -> str:
    if not popup_inner_text:
        return ""

    lines = [" ".join((ln or "").split()) for ln in popup_inner_text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]

    if popup_type == "definition":
        rx = re.compile(r"^\s*Definition\b", re.I)
        for ln in lines[:12]:
            if rx.match(ln):
                return ln[:400]
        for ln in lines[:12]:
            if len(ln) >= 12 and ":" in ln and len(ln) <= 200:
                return ln[:400]
        if lines:
            return lines[0][:400]
        head = popup_inner_text.strip().split("\n")[0].strip()
        return head[:400] if head else "Definition"

    if popup_type in ("citation", "footnote"):
        for ln in lines[:8]:
            if re.search(r"\d|[A-Za-z]", ln):
                return ln[:400]
        if lines:
            return lines[0][:400]

    if popup_type == "context":
        for ln in lines[:10]:
            low = ln.lower()
            if "context" in low or "link" in low:
                return ln[:400]
        if lines:
            return lines[0][:400]

    if lines:
        return lines[0][:400]
    head = popup_inner_text.strip().split("\n")[0].strip()
    return head[:400] if head else popup_type.replace("_", " ").title()


def _extract_definition_title(popup_inner_text: str) -> str:
    return _extract_popup_title(popup_inner_text, "definition")


@dataclass
class PopupCaptureRecord:
    popup_id: str
    popup_type: str
    title: str
    normalized_title: str
    text: str
    html: str
    text_hash: str
    depth: int
    parent_popup_id: Optional[str]
    root_trigger_text: str
    trigger_texts: list[str] = field(default_factory=list)
    trigger_sources: list[dict[str, Any]] = field(default_factory=list)
    nested_trigger_candidates: list[dict[str, Any]] = field(default_factory=list)
    captured_nested_popup_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _serialize_popup_record(rec: PopupCaptureRecord, *, compat_definition_id: bool) -> dict[str, Any]:
    base: dict[str, Any] = {
        "popup_id": rec.popup_id,
        "popup_type": rec.popup_type,
        "title": rec.title,
        "normalized_title": rec.normalized_title,
        "text": rec.text,
        "html": rec.html,
        "text_hash": rec.text_hash,
        "depth": rec.depth,
        "parent_popup_id": rec.parent_popup_id,
        "root_trigger_text": rec.root_trigger_text,
        "trigger_texts": sorted(set(rec.trigger_texts)),
        "trigger_sources": rec.trigger_sources[-50:],
        "nested_trigger_candidates": rec.nested_trigger_candidates[:240],
        "captured_nested_popup_ids": sorted(set(rec.captured_nested_popup_ids))[:240],
        "warnings": rec.warnings,
    }
    if compat_definition_id and rec.popup_type == "definition":
        base["definition_id"] = rec.popup_id
    return base


def run_definition_capture(
    *,
    page: Page,
    content_frame: Optional[Frame] = None,
    mcg_code: str,
    mcg_title: str,
    source_url: str,
    out_prefix: str,
    raw_html_dir: Path,
    audits_dir: Path,
    project_root: Path,
    log: ProgressLogger,
    progress: dict[str, Any],
    started_mono: float,
    max_triggers: int,
    recursion_depth: int,
    click_timeout_ms: int,
    warnings_accum: Optional[list[str]] = None,
    terminal_progress: Optional[CaptureTerminalReporter] = None,
) -> dict[str, Any]:
    dom: ContentRoot = content_frame if content_frame is not None else page
    popups_by_key: dict[str, PopupCaptureRecord] = {}
    records_by_id: dict[str, PopupCaptureRecord] = {}
    dup_merge_count = 0
    nested_triggers_discovered_total = 0
    nested_enqueued_total = 0

    defs_json_path = raw_html_dir / f"{out_prefix}.definitions.raw.json"
    defs_html_path = raw_html_dir / f"{out_prefix}.definitions.raw.html"
    triggers_jsonl = raw_html_dir / f"{out_prefix}.definition-triggers.jsonl"
    failures_jsonl = raw_html_dir / f"{out_prefix}.definition-failures.jsonl"

    popups_json_path = raw_html_dir / f"{out_prefix}.popups.raw.json"
    popups_html_path = raw_html_dir / f"{out_prefix}.popups.raw.html"
    popup_triggers_jsonl = raw_html_dir / f"{out_prefix}.popup-triggers.jsonl"
    popup_failures_jsonl = raw_html_dir / f"{out_prefix}.popup-failures.jsonl"

    audit_json_path = audits_dir / f"{out_prefix}.definition-capture.audit.json"
    audit_md_path = audits_dir / f"{out_prefix}.definition-capture.summary.md"

    for pth in (
        triggers_jsonl,
        failures_jsonl,
        popup_triggers_jsonl,
        popup_failures_jsonl,
    ):
        if pth.exists():
            pth.unlink()

    clicked_trigger_count = 0
    direct_invocation_count = 0
    popup_detected_count = 0
    failed_trigger_count = 0
    queued_trigger_candidates = 0
    queued_unique_enqueue_keys = 0
    skipped_enqueue_duplicate_count = 0
    skipped_duplicate_popup_count = 0

    fingerprints_clicked: set[str] = set()
    enqueue_keys_seen: set[str] = set()
    failures_audit: list[dict[str, Any]] = []

    popup_type_capture_counts: dict[str, int] = {
        "definition": 0,
        "citation": 0,
        "footnote": 0,
        "context": 0,
        "other_popup": 0,
    }

    progress["stage"] = "popup_capture"
    progress["last_action"] = "popup_gather"

    screenshot_fail_idx = 0
    attempt_seq = 0

    queue: deque[tuple[int, Optional[str], str, dict[str, Any]]] = deque()
    max_depth_reached = 0

    def write_pair_triggers(rec: dict[str, Any]) -> None:
        _append_jsonl(triggers_jsonl, rec)
        _append_jsonl(popup_triggers_jsonl, rec)

    def write_pair_failures(rec: dict[str, Any]) -> None:
        _append_jsonl(failures_jsonl, rec)
        _append_jsonl(popup_failures_jsonl, rec)

    def enqueue(depth: int, parent_popup_id_val: Optional[str], root_tt: str, cand: dict[str, Any]) -> None:
        nonlocal queued_trigger_candidates, queued_unique_enqueue_keys, skipped_enqueue_duplicate_count, nested_enqueued_total
        oc_raw_l = cand.get("onclick")
        oc_full_l = oc_raw_l if isinstance(oc_raw_l, str) else ""

        pt_l, hk_l, invoke_args_l = classify_popup_invoke(oc_full_l)

        trig_hint = str(cand.get("text") or "").strip()[:520]
        arg_identity = _popup_arg_identity_for_queue(hk_l, invoke_args_l)
        if not arg_identity.strip():
            arg_identity = _sha256_utf8(oc_full_l[:520])[:20]

        edk = _enqueue_dedupe_key(
            hk_l or "click",
            arg_identity,
            trig_hint,
            parent_popup_id_val,
            depth,
        )
        if edk in enqueue_keys_seen:
            skipped_enqueue_duplicate_count += 1
            log.info(
                "popup_queue_skip_duplicate",
                {"depth": depth, "enqueueKeyTrunc": edk[:160], "handler": hk_l, "hint": trig_hint[:120]},
            )
            return
        if len(queue) >= max_triggers * 24:
            return

        enqueue_keys_seen.add(edk)
        queued_unique_enqueue_keys += 1
        queued_trigger_candidates += 1
        rt = root_tt.strip() if root_tt else "(page)"
        if depth > 0:

            nested_enqueued_total += 1

        queue.append((depth, parent_popup_id_val, rt, cand))
        log.info(
            "popup_queue_enqueue",
            {
                "depth": depth,
                "parent_popup_id": parent_popup_id_val,
                "plannedType": pt_l,
                "handler": hk_l,
                "hint": trig_hint[:140],
                "enqueueKeyTrunc": edk[:200],
            },
        )

    seeds, gs0 = _gather_trigger_candidates(
        dom,
        root_sel=None,
        max_gather=min(1200, max(max_triggers * 6, 400)),
        inside_popup=False,
    )
    candidate_trigger_count = len(seeds)
    popup_definition_trigger_count = sum(
        1 for s in seeds if _parse_popup_definition_args(str(s.get("onclick") or ""))
    )
    citation_seed_hints = sum(1 for s in seeds if _POPUP_CITATION_HEAD_RE.search(str(s.get("onclick") or "")))
    foot_seed_hints = sum(1 for s in seeds if _POPUP_FOOTNOTE_HEAD_RE.search(str(s.get("onclick") or "")))

    for sd in seeds:
        rtt = str(sd.get("text") or "").strip() or "(page)"
        enqueue(0, None, rtt, sd)

    log.step(
        "Popup capture queued triggers",
        {
            "queueSize": len(queue),
            "seedCount": len(seeds),
            "candidateTriggerCount": candidate_trigger_count,
            "popupDefinitionTriggerCount": popup_definition_trigger_count,
            "gatherStats": gs0,
            "citationOnclickTriggersSeed": citation_seed_hints,
            "footnoteOnclickTriggersSeed": foot_seed_hints,
        },
    )

    trig_cap = int(max_triggers)
    popped_budget = trig_cap
    skipped_repeat_fingerprint = 0

    def _def_ui_tick(consumed: int, force: bool = False) -> None:
        if terminal_progress is None:
            return
        defs_n = sum(1 for r in popups_by_key.values() if r.popup_type == "definition")
        terminal_progress.definition_tick(
            triggers_done=consumed,
            max_triggers=trig_cap,
            popup_count=len(popups_by_key),
            definition_count=defs_n,
            failed=failed_trigger_count,
            skipped=skipped_enqueue_duplicate_count + skipped_repeat_fingerprint,
            force=force,
        )

    if terminal_progress is not None:
        terminal_progress.phase("popup_queue_processing", f"budget={trig_cap} queued={len(queue)}")

    while queue and popped_budget > 0:
        popped_budget -= 1
        consumed = trig_cap - popped_budget

        depth, parent_popup_id_val, root_tt_chain, cand = queue.popleft()
        fp = _trigger_fingerprint(cand)

        if fp in fingerprints_clicked:
            skipped_repeat_fingerprint += 1
            _def_ui_tick(consumed)
            continue

        attempt_seq += 1
        max_depth_reached = max(max_depth_reached, depth)

        xp = str(cand.get("xpath") or "").strip()

        trig_text = str(cand.get("text") or "").strip()
        oc_full = cand.get("onclick")
        oc_full_s = oc_full if isinstance(oc_full, str) else ""

        planned_type, handler_key, invoke_args = classify_popup_invoke(oc_full_s)

        progress["last_action"] = f"popup_attempt_{attempt_seq}"

        trig_attrs: dict[str, Any] = {
            "tag": cand.get("tag"),
            "href": cand.get("href"),
            "onclick_truncated": (oc_full_s[:240] + "…") if len(oc_full_s) > 240 else oc_full_s,
            "class": cand.get("className"),
            "id": cand.get("idName"),
            "title_or_aria": cand.get("titleAria"),
            "data_attrs": cand.get("data_attrs") or {},
        }

        trig_jsonl_record: dict[str, Any] = {
            "mcg_code": mcg_code,
            "attempt": attempt_seq,
            "depth": depth,
            "parent_popup_id": parent_popup_id_val,
            "root_trigger_text": root_tt_chain[:400],
            "planned_popup_type": planned_type,
            "handler_key": handler_key,
            "url": "",
            "trigger_text": trig_text[:400],
            "trigger_attrs": trig_attrs,
            "page_section_hint": cand.get("page_section_hint"),
            "suspicion_reason": cand.get("_suspicious_reason"),
            "direct_js_error": "",
        }

        popup: Optional[dict[str, Any]] = None

        used_direct_success = False
        did_click = False

        if handler_key.strip() and invoke_args is not None:
            ok_js, js_err = _invoke_window_popup_fn(dom, handler_key, invoke_args)
            if js_err:
                trig_jsonl_record["direct_js_error"] = js_err[:800]
            if ok_js:
                try:
                    page.wait_for_timeout(int(500 + random.random() * 500))
                except Exception:
                    time.sleep(0.625)
                popup = _wait_and_probe_popup(dom, page, click_timeout_ms)
                if _popup_is_valid(popup):
                    used_direct_success = True
                    trig_jsonl_record["opening_method"] = "direct_window_invoke"

        if not _popup_is_valid(popup):
            if not xp:

                fingerprints_clicked.add(fp)
                failed_trigger_count += 1
                trig_jsonl_record["result"] = "missing_xpath"
                trig_jsonl_record["opening_method"] = "none"
                write_pair_triggers(trig_jsonl_record)
                fw = {
                    "trigger_text": trig_text,
                    "xpath": xp or "",
                    "error": "missing_xpath_no_direct_invoke_possible",
                    "planned_popup_type": planned_type,
                }
                write_pair_failures(fw)
                _def_ui_tick(consumed)
                continue

            locator: Optional[Locator] = None
            try:
                locator = locator_for_xpath(dom, xp)

                locator.scroll_into_view_if_needed(timeout=3000)
            except Exception as e:

                fingerprints_clicked.add(fp)

                failed_trigger_count += 1

                trig_jsonl_record["result"] = "skip_locator_scroll_failed"

                write_pair_triggers(trig_jsonl_record)

                fw = {"trigger_text": trig_text, "xpath": xp, "error": str(e)}
                write_pair_failures(fw)
                _def_ui_tick(consumed)
                continue

            try:
                assert locator is not None

                locator.click(timeout=max(click_timeout_ms, 1000))
            except TimeoutError:
                fingerprints_clicked.add(fp)

                failed_trigger_count += 1

                trig_jsonl_record["result"] = "timeout_click_or_obscured"

                write_pair_triggers(trig_jsonl_record)

                fw = {"trigger_text": trig_text, "xpath": xp, "error": "click_timeout_obscured"}

                write_pair_failures(fw)

                screenshot_fail_idx += 1

                try:
                    p = audits_dir / f"{out_prefix}.definition-click-fail-{screenshot_fail_idx}.png"
                    page.screenshot(path=str(p))
                except Exception:
                    pass
                _def_ui_tick(consumed)
                continue
            except Exception as e:

                fingerprints_clicked.add(fp)

                failed_trigger_count += 1

                trig_jsonl_record["result"] = "click_failed"

                write_pair_triggers(trig_jsonl_record)

                fw = {"trigger_text": trig_text, "xpath": xp, "error": str(e)}

                write_pair_failures(fw)

                screenshot_fail_idx += 1

                try:
                    p = audits_dir / f"{out_prefix}.definition-click-fail-{screenshot_fail_idx}.png"
                    page.screenshot(path=str(p))
                except Exception:
                    pass
                _def_ui_tick(consumed)
                continue

            did_click = True

            trig_jsonl_record["opening_method"] = "click"

            popup = _wait_and_probe_popup(dom, page, click_timeout_ms)

        if not _popup_is_valid(popup):

            fingerprints_clicked.add(fp)

            failed_trigger_count += 1

            trig_jsonl_record["result"] = "clicked_no_popup_detected" if did_click else "direct_no_popup_detected"

            screenshot_fail_idx += 1

            fail_path_png = audits_dir / f"{out_prefix}.definition-no-popup-{screenshot_fail_idx}.png"

            try:
                page.screenshot(path=str(fail_path_png))
            except Exception:
                fail_path_png = None

            try:

                trig_jsonl_record["url"] = page.url

            except Exception:

                trig_jsonl_record["url"] = ""

            write_pair_triggers(trig_jsonl_record)

            fw = {
                "trigger_text": trig_text,
                "xpath": xp,
                "error": "no_popup_detected",
                "planned_popup_type": planned_type,

                "screenshot": str(fail_path_png or ""),
            }

            write_pair_failures(fw)

            failures_audit.append(fw)

            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(80)
            except Exception:
                pass
            _def_ui_tick(consumed)
            continue

        fingerprints_clicked.add(fp)

        if used_direct_success:
            direct_invocation_count += 1
            trig_jsonl_record["result"] = "direct_window_invoke_ok"
        elif did_click:

            clicked_trigger_count += 1

            trig_jsonl_record["result"] = "clicked"

        else:
            trig_jsonl_record["result"] = "popup_detected_fallback"

        popup_detected_count += 1

        html_blob = str(popup.get("outerHTML") or "")

        popup_text_raw = str(popup.get("text") or "").strip()

        title = _extract_popup_title(popup_text_raw, planned_type)

        slug = _normalize_title_slug(title)
        if not slug.strip():

            slug = _normalize_title_slug(f"{planned_type}_{trig_text[:80]}")

        text_hash_src = popup_text_raw or html_blob
        th = _sha256_utf8(_normalize_text_for_hash(text_hash_src))
        merge_key = _popup_merge_key(planned_type, slug, th)

        basis_id = _sha256_utf8(f"{mcg_code}|{planned_type}|{slug}|{th}")[:12]
        provisional_popup_id = f"popup.{mcg_code}.{basis_id}"

        trig_src_record: dict[str, Any] = {
            "trigger_text": trig_text[:400],
            "trigger_html": str(cand.get("outer_html") or cand.get("outerHTML") or "")[:2400],
            "trigger_attributes": {**trig_attrs, "onclick": oc_full_s},
            "page_section_hint": str(cand.get("page_section_hint") or ""),
            "source_text_context": str(cand.get("source_text_context") or trig_text),
            "planned_popup_type": planned_type,
        }

        popup_handle = _popup_element_handle(dom, page)
        popup_root_selector = ""

        nested_cands_records: list[dict[str, Any]] = []

        if popup_handle:
            try:

                popup_handle.evaluate("node => node.setAttribute('data-mcg-def-root','1')")

                popup_root_selector = '[data-mcg-def-root="1"]'
            except Exception:
                popup_root_selector = ""

        inner_count_logged = 0

        try:
            if popup_root_selector:
                popup_inner_candidates, _inner_gs = _gather_trigger_candidates(
                    dom,
                    root_sel=popup_root_selector,
                    max_gather=280,
                    inside_popup=True,
                )
                inner_count_logged = len(popup_inner_candidates)
                nested_triggers_discovered_total += inner_count_logged
                log.info(
                    "popup_nested_triggers_discovered",

                    {"parentTitle": title[:120], "count": inner_count_logged, "depth": depth},
                )

            else:
                popup_inner_candidates = []
        except Exception:
            popup_inner_candidates = []

        for ic in popup_inner_candidates:

            ic_fp_in = _trigger_fingerprint(ic)
            ic_oc = ic.get("onclick")

            ic_oc_s = ic_oc if isinstance(ic_oc, str) else ""

            if ic_fp_in == fp:
                continue
            nested_txt = str(ic.get("text") or "").strip()
            nested_pt, nested_hk, nested_args = classify_popup_invoke(ic_oc_s)

            nested_cands_records.append(
                {

                    "text": nested_txt[:180],
                    "href": ic.get("href"),
                    "xpath": ic.get("xpath"),
                    "planned_popup_type": nested_pt,

                    "handler_key": nested_hk,
                    "onclick_tail": ic_oc_s[:240],
                    "invoke_preview": nested_args[:6] if isinstance(nested_args, list) else nested_args,

                },

            )


            if depth < recursion_depth and nested_txt:

                enqueue(depth + 1, provisional_popup_id, root_tt_chain, ic)


        is_new_popup = merge_key not in popups_by_key

        rec: PopupCaptureRecord
        current_popup_id: str

        if not is_new_popup:

            skipped_duplicate_popup_count += 1

            rec = popups_by_key[merge_key]
            dup_merge_count += 1
            log.info(
                "popup_duplicate_skipped",

                {"mergeKey": merge_key[:200], "title": title[:120], "plannedType": planned_type},
            )


            current_popup_id = rec.popup_id

            if trig_text and trig_text not in rec.trigger_texts:
                rec.trigger_texts.append(trig_text)


            rec.trigger_sources.append(trig_src_record)


            seen_nested_sig = {(x.get("text"), x.get("xpath")) for x in rec.nested_trigger_candidates}

            for nc in nested_cands_records[:260]:
                sig = (nc.get("text"), nc.get("xpath"))
                if sig not in seen_nested_sig:
                    seen_nested_sig.add(sig)
                    rec.nested_trigger_candidates.append(nc)


        else:
            warnings_rec: list[str] = []

            if popup.get("kind") == "text_blob":
                warnings_rec.append("popup_detected_via_text_heuristic_maybe_overbroad_outerHTML")

            current_popup_id = provisional_popup_id

            rec = PopupCaptureRecord(
                popup_id=current_popup_id,
                popup_type=planned_type,

                title=title or "(untitled popup)",


                normalized_title=slug,

                text=popup_text_raw,

                html=html_blob,

                text_hash=th,

                depth=depth,

                parent_popup_id=parent_popup_id_val,

                root_trigger_text=root_tt_chain,

                trigger_texts=[trig_text] if trig_text else [],

                trigger_sources=[trig_src_record],

                nested_trigger_candidates=nested_cands_records[:260],

                captured_nested_popup_ids=[],

                warnings=warnings_rec,
            )


            popups_by_key[merge_key] = rec

            records_by_id[current_popup_id] = rec

            popup_type_capture_counts[planned_type] = int(popup_type_capture_counts.get(planned_type, 0)) + 1

            if parent_popup_id_val:
                prec = records_by_id.get(parent_popup_id_val)
                if prec and current_popup_id not in prec.captured_nested_popup_ids:


                    prec.captured_nested_popup_ids.append(current_popup_id)

                    log.info(
                        "popup_nested_captured_linked",
                        {
                            "child": current_popup_id,
                            "parent": parent_popup_id_val,
                            "type": planned_type,
                        },

                    )


        trig_jsonl_record["popup_kind"] = popup.get("kind")

        trig_jsonl_record["planned_popup_type"] = planned_type

        trig_jsonl_record["popup_id_emit"] = current_popup_id[:80]

        trig_jsonl_record["merge_key_trunc"] = merge_key[:200]

        trig_jsonl_record["normalized_title"] = slug

        trig_jsonl_record["text_hash"] = th

        try:
            trig_jsonl_record["url"] = page.url
        except Exception:
            trig_jsonl_record["url"] = ""

        write_pair_triggers(trig_jsonl_record)

        try:
            if popup_handle:


                popup_handle.evaluate("node => node.removeAttribute('data-mcg-def-root')")
        except Exception:
            pass


        _close_popup(dom, page, log)

        try:
            if popup_handle:


                popup_handle.dispose()
        except Exception:
            pass

        update_elapsed(progress, started_mono)

        try:
            page.wait_for_timeout(int(200 + random.random() * 300))
        except Exception:
            time.sleep((200 + random.random() * 300) / 1000.0)

        _def_ui_tick(consumed)

    if terminal_progress is not None:
        _def_ui_tick(trig_cap - popped_budget, force=True)
        terminal_progress.definition_finish_line()

    pops_sorted = sorted(popups_by_key.values(), key=lambda r: (r.popup_type, r.normalized_title, r.popup_id))
    definitions_only = [p for p in pops_sorted if p.popup_type == "definition"]

    big_html_defs: list[str] = []

    big_html_defs.append(f"<!-- definitions aggregate for {mcg_code} ({out_prefix}) -->\n")


    for rd in definitions_only:


        esc = rd.title.replace("--", "- -")

        big_html_defs.append(f"<!-- popup_id:{rd.popup_id} -->\n<div class=\"mcg-definition-capture\">\n")

        big_html_defs.append(rd.html)

        big_html_defs.append("\n</div>\n")

    defs_html_path.write_text("".join(big_html_defs), encoding="utf-8")

    big_pop_html: list[str] = []

    big_pop_html.append(f"<!-- all popups aggregate for {mcg_code} ({out_prefix}) -->\n")


    for rd in pops_sorted:
        esc = rd.title.replace("--", "- -")
        big_pop_html.append(f"<!-- type:{rd.popup_type} popup_id:{rd.popup_id} -->\n")

        big_pop_html.append('<div class="mcg-popup-capture">\n')

        big_pop_html.append(rd.html)

        big_pop_html.append("\n</div>\n")


    popups_html_path.write_text("".join(big_pop_html), encoding="utf-8")

    lw_parts: list[str] = []

    for d in pops_sorted:
        lw_parts.append((d.title or "").lower())


        lw_parts.append("\n")
        lw_parts.append((d.text or "").lower())

    lw = " ".join(lw_parts)


    processed_click_triggers = len(fingerprints_clicked)

    defs_payload = {
        "schema_version": "mcg_popup_capture.v2",
        "mcg_code": mcg_code,
        "mcg_title": mcg_title,
        "source_url": source_url,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "definition_count": len(definitions_only),

        "popup_count": len(pops_sorted),
        "citation_count": int(popup_type_capture_counts.get("citation", 0)),
        "footnote_count": int(popup_type_capture_counts.get("footnote", 0)),
        "context_count": int(popup_type_capture_counts.get("context", 0)),
        "other_popup_count": int(popup_type_capture_counts.get("other_popup", 0)),
        "trigger_attempts_recorded": int(processed_click_triggers),
        "definitions": [_serialize_popup_record(d, compat_definition_id=True) for d in definitions_only],

        "popups": [_serialize_popup_record(d, compat_definition_id=d.popup_type == "definition") for d in pops_sorted],
        "failures": failures_audit[-400:],

        "audit": {},
    }

    def_audit = {
        "popup_count": len(pops_sorted),
        "definition_count": len(definitions_only),
        "citation_count": int(popup_type_capture_counts.get("citation", 0)),
        "footnote_count": int(popup_type_capture_counts.get("footnote", 0)),
        "context_count": int(popup_type_capture_counts.get("context", 0)),
        "other_popup_count": int(popup_type_capture_counts.get("other_popup", 0)),
        "nested_trigger_count": int(nested_triggers_discovered_total),
        "nested_enqueued_total": int(nested_enqueued_total),
        "captured_nested_popup_count": sum(1 for x in pops_sorted if x.depth and x.depth > 0),

        "max_depth_reached": int(max_depth_reached),
        "candidate_trigger_count": int(candidate_trigger_count),
        "popup_definition_trigger_count": int(popup_definition_trigger_count),
        "trigger_queue_enqueues_seen": int(queued_trigger_candidates),
        "enqueue_unique_keys": int(queued_unique_enqueue_keys),
        "enqueue_skipped_duplicates": int(skipped_enqueue_duplicate_count),
        "clicked_trigger_count": int(clicked_trigger_count),
        "direct_invocation_count": int(direct_invocation_count),
        "popup_detected_count": int(popup_detected_count),
        "failed_trigger_count": int(failed_trigger_count),

        "duplicate_popup_merge_count": int(dup_merge_count),
        "skipped_duplicate_enqueue": int(skipped_enqueue_duplicate_count),
        "skipped_duplicate_popup_body": int(skipped_duplicate_popup_count),
        "hemodynamic_instability_found": bool(re.search(r"hemodynamic\s+instability", lw)),
        "tachycardia_found": "tachycardia" in lw,
        "hypotension_found": "hypotension" in lw,
        "orthostatic_hypotension_found": "orthostatic" in lw and "hypotension" in lw,
        "altered_mental_status_found": bool(re.search(r"altered\s+mental\s+status", lw)),
        "shock_index_found": bool(re.search(r"shock\s+index", lw)),
        "mean_arterial_pressure_found": bool(re.search(r"mean\s+arterial\s+pressure", lw)),
        "vasopressor_found": bool(re.search(r"vasopressor|inotropic", lw)),
        "lactate_found": "lactate" in lw,
        "citation_refs_captured": bool(popup_type_capture_counts.get("citation", 0) > 0),
        "footnote_refs_captured": bool(popup_type_capture_counts.get("footnote", 0) > 0),
        "context_links_captured": bool(popup_type_capture_counts.get("context", 0) > 0),
        "definition_titles": [d.title for d in definitions_only][:500],

        "popup_titles": [p.title for p in pops_sorted][:800],
        "popup_type_histogram": popup_type_capture_counts,
        "warnings": ([] if not warnings_accum else [*warnings_accum])[:],
        "enqueue_dedupe_explanation": "handler|arg_identity|normalized_trigger|parent_popup_id|depth",
    }


    defs_payload["audit"] = def_audit


    defs_json_path.write_text(json.dumps(defs_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


    pops_only_payload = {
        "schema_version": "mcg_popup_capture.v2",

        **{k: v for k, v in defs_payload.items() if k in ("mcg_code", "mcg_title", "source_url", "captured_at")},
        "popups_html_note": str(popups_html_path.relative_to(project_root)),
        "popups": defs_payload["popups"],
        "failures": defs_payload["failures"],
        "audit": defs_payload["audit"],

        "counts": {
            **{str(k): int(defs_payload[k]) for k in (
                "definition_count",
                "popup_count",
                "citation_count",
                "footnote_count",
                "context_count",
                "other_popup_count",
            ) if k in defs_payload},
        },

    }


    popups_json_path.write_text(json.dumps(pops_only_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


    audit_json_path.write_text(json.dumps(def_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


    audit_md_lines = [

        f"# Popup capture summary — `{mcg_code}`",

        "",

        f"- **candidate triggers (gathered)**: `{candidate_trigger_count}`",

        f"- **onclick popup_definition triggers**: `{popup_definition_trigger_count}`",

        f"- **unique popups stored**: `{len(pops_sorted)}`",

        f"- **definition popups**: `{len(definitions_only)}`",

        f"- **citation popups**: `{popup_type_capture_counts.get('citation', 0)}`",

        f"- **footnote popups**: `{popup_type_capture_counts.get('footnote', 0)}`",

        f"- **context popups**: `{popup_type_capture_counts.get('context', 0)}`",

        f"- **popups detected (opens)**: `{popup_detected_count}`",

        f"- **direct JS invocations (success)**: `{direct_invocation_count}`",

        f"- **triggers_clicked (success)**: `{clicked_trigger_count}`",

        f"- **triggers_failed**: `{failed_trigger_count}`",

        f"- **nested triggers discovered**: `{nested_triggers_discovered_total}`",

        f"- **nested queue enqueues**: `{nested_enqueued_total}`",

        f"- **captured_nested depth>0**: `{def_audit['captured_nested_popup_count']}`",

        f"- **max depth observed**: `{max_depth_reached}`",

        "",
        "## Artifacts",
        "",
        f"- `{defs_json_path.relative_to(project_root)}`",

        f"- `{popups_json_path.relative_to(project_root)}`",
        f"- `{defs_html_path.relative_to(project_root)}`",
        f"- `{popups_html_path.relative_to(project_root)}`",
        f"- `{triggers_jsonl.relative_to(project_root)}` + `{popup_triggers_jsonl.relative_to(project_root)}`",
        f"- `{failures_jsonl.relative_to(project_root)}` + `{popup_failures_jsonl.relative_to(project_root)}`",

        f"- `{audit_json_path.relative_to(project_root)}`",

        "",
    ]

    audit_md_path.write_text("\n".join(audit_md_lines) + "\n", encoding="utf-8")


    log.success(
        "Popup capture complete",
        {
            "uniquePopups": len(pops_sorted),
            "definitionsSubset": len(definitions_only),

            "popupDetected": popup_detected_count,

            "directInvocations": direct_invocation_count,
            "clicked": clicked_trigger_count,
            "failed": failed_trigger_count,

            **popup_type_capture_counts,
            "nestedTriggersDiscovered": nested_triggers_discovered_total,

            "skippedDuplicateBodies": skipped_duplicate_popup_count,
        },

    )


    rel = lambda p: str(p.relative_to(project_root))

    out = {
        "enabled": True,
        "schema_version": "mcg_popup_capture.v2",

        "definition_count": len(definitions_only),

        "popup_count": len(pops_sorted),
        "citation_count": int(popup_type_capture_counts.get("citation", 0)),
        "footnote_count": int(popup_type_capture_counts.get("footnote", 0)),
        "context_count": int(popup_type_capture_counts.get("context", 0)),

        "trigger_count": int(processed_click_triggers),
        "candidate_trigger_count": int(candidate_trigger_count),

        "popup_definition_trigger_count": int(popup_definition_trigger_count),
        "clicked_trigger_count": int(clicked_trigger_count),

        "direct_invocation_count": int(direct_invocation_count),
        "popup_detected_count": int(popup_detected_count),
        "failed_trigger_count": int(failed_trigger_count),
        "audit_path_rel": rel(audit_json_path),
        "definitions_json_path_rel": rel(defs_json_path),
        "popups_json_path_rel": rel(popups_json_path),

        "definitions_html_path_rel": rel(defs_html_path),
        "popups_html_path_rel": rel(popups_html_path),

        "triggers_jsonl_path_rel": rel(triggers_jsonl),
        "popup_triggers_jsonl_path_rel": rel(popup_triggers_jsonl),
        "failures_jsonl_path_rel": rel(failures_jsonl),
        "popup_failures_jsonl_path_rel": rel(popup_failures_jsonl),

        "audit": def_audit,
        "skipped_citation_count": 0,
        "skipped_footnote_count": 0,
    }

    progress["definition_capture"] = {
        "definition_count": len(definitions_only),
        "clicked": clicked_trigger_count,

        "popup_count": len(pops_sorted),

    }


    progress["popup_capture"] = {

        "popup_count": len(pops_sorted),
        "definition_count": len(definitions_only),

    }


    progress["last_action"] = "popup_capture_complete"

    return out
