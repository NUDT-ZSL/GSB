from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .canonical import (
    atomic_write,
    canonical_bytes,
    first_difference,
    sha256_bytes,
    sha256_file,
    sha256_json,
)
from .errors import PipelineError, error
from .kernel import compare_results, result_digest, serial_aggregate
from .locks import file_lock
from .manifest import Manifest, TaskSpec, load_and_validate_manifest
from .sandbox import execute_in_sandbox, sandbox_failure
from .store import (
    Store,
    build_cache_components,
    cache_identity,
    components_digest,
    run_id_for,
)


@dataclass
class TaskExecution:
    task: str
    ok: bool
    cache_hit: bool
    recovered: bool
    logical_start_ms: int
    logical_end_ms: int
    version: str | None = None
    cache_key: str | None = None
    result_digest: str | None = None
    error: PipelineError | None = None


@dataclass
class RuntimeTask:
    spec: TaskSpec
    order: int
    state: str = "pending"
    phase: str = "pending"
    outcome: str = "pending"
    reason: str | None = None
    logical_start_ms: int | None = None
    logical_end_ms: int | None = None
    cache_hit: bool = False
    version: str | None = None
    cache_key: str | None = None
    result_digest: str | None = None
    error: PipelineError | None = None
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    force_start: bool = False
    planned_cache_key: str | None = None


@dataclass
class Identity:
    task: TaskSpec
    input_paths: list[Path]
    input_digests: list[dict[str, Any]]
    dependencies: dict[str, dict[str, Any]]
    environment: dict[str, str]
    components: dict[str, Any]
    version: str
    cache_key: str
    permission_digest: str


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _same_digest(path: Path, expected: str | None) -> bool:
    if not expected or not path.is_file():
        return False
    try:
        return sha256_file(path) == expected
    except OSError:
        return False


def _parse_result(data: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _validated_environment(task: TaskSpec) -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in task.env_allowlist
        if key in os.environ
    }


