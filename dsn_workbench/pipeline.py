from __future__ import annotations

import copy
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import (
    STAGES,
    ContentAddressedCache,
    make_stage_key,
)
from .config import ConfigBundle, read_config
from .errors import (
    E_CACHE_CORRUPT,
    E_CHECKPOINT_CORRUPT,
    E_FORMAT_CORRUPT,
    E_PARALLEL_MISMATCH,
    WorkbenchError,
)
from .invalidation import InvalidationPlan, plan_and_alias
from .normalization import normalize_sentence
from .resources import ResourceDecision, resolve_resources
from .segmentation import disambiguate, generate_candidates
from .state import StageMachine, StateStore
from .tagging import tag_and_validate
from .tasks import load_tasks, task_digest
from .util import (
    canonical_json_bytes,
    content_hash,
    sentence_sort_key,
    sha256_bytes,
)


LOGGER = logging.getLogger("dsn_workbench.pipeline")


class SimulatedCrash(RuntimeError):
    def __init__(self, stage: str, batches_completed: int):
        super().__init__(
            f"simulated crash after {batches_completed} batch(es) of {stage}"
        )
        self.stage = stage
        self.batches_completed = batches_completed


@dataclass
class PipelineOptions:
    verify_parallel: bool = True
    crash_stage: str | None = None
    crash_after_batches: int | None = None
    use_checkpoints: bool = True
    workers_override: int | None = None
    batch_size_override: int | None = None


@dataclass
class PipelineRun:
    outputs: list[dict[str, Any]]
    report: dict[str, Any]


