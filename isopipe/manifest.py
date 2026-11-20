from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .canonical import canonical_bytes, sha256_bytes, sha256_file
from .errors import PipelineError, error


TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SCHEMA = "isolated-pipeline-manifest-v1"
OPERATIONS = {"aggregate", "probe_read", "probe_write"}


@dataclass(frozen=True)
class TaskSpec:
    name: str
    inputs: tuple[str, ...]
    depends_on: tuple[str, ...]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    env_allowlist: tuple[str, ...]
    timeout_ms: int
    logical_cost_ms: int
    output: str
    workdir: str
    operation: str = "aggregate"
    probe_path: str | None = None
    compute_delay_ms: int = 0

    @property
    def permission_declaration(self) -> dict[str, Any]:
        return {
            "reads": list(self.reads),
            "writes": list(self.writes),
            "env_allowlist": list(self.env_allowlist),
            "timeout_ms": self.timeout_ms,
            "operation": self.operation,
            "output": self.output,
            "workdir": self.workdir,
            "compute_delay_ms": self.compute_delay_ms,
        }

    def as_dict(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "inputs": list(self.inputs),
            "depends_on": list(self.depends_on),
            "reads": list(self.reads),
            "writes": list(self.writes),
            "env_allowlist": list(self.env_allowlist),
            "timeout_ms": self.timeout_ms,
            "logical_cost_ms": self.logical_cost_ms,
            "output": self.output,
            "workdir": self.workdir,
            "operation": self.operation,
        }
        if self.probe_path is not None:
            result["probe_path"] = self.probe_path
        return result


@dataclass(frozen=True)
class Manifest:
    path: Path
    root: Path
    raw_digest: str
    schema: str
    kernel_version: str
    fixed_scale: int
    max_parallel: int
    latency_budget_ms: int
    clock_start_ms: int
    tasks: tuple[TaskSpec, ...]

    @property
    def by_name(self) -> dict[str, TaskSpec]:
        return {task.name: task for task in self.tasks}

    @property
    def topological_tasks(self) -> tuple[TaskSpec, ...]:
        by_name = self.by_name
        order_index = {task.name: index for index, task in enumerate(self.tasks)}
        remaining = set(by_name)
        completed: set[str] = set()
        result: list[TaskSpec] = []
        while remaining:
            ready = [
                by_name[name]
                for name in remaining
                if all(dep in completed for dep in by_name[name].depends_on)
            ]
            if not ready:
                raise error(
                    "E_DEPENDENCY_INVALID",
                    "dependency graph contains a cycle",
                    tasks=sorted(remaining),
                )
            ready.sort(key=lambda task: order_index[task.name])
            for task in ready:
                result.append(task)
                completed.add(task.name)
                remaining.remove(task.name)
        return tuple(result)

    def task_digest(self, task: TaskSpec) -> str:
        return sha256_bytes(canonical_bytes(task.as_dict()))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _load_json(path: Path, code: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_constant=_reject_json_constant)
    except FileNotFoundError:
        raise error(code, "manifest file does not exist", path=str(path))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise error(code, "manifest JSON cannot be parsed", path=str(path), reason=str(exc))


