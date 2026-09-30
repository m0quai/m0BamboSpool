# Single-flight background processing for printer 3MF files.
#
# The MQTT callback only queues work here.  The worker owns the transfer and
# publishes a small, synchronized status object for the web UI.

from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import urlparse

import tools_3mf
from logger import log


def make_job_key(task_id, subtask_id):
    # Build the same stable key for printer IDs regardless of their type.
    if task_id is None and subtask_id is None:
        return None
    task = "" if task_id is None else str(task_id)
    subtask = "" if subtask_id is None else str(subtask_id)
    return f"{task}:{subtask}"


class Job3MFManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run, name="3mf-worker", daemon=True)
        self._worker.start()

    def enqueue(self, job_key: str, source: str, callback: Callable[[str, str | None, dict, Exception | None], None]) -> dict:
        job_key = str(job_key or "unknown")
        with self._lock:
            current = self._jobs.get(job_key)
            if current and current.get("source") == source and current.get("state") in {
                "queued", "resolving", "downloading", "processing"
            }:
                log(f"[3MF] Doppeldownload verhindert: job={job_key} state={current['state']}")
                return dict(current)

            status = {
                "job_key": job_key,
                "state": "queued",
                "percent": None,
                "bytes_downloaded": 0,
                "bytes_total": 0,
                "speed_bytes_per_second": 0,
                "remote_path": None,
                "local_path": None,
                "started_at": None,
                "updated_at": time.time(),
                "elapsed_seconds": 0,
                "error": None,
                "source": source,
                "callback": callback,
            }
            self._jobs[job_key] = status
            self._cancel_events[job_key] = threading.Event()
            self._queue.put(job_key)
            log(f"[3MF] Job queued task={job_key}")
            return dict(status)

    def cancel(self, job_key: str | None, reason: str) -> bool:
        with self._lock:
            candidates = [
                key for key, status in self._jobs.items()
                if status.get("state") in {"queued", "resolving", "downloading", "processing"}
            ]
            if job_key is None or str(job_key) not in self._jobs:
                job_key = candidates[-1] if candidates else None
            if job_key is None:
                return False
            status = self._jobs.get(str(job_key))
            if not status or status.get("state") not in {"queued", "resolving", "downloading", "processing"}:
                return False
            event = self._cancel_events.get(str(job_key))
            if event:
                event.set()
            status.update(state="cancelled", error=reason, updated_at=time.time())
            log(f"[3MF] Aktiver Download abgebrochen: job={job_key}, grund={reason}")
            return True

    def get(self, job_key: str | None = None) -> dict:
        with self._lock:
            if job_key:
                return dict(self._jobs.get(str(job_key), {"job_key": str(job_key), "state": "idle"}))
            if not self._jobs:
                return {"job_key": None, "state": "idle"}
            latest = max(self._jobs.values(), key=lambda item: item.get("updated_at", 0))
            return dict(latest)

    def get_active(self) -> dict:
        with self._lock:
            active = [
                status for status in self._jobs.values()
                if status.get("state") in {"queued", "resolving", "downloading", "processing", "ready"}
            ]
            if not active:
                return {"job_key": None, "state": "idle"}
            return dict(max(active, key=lambda item: item.get("updated_at", 0)))

    def _update(self, job_key: str, **values) -> None:
        with self._lock:
            status = self._jobs.get(job_key)
            if not status:
                return
            status.update(values)
            status["updated_at"] = time.time()
            started = status.get("started_at")
            if started:
                status["elapsed_seconds"] = round(status["updated_at"] - started, 2)

    def _progress(self, job_key: str, downloaded: int, total: int) -> None:
        with self._lock:
            status = self._jobs.get(job_key) or {}
            started = status.get("started_at") or time.time()
            now = time.time()
            if now - status.get("last_progress_at", 0) < 0.25 and total and downloaded < total:
                return
            status["last_progress_at"] = now
        elapsed = max(time.time() - started, 0.001)
        percent = round(downloaded * 100 / total, 1) if total else None
        self._update(
            job_key,
            bytes_downloaded=int(downloaded or 0),
            bytes_total=int(total or 0),
            percent=percent,
            speed_bytes_per_second=round(downloaded / elapsed),
        )

    def _download(self, source: str, destination, job_key: str, cancel_event: threading.Event) -> str | None:
        parsed = urlparse(str(source or ""))
        if parsed.scheme in ("http", "https"):
            tools_3mf.download3mfFromCloud(source, destination, lambda done, total: self._progress(job_key, done, total), cancel_event)
            return None
        if parsed.scheme == "local":
            tools_3mf.download3mfFromLocalFilesystem(parsed.path, destination, lambda done, total: self._progress(job_key, done, total), cancel_event)
            return None
        if parsed.scheme in ("ftp", "ftps"):
            source = parsed.path or parsed.netloc
        return tools_3mf.download3mfFromFTP(
            source, destination, lambda done, total: self._progress(job_key, done, total), cancel_event
        )

    def _run(self) -> None:
        while True:
            job_key = self._queue.get()
            callback = None
            local_path = None
            try:
                with self._lock:
                    status = self._jobs.get(job_key)
                    if not status:
                        continue
                    source = status["source"]
                    callback = status["callback"]
                    cancel_event = self._cancel_events[job_key]
                    if cancel_event.is_set():
                        continue
                started = time.time()
                self._update(job_key, state="resolving", started_at=started)
                log(f"[3MF] Worker gestartet: job={job_key}, source={source!r}")
                download_started = time.monotonic()
                with tempfile.NamedTemporaryFile(suffix=".3mf", delete=False) as temp_file:
                    local_path = temp_file.name
                    self._update(job_key, state="downloading", local_path=local_path)
                    remote_path = self._download(source, temp_file, job_key, cancel_event)

                if cancel_event.is_set():
                    raise tools_3mf.DownloadCancelledError("Druck wurde abgebrochen")

                download_seconds = time.monotonic() - download_started
                download_bytes = os.path.getsize(local_path)
                log(
                    f"[3MF] Download abgeschlossen: job={job_key}, "
                    f"pfad={remote_path or source!r}, bytes={download_bytes}, "
                    f"dauer={download_seconds:.2f}s"
                )
                self._update(job_key, state="processing", remote_path=remote_path or source)
                parse_started = time.monotonic()
                metadata = tools_3mf.getMetaDataFromLocal3mf(local_path, remote_path or source)
                if not metadata:
                    raise RuntimeError("3MF-Metadaten konnten nicht gelesen werden")
                parse_seconds = time.monotonic() - parse_started
                log(
                    f"[3MF] Verarbeitung abgeschlossen: job={job_key}, "
                    f"dauer={parse_seconds:.2f}s, gesamt={time.time() - started:.2f}s"
                )
                if cancel_event.is_set():
                    raise tools_3mf.DownloadCancelledError("Druck wurde abgebrochen")
                self._update(job_key, state="ready", percent=100 if self.get(job_key).get("bytes_total") else None)
                if remote_path:
                    metadata["_remote_3mf_path"] = remote_path
                callback(job_key, local_path, metadata, None)
            except Exception as exc:
                cancelled = self._cancel_events.get(job_key, threading.Event()).is_set()
                if cancelled:
                    log(f"[3MF] Downloadabbruch bestätigt: job={job_key}")
                    self._update(job_key, state="cancelled", error=str(exc))
                else:
                    log(f"[3MF] Job error task={job_key}: {exc}")
                    self._update(job_key, state="error", error=str(exc))
                if callback and not cancelled:
                    try:
                        callback(job_key, None, {}, exc)
                    except Exception as callback_error:
                        log(f"[3MF] Fehler im Job-Callback task={job_key}: {callback_error}")
                if local_path:
                    try:
                        os.unlink(local_path)
                    except OSError:
                        pass
            finally:
                self._queue.task_done()


JOBS_3MF = Job3MFManager()