class PipelineEngine:
    def __init__(
        self,
        *,
        workspace: Path,
        manifest_path: Path,
        state_db: Path,
        cache_dir: Path,
        clock_start_ms: int | None = None,
        fault_inject: str | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.manifest_path = manifest_path
        self.state_db = state_db
        self.cache_dir = cache_dir
        self.clock_start_ms = clock_start_ms
        self.fault_inject = fault_inject
        self.manifest = self._load_manifest()
        manifest_identity = (
            self.manifest.path.relative_to(self.workspace).as_posix()
            if self.manifest.path.is_relative_to(self.workspace)
            else str(self.manifest.path)
        )
        self.run_id = run_id_for(manifest_identity)

    def _load_manifest(self) -> Manifest:
        state_path = self.state_db
        if not state_path.is_absolute():
            state_path = self.workspace / state_path
        cache_path = self.cache_dir
        if not cache_path.is_absolute():
            cache_path = self.workspace / cache_path
        for label, path in (("state", state_path), ("cache", cache_path)):
            try:
                path.resolve(strict=False).relative_to(self.workspace)
            except ValueError:
                raise error(
                    "E_PATH_ESCAPE",
                    f"{label} path must be inside the workspace",
                    path=str(path),
                )
        self.state_db = state_path.resolve(strict=False)
        self.cache_dir = cache_path.resolve(strict=False)
        protected = [
            self.state_db,
            self.cache_dir,
            self.state_db.parent / "locks",
            self.manifest_path.resolve(strict=False),
        ]
        return load_and_validate_manifest(
            self.manifest_path,
            self.workspace,
            protected_paths=protected,
        )

    def _open_store(self) -> Store:
        store = Store(self.state_db, self.cache_dir)
        store.initialize()
        return store

    def _build_identity(
        self,
        store: Store,
        task: TaskSpec,
        dependency_rows: dict[str, sqlite3.Row],
    ) -> Identity:
        direct_paths = [
            (self.workspace / Path(*PurePosixPath(item).parts)).resolve(strict=False)
            for item in task.inputs
        ]
        input_paths = list(direct_paths)
        dependencies: dict[str, dict[str, Any]] = {}
        for name in task.depends_on:
            row = dependency_rows.get(name)
            if row is None or row["phase"] != "committed":
                raise error(
                    "E_DEPENDENCY_FAILED",
                    "dependency has no committed result",
                    task=task.name,
                    dependency=name,
                )
            output_path = str(row["output_path"])
            input_paths.append(
                (self.workspace / Path(*PurePosixPath(output_path).parts)).resolve(
                    strict=False
                )
            )
            dependencies[name] = {
                "cache_key": row["cache_key"],
                "result_digest": row["result_digest"],
                "version": row["version"],
            }
        def digest_paths(paths: list[Path]) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for path in paths:
                if not path.is_file():
                    raise error(
                        "E_INPUT_MISSING",
                        "task input file is missing",
                        task=task.name,
                        input=_relative(self.workspace, path),
                    )
                result.append(
                    {
                        "path": _relative(self.workspace, path),
                        "sha256": sha256_file(path),
                        "size": path.stat().st_size,
                    }
                )
            return result

        input_digests = digest_paths(direct_paths)
        all_input_digests = digest_paths(input_paths)
        for path in input_paths:
            if not path.is_file():
                raise error(
                    "E_INPUT_MISSING",
                    "task input file is missing",
                    task=task.name,
                    input=_relative(self.workspace, path),
                )
        environment = _validated_environment(task)
        components = build_cache_components(
            kernel_version=self.manifest.kernel_version,
            fixed_scale=self.manifest.fixed_scale,
            task=task,
            input_digests=input_digests,
            dependencies=dependencies,
            environment=environment,
        )
        version, cache_key = cache_identity(components)
        return Identity(
            task=task,
            input_paths=input_paths,
            input_digests=all_input_digests,
            dependencies=dependencies,
            environment=environment,
            components=components,
            version=version,
            cache_key=cache_key,
            permission_digest=sha256_json(task.permission_declaration),
        )

    def _planned_identities(self) -> dict[str, Identity]:
        planned: dict[str, Identity] = {}
        for task in self.manifest.topological_tasks:
            direct_paths = [
                (self.workspace / Path(*PurePosixPath(item).parts)).resolve(
                    strict=False
                )
                for item in task.inputs
            ]
            input_digests: list[dict[str, Any]] = []
            for path in direct_paths:
                if not path.is_file():
                    raise error(
                        "E_INPUT_MISSING",
                        "task input file is missing",
                        task=task.name,
                        input=_relative(self.workspace, path),
                    )
                input_digests.append(
                    {
                        "path": _relative(self.workspace, path),
                        "sha256": sha256_file(path),
                        "size": path.stat().st_size,
                    }
                )
            dependencies = {
                name: {
                    "version": planned[name].version,
                    "cache_key": planned[name].cache_key,
                }
                for name in task.depends_on
            }
            environment = _validated_environment(task)
            components = build_cache_components(
                kernel_version=self.manifest.kernel_version,
                fixed_scale=self.manifest.fixed_scale,
                task=task,
                input_digests=input_digests,
                dependencies=dependencies,
                environment=environment,
            )
            version, cache_key = cache_identity(components)
            planned[task.name] = Identity(
                task=task,
                input_paths=direct_paths,
                input_digests=input_digests,
                dependencies=dependencies,
                environment=environment,
                components=components,
                version=version,
                cache_key=cache_key,
                permission_digest=sha256_json(task.permission_declaration),
            )
        return planned

    def _invalidate_planned_changes(
        self, store: Store, planned: dict[str, Identity], watermark: int
    ) -> None:
        for task in self.manifest.topological_tasks:
            identity = planned[task.name]
            old = store.get_task(self.run_id, task.name)
            if old is None or not old["cache_key"] or old["cache_key"] == identity.cache_key:
                continue
            old_components = None
            if old["components_json"]:
                try:
                    old_components = json.loads(old["components_json"])
                except json.JSONDecodeError:
                    old_components = None
            store.invalidate_stale_cache(
                run_id=self.run_id,
                task_name=task.name,
                old_version=old["version"],
                old_cache_key=old["cache_key"],
                old_components=old_components,
                new_version=identity.version,
                new_cache_key=identity.cache_key,
                new_components=identity.components,
                watermark=watermark,
            )

    def _dependency_rows(
        self, store: Store, task: TaskSpec
    ) -> dict[str, sqlite3.Row]:
        rows: dict[str, sqlite3.Row] = {}
        for name in task.depends_on:
            row = store.get_task(self.run_id, name)
            if row is not None:
                rows[name] = row
        return rows

    def _cache_result(self, store: Store, cache_key: str) -> dict[str, Any] | None:
        entry = store.get_cache(cache_key)
        if entry is None or entry["state"] != "valid":
            return None
        path = store.cache_path(cache_key)
        if not _same_digest(path, entry["result_digest"]):
            store.mark_cache_invalid(cache_key, "cache_file_digest_mismatch")
            return None
        try:
            result = json.loads(entry["result_json"])
        except json.JSONDecodeError:
            store.mark_cache_invalid(cache_key, "cache_metadata_json_invalid")
            return None
        if sha256_json(result) != entry["result_digest"]:
            store.mark_cache_invalid(cache_key, "cache_result_digest_mismatch")
            return None
        return result

    def _assert_inputs_unchanged(self, identity: Identity) -> None:
        for item in identity.input_digests:
            path = self.workspace / Path(*PurePosixPath(item["path"]).parts)
            if not path.is_file():
                raise error(
                    "E_DIGEST_MISMATCH",
                    "input disappeared while the task was running",
                    task=identity.task.name,
                    input=item["path"],
                    expected=item["sha256"],
                    actual=None,
                )
            actual = sha256_file(path)
            if actual != item["sha256"]:
                raise error(
                    "E_DIGEST_MISMATCH",
                    "input changed while the task was running",
                    task=identity.task.name,
                    input=item["path"],
                    expected=item["sha256"],
                    actual=actual,
                )

    def _restore_output(self, store: Store, cache_key: str, output_path: Path) -> bool:
        cache_path = store.cache_path(cache_key)
        entry = store.get_cache(cache_key)
        if entry is None or not _same_digest(cache_path, entry["result_digest"]):
            return False
        data = cache_path.read_bytes() + b"\n"
        if output_path.is_file() and sha256_bytes(data) == sha256_file(output_path):
            return True
        atomic_write(output_path, data)
        return _same_digest(output_path, sha256_bytes(data))

    def _recover_committed(
        self,
        store: Store,
        row: sqlite3.Row,
        task: TaskSpec,
        *,
        cache_key: str,
        version: str,
    ) -> TaskExecution | None:
        if row["version"] != version or row["cache_key"] != cache_key:
            return None
        result = self._cache_result(store, cache_key)
        if result is None:
            return None
        output_path = self.workspace / Path(*PurePosixPath(task.output).parts)
        cache_path = store.cache_path(cache_key)
        output_digest = sha256_bytes(cache_path.read_bytes() + b"\n")
        if not _same_digest(output_path, row["output_digest"]):
            atomic_write(output_path, cache_path.read_bytes() + b"\n")
            if not _same_digest(output_path, output_digest):
                return None
        with store.transaction() as connection:
            connection.execute(
                "UPDATE task_states SET cache_hit=1, recovered=1, phase='committed', "
                "outcome='succeeded', reason='cache_reused', "
                "error_code=NULL, error_message=NULL "
                "WHERE run_id=? AND task_name=?",
                (self.run_id, task.name),
            )
        return TaskExecution(
            task=task.name,
            ok=True,
            cache_hit=True,
            recovered=True,
            logical_start_ms=int(row["logical_start_ms"] or 0),
            logical_end_ms=int(row["logical_end_ms"] or 0),
            version=version,
            cache_key=cache_key,
            result_digest=row["result_digest"],
        )

    def _recover_prepared(
        self,
        store: Store,
        row: sqlite3.Row,
        task: TaskSpec,
        *,
        cache_key: str,
        version: str,
    ) -> TaskExecution | None:
        if row["version"] != version or row["cache_key"] != cache_key:
            return None
        expected_result = row["result_digest"]
        expected_output = row["output_digest"]
        if not expected_result or not expected_output:
            return None
        output_path = self.workspace / Path(*PurePosixPath(str(row["output_path"])).parts)
        staging_path = (
            self.workspace / Path(*PurePosixPath(str(row["staging_path"])).parts)
            if row["staging_path"]
            else None
        )
        cache_path = store.cache_path(cache_key)
        source_bytes: bytes | None = None
        if output_path.is_file() and sha256_file(output_path) == expected_output:
            source_bytes = output_path.read_bytes()
        elif staging_path is not None and staging_path.is_file() and (
            sha256_file(staging_path) == expected_output
        ):
            source_bytes = staging_path.read_bytes()
        elif _same_digest(cache_path, expected_result):
            source_bytes = cache_path.read_bytes() + b"\n"
        if source_bytes is None:
            return None
        result = _parse_result(source_bytes)
        if result is None or result_digest(result) != expected_result:
            return None
        atomic_write(cache_path, canonical_bytes(result))
        if not output_path.is_file() or sha256_file(output_path) != expected_output:
            atomic_write(output_path, source_bytes)
        if sha256_file(output_path) != expected_output:
            return None
        store.commit_task(
            run_id=self.run_id,
            task_name=task.name,
            version=version,
            cache_key=cache_key,
            cache_path=cache_path,
            output_path=task.output,
            output_digest=expected_output,
            result_digest=expected_result,
            result=result,
            cache_hit=False,
            recovered=True,
            stdout=str(row["stdout"] or ""),
            stderr=str(row["stderr"] or ""),
            watermark=int(row["updated_watermark"]),
        )
        return TaskExecution(
            task=task.name,
            ok=True,
            cache_hit=False,
            recovered=True,
            logical_start_ms=int(row["logical_start_ms"] or 0),
            logical_end_ms=int(row["logical_end_ms"] or 0),
            version=version,
            cache_key=cache_key,
            result_digest=expected_result,
        )

    def _execute_task(
        self,
        task: TaskSpec,
        *,
        logical_start_ms: int,
        logical_end_ms: int,
        expected_cache_key: str | None = None,
    ) -> TaskExecution:
        store = self._open_store()
        try:
            dependency_rows = self._dependency_rows(store, task)
            identity = self._build_identity(store, task, dependency_rows)
            if expected_cache_key is not None and identity.cache_key != expected_cache_key:
                raise error(
                    "E_DIGEST_MISMATCH",
                    "task inputs changed after the run snapshot was planned",
                    task=task.name,
                    expected_cache_key=expected_cache_key,
                    actual_cache_key=identity.cache_key,
                )
            with file_lock(store.lock_path(identity.cache_key)):
                old = store.get_task(self.run_id, task.name)
                if old is not None:
                    old_components = None
                    if old["components_json"]:
                        try:
                            old_components = json.loads(old["components_json"])
                        except json.JSONDecodeError:
                            old_components = None
                    store.invalidate_stale_cache(
                        run_id=self.run_id,
                        task_name=task.name,
                        old_version=old["version"],
                        old_cache_key=old["cache_key"],
                        old_components=old_components,
                        new_version=identity.version,
                        new_cache_key=identity.cache_key,
                        new_components=identity.components,
                        watermark=logical_start_ms,
                    )
                    if old["phase"] == "committed":
                        recovered = self._recover_committed(
                            store,
                            old,
                            task,
                            cache_key=identity.cache_key,
                            version=identity.version,
                        )
                        if recovered is not None:
                            return recovered
                    elif old["phase"] == "prepared":
                        recovered = self._recover_prepared(
                            store,
                            old,
                            task,
                            cache_key=identity.cache_key,
                            version=identity.version,
                        )
                        if recovered is not None:
                            return recovered

                cached = self._cache_result(store, identity.cache_key)
                if cached is not None:
                    store.mark_task_running(
                        run_id=self.run_id,
                        task_name=task.name,
                        version=identity.version,
                        cache_key=identity.cache_key,
                        permission_digest=identity.permission_digest,
                        components=identity.components,
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_start_ms,
                        output_path=task.output,
                        environment=identity.environment,
                        watermark=logical_start_ms,
                    )
                    output_path = self.workspace / Path(*PurePosixPath(task.output).parts)
                    if not self._restore_output(store, identity.cache_key, output_path):
                        store.mark_cache_invalid(identity.cache_key, "output_restore_failed")
                        self._fault("cache")
                    else:
                        store.commit_task(
                            run_id=self.run_id,
                            task_name=task.name,
                            version=identity.version,
                            cache_key=identity.cache_key,
                            cache_path=store.cache_path(identity.cache_key),
                            output_path=task.output,
                            output_digest=sha256_file(output_path),
                            result_digest=sha256_json(cached),
                            result=cached,
                            cache_hit=True,
                            recovered=False,
                            stdout="",
                            stderr="",
                            watermark=logical_start_ms,
                        )
                        return TaskExecution(
                            task=task.name,
                            ok=True,
                            cache_hit=True,
                            recovered=False,
                            logical_start_ms=logical_start_ms,
                            logical_end_ms=logical_start_ms,
                            version=identity.version,
                            cache_key=identity.cache_key,
                            result_digest=sha256_json(cached),
                        )

                store.mark_task_running(
                    run_id=self.run_id,
                    task_name=task.name,
                    version=identity.version,
                    cache_key=identity.cache_key,
                    permission_digest=identity.permission_digest,
                    components=identity.components,
                    logical_start_ms=logical_start_ms,
                    logical_end_ms=logical_end_ms,
                    output_path=task.output,
                    environment=identity.environment,
                    watermark=logical_end_ms,
                )
                self._fault("running", task.name)
                workdir = self.workspace / Path(*PurePosixPath(task.workdir).parts)
                output_path = self.workspace / Path(*PurePosixPath(task.output).parts)
                staging_path = workdir / f".{task.name}.staging.json"
                envelope_path = workdir / f".{task.name}.envelope.json"
                read_scopes = [
                    (self.workspace / Path(*PurePosixPath(item).parts)).resolve(
                        strict=False
                    )
                    for item in task.reads
                ]
                read_scopes.extend(identity.input_paths)
                write_scopes = [
                    (self.workspace / Path(*PurePosixPath(item).parts)).resolve(
                        strict=False
                    )
                    for item in task.writes
                ]
                if task.operation in ("probe_read", "probe_write"):
                    probe = execute_in_sandbox(
                        root=self.workspace,
                        task=task.as_dict(),
                        input_paths=identity.input_paths,
                        read_scopes=read_scopes,
                        write_scopes=write_scopes,
                        workdir=workdir,
                        output_path=output_path,
                        staging_path=staging_path,
                        envelope_path=envelope_path,
                        environment=identity.environment,
                        fixed_scale=self.manifest.fixed_scale,
                        kernel_version=self.manifest.kernel_version,
                        timeout_ms=task.timeout_ms,
                    )
                    if probe.ok:
                        failure = error(
                            "E_SANDBOX_UNAVAILABLE",
                            "sandbox probe unexpectedly succeeded",
                            task=task.name,
                        )
                    else:
                        failure = sandbox_failure(probe, task_name=task.name)
                    store.mark_task_status(
                        run_id=self.run_id,
                        task_name=task.name,
                        phase="failed",
                        outcome="failed",
                        reason=f"started_failure:{failure.code}",
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        latency_ms=logical_end_ms - logical_start_ms,
                        error_code=failure.code,
                        error_message=failure.message,
                        watermark=logical_end_ms,
                    )
                    return TaskExecution(
                        task=task.name,
                        ok=False,
                        cache_hit=False,
                        recovered=False,
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        error=failure,
                    )
                try:
                    serial_result = serial_aggregate(
                        task,
                        identity.input_paths,
                        self.workspace,
                        fixed_scale=self.manifest.fixed_scale,
                        environment=identity.environment,
                        kernel_version=self.manifest.kernel_version,
                    )
                except PipelineError as exc:
                    store.mark_task_status(
                        run_id=self.run_id,
                        task_name=task.name,
                        phase="failed",
                        outcome="failed",
                        reason=f"serial_compute_failed:{exc.code}",
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        latency_ms=logical_end_ms - logical_start_ms,
                        error_code=exc.code,
                        error_message=exc.message,
                        watermark=logical_end_ms,
                    )
                    return TaskExecution(
                        task=task.name,
                        ok=False,
                        cache_hit=False,
                        recovered=False,
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        error=exc,
                    )

                try:
                    self._assert_inputs_unchanged(identity)
                except PipelineError as exc:
                    store.mark_task_status(
                        run_id=self.run_id,
                        task_name=task.name,
                        phase="failed",
                        outcome="failed",
                        reason=f"input_changed:{exc.code}",
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        latency_ms=logical_end_ms - logical_start_ms,
                        error_code=exc.code,
                        error_message=exc.message,
                        watermark=logical_end_ms,
                    )
                    return TaskExecution(
                        task=task.name,
                        ok=False,
                        cache_hit=False,
                        recovered=False,
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        error=exc,
                    )

                sandbox = execute_in_sandbox(
                    root=self.workspace,
                    task=task.as_dict(),
                    input_paths=identity.input_paths,
                    read_scopes=read_scopes,
                    write_scopes=write_scopes,
                    workdir=workdir,
                    output_path=output_path,
                    staging_path=staging_path,
                    envelope_path=envelope_path,
                    environment=identity.environment,
                    fixed_scale=self.manifest.fixed_scale,
                    kernel_version=self.manifest.kernel_version,
                    timeout_ms=task.timeout_ms,
                )
                if not sandbox.ok:
                    failure = sandbox_failure(sandbox, task_name=task.name)
                    store.mark_task_status(
                        run_id=self.run_id,
                        task_name=task.name,
                        phase="failed",
                        outcome="failed",
                        reason=f"started_failure:{failure.code}",
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        latency_ms=logical_end_ms - logical_start_ms,
                        error_code=failure.code,
                        error_message=failure.message,
                        watermark=logical_end_ms,
                    )
                    return TaskExecution(
                        task=task.name,
                        ok=False,
                        cache_hit=False,
                        recovered=False,
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        error=failure,
                    )

                try:
                    self._assert_inputs_unchanged(identity)
                    staging_bytes = staging_path.read_bytes()
                    sandbox_result = _parse_result(staging_bytes)
                    if sandbox_result is None:
                        raise error(
                            "E_DIGEST_MISMATCH",
                            "sandbox result is not a JSON object",
                            task=task.name,
                        )
                    envelope = sandbox.envelope or {}
                    if envelope.get("result_digest") != result_digest(sandbox_result):
                        raise error(
                            "E_DIGEST_MISMATCH",
                            "sandbox envelope digest does not match result",
                            task=task.name,
                            expected=envelope.get("result_digest"),
                            actual=result_digest(sandbox_result),
                        )
                    compare_results(
                        serial_result,
                        sandbox_result,
                        task=task.name,
                        dependencies=task.depends_on,
                    )
                    expected_staging = canonical_bytes(serial_result) + b"\n"
                    if staging_bytes != expected_staging:
                        difference = first_difference(serial_result, sandbox_result)
                        raise error(
                            "E_EXECUTION_DIVERGENCE",
                            "serial and sandbox byte streams differ",
                            task=task.name,
                            first_divergent_field=(difference or {}).get("field"),
                            serial_value=(difference or {}).get("serial"),
                            sandbox_value=(difference or {}).get("sandbox"),
                            dependency_sources={
                                "depends_on": list(task.depends_on),
                                "inputs": [
                                    item["path"] for item in identity.input_digests
                                ],
                            },
                        )
                except PipelineError as exc:
                    store.mark_task_status(
                        run_id=self.run_id,
                        task_name=task.name,
                        phase="failed",
                        outcome="failed",
                        reason=f"consistency_rejected:{exc.code}",
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        latency_ms=logical_end_ms - logical_start_ms,
                        error_code=exc.code,
                        error_message=exc.message,
                        watermark=logical_end_ms,
                    )
                    return TaskExecution(
                        task=task.name,
                        ok=False,
                        cache_hit=False,
                        recovered=False,
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        error=exc,
                    )

                result_sha = result_digest(serial_result)
                output_sha = sha256_file(staging_path)
                atomic_write(store.cache_path(identity.cache_key), canonical_bytes(serial_result))
                store.prepare_commit(
                    run_id=self.run_id,
                    task_name=task.name,
                    output_path=task.output,
                    staging_path=_relative(self.workspace, staging_path),
                    output_digest=output_sha,
                    result_digest=result_sha,
                    stdout=sandbox.stdout,
                    stderr=sandbox.stderr,
                    watermark=logical_end_ms,
                )
                self._fault("prepared", task.name)
                published = staging_path.read_bytes()
                if sha256_bytes(published) != output_sha:
                    store.mark_task_status(
                        run_id=self.run_id,
                        task_name=task.name,
                        phase="failed",
                        outcome="failed",
                        reason="digest_mismatch_before_publish",
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        latency_ms=logical_end_ms - logical_start_ms,
                        error_code="E_DIGEST_MISMATCH",
                        error_message="staging file changed before publish",
                        watermark=logical_end_ms,
                    )
                    return TaskExecution(
                        task=task.name,
                        ok=False,
                        cache_hit=False,
                        recovered=False,
                        logical_start_ms=logical_start_ms,
                        logical_end_ms=logical_end_ms,
                        error=error("E_DIGEST_MISMATCH", "staging file changed"),
                    )
                atomic_write(output_path, published)
                if sha256_file(output_path) != output_sha:
                    raise error(
                        "E_DIGEST_MISMATCH",
                        "published output digest mismatch",
                        task=task.name,
                    )
                store.commit_task(
                    run_id=self.run_id,
                    task_name=task.name,
                    version=identity.version,
                    cache_key=identity.cache_key,
                    cache_path=store.cache_path(identity.cache_key),
                    output_path=task.output,
                    output_digest=output_sha,
                    result_digest=result_sha,
                    result=serial_result,
                    cache_hit=False,
                    recovered=False,
                    stdout=sandbox.stdout,
                    stderr=sandbox.stderr,
                    watermark=logical_end_ms,
                )
                self._fault("committed", task.name)
                return TaskExecution(
                    task=task.name,
                    ok=True,
                    cache_hit=False,
                    recovered=False,
                    logical_start_ms=logical_start_ms,
                    logical_end_ms=logical_end_ms,
                    version=identity.version,
                    cache_key=identity.cache_key,
                    result_digest=result_sha,
                )
        except PipelineError as exc:
            try:
                store.mark_task_status(
                    run_id=self.run_id,
                    task_name=task.name,
                    phase="failed",
                    outcome="failed",
                    reason=f"started_failure:{exc.code}",
                    logical_start_ms=logical_start_ms,
                    logical_end_ms=logical_end_ms,
                    latency_ms=logical_end_ms - logical_start_ms,
                    error_code=exc.code,
                    error_message=exc.message,
                    watermark=logical_end_ms,
                )
            except PipelineError:
                pass
            return TaskExecution(
                task=task.name,
                ok=False,
                cache_hit=False,
                recovered=False,
                logical_start_ms=logical_start_ms,
                logical_end_ms=logical_end_ms,
                error=exc,
            )
        except BaseException as exc:
            internal = error(
                "E_INTERNAL",
                f"task execution failed unexpectedly: {exc}",
                task=task.name,
            )
            try:
                store.mark_task_status(
                    run_id=self.run_id,
                    task_name=task.name,
                    phase="failed",
                    outcome="failed",
                    reason="started_failure:E_INTERNAL",
                    logical_start_ms=logical_start_ms,
                    logical_end_ms=logical_end_ms,
                    latency_ms=logical_end_ms - logical_start_ms,
                    error_code="E_INTERNAL",
                    error_message=str(exc),
                    watermark=logical_end_ms,
                )
            except PipelineError:
                pass
            return TaskExecution(
                task=task.name,
                ok=False,
                cache_hit=False,
                recovered=False,
                logical_start_ms=logical_start_ms,
                logical_end_ms=logical_end_ms,
                error=internal,
            )
        finally:
            store.close()

    def _fault(self, phase: str, task_name: str | None = None) -> None:
        if not self.fault_inject:
            return
        wanted_phase, _, wanted_task = self.fault_inject.partition(":")
        if wanted_phase != phase:
            return
        if wanted_task and wanted_task != task_name:
            return
        os._exit(86)

    def _precheck_committed(
        self,
        store: Store,
        runtime: RuntimeTask,
        dependency_rows: dict[str, sqlite3.Row],
    ) -> bool:
        try:
            identity = self._build_identity(store, runtime.spec, dependency_rows)
        except PipelineError:
            return False
        row = store.get_task(self.run_id, runtime.spec.name)
        if row is None:
            return False
        if row["version"] != identity.version or row["cache_key"] != identity.cache_key:
            return False
        if row["phase"] == "prepared":
            recovered = self._recover_prepared(
                store,
                row,
                runtime.spec,
                cache_key=identity.cache_key,
                version=identity.version,
            )
            if recovered is not None:
                runtime.state = "succeeded"
                runtime.phase = "committed"
                runtime.outcome = "succeeded"
                runtime.reason = "recovered_half_commit"
                runtime.version = identity.version
                runtime.cache_key = identity.cache_key
                runtime.result_digest = recovered.result_digest
                runtime.logical_start_ms = recovered.logical_start_ms
                runtime.logical_end_ms = recovered.logical_end_ms
                return True
            runtime.force_start = True
            return False
        if row["phase"] == "running":
            runtime.force_start = True
            return False
        if row["phase"] != "committed":
            return False
        output_path = self.workspace / Path(*PurePosixPath(runtime.spec.output).parts)
        if not _same_digest(output_path, row["output_digest"]):
            return False
        if not _same_digest(store.cache_path(identity.cache_key), row["result_digest"]):
            return False
        with store.transaction() as connection:
            connection.execute(
                "UPDATE task_states SET cache_hit=1, phase='committed', "
                "outcome='succeeded', reason='cache_reused', "
                "error_code=NULL, error_message=NULL "
                "WHERE run_id=? AND task_name=?",
                (self.run_id, runtime.spec.name),
            )
        runtime.state = "succeeded"
        runtime.phase = "committed"
        runtime.outcome = "succeeded"
        runtime.reason = "cache_reused"
        runtime.cache_hit = True
        runtime.version = identity.version
        runtime.cache_key = identity.cache_key
        runtime.result_digest = row["result_digest"]
        runtime.logical_start_ms = int(row["logical_start_ms"] or 0)
        runtime.logical_end_ms = int(row["logical_start_ms"] or 0)
        return True

    def execute(self, *, resume: bool) -> dict[str, Any]:
        db_exists = self.state_db.exists()
        if resume and not db_exists:
            raise error(
                "E_STATE_NOT_FOUND",
                "resume requires an existing SQLite state database",
                state_db=str(self.state_db),
            )
        store = self._open_store()
        try:
            existing_run = store.get_run(self.run_id)
            if resume and existing_run is None:
                raise error(
                    "E_STATE_NOT_FOUND",
                    "resume requires persisted state for this manifest",
                    run_id=self.run_id,
                )
            origin = (
                self.clock_start_ms
                if self.clock_start_ms is not None
                else (
                    int(existing_run["clock_start_ms"])
                    if existing_run is not None
                    else self.manifest.clock_start_ms
                )
            )
            watermark = (
                int(existing_run["logical_watermark"]) if existing_run is not None else origin
            )
            if self.clock_start_ms is not None and self.clock_start_ms < watermark:
                raise error(
                    "E_CLOCK_REGRESSION",
                    "requested logical clock is behind persisted watermark",
                    previous=watermark,
                    requested=self.clock_start_ms,
                )
            run_id = store.ensure_run(
                run_id=self.run_id,
                manifest_path=self.manifest.path.relative_to(self.workspace).as_posix()
                if self.manifest.path.is_relative_to(self.workspace)
                else str(self.manifest.path),
                manifest_digest=self.manifest.raw_digest,
                latency_budget_ms=self.manifest.latency_budget_ms,
                clock_start_ms=origin,
            )
            if run_id != self.run_id:
                raise error("E_INTERNAL", "run id mismatch")
            deadline = origin + self.manifest.latency_budget_ms
            tasks = list(self.manifest.topological_tasks)
            runtimes = {
                task.name: RuntimeTask(spec=task, order=index)
                for index, task in enumerate(tasks)
            }
            by_name = {task.name: task for task in tasks}
            clock = max(origin, watermark)
            planned = self._planned_identities()
            self._invalidate_planned_changes(store, planned, clock)
            for name, runtime in runtimes.items():
                runtime.planned_cache_key = planned[name].cache_key
            active: dict[Any, RuntimeTask] = {}
            executor = ThreadPoolExecutor(max_workers=self.manifest.max_parallel)

            def refresh_dependencies() -> None:
                nonlocal clock
                changed = True
                while changed:
                    changed = False
                    for task in tasks:
                        runtime = runtimes[task.name]
                        if runtime.state != "pending":
                            continue
                        dep_states = [runtimes[name].state for name in task.depends_on]
                        if any(state == "failed" for state in dep_states):
                            failed = next(
                                name
                                for name in task.depends_on
                                if runtimes[name].state == "failed"
                            )
                            runtime.state = "abandoned"
                            runtime.phase = "abandoned"
                            runtime.outcome = "abandoned"
                            runtime.reason = f"dependency_failed:{failed}"
                            self._persist_runtime_status(store, runtime, clock)
                            changed = True
                            continue
                        if any(state == "abandoned" for state in dep_states):
                            failed = next(
                                name
                                for name in task.depends_on
                                if runtimes[name].state == "abandoned"
                            )
                            runtime.state = "abandoned"
                            runtime.phase = "abandoned"
                            runtime.outcome = "abandoned"
                            runtime.reason = f"dependency_abandoned:{failed}"
                            self._persist_runtime_status(store, runtime, clock)
                            changed = True
                            continue
                        if all(state == "succeeded" for state in dep_states):
                            rows = self._dependency_rows(store, task)
                            if self._precheck_committed(store, runtime, rows):
                                changed = True

            def ready_tasks() -> list[RuntimeTask]:
                ready = []
                for task in tasks:
                    runtime = runtimes[task.name]
                    if runtime.state != "pending":
                        continue
                    if all(
                        runtimes[name].state == "succeeded" for name in task.depends_on
                    ):
                        ready.append(runtime)
                return ready

            def launch(runtime: RuntimeTask) -> Any:
                runtime.state = "running"
                runtime.phase = "running"
                runtime.outcome = "running"
                runtime.logical_start_ms = clock
                runtime.logical_end_ms = clock + runtime.spec.logical_cost_ms
                future = executor.submit(
                    self._execute_task,
                    runtime.spec,
                    logical_start_ms=runtime.logical_start_ms,
                    logical_end_ms=runtime.logical_end_ms,
                    expected_cache_key=runtime.planned_cache_key,
                )
                active[future] = runtime
                return future

            while True:
                refresh_dependencies()
                if active:
                    while len(active) < self.manifest.max_parallel:
                        ready = [
                            item
                            for item in ready_tasks()
                            if clock < deadline or item.force_start
                        ]
                        if not ready:
                            break
                        runtime = ready[0]
                        launch(runtime)
                    if not active:
                        break
                    future = min(
                        active,
                        key=lambda item: (
                            active[item].logical_end_ms or 0,
                            active[item].order,
                        ),
                    )
                    runtime = active.pop(future)
                    result = future.result()
                    clock = max(
                        clock,
                        runtime.logical_end_ms or clock,
                    )
                    if result.ok:
                        runtime.state = "succeeded"
                        runtime.phase = "committed"
                        runtime.outcome = "succeeded"
                        runtime.reason = (
                            "cache_reused"
                            if result.cache_hit
                            else (
                                "recovered_half_commit"
                                if result.recovered
                                else None
                            )
                        )
                        runtime.cache_hit = result.cache_hit
                        runtime.version = result.version
                        runtime.cache_key = result.cache_key
                        runtime.result_digest = result.result_digest
                    else:
                        runtime.state = "failed"
                        runtime.phase = "failed"
                        runtime.outcome = "failed"
                        runtime.reason = (
                            f"started_failure:{result.error.code}"
                            if result.error
                            else "started_failure"
                        )
                        runtime.error = result.error
                    continue

                ready = ready_tasks()
                if not ready:
                    break
                if clock >= deadline:
                    forced = [item for item in ready if item.force_start]
                    if forced:
                        for runtime in forced[: self.manifest.max_parallel]:
                            launch(runtime)
                        continue
                    for task in tasks:
                        runtime = runtimes[task.name]
                        if runtime.state == "pending":
                            runtime.state = "not_started"
                            runtime.phase = "not_started"
                            runtime.outcome = "not_started_budget"
                            runtime.reason = "latency_budget_exhausted"
                            self._persist_runtime_status(store, runtime, clock)
                    break
                for runtime in ready[: self.manifest.max_parallel]:
                    launch(runtime)

            executor.shutdown(wait=True)
            final_watermark = max(clock, store.get_watermark(self.run_id))
            rows = store.tasks_for_run(self.run_id)
            outcomes = {str(row["task_name"]): str(row["outcome"]) for row in rows}
            if all(value == "succeeded" for value in outcomes.values()) and len(
                outcomes
            ) == len(tasks):
                final_status = "success"
            elif any(value == "failed" for value in outcomes.values()):
                final_status = "failed"
            else:
                final_status = "partial"
            store.finish_run(self.run_id, final_status, final_watermark)
            return self.build_status(store, run_id, include_invalidations=True)
        finally:
            store.close()

    def _persist_runtime_status(
        self, store: Store, runtime: RuntimeTask, watermark: int
    ) -> None:
        try:
            store.mark_task_status(
                run_id=self.run_id,
                task_name=runtime.spec.name,
                phase=runtime.phase,
                outcome=runtime.outcome,
                reason=runtime.reason or "",
                logical_start_ms=runtime.logical_start_ms,
                logical_end_ms=runtime.logical_end_ms,
                latency_ms=(
                    runtime.logical_end_ms - runtime.logical_start_ms
                    if runtime.logical_start_ms is not None
                    and runtime.logical_end_ms is not None
                    else None
                ),
                error_code=runtime.error.code if runtime.error else None,
                error_message=runtime.error.message if runtime.error else None,
                watermark=watermark,
            )
        except PipelineError:
            pass

    def build_status(
        self, store: Store, run_id: str, *, include_invalidations: bool
    ) -> dict[str, Any]:
        run = store.get_run(run_id)
        rows = store.tasks_for_run(run_id)
        tasks = []
        for row in rows:
            tasks.append(
                {
                    "task": row["task_name"],
                    "phase": row["phase"],
                    "outcome": row["outcome"],
                    "reason": row["reason"],
                    "version": row["version"],
                    "cache_key": row["cache_key"],
                    "cache_hit": bool(row["cache_hit"]),
                    "recovered": bool(row["recovered"]),
                    "logical_start_ms": row["logical_start_ms"],
                    "logical_end_ms": row["logical_end_ms"],
                    "latency_observed_ms": row["latency_ms"],
                    "latency_source": "injected_logical_clock",
                    "output_path": row["output_path"],
                    "output_digest": row["output_digest"],
                    "result_digest": row["result_digest"],
                    "error_code": row["error_code"],
                    "error_message": row["error_message"],
                }
            )
        result: dict[str, Any] = {
            "schema": "pipeline-status-v1",
            "run_id": run_id,
            "manifest_digest": run["manifest_digest"] if run else self.manifest.raw_digest,
            "pipeline_status": run["status"] if run else "unknown",
            "max_parallel": self.manifest.max_parallel,
            "latency_budget_ms": self.manifest.latency_budget_ms,
            "logical_clock_watermark": int(run["logical_watermark"]) if run else 0,
            "tasks": tasks,
        }
        if include_invalidations:
            result["cache_invalidations"] = store.invalidations(run_id)
        return result

    def status(self) -> dict[str, Any]:
        if not self.state_db.exists():
            raise error(
                "E_STATE_NOT_FOUND",
                "state database does not exist",
                state_db=str(self.state_db),
            )
        store = Store(self.state_db, self.cache_dir)
        store.initialize_readonly()
        try:
            run = store.get_run(self.run_id)
            if run is None:
                raise error(
                    "E_STATE_NOT_FOUND",
                    "manifest has no persisted state",
                    run_id=self.run_id,
                )
            return self.build_status(store, self.run_id, include_invalidations=True)
        finally:
            store.close()


def run_summary(status: dict[str, Any]) -> dict[str, Any]:
    tasks = status["tasks"]
    failures = [
        {
            "task": task["task"],
            "code": task["error_code"],
            "message": task["error_message"],
        }
        for task in tasks
        if task["error_code"] is not None
    ]
    return {
        "schema": "pipeline-run-v1",
        "run_id": status["run_id"],
        "manifest_digest": status["manifest_digest"],
        "outcome": status["pipeline_status"],
        "logical_clock_watermark": status["logical_clock_watermark"],
        "latency_budget_ms": status["latency_budget_ms"],
        "max_parallel": status["max_parallel"],
        "tasks": tasks,
        "cache_invalidations": status.get("cache_invalidations", []),
        "errors": failures,
    }
