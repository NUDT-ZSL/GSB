"""Bounded-concurrency upload scheduler with resume and deterministic timing."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .chunker import build_manifest, read_chunk_bytes
from .client import mono_ms
from .errors import BusinessError, E_TRANSPORT


class Scheduler:
    """Schedule chunk uploads under a global cap and per-session quota.

    ``global_concurrency`` bounds total in-flight uploads across all sessions
    sharing this scheduler; ``session_quota`` bounds one session. Tasks queue
    on the semaphores and cannot bypass the limits. Every attempt records a
    stable start/end timestamp and attempt number in monotonic milliseconds.
    """

    def __init__(self, client, global_concurrency=4, session_quota=2,
                 max_retries=2):
        if global_concurrency < 1 or session_quota < 1:
            raise ValueError("concurrency limits must be >= 1")
        if session_quota > global_concurrency:
            raise ValueError("session quota cannot exceed global concurrency")
        self.client = client
        self.max_workers = global_concurrency
        self.session_quota = session_quota
        self.max_retries = max_retries
        self._global_slots = threading.BoundedSemaphore(global_concurrency)
        self._session_slots = {}
        self._slots_lock = threading.Lock()
        self._inflight = set()
        self._inflight_lock = threading.Lock()
        self.attempts = []

    def _session_semaphore(self, session_id):
        with self._slots_lock:
            semaphore = self._session_slots.get(session_id)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(self.session_quota)
                self._session_slots[session_id] = semaphore
            return semaphore

    def upload_chunk(self, session_id, index, data):
        """Upload one chunk, collapsing concurrent duplicates of the same index."""
        inflight_key = (session_id, index)
        with self._inflight_lock:
            if inflight_key in self._inflight:
                # Another thread already owns this exact session/index pair.
                duplicate = True
            else:
                self._inflight.add(inflight_key)
                duplicate = False
        if duplicate:
            return self._wait_for_registered(session_id, index)

        session_slots = self._session_semaphore(session_id)
        attempt_no = 0
        try:
            while True:
                attempt_no += 1
                started = mono_ms()
                try:
                    session_slots.acquire()
                    try:
                        self._global_slots.acquire()
                        try:
                            result = self.client.upload_chunk(
                                session_id, index, data)
                        finally:
                            self._global_slots.release()
                    finally:
                        session_slots.release()
                except BusinessError as error:
                    ended = mono_ms()
                    self.attempts.append({
                        "session_id": session_id, "index": index,
                        "attempt": attempt_no, "start_ms": started,
                        "end_ms": ended,
                        "duration_ms": max(0, ended - started), "ok": False,
                        "deduplicated": False, "error": error.code,
                    })
                    if error.code == E_TRANSPORT and attempt_no <= self.max_retries:
                        continue
                    raise
                ended = mono_ms()
                record = {
                    "session_id": session_id,
                    "index": index,
                    "attempt": attempt_no,
                    "start_ms": started,
                    "end_ms": ended,
                    "duration_ms": max(0, ended - started),
                    "ok": True,
                    "deduplicated": result.get("deduplicated", False),
                    "error": None,
                }
                self.attempts.append(record)
                return result
        finally:
            with self._inflight_lock:
                self._inflight.discard(inflight_key)

    def _wait_for_registered(self, session_id, index):
        # The server stores the authoritative single result; polling the
        # session guarantees concurrent duplicates converge to that result.
        while True:
            session = self.client.get_session(session_id)
            for chunk in session["chunks"]:
                if chunk["index"] == index and chunk["uploaded"]:
                    return {"session_id": session_id, "index": index,
                            "deduplicated": True, "digest": chunk["digest"]}
            time.sleep(0.005)

    def run(self, jobs):
        """Run ``jobs`` (list of dicts with session_id/index/data) concurrently."""
        results = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_map = {
                pool.submit(self.upload_chunk, job["session_id"], job["index"],
                            job["data"]): position
                for position, job in enumerate(jobs)
            }
            for future in as_completed(future_map):
                results[future_map[future]] = future.result()
        return results

    def ordered_attempts(self):
        return sorted(self.attempts, key=lambda item: (item["index"], item["attempt"]))


class UploadWorkflow:
    """High-level deterministic manifest + session + scheduler workflow."""

    def __init__(self, client, global_concurrency=4, session_quota=2):
        self.client = client
        self.global_concurrency = global_concurrency
        self.session_quota = session_quota

    def build_plan(self, file_path, chunk_size, algorithm=None):
        return build_manifest(file_path, chunk_size, algorithm)

    def start_upload(self, file_path, object_key, chunk_size, algorithm=None,
                     labels=None, session_id=None, expect_version=None,
                     only_indices=None):
        manifest = self.build_plan(file_path, chunk_size, algorithm)
        session = self.client.create_session(
            object_key, manifest, labels=labels, session_id=session_id,
            expect_version=expect_version)
        scheduler = Scheduler(self.client, self.global_concurrency,
                              self.session_quota)
        selected = set(only_indices) if only_indices is not None else None
        jobs = []
        for chunk in manifest["chunks"]:
            if selected is not None and chunk["index"] not in selected:
                continue
            data = read_chunk_bytes(file_path, chunk["offset"], chunk["length"])
            jobs.append({"session_id": session["session_id"],
                         "index": chunk["index"], "data": data})
        if jobs:
            scheduler.run(jobs)
        return {"session": self.client.get_session(session["session_id"]),
                "manifest": manifest,
                "attempts": scheduler.ordered_attempts()}

    def resume_upload(self, file_path, session_id, chunk_size, algorithm=None):
        """Resume from persisted state; already registered chunks are skipped."""
        session = self.client.get_session(session_id)
        if session["state"] in ("committed", "aborted"):
            raise BusinessError("E_STATE", "cannot resume a terminal session",
                                {"state": session["state"]})
        manifest = self.build_plan(file_path, chunk_size, algorithm)
        completed = set(session["completed"])
        scheduler = Scheduler(self.client, self.global_concurrency,
                              self.session_quota)
        jobs = []
        for chunk in manifest["chunks"]:
            if chunk["index"] in completed:
                continue
            data = read_chunk_bytes(file_path, chunk["offset"], chunk["length"])
            jobs.append({"session_id": session_id, "index": chunk["index"],
                         "data": data})
        if jobs:
            scheduler.run(jobs)
        return {"session": self.client.get_session(session_id),
                "attempts": scheduler.ordered_attempts(),
                "skipped": sorted(completed)}

    def complete(self, session_id, expect_version=None):
        self.client.verify_session(session_id)
        return self.client.commit_session(session_id, expect_version=expect_version)
