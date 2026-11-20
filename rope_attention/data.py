"""Deterministic task manifest and Q/K/V sample generation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import E_INPUT_CORRUPT, RopeError
from .models import SUPPORTED_CONFIG_VERSIONS, V3_VARIANTS
from .util import (
    atomic_write_bytes,
    canonical_json_bytes,
    hash_array,
    load_json_checked,
    sha256_bytes,
    sha256_json,
)


TASK_FIELDS = (
    "case_id",
    "seq_len",
    "head_dim",
    "num_heads",
    "base_theta",
    "pos_encoding_variant",
    "pos_scale",
    "block_size",
    "causal",
    "seed",
    "config_version",
)


@dataclass(frozen=True)
class AttentionTask:
    case_id: str
    seq_len: int
    head_dim: int
    num_heads: int
    base_theta: float
    pos_encoding_variant: str
    pos_scale: float
    block_size: int
    causal: bool
    seed: int
    config_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_theta": self.base_theta,
            "block_size": self.block_size,
            "case_id": self.case_id,
            "causal": self.causal,
            "config_version": self.config_version,
            "head_dim": self.head_dim,
            "num_heads": self.num_heads,
            "pos_encoding_variant": self.pos_encoding_variant,
            "pos_scale": self.pos_scale,
            "seed": self.seed,
            "seq_len": self.seq_len,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AttentionTask":
        missing = sorted(set(TASK_FIELDS) - set(value))
        unknown = sorted(set(value) - set(TASK_FIELDS))
        if missing or unknown:
            raise RopeError(
                E_INPUT_CORRUPT,
                "task fields do not match the declared schema",
                details={"missing": missing, "unknown": unknown},
            )
        try:
            case_id = str(value["case_id"])
            seq_len = _strict_int(value["seq_len"], "seq_len")
            head_dim = _strict_int(value["head_dim"], "head_dim")
            num_heads = _strict_int(value["num_heads"], "num_heads")
            base_theta = _strict_float(value["base_theta"], "base_theta")
            variant = str(value["pos_encoding_variant"])
            pos_scale = _strict_float(value["pos_scale"], "pos_scale")
            block_size = _strict_int(value["block_size"], "block_size")
            causal = value["causal"]
            seed = _strict_int(value["seed"], "seed")
            config_version = str(value["config_version"])
        except (TypeError, ValueError) as exc:
            raise RopeError(
                E_INPUT_CORRUPT,
                "task field cannot be coerced to its declared type",
                details={"reason": str(exc)},
            ) from exc
        if not case_id:
            raise RopeError(
                E_INPUT_CORRUPT,
                "case_id must not be empty",
                details={"field": "case_id"},
            )
        if seq_len <= 0 or head_dim <= 0 or num_heads <= 0 or block_size <= 0:
            raise RopeError(
                E_INPUT_CORRUPT,
                "shape and block fields must be positive",
                details={"case_id": case_id},
            )
        if head_dim % 2:
            raise RopeError(
                E_INPUT_CORRUPT,
                "head_dim must be even for interleaved RoPE pairs",
                details={"case_id": case_id, "head_dim": head_dim},
            )
        if base_theta <= 0 or pos_scale <= 0:
            raise RopeError(
                E_INPUT_CORRUPT,
                "base_theta and pos_scale must be positive",
                details={"case_id": case_id},
            )
        if not math.isfinite(base_theta) or not math.isfinite(pos_scale):
            raise RopeError(
                E_INPUT_CORRUPT,
                "position parameters must be finite",
                details={"case_id": case_id},
            )
        if variant not in V3_VARIANTS:
            raise RopeError(
                E_INPUT_CORRUPT,
                "task variant is not supported",
                details={"case_id": case_id, "variant": variant},
            )
        if not isinstance(causal, bool):
            raise RopeError(
                E_INPUT_CORRUPT,
                "causal must be boolean",
                details={"case_id": case_id},
            )
        if seed < 0:
            raise RopeError(
                E_INPUT_CORRUPT,
                "seed must be non-negative",
                details={"case_id": case_id},
            )
        if config_version not in SUPPORTED_CONFIG_VERSIONS:
            raise RopeError(
                E_INPUT_CORRUPT,
                "task config_version is not supported",
                details={"case_id": case_id, "config_version": config_version},
            )
        return cls(
            case_id=case_id,
            seq_len=seq_len,
            head_dim=head_dim,
            num_heads=num_heads,
            base_theta=base_theta,
            pos_encoding_variant=variant,
            pos_scale=pos_scale,
            block_size=block_size,
            causal=causal,
            seed=seed,
            config_version=config_version,
        )


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} cannot be boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value.strip())
    raise ValueError(f"{field} is not an integer")


def _strict_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} cannot be boolean")
    return float(value)


@dataclass(frozen=True)
class AttentionTensors:
    q: np.ndarray
    k: np.ndarray
    v: np.ndarray

    def hashes(self) -> dict[str, str]:
        return {
            "k": hash_array(self.k),
            "q": hash_array(self.q),
            "v": hash_array(self.v),
        }


def load_tasks(path: Path) -> list[AttentionTask]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RopeError(
            E_INPUT_CORRUPT,
            "unable to read task manifest",
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    tasks: list[AttentionTask] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RopeError(
                E_INPUT_CORRUPT,
                "task JSONL line is not valid JSON",
                details={"path": str(path), "line": line_number, "reason": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise RopeError(
                E_INPUT_CORRUPT,
                "task JSONL line must be an object",
                details={"path": str(path), "line": line_number},
            )
        task = AttentionTask.from_dict(value)
        if task.case_id in seen:
            raise RopeError(
                E_INPUT_CORRUPT,
                "task case_id is duplicated",
                details={"path": str(path), "line": line_number, "case_id": task.case_id},
            )
        seen.add(task.case_id)
        tasks.append(task)
    return sorted(tasks, key=lambda item: item.case_id)


def write_tasks(path: Path, tasks: Iterable[AttentionTask]) -> None:
    ordered = sorted(tasks, key=lambda item: item.case_id)
    data = b"".join(canonical_json_bytes(task.to_dict()) + b"\n" for task in ordered)
    atomic_write_bytes(path, data)


def generate_tensors(task: AttentionTask) -> AttentionTensors:
    rng = np.random.Generator(np.random.PCG64(task.seed))
    shape = (task.num_heads, task.seq_len, task.head_dim)
    q = rng.standard_normal(shape, dtype=np.float64)
    k = rng.standard_normal(shape, dtype=np.float64)
    v = rng.standard_normal(shape, dtype=np.float64)
    return AttentionTensors(
        q=np.ascontiguousarray(q),
        k=np.ascontiguousarray(k),
        v=np.ascontiguousarray(v),
    )


def write_tensors(path: Path, task: AttentionTask, tensors: AttentionTensors) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "case_id": task.case_id,
        "hashes": tensors.hashes(),
        "k": tensors.k,
        "q": tensors.q,
        "v": tensors.v,
    }
    with path.open("wb") as handle:
        np.savez(handle, **payload)


def load_tensors(path: Path, task: AttentionTask) -> AttentionTensors:
    try:
        with np.load(path, allow_pickle=False) as archive:
            q = np.ascontiguousarray(archive["q"], dtype=np.float64)
            k = np.ascontiguousarray(archive["k"], dtype=np.float64)
            v = np.ascontiguousarray(archive["v"], dtype=np.float64)
    except (OSError, KeyError, ValueError) as exc:
        raise RopeError(
            E_INPUT_CORRUPT,
            "unable to read Q/K/V tensor archive",
            details={"path": str(path), "case_id": task.case_id, "reason": str(exc)},
        ) from exc
    expected = (task.num_heads, task.seq_len, task.head_dim)
    if q.shape != expected or k.shape != expected or v.shape != expected:
        raise RopeError(
            E_INPUT_CORRUPT,
            "tensor shapes do not match the task manifest",
            details={
                "case_id": task.case_id,
                "expected": list(expected),
                "q": list(q.shape),
                "k": list(k.shape),
                "v": list(v.shape),
            },
        )
    return AttentionTensors(q=q, k=k, v=v)


def sample_configs() -> dict[str, str]:
    """Three semantically equivalent samples with deliberately different syntax."""

    return {
        "v1": "v1\n10000.0|1.0|6\n",
        "v2": "v2\nbase_theta=10000.0|pos_scale=1.0|block_size=6|variant=rope\n",
        "v3": (
            "v3\n"
            '{"base_theta":10000.0,"block_size":6,'
            '"frequency_adjustment":{"block_tokens":12,"factor":1.0,"method":"identity"},'
            '"per_head_scale":[1.0,1.0,1.0,1.0],"pos_scale":1.0,"variant":"rope"}\n'
        ),
    }


def lossy_v3_config() -> str:
    return (
        "v3\n"
        '{"base_theta":10000.0,"block_size":6,'
        '"frequency_adjustment":{"block_tokens":4,"factor":1.25,"method":"blockwise"},'
        '"per_head_scale":[0.5,1.0,1.5,2.0],"pos_scale":1.0,"variant":"block_freq"}\n'
    )


def sample_tasks() -> list[AttentionTask]:
    specs = [
        ("case_a_causal", 33, 16, 4, 101, True, "v1"),
        ("case_b_full", 27, 12, 4, 202, False, "v2"),
        ("case_c_causal", 41, 8, 4, 303, True, "v3"),
        ("case_d_causal", 19, 16, 4, 404, True, "v2"),
    ]
    return [
        AttentionTask(
            case_id=case_id,
            seq_len=seq_len,
            head_dim=head_dim,
            num_heads=num_heads,
            base_theta=10000.0,
            pos_encoding_variant="rope",
            pos_scale=1.0,
            block_size=6,
            causal=causal,
            seed=seed,
            config_version=config_version,
        )
        for case_id, seq_len, head_dim, num_heads, seed, causal, config_version in specs
    ]


def prepare_sample_workspace(root: Path) -> dict[str, Any]:
    root = root.resolve()
    samples = root / "samples"
    config_dir = samples / "configs"
    tensor_dir = samples / "tensors"
    config_dir.mkdir(parents=True, exist_ok=True)
    tensor_dir.mkdir(parents=True, exist_ok=True)

    configs = sample_configs()
    for version, text in configs.items():
        atomic_write_bytes(config_dir / f"{version}.conf", text.encode("utf-8"))
        atomic_write_bytes(config_dir / f"{version}.conf.sha256", sha256_bytes(text.encode("utf-8")).encode("ascii") + b"\n")
    atomic_write_bytes(config_dir / "v3_lossy.conf", lossy_v3_config().encode("utf-8"))

    tasks = sample_tasks()
    write_tasks(samples / "tasks.jsonl", tasks)
    manifest: dict[str, Any] = {
        "schema": "rope-attention-sample/v1",
        "tasks_sha256": sha256_bytes((samples / "tasks.jsonl").read_bytes()),
        "tensors": {},
    }
    for task in tasks:
        tensors = generate_tensors(task)
        tensor_path = tensor_dir / f"{task.case_id}.npz"
        write_tensors(tensor_path, task, tensors)
        manifest["tensors"][task.case_id] = {
            "hashes": tensors.hashes(),
            "path": str(tensor_path.relative_to(root)).replace("\\", "/"),
            "shape": [task.num_heads, task.seq_len, task.head_dim],
        }
    manifest["checksum"] = sha256_json(manifest)
    atomic_write_bytes(
        samples / "tensor_manifest.json",
        canonical_json_bytes(manifest, pretty=True) + b"\n",
    )
    return manifest


def verify_tensor_manifest(root: Path) -> dict[str, Any]:
    samples = root / "samples"
    manifest = load_json_checked(samples / "tensor_manifest.json")
    if manifest.get("tasks_sha256") != sha256_bytes((samples / "tasks.jsonl").read_bytes()):
        raise RopeError(
            E_INPUT_CORRUPT,
            "task manifest checksum mismatch",
            details={"path": str(samples / "tasks.jsonl")},
        )
    checks: list[dict[str, Any]] = []
    ok = True
    for task in load_tasks(samples / "tasks.jsonl"):
        tensors = load_tensors(samples / "tensors" / f"{task.case_id}.npz", task)
        expected = manifest["tensors"][task.case_id]["hashes"]
        actual = tensors.hashes()
        matches = expected == actual
        ok = ok and matches
        checks.append(
            {
                "case_id": task.case_id,
                "hashes": actual,
                "matches_manifest": matches,
            }
        )
    return {"ok": ok, "checks": checks}

