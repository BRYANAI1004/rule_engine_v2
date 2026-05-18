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
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, TimeoutError, sync_playwright

from progress_logger import Heartbeat, ProgressLogger, fresh_capture_progress, update_elapsed

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = PROJECT_ROOT / ".local" / "playwright" / "mcg-careweb"
RAW_HTML_DIR = PROJECT_ROOT / "rules" / "mcg" / "raw-html"
AUDIT_DIR = PROJECT_ROOT / "rules" / "mcg" / "audits"

MAX_EXPAND_PASSES = 30
TEXT_TRUNCATE = 120
REMAINING_PREVIEW = 20
ADDITIONAL_LOGIN_HINTS = [
    "Clinical Indications for Admission to Inpatient Care",
]


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


def _warn_if_section_terms(text: str) -> bool:
    low = text.lower()
    keys = ["admission", "clinical indications", "discharge", "discharge planning", "discharge destination"]
    return any(k in low for k in keys)


def scroll_full_page(page: Page, log: ProgressLogger, progress: dict[str, Any], started: float) -> None:
    progress["stage"] = "scroll"
    progress["last_action"] = "scroll_started"
    update_elapsed(progress, started)
    log.step("Full-page scroll started")

    page.evaluate("() => window.scrollTo(0, 0)")
    step_px = 800
    step_index = 0
    scroll_height = page.evaluate(
        "() => Math.max(document.body?.scrollHeight ?? 0, document.documentElement.scrollHeight ?? 0)"
    )
    scroll_y = 0

    while scroll_y + 1 < scroll_height:
        scroll_y = min(scroll_y + step_px, scroll_height)
        page.evaluate("(y) => window.scrollTo(0, y)", scroll_y)
        page.wait_for_timeout(80)
        step_index += 1
        scroll_height = page.evaluate(
            "() => Math.max(document.body?.scrollHeight ?? 0, document.documentElement.scrollHeight ?? 0)"
        )
        if step_index % 3 == 0 or scroll_y >= scroll_height - 1:
            update_elapsed(progress, started)
            log.info("Scrolling page", {"scrollY": scroll_y, "scrollHeight": scroll_height})
            progress["last_action"] = f"scroll_y_{scroll_y}"

    progress["last_action"] = "scroll_completed"
    update_elapsed(progress, started)
    log.success("Full-page scroll completed", {"finalScrollHeight": scroll_height, "finalScrollY": scroll_y})


