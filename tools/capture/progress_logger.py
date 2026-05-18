"""Structured terminal logging for MCG capture runs."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional


def _utc_iso_z() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _compact_json(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return json.dumps({"repr": repr(data)}, ensure_ascii=False, separators=(",", ":"))


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