def _require_int(
    value: Any, field: str, *, positive: bool = False, allow_zero: bool = False
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error("E_MANIFEST_SCHEMA", f"{field} must be an integer", field=field)
    if positive and value <= 0:
        raise error("E_TIMEOUT_INVALID", f"{field} must be a positive integer", field=field)
    if allow_zero and value < 0:
        raise error("E_MANIFEST_SCHEMA", f"{field} must be non-negative", field=field)
    return value


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise error("E_MANIFEST_SCHEMA", f"{field} must be a list of strings", field=field)
    return tuple(value)


def _normalise_relative_path(raw: str, root: Path, field: str, *, allow_root: bool = False) -> str:
    if not raw or "\x00" in raw:
        raise error("E_MANIFEST_SCHEMA", f"{field} contains an empty or invalid path", path=raw)
    posix = PurePosixPath(raw.replace("\\", "/"))
    if posix.is_absolute() or re.match(r"^[A-Za-z]:", raw):
        raise error("E_PATH_ESCAPE", f"{field} must be relative to the workspace root", path=raw)
    if any(part == ".." for part in posix.parts):
        raise error("E_PATH_ESCAPE", f"{field} must not contain '..'", path=raw)
    normal = posix.as_posix()
    if normal in ("", "."):
        if allow_root:
            return "."
        raise error("E_PATH_ESCAPE", f"{field} must not be the workspace root", path=raw)
    absolute = (root / Path(*posix.parts)).resolve(strict=False)
    try:
        absolute.relative_to(root)
    except ValueError:
        raise error("E_PATH_ESCAPE", f"{field} escapes the workspace root", path=raw)
    if not allow_root and absolute == root:
        raise error("E_PATH_ESCAPE", f"{field} must not be the workspace root", path=raw)
    return absolute.relative_to(root).as_posix()


def _path_is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _covered_by_scope(path: Path, scopes: Iterable[Path]) -> bool:
    for scope in scopes:
        if path == scope or _path_is_within(path, scope):
            return True
    return False


def _resolve(root: Path, relative: str) -> Path:
    return (root / Path(*PurePosixPath(relative).parts)).resolve(strict=False)


def _check_task_paths(
    task: TaskSpec,
    root: Path,
    write_scopes: list[Path],
    read_scopes: list[Path],
) -> None:
    output = _resolve(root, task.output)
    workdir = _resolve(root, task.workdir)
    if not _covered_by_scope(output, write_scopes):
        raise error(
            "E_MANIFEST_SCHEMA",
            "task output must be inside a declared write scope",
            task=task.name,
            output=task.output,
        )
    if not _covered_by_scope(workdir, write_scopes):
        raise error(
            "E_MANIFEST_SCHEMA",
            "task workdir must be inside a declared write scope",
            task=task.name,
            workdir=task.workdir,
        )
    for source in task.inputs:
        if not _covered_by_scope(_resolve(root, source), read_scopes):
            raise error(
                "E_MANIFEST_SCHEMA",
                "task input must be inside a declared read scope",
                task=task.name,
                input=source,
            )


def load_and_validate_manifest(
    manifest_path: str | os.PathLike[str],
    workspace_root: str | os.PathLike[str],
    *,
    protected_paths: Iterable[str | os.PathLike[str]] = (),
) -> Manifest:
    root = Path(workspace_root).resolve()
    path = Path(manifest_path)
    if not path.is_absolute():
        path = root / path
    path = path.resolve(strict=False)
    data = _load_json(path, "E_MANIFEST_JSON")
    if not isinstance(data, dict):
        raise error("E_MANIFEST_SCHEMA", "manifest root must be a JSON object")
    if data.get("schema") != SCHEMA:
        raise error(
            "E_MANIFEST_SCHEMA",
            "unsupported manifest schema",
            expected=SCHEMA,
            actual=data.get("schema"),
        )
    kernel_version = data.get("kernel_version")
    if not isinstance(kernel_version, str) or not kernel_version:
        raise error("E_MANIFEST_SCHEMA", "kernel_version must be a non-empty string")
    fixed_scale = _require_int(data.get("fixed_scale"), "fixed_scale", allow_zero=True)
    if fixed_scale > 9:
        raise error("E_MANIFEST_SCHEMA", "fixed_scale must be at most 9")
    max_parallel = _require_int(data.get("max_parallel"), "max_parallel", positive=True)
    budget = _require_int(
        data.get("latency_budget_ms"), "latency_budget_ms", allow_zero=True
    )
    clock_start = _require_int(data.get("clock_start_ms", 0), "clock_start_ms", allow_zero=True)
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise error("E_MANIFEST_SCHEMA", "tasks must be a non-empty list")

    tasks: list[TaskSpec] = []
    names: set[str] = set()
    outputs: set[str] = set()
    for index, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise error("E_MANIFEST_SCHEMA", "each task must be an object", index=index)
        name = raw.get("name")
        if not isinstance(name, str) or not TASK_NAME_RE.fullmatch(name):
            raise error(
                "E_MANIFEST_SCHEMA",
                "task name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
                index=index,
            )
        if name in names:
            raise error("E_DUPLICATE_TASK", "task names must be unique", task=name)
        names.add(name)

        timeout_ms = _require_int(raw.get("timeout_ms"), "timeout_ms", positive=True)
        logical_cost = _require_int(
            raw.get("logical_cost_ms", 1), "logical_cost_ms", positive=True
        )
        compute_delay = _require_int(
            raw.get("compute_delay_ms", 0), "compute_delay_ms", allow_zero=True
        )
        if timeout_ms > 2**31 - 1:
            raise error("E_TIMEOUT_INVALID", "timeout_ms is too large", task=name)
        operation = raw.get("operation", "aggregate")
        if operation not in OPERATIONS:
            raise error(
                "E_MANIFEST_SCHEMA",
                "unsupported task operation",
                task=name,
                operation=operation,
            )
        inputs = _string_list(raw.get("inputs", []), "inputs")
        depends_on = _string_list(raw.get("depends_on", []), "depends_on")
        reads = _string_list(raw.get("reads", []), "reads")
        writes = _string_list(raw.get("writes", []), "writes")
        env_allowlist = _string_list(raw.get("env_allowlist", []), "env_allowlist")
        if len(set(env_allowlist)) != len(env_allowlist):
            raise error("E_ENV_INVALID", "environment whitelist has duplicates", task=name)
        for key in env_allowlist:
            if not ENV_NAME_RE.fullmatch(key):
                raise error(
                    "E_ENV_INVALID",
                    "environment whitelist key is invalid",
                    task=name,
                    key=key,
                )
        if not isinstance(raw.get("output"), str) or not raw.get("output"):
            raise error("E_MANIFEST_SCHEMA", "output must be a non-empty relative path", task=name)
        if not isinstance(raw.get("workdir"), str) or not raw.get("workdir"):
            raise error("E_MANIFEST_SCHEMA", "workdir must be a non-empty relative path", task=name)
        if len(set(inputs)) != len(inputs):
            raise error("E_MANIFEST_SCHEMA", "inputs contains duplicates", task=name)
        if len(set(depends_on)) != len(depends_on):
            raise error("E_MANIFEST_SCHEMA", "depends_on contains duplicates", task=name)
        if len(set(reads)) != len(reads) or len(set(writes)) != len(writes):
            raise error("E_MANIFEST_SCHEMA", "reads/writes contains duplicates", task=name)
        probe_path = raw.get("probe_path")
        if operation in ("probe_read", "probe_write"):
            if not isinstance(probe_path, str) or not probe_path:
                raise error(
                    "E_MANIFEST_SCHEMA", "probe tasks require probe_path", task=name
                )
        elif probe_path is not None:
            raise error(
                "E_MANIFEST_SCHEMA", "probe_path is only valid for probe tasks", task=name
            )

        reads_norm = tuple(
            _normalise_relative_path(item, root, "reads", allow_root=False) for item in reads
        )
        writes_norm = tuple(
            _normalise_relative_path(item, root, "writes", allow_root=False) for item in writes
        )
        inputs_norm = tuple(
            _normalise_relative_path(item, root, "inputs", allow_root=False) for item in inputs
        )
        output_norm = _normalise_relative_path(raw["output"], root, "output")
        workdir_norm = _normalise_relative_path(raw["workdir"], root, "workdir")
        probe_norm = (
            _normalise_relative_path(probe_path, root, "probe_path")
            if probe_path is not None
            else None
        )
        if output_norm in outputs:
            raise error(
                "E_MANIFEST_SCHEMA", "task outputs must be unique", task=name, output=output_norm
            )
        outputs.add(output_norm)

        read_scope_paths = [_resolve(root, item) for item in reads_norm]
        write_scope_paths = [_resolve(root, item) for item in writes_norm]
        task = TaskSpec(
            name=name,
            inputs=inputs_norm,
            depends_on=depends_on,
            reads=reads_norm,
            writes=writes_norm,
            env_allowlist=env_allowlist,
            timeout_ms=timeout_ms,
            logical_cost_ms=logical_cost,
            output=output_norm,
            workdir=workdir_norm,
            operation=operation,
            probe_path=probe_norm,
            compute_delay_ms=compute_delay,
        )
        _check_task_paths(task, root, write_scope_paths, read_scope_paths)
        for protected in protected_paths:
            protected_path = Path(protected)
            if not protected_path.is_absolute():
                protected_path = root / protected_path
            protected_path = protected_path.resolve(strict=False)
            for scope in [*write_scope_paths, *read_scope_paths]:
                if (
                    protected_path == scope
                    or _path_is_within(scope, protected_path)
                    or _path_is_within(protected_path, scope)
                ):
                    raise error(
                        "E_MANIFEST_SCHEMA",
                        "task scope overlaps protected pipeline state",
                        task=name,
                        scope=scope.relative_to(root).as_posix(),
                    )
        for source in inputs_norm:
            source_path = _resolve(root, source)
            if not source_path.exists():
                raise error(
                    "E_INPUT_MISSING",
                    "input dataset does not exist",
                    task=name,
                    input=source,
                )
            if not source_path.is_file():
                raise error(
                    "E_INPUT_INVALID",
                    "input dataset is not a regular file",
                    task=name,
                    input=source,
                )
            try:
                sha256_file(source_path)
            except OSError as exc:
                raise error(
                    "E_INPUT_INVALID",
                    "input digest cannot be computed",
                    task=name,
                    input=source,
                    reason=str(exc),
                )
        tasks.append(task)

    task_names = {task.name for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in task_names:
                raise error(
                    "E_DEPENDENCY_INVALID",
                    "dependency task does not exist",
                    task=task.name,
                    dependency=dependency,
                )
            if dependency == task.name:
                raise error(
                    "E_DEPENDENCY_INVALID", "task cannot depend on itself", task=task.name
                )
        for dependency in task.depends_on:
            dep_task = next(item for item in tasks if item.name == dependency)
            if not _covered_by_scope(_resolve(root, dep_task.output), [
                _resolve(root, item) for item in task.reads
            ]):
                raise error(
                    "E_MANIFEST_SCHEMA",
                    "dependency output must be inside the downstream read scope",
                    task=task.name,
                    dependency=dependency,
                    path=dep_task.output,
                )

    manifest = Manifest(
        path=path,
        root=root,
        raw_digest=sha256_bytes(canonical_bytes(data)),
        schema=SCHEMA,
        kernel_version=kernel_version,
        fixed_scale=fixed_scale,
        max_parallel=max_parallel,
        latency_budget_ms=budget,
        clock_start_ms=clock_start,
        tasks=tuple(tasks),
    )
    _ = manifest.topological_tasks
    return manifest