def _body_inner_text(page: Page) -> str:
    return page.evaluate("() => document.body ? document.body.innerText : ''") or ""


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
) -> Any:
    """
    Navigate with wait_until=domcontentloaded, up to two attempts.
    On TimeoutError, accept stalled navigation if guideline markers appear in the DOM.
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
            detected = page_has_target_content(page, mcg_code, mcg_title)
            log.info(
                "Navigation attempt ended (timeout)",
                {"attempt": attempt, "currentUrl": cur, "pageTitle": title, "targetContentDetected": detected},
            )
            if detected:
                log.warn(
                    "Navigation timed out, but target guideline content is present. Continuing capture.",
                    {"attempt": attempt, "currentUrl": cur, "pageTitle": title},
                )
                progress["current_url"] = cur or _safe_page_url(page)
                return None
            if attempt == 1:
                log.warn(
                    "Navigation timed out without target content; retrying goto",
                    {"attempt": attempt, "currentUrl": cur, "pageTitle": title},
                )
                continue
            msg = (
                f"Navigation timed out after 2 attempts and target guideline content was not detected "
                f"(url={cur!r}, title={title!r})"
            )
            log.error(msg, {"currentUrl": cur, "pageTitle": title})
            raise RuntimeError(msg) from e
        except Exception as e:
            cur = _safe_page_url(page)
            title = _safe_page_title(page)
            detected = page_has_target_content(page, mcg_code, mcg_title)
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
                    "Navigation failed, but target guideline content is present. Continuing capture.",
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
            )
            log.error(msg, {"currentUrl": cur, "pageTitle": title, "errorType": type(e).__name__})
            raise RuntimeError(msg) from e

    raise RuntimeError("Navigation failed after retries (internal error)")


def _should_skip_danger_click(text: str, href: Optional[str]) -> Optional[str]:
    tl = text.lower()
    for s in SKIP_LINK_TEXT_SNIPPETS:
        if s in tl:
            return f"blocked text contains {s!r}"
    if href and href.strip().lower().startswith("http"):
        return "absolute http(s) href"
    return None


def _matches_expand_signal(
    text: str,
    href: Optional[str],
    onclick: Optional[str],
    aria_expanded: Optional[str],
    class_name: Optional[str],
    elem_id: Optional[str],
    title: Optional[str],
) -> bool:
    if onclick and "expand" in onclick.lower():
        return True
    if aria_expanded == "false":
        return True
    for blob in (class_name or "", elem_id or "", title or ""):
        if "expand" in blob.lower():
            return True
    t = text
    if re.search(r"\bExpand\b", t, flags=re.I):
        return True
    if "[ Expand" in t or "[Expand" in t.replace(" ", " "):
        return True
    if re.match(r"Expand\s+", t.strip(), flags=re.I):
        return True
    return False


def _is_expand_all_text(text: str) -> bool:
    return bool(re.search(r"\bexpand\s*all\b", text, flags=re.I))


def _candidate_locators(page: Page) -> list[Locator]:
    """Collect locators in DOM order for relevant interactive / expandable nodes."""
    sel = (
        "a, button, [role='button'], [role='link'], "
        "[aria-expanded='false'], [onclick*='expand' i], "
        "[class*='expand' i], [id*='expand' i], [title*='expand' i]"
    )
    loc = page.locator(sel)
    n = loc.count()
    return [loc.nth(i) for i in range(n)]


def _describe_locator(el: Locator) -> dict[str, Any]:
    try:
        tag = el.evaluate("e => e.tagName.toLowerCase()")
    except Exception:
        tag = "?"
    text = ""
    try:
        text = el.inner_text(timeout=800) or ""
    except Exception:
        pass
    href = el.get_attribute("href")
    onclick = el.get_attribute("onclick")
    aria = el.get_attribute("aria-expanded")
    class_name = el.get_attribute("class")
    elem_id = el.get_attribute("id")
    ttl = el.get_attribute("title")
    return {
        "tag": tag,
        "text": text,
        "href": href,
        "onclick": onclick,
        "aria_expanded": aria,
        "class": class_name,
        "id": elem_id,
        "title": ttl,
    }


def collect_expand_candidates(page: Page) -> tuple[list[tuple[Locator, dict[str, Any], bool]], int]:
    """
    Returns ordered (expand_all_first): list of (locator, meta, is_expand_all), and skipped count.
    """
    expand_all: list[tuple[Locator, dict[str, Any], bool]] = []
    rest: list[tuple[Locator, dict[str, Any], bool]] = []
    skipped = 0

    for el in _candidate_locators(page):
        try:
            vis = el.is_visible(timeout=500)
        except Exception:
            vis = False
        if not vis:
            continue
        meta = _describe_locator(el)
        text = meta.get("text") or ""
        href = meta.get("href")
        skip_reason = _should_skip_danger_click(text, href)
        if skip_reason:
            skipped += 1
            continue
        if not _matches_expand_signal(
            text,
            href,
            meta.get("onclick"),
            meta.get("aria_expanded"),
            meta.get("class"),
            meta.get("id"),
            meta.get("title"),
        ):
            continue
        is_all = _is_expand_all_text(text)
        if is_all:
            expand_all.append((el, meta, True))
        else:
            rest.append((el, meta, False))

    ordered = expand_all + rest
    return ordered, skipped


def count_remaining_expand_controls(page: Page) -> tuple[int, list[str]]:
    previews: list[str] = []
    candidates, _ = collect_expand_candidates(page)
    count = len(candidates)
    for _, meta, _ in candidates[:REMAINING_PREVIEW]:
        previews.append(_truncate(meta.get("text") or ""))
    return count, previews


def section_probe(page: Page) -> dict[str, bool]:
    t = _body_inner_text(page)
    return {
        "has_admission": "Clinical Indications for Admission to Inpatient Care" in t,
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


def _save_diagnostics(page: Page, out_prefix: str, reason: str) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    body = _body_inner_text(page)
    diag = {
        "reason": reason,
        "url": page.url,
        "title": page.title(),
        "body_inner_text_length": len(body),
        "body_inner_text_preview": body[:4000],
    }
    p = AUDIT_DIR / f"{out_prefix}.capture-diagnostics.json"
    p.write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def run_expand_passes(
    page: Page,
    target_url: str,
    log: ProgressLogger,
    progress: dict[str, Any],
    started: float,
    warnings: list[str],
) -> tuple[int, int]:
    total_clicked = 0
    passes = 0

    for pass_num in range(1, MAX_EXPAND_PASSES + 1):
        passes = pass_num
        progress["stage"] = "expand"
        progress["expand_pass"] = pass_num
        progress["last_action"] = f"expand_pass_{pass_num}_started"
        update_elapsed(progress, started)

        log.step(f"Expand pass {pass_num} started")

        candidates, skipped_count = collect_expand_candidates(page)
        candidate_count = len(candidates)

        clicked_this_pass = 0
        warning_this_pass = 0
        for el, meta, _is_all in candidates:
            text = meta.get("text") or ""
            label_short = _truncate(text)
            url_before_click = page.url
            try:
                el.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            try:
                el.click(timeout=8000)
                clicked_this_pass += 1
                total_clicked += 1
                progress["expand_click_count"] = total_clicked
                progress["last_action"] = f"clicked_expand_{total_clicked}"
                update_elapsed(progress, started)
                log.info("Clicked expand control", {"text": label_short})
                page.wait_for_timeout(120)
            except Exception as e:
                warning_this_pass += 1
                msg = str(e)
                warnings.append(f"Expand click failed — {_truncate(text)} — {msg}")
                progress["warning_count"] = len(warnings)
                log.warn("Expand click failed", {"text": label_short, "reason": msg})

            url_after = page.url
            if not _same_target_page(url_after, target_url):
                warn = f"Navigation away from target after click — {_truncate(text)} — {url_before_click} -> {url_after}"
                warnings.append(warn)
                progress["warning_count"] = len(warnings)
                log.warn("Unexpected navigation; going back", {"from": url_before_click, "to": url_after, "control": label_short})
                try:
                    page.go_back(wait_until="load", timeout=60_000)
                    page.wait_for_timeout(400)
                except Exception as e:
                    warnings.append(f"go_back failed after stray navigation: {e}")
                    progress["warning_count"] = len(warnings)

        update_elapsed(progress, started)
        remaining_count, _ = count_remaining_expand_controls(page)
        progress["current_url"] = page.url

        log.success(
            f"Expand pass {pass_num} completed",
            {
                "pass": pass_num,
                "candidateCount": candidate_count,
                "clickedThisPass": clicked_this_pass,
                "skippedCount": skipped_count,
                "warningsThisPass": warning_this_pass,
                "totalClicked": total_clicked,
                "currentUrl": page.url,
                "remainingExpandControlCount": remaining_count,
            },
        )

        if clicked_this_pass == 0:
            log.info("No expand controls clicked this pass; stopping expand loop", {"pass": pass_num})
            break

        page.wait_for_timeout(int(500 + random.random() * 500))
        scroll_full_page(page, log, progress, started)

    return passes, total_clicked


def run_capture(
    *,
    mcg_code: str,
    mcg_title: str,
    url: str,
    out_prefix: str,
) -> int:
    started_mono = time.monotonic()
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    progress = fresh_capture_progress(started_iso)
    warnings: list[str] = []

    scope = f"capture:{mcg_code}"
    log = ProgressLogger(scope)

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

    exit_code = 0
    try:
        log.step("Opening browser", {"userDataDir": str(PROFILE_DIR)})
        progress["stage"] = "browser_launch"
        progress["last_action"] = "launch_persistent_context"

        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        heartbeat = Heartbeat(10.0, log, _snapshot)
        heartbeat.start()

        log.step("Opening page", {"url": url})
        progress["stage"] = "navigate"
        progress["last_action"] = "goto"
        assert page is not None
        nav_response = _goto_domcontent_with_retries(page, url, mcg_code, mcg_title, log, progress)

        target_detected = page_has_target_content(page, mcg_code, mcg_title)
        checkpoint: dict[str, Any] = {
            "currentUrl": _safe_page_url(page),
            "pageTitle": _safe_page_title(page),
            "targetContentDetected": target_detected,
        }
        if nav_response is not None:
            checkpoint["responseStatus"] = nav_response.status
            checkpoint["responseUrl"] = nav_response.url
        log.info("Navigation checkpoint", checkpoint)

        progress["last_action"] = "goto_complete"
        progress["stage"] = "raw_capture" if target_detected else "login_check"

        cu = _safe_page_url(page)
        if not cu:
            try:
                cu = page.url
            except Exception:
                cu = url
        progress["current_url"] = cu
        target_url = cu

        if not page_has_login_markers(page, mcg_code, mcg_title):
            print(
                "Please log in in the opened browser window, navigate or return to the target guideline page if needed, "
                "then press Enter in this terminal to continue.",
                flush=True,
            )
            input()
            target_url = page.url
            if not page_has_login_markers(page, mcg_code, mcg_title):
                dpath = _save_diagnostics(page, out_prefix, "login_markers_missing_after_wait")
                msg = (
                    f"Expected guideline markers not found after login wait. "
                    f"Diagnostics: {dpath}"
                )
                log.error(msg, {"url": page.url})
                raise RuntimeError(msg)

        progress["stage"] = "raw_capture"
        progress["last_action"] = "save_raw_html"
        log.step("Saving raw HTML (pre-expand)")
        raw_html = page.content()
        raw_path.write_text(raw_html, encoding="utf-8")

        passes, clicks = run_expand_passes(page, target_url, log, progress, started_mono, warnings)

        progress["stage"] = "post_expand"
        progress["last_action"] = "scroll_after_expand"
        scroll_full_page(page, log, progress, started_mono)

        expanded_html = page.content()
        expanded_html_path.write_text(expanded_html, encoding="utf-8")
        body_text = _body_inner_text(page)
        expanded_txt_path.write_text(body_text, encoding="utf-8")

        probe = section_probe(page)
        remaining_count, remaining_previews = count_remaining_expand_controls(page)

        for txt in remaining_previews:
            if _warn_if_section_terms(txt):
                w = f"Remaining expand control near section keyword — {txt}"
                warnings.append(w)
                progress["warning_count"] = len(warnings)
                log.warn("Remaining expand control near critical section", {"text": txt})

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

        manifest = {
            "mcg_code": mcg_code,
            "mcg_title": mcg_title,
            "source_url": url,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
            "remaining_expand_control_count": remaining_count,
            "remaining_expand_controls": [
                {"text": _truncate(t, 120)} for t in remaining_previews
            ],
            "section_probe": probe,
            "warnings": warnings,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        warn_lines = "\n".join(f"- {w}" for w in warnings) if warnings else "- (none)"
        summary_md = f"""# {out_prefix} Capture Summary

