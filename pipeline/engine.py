"""Pipeline orchestration: concurrency, snapshots, faults and reports."""

from __future__ import annotations

import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor


from .atomicio import atomic_write_json
from .config import FusionConfig, load_fusion_config
from .errors import PipelineError, STAGE_ALIGN, STAGE_FUSION
from .fusion import ACCEPTED, REJECTED, fuse_round
from .hashing import content_hash
from .injection import (FAULT_ASSERTION_MISSING, FAULT_IMAGE_TRUNCATED,
                        FAULT_NON_FINITE, FAULT_REGION_OUT_OF_RANGE,
                        FAULT_RELATION_DANGLING, Injection,
                        active_injections, apply_assertion_fault,
                        apply_feature_fault, apply_image_fault,
                        apply_relation_fault, load_injection_config,
                        routing_for)
from .report import build_report, failure_sort_key, hit_sort_key, task_sort_key
from .samples import (_decode_png, _validate_assertions, discover_sample_dirs,
                      load_sample)
from .snapshot import (Snapshot, failure_path, load_snapshot,
                       snapshot_path, write_failures, write_snapshot)
from .textrel import (align_relations, load_relation_table, text_features)
from .vision import quantization_diagnostics, visual_pipeline


class GracefulExit(Exception):
    """Raised inside the run loop after a handleable exit signal."""