def _run_stage_item(
    stage: str,
    item: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    task = item["task"]
    if stage == "normalize":
        return normalize_sentence(task["sentence_id"], task["raw_text"], config)
    if stage == "candidates":
        return generate_candidates(item["normalized"], config)
    if stage == "disambiguation":
        return disambiguate(
            item["normalized"], item["candidate_artifact"], config
        )
    if stage == "tagging":
        return tag_and_validate(
            item["normalized"], item["token_artifact"], config
        )
    raise ValueError(f"unknown stage: {stage}")


def _task_config(
    config: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    if (
        task["domain"] == config["domain"]
        and task["lang"] == config["locale"]
    ):
        return config
    scoped = dict(config)
    scoped["domain"] = task["domain"]
    scoped["locale"] = task["lang"]
    return scoped


def _worker_stage_batch(payload: dict[str, Any]) -> dict[str, Any]:
    stage = payload["stage"]
    config = payload["config"]
    artifacts: list[dict[str, Any]] = []
    for item in payload["items"]:
        try:
            artifacts.append(
                _run_stage_item(
                    stage, item, _task_config(config, item["task"])
                )
            )
        except WorkbenchError as exc:
            return {
                "ok": False,
                "sentence_id": item["task"]["sentence_id"],
                "error": exc.to_dict()["error"],
            }
    return {"ok": True, "artifacts": artifacts}


def _validate_artifact(
    stage: str, sentence_id: str, artifact: Any
) -> dict[str, Any]:
    if not isinstance(artifact, dict) or artifact.get("sentence_id") != sentence_id:
        raise WorkbenchError(
            E_CACHE_CORRUPT,
            "stage artifact has the wrong sentence identity",
            {"stage": stage, "sentence_id": sentence_id},
        )
    return artifact


def _stage_key(
    stage: str,
    task: dict[str, Any],
    bundle: ConfigBundle,
    upstream_hash: str,
) -> dict[str, Any]:
    return make_stage_key(
        stage=stage,
        sentence_id=task["sentence_id"],
        raw_text=task["raw_text"],
        config=bundle,
        upstream_hash=upstream_hash,
        domain=task["domain"],
        locale=task["lang"],
    )


def _raise_worker_error(error: dict[str, Any]) -> None:
    raise WorkbenchError(
        str(error.get("code", E_FORMAT_CORRUPT)),
        str(error.get("message", "worker failed")),
        dict(error.get("details") or {}),
    )


def _compute_stage(
    *,
    stage: str,
    tasks: list[dict[str, Any]],
    bundle: ConfigBundle,
    cache: ContentAddressedCache,
    state: StateStore,
    previous_artifacts: dict[str, dict[str, dict[str, Any]]],
    previous_hashes: dict[str, str],
    decision: ResourceDecision,
    options: PipelineOptions,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, str],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    artifacts: dict[str, dict[str, Any]] = {}
    object_hashes: dict[str, str] = {}
    entries: dict[str, dict[str, Any]] = {}
    compute_items: list[dict[str, Any]] = []
    compute_keys: dict[str, dict[str, Any]] = {}
    cache_hits = 0

    for task in tasks:
        sentence_id = task["sentence_id"]
        upstream_hash = (
            "root" if stage == "normalize" else previous_hashes[sentence_id]
        )
        key = _stage_key(stage, task, bundle, upstream_hash)
        cached = cache.get(stage, key)
        if cached is not None:
            artifact, object_hash = cached
            artifact = _validate_artifact(stage, sentence_id, artifact)
            artifacts[sentence_id] = artifact
            object_hashes[sentence_id] = object_hash
            entries[sentence_id] = {
                "stage": stage,
                "key": key,
                "key_hash": content_hash(key),
                "object_hash": object_hash,
            }
            cache_hits += 1
            continue

        item: dict[str, Any] = {"task": task}
        if stage in ("candidates", "disambiguation", "tagging"):
            item["normalized"] = previous_artifacts["normalize"][sentence_id]
        if stage == "disambiguation":
            item["candidate_artifact"] = previous_artifacts["candidates"][
                sentence_id
            ]
        if stage == "tagging":
            item["token_artifact"] = previous_artifacts["disambiguation"][
                sentence_id
            ]
        compute_items.append(item)
        compute_keys[sentence_id] = key

    computed_count = 0
    batch_count = 0
    if compute_items:
        batches = [
            compute_items[index : index + decision.batch_size]
            for index in range(0, len(compute_items), decision.batch_size)
        ]
        if decision.workers > 1 and len(compute_items) > 1:
            context = multiprocessing.get_context("fork")
            with ProcessPoolExecutor(
                max_workers=min(decision.workers, len(compute_items)),
                mp_context=context,
            ) as executor:
                futures = [
                    executor.submit(
                        _worker_stage_batch,
                        {
                            "stage": stage,
                            "config": bundle.data,
                            "items": batch,
                        },
                    )
                    for batch in batches
                ]
                for batch_index, (batch, future) in enumerate(
                    zip(batches, futures), 1
                ):
                    result = future.result()
                    if not result.get("ok"):
                        _raise_worker_error(result["error"])
                    batch_artifacts = result["artifacts"]
                    if len(batch_artifacts) != len(batch):
                        raise WorkbenchError(
                            E_CACHE_CORRUPT,
                            "worker returned the wrong artifact count",
                            {
                                "stage": stage,
                                "expected": len(batch),
                                "actual": len(batch_artifacts),
                            },
                        )
                    for item, artifact in zip(batch, batch_artifacts):
                        sentence_id = item["task"]["sentence_id"]
                        artifact = _validate_artifact(
                            stage, sentence_id, artifact
                        )
                        artifacts[sentence_id] = artifact
                        computed_count += 1
                    batch_count = batch_index
                    computed_ids = sorted(artifacts, key=sentence_sort_key)
                    all_keys = [
                        content_hash(compute_keys[sentence_id])
                        for sentence_id in computed_ids
                        if sentence_id in compute_keys
                    ]
                    state.write_pending_batch(
                        stage,
                        batch_index=batch_index,
                        sentence_ids=computed_ids,
                        payload_hash=content_hash(
                            {
                                "stage": stage,
                                "sentence_ids": computed_ids,
                            }
                        ),
                        key_hashes=all_keys,
                        logical_clock_ms=bundle.data["logical_clock_ms"],
                    )
                    if (
                        options.crash_stage == stage
                        and options.crash_after_batches is not None
                        and batch_index >= options.crash_after_batches
                    ):
                        raise SimulatedCrash(stage, batch_index)
        else:
            for batch_index, batch in enumerate(batches, 1):
                for item in batch:
                    artifact = _run_stage_item(
                        stage,
                        item,
                        _task_config(bundle.data, item["task"]),
                    )
                    sentence_id = item["task"]["sentence_id"]
                    artifact = _validate_artifact(stage, sentence_id, artifact)
                    artifacts[sentence_id] = artifact
                    computed_count += 1
                batch_count = batch_index
                computed_ids = sorted(artifacts, key=sentence_sort_key)
                all_keys = [
                    content_hash(compute_keys[sentence_id])
                    for sentence_id in computed_ids
                    if sentence_id in compute_keys
                ]
                state.write_pending_batch(
                    stage,
                    batch_index=batch_index,
                    sentence_ids=computed_ids,
                    payload_hash=content_hash(
                        {
                            "stage": stage,
                            "sentence_ids": computed_ids,
                        }
                    ),
                    key_hashes=all_keys,
                    logical_clock_ms=bundle.data["logical_clock_ms"],
                )
                if (
                    options.crash_stage == stage
                    and options.crash_after_batches is not None
                    and batch_index >= options.crash_after_batches
                ):
                    raise SimulatedCrash(stage, batch_index)

    for sentence_id in sorted(compute_keys, key=sentence_sort_key):
        key = compute_keys[sentence_id]
        object_hash = cache.put(stage, key, artifacts[sentence_id])
        object_hashes[sentence_id] = object_hash
        entries[sentence_id] = {
            "stage": stage,
            "key": key,
            "key_hash": content_hash(key),
            "object_hash": object_hash,
        }

    if set(artifacts) != {task["sentence_id"] for task in tasks}:
        raise WorkbenchError(
            E_CACHE_CORRUPT,
            "stage did not produce exactly one artifact per sentence",
            {"stage": stage},
        )
    return artifacts, object_hashes, entries, {
        "stage": stage,
        "cache_hits": cache_hits,
        "computed": computed_count,
        "batches": batch_count,
    }


def _checkpoint_artifacts(
    *,
    checkpoint: dict[str, Any],
    stage: str,
    tasks: list[dict[str, Any]],
    cache: ContentAddressedCache,
    bundle: ConfigBundle,
    previous_hashes: dict[str, str],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, str],
    dict[str, dict[str, Any]],
]:
    payload = cache.get_object(checkpoint["payload_hash"])
    if (
        not isinstance(payload, dict)
        or payload.get("stage") != stage
        or not isinstance(payload.get("artifacts"), list)
    ):
        raise WorkbenchError(
            E_CHECKPOINT_CORRUPT,
            "checkpoint payload shape is invalid",
            {"stage": stage},
        )
    artifacts: dict[str, dict[str, Any]] = {}
    expected = {task["sentence_id"] for task in tasks}
    for artifact in payload["artifacts"]:
        if not isinstance(artifact, dict):
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint payload contains a non-object artifact",
                {"stage": stage},
            )
        sentence_id = str(artifact.get("sentence_id"))
        if sentence_id in artifacts:
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint payload contains duplicate sentence artifacts",
                {"stage": stage, "sentence_id": sentence_id},
            )
        artifacts[sentence_id] = artifact
    if set(artifacts) != expected:
        raise WorkbenchError(
            E_CHECKPOINT_CORRUPT,
            "checkpoint payload sentence set does not match tasks",
            {
                "stage": stage,
                "missing": sorted(expected - set(artifacts)),
                "extra": sorted(set(artifacts) - expected),
            },
        )
    completed = checkpoint.get("completed_sentence_ids")
    if not isinstance(completed, list) or set(completed) != expected:
        raise WorkbenchError(
            E_CHECKPOINT_CORRUPT,
            "checkpoint completed sentence set is invalid",
            {
                "stage": stage,
                "completed": completed,
                "expected": sorted(expected, key=sentence_sort_key),
            },
        )
    object_hashes: dict[str, str] = {}
    entries: dict[str, dict[str, Any]] = {}
    for task in tasks:
        sentence_id = task["sentence_id"]
        upstream_hash = (
            "root" if stage == "normalize" else previous_hashes[sentence_id]
        )
        key = _stage_key(stage, task, bundle, upstream_hash)
        object_hash = cache.put_object(artifacts[sentence_id])
        object_hashes[sentence_id] = object_hash
        entries[sentence_id] = {
            "stage": stage,
            "key": key,
            "key_hash": content_hash(key),
            "object_hash": object_hash,
        }
    return artifacts, object_hashes, entries


