"""Workbench orchestration: edits, incremental decoding, concurrency,
fault-injection isolation, snapshot recovery and diagnostics.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

from . import decoders
from .cache import (
    INVALIDATION_MISSING,
    TrajectoryStore,
    atomic_write_json,
    cache_key,
    classify_invalidation,
)
from .convert import convert_policy
from .errors import (
    E_REQUEST_MISSING,
    E_SNAPSHOT_STALE,
    WorkbenchError,
    failure,
)
from .policy import RANGES, parse_policy_text
from .report import write_report
from .requests_io import load_requests
from .scoring import ScoreTable
from .snapshot import SnapshotManager, config_fingerprint

STAGE_PARSE = "parse"
STAGE_CONVERT = "convert"
STAGE_DECODE = "decode"
STAGE_SNAPSHOT = "snapshot"


def _read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class Workbench:
    def __init__(self, config, root="."):
        self.config = config
        self.root = root
        self.workdir = os.path.join(root, config["paths"]["workdir"])
        self.max_workers = int(config.get("max_workers", 1))
        self.top_n = int(config.get("candidate_top_n", 5))
        self.policy = None
        self.overrides = {}
        self.requests = []
        self.table = None
        self.vocab_size = 0
        self.downgrade_losses = []
        self.sidecar = {}
        self.conversions = []
        self.injection_hits = []
        self.failures = []
        self.stopped = False
        self._stop_requested = False
        self._prev_state = None
        self.store = TrajectoryStore(os.path.join(self.workdir, "traces"))
        self.snapshots = SnapshotManager(os.path.join(self.workdir, "snapshot"))

    # ------------------------------------------------------------------ I/O
    def _path(self, key):
        return os.path.join(self.root, self.config["paths"][key])

    def load_inputs(self):
        vocab_lines = _read_text(self._path("vocab")).strip().splitlines()
        self.vocab_size = len([ln for ln in vocab_lines if ln.strip()])
        # Subword rules are parsed (validated as readable); the deterministic
        # decoders operate on token ids directly.
        _ = _read_text(self._path("subword_rules"))
        self.table = ScoreTable.load(self._path("scores"))
        self.requests = load_requests(self._path("requests"))
        active = self.config.get("active_policy", "v2")
        key = "policy_v1" if active == "v1" else "policy_v2"
        self.policy = parse_policy_text(_read_text(self._path(key)), active)
        self._apply_injections()

    # ------------------------------------------------------------- injection
    def _apply_injections(self):
        inj_path = self._path("injection")
        if not os.path.exists(inj_path):
            return
        with open(inj_path, "r", encoding="utf-8") as fh:
            items = json.load(fh).get("items", [])
        for item in items:
            iid = item["id"]
            kind = item["kind"]
            if kind == "policy_line":
                try:
                    parse_policy_text(item["line"], item.get("version", "v1"))
                except WorkbenchError as exc:
                    self.injection_hits.append(iid)
                    self.failures.append(
                        failure(None, STAGE_PARSE, exc.code, exc.message, iid)
                    )
            elif kind == "policy_json_downgrade":
                try:
                    pol = parse_policy_text(json.dumps(item["doc"]), "v2")
                    convert_policy(pol, "v1", seed_width_rule="error")
                except WorkbenchError as exc:
                    self.injection_hits.append(iid)
                    self.failures.append(
                        failure(None, STAGE_CONVERT, exc.code, exc.message, iid)
                    )
            elif kind == "score_empty_entry":
                entries = dict(self.table._entries)
                entries[item["fingerprint"]] = []
                self.table = ScoreTable(entries)
                self.injection_hits.append(iid)
            else:
                raise WorkbenchError("E_INJECTION", f"unknown injection kind {kind!r}")

    # ----------------------------------------------------------------- edits
    def _validate_fields(self, fields):
        for name, value in fields.items():
            if name in RANGES:
                lo, hi = RANGES[name]
                if not (lo <= value <= hi):
                    raise WorkbenchError(
                        "E_POLICY_RANGE",
                        f"field {name!r} value {value!r} out of range [{lo}, {hi}]",
                    )
            elif name == "decoder_type":
                if value not in decoders.DECODERS:
                    raise WorkbenchError(
                        "E_POLICY_RANGE", f"unknown decoder_type {value!r}"
                    )
            elif name == "seed":
                if not (0 <= value <= 2**64 - 1):
                    raise WorkbenchError("E_POLICY_RANGE", "seed out of uint64 range")
            else:
                raise WorkbenchError(
                    "E_POLICY_UNKNOWN_KEY", f"unknown editable field {name!r}"
                )

    def edit_global_set(self, fields):
        """Global policy field modification; invalidates non-overridden traces."""
        self._validate_fields(fields)
        self.policy = self.policy.with_fields(**fields)

    def edit_override(self, request_id, fields=None, policy_text=None, version=None):
        """Per-request policy override; only affects that request."""
        if request_id not in {r.request_id for r in self.requests}:
            raise WorkbenchError(E_REQUEST_MISSING, f"no such request {request_id!r}")
        if policy_text is not None:
            policy = parse_policy_text(policy_text, version or "v2")
        else:
            self._validate_fields(fields or {})
            policy = self.policy.with_fields(**(fields or {}))
        self.overrides[request_id] = policy

    def edit_switch_version(self, target):
        """Explicit version switch; converts on demand and records losses."""
        source = self.policy.source_version
        converted, sidecar, losses = convert_policy(
            self.policy,
            target,
            temp_quantum=float(self.config.get("temp_quantum", 0.001)),
            seed_width_rule=self.config.get("seed_width_rule", "truncate"),
        )
        self.policy = converted
        self.sidecar = sidecar
        self.downgrade_losses = [e.to_dict() for e in losses]
        self.conversions.append(
            {
                "from": source,
                "to": target,
                "losses": self.downgrade_losses,
                "sidecar": sidecar,
            }
        )
        if sidecar:
            atomic_write_json(
                os.path.join(self.workdir, "bypass_metadata.json"), sidecar
            )

    def edit_delete_request(self, request_id):
        self.requests = [r for r in self.requests if r.request_id != request_id]
        self.overrides.pop(request_id, None)

    def apply_edit(self, edit):
        kind = edit["kind"]
        if kind == "global_set":
            self.edit_global_set(edit["fields"])
        elif kind == "override":
            self.edit_override(
                edit["request_id"],
                fields=edit.get("fields"),
                policy_text=edit.get("policy_text"),
                version=edit.get("version"),
            )
        elif kind == "switch_version":
            self.edit_switch_version(edit["target"])
        elif kind == "delete_request":
            self.edit_delete_request(edit["request_id"])
        elif kind == "replay":
            pass  # replay is a decode action, handled by replay()
        else:
            raise WorkbenchError("E_EDIT", f"unknown edit kind {kind!r}")

    def request_stop(self):
        self._stop_requested = True

    # ----------------------------------------------------------------- decode
    def _effective_policy(self, request_id):
        return self.overrides.get(request_id, self.policy)

    def _decode_one(self, request):
        policy = self._effective_policy(request.request_id)
        return decoders.decode(policy, self.table, request, top_n=self.top_n)

    def replay(self, request_ids=None):
        """Batch replay in stable request-ID order; equals one-by-one decoding."""
        ids = set(request_ids) if request_ids else None
        selected = [r for r in self.requests if ids is None or r.request_id in ids]
        selected.sort(key=lambda r: r.sort_key())
        return [self._decode_one(r) for r in selected]

    def _loss_marks_for(self, request_id):
        if request_id in self.overrides:
            return []
        return list(self.downgrade_losses)

    # -------------------------------------------------------------------- run
    def run(self):
        cfg_fp = config_fingerprint(self.config)
        snap_version = int(self.config.get("snapshot_version", 1))
        snap_status, manifest, reason = self.snapshots.validate(snap_version, cfg_fp)
        snapshot_info = {
            "status": snap_status,
            "recovered": False,
            "completed_requests": [],
            "resume_point": None,
        }
        if snap_status == "stale":
            self.failures.append(
                failure(None, STAGE_SNAPSHOT, E_SNAPSHOT_STALE, reason)
            )
            if self.config.get("recover_from_snapshot", False) and manifest:
                snapshot_info["recovered"] = True
                snapshot_info["completed_requests"] = manifest.get(
                    "completed_requests", []
                )
        elif snap_status == "hit" and manifest:
            completed = manifest.get("completed_requests", [])
            snapshot_info["recovered"] = bool(completed)
            snapshot_info["completed_requests"] = completed
            if completed:
                snapshot_info["resume_point"] = completed[-1]

        if self._prev_state is None and manifest:
            # Cross-process recovery: resume invalidation bookkeeping from the
            # last fully replaced manifest.
            self._prev_state = manifest.get("prev_state")

        policy_fp = self.policy.fingerprint()
        score_fp = self.table.fingerprint
        override_fps = {rid: p.fingerprint() for rid, p in self.overrides.items()}

        results = []
        to_compute = []
        completed_set = set(snapshot_info["completed_requests"])
        stats = {"hits": 0, "misses": 0}
        invalidations = {"policy": 0, "request": 0, "scoretable": 0, "missing": 0}
        new_keys = {}

        for request in self.requests:
            policy = self._effective_policy(request.request_id)
            key = cache_key(request.context_fingerprint, policy.fingerprint(), score_fp)
            new_keys[request.request_id] = key
            source = classify_invalidation(
                request.request_id, self._prev_state, score_fp, policy_fp, override_fps
            )
            if source is None and self.store.has(key):
                stats["hits"] += 1
                trace = self.store.load(key)
                completed_set.add(request.request_id)
                results.append(self._result_entry(request, trace, "reused", None))
            else:
                source = source or INVALIDATION_MISSING
                stats["misses"] += 1
                invalidations[source] += 1
                to_compute.append((request, key, source))

        computed = {}
        if to_compute and self.max_workers > 1:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {
                    pool.submit(self._decode_one, req): (req, key, src)
                    for req, key, src in to_compute
                }
                for fut in futures:
                    req, key, src = futures[fut]
                    try:
                        computed[req.request_id] = (fut.result(), key, src)
                    except WorkbenchError as exc:
                        computed[req.request_id] = (exc, key, src)
        else:
            for req, key, src in to_compute:
                if self._stop_requested:
                    self.stopped = True
                    break
                try:
                    computed[req.request_id] = (self._decode_one(req), key, src)
                except WorkbenchError as exc:
                    computed[req.request_id] = (exc, key, src)

        # Merge in stable request-ID order regardless of thread scheduling.
        run_failures = []
        for request, key, source in to_compute:
            entry = computed.get(request.request_id)
            if entry is None:
                continue  # stopped before this request ran
            trace, key, source = entry
            if isinstance(trace, WorkbenchError):
                run_failures.append(
                    failure(request.request_id, STAGE_DECODE, trace.code, trace.message)
                )
                continue
            self.store.save(key, trace)
            completed_set.add(request.request_id)
            results.append(self._result_entry(request, trace, "recomputed", source))

        results.sort(key=lambda e: e["request_id"])
        all_failures = sorted(
            self.failures + run_failures,
            key=lambda f: (str(f["request_id"]), f["stage"], f["code"]),
        )

        self._prev_state = {
            "keys": new_keys,
            "policy_fp": policy_fp,
            "score_fp": score_fp,
            "override_fps": override_fps,
        }

        manifest = {
            "global_version": snap_version,
            "config_fingerprint": cfg_fp,
            "policy_source_version": self.policy.source_version,
            "policy_fingerprint": policy_fp,
            "score_fingerprint": score_fp,
            "completed_requests": sorted(completed_set),
            "prev_state": self._prev_state,
            "artifacts": {
                "policy": snap_version,
                "traces": snap_version,
                "bypass": snap_version,
            },
        }
        self.snapshots.write_manifest(manifest)
        atomic_write_json(
            os.path.join(self.workdir, "policy.normalized.json"),
            self.policy.normalized(),
        )

        run_result = {
            "snapshot_version": snap_version,
            "snapshot": snapshot_info,
            "policy_source_version": self.policy.source_version,
            "policy_fingerprint": policy_fp,
            "score_fingerprint": score_fp,
            "requests": results,
            "decoder_summaries": self._decoder_summaries(results),
            "downgrade_losses": list(self.downgrade_losses),
            "cache_stats": {
                "hits": stats["hits"],
                "misses": stats["misses"],
                "invalidations": invalidations,
            },
            "conversions": list(self.conversions),
            "injection_hits": list(self.injection_hits),
            "failures": all_failures,
            "stopped": self.stopped,
        }
        write_report(os.path.join(self.workdir, "report.json"), run_result)
        return run_result

    def _result_entry(self, request, trace, status, invalidation):
        return {
            "request_id": request.request_id,
            "decoder": trace["decoder"],
            "tokens": trace["tokens"],
            "steps": trace["steps"],
            "finish_reason": trace["finish_reason"],
            "cache": {"status": status, "invalidation": invalidation},
            "loss_marks": self._loss_marks_for(request.request_id),
        }

    @staticmethod
    def _decoder_summaries(results):
        summaries = {}
        for entry in results:
            dec = summaries.setdefault(
                entry["decoder"], {"requests": 0, "total_steps": 0, "score_sum": 0.0}
            )
            dec["requests"] += 1
            dec["total_steps"] += len(entry["steps"])
            dec["score_sum"] = round(
                dec["score_sum"] + sum(s["score"] for s in entry["steps"]), 9
            )
        return summaries
