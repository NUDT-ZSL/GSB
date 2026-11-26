"""Deterministic decomposition of operator jobs into dependency-bound subtasks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from .models import Plan, Task
from .util import canonical_json, digest_obj, digest_text


@dataclass(frozen=True)
class Subtask:
    subtask_id: str
    job_id: str
    job_revision: int
    task_hash: str
    ordinal: int
    phase: str
    shard_index: int
    shard_count: int
    input_shard: dict[str, Any]
    input_shapes: tuple[tuple[int, ...], ...]
    output_shape: tuple[int, ...]
    seed: int
    dependencies: tuple[str, ...]
    precision_sensitive: bool

    @property
    def semantic_key(self) -> tuple[str, str, int]:
        return (self.job_id, self.phase, self.shard_index)

    @property
    def key_string(self) -> str:
        return f"{self.job_id}:{self.phase}:{self.shard_index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "task_hash": self.task_hash,
            "ordinal": self.ordinal,
            "phase": self.phase,
            "shard_index": self.shard_index,
            "shard_count": self.shard_count,
            "input_shard": self.input_shard,
            "input_shapes": [list(shape) for shape in self.input_shapes],
            "output_shape": list(self.output_shape),
            "seed": self.seed,
            "dependencies": list(self.dependencies),
            "precision_sensitive": self.precision_sensitive,
        }

    def hash_payload(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("subtask_id", None)
        return payload

    @property
    def subtask_hash(self) -> str:
        return digest_obj(self.hash_payload())


@dataclass(frozen=True)
class SubtaskChain:
    job_id: str
    revision: int
    subtasks: tuple[Subtask, ...]

    @property
    def digest(self) -> str:
        return digest_obj(
            {
                "job_id": self.job_id,
                "revision": self.revision,
                "subtasks": [
                    {
                        "subtask_id": subtask.subtask_id,
                        "ordinal": subtask.ordinal,
                        "semantic_key": list(subtask.semantic_key),
                        "dependencies": list(subtask.dependencies),
                    }
                    for subtask in self.subtasks
                ],
            }
        )

    @property
    def final_subtask(self) -> Subtask:
        return self.subtasks[-1]


def _rng_seed(global_seed: int, task: Task, *parts: Any) -> int:
    payload = {
        "global_seed": global_seed,
        "job_id": task.job_id,
        "task_hash": task.task_hash,
        "parts": list(parts),
    }
    return int(hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16], 16)


def _shard_count(global_seed: int, task: Task, reduction_dim: int) -> int:
    if reduction_dim <= 1:
        return 1
    seed = _rng_seed(global_seed, task, "shard_count")
    return min(reduction_dim, 2 + seed % 3)


def _split_dimension(total: int, parts: int) -> list[tuple[int, int]]:
    base = total // parts
    remainder = total % parts
    offset = 0
    result: list[tuple[int, int]] = []
    for index in range(parts):
        size = base + (1 if index < remainder else 0)
        result.append((offset, size))
        offset += size
    return result


def _output_shape(task: Task) -> tuple[int, ...]:
    lhs, rhs = task.input_shapes
    if task.op_kind == "matmul":
        return (lhs[0], rhs[1])
    _, _, height, width = lhs
    _, _, kernel_h, kernel_w = rhs
    return (lhs[0], rhs[0], height - kernel_h + 1, width - kernel_w + 1)


def generate_job_subtasks(
    task: Task,
    global_seed: int,
    *,
    external_dependencies: Iterable[str] = (),
) -> SubtaskChain:
    """Generate the canonical weight-reduction -> activation chain for one job.

    All choices are made from the SHA-256 of canonical domain data.  Nothing
    depends on Python's hash randomisation, wall-clock time, process ordering,
    or filesystem enumeration.
    """

    lhs, rhs = task.input_shapes
    reduction_dim = lhs[1] if task.op_kind == "matmul" else lhs[1]
    shard_count = _shard_count(global_seed, task, reduction_dim)
    spans = _split_dimension(reduction_dim, shard_count)
    output_shape = _output_shape(task)
    external_deps = tuple(sorted(set(external_dependencies)))
    subtasks: list[Subtask] = []
    prior_dependency: tuple[str, ...] = ()

    for shard_index, (offset, size) in enumerate(spans):
        if task.op_kind == "matmul":
            input_shapes = ((lhs[0], size), (size, rhs[1]))
        else:
            input_shapes = (
                (lhs[0], size, lhs[2], lhs[3]),
                (rhs[0], size, rhs[2], rhs[3]),
            )
        dependencies = prior_dependency if shard_index > 0 else external_deps
        seed = _rng_seed(
            global_seed,
            task,
            "weight_sum",
            shard_index,
            shard_count,
            offset,
            size,
            dependencies,
        )
        payload = {
            "job_id": task.job_id,
            "job_revision": task.revision,
            "task_hash": task.task_hash,
            "ordinal": len(subtasks),
            "phase": "weight_sum",
            "shard_index": shard_index,
            "shard_count": shard_count,
            "input_shard": {
                "axis": 1,
                "offset": offset,
                "size": size,
                "index": shard_index,
                "count": shard_count,
            },
            "input_shapes": [list(shape) for shape in input_shapes],
            "output_shape": list(output_shape),
            "seed": seed,
            "dependencies": list(dependencies),
            "precision_sensitive": task.precision_sensitive,
        }
        subtask_id = "st_" + digest_obj(payload)
        subtask = Subtask(
            subtask_id=subtask_id,
            job_id=task.job_id,
            job_revision=task.revision,
            task_hash=task.task_hash,
            ordinal=len(subtasks),
            phase="weight_sum",
            shard_index=shard_index,
            shard_count=shard_count,
            input_shard=payload["input_shard"],
            input_shapes=input_shapes,
            output_shape=output_shape,
            seed=seed,
            dependencies=dependencies,
            precision_sensitive=task.precision_sensitive,
        )
        subtasks.append(subtask)
        prior_dependency = (subtask_id,)

    activation_dependencies = tuple(subtask.subtask_id for subtask in subtasks)
    activation_seed = _rng_seed(
        global_seed,
        task,
        "activation",
        0,
        1,
        activation_dependencies,
    )
    payload = {
        "job_id": task.job_id,
        "job_revision": task.revision,
        "task_hash": task.task_hash,
        "ordinal": len(subtasks),
        "phase": "activation",
        "shard_index": 0,
        "shard_count": 1,
        "input_shard": {
            "axis": 1,
            "offset": 0,
            "size": reduction_dim,
            "index": 0,
            "count": 1,
        },
        "input_shapes": [list(shape) for shape in task.input_shapes],
        "output_shape": list(output_shape),
        "seed": activation_seed,
        "dependencies": list(activation_dependencies),
        "precision_sensitive": task.precision_sensitive,
    }
    activation_id = "st_" + digest_obj(payload)
    subtasks.append(
        Subtask(
            subtask_id=activation_id,
            job_id=task.job_id,
            job_revision=task.revision,
            task_hash=task.task_hash,
            ordinal=len(subtasks),
            phase="activation",
            shard_index=0,
            shard_count=1,
            input_shard=payload["input_shard"],
            input_shapes=task.input_shapes,
            output_shape=output_shape,
            seed=activation_seed,
            dependencies=activation_dependencies,
            precision_sensitive=task.precision_sensitive,
        )
    )
    return SubtaskChain(
        job_id=task.job_id,
        revision=task.revision,
        subtasks=tuple(subtasks),
    )


def _topological_jobs(plan: Plan) -> list[str]:
    jobs = plan.jobs
    state: dict[str, int] = {}
    result: list[str] = []

    def visit(job_id: str) -> None:
        if state.get(job_id) == 2:
            return
        if state.get(job_id) == 1:
            raise RuntimeError("validated plan unexpectedly contains a cycle")
        state[job_id] = 1
        for dep in sorted(jobs[job_id].dependencies):
            visit(dep)
        state[job_id] = 2
        result.append(job_id)

    for job_id in sorted(jobs):
        visit(job_id)
    return result


def generate_plan_chains(
    plan: Plan, tasks: dict[str, Task]
) -> dict[str, SubtaskChain]:
    """Generate every job chain, including cross-job dependencies."""

    chains: dict[str, SubtaskChain] = {}
    for job_id in _topological_jobs(plan):
        if job_id not in tasks:
            raise KeyError(job_id)
        external: list[str] = []
        for dep in plan.jobs[job_id].dependencies:
            dep_chain = chains.get(dep)
            if dep_chain is None:
                raise KeyError(dep)
            external.append(dep_chain.final_subtask.subtask_id)
        chains[job_id] = generate_job_subtasks(
            tasks[job_id],
            plan.global_seed,
            external_dependencies=external,
        )
    return chains


def flatten_chains(chains: dict[str, SubtaskChain]) -> list[Subtask]:
    return [
        subtask
        for job_id in sorted(chains)
        for subtask in chains[job_id].subtasks
    ]


def plan_subtask_summary(chains: dict[str, SubtaskChain]) -> str:
    return digest_obj(
        {
            job_id: {
                "revision": chain.revision,
                "digest": chain.digest,
                "subtasks": [
                    {
                        "subtask_id": subtask.subtask_id,
                        "ordinal": subtask.ordinal,
                        "phase": subtask.phase,
                        "shard_index": subtask.shard_index,
                        "dependencies": list(subtask.dependencies),
                        "seed": subtask.seed,
                        "input_shard": subtask.input_shard,
                    }
                    for subtask in chain.subtasks
                ],
            }
            for job_id, chain in sorted(chains.items())
        }
    )


def simulate_output_digest(subtask: Subtask) -> str:
    """Deterministic stand-in for a committed engine result digest."""

    return digest_text(
        canonical_json(
            {
                "subtask_id": subtask.subtask_id,
                "subtask_hash": subtask.subtask_hash,
                "seed": subtask.seed,
                "input_shard": subtask.input_shard,
                "output_shape": list(subtask.output_shape),
            }
        )
    )