def _comparison_payload(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "sentence_id": artifact["sentence_id"],
        "tokens": artifact["tokens"],
        "pos_sequence": artifact["pos_sequence"],
    }


def _serial_reference(
    tasks: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for task in tasks:
        scoped = _task_config(config, task)
        normalized = normalize_sentence(
            task["sentence_id"], task["raw_text"], scoped
        )
        candidates = generate_candidates(normalized, scoped)
        tokens = disambiguate(normalized, candidates, scoped)
        tagged = tag_and_validate(normalized, tokens, scoped)
        results.append(_comparison_payload(tagged))
    return results


def _verify_serial_parallel(
    tasks: list[dict[str, Any]],
    config: dict[str, Any],
    tagged_by_sentence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    serial = _serial_reference(tasks, config)
    serial_by_id = {
        result["sentence_id"]: result for result in serial
    }
    serial_bytes = canonical_json_bytes(serial)
    parallel_sequence = [
        _comparison_payload(tagged_by_sentence[task["sentence_id"]])
        for task in tasks
    ]
    parallel_bytes = canonical_json_bytes(parallel_sequence)
    first_difference = None
    for task in tasks:
        sentence_id = task["sentence_id"]
        left = canonical_json_bytes(serial_by_id[sentence_id])
        right = canonical_json_bytes(
            _comparison_payload(tagged_by_sentence[sentence_id])
        )
        if left != right:
            first_difference = {
                "sentence_id": sentence_id,
                "serial_sha256": sha256_bytes(left),
                "parallel_sha256": sha256_bytes(right),
            }
            break
    if first_difference is not None or serial_bytes != parallel_bytes:
        raise WorkbenchError(
            E_PARALLEL_MISMATCH,
            "parallel output differs from independent serial execution",
            {
                "first_difference": first_difference,
                "serial_sha256": sha256_bytes(serial_bytes),
                "parallel_sha256": sha256_bytes(parallel_bytes),
            },
        )
    return {
        "status": "verified",
        "sentence_count": len(tasks),
        "serial_sha256": sha256_bytes(serial_bytes),
        "parallel_sha256": sha256_bytes(parallel_bytes),
        "first_difference": None,
    }


def _failure_report(
    *,
    stage: str,
    reason: str,
    simulated_crash: bool,
    checkpoints: dict[str, dict[str, Any]],
    cache_plan: InvalidationPlan,
    decision: ResourceDecision,
    cache: ContentAddressedCache,
) -> dict[str, Any]:
    return {
        "ok": False,
        "stage": stage,
        "reason": reason,
        "simulated_crash": simulated_crash,
        "checkpoints": checkpoints,
        "cache_plan": cache_plan.to_dict(),
        "resources": decision.to_dict(),
        "cache_stats": cache.stats(),
    }


def _apply_overrides(
    bundle: ConfigBundle, options: PipelineOptions
) -> tuple[ConfigBundle, dict[str, Any]]:
    data = copy.deepcopy(bundle.data)
    if options.workers_override is not None:
        data["workers"] = options.workers_override
    if options.batch_size_override is not None:
        data["batch_size"] = options.batch_size_override
    if data["workers"] < 1 or data["batch_size"] < 1:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "worker and batch overrides must be positive",
            {
                "workers": data["workers"],
                "batch_size": data["batch_size"],
            },
        )
    return (
        ConfigBundle(
            data=data,
            format_version=bundle.format_version,
            source=bundle.source,
            provenance=dict(bundle.provenance),
        ),
        {"workers": data["workers"], "batch_size": data["batch_size"]},
    )


def run_pipeline(
    config_source: str | Path,
    tasks_source: str | Path,
    *,
    cache_dir: str | Path,
    state_dir: str | Path,
    options: PipelineOptions | None = None,
) -> PipelineRun:
    options = options or PipelineOptions()
    if options.crash_stage is not None and options.crash_stage not in STAGES:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "crash_stage must name a pipeline stage",
            {"crash_stage": options.crash_stage, "stages": list(STAGES)},
        )
    if (
        options.crash_after_batches is not None
        and options.crash_after_batches < 1
    ):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "crash_after_batches must be >= 1",
            {"crash_after_batches": options.crash_after_batches},
        )

    original_bundle = read_config(config_source)
    bundle, requested = _apply_overrides(original_bundle, options)
    tasks = load_tasks(tasks_source)
    if not tasks:
        raise WorkbenchError(
            E_FORMAT_CORRUPT, "task list is empty", {"path": str(tasks_source)}
        )
    for task in tasks:
        if task["domain"] not in bundle.data["domain_tags"]:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "task domain has no controlled tag set",
                {
                    "sentence_id": task["sentence_id"],
                    "task_domain": task["domain"],
                    "available_domains": sorted(
                        bundle.data["domain_tags"].keys()
                    ),
                },
            )

    decision = resolve_resources(bundle.data, tasks)
    if decision.workers != bundle.data["workers"]:
        bundle.data["workers"] = decision.workers
    if decision.batch_size != bundle.data["batch_size"]:
        bundle.data["batch_size"] = decision.batch_size

    cache = ContentAddressedCache(cache_dir)
    state = StateStore(state_dir, cache)
    logical_clock = bundle.data["logical_clock_ms"]
    pending_discarded = state.discard_pending(ledger_clock_ms=logical_clock)
    snapshot = state.commit_config_snapshot(bundle, logical_clock_ms=logical_clock)
    digest = task_digest(tasks)
    old_manifest = state.read_manifest()
    plan = plan_and_alias(
        old_manifest=old_manifest,
        new_bundle=bundle,
        tasks=tasks,
        cache=cache,
    )
    state.ledger.append(
        "run_start",
        logical_clock,
        {
            "config_effective_hash": bundle.effective_hash,
            "task_digest": digest,
            "sentence_count": len(tasks),
            "requested": requested,
            "resources": decision.to_dict(),
            "snapshot_id": snapshot["snapshot_id"],
            "pending_discarded": pending_discarded,
            "cache_plan": plan.to_dict(),
        },
    )
    for degradation in decision.degradations:
        state.ledger.append(
            "resource_degradation",
            logical_clock,
            degradation,
        )

    machine = StageMachine()
    stage_artifacts: dict[str, dict[str, dict[str, Any]]] = {}
    stage_hashes: dict[str, dict[str, str]] = {}
    stage_entries: dict[str, dict[str, dict[str, Any]]] = {}
    checkpoints: dict[str, dict[str, Any]] = {}
    stage_reports: list[dict[str, Any]] = []
    config_fingerprint = bundle.effective_hash

    try:
        for stage in STAGES:
            machine.enter(stage)
            checkpoint = None
            if options.use_checkpoints:
                checkpoint = state.load_checkpoint(
                    stage,
                    task_digest=digest,
                    config_fingerprint=config_fingerprint,
                )
            previous_artifacts = stage_artifacts
            previous_hashes = stage_hashes.get(
                {"candidates": "normalize",
                 "disambiguation": "candidates",
                 "tagging": "disambiguation"}.get(stage, ""),
                {},
            )
            if checkpoint is not None:
                artifacts, hashes, entries = _checkpoint_artifacts(
                    checkpoint=checkpoint,
                    stage=stage,
                    tasks=tasks,
                    cache=cache,
                    bundle=bundle,
                    previous_hashes=previous_hashes,
                )
                checkpoints[stage] = checkpoint
                stage_report = {
                    "stage": stage,
                    "checkpoint_reused": True,
                    "checkpoint_version": checkpoint["version"],
                    "cache_hits": 0,
                    "computed": 0,
                    "batches": 0,
                }
                state.ledger.append(
                    "checkpoint_reused",
                    logical_clock,
                    {
                        "stage": stage,
                        "version": checkpoint["version"],
                        "completed_count": len(artifacts),
                    },
                    checkpoint_version=checkpoint["version"],
                )
            else:
                artifacts, hashes, entries, stage_report = _compute_stage(
                    stage=stage,
                    tasks=tasks,
                    bundle=bundle,
                    cache=cache,
                    state=state,
                    previous_artifacts=previous_artifacts,
                    previous_hashes=previous_hashes,
                    decision=decision,
                    options=options,
                )
                payload = {
                    "schema": "stage-payload-v1",
                    "stage": stage,
                    "artifacts": [
                        artifacts[task["sentence_id"]] for task in tasks
                    ],
                }
                checkpoint = state.commit_checkpoint(
                    stage,
                    completed_sentence_ids=[
                        task["sentence_id"] for task in tasks
                    ],
                    expected_sentence_ids={
                        task["sentence_id"] for task in tasks
                    },
                    payload=payload,
                    task_digest=digest,
                    config_fingerprint=config_fingerprint,
                    logical_clock_ms=logical_clock,
                )
                checkpoints[stage] = checkpoint
                stage_report["checkpoint_reused"] = False
                stage_report["checkpoint_version"] = checkpoint["version"]
            stage_artifacts[stage] = artifacts
            stage_hashes[stage] = hashes
            stage_entries[stage] = entries
            stage_reports.append(stage_report)
            state.ledger.append(
                "stage_complete",
                logical_clock,
                stage_report,
                checkpoint_version=checkpoint["version"],
            )
            LOGGER.info(
                "stage %s complete: %s",
                stage,
                stage_report,
            )
            machine.complete()

        tagged_artifacts = stage_artifacts["tagging"]
        if (
            options.verify_parallel
            and requested["workers"] > 1
            and decision.workers > 1
        ):
            parallel_verification = _verify_serial_parallel(
                tasks, bundle.data, tagged_artifacts
            )
            state.ledger.append(
                "parallel_verification",
                logical_clock,
                parallel_verification,
            )
        else:
            parallel_verification = {
                "status": (
                    "serial_path"
                    if decision.workers == 1
                    else "not_requested"
                ),
                "first_difference": None,
            }

        outputs: list[dict[str, Any]] = []
        manifest_sentences: dict[str, Any] = {}
        final_hashes: list[str] = []
        reference_token_matches = 0
        reference_pos_matches = 0
        for task in tasks:
            sentence_id = task["sentence_id"]
            normalized = stage_artifacts["normalize"][sentence_id]
            candidates = stage_artifacts["candidates"][sentence_id]
            tagged = tagged_artifacts[sentence_id]
            comparison = _comparison_payload(tagged)
            final_hash = content_hash(comparison)
            final_hashes.append(final_hash)
            token_match = (
                [token["text"] for token in tagged["tokens"]]
                == task["token_list"]
            )
            pos_match = tagged["pos_sequence"] == task["pos_tag_list"]
            reference_token_matches += int(token_match)
            reference_pos_matches += int(pos_match)
            output = {
                "sentence_id": sentence_id,
                "raw_text": task["raw_text"],
                "normalized_text": normalized["normalized_text"],
                "candidates": candidates["candidates"],
                "tokens": tagged["tokens"],
                "pos_sequence": tagged["pos_sequence"],
                "validation": tagged["validation"],
                "checksum": final_hash,
                "reference_match": {
                    "token_list": token_match,
                    "pos_tag_list": pos_match,
                },
            }
            outputs.append(output)

            starts: dict[int, int] = {}
            dictionary_terms: set[str] = set()
            rule_ids: set[str] = set()
            for candidate in candidates["candidates"]:
                if candidate["source"] != "unknown":
                    starts[candidate["start"]] = starts.get(candidate["start"], 0) + 1
                if candidate["source"] == "dictionary":
                    dictionary_terms.add(candidate["text"])
                elif candidate["source"] in ("prefix_rule", "suffix_rule"):
                    rule_ids.add(str(candidate["source_id"]))
            manifest_sentences[sentence_id] = {
                "raw_hash": content_hash(task["raw_text"]),
                "normalized_text": normalized["normalized_text"],
                "domain": task["domain"],
                "locale": task["lang"],
                "dictionary_terms": sorted(dictionary_terms),
                "rule_ids": sorted(rule_ids),
                "candidate_overlap_count": sum(
                    1 for count in starts.values() if count > 1
                ),
                "stages": {
                    stage: stage_entries[stage][sentence_id] for stage in STAGES
                },
                "final_hash": final_hash,
            }

        output_bytes = canonical_json_bytes(outputs)
        manifest = {
            "schema": "run-manifest-v1",
            "task_digest": digest,
            "config": bundle.data,
            "config_effective_hash": bundle.effective_hash,
            "semantic_hashes": bundle.semantic_hashes,
            "cache_plan": plan.to_dict(),
            "checkpoints": checkpoints,
            "sentences": manifest_sentences,
            "output_sha256": sha256_bytes(output_bytes),
        }
        state.write_manifest(manifest)
        report = {
            "ok": True,
            "config_source": str(config_source),
            "tasks_source": str(tasks_source),
            "sentence_count": len(tasks),
            "task_digest": digest,
            "config_effective_hash": bundle.effective_hash,
            "semantic_hashes": bundle.semantic_hashes,
            "requested_resources": requested,
            "resources": decision.to_dict(),
            "degradations": decision.degradations,
            "config_snapshot": snapshot,
            "pending_discarded": pending_discarded,
            "cache_plan": plan.to_dict(),
            "cache_stats": cache.stats(),
            "stage_reports": stage_reports,
            "checkpoints": checkpoints,
            "parallel_verification": parallel_verification,
            "output_sha256": sha256_bytes(output_bytes),
            "final_hashes": final_hashes,
            "reference_metrics": {
                "token_list_matches": reference_token_matches,
                "pos_tag_list_matches": reference_pos_matches,
                "sentence_count": len(tasks),
            },
        }
        state.write_last_run(report)
        state.ledger.append(
            "run_complete",
            logical_clock,
            {
                "output_sha256": report["output_sha256"],
                "sentence_count": len(tasks),
                "checkpoints": {
                    stage: checkpoint["version"]
                    for stage, checkpoint in checkpoints.items()
                },
            },
        )
        return PipelineRun(outputs=outputs, report=report)
    except SimulatedCrash as exc:
        report = _failure_report(
            stage=exc.stage,
            reason="simulated_crash",
            simulated_crash=True,
            checkpoints=checkpoints,
            cache_plan=plan,
            decision=decision,
            cache=cache,
        )
        report["batches_completed"] = exc.batches_completed
        state.write_last_run(report)
        state.ledger.append(
            "run_interrupted",
            logical_clock,
            {
                "stage": exc.stage,
                "batches_completed": exc.batches_completed,
                "checkpoints": {
                    stage: checkpoint["version"]
                    for stage, checkpoint in checkpoints.items()
                },
            },
        )
        raise
    except WorkbenchError as exc:
        state.write_last_run(
            _failure_report(
                stage=(
                    STAGES[machine.index]
                    if machine.index < len(STAGES)
                    else "complete"
                ),
                reason=exc.code,
                simulated_crash=False,
                checkpoints=checkpoints,
                cache_plan=plan,
                decision=decision,
                cache=cache,
            )
        )
        state.ledger.append(
            "run_error",
            logical_clock,
            exc.to_dict()["error"],
        )
        raise

