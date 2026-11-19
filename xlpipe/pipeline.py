"""Pipeline orchestration: stage state machine, recovery, staleness.

Recovery contract:
  * committed stage snapshots are loaded, never recomputed;
  * uncommitted in-progress work lives under state/working and is wiped
    when a StageStore opens, so a killed stage restarts from its origin;
  * a stage is re-run wholesale (version + 1) when its dependencies
    (upstream version, resource versions, config hash, input hash) changed.

Kill-switch demo hooks (environment variables):
  XLPIPE_KILL_AT="stage:count"   os._exit() after `count` pairs in `stage`
  XLPIPE_KILL_BEFORE_COMMIT="stage"  os._exit() right before the commit
"""

import json
import os

from .errors import E_INPUT_PARSE, E_RESOURCE_LIMIT, PipelineError
from .resources import (
    KIND_LANG_PAIRS,
    KIND_TRANSLATIONS,
    KIND_WEIGHTS,
    ResourceBundle,
)
from .stages import (
    STAGE_ALIGN,
    STAGE_NORMALIZE,
    STAGE_ORDER,
    STAGE_SCORE,
    STAGE_SUMMARIZE,
    WorkingBuffer,
    stage_align,
    stage_normalize,
    stage_score,
    stage_summarize,
)
from .store import StageStore
from .util import sha256_hex


def load_pairs(data_path):
    pairs = []
    with open(data_path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                pairs.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise PipelineError(
                    E_INPUT_PARSE,
                    f"input line {lineno} is not valid JSON: {exc}",
                    stage=STAGE_NORMALIZE,
                    pair_id=f"line:{lineno}") from exc
    pairs.sort(key=lambda r: str(r.get("pair_id")))
    return pairs


class Pipeline:
    def __init__(self, data_path, resources_dir, config, state_dir):
        self.data_path = data_path
        self.resources_dir = resources_dir
        self.cfg = config
        self.pairs = load_pairs(data_path)
        with open(data_path, "rb") as fh:
            self.input_hash = sha256_hex(fh.read())
        self.resources = ResourceBundle.load(resources_dir)
        self.store = StageStore(state_dir)
        self.events = []  # executed stage transitions, for traceability

    # ---------------- dependency tracking ----------------

    def desired_deps(self, stage):
        versions = self.resources.versions()
        cfg_hash = self.cfg.config_hash()
        if stage == STAGE_NORMALIZE:
            return {"upstream": None, "input": self.input_hash,
                    "resources": {}, "config": cfg_hash}
        if stage == STAGE_ALIGN:
            return {"upstream": self._committed_version(STAGE_NORMALIZE),
                    "input": self.input_hash,
                    "resources": {
                        KIND_LANG_PAIRS: versions[KIND_LANG_PAIRS],
                        KIND_TRANSLATIONS: versions[KIND_TRANSLATIONS]},
                    "config": cfg_hash}
        if stage == STAGE_SCORE:
            return {"upstream": self._committed_version(STAGE_ALIGN),
                    "input": self.input_hash,
                    "resources": {KIND_WEIGHTS: versions[KIND_WEIGHTS]},
                    "config": cfg_hash}
        return {"upstream": self._committed_version(STAGE_SCORE),
                "input": self.input_hash,
                "resources": {KIND_WEIGHTS: versions[KIND_WEIGHTS]},
                "config": cfg_hash}

    def _committed_version(self, stage):
        committed = self.store.committed(stage)
        return committed["version"] if committed else None

    def is_stale(self, stage):
        committed = self.store.committed(stage)
        if not committed:
            return True
        meta = self.store.load_meta(stage, committed["version"])
        return meta.get("deps") != self.desired_deps(stage)

    # ---------------- stage execution ----------------

    def _stage_records(self, stage):
        if stage == STAGE_NORMALIZE:
            return stage_normalize(self.pairs, self.cfg)
        if stage == STAGE_ALIGN:
            upstream = self.store.iter_snapshot(
                STAGE_NORMALIZE, self._committed_version(STAGE_NORMALIZE))
            return stage_align(upstream, self.resources, self.cfg)
        if stage == STAGE_SCORE:
            upstream = self.store.iter_snapshot(
                STAGE_ALIGN, self._committed_version(STAGE_ALIGN))
            return stage_score(upstream, self.resources.weights)
        upstream = self.store.iter_snapshot(
            STAGE_SCORE, self._committed_version(STAGE_SCORE))
        return stage_summarize(upstream, self.resources.weights)

    def _run_stage(self, stage, version):
        from .stages import DEGRADE_MEM_SPILL
        limit = self.cfg.limits.max_pairs_per_stage
        working_path = os.path.join(self.store.working_dir, f"{stage}.jsonl")
        buf = WorkingBuffer(working_path, self.cfg.limits.max_working_bytes)
        kill_at = os.environ.get("XLPIPE_KILL_AT", "")
        count = 0
        for record in self._stage_records(stage):
            count += 1
            if count > limit:
                raise PipelineError(
                    E_RESOURCE_LIMIT,
                    f"stage {stage} exceeds max_pairs_per_stage={limit}",
                    stage=stage, pair_id=record.get("pair_id"))
            buf.append(record)
            if kill_at == f"{stage}:{count}":
                os._exit(3)  # simulated hard kill: no cleanup, no commit
        if os.environ.get("XLPIPE_KILL_BEFORE_COMMIT", "") == stage:
            os._exit(4)
        data = buf.finalize()
        degradations = []
        if buf.spilled:
            degradations.append(DEGRADE_MEM_SPILL)
        meta = {
            "pair_count": count,
            "deps": self.desired_deps(stage),
            "degradations": degradations,
        }
        if stage == STAGE_ALIGN:
            meta["mapping_conflicts"] = self.resources.conflicts
        return self.store.commit(stage, version, data, meta)

    def run(self):
        for stage in STAGE_ORDER:
            committed = self.store.committed(stage)
            if committed and not self.is_stale(stage):
                self.events.append({
                    "stage": stage, "action": "skip_committed",
                    "version": committed["version"],
                })
                continue
            version = (committed["version"] if committed else 0) + 1
            meta = self._run_stage(stage, version)
            self.events.append({
                "stage": stage, "action": "committed",
                "version": meta["version"], "checksum": meta["checksum"],
                "pair_count": meta["pair_count"],
            })
        return self.events
