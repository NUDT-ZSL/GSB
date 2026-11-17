"""Threaded pipeline orchestration, incremental caching and recovery."""

from __future__ import annotations

import threading
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .cache import PipelineCache
from .constants import STAGES, STAGE_ORDER
from .errors import BusinessError
from .incremental import plan_invalidations
from .injection import InjectionConfig, load_injections
from .intent import IntentScorer
from .loader import LoadResult, load_corpus
from .merge import merge_results
from .models import Sample
from .normalization import Normalizer
from .report import build_report
from .resources import ResourceBundle, load_resources
from .sequence import BIESOTagger
from .slots import EntityExtractor
from .snapshot import SnapshotStore
from .utils import quantize, stable_hash


@dataclass
class PipelineResult:
    report: dict[str, Any]
    results: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    snapshot_version: int
    complete: bool
    interrupted: bool


def _stage_injection_payload(injections: Iterable[Any]) -> list[dict[str, Any]]:
    return [item.as_dict() for item in injections]


def _relevant_intent_parts(resources: ResourceBundle, text: str) -> dict[str, Any]:
    features = []
    for name, definition in sorted(resources.intents.items()):
        for feature in definition.features:
            if feature.term in text:
                key = f"intent_feature:{name}:{feature.term}"
                features.append([key, resources.entry_manifest.get(key)])
    conflicts = []
    for rule in resources.conflicts:
        if rule.trigger in text:
            key = f"rule_conflict:{rule.trigger}:{rule.winner}:{rule.loser}"
            conflicts.append([key, resources.entry_manifest.get(key)])
    return {"features": features, "conflicts": conflicts}


def _relevant_entity_parts(resources: ResourceBundle, text: str) -> list[list[str | None]]:
    result: list[list[str | None]] = []
    for entity_type in sorted(resources.entities):
        for entry in resources.entities[entity_type]:
            if entry.term in text:
                key = f"entity:{entity_type}:{entry.term}"
                result.append([key, resources.entry_manifest.get(key)])
    return result


def _relevant_synonym_parts(resources: ResourceBundle, text: str) -> list[list[str | None]]:
    result: list[list[str | None]] = []
    nfkc = unicodedata.normalize(str(resources.config.normalize.get("unicode_form", "NFKC")), text)
    for rule in resources.synonyms:
        if rule.source in text or rule.source in nfkc:
            key = f"rule_synonym:{rule.source}"
            result.append([key, resources.entry_manifest.get(key)])
    return result


