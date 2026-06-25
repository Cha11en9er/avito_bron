"""Фоновая запись в Google Таблицу: парсинг Avito не ждёт API Sheets."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from google_sheets.batch_sync import PendingListingSync


_STOP = object()


@dataclass
class AsyncSheetSyncWorker:
    sh: Any
    settings: Any
    min_interval_s: float = 1.0
    log_urls: list[str] = field(default_factory=list)
    _q: queue.Queue = field(default_factory=queue.Queue, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _pending: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _last_sync_at: float = field(default=0.0, init=False, repr=False)
    last_submit_ok: bool = field(default=True, init=False, repr=False)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="sheet-sync", daemon=True)
        self._thread.start()

    def submit(self, item: PendingListingSync) -> None:
        with self._lock:
            self._pending += 1
        self._q.put(item)
        self.start()

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def drain(self, *, timeout: float | None = None) -> None:
        """Дождаться записи всех задач из очереди."""
        if timeout is None:
            self._q.join()
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending <= 0:
                    return
            time.sleep(0.2)
        self._q.join()

    def shutdown(self, *, wait: bool = True) -> None:
        self._q.put(_STOP)
        if wait and self._thread is not None:
            self._thread.join(timeout=120)

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        elapsed = time.monotonic() - self._last_sync_at
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _run(self) -> None:
        from google_sheets.sheet_session import init_parse_sheet_context
        from google_sheets.sync import sync_after_listing

        init_parse_sheet_context(self.sh, self.settings, log_urls=self.log_urls)

        while True:
            item = self._q.get()
            try:
                if item is _STOP:
                    break
                self._throttle()
                ok, _ = sync_after_listing(
                    self.sh,
                    self.settings,
                    item.record,
                    item.columns,
                    item.booking_prices,
                    item.listing_url,
                    removed=item.removed,
                    queue_next_index=item.queue_next_row,
                    availability_days=item.availability_days if not item.removed else None,
                    today=item.sheet_today,
                    save_progress=True,
                )
                self.last_submit_ok = ok
                self._last_sync_at = time.monotonic()
            except Exception as exc:
                self.last_submit_ok = False
                print(f"  ошибка записи {getattr(item, 'listing_url', '')[:60]}…: {exc}")
            finally:
                with self._lock:
                    self._pending = max(0, self._pending - 1)
                self._q.task_done()
