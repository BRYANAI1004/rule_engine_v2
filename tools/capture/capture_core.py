#!/usr/bin/env python3
"""
Core MCG CareWeb HTML capture using persistent Chromium profile (manual auth).

Imported by capture_mcg.py. Capture only — no parsing, rules, or Supabase.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional, Union
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Frame, Locator, Page, Playwright, TimeoutError, sync_playwright

from definition_capture import locator_for_xpath, run_definition_capture
from progress_logger import (
    CaptureTerminalReporter,
    Heartbeat,
    ProgressLogger,
    fresh_capture_progress,
    read_preflight_sentinel_status,
    update_elapsed,
)

DomRoot = Union[Page, Frame]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = PROJECT_ROOT / ".local" / "playwright" / "mcg-careweb"
RAW_HTML_DIR = PROJECT_ROOT / "rules" / "mcg" / "raw-html"
AUDIT_DIR = PROJECT_ROOT / "rules" / "mcg" / "audits"

MAX_EXPAND_PASSES = 10
MAX_EXPAND_CLICKS_PER_PASS = 10
EXPAND_POST_CLICK_WAIT_MS = 350
TEXT_TRUNCATE = 120
MEANINGFUL_BODY_TEXT_DELTA = 48
MEANINGFUL_BODY_HTML_DELTA = 256
REMAINING_PREVIEW = 20
ADDITIONAL_LOGIN_HINTS = [
    "Clinical Indications for Admission to Inpatient Care",
]

SEARCH_PAGE_ADMISSION_MARKERS = (
    "Clinical Indications for Admission to Inpatient Care",
    "Care Planning - Inpatient Admission and Alternatives",
)

_QUICK_SEARCH_INPUT_JS = r"""
() => {
  function norm(s) {
    return String(s || '').replace(/\s+/g, ' ').trim();
  }

  const labels = Array.from(document.querySelectorAll('label, span, div, td, th, p, strong'));
  for (const el of labels) {
    const t = norm(el.innerText);
    if (!t || t.length > 160) continue;
    if (/quick\s*search/i.test(t) || (/guideline/i.test(t) && /contained\s+in/i.test(t))) {
      const forId = el.getAttribute('for');
      if (forId) {
        const inp = document.getElementById(forId);
        if (inp && inp.tagName === 'INPUT') return inp;
      }
      let cur = el.nextElementSibling;
      for (let i = 0; i < 5 && cur; i++) {
        const inp = cur.querySelector && cur.querySelector('input');
        if (inp && inp.tagName === 'INPUT') return inp;
        if (cur.tagName === 'INPUT') return cur;
        cur = cur.nextElementSibling;
      }
      const inner = el.querySelector('input');
      if (inner) return inner;
    }
  }

  const inputs = Array.from(
    document.querySelectorAll('input[type="text"], input[type="search"], input:not([type])')
  );
  for (const inp of inputs) {
    const ph = norm(inp.getAttribute('placeholder') || '');
    if (/quick\s*search/i.test(ph)) return inp;
    if (/guideline\s*content/i.test(ph) || /contained\s+in\s+guideline/i.test(ph)) return inp;
    const ar = norm(inp.getAttribute('aria-label') || '');
    if (/quick\s*search/i.test(ar)) return inp;
    const tid = inp.getAttribute('title') || '';
    if (/quick\s*search/i.test(tid)) return inp;
  }
  return null;
}
"""

_QUICK_SEARCH_COLLECT_TEXT_INPUTS_JS = r"""
() => {
  function norm(s) {
    return String(s || '').replace(/\s+/g, ' ').trim();
  }
  function visible(el) {
    try {
      if (!el || el.nodeType !== 1) return false;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      const r = el.getBoundingClientRect();
      return !!(r && (r.width > 0 || r.height > 0));
    } catch (e) {
      return false;
    }
  }
  function bboxOf(el) {
    try {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    } catch (e) {
      return null;
    }
  }
  function nearbyText(el) {
    const parts = [];
    let cur = el;
    for (let i = 0; i < 9 && cur; i++) {
      const t = norm(cur.innerText || '');
      if (t && t.length) parts.push(t.slice(0, 260));
      cur = cur.parentElement;
    }
    return parts.join(' | ').slice(0, 520);
  }
  const badTypes = new Set(['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'image', 'reset']);
  const rows = [];
  let localIndex = 0;
  for (const inp of document.querySelectorAll('input')) {
    if (!inp || inp.tagName !== 'INPUT') continue;
    const typ = String(inp.getAttribute('type') || 'text').toLowerCase();
    if (badTypes.has(typ)) continue;
    const form = inp.closest('form');
    const bb = bboxOf(inp);
    const dis = !!inp.disabled || inp.hasAttribute('disabled');
    const ro = !!inp.readOnly || inp.hasAttribute('readonly');
    rows.push({
      local_index: localIndex,
      tag: 'INPUT',
      type: typ || 'text',
      name: String(inp.getAttribute('name') || ''),
      id: String(inp.getAttribute('id') || ''),
      placeholder: String(inp.getAttribute('placeholder') || ''),
      aria_label: String(inp.getAttribute('aria-label') || ''),
      title_attr: String(inp.getAttribute('title') || ''),
      value: String(inp.value || '').slice(0, 200),
      disabled: dis,
      readOnly: ro,
      visible: visible(inp),
      enabled: !dis,
      bbox: bb,
      nearby_text: nearbyText(inp),
      form_action: form ? String(form.getAttribute('action') || '') : '',
      form_method: form ? String(form.getAttribute('method') || '') : '',
      form_id: form ? String(form.getAttribute('id') || '') : '',
    });
    localIndex++;
  }
  return rows;
}
"""

_QUICK_SEARCH_RESOLVE_INPUT_BY_INDEX_JS = r"""
(idx) => {
  const badTypes = new Set(['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'image', 'reset']);
  const elements = [];
  for (const inp of document.querySelectorAll('input')) {
    if (!inp || inp.tagName !== 'INPUT') continue;
    const typ = String(inp.getAttribute('type') || 'text').toLowerCase();
    if (badTypes.has(typ)) continue;
    elements.push(inp);
  }
  const i = Number(idx);
  if (!Number.isFinite(i) || i < 0 || i >= elements.length) return null;
  return elements[i] || null;
}
"""

_QUICK_SEARCH_JS_SET_VALUE = r"""
(el, val) => {
  try {
    el.focus();
    if (el.readOnly) {
      try { el.readOnly = false; } catch (e1) {}
    }
    if (el.disabled) {
      try { el.disabled = false; } catch (e2) {}
    }
    el.value = String(val || '');
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('keyup', { bubbles: true }));
    return true;
  } catch (e) {
    return false;
  }
}
"""

_QUICK_SEARCH_JS_CLICK_SEARCH_IN_FORM = r"""
(el) => {
  const form = el.closest('form');
  if (!form) return false;
  const cands = Array.from(
    form.querySelectorAll('button:not([disabled]), input[type="submit"]:not([disabled]), input[type="button"]:not([disabled])')
  );
  for (const b of cands) {
    const label = String((b.innerText || b.textContent || b.value || '')).trim();
    if (!label) continue;
    if (/search\s+content/i.test(label)) continue;
    if (/^search$/i.test(label) || /^search$/i.test(String(b.value || '').trim())) {
      b.click();
      return true;
    }
  }
  for (const b of cands) {
    const label = String((b.innerText || b.textContent || b.value || '')).trim();
    if (!label) continue;
    if (/search\s+content/i.test(label)) continue;
    if (/^search\b/i.test(label) && label.length <= 48) {
      b.click();
      return true;
    }
  }
  return false;
}
"""

_QUICK_SEARCH_JS_FORM_SUBMIT = r"""
(el) => {
  const form = el.closest('form');
  if (!form) return false;
  try {
    form.submit();
    return true;
  } catch (e) {
    return false;
  }
}
"""

_QUICK_SEARCH_JS_DISPATCH_ENTER = r"""
(el) => {
  try {
    el.focus();
    ['keydown', 'keypress', 'keyup'].forEach((k) => {
      el.dispatchEvent(
        new KeyboardEvent(k, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true })
      );
    });
    return true;
  } catch (e) {
    return false;
  }
}
"""


_SEARCH_RESULTS_COLLECT_TABLE_ROWS_JS = r"""
() => {
  function normRow(s) {
    return String(s || '').replace(/\s+/g, ' ').trim();
  }
  const out = [];
  const seen = new Set();
  for (const tr of document.querySelectorAll('table tr, table tbody tr')) {
    const t = normRow(tr.innerText || '');
    if (t.length < 8) continue;
    if (!/\bM[- ]?\d{2,5}\b/i.test(t)) continue;
    if (!t.includes('|')) continue;
    const links = [];
    tr.querySelectorAll('a[href]').forEach((a) => {
      const href = a.getAttribute('href') || '';
      if (!href) return;
      links.push({
        text: String(a.innerText || a.textContent || '').trim().slice(0, 96),
        href: String(href).slice(0, 900),
      });
    });
    if (!links.length) continue;
    const key = t.slice(0, 240);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ row_text: t.slice(0, 900), links: links });
  }
  return out;
}
"""

_SEARCH_RESULTS_CLICK_ROW_LINK_JS = r"""
(payload) => {
  function normBlock(s) {
    return String(s || '').replace(/\s+/g, ' ').trim();
  }
  function normLink(s) {
    return String(s || '').trim().toUpperCase().replace(/\s+/g, '').replace(/-/g, '');
  }
  const linkTexts = payload.linkTexts || [];
  const wants = new Set();
  for (const lt of linkTexts) {
    const n = normLink(lt);
    if (n) wants.add(n);
  }
  const rowNeedle = normBlock(payload.rowNeedle || '').slice(0, 120);
  const rowNeedleLong = normBlock(payload.rowNeedleLong || '').slice(0, 520);
  for (const tr of document.querySelectorAll('table tr, table tbody tr')) {
    const full = normBlock(tr.innerText || '');
    if (full.length < 8) continue;
    const needle = (rowNeedleLong || rowNeedle || '').trim();
    if (needle && !full.includes(needle.slice(0, Math.min(100, needle.length)))) continue;
    for (const a of tr.querySelectorAll('a[href]')) {
      const rawLt = String(a.innerText || a.textContent || '').trim();
      const n = normLink(rawLt);
      if (wants.has(n)) {
        try {
          a.click();
          return { ok: true, clicked_text: rawLt.slice(0, 80), href: String(a.getAttribute('href') || '').slice(0, 400) };
        } catch (e) {
          return { ok: false, err: String(e) };
        }
      }
    }
  }
  return { ok: false };
}
"""


def _same_browser_site(url_a: str, url_b: str) -> bool:
    try:
        return urlparse(url_a).netloc == urlparse(url_b).netloc
    except Exception:
        return False


def _quick_search_collect_frame_rows(fr: Frame) -> list[dict[str, Any]]:
    try:
        raw = fr.evaluate(_QUICK_SEARCH_COLLECT_TEXT_INPUTS_JS)
    except Exception:
        return []
    return raw if isinstance(raw, list) else []


def _score_quick_search_candidate_row(row: dict[str, Any], *, shell_frame_url: str) -> int:
    score = 0
    nt = str(row.get("nearby_text") or "").lower()
    ph = str(row.get("placeholder") or "").lower()
    ar = str(row.get("aria_label") or "").lower()
    tit = str(row.get("title_attr") or "").lower()
    blob = f"{nt} {ph} {ar} {tit}"
    if re.search(r"quick\s*search", blob):
        score += 220
    if re.search(r"enter\s+word", nt) and "guideline" in nt:
        score += 90
    if re.search(r"search\s+content", nt) and not re.search(r"quick\s*search", blob[:960]):
        score -= 140
    bb = row.get("bbox") or {}
    try:
        w = float(bb.get("width") or 0)
        h = float(bb.get("height") or 0)
        if w > 2 and h > 2:
            score += 12
    except Exception:
        pass
    fu = str(row.get("frame_url") or "")
    if shell_frame_url and fu == shell_frame_url:
        score += 55
    return score


def _quick_search_row_eligible(row: dict[str, Any]) -> bool:
    if row.get("disabled"):
        return False
    if row.get("readOnly"):
        return False
    if not row.get("visible"):
        return False
    if not row.get("enabled"):
        return False
    bb = row.get("bbox") or {}
    try:
        if float(bb.get("width") or 0) <= 0 or float(bb.get("height") or 0) <= 0:
            return False
    except Exception:
        return False
    return True


def _save_quick_search_candidates_json(out_prefix: str, payload: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    p = AUDIT_DIR / f"{out_prefix}.quick-search-candidates.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def _resolve_quick_search_handle(fr: Frame, local_index: int):
    try:
        h = fr.evaluate_handle(_QUICK_SEARCH_RESOLVE_INPUT_BY_INDEX_JS, int(local_index))
        el = h.as_element() if h is not None else None
        if el is None and h is not None:
            try:
                h.dispose()
            except Exception:
                pass
        return el
    except Exception:
        return None


def _playwright_search_button_click_in_root(root: DomRoot, log: ProgressLogger) -> bool:
    try:
        btn = root.get_by_role("button", name=re.compile(r"^\s*search\s*$", re.I)).first
        if btn.count() > 0 and btn.is_visible(timeout=800):
            btn.click(timeout=5000)
            return True
    except Exception as e:
        log.info("Quick Search Playwright exact Search button skipped", {"error": str(e)})
    try:
        btn2 = root.get_by_role("button", name=re.compile(r"^\s*search\b", re.I)).first
        if btn2.count() > 0 and btn2.is_visible(timeout=800):
            lbl = ""
            try:
                lbl = str(btn2.inner_text() or "")[:80]
            except Exception:
                pass
            if re.search(r"search\s+content", lbl, re.I):
                return False
            btn2.click(timeout=5000)
            return True
    except Exception as e:
        log.info("Quick Search Playwright Search button skipped", {"error": str(e)})
    return False


def _try_quick_search_fill_and_submit_for_candidate(
    *,
    owner_page: Page,
    fr: Frame,
    row: dict[str, Any],
    search_code: str,
    log: ProgressLogger,
    progress: dict[str, Any],
    page_for_wait: Page,
    mcg_code: str,
    mcg_title: str,
) -> tuple[bool, list[str], Optional[Frame], bool]:
    """Try fill+submit pipelines A–E for one candidate; updates progress quick_search_* on success."""
    if progress.get("_abort_quick_search_pipelines"):
        return False, [], None, False
    sc = str(search_code or "").strip()
    li = int(row.get("local_index") or 0)
    handle = _resolve_quick_search_handle(fr, li)
    if handle is None:
        return False, [], None, False

    def _normal_fill() -> tuple[bool, str]:
        try:
            try:
                handle.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            handle.click(timeout=5000)
            handle.fill("")
            handle.fill(sc)
            return True, "normal_fill"
        except Exception as e:
            log.info("Quick Search normal fill failed", {"error": str(e)})
            return False, ""

    def _js_fill() -> tuple[bool, str]:
        try:
            ok = handle.evaluate(_QUICK_SEARCH_JS_SET_VALUE, sc)
            return (True, "js_set_value") if ok else (False, "")
        except Exception as e:
            log.info("Quick Search JS set value failed", {"error": str(e)})
            return False, ""

    def _submit_button_pw() -> tuple[bool, str]:
        if _playwright_search_button_click_in_root(fr, log):
            return True, "button_click"
        return False, ""

    def _submit_button_js() -> tuple[bool, str]:
        try:
            ok = handle.evaluate(_QUICK_SEARCH_JS_CLICK_SEARCH_IN_FORM)
            return (True, "js_button_click") if ok else (False, "")
        except Exception:
            return False, ""

    def _submit_enter_pw() -> tuple[bool, str]:
        try:
            owner_page.keyboard.press("Enter")
            return True, "enter"
        except Exception as e:
            log.info("Quick Search Enter (page keyboard) failed", {"error": str(e)})
            return False, ""

    def _submit_form_js() -> tuple[bool, str]:
        try:
            ok = handle.evaluate(_QUICK_SEARCH_JS_FORM_SUBMIT)
            return (True, "form_submit") if ok else (False, "")
        except Exception:
            return False, ""

    def _submit_enter_js() -> tuple[bool, str]:
        try:
            ok = handle.evaluate(_QUICK_SEARCH_JS_DISPATCH_ENTER)
            return (True, "enter") if ok else (False, "")
        except Exception:
            return False, ""

    plan: list[tuple[Callable[[], tuple[bool, str]], Callable[[], tuple[bool, str]]]] = [
        (_normal_fill, _submit_button_pw),
        (_normal_fill, _submit_enter_pw),
        (_js_fill, _submit_button_js),
        (_js_fill, _submit_form_js),
        (_js_fill, _submit_enter_js),
    ]

    try:
        for attempt_i, (fill_fn, submit_fn) in enumerate(plan):
            if progress.get("_abort_quick_search_pipelines"):
                return False, [], None, False
            ok_f, used_f = fill_fn()
            if not ok_f:
                continue
            progress["quick_search_fill_method"] = used_f
            ok_s, used_s = submit_fn()
            if not ok_s:
                continue
            progress["quick_search_submit_method"] = used_s
            tw = 120_000 if attempt_i == len(plan) - 1 else 25_000
            deadline = time.monotonic() + tw / 1000.0
            ok_m, markers, tgt_fr, in_fr = _poll_search_page_strict_until(
                deadline, page_for_wait, mcg_code, mcg_title
            )
            if ok_m:
                log.info(
                    "Search-page strict target ready (no results click)",
                    {"markers": markers[:10], "submit": used_s, "fill": used_f},
                )
                return True, markers, tgt_fr, in_fr
            clicked, fatal = click_search_result_for_guideline(
                page_for_wait,
                mcg_code=mcg_code,
                mcg_title=mcg_title,
                log=log,
                progress=progress,
            )
            if fatal:
                progress["_abort_quick_search_pipelines"] = True
                return False, [], None, False
            if clicked:
                ok_m2, markers2, tgt_fr2, in_fr2 = _wait_search_page_strict_ready(
                    page_for_wait,
                    mcg_code=mcg_code,
                    mcg_title=mcg_title,
                    log=log,
                    timeout_ms=120_000,
                    log_timeout_error=(tw >= 60_000),
                )
                if ok_m2:
                    return True, markers2, tgt_fr2, in_fr2
        return False, [], None, False
    finally:
        try:
            handle.dispose()
        except Exception:
            pass


def _login_marker_strings(mcg_code: str, mcg_title: str) -> list[str]:
    """Strings that should appear on-page when the correct guideline is loaded after auth."""
    out: list[str] = []
    t = mcg_title.strip()
    if t:
        out.append(t)
    c = mcg_code.strip()
    if c:
        out.append(c)
        m = re.match(r"^M(\d+)$", c, re.I)
        if m:
            num = m.group(1).lstrip("0") or "0"
            out.append(f"M-{num}")
    return out

SKIP_LINK_TEXT_SNIPPETS = [
    "click here to preview",
    "view abstract",
    "context link",
    "return to top",
]

_progress_lock = threading.Lock()


def _truncate(s: str, max_len: int = TEXT_TRUNCATE) -> str:
    t = re.sub(r"\s+", " ", s).strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_target_page(current_url: str, target_url: str) -> bool:
    a, b = urlparse(current_url), urlparse(target_url)
    return (a.scheme, a.netloc, a.path.rstrip("/")) == (b.scheme, b.netloc, b.path.rstrip("/"))


def _norm_url_status(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if s in ("canonical", "search_page"):
        return s
    raise ValueError(f"url_status must be canonical or search_page, got {raw!r}")


def _flex_mcg_identifiers_in_text(text: str, mcg_code: str) -> tuple[bool, list[str]]:
    """
    True if body text contains ORG: M-### (matching this code) or a normalized MCG token.
    Accepts both zero-padded (M-083) and unpadded (M-83) forms when applicable.
    """
    matched: list[str] = []
    t = text or ""
    m = re.match(r"^M(\d+)$", str(mcg_code or "").strip(), re.I)
    if m:
        raw_pad = m.group(1)
        n_int = str(int(raw_pad, 10))
        for v in (raw_pad, n_int):
            if re.search(rf"ORG:\s*M-?\s*{re.escape(v)}\b", t, flags=re.I):
                matched.append(f"ORG: M-{v}")
            if re.search(rf"\bM-?\s*{re.escape(v)}\b", t, flags=re.I):
                matched.append(f"M token {v}")
    mc = str(mcg_code or "").strip().upper()
    if mc and mc in t.upper().replace(" ", ""):
        matched.append(f"literal {mc}")
    return (len(matched) > 0), matched


def _title_substantially_in_text(text: str, mcg_title: str) -> tuple[bool, str]:
    title = re.sub(r"\s+", " ", (mcg_title or "").strip())
    if not title:
        return False, ""
    blob = re.sub(r"\s+", " ", (text or "").strip())
    if title in blob:
        return True, title
    # light punctuation fold for titles with special dash/quotes
    t2 = title.replace("–", "-").replace("—", "-")
    b2 = blob.replace("–", "-").replace("—", "-")
    if t2 in b2:
        return True, t2
    return False, title


def _strict_markers_in_text(text: str, mcg_code: str, mcg_title: str) -> tuple[bool, list[str]]:
    """ORG/MCG + title + one admission phrase — all must match in the same text blob."""
    markers: list[str] = []
    ok_code, code_hits = _flex_mcg_identifiers_in_text(text, mcg_code)
    if ok_code:
        markers.extend(code_hits)
    ok_title, tshow = _title_substantially_in_text(text, mcg_title)
    if ok_title:
        markers.append(f"title:{tshow[:80]}")
    ok_adm = False
    for phrase in SEARCH_PAGE_ADMISSION_MARKERS:
        if phrase in text:
            markers.append(f"section:{phrase[:64]}")
            ok_adm = True
            break
    return (ok_code and ok_title and ok_adm), markers


def _body_inner_text(dom: DomRoot) -> str:
    try:
        return dom.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def _body_inner_html(dom: DomRoot) -> str:
    try:
        return dom.evaluate("() => document.body ? document.body.innerHTML : ''") or ""
    except Exception:
        return ""


def _combined_body_text_all_frames(page: Page) -> str:
    parts: list[str] = []
    for fr in page.frames:
        try:
            t = _body_inner_text(fr)
            if t.strip():
                parts.append(t)
        except Exception:
            continue
    return "\n".join(parts)


def _frame_urls_snapshot(page: Page) -> tuple[int, list[str]]:
    urls: list[str] = []
    for fr in page.frames:
        try:
            urls.append(fr.url)
        except Exception:
            urls.append("")
    return len(urls), urls


def search_page_strict_target_resolve(
    page: Page,
    mcg_code: str,
    mcg_title: str,
) -> tuple[bool, list[str], Optional[Frame], bool]:
    """
    Strict target across frames. Prefer a single frame that contains all markers; else combined text,
    with capture root = frame with largest body text.
    Returns (ok, markers, target_frame, target_detected_in_frame).
    """
    for fr in page.frames:
        try:
            t = _body_inner_text(fr)
        except Exception:
            continue
        ok, mk = _strict_markers_in_text(t, mcg_code, mcg_title)
        if ok:
            try:
                in_fr = fr != page.main_frame
            except Exception:
                in_fr = True
            return True, mk, fr, in_fr
    combined = _combined_body_text_all_frames(page)
    ok, mk = _strict_markers_in_text(combined, mcg_code, mcg_title)
    if not ok:
        return False, [], None, False
    best: Optional[Frame] = None
    best_len = -1
    for fr in page.frames:
        try:
            ln = len(_body_inner_text(fr) or "")
        except Exception:
            ln = 0
        if ln > best_len:
            best_len = ln
            best = fr
    if best is None:
        return True, mk, None, False
    try:
        in_fr = best != page.main_frame
    except Exception:
        in_fr = True
    return True, mk, best, in_fr


def search_page_strict_target_ready(page: Page, mcg_code: str, mcg_title: str) -> tuple[bool, list[str]]:
    """Backward-compatible: strict target using all frames."""
    ok, mk, _, _ = search_page_strict_target_resolve(page, mcg_code, mcg_title)
    return ok, mk


def _wait_search_page_strict_ready(
    page: Page,
    *,
    mcg_code: str,
    mcg_title: str,
    log: ProgressLogger,
    timeout_ms: int = 120_000,
    log_timeout_error: bool = True,
) -> tuple[bool, list[str], Optional[Frame], bool]:
    deadline = time.monotonic() + timeout_ms / 1000.0
    last_markers: list[str] = []
    last_fr: Optional[Frame] = None
    last_in_fr = False
    while time.monotonic() < deadline:
        ok, last_markers, last_fr, last_in_fr = search_page_strict_target_resolve(page, mcg_code, mcg_title)
        if ok:
            log.info(
                "Search-page target markers satisfied",
                {"markers": last_markers[:12], "targetFrameUrl": last_fr.url if last_fr else ""},
            )
            return True, last_markers, last_fr, last_in_fr
        page.wait_for_timeout(280)
    if log_timeout_error:
        log.error(
            "Timed out waiting for search-page target markers",
            {"markers tried": last_markers[:12], "mcg_code": mcg_code},
        )
    return False, last_markers, last_fr, last_in_fr


def _mcg_core_digits(mcg_code: str) -> str:
    m = re.search(r"M[-\s]?(\d{2,5})", (mcg_code or "").strip(), re.I)
    if m:
        return str(int(m.group(1)))
    return ""


def _mcg_wants_rrg_variant(mcg_code: str) -> bool:
    return "RRG" in (mcg_code or "").upper()


def _link_text_variants_for_click(chosen_text: str, digits: str) -> list[str]:
    seen: list[str] = []
    for s in (chosen_text, f"M-{digits}", f"M{digits}", f"m-{digits}"):
        t = str(s or "").strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def _gather_search_result_rows_all_frames(page: Page) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for fi, fr in enumerate(page.frames):
        try:
            raw = fr.evaluate(_SEARCH_RESULTS_COLLECT_TABLE_ROWS_JS)
        except Exception:
            continue
        if not isinstance(raw, list):
            continue
        try:
            furl = fr.url or ""
        except Exception:
            furl = ""
        for r in raw:
            if not isinstance(r, dict):
                continue
            row = dict(r)
            row["frame_index"] = fi
            row["frame_url"] = furl
            flat.append(row)
    return flat


def _parse_result_row_pipe_fields(row_text: str) -> tuple[str, str, str, str]:
    parts = [p.strip() for p in str(row_text or "").split("|") if str(p).strip()]
    code = parts[0] if len(parts) > 0 else ""
    product = parts[1] if len(parts) > 1 else ""
    ptype = parts[2] if len(parts) > 2 else ""
    title = " | ".join(parts[3:]) if len(parts) > 3 else ""
    return code, product, ptype, title


def _score_search_result_row(row: dict[str, Any], *, digits: str, want_rrg: bool, mcg_title: str) -> int:
    text = str(row.get("row_text") or "")
    low = text.lower()
    if not digits:
        return -99999
    if not re.search(rf"M[-\s]?{re.escape(digits)}(?:\b|[\-_])", text, re.I):
        return -99999
    code_cell, prod, ptype, _rt = _parse_result_row_pipe_fields(text)
    prod_u = prod.upper()
    type_u = ptype.upper()
    code_low = code_cell.lower()

    if want_rrg:
        if "rrg" not in low and "rrg" not in code_low:
            return -8500
    else:
        if "rrg" in type_u or re.search(rf"M[-\s]?{re.escape(digits)}[-\s]*RRG", text, re.I):
            return -100000
        if "-rrg" in low.replace(" ", "") or "rrg" in code_low:
            return -100000

    score = 0
    if "ISC" in prod_u or re.search(r"\|\s*ISC\s*\|", text, re.I):
        score += 400
    if "ORG" in type_u or re.search(r"\|\s*ORG\s*\|", text, re.I):
        if not want_rrg:
            score += 650
    if want_rrg and "RRG" in type_u:
        score += 550
    tit = (mcg_title or "").strip()
    if tit and tit.lower()[:52] in low:
        score += 220
    return score


def _pick_code_link_from_row(row: dict[str, Any], *, digits: str, want_rrg: bool) -> Optional[dict[str, Any]]:
    for lk in row.get("links") or []:
        if not isinstance(lk, dict):
            continue
        txt = str(lk.get("text") or "")
        u = txt.upper()
        if not want_rrg and "RRG" in u:
            continue
        m = re.search(r"M[-\s]?(\d{2,5})", txt, re.I)
        if not m:
            continue
        if str(int(m.group(1))) != digits:
            continue
        if not want_rrg and ("RRG" in u or "-RRG" in u.replace(" ", "")):
            continue
        return lk
    return None


def _pick_best_search_result_row(
    flat: list[dict[str, Any]],
    *,
    mcg_code: str,
    mcg_title: str,
) -> tuple[Optional[dict[str, Any]], str]:
    digits = _mcg_core_digits(mcg_code)
    want_rrg = _mcg_wants_rrg_variant(mcg_code)
    if not flat:
        return None, "no_result_rows"
    best: Optional[dict[str, Any]] = None
    best_score = -10**9
    for r in flat:
        s = int(_score_search_result_row(r, digits=digits, want_rrg=want_rrg, mcg_title=mcg_title))
        r["match_score"] = s
        if s > best_score:
            best_score = s
            best = r
    if best is None or best_score <= -50000:
        return None, "no_acceptable_row"
    link = _pick_code_link_from_row(best, digits=digits, want_rrg=want_rrg)
    if link is None:
        return None, "no_suitable_link_in_row"
    best = dict(best)
    best["chosen_link"] = link
    best["digits"] = digits
    code_cell, product, ptype, rtitle = _parse_result_row_pipe_fields(str(best.get("row_text") or ""))
    best["parsed_code"] = code_cell
    best["parsed_product"] = product
    best["parsed_type"] = ptype
    best["parsed_title"] = rtitle
    return best, "selected"


def _search_result_row_manifest_slice(row: dict[str, Any]) -> dict[str, Any]:
    links = row.get("links") if isinstance(row.get("links"), list) else []
    link_summ: list[dict[str, str]] = []
    for lk in links[:12]:
        if isinstance(lk, dict):
            link_summ.append(
                {
                    "text": str(lk.get("text") or "")[:80],
                    "href": str(lk.get("href") or "")[:240],
                },
            )
    code, product, ptype, title = _parse_result_row_pipe_fields(str(row.get("row_text") or ""))
    return {
        "frame_index": int(row.get("frame_index") if row.get("frame_index") is not None else -1),
        "frame_url": str(row.get("frame_url") or ""),
        "code": code[:64],
        "product": product[:32],
        "type": ptype[:32],
        "title": title[:200],
        "row_text": str(row.get("row_text") or "")[:400],
        "links": link_summ,
        "match_score": int(row.get("match_score") or 0),
    }


def _click_selected_search_result(page: Page, pick: dict[str, Any], log: ProgressLogger) -> bool:
    fi = int(pick.get("frame_index") if pick.get("frame_index") is not None else -1)
    frames = page.frames
    if fi < 0 or fi >= len(frames):
        log.warn("search result click: bad frame index", {"frameIndex": fi})
        return False
    fr = frames[fi]
    digits = str(pick.get("digits") or "")
    ch = pick.get("chosen_link")
    ch_text = str((ch or {}).get("text") or "") if isinstance(ch, dict) else ""
    link_texts = _link_text_variants_for_click(ch_text, digits)
    row_needle_long = re.sub(r"\s+", " ", str(pick.get("row_text") or "")).strip()
    row_needle = row_needle_long[:120]
    payload = {"rowNeedle": row_needle, "rowNeedleLong": row_needle_long[:400], "linkTexts": link_texts}
    try:
        res = fr.evaluate(_SEARCH_RESULTS_CLICK_ROW_LINK_JS, payload)
    except Exception as e:
        log.warn("search result click evaluate failed", {"error": str(e)})
        return False
    if isinstance(res, dict) and res.get("ok"):
        log.info(
            "Clicked search result guideline link",
            {"clicked": str(res.get("clicked_text") or ""), "frameIndex": fi},
        )
        return True
    return False


def _poll_search_page_strict_until(deadline: float, page: Page, mcg_code: str, mcg_title: str) -> tuple[bool, list[str], Optional[Frame], bool]:
    last_markers: list[str] = []
    last_fr: Optional[Frame] = None
    last_in_fr = False
    while time.monotonic() < deadline:
        ok, last_markers, last_fr, last_in_fr = search_page_strict_target_resolve(page, mcg_code, mcg_title)
        if ok:
            return True, last_markers, last_fr, last_in_fr
        try:
            page.wait_for_timeout(360)
        except Exception:
            time.sleep(0.36)
    return False, last_markers, last_fr, last_in_fr


def click_search_result_for_guideline(
    page: Page,
    *,
    mcg_code: str,
    mcg_title: str,
    log: ProgressLogger,
    progress: dict[str, Any],
) -> tuple[bool, bool]:
    """
    Detect Quick Search results table rows, pick ORG/ISC row matching mcg_code, click code link.
    Returns (clicked, fatal_no_link) where fatal implies results were detected but navigation failed.
    """
    progress["clicked_search_result"] = False
    rows = _gather_search_result_rows_all_frames(page)
    progress["search_result_rows"] = [_search_result_row_manifest_slice(r) for r in rows][:50]
    progress["search_results_detected"] = bool(rows)
    if not rows:
        progress["selected_search_result"] = {}
        progress["selected_search_result_reason"] = "no_results_table"
        return False, False

    pick, reason = _pick_best_search_result_row(rows, mcg_code=mcg_code, mcg_title=mcg_title)
    progress["selected_search_result_reason"] = reason
    if pick is None:
        progress["selected_search_result"] = {}
        progress["quick_search_failure"] = "search_result_guideline_link_not_found"
        return False, True

    sel_summary = _search_result_row_manifest_slice(pick)
    ch = pick.get("chosen_link")
    if isinstance(ch, dict):
        sel_summary["chosen_link_text"] = str(ch.get("text") or "")[:80]
        sel_summary["chosen_href"] = str(ch.get("href") or "")[:260]
    progress["selected_search_result"] = sel_summary

    if _click_selected_search_result(page, pick, log):
        progress["clicked_search_result"] = True
        return True, False

    progress["quick_search_failure"] = "search_result_guideline_link_not_found"
    return False, True


def _quick_search_input_dom(dom: DomRoot, log: ProgressLogger):
    """Return Playwright ElementHandle for Quick Search input inside this DOM root (page or frame), or None."""
    try:
        h = dom.evaluate_handle(_QUICK_SEARCH_INPUT_JS)
        if h is None:
            return None
        el = h.as_element()
        if el is None:
            try:
                h.dispose()
            except Exception:
                pass
            return None
        return el
    except Exception as e:
        log.warn("Quick Search input discovery failed", {"error": str(e), "errorType": type(e).__name__})
        return None


def _careweb_shell_markers_for_root(root: DomRoot, log: ProgressLogger) -> list[str]:
    markers: list[str] = []
    try:
        text = _body_inner_text(root) or ""
        low = text.lower()
        norm = re.sub(r"\s+", " ", text).strip()
    except Exception:
        return markers

    if _quick_search_input_dom(root, log) is not None:
        markers.append("quick_search_input")

    if re.search(r"quick\s*search", text, re.I):
        markers.append("quick_search_label_or_text")

    if "enter word" in low and "contained in guideline" in low:
        markers.append("phrase_enter_words_in_guideline")
    if "search content" in low:
        markers.append("phrase_search_content")
    if "inpatient & surgical care" in low or ("inpatient" in low and "surgical care" in low):
        markers.append("phrase_inpatient_surgical_care")
    if "optimal recovery guidelines" in low or ("optimal recovery" in low and "guidelines" in low):
        markers.append("phrase_optimal_recovery_guidelines")

    if re.search(r"\bisc\b", norm, re.I):
        markers.append("product_checkbox_isc")
    if re.search(r"\bgrg\b", norm, re.I):
        markers.append("product_checkbox_grg")

    try:
        loc = root.get_by_role("button", name=re.compile(r"search", re.I)).first
        if loc.count() > 0 and loc.is_visible(timeout=500):
            markers.append("search_button")
    except Exception:
        pass

    try:
        lk = root.get_by_role("link", name=re.compile(r"search", re.I)).first
        if lk.count() > 0 and lk.is_visible(timeout=400):
            markers.append("search_link")
    except Exception:
        pass

    try:
        ic = root.locator("input").count()
        if ic > 0:
            markers.append("has_input_fields")
    except Exception:
        pass

    return sorted(dict.fromkeys(markers))


def careweb_search_shell_scan(page: Page, log: ProgressLogger) -> dict[str, Any]:
    all_markers: list[str] = []
    first_idx = -1
    first_url = ""
    first_name = ""
    frames = list(page.frames)
    frame_urls: list[str] = []
    for idx, fr in enumerate(frames):
        try:
            frame_urls.append(fr.url)
        except Exception:
            frame_urls.append("")
        chunk = _careweb_shell_markers_for_root(fr, log)
        for m in chunk:
            if m not in all_markers:
                all_markers.append(m)
        if chunk and first_idx < 0:
            first_idx = idx
            try:
                first_url = fr.url
                first_name = fr.name or ""
            except Exception:
                first_url, first_name = "", ""
    return {
        "detected": len(all_markers) > 0,
        "markers": all_markers,
        "frame_index": first_idx,
        "frame_url": first_url,
        "frame_name": first_name,
        "frame_count": len(frames),
        "frame_urls": frame_urls,
    }


def careweb_search_shell_markers(page: Page, log: ProgressLogger) -> list[str]:
    return careweb_search_shell_scan(page, log)["markers"]


def careweb_search_shell_ready(page: Page, log: ProgressLogger) -> tuple[bool, list[str]]:
    s = careweb_search_shell_scan(page, log)
    return s["detected"], s["markers"]


def run_quick_search_resolution(
    page: Page,
    *,
    search_code: str,
    mcg_code: str,
    mcg_title: str,
    log: ProgressLogger,
    progress: dict[str, Any],
    out_prefix: Optional[str] = None,
) -> tuple[bool, list[str], Optional[Frame], bool]:
    """
    Enumerate text inputs across frames (and same-site context pages), score Quick Search candidates,
    fill + submit with Playwright and JS fallbacks; wait for strict markers across frames.
    """
    for k in (
        "quick_search_failure",
        "_abort_quick_search_pipelines",
        "quick_search_candidate_count",
        "quick_search_selected_frame_url",
        "quick_search_selected_input_summary",
        "quick_search_fill_method",
        "quick_search_submit_method",
        "search_results_detected",
        "search_result_rows",
        "selected_search_result",
        "selected_search_result_reason",
        "clicked_search_result",
    ):
        progress.pop(k, None)

    progress["last_action"] = "quick_search_fill"
    sc = str(search_code or "").strip()
    if not sc:
        log.error("run_quick_search_resolution: empty search_code")
        return False, [], None, False

    shell = careweb_search_shell_scan(page, log)
    shell_fr_url = str(shell.get("frame_url") or "")

    pages_to_scan: list[Page] = [page]
    try:
        base_u = page.url or ""
        for op in page.context.pages:
            if op is not page and base_u and _same_browser_site(base_u, getattr(op, "url", "") or ""):
                pages_to_scan.append(op)
    except Exception:
        pass

    flat_candidates: list[dict[str, Any]] = []
    work_units: list[tuple[Page, Frame, dict[str, Any]]] = []

    for pi, p in enumerate(pages_to_scan):
        for fi, fr in enumerate(p.frames):
            rows = _quick_search_collect_frame_rows(fr)
            try:
                furl = fr.url or ""
            except Exception:
                furl = ""
            try:
                top_url = p.url or ""
            except Exception:
                top_url = ""
            for r in rows:
                if not isinstance(r, dict):
                    continue
                enriched: dict[str, Any] = dict(r)
                enriched["context_page_index"] = pi
                enriched["frame_index"] = fi
                enriched["frame_url"] = furl
                enriched["top_page_url"] = top_url
                enriched["score"] = int(_score_quick_search_candidate_row(enriched, shell_frame_url=shell_fr_url))
                elig = _quick_search_row_eligible(enriched)
                enriched["eligible"] = bool(elig)
                row_snap = {k: v for k, v in enriched.items() if not str(k).startswith("_")}
                flat_candidates.append(row_snap)
                work_units.append((p, fr, enriched))

    progress["quick_search_candidate_count"] = len(work_units)

    def _dump_candidates(fail_reason: str, phase: str) -> None:
        if not out_prefix:
            return
        try:
            _save_quick_search_candidates_json(
                out_prefix,
                {
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "failure_reason": fail_reason,
                    "failure_phase": phase,
                    "search_code": sc,
                    "shell_frame_url": shell_fr_url,
                    "candidate_count": len(flat_candidates),
                    "candidates": flat_candidates,
                },
            )
        except OSError as exc:
            log.warn("quick_search candidates json write failed", {"error": str(exc)})

    if not work_units:
        progress["quick_search_failure"] = "quick_search_input_not_found"
        log.error("Quick Search: no text input candidates in any frame")
        _dump_candidates("quick_search_input_not_found", "collect")
        return False, [], None, False

    eligible_units = [(p, fr, e) for (p, fr, e) in work_units if _quick_search_row_eligible(e)]
    if not eligible_units:
        progress["quick_search_failure"] = "quick_search_input_not_enabled"
        log.error(
            "Quick Search: all input candidates are disabled, hidden, or read-only",
            {"candidateCount": len(work_units)},
        )
        _dump_candidates("quick_search_input_not_enabled", "eligibility")
        return False, [], None, False

    eligible_units.sort(key=lambda t: int(t[2].get("score") or 0), reverse=True)

    for owner_p, fr, row in eligible_units:
        progress["last_action"] = "quick_search_try_candidate"
        ok, markers, tgt_fr, in_fr = _try_quick_search_fill_and_submit_for_candidate(
            owner_page=owner_p,
            fr=fr,
            row=row,
            search_code=sc,
            log=log,
            progress=progress,
            page_for_wait=page,
            mcg_code=mcg_code,
            mcg_title=mcg_title,
        )
        if ok:
            progress["quick_search_selected_frame_url"] = str(row.get("frame_url") or "")
            progress["quick_search_selected_input_summary"] = {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "placeholder": str(row.get("placeholder") or "")[:120],
                "value": str(row.get("value") or "")[:120],
                "local_index": int(row.get("local_index") or 0),
                "frame_url": str(row.get("frame_url") or ""),
                "context_page_index": int(row.get("context_page_index") or 0),
            }
            progress["last_action"] = "quick_search_wait_markers"
            progress.pop("quick_search_failure", None)
            return True, markers, tgt_fr, in_fr

    progress["quick_search_failure"] = "quick_search_fill_submit_failed"
    log.error(
        "Quick Search: fill/submit exhausted for all eligible candidates",
        {"tried": len(eligible_units)},
    )
    _dump_candidates("quick_search_fill_submit_failed", "fill_submit")
    return False, [], None, False


def restore_search_page_target(
    page: Page,
    *,
    shell_url: str,
    search_code: str,
    mcg_code: str,
    mcg_title: str,
    log: ProgressLogger,
    progress: dict[str, Any],
    out_prefix: Optional[str] = None,
) -> tuple[bool, list[str], Optional[Frame], bool]:
    """Reload shell URL then replay Quick Search (URL bar does not track guideline in SPA)."""
    _goto_domcontent_with_retries(
        page,
        shell_url,
        mcg_code,
        mcg_title,
        log,
        progress,
        require_body_target_markers=False,
    )
    return run_quick_search_resolution(
        page,
        search_code=search_code,
        mcg_code=mcg_code,
        mcg_title=mcg_title,
        log=log,
        progress=progress,
        out_prefix=out_prefix,
    )


def _write_failed_capture_manifest(
    *,
    manifest_path: Path,
    mcg_code: str,
    mcg_title: str,
    source_url: str,
    url_status: str,
    search_code: str,
    out_prefix: str,
    reasons: list[str],
    target_detected: bool,
    target_detection_markers: list[str],
    search_shell_detected: bool = False,
    search_shell_markers: Optional[list[str]] = None,
    login_check_mode: str = "canonical",
    quick_search_attempted: bool = False,
    search_shell_frame_url: str = "",
    search_shell_frame_index: int = -1,
    resolved_target_frame_url: str = "",
    resolved_target_frame_index: int = -1,
    frame_count: int = 0,
    frame_urls: Optional[list[str]] = None,
    target_detected_in_frame: bool = False,
    quick_search_candidate_count: int = -1,
    quick_search_selected_frame_url: str = "",
    quick_search_selected_input_summary: Optional[dict[str, Any]] = None,
    quick_search_fill_method: str = "",
    quick_search_submit_method: str = "",
    quick_search_failure: str = "",
    search_results_detected: bool = False,
    search_result_rows: Optional[list[Any]] = None,
    selected_search_result: Optional[dict[str, Any]] = None,
    selected_search_result_reason: str = "",
    clicked_search_result: bool = False,
) -> None:
    """Write a minimal manifest when capture fails before normal artifacts exist."""
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    rel_raw = f"rules/mcg/raw-html/{out_prefix}.full.raw.html"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    ssh = list(search_shell_markers or [])
    blob: dict[str, Any] = {
        "mcg_code": mcg_code,
        "mcg_title": mcg_title,
        "source_url": source_url,
        "url_status": url_status,
        "search_code": search_code or "",
        "resolved_by": "quick_search" if url_status == "search_page" else "canonical_url",
        "target_detected": target_detected,
        "target_detection_markers": list(target_detection_markers),
        "login_check_mode": str(login_check_mode),
        "search_shell_detected": bool(search_shell_detected),
        "search_shell_markers": ssh,
        "quick_search_attempted": bool(quick_search_attempted),
        "search_shell_frame_url": search_shell_frame_url,
        "search_shell_frame_index": int(search_shell_frame_index),
        "resolved_target_frame_url": resolved_target_frame_url,
        "resolved_target_frame_index": int(resolved_target_frame_index),
        "frame_count": int(frame_count),
        "frame_urls": list(frame_urls or []),
        "target_detected_in_frame": bool(target_detected_in_frame),
        "quick_search_candidate_count": int(quick_search_candidate_count),
        "quick_search_selected_frame_url": str(quick_search_selected_frame_url or ""),
        "quick_search_selected_input_summary": dict(quick_search_selected_input_summary or {}),
        "quick_search_fill_method": str(quick_search_fill_method or ""),
        "quick_search_submit_method": str(quick_search_submit_method or ""),
        "quick_search_failure": str(quick_search_failure or ""),
        "search_results_detected": bool(search_results_detected),
        "search_result_rows": list(search_result_rows or [])[:50],
        "selected_search_result": dict(selected_search_result or {}),
        "selected_search_result_reason": str(selected_search_result_reason or ""),
        "clicked_search_result": bool(clicked_search_result),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "capture_status": "failed",
        "capture_status_reasons": list(reasons),
        "raw_html_path": rel_raw,
        "expanded_html_path": f"rules/mcg/raw-html/{out_prefix}.full.expanded.html",
        "expanded_text_path": f"rules/mcg/raw-html/{out_prefix}.full.expanded.txt",
        "screenshot_path": "",
        "summary_path": f"rules/mcg/audits/{out_prefix}.capture-summary.md",
        "raw_html_sha256": "",
        "expanded_html_sha256": "",
        "expanded_text_sha256": "",
        "expand_passes": 0,
        "expand_click_count": 0,
        "expand_phase_outcome": "",
        "expand_phase_summary": {},
        "remaining_expand_control_count": 0,
        "remaining_expand_controls": [],
        "expand_verification": {},
        "section_probe": {},
        "warnings": [],
        "definition_capture": {
            "enabled": False,
            "schema_version": "",
            "definition_count": 0,
            "popup_count": 0,
            "citation_count": 0,
            "footnote_count": 0,
            "context_count": 0,
            "trigger_count": 0,
            "audit_path": "",
            "definitions_json_path": "",
            "popups_json_path": "",
            "popups_html_path": "",
            "popup_triggers_jsonl_path": "",
            "popup_failures_jsonl_path": "",
        },
    }
    manifest_path.write_text(json.dumps(blob, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _href_blocked_for_expand(href: Optional[str]) -> Optional[str]:
    """Return skip reason if href looks like navigation away from guideline content."""
    if not href or not str(href).strip():
        return None
    h = str(href).strip()
    low = h.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return "blocked absolute http(s) href"
    snippets = (
        "login.mcg",
        "/login",
        "authorize",
        "callback",
        "index.html",
        "#top",
        "account",
        "/codes",
        "codes.htm",
        "/abstract",
        "abstract.htm",
        "/references",
        "references.htm",
    )
    for s in snippets:
        if s in low:
            return f"blocked href contains {s!r}"
    return None


def _warn_if_section_terms(text: str) -> bool:
    low = text.lower()
    keys = ["admission", "clinical indications", "discharge", "discharge planning", "discharge destination"]
    return any(k in low for k in keys)


def _expanded_html_saved_ok(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def _nonempty_file(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def _compute_capture_status(
    *,
    capture_definitions: bool,
    definition_capture_aborted: bool,
    expanded_html_path: Path,
    expand_outcome: str,
    warnings: list[str],
    remaining_expand_previews: list[str],
    section_probe: dict[str, Any],
    manifest_definition_capture: dict[str, Any],
    def_summary: Optional[dict[str, Any]],
) -> tuple[str, list[str]]:
    """
    Derive manifest capture_status + reasons.

    failed > needs_review > pass (single label; failed blocks downgrade).
    """

    failed_reasons: list[str] = []

    if definition_capture_aborted:
        failed_reasons.append("definition_capture_aborted")

    if not _expanded_html_saved_ok(expanded_html_path):
        failed_reasons.append("expanded_html_missing_or_empty")

    if failed_reasons:
        return "failed", failed_reasons

    review_reasons: list[str] = []

    exp_o = str(expand_outcome or "")
    if exp_o.startswith("aborted_"):
        review_reasons.append("expand_phase_aborted_outcome")

    blob_warn = " ".join(w.lower() for w in warnings)
    if "unexpected navigation" in blob_warn or "restore navigation failed" in blob_warn:
        review_reasons.append("unexpected_navigation_warning")

    if not bool(section_probe.get("has_admission")):
        review_reasons.append("admission_section_marker_missing")

    for txt in remaining_expand_previews:
        if _warn_if_section_terms(txt):
            review_reasons.append("residual_expand_near_critical_section")
            break

    if capture_definitions:
        dc = manifest_definition_capture
        def_ct = int(dc.get("definition_count") or 0)
        trig_ct = int(dc.get("trigger_count") or 0)

        if def_ct == 0:
            review_reasons.append("definitions_enabled_definition_count_zero")
        if trig_ct == 0:
            review_reasons.append("definitions_enabled_trigger_count_zero")

        fs = def_summary or {}
        failed_tc = int(fs.get("failed_trigger_count") or 0)
        trig_proc = int(fs.get("trigger_count") or 0)
        if trig_proc > 0:
            if failed_tc >= 120:
                review_reasons.append("failed_trigger_count_threshold")
            ratio = failed_tc / float(trig_proc)
            if ratio >= 0.40:
                review_reasons.append("failed_trigger_ratio_threshold")

    if review_reasons:
        return "needs_review", review_reasons

    return "pass", []


def _resolve_capture_exit_code(capture_status: str, *, allow_needs_review_exit_zero: bool) -> int:
    if capture_status == "failed":
        return 1
    if capture_status == "needs_review":
        return 0 if allow_needs_review_exit_zero else 1
    return 0


def scroll_full_page(dom: DomRoot, page: Page, log: ProgressLogger, progress: dict[str, Any], started: float) -> None:
    progress["stage"] = "scroll"
    progress["last_action"] = "scroll_started"
    update_elapsed(progress, started)
    log.step("Full-page scroll started")

    dom.evaluate("() => window.scrollTo(0, 0)")
    step_px = 800
    step_index = 0
    scroll_height = dom.evaluate(
        "() => Math.max(document.body?.scrollHeight ?? 0, document.documentElement.scrollHeight ?? 0)"
    )
    scroll_y = 0

    while scroll_y + 1 < scroll_height:
        scroll_y = min(scroll_y + step_px, scroll_height)
        dom.evaluate("(y) => window.scrollTo(0, y)", scroll_y)
        page.wait_for_timeout(80)
        step_index += 1
        scroll_height = dom.evaluate(
            "() => Math.max(document.body?.scrollHeight ?? 0, document.documentElement.scrollHeight ?? 0)"
        )
        if step_index % 3 == 0 or scroll_y >= scroll_height - 1:
            update_elapsed(progress, started)
            log.info("Scrolling page", {"scrollY": scroll_y, "scrollHeight": scroll_height})
            progress["last_action"] = f"scroll_y_{scroll_y}"

    progress["last_action"] = "scroll_completed"
    update_elapsed(progress, started)
    log.success("Full-page scroll completed", {"finalScrollHeight": scroll_height, "finalScrollY": scroll_y})


def _safe_page_url(page: Page) -> str:
    try:
        return page.url
    except Exception:
        return ""


def _safe_page_title(page: Page) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def page_has_target_content(page: Page, mcg_code: str, mcg_title: str) -> bool:
    """True if body text suggests the expected guideline is loaded (safe if DOM is unstable)."""
    try:
        text = _body_inner_text(page)
    except Exception:
        return False
    for marker in _login_marker_strings(mcg_code, mcg_title):
        if marker and marker in text:
            return True
    if "Clinical Indications for Admission to Inpatient Care" in text:
        return True
    return False


def page_has_login_markers(page: Page, mcg_code: str, mcg_title: str) -> bool:
    text = _body_inner_text(page)
    for marker in _login_marker_strings(mcg_code, mcg_title):
        if marker and marker in text:
            return True
    return any(h in text for h in ADDITIONAL_LOGIN_HINTS)


def _goto_domcontent_with_retries(
    page: Page,
    url: str,
    mcg_code: str,
    mcg_title: str,
    log: ProgressLogger,
    progress: dict[str, Any],
    *,
    require_body_target_markers: bool = True,
) -> Any:
    """
    Navigate with wait_until=domcontentloaded, up to two attempts.
    On TimeoutError, accept stalled navigation if guideline markers appear in the DOM
    (or, when require_body_target_markers=False, if the URL matches the expected location).
    Returns the Response from the last successful goto, or None if we continued after timeout with content.
    """
    nav_response: Any = None

    for attempt in (1, 2):
        try:
            nav_response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            cur = _safe_page_url(page)
            if not cur:
                try:
                    cur = page.url
                except Exception:
                    cur = ""
            progress["current_url"] = cur
            return nav_response
        except TimeoutError as e:
            cur = _safe_page_url(page)
            title = _safe_page_title(page)
            if require_body_target_markers:
                detected = page_has_target_content(page, mcg_code, mcg_title)
            else:
                detected = _same_target_page(cur, url)
            log.info(
                "Navigation attempt ended (timeout)",
                {"attempt": attempt, "currentUrl": cur, "pageTitle": title, "targetContentDetected": detected},
            )
            if detected:
                log.warn(
                    "Navigation timed out, but target guideline content is present. Continuing capture."
                    if require_body_target_markers
                    else "Navigation timed out, but expected shell URL is loaded. Continuing.",
                    {"attempt": attempt, "currentUrl": cur, "pageTitle": title},
                )
                progress["current_url"] = cur or _safe_page_url(page)
                return None
            if attempt == 1:
                log.warn(
                    "Navigation timed out without target content; retrying goto"
                    if require_body_target_markers
                    else "Navigation timed out without expected shell URL; retrying goto",
                    {"attempt": attempt, "currentUrl": cur, "pageTitle": title},
                )
                continue
            msg = (
                f"Navigation timed out after 2 attempts and target guideline content was not detected "
                f"(url={cur!r}, title={title!r})"
                if require_body_target_markers
                else f"Navigation timed out after 2 attempts and expected shell URL was not reached "
                f"(url={cur!r}, title={title!r})"
            )
            log.error(msg, {"currentUrl": cur, "pageTitle": title})
            raise RuntimeError(msg) from e
        except Exception as e:
            cur = _safe_page_url(page)
            title = _safe_page_title(page)
            if require_body_target_markers:
                detected = page_has_target_content(page, mcg_code, mcg_title)
            else:
                detected = _same_target_page(cur, url)
            log.info(
                "Navigation attempt ended (error)",
                {
                    "attempt": attempt,
                    "errorType": type(e).__name__,
                    "error": str(e),
                    "currentUrl": cur,
                    "pageTitle": title,
                    "targetContentDetected": detected,
                },
            )
            if detected:
                log.warn(
                    "Navigation failed, but target guideline content is present. Continuing capture."
                    if require_body_target_markers
                    else "Navigation failed, but expected shell URL is loaded. Continuing capture.",
                    {"attempt": attempt, "currentUrl": cur, "pageTitle": title, "errorType": type(e).__name__},
                )
                progress["current_url"] = cur or _safe_page_url(page)
                return None
            if attempt == 1:
                log.warn(
                    "Navigation failed without target content; retrying goto",
                    {"attempt": attempt, "errorType": type(e).__name__, "error": str(e)},
                )
                continue
            msg = (
                f"Navigation failed after 2 attempts and target guideline content was not detected "
                f"(url={cur!r}, title={title!r}, error={e!r})"
                if require_body_target_markers
                else f"Navigation failed after 2 attempts and expected shell URL was not reached "
                f"(url={cur!r}, title={title!r}, error={e!r})"
            )
            log.error(msg, {"currentUrl": cur, "pageTitle": title, "errorType": type(e).__name__})
            raise RuntimeError(msg) from e

    raise RuntimeError("Navigation failed after retries (internal error)")


_EXPAND_SAFE_CANDIDATES_JS = r"""
() => {
  function visible(el) {
    try {
      if (!el || el.nodeType !== 1) return false;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') === 0) {
        return false;
      }
      const r = el.getClientRects();
      return !!(r && r.length > 0);
    } catch (e) {
      return false;
    }
  }

  function bbox(el) {
    try {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    } catch (e) {
      return null;
    }
  }

  function buildXPath(el) {
    try {
      if (!el || el.nodeType !== 1) return '';
      const segs = [];
      let cur = el;
      for (let depth = 0; depth < 40 && cur && cur.nodeType === 1; depth++) {
        const nm = cur.nodeName.toLowerCase();
        if (nm === 'html') {
          segs.unshift('html');
          break;
        }
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

  function contentRoot() {
    return (
      document.querySelector('main') ||
      document.querySelector('[role="main"]') ||
      document.querySelector('#content') ||
      document.body
    );
  }

  function normInnerText(el) {
    try {
      return String(el.innerText || '').replace(/\s+/g, ' ').trim();
    } catch (e) {
      return '';
    }
  }

  function blockedHref(href) {
    if (!href) return false;
    const low = String(href).trim().toLowerCase();
    const snippets = [
      'login.mcg', '/login', 'authorize', 'callback', 'index.html',
      '#top', 'account', '/codes', 'codes.htm', '/abstract', 'abstract.htm',
      '/references', 'references.htm'
    ];
    for (let i = 0; i < snippets.length; i++) {
      if (low.includes(snippets[i])) return true;
    }
    return false;
  }

  function classifyExpandText(t) {
    const s = String(t || '').replace(/\s+/g, ' ').trim();
    if (!s) return null;
    if (/\bexpand\s+all\b/i.test(s)) return 'expand_all';
    if (/^\[\s*expand\s+all\s*\/\s*collapse\s+all\s*\]\s*$/i.test(s)) return 'expand_toggle';
    if (/^\s*expand\s*$/i.test(s)) return 'expand_only';
    return null;
  }

  const root = contentRoot();
  if (!root) return [];

  const nodes = Array.from(root.querySelectorAll('button, a, [role="button"], [role="link"]'));
  const hits = [];
  const seen = new Set();

  for (let domIndex = 0; domIndex < nodes.length; domIndex++) {
    const el = nodes[domIndex];
    if (!visible(el)) continue;
    const bb = bbox(el);
    if (!bb || bb.width <= 0 || bb.height <= 0) continue;

    const tag = (el.tagName || '').toLowerCase();
    const href = el.getAttribute('href') || '';
    if (tag === 'a' && blockedHref(href)) continue;

    const text = normInnerText(el);
    if (!text) continue;

    const kind = classifyExpandText(text);
    if (!kind) continue;

    const xp = buildXPath(el);
    if (!xp || xp.charAt(0) !== '/') continue;
    if (seen.has(xp)) continue;
    seen.add(xp);

    let tier = 2;
    if (kind === 'expand_all') tier = 0;
    else if (kind === 'expand_toggle') tier = 1;

    hits.push({
      xpath: xp,
      text,
      tag,
      href,
      onclick: el.getAttribute('onclick') || '',
      aria_expanded: el.getAttribute('aria-expanded') || '',
      kind,
      tier,
      domIndex,
      bbox: bb,
    });
  }

  hits.sort((a, b) => {
    if (a.tier !== b.tier) return a.tier - b.tier;
    return a.domIndex - b.domIndex;
  });

  return hits;
}
"""


_EXPAND_GROUP_IDS_FROM_ONCLICK_JS = r"""
() => {
  function visible(el) {
    try {
      if (!el || el.nodeType !== 1) return false;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') === 0) {
        return false;
      }
      const r = el.getClientRects();
      return !!(r && r.length > 0);
    } catch (e) {
      return false;
    }
  }

  function contentRoot() {
    return (
      document.querySelector('main') ||
      document.querySelector('[role="main"]') ||
      document.querySelector('#content') ||
      document.body
    );
  }

  const root = contentRoot();
  if (!root) return [];

  const out = [];
  const seen = new Set();
  const nodes = root.querySelectorAll('[onclick]');
  const rAll = /\bexpandall\s*\(\s*['"]([^'"]+)['"]\s*\)/gi;
  const rOne = /\bexpand\s*\(\s*['"]([^'"]+)['"]\s*\)/gi;

  for (const el of nodes) {
    if (!visible(el)) continue;
    const oc = el.getAttribute('onclick') || '';
    let m;
    rAll.lastIndex = 0;
    while ((m = rAll.exec(oc)) !== null) {
      const id = m[1];
      if (id && !seen.has(id)) {
        seen.add(id);
        out.push(id);
      }
    }
    rOne.lastIndex = 0;
    while ((m = rOne.exec(oc)) !== null) {
      const id = m[1];
      if (id && !seen.has(id)) {
        seen.add(id);
        out.push(id);
      }
    }
  }
  return out;
}
"""


_COLLAPSE_EXPAND_MAP_KEYS_JS = r"""
() => {
  try {
    if (typeof window.collapseExpandMap === 'object' && window.collapseExpandMap !== null) {
      return Object.keys(window.collapseExpandMap);
    }
  } catch (e) {}
  return [];
}
"""


_EXPAND_INVOKE_GROUP_JS = r"""
(gid) => {
  const g = String(gid);
  try {
    if (typeof window.expandall === 'function') {
      window.expandall(g);
      return { ok: true, fn: 'expandall' };
    }
    if (typeof window.expand === 'function') {
      window.expand(g);
      return { ok: true, fn: 'expand' };
    }
  } catch (e) {
    return { ok: false, fn: 'error', err: String(e) };
  }
  return { ok: false, fn: 'none' };
}
"""

MAX_JS_EXPAND_IDS_PER_PASS = 40


def _merge_expand_group_ids(onclick_ids: list[str], map_keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in onclick_ids + map_keys:
        s = str(x or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _gather_expand_group_ids(dom: DomRoot, log: ProgressLogger) -> list[str]:
    oc_ids: list[str] = []
    mk: list[str] = []
    try:
        raw_oc = dom.evaluate(_EXPAND_GROUP_IDS_FROM_ONCLICK_JS)
        if isinstance(raw_oc, list):
            oc_ids = [str(x) for x in raw_oc if x is not None and str(x).strip()]
    except Exception as e:
        log.warn("JS expand id scan failed", {"error": str(e), "errorType": type(e).__name__})
    try:
        raw_mk = dom.evaluate(_COLLAPSE_EXPAND_MAP_KEYS_JS)
        if isinstance(raw_mk, list):
            mk = [str(x) for x in raw_mk if x is not None and str(x).strip()]
    except Exception as e:
        log.warn("collapseExpandMap key scan failed", {"error": str(e), "errorType": type(e).__name__})
    return _merge_expand_group_ids(oc_ids, mk)


def _meaningful_body_growth(start_text_len: int, start_html_len: int, end_text_len: int, end_html_len: int) -> bool:
    dt = end_text_len - start_text_len
    dh = end_html_len - start_html_len
    return dt >= MEANINGFUL_BODY_TEXT_DELTA or dh >= MEANINGFUL_BODY_HTML_DELTA


def _run_js_expand_invocations_for_pass(
    page: Page,
    dom: DomRoot,
    canonical_target_url: str,
    mcg_code: str,
    mcg_title: str,
    log: ProgressLogger,
    progress: dict[str, Any],
    warnings: list[str],
    *,
    pass_num: int,
    already_invoked_expand_group_ids: set[str],
    restore_after_unexpected_navigation: Callable[[], None],
    ordered_full_ids: Optional[list[str]] = None,
    on_after_gid: Optional[Callable[[], None]] = None,
) -> tuple[int, int]:
    """
    Call expandall/expand once per group id (first successful invocation only across all passes).

    Returns (new_unique_success_invocations_this_pass, skipped_already_invoked_this_pass).

    If ordered_full_ids is provided, skips a fresh DOM scan (caller already merged onclick + map keys).
    """
    ordered_full = ordered_full_ids if ordered_full_ids is not None else _gather_expand_group_ids(dom, log)
    ordered = ordered_full[:MAX_JS_EXPAND_IDS_PER_PASS]

    new_invocations = 0
    skipped_already = 0

    if not ordered:
        return 0, 0

    for gid in ordered:
        try:
            if gid in already_invoked_expand_group_ids:
                skipped_already += 1
                log.info(
                    "skipped_already_invoked_group_id",
                    {"groupId": gid, "pass": pass_num},
                )
                continue

            url_before = _safe_page_url(page)
            try:
                ret = dom.evaluate(_EXPAND_INVOKE_GROUP_JS, gid)
            except Exception as e:
                log.warn(
                    "expand JS evaluate failed",
                    {"groupId": gid, "pass": pass_num, "error": str(e)},
                )
                continue
            if not isinstance(ret, dict) or not ret.get("ok"):
                log.info(
                    "expand JS noop or unsupported",
                    {"groupId": gid, "pass": pass_num, "detail": ret},
                )
                continue

            already_invoked_expand_group_ids.add(gid)
            new_invocations += 1

            log.info(
                "expand_js_invocation",
                {
                    "groupId": gid,
                    "pass": pass_num,
                    "fn": ret.get("fn"),
                    "urlBefore": url_before,
                },
            )

            page.wait_for_timeout(EXPAND_POST_CLICK_WAIT_MS)
            url_after = _safe_page_url(page)

            if not _same_target_page(url_after, canonical_target_url):
                warn = (
                    f"Unexpected navigation during JS expand — groupId={gid!r} — "
                    f"{url_before} -> {url_after}"
                )
                warnings.append(warn)
                progress["warning_count"] = len(warnings)
                log.error(
                    "Unexpected navigation during JS expand; restoring canonical guideline URL",
                    {
                        "groupId": gid,
                        "from": url_before,
                        "to": url_after,
                        "canonicalTargetUrl": canonical_target_url,
                    },
                )
                try:
                    restore_after_unexpected_navigation()
                except Exception as e:
                    warnings.append(f"Restore navigation failed after JS expand navigation: {e}")
                    progress["warning_count"] = len(warnings)
                break
        finally:
            if on_after_gid is not None:
                on_after_gid()

    if new_invocations:
        page.wait_for_timeout(int(220 + random.random() * 200))

    return new_invocations, skipped_already


def _gather_safe_expand_rows(dom: DomRoot) -> list[dict[str, Any]]:
    try:
        raw = dom.evaluate(_EXPAND_SAFE_CANDIDATES_JS)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for row in raw:
        if isinstance(row, dict) and row.get("xpath"):
            out.append(row)
    return out


def _text_blocked_for_expand(text: str) -> Optional[str]:
    tl = text.lower()
    for s in SKIP_LINK_TEXT_SNIPPETS:
        if s in tl:
            return f"blocked text contains {s!r}"
    return None


def collect_expand_candidates(root: DomRoot) -> tuple[list[tuple[Locator, dict[str, Any]]], int]:
    """
    Narrow expand controls: visible text must match Expand All / bracketed toggle / standalone Expand,
    scoped to main/content/body. Does not use broad document-wide expand* attribute selectors.
    """
    rows = _gather_safe_expand_rows(root)
    candidates: list[tuple[Locator, dict[str, Any]]] = []
    skipped = 0
    for row in rows:
        xp = str(row.get("xpath") or "")
        if not xp.startswith("/"):
            skipped += 1
            continue
        text = str(row.get("text") or "")
        href_raw = row.get("href")
        href = href_raw.strip() if isinstance(href_raw, str) else None
        if href == "":
            href = None
        br = _href_blocked_for_expand(href)
        if br:
            skipped += 1
            continue
        bt = _text_blocked_for_expand(text)
        if bt:
            skipped += 1
            continue
        loc = locator_for_xpath(root, xp)
        bbox = row.get("bbox")
        meta: dict[str, Any] = {
            "tag": row.get("tag"),
            "text": text,
            "href": href,
            "onclick": row.get("onclick"),
            "aria_expanded": row.get("aria_expanded"),
            "expand_kind": row.get("kind"),
            "xpath": xp,
            "bbox_js": bbox if isinstance(bbox, dict) else None,
        }
        candidates.append((loc, meta))
    return candidates, skipped


def _locator_bbox_playwright(loc: Locator) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    try:
        box = loc.bounding_box()
    except Exception as e:
        return False, None, f"bounding_box_error:{type(e).__name__}"
    if box is None:
        return False, None, "bounding_box_null"
    w = float(box.get("width") or 0)
    h = float(box.get("height") or 0)
    if w <= 0 or h <= 0:
        return False, box, "bounding_box_zero_size"
    return True, box, None


def _wait_for_target_document_ready(
    page: Page,
    *,
    expected_title: str,
    mcg_code: str,
    mcg_title: str,
    log: ProgressLogger,
    timeout_ms: int = 120_000,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    exp = expected_title.strip()
    while time.monotonic() < deadline:
        title = _safe_page_title(page)
        if exp and exp in title:
            log.info("Target page title matched expectation", {"pageTitle": title[:240]})
            return
        if page_has_target_content(page, mcg_code, mcg_title):
            log.info(
                "Target guideline markers detected in document body",
                {"pageTitle": title[:240]},
            )
            return
        page.wait_for_timeout(250)
    cur = _safe_page_url(page)
    tit = _safe_page_title(page)
    msg = (
        f"Timed out waiting for target guideline document "
        f"(expected title substring {exp!r} or body markers). url={cur!r} title={tit!r}"
    )
    log.error(msg, {"currentUrl": cur, "pageTitle": tit})
    raise RuntimeError(msg)


def _ensure_target_guideline_ready(
    page: Page,
    *,
    url_status: str,
    shell_or_canonical_url: str,
    search_code: Optional[str],
    mcg_code: str,
    mcg_title: str,
    log: ProgressLogger,
    progress: dict[str, Any],
    out_prefix: Optional[str] = None,
) -> tuple[bool, list[str], Optional[Frame], bool]:
    """
    Post-login checkpoint: load canonical URL or CareWeb shell + Quick Search.
    Returns (ok, target_detection_markers, resolved_target_frame, target_detected_in_frame) —
    canonical mode: (True, [], None, False).
    """
    progress["stage"] = "target_reload"
    if url_status == "search_page":
        progress["last_action"] = "goto_shell_after_login"
        log.step(
            "Loading CareWeb shell + Quick Search (post-login checkpoint)",
            {"shellUrl": shell_or_canonical_url, "searchCode": search_code},
        )
        _goto_domcontent_with_retries(
            page,
            shell_or_canonical_url,
            mcg_code,
            mcg_title,
            log,
            progress,
            require_body_target_markers=False,
        )
        ok, markers, tgt_fr, in_fr = run_quick_search_resolution(
            page,
            search_code=str(search_code or ""),
            mcg_code=mcg_code,
            mcg_title=mcg_title,
            log=log,
            progress=progress,
            out_prefix=out_prefix,
        )
        progress["current_url"] = _safe_page_url(page)
        return ok, markers, tgt_fr, in_fr

    progress["last_action"] = "goto_canonical_after_login"
    log.step(
        "Reloading canonical guideline URL (post-login checkpoint)",
        {"canonicalUrl": shell_or_canonical_url},
    )
    _goto_domcontent_with_retries(page, shell_or_canonical_url, mcg_code, mcg_title, log, progress)
    _wait_for_target_document_ready(page, expected_title=mcg_title, mcg_code=mcg_code, mcg_title=mcg_title, log=log)
    progress["current_url"] = _safe_page_url(page)
    return True, [], None, False


def count_remaining_expand_controls(root: DomRoot) -> tuple[int, list[str]]:
    previews: list[str] = []
    candidates, _ = collect_expand_candidates(root)
    count = len(candidates)
    for _, meta in candidates[:REMAINING_PREVIEW]:
        previews.append(_truncate(meta.get("text") or ""))
    return count, previews


def section_probe(root: DomRoot) -> dict[str, bool]:
    t = _body_inner_text(root)
    return {
        "has_admission": (
            "Clinical Indications for Admission to Inpatient Care" in t
            or "Care Planning - Inpatient Admission and Alternatives" in t
        ),
        "has_admission_indicated": "Admission is indicated" in t,
        "has_discharge": "Discharge" in t,
        "has_discharge_planning": "Discharge Planning" in t,
        "has_discharge_destination": "Discharge Destination" in t,
        "has_extended_stay": "Extended Stay" in t,
        "has_optimal_recovery_course": "Optimal Recovery Course" in t,
        "has_evidence_summary": "Evidence Summary" in t,
        "has_references": "References" in t,
        "has_footnotes": "Footnotes" in t,
        "has_codes": "Codes" in t,
    }


def _expand_verification_flags(root: DomRoot) -> dict[str, bool]:
    t = (_body_inner_text(root) or "").lower()
    return {
        "hemodynamic_instability_visible": "hemodynamic instability" in t,
        "severe_hypertension_visible": "severe hypertension" in t,
        "prolonged_cardiac_telemetry_visible": "prolonged cardiac telemetry" in t,
        "dangerous_arrhythmia_visible": "dangerous arrhythmia" in t,
    }


def _frame_snippets_for_diag(page: Page, preview_len: int = 500) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, fr in enumerate(page.frames):
        row: dict[str, Any] = {"frame_index": idx, "frame_url": "", "frame_name": "", "body_preview": ""}
        try:
            row["frame_url"] = fr.url
        except Exception:
            pass
        try:
            row["frame_name"] = fr.name or ""
        except Exception:
            pass
        try:
            row["body_preview"] = (_body_inner_text(fr) or "")[:preview_len]
        except Exception as e:
            row["body_preview"] = f"(error:{type(e).__name__})"
        rows.append(row)
    return rows


def _save_diagnostics(page: Page, out_prefix: str, reason: str, *, include_frame_bodies: bool = True) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    body = _body_inner_text(page)
    diag: dict[str, Any] = {
        "reason": reason,
        "url": page.url,
        "title": page.title(),
        "body_inner_text_length": len(body),
        "body_inner_text_preview": body[:4000],
        "frame_count": len(page.frames),
        "frame_urls": [fr.url for fr in page.frames],
    }
    if include_frame_bodies:
        diag["frame_bodies"] = _frame_snippets_for_diag(page, 500)
    p = AUDIT_DIR / f"{out_prefix}.capture-diagnostics.json"
    p.write_text(json.dumps(diag, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def run_expand_passes(
    page: Page,
    canonical_target_url: str,
    mcg_code: str,
    mcg_title: str,
    log: ProgressLogger,
    progress: dict[str, Any],
    started: float,
    warnings: list[str],
    terminal: Optional[CaptureTerminalReporter] = None,
    *,
    restore_after_unexpected_navigation: Callable[[], None],
    dom_root: Optional[DomRoot] = None,
    dom_root_supplier: Optional[Callable[[], DomRoot]] = None,
) -> tuple[int, int, str, dict[str, Any]]:
    """
    Expand collapsible sections using narrow, text-scoped controls only.

    Returns (passes_run, total_clicked, outcome, expand_phase_summary).

    outcome includes:
    - ok: normal early completion (e.g. Expand All cleared controls or deliberate halt without a specialized reason)
    - stopped_no_progress: no new JS ids, no UI expand clicks, no meaningful DOM/text growth in the pass
    - stopped_stable_body: body text + HTML lengths unchanged for two consecutive passes
    - stopped_max_passes: reached MAX_EXPAND_PASSES
    - aborted_unexpected_navigation / aborted_consecutive_timeouts
    """
    total_clicked = 0
    passes = 0
    outcome = "ok"
    consecutive_expand_failures = 0

    already_invoked_expand_group_ids: set[str] = set()
    total_expand_group_ids_seen: set[str] = set()
    already_invoked_skips_accum = 0
    max_passes_reached = False

    prev_pass_body_snapshot: Optional[tuple[int, int]] = None
    stable_same_body_passes = 0

    final_body_text_length = 0
    final_body_html_length = 0
    final_visible_expand_candidate_count = 0
    final_remaining_expand_control_count = 0

    for pass_num in range(1, MAX_EXPAND_PASSES + 1):
        root: DomRoot = (
            dom_root_supplier()
            if dom_root_supplier is not None
            else (dom_root if dom_root is not None else page)
        )
        passes = pass_num
        progress["stage"] = "expand"
        progress["expand_pass"] = pass_num
        progress["last_action"] = f"expand_pass_{pass_num}_started"
        update_elapsed(progress, started)

        log.step(f"Expand pass {pass_num} started")

        pass_start_text_len = len(_body_inner_text(root))
        pass_start_html_len = len(_body_inner_html(root))

        def _expand_tick(*, ui_clicks: int, force: bool) -> None:
            if terminal is None:
                return
            try:
                bt = len(_body_inner_text(root))
            except Exception:
                bt = pass_start_text_len
            disp = outcome if outcome != "ok" else "running"
            terminal.expand_emit(
                pass_num=pass_num,
                max_passes=MAX_EXPAND_PASSES,
                js_invoked_unique=len(already_invoked_expand_group_ids),
                js_groups_total_seen=len(total_expand_group_ids_seen),
                ui_clicks_this_pass=ui_clicks,
                body_text_delta=bt - pass_start_text_len,
                outcome_so_far=disp,
                force=force,
            )

        if terminal is not None:
            _expand_tick(ui_clicks=0, force=True)

        merged_seen_this_scan = _gather_expand_group_ids(root, log)
        total_expand_group_ids_seen.update(str(x).strip() for x in merged_seen_this_scan if str(x).strip())

        js_invoked_new, skipped_already = _run_js_expand_invocations_for_pass(
            page,
            root,
            canonical_target_url,
            mcg_code,
            mcg_title,
            log,
            progress,
            warnings,
            pass_num=pass_num,
            already_invoked_expand_group_ids=already_invoked_expand_group_ids,
            restore_after_unexpected_navigation=restore_after_unexpected_navigation,
            ordered_full_ids=merged_seen_this_scan,
            on_after_gid=(lambda: _expand_tick(ui_clicks=0, force=False)) if terminal is not None else None,
        )
        already_invoked_skips_accum += skipped_already

        if terminal is not None:
            _expand_tick(ui_clicks=0, force=True)

        if js_invoked_new:
            total_clicked += js_invoked_new
            progress["expand_click_count"] = total_clicked
            progress["last_action"] = f"expand_js_pass_{pass_num}_count_{js_invoked_new}"
            consecutive_expand_failures = 0
            update_elapsed(progress, started)
            log.info(
                "Expand JS invocations completed for pass",
                {
                    "pass": pass_num,
                    "newUniqueJsInvocations": js_invoked_new,
                    "skippedAlreadyInvoked": skipped_already,
                    "totalExpandActions": total_clicked,
                },
            )

        candidates, skipped_count = collect_expand_candidates(root)
        candidate_count = len(candidates)

        log.info(
            "Expand candidate summary",
            {
                "pass": pass_num,
                "newUniqueJsInvocationsThisPass": js_invoked_new,
                "skippedAlreadyInvokedThisPass": skipped_already,
                "candidateCount": candidate_count,
                "skippedDuringBuild": skipped_count,
                "maxClicksThisPass": MAX_EXPAND_CLICKS_PER_PASS,
                "canonicalTargetUrl": canonical_target_url,
            },
        )

        clicked_this_pass = 0
        warning_this_pass = 0
        stop_expand = False

        for el, meta in candidates[:MAX_EXPAND_CLICKS_PER_PASS]:
            if stop_expand:
                break

            text = meta.get("text") or ""
            label_short = _truncate(text)
            url_before_click = _safe_page_url(page)

            visible_pw = False
            try:
                visible_pw = el.is_visible(timeout=600)
            except Exception:
                visible_pw = False
            if not visible_pw:
                warning_this_pass += 1
                log.warn(
                    "Skipping expand control (not visible to Playwright)",
                    {"text": label_short, "urlBefore": url_before_click},
                )
                continue

            bbox_ok, bbox_pw, bbox_reason = _locator_bbox_playwright(el)
            if not bbox_ok:
                warning_this_pass += 1
                log.warn(
                    "Skipping expand control (bounding box not clickable)",
                    {"text": label_short, "bbox": bbox_pw, "reason": bbox_reason},
                )
                continue

            oc_full = meta.get("onclick") if isinstance(meta.get("onclick"), str) else ""
            onclick_trunc = (oc_full[:320] + "…") if len(oc_full) > 320 else oc_full
            ctrl_snapshot = {
                "text": label_short,
                "href": meta.get("href"),
                "onclick": onclick_trunc,
                "expandKind": meta.get("expand_kind"),
                "visible": visible_pw,
                "boundingBox": bbox_pw,
                "boundingBoxJs": meta.get("bbox_js"),
            }

            log.info(
                "Expand click planned",
                {"urlBefore": url_before_click, "control": ctrl_snapshot},
            )

            try:
                el.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass

            try:
                el.click(timeout=8000)
            except TimeoutError as e:
                consecutive_expand_failures += 1
                warning_this_pass += 1
                msg = str(e)
                warnings.append(f"Expand click timed out — {_truncate(text)} — {msg}")
                progress["warning_count"] = len(warnings)
                log.warn(
                    "Expand click timed out",
                    {
                        "text": label_short,
                        "urlBefore": url_before_click,
                        "consecutiveFailures": consecutive_expand_failures,
                        "control": ctrl_snapshot,
                    },
                )
                if consecutive_expand_failures >= 2:
                    log.warn(
                        "Stopping expand phase after consecutive click timeouts",
                        {"pass": pass_num, "consecutiveFailures": consecutive_expand_failures},
                    )
                    outcome = "aborted_consecutive_timeouts"
                    stop_expand = True
                continue
            except Exception as e:
                warning_this_pass += 1
                msg = str(e)
                warnings.append(f"Expand click failed — {_truncate(text)} — {msg}")
                progress["warning_count"] = len(warnings)
                log.warn(
                    "Expand click failed",
                    {"text": label_short, "reason": msg, "control": ctrl_snapshot},
                )
                continue

            clicked_this_pass += 1
            total_clicked += 1
            consecutive_expand_failures = 0
            progress["expand_click_count"] = total_clicked
            progress["last_action"] = f"clicked_expand_{total_clicked}"
            update_elapsed(progress, started)
            if terminal is not None:
                _expand_tick(ui_clicks=clicked_this_pass, force=True)

            page.wait_for_timeout(EXPAND_POST_CLICK_WAIT_MS)
            url_after = _safe_page_url(page)

            log.info(
                "Expand click completed",
                {
                    "text": label_short,
                    "urlBefore": url_before_click,
                    "urlAfter": url_after,
                    "control": ctrl_snapshot,
                },
            )

            if not _same_target_page(url_after, canonical_target_url):
                warn = (
                    f"Unexpected navigation during expand — {_truncate(text)} — "
                    f"{url_before_click} -> {url_after}"
                )
                warnings.append(warn)
                progress["warning_count"] = len(warnings)
                log.error(
                    "Unexpected navigation during expand; restoring canonical guideline URL and stopping expand phase",
                    {
                        "from": url_before_click,
                        "to": url_after,
                        "canonicalTargetUrl": canonical_target_url,
                        "control": ctrl_snapshot,
                    },
                )
                try:
                    restore_after_unexpected_navigation()
                except Exception as e:
                    warnings.append(f"Restore navigation failed after stray navigation: {e}")
                    progress["warning_count"] = len(warnings)
                    log.error(
                        "Failed to restore canonical URL after unexpected navigation",
                        {"error": str(e), "errorType": type(e).__name__},
                    )
                outcome = "aborted_unexpected_navigation"
                stop_expand = True
                break

            if meta.get("expand_kind") == "expand_all":
                remaining_after_all, _ = count_remaining_expand_controls(root)
                if remaining_after_all == 0:
                    log.info(
                        "Expand All succeeded; no remaining narrow expand controls — stopping expand phase",
                        {"pass": pass_num, "remainingExpandControlCount": remaining_after_all},
                    )
                    stop_expand = True
                    break

        update_elapsed(progress, started)
        remaining_count, _ = count_remaining_expand_controls(root)
        candidates_end, _skipped_end = collect_expand_candidates(root)
        visible_expand_candidate_count = len(candidates_end)

        body_text_length = len(_body_inner_text(root))
        body_html_length = len(_body_inner_html(root))

        final_body_text_length = body_text_length
        final_body_html_length = body_html_length
        final_visible_expand_candidate_count = visible_expand_candidate_count
        final_remaining_expand_control_count = remaining_count

        progress["current_url"] = _safe_page_url(page)

        meaningful_growth = _meaningful_body_growth(
            pass_start_text_len,
            pass_start_html_len,
            body_text_length,
            body_html_length,
        )
        had_progress = js_invoked_new > 0 or clicked_this_pass > 0 or meaningful_growth

        log.success(
            f"Expand pass {pass_num} completed",
            {
                "pass": pass_num,
                "newUniqueJsInvocationsThisPass": js_invoked_new,
                "skippedAlreadyInvokedThisPass": skipped_already,
                "new_group_ids_invoked_this_pass": js_invoked_new,
                "candidateCount": candidate_count,
                "clickedThisPass": clicked_this_pass,
                "skippedCount": skipped_count,
                "warningsThisPass": warning_this_pass,
                "totalClicked": total_clicked,
                "currentUrl": progress["current_url"],
                "remainingExpandControlCount": remaining_count,
                "visibleExpandCandidateCount": visible_expand_candidate_count,
                "bodyTextLength": body_text_length,
                "bodyHtmlLength": body_html_length,
                "meaningfulDomGrowthThisPass": meaningful_growth,
                "hadProgressThisPass": had_progress,
                "expandOutcomeSoFar": outcome,
            },
        )

        if terminal is not None:
            _expand_tick(ui_clicks=clicked_this_pass, force=True)

        if stop_expand:
            log.warn("Expand phase halted early", {"pass": pass_num, "outcome": outcome})
            break

        snap = (body_text_length, body_html_length)
        if prev_pass_body_snapshot is not None and snap == prev_pass_body_snapshot:
            stable_same_body_passes += 1
        else:
            stable_same_body_passes = 0
        prev_pass_body_snapshot = snap

        if stable_same_body_passes >= 2:
            outcome = "stopped_stable_body"
            log.info(
                "Stopping expand phase: body text/HTML lengths unchanged for consecutive passes",
                {
                    "pass": pass_num,
                    "stableSameBodyPasses": stable_same_body_passes,
                    "bodyTextLength": body_text_length,
                    "bodyHtmlLength": body_html_length,
                },
            )
            break

        if pass_num >= MAX_EXPAND_PASSES:
            outcome = "stopped_max_passes"
            max_passes_reached = True
            log.info(
                "Stopping expand phase: max expand passes reached",
                {"pass": pass_num, "maxExpandPasses": MAX_EXPAND_PASSES},
            )
            break

        if not had_progress:
            outcome = "stopped_no_progress"
            log.info(
                "Stopping expand phase: no progress this pass "
                "(no new JS group IDs, no UI expand clicks, no meaningful DOM/text growth)",
                {"pass": pass_num},
            )
            break

        page.wait_for_timeout(int(500 + random.random() * 500))
        scroll_full_page(root, page, log, progress, started)

    remaining_final = final_remaining_expand_control_count
    phase_summary = {
        "expand_phase_outcome": outcome,
        "total_expand_group_ids_seen": len(total_expand_group_ids_seen),
        "total_expand_group_ids_invoked": len(already_invoked_expand_group_ids),
        "already_invoked_skips": already_invoked_skips_accum,
        "max_passes_reached": max_passes_reached,
        "body_text_length": final_body_text_length,
        "body_html_length": final_body_html_length,
        "visible_expand_candidate_count": final_visible_expand_candidate_count,
        "remaining_expand_control_count": remaining_final,
    }

    log.step("Expand phase complete", phase_summary)
    log.info("expand_phase_transition", phase_summary)

    if terminal is not None:
        terminal.expand_finish_line()

    return passes, total_clicked, outcome, phase_summary


def run_capture(
    *,
    mcg_code: str,
    mcg_title: str,
    url: str,
    out_prefix: str,
    capture_definitions: bool = True,
    max_definition_triggers: int = 300,
    definition_recursion_depth: int = 2,
    definition_click_timeout_ms: int = 3000,
    allow_needs_review_exit_zero: bool = False,
    progress_enabled: bool = True,
    progress_style: str = "bar",
    url_status: str = "canonical",
    search_code: Optional[str] = None,
    resolver_only: bool = False,
) -> int:
    started_mono = time.monotonic()
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    progress = fresh_capture_progress(started_iso)
    warnings: list[str] = []

    terminal: Optional[CaptureTerminalReporter] = None
    if progress_enabled:
        terminal = CaptureTerminalReporter(
            enabled=True,
            requested_style=progress_style,
            mcg_code=mcg_code,
            mcg_title=mcg_title,
            max_definition_triggers=max_definition_triggers,
        )

    scope = f"capture:{mcg_code}"
    log = ProgressLogger(scope)

    usk = _norm_url_status(url_status)
    sc = str(search_code or "").strip()
    if usk == "search_page" and not sc:
        raise ValueError("url_status=search_page requires a non-empty search_code (--search-code)")
    target_detection_markers: list[str] = []
    login_check_mode = usk
    quick_search_attempted = False
    search_shell_detected = False
    search_shell_markers: list[str] = []
    search_shell_frame_url = ""
    search_shell_frame_name = ""
    search_shell_frame_index = -1
    frame_count = 0
    frame_urls: list[str] = []
    guideline_frame: Optional[Frame] = None
    target_detected_in_frame = False
    resolved_target_frame_url = ""
    resolved_target_frame_name = ""
    resolved_target_frame_index = -1
    content_root: Optional[DomRoot] = None

    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    pw: Optional[Playwright] = None
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    heartbeat: Optional[Heartbeat] = None

    raw_path = RAW_HTML_DIR / f"{out_prefix}.full.raw.html"
    expanded_html_path = RAW_HTML_DIR / f"{out_prefix}.full.expanded.html"
    expanded_txt_path = RAW_HTML_DIR / f"{out_prefix}.full.expanded.txt"
    defs_json_path_default = RAW_HTML_DIR / f"{out_prefix}.definitions.raw.json"

    manifest_path = RAW_HTML_DIR / f"{out_prefix}.capture-manifest.json"
    summary_path = AUDIT_DIR / f"{out_prefix}.capture-summary.md"
    screenshot_path = AUDIT_DIR / f"{out_prefix}.capture-full-page.png"

    def _snapshot() -> dict[str, Any]:
        with _progress_lock:
            update_elapsed(progress, started_mono)
            return {
                "stage": progress.get("stage", ""),
                "elapsedSeconds": progress.get("elapsed_seconds", 0),
                "expandPass": progress.get("expand_pass", 0),
                "totalClicked": progress.get("expand_click_count", 0),
                "warningCount": len(warnings),
                "lastAction": progress.get("last_action", ""),
            }

    exit_code = 1
    try:
        log.step("Opening browser", {"userDataDir": str(PROFILE_DIR)})
        if terminal is not None:
            terminal.phase("browser_start", PROFILE_DIR.name)
        progress["stage"] = "browser_launch"
        progress["last_action"] = "launch_persistent_context"

        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        if not progress_enabled:
            heartbeat = Heartbeat(10.0, log, _snapshot)
            heartbeat.start()

        log.step("Opening page", {"url": url, "urlStatus": usk})
        progress["stage"] = "navigate"
        progress["last_action"] = "goto"
        assert page is not None
        nav_response = _goto_domcontent_with_retries(
            page,
            url,
            mcg_code,
            mcg_title,
            log,
            progress,
            require_body_target_markers=(usk != "search_page"),
        )

        if usk == "search_page":
            shell_scan = careweb_search_shell_scan(page, log)
            search_shell_detected = bool(shell_scan.get("detected"))
            search_shell_markers = list(shell_scan.get("markers") or [])
            search_shell_frame_url = str(shell_scan.get("frame_url") or "")
            search_shell_frame_name = str(shell_scan.get("frame_name") or "")
            search_shell_frame_index = int(shell_scan.get("frame_index") if shell_scan.get("frame_index") is not None else -1)
            frame_count = int(shell_scan.get("frame_count") or 0)
            frame_urls = list(shell_scan.get("frame_urls") or [])
            target_detected_initial = search_shell_detected
        else:
            frame_count, frame_urls = _frame_urls_snapshot(page)
            target_detected_initial = page_has_target_content(page, mcg_code, mcg_title)

        checkpoint: dict[str, Any] = {
            "currentUrl": _safe_page_url(page),
            "pageTitle": _safe_page_title(page),
            "targetContentDetected": target_detected_initial,
            "urlStatus": usk,
            "searchShellDetected": search_shell_detected if usk == "search_page" else None,
            "searchShellMarkers": search_shell_markers if usk == "search_page" else None,
            "searchShellFrameUrl": search_shell_frame_url if usk == "search_page" else None,
            "searchShellFrameIndex": search_shell_frame_index if usk == "search_page" else None,
            "frameCount": frame_count,
            "frameUrls": frame_urls,
        }
        if nav_response is not None:
            checkpoint["responseStatus"] = nav_response.status
            checkpoint["responseUrl"] = nav_response.url
        log.info("Navigation checkpoint", checkpoint)

        progress["last_action"] = "goto_complete"
        progress["stage"] = "raw_capture" if target_detected_initial else "login_check"

        progress["current_url"] = _safe_page_url(page) or url

        if usk == "search_page":
            login_ok = search_shell_detected
        else:
            login_ok = page_has_login_markers(page, mcg_code, mcg_title)

        if not login_ok:
            print(
                "Please log in in the opened browser window. "
                "For search_page mode, return to the CareWeb shell (index) with Quick Search visible if needed; "
                "for canonical mode, navigate to the target guideline if needed. "
                "Then press Enter in this terminal to continue.",
                flush=True,
            )
            input()
            if usk == "search_page":
                shell_scan = careweb_search_shell_scan(page, log)
                search_shell_detected = bool(shell_scan.get("detected"))
                search_shell_markers = list(shell_scan.get("markers") or [])
                search_shell_frame_url = str(shell_scan.get("frame_url") or "")
                search_shell_frame_name = str(shell_scan.get("frame_name") or "")
                search_shell_frame_index = int(shell_scan.get("frame_index") if shell_scan.get("frame_index") is not None else -1)
                frame_count = int(shell_scan.get("frame_count") or 0)
                frame_urls = list(shell_scan.get("frame_urls") or [])
                login_ok = search_shell_detected
            else:
                login_ok = page_has_login_markers(page, mcg_code, mcg_title)
            if not login_ok:
                if usk == "search_page":
                    dpath = _save_diagnostics(page, out_prefix, "search_shell_not_ready_after_wait")
                    fc, fus = _frame_urls_snapshot(page)
                    _write_failed_capture_manifest(
                        manifest_path=manifest_path,
                        mcg_code=mcg_code,
                        mcg_title=mcg_title,
                        source_url=url,
                        url_status=usk,
                        search_code=sc,
                        out_prefix=out_prefix,
                        reasons=["search_shell_not_ready"],
                        target_detected=False,
                        target_detection_markers=[],
                        search_shell_detected=False,
                        search_shell_markers=search_shell_markers,
                        login_check_mode=login_check_mode,
                        quick_search_attempted=False,
                        search_shell_frame_url=search_shell_frame_url,
                        search_shell_frame_index=search_shell_frame_index,
                        frame_count=fc,
                        frame_urls=fus,
                    )
                    log.error(
                        "CareWeb search shell markers not found after login wait",
                        {
                            "diagnostics": str(dpath),
                            "markersAttempted": search_shell_markers,
                            "frameUrls": fus,
                        },
                    )
                    return 1
                dpath = _save_diagnostics(page, out_prefix, "login_markers_missing_after_wait")
                msg = (
                    f"Expected CareWeb session markers not found after login wait. "
                    f"Diagnostics: {dpath}"
                )
                log.error(msg, {"url": page.url})
                raise RuntimeError(msg)

        if usk == "search_page":
            quick_search_attempted = True
        ok_ready, markers_chk, guideline_frame, target_detected_in_frame = _ensure_target_guideline_ready(
            page,
            url_status=usk,
            shell_or_canonical_url=url,
            search_code=sc if usk == "search_page" else None,
            mcg_code=mcg_code,
            mcg_title=mcg_title,
            log=log,
            progress=progress,
            out_prefix=out_prefix,
        )
        target_detection_markers = markers_chk
        if guideline_frame is not None:
            try:
                resolved_target_frame_url = guideline_frame.url
            except Exception:
                resolved_target_frame_url = ""
            try:
                resolved_target_frame_name = guideline_frame.name or ""
            except Exception:
                resolved_target_frame_name = ""
            for _fi, _fr in enumerate(page.frames):
                if _fr == guideline_frame:
                    resolved_target_frame_index = _fi
                    break
        frame_count, frame_urls = _frame_urls_snapshot(page)
        if not ok_ready:
            qsf = str(progress.get("quick_search_failure") or "")
            if usk == "search_page" and qsf == "quick_search_input_not_enabled":
                fail_reason = "quick_search_input_not_enabled"
                reasons = ["quick_search_input_not_enabled"]
            elif usk == "search_page" and qsf == "quick_search_input_not_found":
                fail_reason = "quick_search_input_not_found"
                reasons = ["quick_search_input_not_found"]
            elif usk == "search_page" and qsf == "search_result_guideline_link_not_found":
                fail_reason = "search_result_guideline_link_not_found"
                reasons = ["search_result_guideline_link_not_found"]
            else:
                fail_reason = "search_target_not_detected_after_search"
                reasons = ["search_target_not_detected"]
            fail_diag = _save_diagnostics(page, out_prefix, fail_reason, include_frame_bodies=True)
            qsc = int(progress.get("quick_search_candidate_count") if progress.get("quick_search_candidate_count") is not None else -1)
            _write_failed_capture_manifest(
                manifest_path=manifest_path,
                mcg_code=mcg_code,
                mcg_title=mcg_title,
                source_url=url,
                url_status=usk,
                search_code=sc,
                out_prefix=out_prefix,
                reasons=reasons,
                target_detected=False,
                target_detection_markers=target_detection_markers,
                search_shell_detected=search_shell_detected if usk == "search_page" else False,
                search_shell_markers=search_shell_markers if usk == "search_page" else [],
                login_check_mode=login_check_mode,
                quick_search_attempted=quick_search_attempted,
                search_shell_frame_url=search_shell_frame_url if usk == "search_page" else "",
                search_shell_frame_index=int(search_shell_frame_index) if usk == "search_page" else -1,
                resolved_target_frame_url=resolved_target_frame_url,
                resolved_target_frame_index=int(resolved_target_frame_index),
                frame_count=frame_count,
                frame_urls=frame_urls,
                target_detected_in_frame=target_detected_in_frame,
                quick_search_candidate_count=qsc,
                quick_search_selected_frame_url=str(progress.get("quick_search_selected_frame_url") or ""),
                quick_search_selected_input_summary=(
                    progress.get("quick_search_selected_input_summary")
                    if isinstance(progress.get("quick_search_selected_input_summary"), dict)
                    else {}
                ),
                quick_search_fill_method=str(progress.get("quick_search_fill_method") or ""),
                quick_search_submit_method=str(progress.get("quick_search_submit_method") or ""),
                quick_search_failure=qsf,
                search_results_detected=bool(progress.get("search_results_detected"))
                if usk == "search_page"
                else False,
                search_result_rows=(
                    list(progress.get("search_result_rows"))[:50]
                    if isinstance(progress.get("search_result_rows"), list)
                    else []
                ),
                selected_search_result=(
                    dict(progress.get("selected_search_result"))
                    if isinstance(progress.get("selected_search_result"), dict)
                    else {}
                ),
                selected_search_result_reason=str(progress.get("selected_search_result_reason") or ""),
                clicked_search_result=bool(progress.get("clicked_search_result")) if usk == "search_page" else False,
            )
            log.error(
                "Capture failed before guideline target was ready",
                {
                    "reasons": reasons,
                    "markersSeen": target_detection_markers,
                    "quickSearchFailure": qsf,
                    "diagnostics": str(fail_diag),
                },
            )
            return 1

        assert page is not None
        content_root = guideline_frame if guideline_frame is not None else page

        if resolver_only:
            rmarkers = list(target_detection_markers)
            if usk != "search_page":
                _, rstrict = search_page_strict_target_ready(page, mcg_code, mcg_title)
                rmarkers = list(rstrict)
            td_ok_res, _ = search_page_strict_target_ready(page, mcg_code, mcg_title)
            td_display = bool(td_ok_res) if usk == "search_page" else True
            fc_ro, fus_ro = _frame_urls_snapshot(page)
            resolver_blob: dict[str, Any] = {
                "mcg_code": mcg_code,
                "mcg_title": mcg_title,
                "source_url": url,
                "url_status": usk,
                "search_code": sc if usk == "search_page" else "",
                "resolved_by": "quick_search" if usk == "search_page" else "canonical_url",
                "resolver_only": True,
                "capture_status": "pass",
                "capture_status_reasons": [],
                "target_detected": td_display,
                "target_detected_in_frame": bool(target_detected_in_frame) if usk == "search_page" else False,
                "target_detection_markers": rmarkers,
                "login_check_mode": login_check_mode,
                "search_shell_detected": search_shell_detected if usk == "search_page" else False,
                "search_shell_markers": list(search_shell_markers) if usk == "search_page" else [],
                "search_shell_frame_url": search_shell_frame_url if usk == "search_page" else "",
                "search_shell_frame_name": search_shell_frame_name if usk == "search_page" else "",
                "search_shell_frame_index": int(search_shell_frame_index) if usk == "search_page" else -1,
                "resolved_target_frame_url": resolved_target_frame_url,
                "resolved_target_frame_name": resolved_target_frame_name,
                "resolved_target_frame_index": int(resolved_target_frame_index),
                "frame_count": int(fc_ro),
                "frame_urls": list(fus_ro),
                "quick_search_attempted": quick_search_attempted,
                "quick_search_candidate_count": int(progress.get("quick_search_candidate_count") or -1)
                if usk == "search_page"
                else -1,
                "quick_search_selected_frame_url": str(progress.get("quick_search_selected_frame_url") or "")
                if usk == "search_page"
                else "",
                "quick_search_selected_input_summary": (
                    dict(progress.get("quick_search_selected_input_summary"))
                    if usk == "search_page" and isinstance(progress.get("quick_search_selected_input_summary"), dict)
                    else {}
                ),
                "quick_search_fill_method": str(progress.get("quick_search_fill_method") or "")
                if usk == "search_page"
                else "",
                "quick_search_submit_method": str(progress.get("quick_search_submit_method") or "")
                if usk == "search_page"
                else "",
                "search_results_detected": bool(progress.get("search_results_detected"))
                if usk == "search_page"
                else False,
                "search_result_rows": (
                    list(progress.get("search_result_rows"))[:50]
                    if usk == "search_page" and isinstance(progress.get("search_result_rows"), list)
                    else []
                ),
                "selected_search_result": (
                    dict(progress.get("selected_search_result"))
                    if usk == "search_page" and isinstance(progress.get("selected_search_result"), dict)
                    else {}
                ),
                "selected_search_result_reason": str(progress.get("selected_search_result_reason") or "")
                if usk == "search_page"
                else "",
                "clicked_search_result": bool(progress.get("clicked_search_result")) if usk == "search_page" else False,
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "raw_html_path": "",
                "expanded_html_path": "",
                "expanded_text_path": "",
                "definition_capture": {
                    "enabled": False,
                    "schema_version": "",
                    "definition_count": 0,
                    "popup_count": 0,
                    "citation_count": 0,
                    "footnote_count": 0,
                    "context_count": 0,
                    "trigger_count": 0,
                    "audit_path": "",
                    "definitions_json_path": "",
                    "popups_json_path": "",
                    "popups_html_path": "",
                    "popup_triggers_jsonl_path": "",
                    "popup_failures_jsonl_path": "",
                },
            }
            try:
                manifest_path.write_text(json.dumps(resolver_blob, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                log.warn("Resolver-only manifest write failed", {"error": str(exc)})

            log.success(
                "Resolver-only mode: guideline target verified",
                {"markers": rmarkers, "urlStatus": usk},
            )
            if terminal is not None:
                terminal.phase("resolver_only_complete", "ok")
            print(
                json.dumps(
                    {
                        "resolver_only": True,
                        "ok": True,
                        "url_status": usk,
                        "search_code": sc,
                        "login_check_mode": login_check_mode,
                        "search_shell_detected": search_shell_detected if usk == "search_page" else False,
                        "search_shell_markers": list(search_shell_markers) if usk == "search_page" else [],
                        "search_shell_frame_url": search_shell_frame_url if usk == "search_page" else "",
                        "search_shell_frame_index": int(search_shell_frame_index) if usk == "search_page" else -1,
                        "resolved_target_frame_url": resolved_target_frame_url,
                        "resolved_target_frame_index": int(resolved_target_frame_index),
                        "frame_count": int(fc_ro),
                        "frame_urls": list(fus_ro),
                        "target_detected_in_frame": bool(target_detected_in_frame) if usk == "search_page" else False,
                        "quick_search_attempted": quick_search_attempted,
                        "quick_search_candidate_count": int(progress.get("quick_search_candidate_count") or -1)
                        if usk == "search_page"
                        else -1,
                        "quick_search_selected_frame_url": str(progress.get("quick_search_selected_frame_url") or "")
                        if usk == "search_page"
                        else "",
                        "quick_search_selected_input_summary": (
                            dict(progress.get("quick_search_selected_input_summary"))
                            if usk == "search_page"
                            and isinstance(progress.get("quick_search_selected_input_summary"), dict)
                            else {}
                        ),
                        "quick_search_fill_method": str(progress.get("quick_search_fill_method") or "")
                        if usk == "search_page"
                        else "",
                        "quick_search_submit_method": str(progress.get("quick_search_submit_method") or "")
                        if usk == "search_page"
                        else "",
                        "search_results_detected": bool(progress.get("search_results_detected"))
                        if usk == "search_page"
                        else False,
                        "search_result_rows": (
                            list(progress.get("search_result_rows"))[:30]
                            if usk == "search_page" and isinstance(progress.get("search_result_rows"), list)
                            else []
                        ),
                        "selected_search_result": (
                            dict(progress.get("selected_search_result"))
                            if usk == "search_page" and isinstance(progress.get("selected_search_result"), dict)
                            else {}
                        ),
                        "selected_search_result_reason": str(progress.get("selected_search_result_reason") or "")
                        if usk == "search_page"
                        else "",
                        "clicked_search_result": bool(progress.get("clicked_search_result"))
                        if usk == "search_page"
                        else False,
                        "target_detected": td_display,
                        "resolved_by": resolver_blob["resolved_by"],
                        "target_detection_markers": rmarkers,
                        "manifest_path": str(manifest_path.relative_to(PROJECT_ROOT)),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )
            return 0
        if terminal is not None:
            terminal.phase("login_or_target_ready", _safe_page_url(page)[:120])

        assert content_root is not None

        progress["stage"] = "raw_capture"
        progress["last_action"] = "save_raw_html"
        log.step("Saving raw HTML (pre-expand)")
        raw_html = content_root.content()
        raw_path.write_text(raw_html, encoding="utf-8")
        if terminal is not None:
            terminal.phase("raw_html_saved", str(raw_path.relative_to(PROJECT_ROOT)))

        if terminal is not None:
            terminal.phase("expand_phase", "narrow expand controls + JS group ids")

        def _restore_after_unexpected_navigation() -> None:
            nonlocal guideline_frame, content_root, target_detected_in_frame
            nonlocal resolved_target_frame_url, resolved_target_frame_name, resolved_target_frame_index
            nonlocal frame_count, frame_urls
            if usk == "search_page":
                ok_r, _mk, new_fr, in_fr = restore_search_page_target(
                    page,
                    shell_url=url,
                    search_code=sc,
                    mcg_code=mcg_code,
                    mcg_title=mcg_title,
                    log=log,
                    progress=progress,
                    out_prefix=out_prefix,
                )
                if not ok_r:
                    raise RuntimeError(
                        "restore_search_page_target_failed: strict markers not found after unexpected navigation"
                    )
                guideline_frame = new_fr
                target_detected_in_frame = in_fr
                resolved_target_frame_url, resolved_target_frame_name = "", ""
                resolved_target_frame_index = -1
                if new_fr is not None:
                    try:
                        resolved_target_frame_url = new_fr.url
                    except Exception:
                        pass
                    try:
                        resolved_target_frame_name = new_fr.name or ""
                    except Exception:
                        pass
                    for _fi, _fr in enumerate(page.frames):
                        if _fr == new_fr:
                            resolved_target_frame_index = _fi
                            break
                content_root = guideline_frame if guideline_frame is not None else page
                frame_count, frame_urls = _frame_urls_snapshot(page)
                return
            _goto_domcontent_with_retries(page, url, mcg_code, mcg_title, log, progress)

        expand_dom: Optional[DomRoot] = guideline_frame if guideline_frame is not None else None
        passes, clicks, expand_outcome, expand_phase_summary = run_expand_passes(
            page,
            url,
            mcg_code,
            mcg_title,
            log,
            progress,
            started_mono,
            warnings,
            terminal=terminal,
            restore_after_unexpected_navigation=_restore_after_unexpected_navigation,
            dom_root=expand_dom if usk != "search_page" else None,
            dom_root_supplier=(
                (lambda: guideline_frame if guideline_frame is not None else page) if usk == "search_page" else None
            ),
        )

        if expand_outcome.startswith("aborted_"):
            log.warn(
                "Expand phase ended with an aborted outcome; restoring guideline before continuing",
                {"expandOutcome": expand_outcome, "url": url, "urlStatus": usk},
            )
            try:
                if usk == "search_page":
                    ok_pe, _mk, new_fr2, in_fr2 = restore_search_page_target(
                        page,
                        shell_url=url,
                        search_code=sc,
                        mcg_code=mcg_code,
                        mcg_title=mcg_title,
                        log=log,
                        progress=progress,
                        out_prefix=out_prefix,
                    )
                    if not ok_pe:
                        raise RuntimeError("post_expand_search_restore_failed")
                    guideline_frame = new_fr2
                    target_detected_in_frame = in_fr2
                    resolved_target_frame_url, resolved_target_frame_name = "", ""
                    resolved_target_frame_index = -1
                    if new_fr2 is not None:
                        try:
                            resolved_target_frame_url = new_fr2.url
                        except Exception:
                            pass
                        try:
                            resolved_target_frame_name = new_fr2.name or ""
                        except Exception:
                            pass
                        for _fi, _fr in enumerate(page.frames):
                            if _fr == new_fr2:
                                resolved_target_frame_index = _fi
                                break
                    content_root = guideline_frame if guideline_frame is not None else page
                    frame_count, frame_urls = _frame_urls_snapshot(page)
                else:
                    _goto_domcontent_with_retries(page, url, mcg_code, mcg_title, log, progress)
            except Exception as e:
                w = f"Post-expand reload failed after outcome={expand_outcome!r}: {e}"
                warnings.append(w)
                progress["warning_count"] = len(warnings)
                log.error(w, {"errorType": type(e).__name__})
            if usk == "search_page":
                ok_cont, _ = search_page_strict_target_ready(page, mcg_code, mcg_title)
                if not ok_cont:
                    msg = (
                        f"Capture cannot continue after expand phase outcome={expand_outcome!r}: "
                        f"search-page strict target markers not detected (url={_safe_page_url(page)!r})."
                    )
                    log.error(msg)
                    raise RuntimeError(msg)
            elif not page_has_target_content(page, mcg_code, mcg_title):
                msg = (
                    f"Capture cannot continue after expand phase outcome={expand_outcome!r}: "
                    f"target guideline content was not detected (url={_safe_page_url(page)!r})."
                )
                log.error(msg)
                raise RuntimeError(msg)
        elif expand_outcome != "ok":
            log.info(
                "Expand phase stopped with a bounded outcome; continuing toward definition capture when enabled",
                {"expandOutcome": expand_outcome, **expand_phase_summary},
            )

        progress["stage"] = "post_expand"
        progress["last_action"] = "scroll_after_expand"
        scroll_full_page(content_root, page, log, progress, started_mono)

        expand_verification = _expand_verification_flags(content_root)
        log.step(
            "Expand verification (admission subtree keywords)",
            expand_verification,
        )

        expanded_html = content_root.content()
        expanded_html_path.write_text(expanded_html, encoding="utf-8")
        body_text = _body_inner_text(content_root)
        expanded_txt_path.write_text(body_text, encoding="utf-8")
        if terminal is not None:
            terminal.phase(
                "expanded_html_saved",
                str(expanded_html_path.relative_to(PROJECT_ROOT)),
            )

        probe = section_probe(content_root)
        remaining_count, remaining_previews = count_remaining_expand_controls(content_root)

        for txt in remaining_previews:
            if _warn_if_section_terms(txt):
                w = f"Remaining expand control near section keyword — {txt}"
                warnings.append(w)
                progress["warning_count"] = len(warnings)
                log.warn("Remaining expand control near critical section", {"text": txt})

        def_summary: Optional[dict[str, Any]] = None
        definition_capture_aborted = False

        if capture_definitions:
            if usk == "search_page":
                ok_s, _ = search_page_strict_target_ready(page, mcg_code, mcg_title)
                if not ok_s:
                    log.warn(
                        "Search-page strict markers missing before definition capture; rerunning Quick Search",
                        {"currentUrl": _safe_page_url(page)},
                    )
                    try:
                        ok_r, new_mk, new_fr3, in_fr3 = restore_search_page_target(
                            page,
                            shell_url=url,
                            search_code=sc,
                            mcg_code=mcg_code,
                            mcg_title=mcg_title,
                            log=log,
                            progress=progress,
                            out_prefix=out_prefix,
                        )
                        if ok_r and new_mk:
                            target_detection_markers = new_mk
                        if ok_r:
                            guideline_frame = new_fr3
                            target_detected_in_frame = in_fr3
                            resolved_target_frame_url, resolved_target_frame_name = "", ""
                            resolved_target_frame_index = -1
                            if new_fr3 is not None:
                                try:
                                    resolved_target_frame_url = new_fr3.url
                                except Exception:
                                    pass
                                try:
                                    resolved_target_frame_name = new_fr3.name or ""
                                except Exception:
                                    pass
                                for _fi, _fr in enumerate(page.frames):
                                    if _fr == new_fr3:
                                        resolved_target_frame_index = _fi
                                        break
                            content_root = guideline_frame if guideline_frame is not None else page
                            frame_count, frame_urls = _frame_urls_snapshot(page)
                    except Exception as e:
                        msg = f"Cannot start definition capture: search-page restore failed: {e}"
                        warnings.append(msg)
                        progress["warning_count"] = len(warnings)
                        log.error(msg, {"errorType": type(e).__name__})
                        raise RuntimeError(msg) from e
                    ok_s2, _ = search_page_strict_target_ready(page, mcg_code, mcg_title)
                    if not ok_s2:
                        msg = (
                            "Definition capture refused: search-page target markers not detected after restore "
                            f"(url={_safe_page_url(page)!r})."
                        )
                        log.error(msg)
                        raise RuntimeError(msg)
            else:
                if not _same_target_page(_safe_page_url(page), url):
                    log.warn(
                        "URL drift detected before definition capture; reloading canonical guideline URL",
                        {"currentUrl": _safe_page_url(page), "canonicalUrl": url},
                    )
                    try:
                        _goto_domcontent_with_retries(page, url, mcg_code, mcg_title, log, progress)
                    except Exception as e:
                        msg = f"Cannot start definition capture: failed to reload canonical guideline URL: {e}"
                        warnings.append(msg)
                        progress["warning_count"] = len(warnings)
                        log.error(msg, {"errorType": type(e).__name__})
                        raise RuntimeError(msg) from e
                if not page_has_target_content(page, mcg_code, mcg_title):
                    msg = (
                        "Definition capture refused: target guideline content not detected on the canonical URL "
                        f"({_safe_page_url(page)!r}). Reload the correct guideline page and rerun capture."
                    )
                    log.error(msg)
                    raise RuntimeError(msg)

            log.step(
                "[STEP] Definition capture started",
                {
                    "maxTriggers": max_definition_triggers,
                    "recursionDepth": definition_recursion_depth,
                    "clickTimeoutMs": definition_click_timeout_ms,
                    **expand_phase_summary,
                },
            )
            if terminal is not None:
                terminal.phase("definition_capture", f"max_triggers={max_definition_triggers}")
            progress["stage"] = "definition_capture"
            progress["last_action"] = "definition_capture_start"
            assert page is not None

            try:
                def_summary = run_definition_capture(
                    page=page,
                    content_frame=guideline_frame if usk == "search_page" else None,
                    mcg_code=mcg_code,
                    mcg_title=mcg_title,
                    source_url=url,
                    out_prefix=out_prefix,
                    raw_html_dir=RAW_HTML_DIR,
                    audits_dir=AUDIT_DIR,
                    project_root=PROJECT_ROOT,
                    log=log,
                    progress=progress,
                    started_mono=started_mono,
                    max_triggers=int(max_definition_triggers),
                    recursion_depth=int(definition_recursion_depth),
                    click_timeout_ms=int(definition_click_timeout_ms),
                    warnings_accum=warnings,
                    terminal_progress=terminal,
                )
            except Exception as e:
                definition_capture_aborted = True
                msg = f"Definition capture aborted: {type(e).__name__}: {e}"
                warnings.append(msg)
                progress["warning_count"] = len(warnings)
                log.error("Definition capture aborted", {"error": str(e), "errorType": type(e).__name__})
                def_summary = None

        screenshot_ok = False
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
            screenshot_ok = True
        except Exception as e:
            w = f"Full-page screenshot failed: {e}"
            warnings.append(w)
            progress["warning_count"] = len(warnings)
            log.warn("Full-page screenshot failed", {"error": str(e)})

        raw_sha = _sha256_file(raw_path)
        exp_sha = _sha256_file(expanded_html_path)
        txt_sha = _sha256_file(expanded_txt_path)

        defs_audit_rel_expected = str((AUDIT_DIR / f"{out_prefix}.definition-capture.audit.json").relative_to(PROJECT_ROOT))
        defs_json_rel_expected = str(defs_json_path_default.relative_to(PROJECT_ROOT))

        if capture_definitions and def_summary is not None:
            manifest_definition_capture: dict[str, Any] = {
                "enabled": True,
                "schema_version": str(def_summary.get("schema_version", "mcg_popup_capture.v2")),
                "definition_count": int(def_summary.get("definition_count", 0)),
                "popup_count": int(def_summary.get("popup_count", def_summary.get("definition_count", 0))),
                "citation_count": int(def_summary.get("citation_count", 0)),
                "footnote_count": int(def_summary.get("footnote_count", 0)),
                "context_count": int(def_summary.get("context_count", 0)),
                "trigger_count": int(def_summary.get("trigger_count", 0)),
                "audit_path": str(def_summary.get("audit_path_rel", defs_audit_rel_expected)),
                "definitions_json_path": str(def_summary.get("definitions_json_path_rel", defs_json_rel_expected)),
                "popups_json_path": str(def_summary.get("popups_json_path_rel", "")),
                "popups_html_path": str(def_summary.get("popups_html_path_rel", "")),
                "popup_triggers_jsonl_path": str(def_summary.get("popup_triggers_jsonl_path_rel", "")),
                "popup_failures_jsonl_path": str(def_summary.get("popup_failures_jsonl_path_rel", "")),
            }
            definition_capture_notes_md = (
                f"\n- **Popup capture ({out_prefix}.definitions.raw.json + {out_prefix}.popups.raw.json)**: "
                f"definitions `{manifest_definition_capture['definition_count']}`, "
                f"all popups `{manifest_definition_capture['popup_count']}`, "
                f"triggers processed `{manifest_definition_capture['trigger_count']}`"
            )
        elif capture_definitions:
            manifest_definition_capture = {
                "enabled": True,
                "schema_version": "mcg_popup_capture.v2",
                "definition_count": 0,
                "popup_count": 0,
                "citation_count": 0,
                "footnote_count": 0,
                "context_count": 0,
                "trigger_count": 0,
                "audit_path": defs_audit_rel_expected,
                "definitions_json_path": defs_json_rel_expected,
                "popups_json_path": "",
                "popups_html_path": "",
                "popup_triggers_jsonl_path": "",
                "popup_failures_jsonl_path": "",
            }
            definition_capture_notes_md = (
                f"\n- **Definition capture**: attempted but produced no structured summary artifact "
                f"(see `{defs_audit_rel_expected}` if present)"
            )
        else:
            manifest_definition_capture = {
                "enabled": False,
                "schema_version": "",
                "definition_count": 0,
                "popup_count": 0,
                "citation_count": 0,
                "footnote_count": 0,
                "context_count": 0,
                "trigger_count": 0,
                "audit_path": "",
                "definitions_json_path": "",
                "popups_json_path": "",
                "popups_html_path": "",
                "popup_triggers_jsonl_path": "",
                "popup_failures_jsonl_path": "",
            }
            definition_capture_notes_md = ""

        capture_status, capture_status_reasons = _compute_capture_status(
            capture_definitions=capture_definitions,
            definition_capture_aborted=definition_capture_aborted,
            expanded_html_path=expanded_html_path,
            expand_outcome=expand_outcome,
            warnings=warnings,
            remaining_expand_previews=remaining_previews,
            section_probe=probe,
            manifest_definition_capture=manifest_definition_capture,
            def_summary=def_summary,
        )

        preflight_audit_path = AUDIT_DIR / f"{out_prefix}.capture-completeness.preflight.json"
        if terminal is not None:
            terminal.phase(
                "audit_preflight",
                str(preflight_audit_path.relative_to(PROJECT_ROOT)),
            )

        td_ok_final, td_markers_final = search_page_strict_target_ready(page, mcg_code, mcg_title)
        manifest_markers = list(td_markers_final) if td_markers_final else list(target_detection_markers)

        fc_m, fus_m = _frame_urls_snapshot(page)
        manifest = {
            "mcg_code": mcg_code,
            "mcg_title": mcg_title,
            "source_url": url,
            "url_status": usk,
            "search_code": sc if usk == "search_page" else "",
            "resolved_by": "quick_search" if usk == "search_page" else "canonical_url",
            "target_detected": bool(td_ok_final),
            "target_detected_in_frame": bool(target_detected_in_frame) if usk == "search_page" else False,
            "target_detection_markers": manifest_markers,
            "login_check_mode": login_check_mode,
            "search_shell_detected": search_shell_detected if usk == "search_page" else False,
            "search_shell_markers": list(search_shell_markers) if usk == "search_page" else [],
            "search_shell_frame_url": search_shell_frame_url if usk == "search_page" else "",
            "search_shell_frame_name": search_shell_frame_name if usk == "search_page" else "",
            "search_shell_frame_index": int(search_shell_frame_index) if usk == "search_page" else -1,
            "resolved_target_frame_url": resolved_target_frame_url,
            "resolved_target_frame_name": resolved_target_frame_name,
            "resolved_target_frame_index": int(resolved_target_frame_index),
            "frame_count": int(fc_m),
            "frame_urls": list(fus_m),
            "quick_search_attempted": quick_search_attempted,
            "quick_search_candidate_count": int(progress.get("quick_search_candidate_count") or -1)
            if usk == "search_page"
            else -1,
            "quick_search_selected_frame_url": str(progress.get("quick_search_selected_frame_url") or "")
            if usk == "search_page"
            else "",
            "quick_search_selected_input_summary": (
                dict(progress.get("quick_search_selected_input_summary"))
                if usk == "search_page" and isinstance(progress.get("quick_search_selected_input_summary"), dict)
                else {}
            ),
            "quick_search_fill_method": str(progress.get("quick_search_fill_method") or "")
            if usk == "search_page"
            else "",
            "quick_search_submit_method": str(progress.get("quick_search_submit_method") or "")
            if usk == "search_page"
            else "",
            "search_results_detected": bool(progress.get("search_results_detected")) if usk == "search_page" else False,
            "search_result_rows": (
                list(progress.get("search_result_rows"))[:50]
                if usk == "search_page" and isinstance(progress.get("search_result_rows"), list)
                else []
            ),
            "selected_search_result": (
                dict(progress.get("selected_search_result"))
                if usk == "search_page" and isinstance(progress.get("selected_search_result"), dict)
                else {}
            ),
            "selected_search_result_reason": str(progress.get("selected_search_result_reason") or "")
            if usk == "search_page"
            else "",
            "clicked_search_result": bool(progress.get("clicked_search_result")) if usk == "search_page" else False,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "capture_status": capture_status,
            "capture_status_reasons": capture_status_reasons,
            "raw_html_path": str(raw_path.relative_to(PROJECT_ROOT)),
            "expanded_html_path": str(expanded_html_path.relative_to(PROJECT_ROOT)),
            "expanded_text_path": str(expanded_txt_path.relative_to(PROJECT_ROOT)),
            "screenshot_path": str(screenshot_path.relative_to(PROJECT_ROOT)) if screenshot_ok else "",
            "summary_path": str(summary_path.relative_to(PROJECT_ROOT)),
            "raw_html_sha256": raw_sha,
            "expanded_html_sha256": exp_sha,
            "expanded_text_sha256": txt_sha,
            "expand_passes": passes,
            "expand_click_count": clicks,
            "expand_phase_outcome": expand_outcome,
            "expand_phase_summary": expand_phase_summary,
            "remaining_expand_control_count": remaining_count,
            "remaining_expand_controls": [
                {"text": _truncate(t, 120)} for t in remaining_previews
            ],
            "expand_verification": expand_verification,
            "section_probe": probe,
            "warnings": warnings,
            "definition_capture": manifest_definition_capture,
        }
        try:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            log.error("Manifest write failed", {"error": str(exc), "path": str(manifest_path)})
            raise RuntimeError(f"manifest_write_failed:{manifest_path}") from exc

        exit_code = _resolve_capture_exit_code(
            capture_status,
            allow_needs_review_exit_zero=allow_needs_review_exit_zero,
        )

        warn_lines = "\n".join(f"- {w}" for w in warnings) if warnings else "- (none)"
        csr_lines = "\n".join(f"- `{r}`" for r in capture_status_reasons) if capture_status_reasons else "- (none)"
        summary_md = f"""# {out_prefix} Capture Summary

- **MCG code**: {mcg_code}
- **Title**: {mcg_title}
- **URL**: {url}
- **URL status**: `{usk}`{f" (`search_code`: `{sc}`)" if usk == "search_page" else ""}
- **Resolved by**: `{"quick_search" if usk == "search_page" else "canonical_url"}`
- **Target detected (strict)**: {td_ok_final}
- **Login check mode**: `{login_check_mode}`
- **Search shell detected**: {search_shell_detected if usk == "search_page" else False}
- **Search shell markers**: {json.dumps(search_shell_markers if usk == "search_page" else [], ensure_ascii=False)}
- **Quick Search attempted**: {quick_search_attempted}
- **Captured at**: {manifest['captured_at']}
- **Capture status**: `{capture_status}`
- **Capture status reasons**

{csr_lines}

- **Raw HTML path**: `{manifest['raw_html_path']}`
- **Expanded HTML path**: `{manifest['expanded_html_path']}`
- **Expanded text path**: `{manifest['expanded_text_path']}`
- **Screenshot path**: `{manifest['screenshot_path'] or '(failed)'}`
- **SHA256 (raw / expanded HTML / expanded text)**: `{raw_sha}` / `{exp_sha}` / `{txt_sha}`
- **Expand passes**: {passes}
- **Expand phase outcome**: {expand_outcome}
- **Expand click count**: {clicks}
- **Remaining expand controls**: {remaining_count}
- **Section probe**: {json.dumps(probe, ensure_ascii=False)}
{definition_capture_notes_md}
- **Warnings**

{warn_lines}

- **Next step**: Step 2 will parse expanded HTML into source-tree JSON
"""
        summary_path.write_text(summary_md, encoding="utf-8")

        elapsed = int(time.monotonic() - started_mono)
        trig_proc = int(manifest_definition_capture.get("trigger_count") or 0)
        fail_trig = int((def_summary or {}).get("failed_trigger_count") or 0)
        defs_c = int(manifest_definition_capture.get("definition_count") or 0)
        pop_c = int(manifest_definition_capture.get("popup_count") or 0)
        sentinel = read_preflight_sentinel_status(str(preflight_audit_path))

        if terminal is not None:
            terminal.phase("complete", capture_status)
            terminal.print_capture_summary(
                capture_status=capture_status,
                expanded_html=_expanded_html_saved_ok(expanded_html_path),
                expanded_text=_nonempty_file(expanded_txt_path),
                expand_outcome=expand_outcome,
                definitions_captured=defs_c,
                total_popups=pop_c,
                triggers_processed=trig_proc,
                failed_triggers=fail_trig,
                sentinel_check=sentinel,
                manifest_path=str(manifest_path.relative_to(PROJECT_ROOT)),
                audit_preflight_path=str(preflight_audit_path.relative_to(PROJECT_ROOT)),
            )
            print(f"(Elapsed {elapsed}s · Exit {exit_code} · Warnings {len(warnings)})", flush=True)
        else:
            print(flush=True)
            print("================ MCG CAPTURE COMPLETE ================", flush=True)
            print(f"MCG: {mcg_code} {mcg_title}", flush=True)
            print(f"URL: {url}", flush=True)
            print(f"Elapsed: {elapsed} seconds", flush=True)
            print(f"Expand passes: {passes}", flush=True)
            print(f"Expand phase outcome: {expand_outcome}", flush=True)
            print(f"Expand clicks: {clicks}", flush=True)
            print(f"Remaining expand controls: {remaining_count}", flush=True)
            print(f"Raw HTML: {raw_path}", flush=True)
            print(f"Expanded HTML: {expanded_html_path}", flush=True)
            print(f"Expanded text: {expanded_txt_path}", flush=True)
            print(f"Manifest: {manifest_path}", flush=True)
            print(f"Summary: {summary_path}", flush=True)
            print(f"Screenshot: {screenshot_path}", flush=True)
            print(f"Capture status: {capture_status}", flush=True)
            if capture_status_reasons:
                print(f"Capture status reasons: {capture_status_reasons}", flush=True)
            print(f"Exit code (needs_review policy): {exit_code}", flush=True)
            print(f"Warnings: {len(warnings)}", flush=True)
            if manifest_definition_capture.get("enabled"):
                print(
                    "Popup capture: "
                    f"definitions={manifest_definition_capture.get('definition_count')} "
                    f"popups={manifest_definition_capture.get('popup_count')} "
                    f"triggers_processed={manifest_definition_capture.get('trigger_count')}",
                    flush=True,
                )
            print("======================================================", flush=True)

    except Exception as e:
        if terminal is not None:
            terminal.expand_finish_line()
            terminal.definition_finish_line()
        exit_code = 1
        elapsed = int(time.monotonic() - started_mono)
        tb = traceback.format_exc()
        exc_tb = getattr(e, "__traceback__", None)
        last_tb = traceback.extract_tb(exc_tb)[-1] if exc_tb else None
        loc_hint = ""
        if last_tb is not None:
            loc_hint = f"{last_tb.filename}:{last_tb.lineno} in {last_tb.name}"
        partial: list[str] = []
        for p in (raw_path, expanded_html_path, manifest_path):
            if p.exists():
                partial.append(str(p))
        warn_diag_path: Optional[Path] = None
        warnings.append(f"Failure location: {loc_hint}" if loc_hint else "Failure location: (unknown)")
        warnings.append("Traceback:\n" + tb)
        try:
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            fail_diag = {
                "error": str(e),
                "errorType": type(e).__name__,
                "stage": progress.get("stage", ""),
                "lastAction": progress.get("last_action", ""),
                "failureLocation": loc_hint or None,
                "traceback": tb,
                "warningsTail": warnings[-10:] if warnings else [],
            }
            warn_diag_path = AUDIT_DIR / f"{out_prefix}.capture-failure.json"
            warn_diag_path.write_text(json.dumps(fail_diag, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            warn_diag_path = None
        print(flush=True)
        print("================ MCG CAPTURE FAILED ==================", flush=True)
        print(f"Stage: {progress.get('stage', '')}", flush=True)
        print(f"Elapsed: {elapsed}", flush=True)
        print(f"Last action: {progress.get('last_action', '')}", flush=True)
        print(f"Current URL: {progress.get('current_url', '')}", flush=True)
        if loc_hint:
            print(f"Failure location: {loc_hint}", flush=True)
        print(f"Error: {e}", flush=True)
        print(f"Failure diagnostics: {warn_diag_path}" if warn_diag_path else "Failure diagnostics: (not written)", flush=True)
        print("Traceback:", flush=True)
        print(tb, end="" if tb.endswith("\n") else "\n", flush=True)
        print(f"Warnings: {len(warnings)}", flush=True)
        for w in warnings[:30]:
            print(f"  - {w}", flush=True)
        print(f"Partial outputs saved: {', '.join(partial) if partial else '(none)'}", flush=True)
        print("======================================================", flush=True)
        log.error(
            "Capture failed",
            {
                "error": str(e),
                "errorType": type(e).__name__,
                "failureLocation": loc_hint or None,
                "traceback": tb,
                "failureDiagnosticsPath": str(warn_diag_path) if warn_diag_path else None,
            },
        )

    finally:
        if heartbeat is not None:
            heartbeat.stop()
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass

    return exit_code