class PipelineEngine:
    """Deterministic engine whose output is independent of thread scheduling."""

    def __init__(self, config: FusionConfig, relations, injections,
                 work_dir: str, relation_hash: str, injection_hash: str):
        self.config = config
        self.relations = relations
        self.injections = injections
        self.work_dir = work_dir
        self.relation_hash = relation_hash
        self.injection_hash = injection_hash
        self.config_hash = config.hash()
        self._lock = threading.Lock()
        self._graceful = threading.Event()
        self._interrupt_after_round = self._interrupt_setting()

    @staticmethod
    def _interrupt_setting() -> int | None:
        value = os.environ.get("PIPELINE_INTERRUPT_AFTER_ROUND")
        if value is None or value == "":
            return None
        return int(value)

    # ------------------------------------------------------------------
    # Signal handling: finish the current round, persist, exit code 0.
    # ------------------------------------------------------------------
    def install_signal_handlers(self) -> None:
        def handler(signum, frame):  # pragma: no cover - exercised via tests
            self._graceful.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # non-main threads / restricted env
                pass

    # ------------------------------------------------------------------
    # Per-sample, per-round processing
    # ------------------------------------------------------------------
    def _inject_failure(self, injection: Injection, exc: PipelineError,
                        sample: str, round_index: int) -> PipelineError:
        code, stage = routing_for(injection.fault)
        return PipelineError(code, stage, str(exc))

    def _process_sample_round(self, sample, round_index, active):
        config = self.config

        # --- visual feature stage (image truncation lands here) ---
        png_bytes = sample.png_bytes
        visual_faults = [item for item in active
                         if item.fault == FAULT_IMAGE_TRUNCATED]
        if visual_faults:
            injection = visual_faults[0]
            png_bytes = apply_image_fault(png_bytes, injection)
        try:
            image = _decode_png(png_bytes, config.image_width,
                                config.image_height)
            visual = visual_pipeline(image, config)
        except PipelineError as exc:
            if visual_faults:
                code, stage = routing_for(FAULT_IMAGE_TRUNCATED)
                raise PipelineError(code, stage, str(exc)) from exc
            raise

        # --- text feature + relation alignment stage ---
        assertions = sample.assertions
        align_faults = [item for item in active if item.fault in (
            FAULT_ASSERTION_MISSING, FAULT_RELATION_DANGLING,
            FAULT_REGION_OUT_OF_RANGE)]
        if any(item.fault == FAULT_ASSERTION_MISSING for item in align_faults):
            injection = next(item for item in align_faults
                             if item.fault == FAULT_ASSERTION_MISSING)
            assertions = apply_assertion_fault(assertions, injection)
        try:
            _validate_assertions(assertions)
        except PipelineError as exc:
            injection = next(item for item in align_faults
                             if item.fault == FAULT_ASSERTION_MISSING)
            code, stage = routing_for(FAULT_ASSERTION_MISSING)
            raise PipelineError(code, stage, str(exc)) from exc

        relation_set = list(self.relations)
        for injection in align_faults:
            if injection.fault in (FAULT_RELATION_DANGLING,
                                   FAULT_REGION_OUT_OF_RANGE):
                relation_set = apply_relation_fault(
                    relation_set, injection, config)
        try:
            usable, _dangling = align_relations(
                relation_set,
                {entry["assertion_id"]
                 for entry in assertions["assertions"]},
                config.region_count, strict=bool(align_faults))
        except PipelineError as exc:
            injection = next(item for item in align_faults
                             if item.fault in (FAULT_RELATION_DANGLING,
                                               FAULT_REGION_OUT_OF_RANGE))
            code, stage = routing_for(injection.fault)
            raise PipelineError(code, stage, str(exc)) from exc

        text = text_features(assertions, config.feature_dim)

        # --- fusion stage (non-finite injection lands here) ---
        fusion_faults = [item for item in active
                         if item.fault == FAULT_NON_FINITE]
        if fusion_faults:
            visual = apply_feature_fault(visual, fusion_faults[0])
        try:
            record = fuse_round(visual, text, usable, config, round_index)
        except PipelineError as exc:
            if fusion_faults:
                code, stage = routing_for(FAULT_NON_FINITE)
                raise PipelineError(code, stage, str(exc)) from exc
            raise
        return record

    def _failure_record(self, sample_name, round_index, stage, code,
                        message, injection):
        return {
            "sample": sample_name,
            "round": round_index,
            "stage": stage,
            "code": code,
            "message": message,
            "injection_id": injection.injection_id if injection else None,
        }

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------
    def run(self, input_dir: str):
        config = self.config
        manifest = [
            os.path.basename(path) for path in discover_sample_dirs(input_dir)
        ]

        # Load and validate every sample up front; invalid samples become
        # E_SAMPLE_INVALID failure records and never enter round processing.
        loaded = {}
        failures: list[dict] = []
        for name in manifest:
            try:
                loaded[name] = load_sample(
                    os.path.join(input_dir, name), config.image_width,
                    config.image_height)
            except PipelineError as exc:
                failures.append(self._failure_record(
                    name, -1, exc.stage, exc.code, str(exc), None))

        active_samples = sorted(
            name for name in manifest if name in loaded)
        resumed = load_snapshot(
            self.work_dir, self.config_hash, self.relation_hash,
            self.injection_hash)
        if resumed is not None:
            version = resumed.version
            start_round = resumed.round + 1
            rounds = {key: list(value)
                      for key, value in resumed.rounds.items()}
            score_summary = {key: dict(value)
                             for key, value in resumed.score_summary.items()}
            failed_samples = set(resumed.failed_samples)
            injection_hits = list(resumed.injection_hits)
            failures.extend(resumed.failures)
            static_dangling = list(resumed.static_dangling)
        else:
            # Static dangling relations are validated once per fresh run,
            # never treated as associations, and recorded regardless of
            # injected faults. They are persisted in the first snapshot.
            assertion_ids = set()
            for sample in loaded.values():
                assertion_ids.update(
                    entry["assertion_id"]
                    for entry in sample.assertions["assertions"])
            _usable_relations, static_dangling = align_relations(
                self.relations, assertion_ids, config.region_count,
                strict=False)
            for entry in static_dangling:
                failures.append({
                    "sample": None,
                    "round": -1,
                    "stage": STAGE_ALIGN,
                    "code": "E_RELATION_DANGLING",
                    "message": "static relation references missing target: "
                               + ",".join(entry["reasons"]),
                    "injection_id": None,
                    "relation_id": entry["relation_id"],
                })
            version = 0
            start_round = 0
            rounds = {name: [] for name in active_samples}
            score_summary = {name: {} for name in active_samples}
            failed_samples: set[str] = set()
            injection_hits = []

        graceful_exit = False
        for round_index in range(start_round, config.max_rounds):
            eligible = [name for name in active_samples
                        if name not in failed_samples]
            ordered = sorted(
                eligible, key=lambda name: task_sort_key(name, round_index))
            max_workers = max(1, min(config.concurrency, len(ordered) or 1))

            round_results: dict[str, object] = {}
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_map = {
                    pool.submit(
                        self._process_sample_round,
                        loaded[name], round_index,
                        active_injections(
                            self.injections, name, round_index)): name
                    for name in ordered
                }
                for future in future_map:
                    name = future_map[future]
                    try:
                        round_results[name] = future.result()
                    except PipelineError as exc:
                        round_results[name] = exc
                    except Exception as exc:  # defensive isolation boundary
                        round_results[name] = PipelineError(
                            "E_FUSION_ERROR", STAGE_FUSION, repr(exc))

            # Commit this round in deterministic sample-name order.
            for name in ordered:
                result = round_results[name]
                hits = active_injections(
                    self.injections, name, round_index)
                if isinstance(result, PipelineError):
                    failed_samples.add(name)
                    injection = hits[0] if hits else None
                    failures.append(self._failure_record(
                        name, round_index, result.stage, result.code,
                        str(result), injection))
                    for hit in hits:
                        injection_hits.append({
                            "injection_id": hit.injection_id,
                            "fault": hit.fault,
                            "sample": name,
                            "round": round_index,
                            "code": result.code,
                            "stage": result.stage,
                            "hit": True,
                        })
                    continue

                rounds[name].append(result)
                score_summary[name] = {
                    "round": round_index,
                    "score": result["score"],
                    "confidence": result["confidence"],
                    "decision": result["decision"],
                }
                for hit in hits:
                    injection_hits.append({
                        "injection_id": hit.injection_id,
                        "fault": hit.fault,
                        "sample": name,
                        "round": round_index,
                        "code": None,
                        "stage": None,
                        "hit": True,
                    })

            version += 1
            snapshot = Snapshot(
                version=version,
                round=round_index,
                config_hash=self.config_hash,
                relation_hash=self.relation_hash,
                injection_hash=self.injection_hash,
                sample_cursor=ordered,
                score_summary=score_summary,
                rounds=rounds,
                failed_samples=sorted(failed_samples),
                injection_hits=sorted(injection_hits, key=hit_sort_key),
                failures=sorted(failures, key=failure_sort_key),
                static_dangling=static_dangling,
            )
            write_snapshot(self.work_dir, snapshot)
            write_failures(
                self.work_dir,
                sorted(failures, key=failure_sort_key),
                sorted(injection_hits, key=hit_sort_key))

            if self._graceful.is_set() or (
                    self._interrupt_after_round is not None
                    and round_index >= self._interrupt_after_round):
                graceful_exit = True
                break

        if graceful_exit:
            raise GracefulExit(snapshot_path(self.work_dir))

        sample_results = []
        for name in active_samples:
            trajectory = rounds.get(name, [])
            if name in failed_samples or not trajectory:
                continue
            last = trajectory[-1]
            final_decision = ACCEPTED if last["decision"] == ACCEPTED \
                else REJECTED
            if final_decision == REJECTED and last["decision"] != REJECTED:
                final_decision = REJECTED
            if last["decision"] == "RUNNING":
                final_decision = ACCEPTED
                final_reason = "rounds_exhausted"
            else:
                final_reason = last["reason"]
            sample_results.append({
                "sample": name,
                "final_decision": final_decision,
                "final_reason": final_reason,
                "final_confidence": last["confidence"],
                "final_score": last["score"],
                "rounds_completed": len(trajectory),
                "score_trajectory": [
                    {"round": item["round"], "score": item["score"],
                     "confidence": item["confidence"],
                     "temperature": item["temperature"],
                     "decision": item["decision"]}
                    for item in trajectory
                ],
                "evidence_decomposition": [
                    {"round": item["round"],
                     "contributions": item["contributions"]}
                    for item in trajectory
                ],
                "score_summary": score_summary.get(name, {}),
            })

        diagnostics = {
            "quantization": quantization_diagnostics(config),
            "temperature_schedule": [
                {"round": index,
                 "temperature": config.fusion_temperature
                 * (1.0 + 0.1 * index)}
                for index in range(config.max_rounds)
            ],
            "text_weight": config.text_weight,
            "conflict_discount": config.conflict_discount,
            "supplement_boost": config.supplement_boost,
        }
        report = build_report(
            config_dict=config.hashable_dict(),
            config_hash=self.config_hash,
            relation_hash=self.relation_hash,
            injection_hash=self.injection_hash,
            sample_results=sample_results,
            failures=failures,
            injection_hits=injection_hits,
            snapshot_version=version,
            static_dangling=static_dangling,
            manifest=manifest,
            diagnostics=diagnostics,
        )
        return report, version


