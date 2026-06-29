"""Фоновая запись в Google Таблицу: парсинг Avito не ждёт API Sheets."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from google_sheets.batch_sync import PendingListingSync
from google_sheets.client import is_sheets_quota_error, set_api_min_interval

_STOP = object()
_QUOTA_REQUEUE_WAITS = (20.0, 40.0, 60.0, 90.0, 120.0)
_MAX_REQUEUE = len(_QUOTA_REQUEUE_WAITS)


@dataclass
class AsyncSheetSyncWorker:
    sh: Any
    settings: Any
    min_interval_s: float = 3.5
    log_urls: list[str] = field(default_factory=list)
    _q: queue.Queue = field(default_factory=queue.Queue, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _pending: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _last_sync_at: float = field(default=0.0, init=False, repr=False)
    _last_progress_row: int | None = field(default=None, init=False, repr=False)
    _finalized: bool = field(default=False, init=False, repr=False)
    last_submit_ok: bool = field(default=True, init=False, repr=False)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        set_api_min_interval(self.min_interval_s)
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

    def drain(self, *, timeout: float | None = None) -> int | None:
        """Дождаться записи всех задач из очереди. Возвращает следующую строку листа."""
        if self._finalized:
            return self._last_progress_row
        if timeout is None:
            self._q.join()
            return self._flush_progress()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending <= 0 and self._q.unfinished_tasks == 0:
                    return self._flush_progress()
            time.sleep(0.2)
        self._q.join()
        return self._flush_progress()

    def finalize_sync(self) -> int | None:
        """Конец прогона: дождаться всей очереди и сохранить прогресс в настройках."""
        if self._finalized:
            return self._last_progress_row
        row = self.drain()
        self._finalized = True
        return row

    def shutdown(self, *, wait: bool = True) -> None:
        if not self._finalized:
            self.finalize_sync()
        self._q.put(_STOP)
        if wait and self._thread is not None:
            self._thread.join(timeout=180)

    def _flush_progress(self) -> int | None:
        row = self._last_progress_row
        if row is None or self.sh is None or self.settings is None:
            return row
        try:
            from google_sheets.iterations import current_queue_len, save_iteration_progress

            save_iteration_progress(
                self.sh,
                self.settings,
                row,
                total_urls=current_queue_len(),
            )
        except Exception:
            return row
        return row

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        elapsed = time.monotonic() - self._last_sync_at
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _sync_one(self, item: PendingListingSync) -> None:
        from google_sheets.sync import sync_after_listing

        ok, _ = sync_after_listing(
            self.sh,
            self.settings,
            item.record,
            item.columns,
            item.booking_prices,
            item.listing_url,
            removed=item.removed,
            queue_next_index=None,
            log_sheet_row=item.log_sheet_row,
            availability_days=item.availability_days if not item.removed else None,
            today=item.sheet_today,
            save_progress=False,
        )
        self.last_submit_ok = ok
        if item.queue_next_row is not None:
            self._last_progress_row = item.queue_next_row
        self._last_sync_at = time.monotonic()

    def _run(self) -> None:
        from google_sheets.sheet_session import init_parse_sheet_context

        init_parse_sheet_context(
            self.sh,
            self.settings,
            log_urls=self.log_urls,
            skip_logs_ensure=True,
        )

        while True:
            item = self._q.get()
            requeue = False
            try:
                if item is _STOP:
                    break
                self._throttle()
                self._sync_one(item)
            except Exception as exc:
                self.last_submit_ok = False
                if is_sheets_quota_error(exc) and item.attempts < _MAX_REQUEUE:
                    item.attempts += 1
                    wait = _QUOTA_REQUEUE_WAITS[item.attempts - 1]
                    print(
                        f"  лимит Google API, повтор {item.attempts}/{_MAX_REQUEUE} "
                        f"через {wait:.0f} с…"
                    )
                    time.sleep(wait)
                    self._q.put(item)
                    requeue = True
                else:
                    print(
                        f"  ошибка записи {getattr(item, 'listing_url', '')[:60]}…: {exc}"
                    )
            finally:
                if not requeue:
                    with self._lock:
                        self._pending = max(0, self._pending - 1)
                self._q.task_done()

        if not self._finalized:
            self._flush_progress()
