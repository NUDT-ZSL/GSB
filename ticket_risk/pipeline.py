"""Per-sample stage pipeline and full-serial verification helpers."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Event
from typing import Any, Callable

from .errors import E_STAGE_EXCEPTION, Failure
from .loader import Sample
from .scoring import fusion_stage, normalize_stage, risk_stage, sentiment_stage
from .state import STAGES, RuntimeState
from .util import sha256_obj


ALL_STAGES = set(STAGES)


@dataclass
class ChangePlan:
    command_version: int
    affected_stages: dict[str, set[str]] = field(default_factory=dict)
    reason: str = ""

    def add(self, sample_ids: list[str] | set[str], stages: set[str]) -> None:
        for sample_id in sample_ids:
            self.affected_stages.setdefault(sample_id, set()).update(stages)

    def merge(self, other: "ChangePlan") -> None:
        for sample_id, stages in other.affected_stages.items():
            self.affected_stages.setdefault(sample_id, set()).update(stages)
        if other.reason:
            self.reason = (
                f"{self.reason};{other.reason}" if self.reason else other.reason
            )

    @property
    def affected_sample_ids(self) -> list[str]:
        return sorted(self.affected_stages)

    @property
    def total_samples(self) -> int:
        return len(self.affected_stages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_version": self.command_version,
            "reason": self.reason,
            "affected_sample_ids": self.affected_sample_ids,
            "affected_stage_counts": {
                stage: sum(stage in stages for stages in self.affected_stages.values())
                for stage in STAGES
            },
        }


@dataclass
class SampleComputation:
    sample_id: str
    task_order_key: str
    artifacts: dict[str, dict[str, Any]]
    failure: Failure | None
    computed_stages: list[str]
    reused_stages: list[str]


def stable_stage_order_key(sample_id: str, stage: str) -> str:
    return sha256_obj({"sample_id": sample_id, "stage": stage})


def compute_sample(
    sample: Sample,
    config: dict[str, Any],
    rules: dict[str, Any],
    base_artifacts: dict[str, dict[str, Any]] | None,
    recompute_stages: set[str] | None,
    force_all: bool = False,
) -> SampleComputation:
    base = base_artifacts or {}
    computed_stages: list[str] = []
    reused_stages: list[str] = []
    artifacts: dict[str, dict[str, Any]] = {}
    requested = set(recompute_stages or ())
    if force_all or not base or any(stage not in base for stage in STAGES):
        requested = set(ALL_STAGES)

    for stage in STAGES:
        if stage not in requested and stage in base:
            artifacts[stage] = base[stage]
            reused_stages.append(stage)
            continue
        try:
            if stage == "normalize":
                artifact = normalize_stage(sample, rules, config)
            elif stage == "sentiment":
                artifact = sentiment_stage(sample, artifacts["normalize"], rules, config)
            elif stage == "risk":
                artifact = risk_stage(sample, artifacts["normalize"], rules, config)
            elif stage == "fusion":
                artifact = fusion_stage(
                    sample,
                    artifacts["sentiment"],
                    artifacts["risk"],
                    rules,
                    config,
                )
            else:
                raise RuntimeError(f"unknown stage: {stage}")
        except Exception as exc:
            failure = Failure(
                business_code=E_STAGE_EXCEPTION,
                detail=str(exc),
                sample_id=sample.sample_id,
                line=sample.line,
                stage=stage,
                exception_type=type(exc).__name__,
                scope="sample_stage",
            )
            return SampleComputation(
                sample_id=sample.sample_id,
                task_order_key=stable_stage_order_key(sample.sample_id, "pipeline"),
                artifacts=artifacts,
                failure=failure,
                computed_stages=computed_stages,
                reused_stages=reused_stages,
            )
        artifact = dict(artifact)
        artifact["order_key"] = stable_stage_order_key(sample.sample_id, stage)
        artifacts[stage] = artifact
        computed_stages.append(stage)

    return SampleComputation(
        sample_id=sample.sample_id,
        task_order_key=stable_stage_order_key(sample.sample_id, "pipeline"),
        artifacts=artifacts,
        failure=None,
        computed_stages=computed_stages,
        reused_stages=reused_stages,
    )


def run_full_serial(
    samples: list[Sample],
    config: dict[str, Any],
    rules: dict[str, Any],
    loader_failures: list[Failure] | None = None,
) -> RuntimeState:
    """Baseline from-scratch execution in strict sample-ID order."""

    artifacts: dict[str, dict[str, dict[str, Any]]] = {}
    failures = list(loader_failures or [])
    for sample in sorted(samples, key=lambda item: item.sample_id):
        computation = compute_sample(sample, config, rules, None, None, force_all=True)
        artifacts[sample.sample_id] = computation.artifacts
        if computation.failure is not None:
            failures.append(computation.failure)
    failures.sort(key=lambda item: item.sort_key())
    return RuntimeState(
        version=0,
        command_count=0,
        samples=sorted(samples, key=lambda item: item.sample_id),
        rules=rules,
        config=config,
        artifacts=artifacts,
        failures=failures,
        base_config_hash="",
        base_resource_fingerprint="",
        samples_fingerprint="",
        stage_versions={stage: 1 for stage in STAGES},
    )


def run_parallel_sample_set(
    samples: list[Sample],
    config: dict[str, Any],
    rules: dict[str, Any],
    base_artifacts: dict[str, dict[str, dict[str, Any]]] | None,
    stage_plan: dict[str, set[str]] | None,
    max_workers: int,
    progress_callback: Callable[[SampleComputation], None] | None = None,
    cancel_event: Event | None = None,
) -> tuple[dict[str, SampleComputation], bool]:
    """Execute independent sample pipelines concurrently.

    The caller always merges by sample ID, so thread completion order has no
    effect on output order.  The boolean return value reports cancellation.
    """

    selected = sorted(samples, key=lambda item: item.sample_id)
    results: dict[str, SampleComputation] = {}
    cancelled = False
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as executor:
        futures: dict[Future[SampleComputation], str] = {}
        for sample in selected:
            stages = None if stage_plan is None else stage_plan.get(sample.sample_id, set())
            future = executor.submit(
                compute_sample,
                sample,
                config,
                rules,
                None if base_artifacts is None else base_artifacts.get(sample.sample_id, {}),
                stages,
                stage_plan is None,
            )
            futures[future] = sample.sample_id
        for future in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                for pending in futures:
                    pending.cancel()
                break
            computation = future.result()
            results[computation.sample_id] = computation
            if progress_callback is not None:
                progress_callback(computation)
    return results, cancelled


def compare_canonical(left: dict[str, Any], right: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if len(differences) >= limit:
            return
        if type(a) is not type(b):
            differences.append({"path": path, "left": a, "right": b})
            return
        if isinstance(a, dict):
            keys = sorted(set(a) | set(b))
            for key in keys:
                if key not in a:
                    differences.append({"path": f"{path}.{key}", "left": "<missing>", "right": b[key]})
                elif key not in b:
                    differences.append({"path": f"{path}.{key}", "left": a[key], "right": "<missing>"})
                else:
                    walk(a[key], b[key], f"{path}.{key}")
                if len(differences) >= limit:
                    return
            return
        if isinstance(a, list):
            if len(a) != len(b):
                differences.append({"path": f"{path}.length", "left": len(a), "right": len(b)})
            for index, (left_item, right_item) in enumerate(zip(a, b)):
                walk(left_item, right_item, f"{path}[{index}]")
                if len(differences) >= limit:
                    return
            return
        if a != b:
            differences.append({"path": path, "left": a, "right": b})

    walk(left, right, "$")
    return differences
