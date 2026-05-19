#!/usr/bin/env python3
"""
Discover ISC ORG guideline canonical URLs from CareWeb TOC (URL indexing only).

Crawls same-origin TOC pages and /ed30/isc/*.htm links using the persistent
Chromium profile at .local/playwright/mcg-careweb (manual login as needed).

Does NOT capture full guideline content, definitions, parse rules, or touch DB.

Navigation budgets:
  --max-toc-pages caps TOC (toc.pl) traversal (default 500).
  --max-pages caps how many ISC guideline .htm URLs are opened for classification (default 1000).
  For a full ISC sweep, TOC is typically ~90 pages and hundreds of unique .htm URLs may exist;
  increase --max-pages if inspection stops early while URLs remain.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = PROJECT_ROOT / ".local" / "playwright" / "mcg-careweb"

ALLOWED_NETLOC = "careweb.careguidelines.com"
TOC_PATH_MARKER = "/ed30/scripts/toc.pl"
ISC_GUIDELINE_PREFIX = "/ed30/isc/"

# Match M-85, M-085, M85, M085, M-05, M005, etc.
MCODE_RE = re.compile(r"(?<![A-Za-z0-9])(M)\s*-?\s*(0*\d{1,5})(?!\d)", re.I)

URL_SKIP_HINTS = (
    "methodology",
    "benchmark",
    "dashboard",
    "references.htm",
    "_ref.htm",
    "index.htm",
    "toc.pl",
)


def _truthy_env_headless() -> bool:
    v = str(sys.environ.get("MCG_TOC_HEADLESS", "")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


@dataclass
class GuidelineRecord:
    mcg_code: str
    mcg_original_code: str
    title: str
    url: str
    confidence: str
    evidence: dict[str, Any]
    category_path: str = ""
    link_text: str = ""
    page_title: str = ""

    def to_json_row(self) -> dict[str, Any]:
        return {
            "mcg_code": self.mcg_code,
            "mcg_original_code": self.mcg_original_code,
            "title": self.title,
            "url": self.url,
            "confidence": self.confidence,
            "evidence": dict(self.evidence),
            "category_path": self.category_path or None,
            "link_text": self.link_text or None,
            "page_title": self.page_title or None,
        }


@dataclass
class WorkingCandidate:
    url: str
    link_texts: list[str] = field(default_factory=list)
    source_toc_urls: list[str] = field(default_factory=list)
    category_hints: list[str] = field(default_factory=list)


def normalize_m_digits(raw_digits: str) -> str:
    """Normalize numeric part to M + at least 3-digit code (M005, M085, M190)."""
    n = raw_digits.lstrip("0") or "0"
    if len(n) < 3:
        n = n.zfill(3)
    return f"M{n}"


def extract_mcode_matches(text: str) -> list[tuple[str, str]]:
    """
    Return list of (normalized_mcg_code, original_snippet) for each match.
    Original snippet preserves matched substring with minimal whitespace collapse.
    """
    out: list[tuple[str, str]] = []
    if not text:
        return out
    for m in MCODE_RE.finditer(text):
        digits = m.group(2)
        if not digits.lstrip("0"):
            continue
        original = m.group(0)
        norm = normalize_m_digits(digits)
        out.append((norm, original.strip()))
    return out


def pick_primary_code(candidates: list[tuple[str, str]], body: str) -> Optional[tuple[str, str]]:
    """Prefer ORG-associated mention, else first."""
    if not candidates:
        return None
    upper_body = body.upper()
    for norm, orig in candidates:
        idx = body.upper().find(orig.upper())
        if idx < 0:
            continue
        window = upper_body[max(0, idx - 24) : idx + len(orig) + 8]
        if "ORG" in window:
            return (norm, orig)
    return candidates[0]


def is_allowed_careweb(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and p.netloc.lower() == ALLOWED_NETLOC
    except Exception:
        return False


def is_toc_url(url: str) -> bool:
    p = urlparse(url)
    return TOC_PATH_MARKER in (p.path or "").lower() or (p.path or "").lower().endswith("/toc.pl")


def is_isc_guideline_shape(url: str) -> bool:
    p = urlparse(url)
    path = (p.path or "").lower()
    if ISC_GUIDELINE_PREFIX not in path:
        return False
    if not path.endswith(".htm"):
        return False
    low = url.lower()
    for hint in URL_SKIP_HINTS:
        if hint in low:
            return False
    return True


def absolutize(base_url: str, href: str) -> Optional[str]:
    if not href or href.strip().startswith("#"):
        return None
    joined = urljoin(base_url, href.strip())
    if not is_allowed_careweb(joined):
        return None
    # Normalize fragment away for dedupe
    p = urlparse(joined)
    clean = p._replace(fragment="").geturl()
    return clean


def looks_like_guideline_page(body_text: str, page_title: str, h1: str) -> bool:
    blob = f"{page_title}\n{h1}\n{body_text[:12000]}"
    upper = blob.upper()
    if "ORG" not in upper and "CLINICAL INDICATIONS" not in upper:
        return False
    return bool(extract_mcode_matches(blob))


def infer_confidence(
    found_code_in: str,
    found_title_in: str,
    org_hit: bool,
    clinical_hit: bool,
) -> str:
    score = 0
    if found_code_in == "link_text":
        score += 2
    elif found_code_in == "page_title":
        score += 1
    elif found_code_in == "page_body":
        score += 1
    if found_title_in == "h1":
        score += 2
    elif found_title_in == "page_title":
        score += 1
    elif found_title_in == "link_text":
        score += 1
    if org_hit:
        score += 2
    if clinical_hit:
        score += 1
    if score >= 5:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def sort_by_mcode(records: list[GuidelineRecord]) -> list[GuidelineRecord]:
    def key(r: GuidelineRecord) -> tuple[int, str]:
        m = re.match(r"^M(\d+)$", r.mcg_code, re.I)
        num = int(m.group(1)) if m else 10**9
        return (num, r.mcg_code.upper())

    return sorted(records, key=key)


def _random_delay(min_ms: int, max_ms: int) -> None:
    time.sleep(random.uniform(min_ms / 1000.0, max_ms / 1000.0))


def collect_links_from_page(page) -> list[tuple[str, str, str]]:
    """
    Return list of (href, link_text, category_hint).
    category_hint is best-effort from nearest preceding heading-ish text in TOC.
    """
    return page.evaluate(
        """() => {
      const out = [];
      const anchors = Array.from(document.querySelectorAll('a[href]'));
      for (const a of anchors) {
        let hint = '';
        let el = a;
        for (let i = 0; i < 8 && el; i++) {
          let prev = el.previousElementSibling;
          while (prev) {
            const tag = prev.tagName && prev.tagName.toUpperCase();
            if (tag && /^H[1-6]$/.test(tag)) {
              hint = (prev.innerText || '').trim().slice(0, 200);
              break;
            }
            if (tag === 'TD' || tag === 'TH') {
              hint = (prev.innerText || '').trim().slice(0, 200);
              break;
            }
            prev = prev.previousElementSibling;
          }
          if (hint) break;
          el = el.parentElement;
        }
        const href = a.getAttribute('href') || '';
        const text = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
        out.push([href, text, hint]);
      }
      return out;
    }"""
    )


def merge_candidate(store: dict[str, WorkingCandidate], url: str, link_text: str, toc_src: str, cat: str) -> None:
    w = store.get(url)
    if w is None:
        w = WorkingCandidate(url=url)
        store[url] = w
    if link_text and link_text not in w.link_texts:
        w.link_texts.append(link_text)
    if toc_src and toc_src not in w.source_toc_urls:
        w.source_toc_urls.append(toc_src)
    if cat and cat not in w.category_hints:
        w.category_hints.append(cat)


def score_url_quality(url: str, body_has_org_mcode: bool) -> int:
    s = 0
    low = url.lower()
    if ISC_GUIDELINE_PREFIX in low and low.endswith(".htm"):
        s += 100
    if "index.htm" in low or "index.html" in low:
        s -= 200
    if "toc.pl" in low:
        s -= 500
    if body_has_org_mcode:
        s += 40
    return s


def choose_best_record(existing: GuidelineRecord, challenger: GuidelineRecord) -> GuidelineRecord:
    """Prefer canonical ISC .htm, ORG+near-code body hits, higher confidence."""

    def score(rec: GuidelineRecord) -> tuple[int, str]:
        org_near = bool(rec.evidence.get("body_org_near_mcode"))
        url_s = score_url_quality(rec.url, org_near)
        conf_rank = {"high": 3, "medium": 2, "low": 1}.get(rec.confidence, 0)
        return (url_s + conf_rank * 10, rec.url)

    return existing if score(existing) >= score(challenger) else challenger


def body_org_near_mcode(body_text: str, norm_code: str) -> bool:
    """True if ORG appears near an mention of this M-code (original or normalized forms)."""
    if not body_text or not norm_code:
        return False
    m = re.match(r"^M(\d+)$", norm_code, re.I)
    if not m:
        return False
    digits = m.group(1)
    variants = {
        norm_code.upper(),
        f"M-{digits}",
        f"M-{digits.lstrip('0') or '0'}",
        digits,
    }
    upper = body_text.upper()
    if "ORG" not in upper:
        return False
    for vm in MCODE_RE.finditer(body_text):
        n = normalize_m_digits(vm.group(2))
        if n.upper() != norm_code.upper():
            continue
        start = max(0, vm.start() - 48)
        chunk = body_text[start : vm.start() + 24].upper()
        if "ORG" in chunk:
            return True
    for v in variants:
        if not v:
            continue
        idx = upper.find(v.upper())
        if idx < 0:
            continue
        window = upper[max(0, idx - 40) : idx + len(v) + 20]
        if "ORG" in window:
            return True
    return False


def parse_only_codes(raw: Optional[str]) -> Optional[set[str]]:
    if not raw or not str(raw).strip():
        return None
    out: set[str] = set()
    for part in str(raw).split(","):
        p = part.strip().upper()
        if not p:
            continue
        if re.match(r"^M\d+$", p):
            out.add(normalize_m_digits(p[1:]))
        else:
            # tolerate M-85
            for norm, _ in extract_mcode_matches(p):
                out.add(norm)
    return out


def write_csv(path: Path, rows: list[GuidelineRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "mcg_code",
        "mcg_original_code",
        "title",
        "url",
        "confidence",
        "found_code_in",
        "found_title_in",
        "source_toc_url",
        "category_path",
        "page_title",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            ev = r.evidence
            w.writerow(
                {
                    "mcg_code": r.mcg_code,
                    "mcg_original_code": r.mcg_original_code,
                    "title": r.title,
                    "url": r.url,
                    "confidence": r.confidence,
                    "found_code_in": ev.get("found_code_in", ""),
                    "found_title_in": ev.get("found_title_in", ""),
                    "source_toc_url": ev.get("source_toc_url", ""),
                    "category_path": r.category_path,
                    "page_title": r.page_title,
                }
            )


def write_md(path: Path, rows: list[GuidelineRecord], stats: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# MCG CareWeb TOC index (ISC)",
        "",
        f"- TOC pages visited: {stats.get('toc_pages_visited', 0)}",
        f"- Candidate guideline link discoveries (TOC pass, includes duplicates): {stats.get('candidate_links_raw', 0)}",
        f"- Unique ISC `.htm` URLs queued from TOC: {stats.get('unique_isc_htm_urls', 0)}",
        f"- ISC `.htm` URLs opened for classification: {stats.get('guideline_pages_opened', 0)}",
        f"- Unique M-codes in index: {stats.get('unique_mcodes', 0)}",
        "",
        "| M-code | Title | URL | Confidence |",
        "| --- | --- | --- | --- |",
    ]
    for r in rows:
        title_esc = (r.title or "").replace("|", "\\|")
        lines.append(f"| {r.mcg_code} | {title_esc} | `{r.url}` | {r.confidence} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def build_guideline_record(
    url: str,
    link_blob: str,
    source_toc: str,
    category_path: str,
    page_title: str,
    h1: str,
    body_text: str,
) -> Optional[GuidelineRecord]:
    if not looks_like_guideline_page(body_text, page_title, h1):
        return None

    combined_link = link_blob
    body_codes = extract_mcode_matches(body_text)
    title_codes = extract_mcode_matches(page_title + " " + h1)
    link_codes = extract_mcode_matches(combined_link)

    found_code_in = ""
    primary: Optional[tuple[str, str]] = None

    if link_codes:
        primary = pick_primary_code(link_codes, combined_link)
        found_code_in = "link_text"
    if primary is None and title_codes:
        primary = pick_primary_code(title_codes, page_title + " " + h1)
        found_code_in = "page_title"
    if primary is None and body_codes:
        primary = pick_primary_code(body_codes, body_text)
        found_code_in = "page_body"

    if primary is None:
        return None

    norm, orig = primary

    title_guess = ""
    found_title_in = ""
    h1t = (h1 or "").strip()
    if h1t:
        title_guess = h1t
        found_title_in = "h1"
    elif page_title.strip():
        title_guess = re.sub(r"\s*[|\-]\s*CareWeb.*$", "", page_title, flags=re.I).strip()
        found_title_in = "page_title"
    else:
        lt = (combined_link or "").strip()
        if lt:
            title_guess = lt
            found_title_in = "link_text"

    upper_body = body_text.upper()
    org_hit = "ORG" in upper_body
    clinical_hit = "CLINICAL INDICATIONS" in upper_body
    org_near = body_org_near_mcode(body_text, norm)

    conf = infer_confidence(found_code_in, found_title_in, org_hit, clinical_hit)

    evidence = {
        "found_code_in": found_code_in,
        "found_title_in": found_title_in,
        "source_toc_url": source_toc,
        "body_org_hit": org_hit,
        "body_clinical_indications_hit": clinical_hit,
        "body_org_near_mcode": org_near,
    }

    return GuidelineRecord(
        mcg_code=norm,
        mcg_original_code=orig,
        title=title_guess,
        url=url,
        confidence=conf,
        evidence=evidence,
        category_path=category_path,
        link_text=combined_link[:500],
        page_title=page_title.strip(),
    )


def run_discovery(
    start_url: str,
    product: str,
    out_dir: Path,
    max_guideline_pages: int,
    max_toc_pages: int,
    delay_min_ms: int,
    delay_max_ms: int,
    only_codes: Optional[set[str]],
    headless: bool,
) -> dict[str, Any]:
    if not is_allowed_careweb(start_url):
        raise SystemExit(f"Start URL must be https://{ALLOWED_NETLOC}/... got {start_url!r}")
    if not is_toc_url(start_url):
        print(f"Warning: start URL does not look like toc.pl: {start_url}", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    product_slug = product.strip().lower() or "isc"

    toc_pages_visited = 0
    guideline_pages_opened = 0

    toc_queue: deque[str] = deque([start_url])
    toc_seen: set[str] = set()
    candidates: dict[str, WorkingCandidate] = {}
    candidate_links_raw = 0

    toc_queue_remaining = 0
    by_code: dict[str, GuidelineRecord] = {}

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        while toc_queue and toc_pages_visited < max_toc_pages:
            toc_url = toc_queue.popleft()
            if toc_url in toc_seen:
                continue
            toc_seen.add(toc_url)

            _random_delay(delay_min_ms, delay_max_ms)
            try:
                page.goto(toc_url, wait_until="domcontentloaded", timeout=120_000)
            except PlaywrightTimeoutError:
                print(f"[TOC] timeout skipping {toc_url}", flush=True)
                continue
            toc_pages_visited += 1

            try:
                harvested = collect_links_from_page(page)
            except Exception as exc:
                print(f"[TOC] link harvest failed {toc_url}: {exc}", flush=True)
                harvested = []

            current = page.url.split("#")[0]
            for href, text, cat_hint in harvested:
                abs_url = absolutize(current, href)
                if not abs_url:
                    continue
                if is_toc_url(abs_url):
                    if abs_url not in toc_seen:
                        toc_queue.append(abs_url)
                    continue
                if is_isc_guideline_shape(abs_url):
                    candidate_links_raw += 1
                    merge_candidate(candidates, abs_url, text, toc_url, cat_hint)

            print(
                f"[TOC] visited {toc_pages_visited} pages | discovered {candidate_links_raw} candidate guidelines | "
                f"M codes {len(by_code)}",
                flush=True,
            )

        if toc_queue:
            toc_queue_remaining = len(toc_queue)
            print(
                f"[TOC] warning: TOC queue not empty ({toc_queue_remaining} remaining); "
                f"raise --max-toc-pages (current {max_toc_pages}) to crawl deeper.",
                flush=True,
            )

        # Inspect guideline URLs
        for url, wc in candidates.items():
            if guideline_pages_opened >= max_guideline_pages:
                print(
                    f"[TOC] max guideline opens ({max_guideline_pages}) reached before finishing inspection.",
                    flush=True,
                )
                break

            link_blob = " | ".join(wc.link_texts) if wc.link_texts else ""
            source_toc = wc.source_toc_urls[0] if wc.source_toc_urls else start_url
            category_path = " > ".join(wc.category_hints[:3]) if wc.category_hints else ""

            _random_delay(delay_min_ms, delay_max_ms)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            except PlaywrightTimeoutError:
                print(f"[TOC] guideline timeout {url}", flush=True)
                continue
            guideline_pages_opened += 1
            page_title = page.title()
            try:
                h1 = page.locator("h1").first.inner_text(timeout=5_000)
            except Exception:
                h1 = ""
            try:
                body_text = page.inner_text("body", timeout=15_000)
            except Exception:
                body_text = ""

            rec = build_guideline_record(
                url=url,
                link_blob=link_blob,
                source_toc=source_toc,
                category_path=category_path,
                page_title=page_title,
                h1=h1,
                body_text=body_text,
            )
            if rec is None:
                continue

            prev = by_code.get(rec.mcg_code)
            if prev is None:
                by_code[rec.mcg_code] = rec
            else:
                by_code[rec.mcg_code] = choose_best_record(prev, rec)

            print(
                f"[TOC] visited {toc_pages_visited} pages | discovered {candidate_links_raw} candidate guidelines | "
                f"M codes {len(by_code)}",
                flush=True,
            )

        context.close()
    finally:
        pw.stop()

    records = sort_by_mcode(list(by_code.values()))
    stats = {
        "toc_pages_visited": toc_pages_visited,
        "candidate_links_raw": candidate_links_raw,
        "guideline_pages_opened": guideline_pages_opened,
        "unique_mcodes": len(records),
        "total_navigations": toc_pages_visited + guideline_pages_opened,
        "max_toc_pages": max_toc_pages,
        "max_guideline_pages": max_guideline_pages,
        "product": product,
        "start_url": start_url,
        "toc_queue_remaining": toc_queue_remaining,
        "unique_isc_htm_urls": len(candidates),
    }

    json_path = out_dir / f"mcg_toc_index.{product_slug}.json"
    csv_path = out_dir / f"mcg_toc_index.{product_slug}.csv"
    md_path = out_dir / f"mcg_toc_index.{product_slug}.md"

    payload = {
        "meta": stats,
        "guidelines": [r.to_json_row() for r in records],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_csv(csv_path, records)
    write_md(md_path, records, stats)

    manifest_path = out_dir / "mcg_capture_manifest.remaining.json"

    def _mcode_sort_key(code: str) -> tuple[int, str]:
        mm = re.match(r"^M(\d+)$", code.strip(), re.I)
        if mm:
            return (int(mm.group(1)), code.upper())
        return (10**9, code.upper())

    manifest_summary: dict[str, Any] = {
        "manifest_path": str(manifest_path),
        "only_codes": sorted(only_codes, key=_mcode_sort_key) if only_codes else [],
        "found_codes": [],
        "missing_codes": [],
    }

    if only_codes is not None:
        manifest_rows: list[dict[str, Any]] = []
        found_set: set[str] = set()
        by_norm = {r.mcg_code.upper(): r for r in records}
        for code in sorted(only_codes, key=_mcode_sort_key):
            rec = by_norm.get(code.upper())
            if rec:
                found_set.add(rec.mcg_code.upper())
                manifest_rows.append(
                    {
                        "mcg_code": rec.mcg_code,
                        "mcg_original_code": rec.mcg_original_code,
                        "title": rec.title,
                        "product": product.upper(),
                        "org": "ORG",
                        "url": rec.url,
                        "url_status": "canonical",
                    }
                )
        manifest_summary["found_codes"] = sorted(found_set, key=_mcode_sort_key)
        manifest_summary["missing_codes"] = sorted(only_codes - found_set, key=_mcode_sort_key)
        manifest_path.write_text(json.dumps(manifest_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"[TOC] done: toc_pages={stats['toc_pages_visited']} "
        f"candidate_links={stats['candidate_links_raw']} unique_m_codes={stats['unique_mcodes']} "
        f"navigations={stats['total_navigations']}",
        flush=True,
    )

    return {"stats": stats, "manifest": manifest_summary if only_codes is not None else None}


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Discover ISC ORG guideline URLs from CareWeb TOC (indexing only).")
    p.add_argument("--start-url", required=True, help="TOC entry URL (toc.pl)")
    p.add_argument("--product", default="ISC", help="Product label for outputs (default ISC)")
    p.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / "rules" / "mcg" / "batch")
    p.add_argument(
        "--max-pages",
        type=int,
        default=1000,
        dest="max_guideline_pages",
        metavar="N",
        help="Max ISC guideline .htm pages to open for classification (default 1000)",
    )
    p.add_argument(
        "--max-toc-pages",
        type=int,
        default=500,
        metavar="N",
        help="Max TOC (toc.pl) pages to crawl before guideline inspection (default 500)",
    )
    p.add_argument("--delay-min-ms", type=int, default=100, help="Min delay between navigations")
    p.add_argument("--delay-max-ms", type=int, default=300, help="Max delay between navigations")
    p.add_argument(
        "--only-codes",
        default="",
        help="Comma-separated M-codes; writes mcg_capture_manifest.remaining.json when set",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless (persistent profile must already be logged in)",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    only = parse_only_codes(args.only_codes or None)
    headless = bool(args.headless) or _truthy_env_headless()
    summary = run_discovery(
        start_url=args.start_url.strip(),
        product=args.product.strip(),
        out_dir=args.out_dir.resolve(),
        max_guideline_pages=max(1, int(args.max_guideline_pages)),
        max_toc_pages=max(1, int(args.max_toc_pages)),
        delay_min_ms=max(0, int(args.delay_min_ms)),
        delay_max_ms=max(int(args.delay_min_ms), int(args.delay_max_ms)),
        only_codes=only,
        headless=headless,
    )
    if only is not None and summary.get("manifest"):
        print(json.dumps(summary["manifest"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