- **MCG code**: {mcg_code}
- **Title**: {mcg_title}
- **URL**: {url}
- **Captured at**: {manifest['captured_at']}
- **Raw HTML path**: `{manifest['raw_html_path']}`
- **Expanded HTML path**: `{manifest['expanded_html_path']}`
- **Expanded text path**: `{manifest['expanded_text_path']}`
- **Screenshot path**: `{manifest['screenshot_path'] or '(failed)'}`
- **SHA256 (raw / expanded HTML / expanded text)**: `{raw_sha}` / `{exp_sha}` / `{txt_sha}`
- **Expand passes**: {passes}
- **Expand click count**: {clicks}
- **Remaining expand controls**: {remaining_count}
- **Section probe**: {json.dumps(probe, ensure_ascii=False)}
- **Warnings**

{warn_lines}

- **Next step**: Step 2 will parse expanded HTML into source-tree JSON
"""
        summary_path.write_text(summary_md, encoding="utf-8")

        elapsed = int(time.monotonic() - started_mono)
        print(flush=True)
        print("================ MCG CAPTURE COMPLETE ================", flush=True)
        print(f"MCG: {mcg_code} {mcg_title}", flush=True)
        print(f"URL: {url}", flush=True)
        print(f"Elapsed: {elapsed} seconds", flush=True)
        print(f"Expand passes: {passes}", flush=True)
        print(f"Expand clicks: {clicks}", flush=True)
        print(f"Remaining expand controls: {remaining_count}", flush=True)
        print(f"Raw HTML: {raw_path}", flush=True)
        print(f"Expanded HTML: {expanded_html_path}", flush=True)
        print(f"Expanded text: {expanded_txt_path}", flush=True)
        print(f"Manifest: {manifest_path}", flush=True)
        print(f"Summary: {summary_path}", flush=True)
        print(f"Screenshot: {screenshot_path}", flush=True)
        print(f"Warnings: {len(warnings)}", flush=True)
        print("======================================================", flush=True)

    except Exception as e:
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
