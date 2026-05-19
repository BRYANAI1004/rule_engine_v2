"""Structured terminal logging + optional live capture progress (TTY-aware)."""

from __future__ import annotations

import json
import math
import sys
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TextIO


def _utc_iso_z() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _compact_json(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return json.dumps({"repr": repr(data)}, ensure_ascii=False, separators=(",", ":"))


def effective_progress_style(requested: str, stream: TextIO) -> str:
    """
    Map user-requested style to an effective style.

    ``bar`` falls back to ``line`` when ``stream`` is not a TTY (carriage return unsafe).
    """
    r = (requested or "bar").strip().lower()
    if r not in {"bar", "line", "silent"}:
        r = "bar"
    if r == "silent":
        return "silent"
    tty = hasattr(stream, "isatty") and bool(stream.isatty())
    if r == "bar" and not tty:
        return "line"
    return r


def _fmt_int_grouped(n: int) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _progress_bar(frac: float, width: int = 24) -> str:
    f = max(0.0, min(1.0, float(frac)))
    filled = int(round(width * f))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


class ProgressLogger:
    """Structured logs: [timestamp] [scope] [LEVEL] message {optional json}."""

    def __init__(self, scope: str) -> None:
        self.scope = scope

    def _emit(self, level: str, message: str, data: Optional[Any] = None) -> None:
        ts = _utc_iso_z()
        suffix = ""
        if data is not None:
            suffix = " " + _compact_json(data)
        print(f"[{ts}] [{self.scope}] [{level}] {message}{suffix}", flush=True)

    def info(self, message: str, data: Optional[Any] = None) -> None:
        self._emit("INFO", message, data)

    def step(self, message: str, data: Optional[Any] = None) -> None:
        self._emit("STEP", message, data)

    def success(self, message: str, data: Optional[Any] = None) -> None:
        self._emit("SUCCESS", message, data)

    def warn(self, message: str, data: Optional[Any] = None) -> None:
        self._emit("WARN", message, data)

    def error(self, message: str, data: Optional[Any] = None) -> None:
        self._emit("ERROR", message, data)


class Heartbeat:
    """Prints periodic INFO logs until stopped (event set)."""

    def __init__(
        self,
        interval_seconds: float,
        log: ProgressLogger,
        get_snapshot: Callable[[], dict[str, Any]],
    ) -> None:
        self.interval_seconds = interval_seconds
        self.log = log
        self.get_snapshot = get_snapshot
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()

        def _run() -> None:
            while not self._stop.wait(self.interval_seconds):
                snap = self.get_snapshot()
                self.log.info("Still running", snap)

        self._thread = threading.Thread(target=_run, name="mcg-capture-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 2.0)
            self._thread = None


class CaptureTerminalReporter:
    """
    Live terminal progress for a single MCG capture run.

    Designed so batch drivers can construct one reporter per guideline and/or add
    optional ``[Batch]`` prefix lines without coupling to capture internals.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        requested_style: str,
        mcg_code: str,
        mcg_title: str,
        max_definition_triggers: int,
        stream: Optional[TextIO] = None,
        batch_prefix: str = "",
    ) -> None:
        self._stream = stream or sys.stdout
        self.enabled = bool(enabled)
        self.requested_style = (requested_style or "bar").strip().lower()
        self.effective_style = effective_progress_style(self.requested_style, self._stream)
        self.mcg_code = mcg_code
        self.mcg_title = mcg_title
        self.max_definition_triggers = max(1, int(max_definition_triggers))
        self.batch_prefix = batch_prefix

        self._lock = threading.Lock()
        self._last_expand_emit = 0.0
        self._last_def_emit = 0.0
        self._def_line_dirty = False

        self._last_triggers_done: int = -1

    def _p(self, text: str, *, nl: bool = True) -> None:
        if not self.enabled or self.effective_style == "silent":
            return
        end = "\n" if nl else ""
        print(text, end=end, file=self._stream, flush=True)

    def _label(self, core: str) -> str:
        if self.batch_prefix:
            return f"{self.batch_prefix} {core}"
        return core

    def phase(self, phase_id: str, detail: str = "") -> None:
        """Emit a single phase line (never uses carriage return)."""
        if not self.enabled or self.effective_style == "silent":
            return
        msg = f"[Phase] {phase_id}"
        if detail:
            msg = f"{msg} — {detail}"
        self._p(self._label(msg))

    def expand_emit(
        self,
        *,
        pass_num: int,
        max_passes: int,
        js_invoked_unique: int,
        js_groups_total_seen: int,
        ui_clicks_this_pass: int,
        body_text_delta: int,
        outcome_so_far: str,
        force: bool = False,
    ) -> None:
        """Live expand line; throttled to ~0.5s unless ``force``."""
        if not self.enabled or self.effective_style == "silent":
            return
        now = time.monotonic()
        with self._lock:
            if not force:
                if (now - self._last_expand_emit) < 0.5:
                    return
            self._last_expand_emit = now

        delta_s = f"+{_fmt_int_grouped(body_text_delta)}" if body_text_delta >= 0 else _fmt_int_grouped(body_text_delta)
        js_part = f"{js_invoked_unique}/{max(1, js_groups_total_seen)}"
        core = (
            f"[Expand] pass {pass_num}/{max_passes} | js groups {js_part} | ui clicks {ui_clicks_this_pass} | "
            f"body {delta_s} chars | status {outcome_so_far}"
        )
        line = self._label(core)
        if self.effective_style == "bar":
            print(f"\r{line}", end="", file=self._stream, flush=True)
        else:
            print(line, file=self._stream, flush=True)

    def expand_finish_line(self) -> None:
        """End a carriage-return expand line so the next stdout line doesn't overwrite it."""
        if (
            not self.enabled
            or self.effective_style == "silent"
            or self.effective_style != "bar"
        ):
            return
        print(file=self._stream, flush=True)

    def definition_tick(
        self,
        *,
        triggers_done: int,
        max_triggers: int,
        popup_count: int,
        definition_count: int,
        failed: int,
        skipped: int,
        force: bool = False,
    ) -> None:
        """Update definition/popup queue progress (bar uses ``\\r``)."""
        if not self.enabled or self.effective_style == "silent":
            return
        now = time.monotonic()
        td = max(0, int(triggers_done))
        with self._lock:
            triggers_changed = td != self._last_triggers_done
            if (
                not force
                and not triggers_changed
                and (now - self._last_def_emit) < 0.5
            ):
                return
            self._last_def_emit = now
            self._last_triggers_done = td

        mt = max(1, int(max_triggers))
        frac = min(1.0, td / float(mt))
        pct_i = int(math.floor(frac * 100.0 + 0.5))
        pct_s = f"{pct_i}%"
        bar = _progress_bar(frac, width=24)
        head = f"[Definitions] {self.mcg_code} {self.mcg_title}"
        stats = (
            f"Triggers: {td}/{mt} | Popups captured: {popup_count} | Definitions: {definition_count} | "
            f"Failed: {failed} | Skipped: {skipped}"
        )
        if self.effective_style == "bar":
            block = f"{head}\n{stats}\n{bar} {pct_s}"
            print(f"\r{block}", end="", file=self._stream, flush=True)
            self._def_line_dirty = True
        else:
            print(f"{head} | {stats} | {bar} {pct_s}", file=self._stream, flush=True)

    def definition_finish_line(self) -> None:
        if not self.enabled or self.effective_style != "bar":
            return
        if self._def_line_dirty:
            print(file=self._stream, flush=True)
            self._def_line_dirty = False

    def batch_capture_line(
        self,
        *,
        index: int,
        total: int,
        status: str,
        definitions: int = 0,
        popups: int = 0,
        failed: int = 0,
    ) -> None:
        """Future batch driver hook; single-line event (no ``\\r``)."""
        if not self.enabled or self.effective_style == "silent":
            return
        core = f"[Batch] {index}/{total} {self.mcg_code} {self.mcg_title} | {status}"
        if status.upper() == "PASS" or definitions or popups or failed:
            core += f" | definitions {definitions} | popups {popups} | failed {failed}"
        self._p(self._label(core))

    def print_capture_summary(
        self,
        *,
        capture_status: str,
        expanded_html: bool,
        expanded_text: bool,
        expand_outcome: str,
        definitions_captured: int,
        total_popups: int,
        triggers_processed: int,
        failed_triggers: int,
        sentinel_check: str,
        manifest_path: str,
        audit_preflight_path: str,
    ) -> None:
        """Always printed when capture reporter is enabled (including ``silent`` live mode)."""
        if not self.enabled:
            return
        ratio_s = ""
        if triggers_processed > 0:
            ratio = 100.0 * float(failed_triggers) / float(triggers_processed)
            ratio_s = f"{ratio:.1f}%"

        lines = [
            "================ CAPTURE SUMMARY ================",
            f"MCG: {self.mcg_code} {self.mcg_title}",
            f"Capture status: {capture_status}",
            f"Expanded HTML: {'yes' if expanded_html else 'no'}",
            f"Expanded text: {'yes' if expanded_text else 'no'}",
            f"Expand outcome: {expand_outcome}",
            f"Definitions captured: {definitions_captured}",
            f"Total popups captured: {total_popups}",
            f"Triggers processed: {triggers_processed}",
            f"Failed triggers: {failed_triggers}",
            f"Failed ratio: {ratio_s or 'n/a'}",
            f"Sentinel check: {sentinel_check}",
            f"Manifest: {manifest_path}",
            f"Audit: {audit_preflight_path}",
            "=================================================",
        ]
        self.definition_finish_line()
        self.expand_finish_line()
        for ln in lines:
            print(ln, file=self._stream, flush=True)


def fresh_capture_progress(started_at_iso: str) -> dict[str, Any]:
    return {
        "stage": "init",
        "started_at": started_at_iso,
        "elapsed_seconds": 0,
        "current_url": "",
        "expand_pass": 0,
        "expand_click_count": 0,
        "warning_count": 0,
        "last_action": "",
    }


def update_elapsed(progress: dict[str, Any], started_monotonic: float) -> None:
    progress["elapsed_seconds"] = int(time.monotonic() - started_monotonic)


def read_preflight_sentinel_status(preflight_json_path: str) -> str:
    """Best-effort: map completeness preflight verdict to a short summary label."""
    try:
        p = Path(preflight_json_path)
        if not p.is_file():
            return "not_run"
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        v = str(data.get("verdict") or "").strip().upper()
        if v == "PASS":
            return "pass"
        if v in {"NEEDS_REVIEW", "NEEDS REVIEW"}:
            return "needs_review"
        return v.lower() or "unknown"
    except Exception:
        return "unavailable"
