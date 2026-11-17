"""Interactive OCR workbench engine.

A background thread pool runs the deterministic pipeline while the caller
feeds parameter-edit commands.  Every command returns within the configured
interaction budget: the newest fully-completed result summary is returned
immediately; when results for the newest parameters are not ready yet the
response carries ``E_INTERACT_BUDGET_PENDING`` and never blocks later
commands.  A graceful exit finishes the current stage snapshot and progress
record before returning exit code 0; budget exhaustion is never reported as a
terminated computation.

Ordering is independent of thread scheduling: each task carries a stable key
derived from file name, line index and stage, and assembled outputs are
re-sorted deterministically before leaving the engine.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .errors import E_INTERACT_BUDGET_PENDING, E_UNKNOWN_COMMAND, WorkbenchError
from .pipeline import StageCache, run_image
from .recognizer import build_templates
from .snapshot import SnapshotStore


def stable_task_key(name, line_index, stage):
    return f"{name:0>64}|{line_index:06d}|{stage}"


class Engine:
    def __init__(self, records, failures, config_bundle, workdir):
        self.records = sorted(records, key=lambda r: r["name"])
        self.failures = sorted(failures, key=lambda f: f["file"])
        self.config = config_bundle
        self.cache = StageCache()
        self.store = SnapshotStore(workdir)
        self.workdir = workdir
        self.global_version = 1
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._shutdown_requested = False
        self._finished = False
        self._exit_code = None
        self._current_config = config_bundle
        self._latest_results = []
        self._latest_version = 0
        self._completed_versions = set()
        self._dirty = True
        self.command_log = []
        self.recovered_version = None
        self._templates = build_templates(config_bundle.recognition)
        self._workers = max(1, config_bundle.detection["max_workers"])
        self._executor = ThreadPoolExecutor(
            max_workers=self._workers, thread_name_prefix="ocr-stage")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------
    def submit_command(self, command, budget_ms=None):
        started = time.perf_counter()
        budget = self.config.detection["interact_budget_ms"] \
            if budget_ms is None else budget_ms
        with self._lock:
            seq = command.get("seq", self.global_version)
            op = command.get("op")
            if op == "set_param":
                self._apply_param(command)
                self.global_version += 1
                self._dirty = True
                target_version = self.global_version
            elif op == "exit":
                self._shutdown_requested = True
                target_version = self.global_version
            elif op == "snapshot":
                target_version = self.global_version
            else:
                response = self._response(
                    seq, op, started, budget, pending=False,
                    target_version=target_version if op == "exit" else
                    self.global_version, code=E_UNKNOWN_COMMAND)
                self.command_log.append(response)
                return response
            self._wake.set()
            ready = target_version in self._completed_versions
            response = self._response(
                seq, op, started, budget, pending=not ready,
                target_version=target_version)
            self.command_log.append(response)
            return response

    def _apply_param(self, command):
        stage = command["stage"]
        param = command["param"]
        value = command["value"]
        if stage == "detection":
            self.config = self.config.with_overrides(
                det_overrides={param: value})
        elif stage == "recognition":
            self.config = self.config.with_overrides(
                rec_overrides={param: value})
        else:
            raise WorkbenchError(E_UNKNOWN_COMMAND, f"stage {stage}")
        self._templates = build_templates(self.config.recognition)

    # ------------------------------------------------------------------
    # Background computation
    # ------------------------------------------------------------------
    def _loop(self):
        while True:
            self._wake.wait(timeout=0.05)
            self._wake.clear()
            with self._lock:
                if self._shutdown_requested:
                    self._persist_locked()
                    self._finished = True
                    self._exit_code = 0
                    self._executor.shutdown(wait=True)
                    return
                if not self._dirty:
                    continue
                config = self.config
                version = self.global_version
                records = list(self.records)
                if version in self._completed_versions:
                    self._dirty = False
                    continue
            self._compute_version(version, config, records)

    def _compute_version(self, version, config, records):
        futures = []
        for index, record in enumerate(records):
            key = stable_task_key(record["name"], index, "pipeline")
            futures.append((key, index, self._executor.submit(
                self._run_one, record, config)))

        by_name = {}
        for key, index, future in futures:
            result = future.result()
            by_name[result.name] = (key, index, result)

        ordered = [by_name[record["name"]][2] for record in records]
        cache_keys = {
            result.name: {
                stage: result.products[stage].cache_key
                for stage in result.products
            }
            for result in ordered
        }
        with self._lock:
            self._latest_results = ordered
            self._latest_version = version
            self._current_config = config
            self._completed_versions.add(version)
            self._dirty = False
            interval = max(1, config.detection["snapshot_interval"])
            # The first completed version always commits so a fresh workdir
            # has an immediate recovery point; later commits follow the
            # configured snapshot interval.
            if version == 1 or version % interval == 0:
                self.store.commit(
                    version, config,
                    [r.name for r in ordered], ordered, cache_keys)

    def _run_one(self, record, config):
        return run_image(record, config, self.cache, self._templates)

    def _persist_locked(self):
        if not self._latest_results:
            return
        cache_keys = {
            result.name: {
                stage: result.products[stage].cache_key
                for stage in result.products
            }
            for result in self._latest_results
        }
        self.store.commit(
            self._latest_version, self._current_config,
            [r.name for r in self._latest_results],
            self._latest_results, cache_keys)

    # ------------------------------------------------------------------
    # Synchronous helper used by the CLI / tests
    # ------------------------------------------------------------------
    def wait_version(self, version, timeout=30):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            with self._lock:
                if version in self._completed_versions:
                    return list(self._latest_results)
            time.sleep(0.01)
        return None

    def wait_idle(self, timeout=30):
        return self.wait_version(self.global_version, timeout)

    # ------------------------------------------------------------------
    # Responses and summaries
    # ------------------------------------------------------------------
    def _response(self, seq, op, started, budget, pending,
                  target_version, code=None):
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            results = list(self._latest_results)
            latest_version = self._latest_version
            completed_lines = sum(len(r.lines) for r in results)
            line_texts = [
                {"image": r.name,
                 "lines": [line["text"] for line in r.lines]}
                for r in results
            ]
            affected = self._affected_lines_locked(results)
            progress = {
                "images_total": len(self.records),
                "images_completed": len(results),
                "failures": len(self.failures),
            }
        status = code if code else (
            E_INTERACT_BUDGET_PENDING if pending else "OK")
        return {
            "seq": seq,
            "op": op,
            "global_version": target_version,
            "latest_completed_version": latest_version,
            "completed_lines": completed_lines,
            "line_texts": line_texts,
            "affected_lines": affected,
            "progress": progress,
            "elapsed_ms": round(elapsed_ms, 3),
            "budget_ms": budget,
            "over_budget": elapsed_ms > budget,
            "status": status,
        }

    def _affected_lines_locked(self, results):
        affected = []
        for result in results:
            if result.recomputed_stages:
                affected.append({
                    "image": result.name,
                    "stages": sorted(result.recomputed_stages),
                    "line_count": len(result.lines),
                })
        return affected

    # ------------------------------------------------------------------
    # Shutdown / recovery
    # ------------------------------------------------------------------
    def request_exit(self):
        with self._lock:
            self._shutdown_requested = True
            self._wake.set()

    def wait_exit(self, timeout=30):
        self._thread.join(timeout=timeout)
        return 0 if self._finished else None

    def shutdown(self):
        self._executor.shutdown(wait=True)

    def recover_from_snapshot(self):
        snapshot = self.store.load()
        if snapshot is None:
            return None
        self.recovered_version = snapshot["global_version"]
        self.global_version = snapshot["global_version"]
        return snapshot