class PipelineRunner:
    def __init__(
        self,
        resources: ResourceBundle,
        *,
        injections: InjectionConfig | None = None,
        state_dir: str | Path = "state",
        workers: int | None = None,
        snapshot_enabled: bool = True,
    ) -> None:
        self.resources = resources
        self.injections = injections or InjectionConfig()
        self.state_dir = Path(state_dir)
        self.workers = workers or resources.config.concurrency
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        self.cache = PipelineCache(self.state_dir / "cache.json").load()
        self.snapshots = SnapshotStore(self.state_dir) if snapshot_enabled else None
        self.normalizer = Normalizer(resources.config, resources.synonyms)
        self.scorer = IntentScorer(resources)
        self.extractor = EntityExtractor(resources)
        self.tagger = BIESOTagger(resources)
        self.cache_lock = threading.Lock()

    def run(
        self,
        load_result: LoadResult,
        *,
        corpus_path: str | Path,
        config_path: str | Path,
        injection_path: str | Path | None = None,
        fresh: bool = False,
        interrupt_after: int | None = None,
        stop_event: threading.Event | None = None,
        snapshot_enabled: bool = True,
    ) -> PipelineResult:
        samples = list(load_result.samples)
        sample_ids = [sample.sample_id for sample in samples]
        text_hashes = {
            sample.sample_id: stable_hash({"text": sample.text, "chars": list(sample.chars)})
            for sample in samples
        }
        corpus_hash = stable_hash([sample.as_dict() for sample in samples])
        config_hash = self.resources.config.config_hash
        resource_hash = self.resources.resource_hash
        injection_hash = self.injections.config_hash
        latest = None
        if self.snapshots is not None and snapshot_enabled:
            if not fresh:
                latest = self.snapshots.load_latest()
                if latest is not None:
                    self.snapshots.validate(
                        latest,
                        config_hash=config_hash,
                        resource_hash=resource_hash,
                        corpus_hash=corpus_hash,
                        injection_hash=injection_hash,
                    )
        loaded_results: dict[str, dict[str, Any]] = {}
        base_version = 0
        if latest is not None:
            base_version = latest.version
            for result in latest.results:
                sid = result.get("sample_id")
                if isinstance(sid, str) and sid in set(sample_ids):
                    loaded_results[sid] = result
        elif self.snapshots is not None:
            base_version = self.snapshots.latest_version()

        plan = plan_invalidations(
            samples=samples,
            resources=self.resources,
            cache=self.cache,
            injections=self.injections,
            text_hashes=text_hashes,
        )
        stage_metrics = {
            stage: {"computed": 0, "cache_hits": 0, "failures": 0, "duration_ms": 0.0}
            for stage in STAGES
        }
        result_map: dict[str, dict[str, Any]] = dict(loaded_results)
        pending = [sample for sample in samples if sample.sample_id not in loaded_results]
        external_stop = stop_event or threading.Event()
        triggered_stop = False
        newly_completed = 0
        last_snapshot_count = len(result_map)
        snapshot_version = base_version

        def commit_snapshot() -> None:
            nonlocal snapshot_version
            if self.snapshots is None or not snapshot_enabled:
                return
            manifest = self.snapshots.commit(
                results=result_map.values(),
                config_hash=config_hash,
                resource_hash=resource_hash,
                corpus_hash=corpus_hash,
                injection_hash=injection_hash,
                base_version=self.snapshots.latest_version(),
            )
            snapshot_version = int(manifest["version"])
            self._save_cache(plan)

        def enqueue_marginal(future: Future[tuple[dict[str, Any], dict[str, Any]]]) -> None:
            nonlocal newly_completed
            result, metrics = future.result()
            sid = str(result["sample_id"])
            result_map[sid] = result
            newly_completed += 1
            for stage in STAGES:
                current = stage_metrics[stage]
                update = metrics.get(stage, {})
                current["computed"] += int(update.get("computed", 0))
                current["cache_hits"] += int(update.get("cache_hits", 0))
                current["failures"] += int(update.get("failures", 0))
                current["duration_ms"] += float(update.get("duration_ms", 0.0))

        if pending:
            pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="intent-stage")
            running: dict[Future[tuple[dict[str, Any], dict[str, Any]]], Sample] = {}
            pending_index = 0
            try:
                while pending_index < len(pending) or running:
                    while (
                        pending_index < len(pending)
                        and len(running) < max(1, self.workers * 2)
                        and not external_stop.is_set()
                        and not (interrupt_after is not None and newly_completed >= interrupt_after)
                    ):
                        sample = pending[pending_index]
                        pending_index += 1
                        future = pool.submit(self._process_sample, sample, text_hashes[sample.sample_id])
                        running[future] = sample
                    if not running:
                        break
                    done, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
                    for future in sorted(done, key=lambda item: running[item].sample_id):
                        running.pop(future, None)
                        enqueue_marginal(future)
                    if interrupt_after is not None and newly_completed >= interrupt_after:
                        triggered_stop = True
                    if external_stop.is_set():
                        triggered_stop = True
                    if triggered_stop:
                        # Drop queued work; submit loop will not add more.
                        pass
                    if len(result_map) - last_snapshot_count >= self.resources.config.snapshot_commit_interval:
                        commit_snapshot()
                        last_snapshot_count = len(result_map)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        if external_stop.is_set() or (interrupt_after is not None and newly_completed >= interrupt_after):
            triggered_stop = True
        all_results = sorted(result_map.values(), key=lambda item: str(item["sample_id"]))
        complete = len(all_results) == len(samples)
        if self.snapshots is not None and snapshot_enabled and (newly_completed or snapshot_version == 0):
            commit_snapshot()
        self._save_cache(plan)
        final_version = snapshot_version
        report = build_report(
            results=all_results,
            loader_failures=load_result.failures,
            stage_metrics=stage_metrics,
            incremental=plan,
            resources=self.resources,
            injections=self.injections,
            snapshot_version=final_version,
            interrupted=bool(triggered_stop and not complete),
            complete=complete,
            corpus_path=str(corpus_path),
            config_path=str(config_path),
            injection_path=str(injection_path) if injection_path is not None else None,
        )
        failures = [dict(item) for item in report["failures"]]
        return PipelineResult(
            report=report,
            results=all_results,
            failures=failures,
            snapshot_version=final_version,
            complete=complete,
            interrupted=bool(triggered_stop and not complete),
        )

    def _save_cache(self, plan: dict[str, Any]) -> None:
        self.cache.update_headers(
            resource_manifest=self.resources.entry_manifest,
            config_hash=self.resources.config.config_hash,
            config_product_manifest=self.resources.config.product_manifest,
            injection_hash=self.injections.config_hash,
            injection_stage_hashes=plan.get("injection_stage_hashes", {}),
            references=plan.get("references", {}),
        )
        self.cache.save()

    def _process_sample(self, sample: Sample, text_hash: str) -> tuple[dict[str, Any], dict[str, Any]]:
        metrics = {
            stage: {"computed": 0, "cache_hits": 0, "failures": 0, "duration_ms": 0.0}
            for stage in STAGES
        }
        products: dict[str, Any] = {}
        injection_ids: set[str] = set()
        task_sort_keys = [f"{sample.sample_id}:{stage}" for stage in STAGES]
        try:
            normalize_injections = self.injections.for_sample_stage(sample.sample_id, "normalize")
            normalize_key = stable_hash(
                {
                    "stage": "normalize",
                    "text_hash": text_hash,
                    "synonyms": _relevant_synonym_parts(self.resources, sample.text),
                    "params": self.resources.config.product_manifest["normalize"],
                    "injections": _stage_injection_payload(normalize_injections),
                }
            )
            normalized = self._stage(
                sample.sample_id,
                "normalize",
                normalize_key,
                lambda: self.normalizer.normalize(sample, normalize_injections),
                metrics,
            )
            products["normalize"] = normalized
            injection_ids.update(normalized.get("injection_ids", []))

            intent_injections = self.injections.for_sample_stage(sample.sample_id, "intent")
            intent_key = stable_hash(
                {
                    "stage": "intent",
                    "upstream": stable_hash(normalized),
                    "relevant": _relevant_intent_parts(self.resources, normalized["text"]),
                    "params": self.resources.config.product_manifest["intent"],
                    "injections": _stage_injection_payload(intent_injections),
                }
            )
            score = self._stage(
                sample.sample_id,
                "intent",
                intent_key,
                lambda: self.scorer.score(normalized, intent_injections),
                metrics,
            )
            products["intent"] = score
            injection_ids.update(score.get("injection_ids", []))

            slot_injections = self.injections.for_sample_stage(sample.sample_id, "slots")
            intent_name = score["intent"]
            intent_def_key = f"intent_def:{intent_name}"
            slots_key = stable_hash(
                {
                    "stage": "slots",
                    "upstream": stable_hash(normalized),
                    "intent": intent_name,
                    "intent_definition": [intent_def_key, self.resources.entry_manifest.get(intent_def_key)],
                    "entities": _relevant_entity_parts(self.resources, normalized["text"]),
                    "params": self.resources.config.product_manifest["slots"],
                    "injections": _stage_injection_payload(slot_injections),
                }
            )
            slot_product = self._stage(
                sample.sample_id,
                "slots",
                slots_key,
                lambda: self.extractor.extract(normalized, score, slot_injections),
                metrics,
            )
            products["slots"] = slot_product
            injection_ids.update(slot_product.get("injection_ids", []))

            sequence_injections = self.injections.for_sample_stage(sample.sample_id, "sequence")
            sequence_key = stable_hash(
                {
                    "stage": "sequence",
                    "upstream": stable_hash(normalized),
                    "model": self.resources.entry_manifest.get("sequence_model"),
                    "entities": _relevant_entity_parts(self.resources, normalized["text"]),
                    "params": self.resources.config.product_manifest["sequence"],
                    "injections": _stage_injection_payload(sequence_injections),
                }
            )
            sequence_product = self._stage(
                sample.sample_id,
                "sequence",
                sequence_key,
                lambda: self.tagger.tag(normalized, sequence_injections),
                metrics,
            )
            products["sequence"] = sequence_product
            injection_ids.update(sequence_product.get("injection_ids", []))

            merge_injections = self.injections.for_sample_stage(sample.sample_id, "merge")
            merge_key = stable_hash(
                {
                    "stage": "merge",
                    "intent": score.get("fingerprint"),
                    "slots": slot_product.get("fingerprint"),
                    "sequence": sequence_product.get("fingerprint"),
                    "params": self.resources.config.product_manifest["merge"],
                    "source_priority": self.resources.config.merge.get("source_priority", ["dictionary", "sequence"]),
                    "injections": _stage_injection_payload(merge_injections),
                }
            )
            merged = self._stage(
                sample.sample_id,
                "merge",
                merge_key,
                lambda: merge_results(
                    slot_product,
                    sequence_product,
                    source_priority=list(self.resources.config.merge.get("source_priority", ["dictionary", "sequence"])),
                    digits=self.resources.config.digits,
                ),
                metrics,
            )
            products["merge"] = merged
            # Merge injections have no built-in fault type; if a future fault is
            # declared here it is still counted as a hit.
            injection_ids.update(item.injection_id for item in merge_injections)
            with self.cache_lock:
                self.cache.set_sample_metadata(sample.sample_id, {"text_hash": text_hash})
            result = {
                "sample_id": sample.sample_id,
                "status": "ok",
                "text": sample.text,
                "normalized_text": normalized["text"],
                "intent": score["intent"],
                "confidence": score["confidence"],
                "threshold_met": score["threshold_met"],
                "scores": score["scores"],
                "normalized_scores": score["normalized_scores"],
                "intent_scores": score["scores"],
                "normalized_intent_scores": score["normalized_scores"],
                "score_details": score["details"],
                "intermediate_scores": {
                    "raw": score["scores"],
                    "normalized": score["normalized_scores"],
                    "feature_hits": score["details"]["feature_hits"],
                    "conflict_penalties": score["details"]["conflict_penalties"],
                },
                "slots": merged["slots"],
                "dictionary_slots": slot_product["slots"],
                "suppressed_slots": slot_product["suppressed"],
                "suppressed": slot_product["suppressed"],
                "downgraded_slots": slot_product["downgraded"],
                "downgraded": slot_product["downgraded"],
                "downgraded_items": slot_product["downgraded"],
                "sequence": {
                    "labels": sequence_product["labels"],
                    "path_score": sequence_product["path_score"],
                    "position_scores": sequence_product["position_scores"],
                    "fragments": sequence_product["fragments"],
                },
                "sequence_labels": sequence_product["labels"],
                "sequence_path_score": sequence_product["path_score"],
                "position_scores": sequence_product["position_scores"],
                "merge": {
                    "covered_fragments": merged["covered_fragments"],
                    "source_priority": merged["source_priority"],
                },
                "covered_fragments": merged["covered_fragments"],
                "injection_ids": sorted(injection_ids),
                "stage_fingerprints": {
                    "normalize": normalize_key,
                    "intent": intent_key,
                    "slots": slots_key,
                    "sequence": sequence_key,
                    "merge": merge_key,
                },
                "task_sort_keys": task_sort_keys,
                "stage_products": products,
            }
            return result, metrics
        except BusinessError as exc:
            stage_name = exc.stage or self._last_attempted_stage(metrics)
            error_injection_ids = exc.details.get("injection_ids", [])
            injection_ids.update(error_injection_ids)
            failure = {
                "sample_id": sample.sample_id,
                "line_number": sample.line_number,
                "stage": stage_name,
                "business_code": exc.code,
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "details": exc.details,
                "injection_ids": sorted(injection_ids),
            }
            result = {
                "sample_id": sample.sample_id,
                "status": "failed",
                "line_number": sample.line_number,
                "error": failure,
                "injection_ids": sorted(injection_ids),
                "task_sort_keys": task_sort_keys,
                "stage_products": products,
            }
            return result, metrics
        except Exception as exc:  # pragma: no cover - defensive isolation
            stage_name = self._last_attempted_stage(metrics)
            failure = {
                "sample_id": sample.sample_id,
                "line_number": sample.line_number,
                "stage": stage_name,
                "business_code": "E_INTERNAL",
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "details": {},
                "injection_ids": sorted(injection_ids),
            }
            result = {
                "sample_id": sample.sample_id,
                "status": "failed",
                "line_number": sample.line_number,
                "error": failure,
                "injection_ids": sorted(injection_ids),
                "task_sort_keys": task_sort_keys,
                "stage_products": products,
            }
            return result, metrics

    def _stage(
        self,
        sample_id: str,
        stage: str,
        key: str,
        compute: Any,
        metrics: dict[str, dict[str, Any]],
    ) -> Any:
        with self.cache_lock:
            hit, value = self.cache.get(sample_id, stage, key)
        if hit:
            metrics[stage]["cache_hits"] += 1
            return value
        started = time.perf_counter()
        metrics[stage]["computed"] += 1
        try:
            value = compute()
        except BaseException:
            metrics[stage]["failures"] += 1
            metrics[stage]["duration_ms"] += (time.perf_counter() - started) * 1000.0
            raise
        metrics[stage]["duration_ms"] += (time.perf_counter() - started) * 1000.0
        with self.cache_lock:
            self.cache.put(sample_id, stage, key, value)
        return value

    @staticmethod
    def _last_attempted_stage(metrics: dict[str, dict[str, Any]]) -> str:
        for stage in reversed(STAGES):
            if metrics[stage]["computed"] or metrics[stage]["cache_hits"] or metrics[stage]["failures"]:
                return stage
        return STAGES[0]


def run_pipeline(
    corpus_path: str | Path,
    resources_dir: str | Path,
    *,
    config_path: str | Path | None = None,
    injection_path: str | Path | None = None,
    state_dir: str | Path = "state",
    workers: int | None = None,
    fresh: bool = False,
    interrupt_after: int | None = None,
    stop_event: threading.Event | None = None,
) -> PipelineResult:
    resources = load_resources(resources_dir, config_path)
    injections = load_injections(injection_path)
    load_result = load_corpus(corpus_path, intent_names=resources.intent_names, allowed_types=resources.entity_types)
    runner = PipelineRunner(
        resources,
        injections=injections,
        state_dir=state_dir,
        workers=workers,
    )
    return runner.run(
        load_result,
        corpus_path=corpus_path,
        config_path=config_path or resources.base_dir / "config.json",
        injection_path=injection_path,
        fresh=fresh,
        interrupt_after=interrupt_after,
        stop_event=stop_event,
    )
