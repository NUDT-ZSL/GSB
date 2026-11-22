"""Pipeline orchestration: full runs, incremental commits, invalidation
propagation, concurrency control and resource limits."""
from __future__ import annotations

import fcntl
import json
import os

from . import nodes
from .cache import ContentCache, hash_bytes, serialize_artifact
from .config import Config
from .conv_core import estimate_block_resident_bytes, out_hw
from .deviation import compute_spectrum
from .errors import Code, PipelineError
from .sample_gen import read_jsonl
from .validation import validate_jobs, validate_param_row

WEIGHT_UPDATE_NODES = [nodes.WEIGHT_QUANT, nodes.BLOCKED_MAC,
                       nodes.REDUCE_DEQUANT]
CALIB_UPDATE_NODES = [nodes.REDUCE_DEQUANT]


class CommitLock:
    """Non-blocking advisory lock: a second concurrent committer gets
    E_STAGE_BUSY immediately instead of waiting."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "commit.lock")
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            self._fh = None
            raise PipelineError(Code.E_STAGE_BUSY,
                                "another incremental commit is in progress")
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False


class Pipeline:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.cache = ContentCache(cfg.cache_dir)
        self._state_path = os.path.join(cfg.state_dir, "state.json")
        self.state = self._load_state()

    def _load_state(self):
        if os.path.exists(self._state_path):
            with open(self._state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return {"registry": {}, "jobs": {}, "weight_params": {},
                "calib_stats": {}, "snapshot_seq": 0, "snapshots": []}

    def _save_state(self):
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2, sort_keys=True)
        os.replace(tmp, self._state_path)

    @property
    def registry(self):
        return self.state["registry"]

    def _next_snapshot_id(self):
        self.state["snapshot_seq"] += 1
        return f"snap-{self.state['snapshot_seq']:04d}"

    def _snapshot_dir(self, snap_id):
        return os.path.join(self.cfg.snapshot_dir, snap_id)

    def _run_chain(self, job, wparams, calib):
        """Run the four-node chain for one job. Returns (keys, reused)."""
        keys, reused = {}, {}
        key, _, r = nodes.run_node(self.cache, nodes.QUANT_INPUT_PREP,
                                   nodes.payload_quant_input_prep(job))
        keys[nodes.QUANT_INPUT_PREP], reused[nodes.QUANT_INPUT_PREP] = key, r
        key, _, r = nodes.run_node(
            self.cache, nodes.WEIGHT_QUANT,
            nodes.payload_weight_quant(job, wparams,
                                       keys[nodes.QUANT_INPUT_PREP]))
        keys[nodes.WEIGHT_QUANT], reused[nodes.WEIGHT_QUANT] = key, r
        key, _, r = nodes.run_node(
            self.cache, nodes.BLOCKED_MAC,
            nodes.payload_blocked_mac(job, wparams,
                                      keys[nodes.QUANT_INPUT_PREP],
                                      keys[nodes.WEIGHT_QUANT]))
        keys[nodes.BLOCKED_MAC], reused[nodes.BLOCKED_MAC] = key, r
        key, _, r = nodes.run_node(
            self.cache, nodes.REDUCE_DEQUANT,
            nodes.payload_reduce_dequant(job, wparams, calib,
                                         keys[nodes.BLOCKED_MAC]))
        keys[nodes.REDUCE_DEQUANT], reused[nodes.REDUCE_DEQUANT] = key, r
        return keys, reused

    def _lineage_record(self, job, wparams, calib, keys):
        out_key = keys[nodes.REDUCE_DEQUANT]
        artifact, _ = self.cache.get(out_key)
        return {
            "job_id": job["job_id"],
            "node_keys": keys,
            "weight_revision": int(wparams["weight_revision"]),
            "calib_revision": int(calib["calib_revision"]),
            "output_key": out_key,
            "output_sha256": hash_bytes(serialize_artifact(artifact)),
        }

    def _write_snapshot(self, snap_id, lineage, invalidation, spectra,
                        validation_errors, degradations):
        sdir = self._snapshot_dir(snap_id)
        os.makedirs(sdir, exist_ok=True)

        def dump(name, obj):
            with open(os.path.join(sdir, name), "w", encoding="utf-8") as fh:
                json.dump(obj, fh, indent=2, sort_keys=True)

        dump("lineage.json", lineage)
        dump("invalidation_report.json", invalidation)
        dump("deviation_spectrum.json", spectra)
        dump("validation_errors.json", validation_errors)
        dump("summary.json", {
            "snapshot_id": snap_id,
            "jobs": sorted(lineage.keys()),
            "affected_jobs": invalidation.get("affected_jobs", []),
            "degradations": degradations,
            "validation_error_count": len(validation_errors),
        })
        self.state["snapshots"].append(snap_id)
        self.state["latest_snapshot"] = snap_id
        self._save_state()
        return sdir

    def full_run(self, jobs, weight_params_rows, calib_rows):
        """Validate and execute all jobs; write a snapshot."""
        valid, val_errors = validate_jobs(jobs, self.registry)
        wp_by_job = {r["job_id"]: r for r in weight_params_rows}
        cal_by_job = {r["job_id"]: r for r in calib_rows}
        for job in list(valid):
            jid = job["job_id"]
            for kind, table in (("weight", wp_by_job), ("calib", cal_by_job)):
                if jid not in table:
                    val_errors.append({
                        "code": Code.E_BAD_MANIFEST,
                        "message": f"missing {kind} params for job",
                        "job_id": jid, "field": f"{kind}_params",
                        "node": nodes.WEIGHT_QUANT if kind == "weight"
                        else nodes.REDUCE_DEQUANT})
        valid = [j for j in valid
                 if j["job_id"] in wp_by_job and j["job_id"] in cal_by_job]

        lineage, spectra, recomputes, reuses = {}, {}, [], []
        tol_failures = []
        for job in valid:
            jid = job["job_id"]
            wp, cal = wp_by_job[jid], cal_by_job[jid]
            keys, reused = self._run_chain(job, wp, cal)
            for node in nodes.NODE_ORDER:
                (reuses if reused[node] else recomputes).append(
                    {"node": node, "job_id": jid, "key": keys[node]})
            artifacts = {n: self.cache.get(keys[n])[0]
                         for n in nodes.NODE_ORDER}
            try:
                spectra[jid] = compute_spectrum(
                    job, wp, cal, artifacts, self.cfg.tolerances)
            except PipelineError as exc:
                if exc.code != Code.E_TOLERANCE_EXCEEDED:
                    raise
                tol_failures.append(exc.to_dict())
            lineage[jid] = self._lineage_record(job, wp, cal, keys)
            self.registry[jid] = {
                "weight_revision": int(wp["weight_revision"]),
                "calib_revision": int(cal["calib_revision"])}
            self.state["jobs"][jid] = job
            self.state["weight_params"][jid] = wp
            self.state["calib_stats"][jid] = cal

        invalidation = {
            "kind": "full_run",
            "affected_nodes": list(nodes.NODE_ORDER),
            "affected_jobs": sorted(lineage.keys()),
            "recompute": recomputes,
            "reuse": reuses,
        }
        snap_id = self._next_snapshot_id()
        self._write_snapshot(snap_id, lineage, invalidation, spectra,
                             val_errors, [])
        return {"snapshot_id": snap_id, "lineage": lineage,
                "invalidation": invalidation, "spectra": spectra,
                "validation_errors": val_errors,
                "tolerance_failures": tol_failures}

    def _check_block_budget(self, job):
        """Return (effective_block_size, degradation|None)."""
        ish, ksh = job["input_shape"], job["kernel_shape"]
        oh, ow = out_hw(ish[2], ish[3], ksh[2], ksh[3],
                        job["stride"], job["padding"])
        rows = ish[0] * oh * ow
        k = ksh[0]
        limit = self.cfg.limits.max_block_resident_bytes
        bs = int(job["block_size"])
        if estimate_block_resident_bytes(bs, rows, k) <= limit:
            return bs, None
        eff = bs
        while eff > 1 and estimate_block_resident_bytes(eff, rows, k) > limit:
            eff //= 2
        return eff, {"category": "BLOCK_SIZE_DEGRADED",
                     "job_id": job["job_id"],
                     "from_block_size": bs, "to_block_size": eff,
                     "limit_bytes": limit}

    def commit(self, update_rows, kind):
        """Apply a higher-version weight/calib update incrementally.

        Only the dependent suffix of the DAG is recomputed; unaffected
        upstream artifacts are reused via the content-addressable cache.
        """
        if kind not in ("weight", "calib"):
            raise PipelineError(Code.E_BAD_MANIFEST,
                                f"unknown update kind '{kind}'")
        with CommitLock(self.cfg.state_dir):
            affected_nodes = (WEIGHT_UPDATE_NODES if kind == "weight"
                              else CALIB_UPDATE_NODES)
            rev_field = ("weight_revision" if kind == "weight"
                         else "calib_revision")

            val_errors = []
            updates = {}
            for row in update_rows:
                errs = validate_param_row(row, kind, self.registry)
                if errs:
                    val_errors.extend(errs)
                else:
                    updates[row["job_id"]] = row
            affected_jobs = sorted(updates.keys())

            if len(affected_jobs) > self.cfg.limits.max_recompute_jobs:
                raise PipelineError(
                    Code.E_RESOURCE_LIMIT,
                    "recompute job count exceeds limit",
                    affected=len(affected_jobs),
                    limit=self.cfg.limits.max_recompute_jobs)

            degradations = []
            planned_parallel = min(len(affected_jobs), 8)
            if planned_parallel > self.cfg.limits.max_parallel_nodes:
                degradations.append({
                    "category": "PARALLELISM_DEGRADED",
                    "from_parallel": planned_parallel,
                    "to_parallel": self.cfg.limits.max_parallel_nodes})
                planned_parallel = self.cfg.limits.max_parallel_nodes

            recompute, reuse = [], []
            lineage = {}
            spectra = {}
            tol_failures = []
            for jid in affected_jobs:
                job = dict(self.state["jobs"][jid])
                wp = dict(self.state["weight_params"][jid])
                cal = dict(self.state["calib_stats"][jid])
                if kind == "weight":
                    wp = updates[jid]
                else:
                    cal = updates[jid]
                job[rev_field] = int(updates[jid][rev_field])

                eff_bs, deg = self._check_block_budget(job)
                if deg:
                    degradations.append(deg)
                    job["block_size"] = eff_bs

                keys, reused = self._run_chain(job, wp, cal)
                for node in nodes.NODE_ORDER:
                    entry = {"node": node, "job_id": jid, "key": keys[node]}
                    if reused[node]:
                        reuse.append(entry)
                    else:
                        recompute.append(entry)

                artifacts = {n: self.cache.get(keys[n])[0]
                             for n in nodes.NODE_ORDER}
                try:
                    spectra[jid] = compute_spectrum(
                        job, wp, cal, artifacts, self.cfg.tolerances)
                except PipelineError as exc:
                    if exc.code != Code.E_TOLERANCE_EXCEEDED:
                        raise
                    tol_failures.append(exc.to_dict())

                lineage[jid] = self._lineage_record(job, wp, cal, keys)
                self.registry[jid][rev_field] = int(updates[jid][rev_field])
                self.state["jobs"][jid] = job
                self.state["weight_params"][jid] = wp
                self.state["calib_stats"][jid] = cal

            invalidation = {
                "kind": f"incremental_{kind}_update",
                "affected_nodes": affected_nodes,
                "affected_jobs": affected_jobs,
                "recompute": recompute,
                "reuse": reuse,
                "planned_parallel_nodes": planned_parallel,
            }
            snap_id = self._next_snapshot_id()
            self._write_snapshot(snap_id, lineage, invalidation, spectra,
                                 val_errors, degradations)
            return {"snapshot_id": snap_id, "lineage": lineage,
                    "invalidation": invalidation, "spectra": spectra,
                    "validation_errors": val_errors,
                    "degradations": degradations,
                    "tolerance_failures": tol_failures}

    def load_snapshot(self, snap_id):
        sdir = self._snapshot_dir(snap_id)
        out = {}
        for name in ("lineage", "invalidation_report", "deviation_spectrum",
                     "validation_errors", "summary"):
            path = os.path.join(sdir, name + ".json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    out[name] = json.load(fh)
            else:
                out[name] = {} if name != "validation_errors" else []
        return out

    def check_result(self, snap_id, job_id):
        """Replay a recorded output and reject stale / broken lineage."""
        snap = self.load_snapshot(snap_id)
        record = snap["lineage"].get(job_id)
        if record is None:
            raise PipelineError(Code.E_LINEAGE_BROKEN,
                                "no lineage record for job in snapshot",
                                job_id=job_id, snapshot=snap_id)
        artifact, ok = nodes.replay(self.cache, record["output_key"])
        if not ok:
            raise PipelineError(Code.E_LINEAGE_BROKEN,
                                "recorded output key does not reproduce the "
                                "recorded output hash",
                                job_id=job_id, snapshot=snap_id,
                                output_key=record["output_key"])
        cur = self.registry.get(job_id)
        diffs = {}
        if cur is not None:
            for f in ("weight_revision", "calib_revision"):
                if cur[f] != record[f]:
                    diffs[f] = {"recorded": record[f], "current": cur[f]}
        if diffs:
            raise PipelineError(
                Code.E_STALE_RESULT,
                "recorded result depends on superseded parameter versions",
                job_id=job_id, snapshot=snap_id, version_diff=diffs,
                output_key=record["output_key"])
        return {"job_id": job_id, "snapshot": snap_id,
                "output_key": record["output_key"],
                "output_sha256": record["output_sha256"],
                "replayed": True, "stale": False}


def load_config_and_pipeline(config_path):
    cfg = Config.load(config_path) if config_path else Config()
    return Pipeline(cfg)


def read_updates(path):
    return read_jsonl(path)