def run_pipeline(input_dir: str, config_path: str, relation_path: str,
                 injection_path: str | None, work_dir: str,
                 report_path: str) -> tuple[str, int, bool]:
    """Convenience entry point used by the CLI. Returns path, version, signal."""
    config = load_fusion_config(config_path)
    relations = load_relation_table(relation_path)
    injections = load_injection_config(injection_path)
    relation_hash = content_hash([item.to_dict() for item in relations])
    injection_hash = content_hash(
        [item.to_dict() for item in injections]) if injections \
        else content_hash([])

    engine = PipelineEngine(
        config=config, relations=relations, injections=injections,
        work_dir=work_dir, relation_hash=relation_hash,
        injection_hash=injection_hash)
    engine.install_signal_handlers()
    try:
        report, version = engine.run(input_dir)
    except GracefulExit:
        return snapshot_path(work_dir), 0, True
    atomic_write_json(report_path, report)
    # Runtime metadata (work dir, concurrency) is intentionally kept out of the
    # main report so different concurrency settings stay byte-identical.
    atomic_write_json(report_path + ".run-metadata.json", {
        "report_path": os.path.abspath(report_path),
        "work_dir": os.path.abspath(work_dir),
        "snapshot_path": os.path.abspath(snapshot_path(work_dir)),
        "failure_path": os.path.abspath(failure_path(work_dir)),
        "configured_concurrency": config.concurrency,
        "snapshot_version": version,
    })
    return report_path, version, False
