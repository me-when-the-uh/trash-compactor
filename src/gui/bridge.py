from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

from .message_types import GuiResponse

_PROGRESS_TYPES = frozenset({"ProgressUpdate", "Status"})

EmitFn = Callable[[list[dict[str, Any]]], None]


class GuiBridge:
    """Thread-safe latest-wins mailbox plus a 50ms dispatch pump."""

    def __init__(self, interval: float = 0.05) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._progress: Optional[dict[str, Any]] = None
        self._summary_current: Optional[dict[str, Any]] = None
        self._summary_total: Optional[dict[str, Any]] = None
        self._summary_dirs: dict[str, dict[str, Any]] = {}
        self._fifo: list[dict[str, Any]] = []
        self._wake = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._emit: Optional[EmitFn] = None

    def start(self, emit: EmitFn) -> None:
        if self._running:
            return
        self._emit = emit
        self._running = True
        self._thread = threading.Thread(
            target=self._pump_loop,
            name="gui-bridge-pump",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None
        self._emit = None

    def enqueue(self, response: GuiResponse) -> None:
        try:
            payload = json.loads(response.to_json())
        except Exception:
            logging.debug("GUI mailbox dropped an unserialisable response", exc_info=True)
            return
        if not isinstance(payload, dict):
            return
        msg_type = str(payload.get("type") or "")
        with self._lock:
            if msg_type in _PROGRESS_TYPES:
                self._progress = payload
            elif msg_type == "FolderSummary":
                self._store_summary(payload)
            else:
                self._fifo.append(payload)
                self._wake.set()

    def take_for_dispatch(self) -> list[dict[str, Any]]:
        """Drain coalesced slots then the discrete FIFO.

        Progress and summaries go first so a trailing ``Stopped`` still sees
        the final totals in the same batch.
        """
        with self._lock:
            items: list[dict[str, Any]] = []
            if self._progress is not None:
                items.append(self._progress)
                self._progress = None
            if self._summary_current is not None:
                items.append(self._summary_current)
                self._summary_current = None
            if self._summary_total is not None:
                items.append(self._summary_total)
                self._summary_total = None
            if self._summary_dirs:
                items.extend(self._summary_dirs.values())
                self._summary_dirs = {}
            if self._fifo:
                items.extend(self._fifo)
                self._fifo = []
            return items

    def _store_summary(self, payload: dict[str, Any]) -> None:
        scope = payload.get("scope") or ""
        if scope == "total":
            self._summary_total = payload
        elif scope == "directory":
            key = payload.get("directory") or ""
            self._summary_dirs[key] = payload
        elif scope == "current":
            self._summary_current = payload
        else:
            self._summary_current = payload
            self._summary_total = payload

    def _pump_loop(self) -> None:
        while True:
            self._wake.wait(timeout=self._interval)
            self._wake.clear()
            stopping = not self._running
            items = self.take_for_dispatch()
            if items:
                emit = self._emit
                if emit is not None:
                    try:
                        emit(items)
                    except Exception:
                        logging.debug("GUI pump dispatch failed", exc_info=True)
            if stopping:
                break
