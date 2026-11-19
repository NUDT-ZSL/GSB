from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .canonical import LogicalClock, key_token
from .crash import CrashInjector
from .errors import fail
from .files import JsonlSpool, digest_file, fsync_directory
from .generator import generate_table_rows
from .locking import exclusive_file_lock
from .models import TableSpec
from .partitioning import partition_for_key
from .storage import StateStore


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_dead_temps(directory: Path) -> None:
    if not directory.exists():
        return
    for path in directory.glob(".tmp-*"):
        pieces = path.name.split("-", 2)
        if len(pieces) < 2:
            continue
        try:
            pid = int(pieces[1])
        except ValueError:
            continue
        if not _pid_alive(pid):
            path.unlink(missing_ok=True)


def committed_file_valid(state: StateStore, commit: dict[str, Any]) -> bool:
    path = state.path_for(commit["relative_path"])
    if not path.exists():
        return False
    digest, size = digest_file(path)
    return digest == commit["digest"] and size == int(commit["bytes"])


def shuffle_side(
    state: StateStore,
    *,
    query_id: str,
    version: int,
    side: str,
    table: TableSpec,
    seed: int,
    rows_override: int,
    partitions: int,
    input_digest: str,
    partition_filter: set[int] | None,
    crash: CrashInjector,
    clock: LogicalClock,
) -> list[dict[str, Any]]:
    selected = sorted(partition_filter) if partition_filter is not None else list(range(partitions))
    existing = {
        int(row["partition_id"]): row
        for row in state.get_partition_commits(query_id, version, side)
    }
    missing: list[int] = []
    for partition_id in selected:
        commit = existing.get(partition_id)
        if commit is None:
            missing.append(partition_id)
            continue
        if not committed_file_valid(state, commit):
            fail(
                "E_STATE_CORRUPT",
                "committed shuffle partition file is missing or corrupt",
                details={
                    "side": side,
                    "partition_id": partition_id,
                    "path": commit["relative_path"],
                },
                exit_code=3,
            )
    if not missing:
        return [existing[p] for p in selected]

    directory = state.root / "data" / f"v{version}" / "shuffle" / side
    directory.mkdir(parents=True, exist_ok=True)
    spools: dict[int, JsonlSpool] = {}
    for partition_id in missing:
        final_path = directory / f"part-{partition_id:05d}.jsonl"
        spools[partition_id] = JsonlSpool(final_path, label=f"{side}:{partition_id}").open()
    rows_seen = 0
    last_clock = clock.current
    try:
        for row in generate_table_rows(table, seed, rows_override=rows_override):
            rows_seen += 1
            clock.tick(1)
            key = row["values"][table.join_key]
            partition_id = partition_for_key(key, partitions)
            spool = spools.get(partition_id)
            if spool is None:
                continue
            spool.write(row, key_token_value=key_token(key))
            crash.maybe(
                f"shuffle_{side}_during",
                rows_seen,
                flush=spool.flush,
            )
        for spool in spools.values():
            spool.close()
    except BaseException:
        for spool in spools.values():
            spool.discard()
        raise

    newly_committed = 0
    for partition_id in missing:
        spool = spools[partition_id]
        lock_path = state.partition_lock_path(
            query_id, version, "partition", partition_id, side=side
        )
        with exclusive_file_lock(lock_path):
            current = state.get_partition_commit(query_id, version, side, partition_id)
            if current is not None:
                if not committed_file_valid(state, current):
                    fail(
                        "E_STATE_CORRUPT",
                        "partition commit exists but its file is invalid",
                        details={"side": side, "partition_id": partition_id},
                        exit_code=3,
                    )
                spool.discard()
                existing[partition_id] = current
                continue
            digest, size, row_count, key_digest = spool.commit()
            clock.tick(1)
            relative_path = state.relative(spool.final_path)
            clock_cost = clock.current - last_clock
            last_clock = clock.current
            state.commit_partition(
                query_id,
                version,
                side,
                partition_id,
                relative_path=relative_path,
                rows=row_count,
                size_bytes=size,
                digest=digest,
                key_digest=key_digest,
                input_digest=input_digest,
                clock_cost=clock_cost,
                logical_clock=clock.current,
            )
            newly_committed += 1
            commit = state.get_partition_commit(query_id, version, side, partition_id)
            assert commit is not None
            existing[partition_id] = commit
            crash.maybe(f"shuffle_{side}_after", newly_committed)
    cleanup_dead_temps(directory)
    fsync_directory(directory)
    return [existing[p] for p in selected]


def shuffle_side_local(
    *,
    table: TableSpec,
    seed: int,
    rows_override: int,
    partitions: int,
    directory: Path,
    clock: LogicalClock,
) -> list[dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    spools = {
        partition_id: JsonlSpool(
            directory / f"part-{partition_id:05d}.jsonl",
            label=f"local-{table.name}:{partition_id}",
        ).open()
        for partition_id in range(partitions)
    }
    for row in generate_table_rows(table, seed, rows_override=rows_override):
        clock.tick(1)
        key = row["values"][table.join_key]
        partition_id = partition_for_key(key, partitions)
        spools[partition_id].write(row, key_token_value=key_token(key))
    commits: list[dict[str, Any]] = []
    for partition_id in range(partitions):
        spool = spools[partition_id]
        digest, size, row_count, key_digest = spool.commit()
        clock.tick(1)
        commits.append(
            {
                "partition_id": partition_id,
                "relative_path": str(spool.final_path),
                "rows": row_count,
                "bytes": size,
                "digest": digest,
                "key_digest": key_digest,
            }
        )
    cleanup_dead_temps(directory)
    return commits

