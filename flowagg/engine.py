from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import FlowAggError, fail
from .locking import FileLock
from .memory import ActiveGroupTable, MemoryStats
from .plan import AggregationPlan
from .quantiles import quantiles_for_group
from .records import BatchData, FlowRecord, read_batch, read_batch_manifest
from .spill import (
    SpillRecord,
    SpillWriter,
    canonical_json,
    maybe_crash,
    read_spill_file,
)
from .state import GroupSpec, GroupState, StateVector, group_public_view
from .store import Store


class TruthOrderSource:
    def __init__(self, values: dict[str, list[int]]) -> None:
        self.values = values

    def group_quantile_order_value(self, group_id: str, field: str, offset: int) -> int:
        return self.values[field][offset]


class Engine:
    def __init__(
        self,
        plan: AggregationPlan,
        db_path: str | Path,
        *,
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        self.plan = plan
        self.db_path = Path(db_path).resolve()
        self.lock_timeout_seconds = lock_timeout_seconds
        self.store: Store | None = None
        self.active: ActiveGroupTable | None = None
        self.writer: SpillWriter | None = None
        self.touched: dict[str, GroupState] = {}
        self.record_groups: list[tuple[str, FlowRecord]] = []
        self.spill_activity = False
        self.existing_spills = False

    @property
    def lock_path(self) -> Path:
        return self.db_path.with_name(self.db_path.name + ".lock")

    @contextmanager
    def session(self) -> Iterator["Engine"]:
        with FileLock(self.lock_path, timeout_seconds=self.lock_timeout_seconds):
            store = Store(self.db_path, self.plan)
            store.open()
            try:
                store.initialize()
                self.store = store
                yield self
            finally:
                self.store = None
                store.close()

    def load(self) -> dict[str, Any]:
        with self.session():
            assert self.store is not None
            return {
                "command": "load",
                "ok": True,
                "plan": self.plan.as_output(),
                "status": self.store.status(),
            }

    def _reset_batch_runtime(self) -> None:
        assert self.store is not None
        self.existing_spills = int(self.store._get_meta("total_spills")) > 0
        self.spill_activity = self.existing_spills
        if self.active is None:
            self.active = ActiveGroupTable(
                plan=self.plan,
                spill_callback=self._spill_group,
            )
        else:
            self.active.evict_all_without_spill()
            self.active.stats = MemoryStats()
            self.active.stats.observe_active(len(self.active.groups), self.active.used_bytes)
        self.writer = SpillWriter(
            self.plan,
            batch_id="",
            first_sequence=self.store.next_spill_sequence(),
        )
        self.touched = {}
        self.record_groups = []

    def _spill_group(self, group: GroupState) -> None:
        assert self.writer is not None
        record = self.writer.write(group)
        self.touched.setdefault(group.group_id, group)
        if record is not None:
            self.spill_activity = True

    def _record_expected(self, row: dict[str, Any] | SpillRecord) -> dict[str, Any]:
        if isinstance(row, SpillRecord):
            return {
                "sha256": row.sha256,
                "spill_seq": row.spill_seq,
                "group_id": row.group_id,
                "group_key_json": row.group_key_json,
                "state_version": row.state_version,
                "path": row.path,
                "size_bytes": row.size_bytes,
            }
        return {
            "sha256": row["sha256"],
            "spill_seq": row["spill_seq"],
            "group_id": row["group_id"],
            "group_key_json": row["group_key_json"],
            "state_version": row["state_version"],
            "path": self.plan.spill_dir / row["path_name"],
            "size_bytes": row["size_bytes"],
        }

    def _load_group_from_disk_chain(
        self,
        group_id: str,
        db_group: GroupState | None,
        previous_touched: GroupState | None,
    ) -> GroupState | None:
        assert self.store is not None
        assert self.active is not None
        assert self.writer is not None
        manifest = self.store.spill_manifest(group_id)
        staged = [record for record in self.writer.records if record.group_id == group_id]
        rows: list[dict[str, Any] | SpillRecord] = [*manifest, *staged]
        rows.sort(key=lambda item: item.spill_seq if isinstance(item, SpillRecord) else item["spill_seq"])
        if not rows and db_group is None:
            return None

        base = db_group or previous_touched
        if base is None:
            if not rows:
                return None
            first_row = rows[0]
            if isinstance(first_row, SpillRecord):
                values = first_row.values
                group_key_json = first_row.group_key_json
            else:
                values = tuple(json.loads(first_row["values_json"]))
                group_key_json = first_row["group_key_json"]
            group = GroupState(
                group_id=group_id,
                group_key_json=group_key_json,
                values=values,
                state=StateVector(),
                state_version=1,
                base_state_version=0,
                new_group=True,
            )
            base = group

        chain_sum = StateVector()
        expected_previous: str | None = None
        skipped = False
        for row in rows:
            expected = self._record_expected(row)
            path: Path = expected["path"]
            size = int(expected["size_bytes"])
            if not self.active.buffer_available(size):
                skipped = True
                break
            obj, payload = read_spill_file(path, expected)
            if expected_previous is not None and obj.get("previous_sha256") != expected_previous:
                raise fail(
                    "E_SPILL_TAMPERED",
                    "spill chain predecessor digest mismatch",
                    path=path.name,
                    group_id=group_id,
                )
            expected_previous = expected["sha256"]
            if obj.get("plan_id") != self.plan.plan_id:
                raise fail(
                    "E_SPILL_TAMPERED",
                    "spill file belongs to another plan",
                    path=path.name,
                )
            if obj.get("dimensions") != list(self.plan.dimensions):
                raise fail(
                    "E_SPILL_TAMPERED",
                    "spill file dimension declaration mismatch",
                    path=path.name,
                )
            if obj.get("group_key_json") != base.group_key_json:
                raise fail(
                    "E_SPILL_TAMPERED",
                    "spill file group key mismatch",
                    path=path.name,
                )
            try:
                delta = StateVector.from_dict(obj["delta"])
            except (KeyError, FlowAggError) as exc:
                raise fail(
                    "E_SPILL_TAMPERED",
                    f"spill delta is malformed: {path.name}",
                    path=path.name,
                ) from exc
            chain_sum.merge(delta)
            self.active.account_read(len(payload))
        if rows:
            self.spill_activity = True
            self.active.stats.readback_count += 1

        if skipped:
            if db_group is None and previous_touched is None:
                raise fail(
                    "E_READ_BUFFER_DEFERRED",
                    "read buffer cannot materialize the only copy of group state",
                    group_id=group_id,
                    read_buffer_bytes=self.plan.read_buffer_bytes,
                    reason="READ_BUFFER_TOO_SMALL",
                )
            self.writer.discard_group(group_id)
            group = previous_touched or db_group
            assert group is not None
            group.state = (
                previous_touched.state.copy()
                if previous_touched is not None
                else db_group.state.copy()  # type: ignore[union-attr]
            )
            if db_group is not None:
                group.spill_sum = db_group.spill_sum.copy()
                group.spill_chain = list(db_group.spill_chain)
                group.last_spill_sha256 = db_group.last_spill_sha256
            else:
                group.spill_sum = StateVector()
                group.spill_chain = []
                group.last_spill_sha256 = None
            return group

        if db_group is not None:
            if chain_sum.count > db_group.state.count:
                if not skipped and not staged:
                    raise fail(
                        "E_STATE_CORRUPT",
                        "spill chain is ahead of the committed group state",
                        group_id=group_id,
                        chain=chain_sum.to_dict(),
                        committed=db_group.state.to_dict(),
                        spill_chain=[
                            int(row["spill_seq"])
                            if isinstance(row, dict)
                            else row.spill_seq
                            for row in rows
                        ],
                    )
            chosen_state = (
                chain_sum.copy()
                if chain_sum.count >= db_group.state.count
                else db_group.state.copy()
            )
            if chosen_state.count == db_group.state.count and chosen_state.to_dict() != db_group.state.to_dict():
                raise fail(
                    "E_STATE_CORRUPT",
                    "spill chain and committed state diverge at equal count",
                    group_id=group_id,
                )
            group = db_group
            group.state = chosen_state
        else:
            assert base is not None
            group = base
            group.state = chain_sum.copy()

        group.spill_sum = chain_sum.copy()
        group.spill_chain = [
            int(row["spill_seq"]) if isinstance(row, dict) else row.spill_seq for row in rows
        ]
        group.last_spill_sha256 = expected_previous or group.last_spill_sha256
        if previous_touched is not None:
            group.state_version = previous_touched.state_version
            group.base_state_version = previous_touched.base_state_version
            group.dirty = previous_touched.dirty
            group.new_group = previous_touched.new_group
        return group

    def _get_or_create_group(self, spec: GroupSpec) -> GroupState:
        assert self.store is not None
        assert self.active is not None
        active = self.active.groups.get(spec.group_id)
        if active is not None:
            group = self.active.touch(spec.group_id)
            self.touched[group.group_id] = group
            return group
        previous_touched = self.touched.get(spec.group_id)
        db_group = self.store.load_group(spec.group_id)
        group = self._load_group_from_disk_chain(spec.group_id, db_group, previous_touched)
        if group is None:
            group = GroupState(
                group_id=spec.group_id,
                group_key_json=spec.group_key_json,
                values=spec.values,
                state=StateVector(),
                state_version=0,
                base_state_version=0,
                new_group=True,
            )
        if group.group_key_json != spec.group_key_json:
            raise fail(
                "E_STATE_CORRUPT",
                "group key does not match its persisted identity",
                group_id=spec.group_id,
            )
        inserted = self.active.insert(group)
        self.touched[group.group_id] = inserted
        return inserted

    def _checkpoint_active(self) -> None:
        assert self.active is not None
        active_groups = sorted(
            self.active.groups.values(), key=lambda group: group.group_key_json
        )
        for group in active_groups:
            self._spill_group(group)
        self.active.evict_all_without_spill()

    def _aggregate_data(self, batch: BatchData, *, resuming: bool = False) -> dict[str, Any]:
        assert self.store is not None
        existing = self.store.get_batch(batch.batch_id)
        if existing is not None and existing["status"] == "committed":
            if existing["content_digest"] != batch.digest:
                raise fail(
                    "E_DIGEST_MISMATCH",
                    "same batch_id was submitted with different content",
                    batch_id=batch.batch_id,
                    committed_digest=existing["content_digest"],
                    submitted_digest=batch.digest,
                )
            result = json.loads(existing["result_json"])
            result["idempotent_replay"] = True
            return result

        if existing is not None:
            self.cleanup_orphans()
        new_watermark = self.store.validate_batch_advance(batch, resuming=resuming)
        self._reset_batch_runtime()
        self.writer.batch_id = batch.batch_id
        intent = self.store.start_intent(batch)
        if existing is None:
            maybe_crash("after_intent")

        created_processing = intent["status"] == "processing"
        try:
            if existing is not None and existing["content_digest"] != batch.digest:
                raise fail(
                    "E_DIGEST_MISMATCH",
                    "processing batch digest mismatch",
                    batch_id=batch.batch_id,
                )

            for record in batch.records:
                spec = GroupSpec.build(
                    self.plan,
                    record.src_endpoint,
                    record.dst_endpoint,
                    record.protocol,
                    record.direction,
                )
                group = self._get_or_create_group(spec)
                group.begin_dirty()
                group.state.add_record(record)
                self.record_groups.append((group.group_id, record))
                self.active.touch(group.group_id)

            assert self.active is not None
            self.active.assert_budget()
            if self.spill_activity:
                self._checkpoint_active()

            groups = sorted(
                self.touched.values(), key=lambda group: group.group_key_json
            )
            spills = list(self.writer.records)
            maybe_crash("before_commit")
            memory_stats = self.active.stats
            result = {
                "batch": {
                    "batch_id": batch.batch_id,
                    "content_digest": batch.digest,
                    "logical_watermark_ms": new_watermark,
                    "max_record_seq": batch.records[-1].record_seq,
                    "records": len(batch.records),
                },
                "command": "aggregate",
                "groups_touched": len(groups),
                "ok": True,
                "readback": {
                    "deferred": memory_stats.deferred_readbacks,
                    "files": memory_stats.readback_files,
                    "groups": memory_stats.readback_count,
                },
                "spill": {
                    "files": len(spills),
                    "sequences": [spill.spill_seq for spill in spills],
                },
                "state_versions": {
                    group.group_key_json: group.state_version for group in groups
                },
            }
            self.store.commit_batch(
                batch,
                groups,
                self.record_groups,
                spills,
                new_watermark=new_watermark,
                memory_stats=memory_stats,
                result=result,
            )
            if self.active is not None:
                self.active.evict_all_without_spill()
            return result
        except Exception:
            if created_processing:
                try:
                    if self.writer is not None:
                        self.writer.cleanup_uncommitted()
                    self.store.abort_intent(batch.batch_id)
                    if self.active is not None:
                        self.active.evict_all_without_spill()
                except Exception:
                    pass
            raise

    def aggregate_path(self, path: str | Path, batch_id: str) -> dict[str, Any]:
        batch = read_batch(path, self.plan, batch_id)
        with self.session():
            return self._aggregate_data(batch)

    def aggregate_manifest(self, manifest_path: str | Path) -> dict[str, Any]:
        entries = read_batch_manifest(manifest_path)
        batches = [
            read_batch(entry["path"], self.plan, entry["batch_id"]) for entry in entries
        ]
        results: list[dict[str, Any]] = []
        with self.session():
            for batch in batches:
                results.append(self._aggregate_data(batch))
            assert self.store is not None
            status = self.store.status()
        return {
            "batches": results,
            "command": "aggregate",
            "ok": True,
            "status": status,
        }

    def cleanup_orphans(self) -> dict[str, int]:
        assert self.store is not None
        self.plan.spill_dir.mkdir(parents=True, exist_ok=True)
        referenced = {
            row["path_name"] for row in self.store.spill_manifest()
        }
        removed_files = 0
        removed_bytes = 0
        for path in sorted(self.plan.spill_dir.iterdir()):
            if not path.is_file():
                continue
            is_spill = path.name.startswith("spill-") and path.name.endswith(".json")
            is_temp = path.name.startswith(".flowagg-") and path.name.endswith(".tmp")
            if (is_spill or is_temp) and path.name not in referenced:
                size = path.stat().st_size
                path.unlink()
                removed_files += 1
                removed_bytes += size
        return {"files": removed_files, "bytes": removed_bytes}

    def resume(self, manifest_path: str | Path | None = None) -> dict[str, Any]:
        with self.session():
            assert self.store is not None
            orphans = self.cleanup_orphans()
            pending = self.store.processing_batches()
            resumed: list[dict[str, Any]] = []
            for row in pending:
                batch = read_batch(row["input_path"], self.plan, row["batch_id"])
                if batch.digest != row["content_digest"]:
                    raise fail(
                        "E_DIGEST_MISMATCH",
                        "batch content changed since the interrupted run",
                        batch_id=row["batch_id"],
                        expected_digest=row["content_digest"],
                        actual_digest=batch.digest,
                    )
                resumed.append(self._aggregate_data(batch, resuming=True))
            continued: list[dict[str, Any]] = []
            if manifest_path is not None:
                for entry in read_batch_manifest(manifest_path):
                    batch = read_batch(entry["path"], self.plan, entry["batch_id"])
                    result = self._aggregate_data(batch, resuming=True)
                    if result.get("idempotent_replay") is not True:
                        continued.append(result)
            return {
                "command": "resume",
                "continued_batches": continued,
                "ok": True,
                "orphans_removed": orphans,
                "resumed_batches": resumed,
                "status": self.store.status(),
            }

    def status(self, *, with_groups: bool = False) -> dict[str, Any]:
        with self.session():
            assert self.store is not None
            result = {
                "command": "status",
                "ok": True,
                "plan_id": self.plan.plan_id,
                "status": self.store.status(),
            }
            if with_groups:
                result["groups"] = self.report_groups()
            return result

    def report_groups(self) -> list[dict[str, Any]]:
        if self.store is None:
            with self.session():
                return self.report_groups()
        assert self.store is not None
        output: list[dict[str, Any]] = []
        for group in self.store.all_groups():
            sample_count = self.store.sample_count(group.group_id)
            if sample_count != group.state.count:
                raise fail(
                    "E_STATE_CORRUPT",
                    "sample series count differs from group count",
                    group_key=group.group_key_json,
                    state_count=group.state.count,
                    sample_count=sample_count,
                )
            quantiles = quantiles_for_group(
                self.store, group.group_id, group.state.count, self.plan
            )
            output.append(group_public_view(self.plan, group, quantiles))
        return output

    def _validate_spill_chains(
        self,
        groups: list[GroupState],
    ) -> dict[str, Any]:
        assert self.store is not None
        group_map = {group.group_id: group for group in groups}
        manifest = self.store.spill_manifest()
        by_group: dict[str, list[dict[str, Any]]] = {}
        referenced: set[str] = set()
        for row in manifest:
            by_group.setdefault(row["group_id"], []).append(row)
            referenced.add(row["path_name"])
        spill_dir = self.plan.spill_dir
        extras = []
        if spill_dir.exists():
            for path in sorted(spill_dir.iterdir()):
                if (
                    path.is_file()
                    and (
                        (path.name.startswith("spill-") and path.name.endswith(".json"))
                        or (
                            path.name.startswith(".flowagg-")
                            and path.name.endswith(".tmp")
                        )
                    )
                    and path.name not in referenced
                ):
                    extras.append(path.name)
        if extras:
            raise fail(
                "E_SPILL_TAMPERED",
                "spill directory contains files absent from the manifest",
                orphan_files=extras,
            )

        total_files = 0
        total_bytes = 0
        complete_groups = 0
        for group in sorted(groups, key=lambda item: item.group_key_json):
            rows = sorted(by_group.get(group.group_id, []), key=lambda row: row["spill_seq"])
            if not rows:
                continue
            chain_sum = StateVector()
            previous_sha: str | None = None
            for row in rows:
                path = spill_dir / row["path_name"]
                if path.parent.resolve() != spill_dir.resolve():
                    raise fail(
                        "E_SPILL_TAMPERED",
                        "spill path escapes the configured spill directory",
                        path=row["path_name"],
                    )
                if row["size_bytes"] + 256 > self.plan.read_buffer_bytes:
                    raise fail(
                        "E_READ_BUFFER_DEFERRED",
                        "read buffer is too small to verify a spill shard",
                        path=row["path_name"],
                        size_bytes=row["size_bytes"],
                        read_buffer_bytes=self.plan.read_buffer_bytes,
                    )
                obj, payload = read_spill_file(path, row)
                if previous_sha is None and obj.get("previous_sha256") is not None:
                    raise fail(
                        "E_SPILL_TAMPERED",
                        "first spill shard has a predecessor digest",
                        path=row["path_name"],
                    )
                if previous_sha is not None and obj.get("previous_sha256") != previous_sha:
                    raise fail(
                        "E_SPILL_TAMPERED",
                        "spill chain predecessor digest mismatch",
                        path=row["path_name"],
                        group_key=group.group_key_json,
                    )
                previous_sha = row["sha256"]
                if obj.get("plan_id") != self.plan.plan_id:
                    raise fail(
                        "E_SPILL_TAMPERED",
                        "spill file belongs to another plan",
                        path=row["path_name"],
                    )
                if obj.get("group_key_json") != group.group_key_json:
                    raise fail(
                        "E_SPILL_TAMPERED",
                        "spill file group key mismatch",
                        path=row["path_name"],
                    )
                if obj.get("dimensions") != list(self.plan.dimensions):
                    raise fail(
                        "E_SPILL_TAMPERED",
                        "spill dimension metadata mismatch",
                        path=row["path_name"],
                    )
                if json.loads(row["values_json"]) != list(group.values):
                    raise fail(
                        "E_SPILL_TAMPERED",
                        "spill manifest values differ from group key",
                        path=row["path_name"],
                    )
                delta = StateVector.from_dict(obj["delta"])
                chain_sum.merge(delta)
                total_files += 1
                total_bytes += len(payload)
            if chain_sum.to_dict() != group.state.to_dict():
                raise fail(
                    "E_VERIFY_MISMATCH",
                    "merged spill chain differs from committed group state",
                    first_divergence_group=list(group.values),
                    database_state=group.state.to_dict(),
                    spill_chain_state=chain_sum.to_dict(),
                    spill_chain=[row["spill_seq"] for row in rows],
                )
            complete_groups += 1
        return {
            "complete_groups": complete_groups,
            "files": total_files,
            "groups": len(by_group),
            "manifest_rows": len(manifest),
            "read_bytes": total_bytes,
        }

    def _build_truth(
        self,
        manifest_path: str | Path,
    ) -> tuple[dict[str, GroupState], dict[str, dict[str, list[int]]], dict[str, Any]]:
        assert self.store is not None
        entries = read_batch_manifest(manifest_path)
        batches = [
            read_batch(entry["path"], self.plan, entry["batch_id"]) for entry in entries
        ]
        committed_rows = {
            row["batch_id"]: row
            for row in self.store.conn.execute(
                "SELECT * FROM batches WHERE status='committed' ORDER BY batch_order"
            ).fetchall()
        }
        manifest_ids = {batch.batch_id for batch in batches}
        if set(committed_rows) != manifest_ids:
            raise fail(
                "E_VERIFY_MISMATCH",
                "manifest batches and committed batches differ",
                committed_batches=sorted(committed_rows),
                manifest_batches=sorted(manifest_ids),
            )
        ordered_committed = sorted(
            committed_rows.values(), key=lambda row: row["batch_order"]
        )
        digest_chain = b"\0" * 32
        for row in ordered_committed:
            digest_chain = hashlib.sha256(
                digest_chain + bytes.fromhex(row["content_digest"])
            ).digest()
        expected_watermark = max(
            (
                int(row["committed_watermark_ms"])
                for row in ordered_committed
                if row["committed_watermark_ms"] is not None
            ),
            default=0,
        )
        expected_consumed = max(
            (int(row["max_record_seq"]) for row in ordered_committed),
            default=0,
        )
        metadata = self.store.meta_dict()
        if (
            digest_chain.hex() != metadata["committed_digest_chain"]
            or expected_watermark != int(metadata["logical_watermark_ms"])
            or expected_consumed != int(metadata["consumed_record_seq"])
            or len(ordered_committed) != int(metadata["committed_batches"])
        ):
            raise fail(
                "E_VERIFY_MISMATCH",
                "committed batch metadata chain does not match the manifest",
                digest_chain_persisted=metadata["committed_digest_chain"],
                digest_chain_recomputed=digest_chain.hex(),
                watermark_persisted=int(metadata["logical_watermark_ms"]),
                watermark_recomputed=expected_watermark,
                consumed_seq_persisted=int(metadata["consumed_record_seq"]),
                consumed_seq_recomputed=expected_consumed,
                committed_batches_persisted=int(metadata["committed_batches"]),
                committed_batches_recomputed=len(ordered_committed),
            )
        truth_groups: dict[str, GroupState] = {}
        values: dict[str, dict[str, list[tuple[int, int, int]]]] = {}
        records_verified = 0
        for batch in batches:
            row = committed_rows[batch.batch_id]
            if row["content_digest"] != batch.digest:
                raise fail(
                    "E_DIGEST_MISMATCH",
                    "manifest batch digest differs from committed digest",
                    batch_id=batch.batch_id,
                )
            for record in batch.records:
                spec = GroupSpec.build(
                    self.plan,
                    record.src_endpoint,
                    record.dst_endpoint,
                    record.protocol,
                    record.direction,
                )
                group = truth_groups.get(spec.group_id)
                if group is None:
                    group = GroupState(
                        group_id=spec.group_id,
                        group_key_json=spec.group_key_json,
                        values=spec.values,
                        state=StateVector(),
                        state_version=0,
                        base_state_version=0,
                    )
                    truth_groups[spec.group_id] = group
                    values[spec.group_id] = {"bytes": [], "packets": []}
                group.state.add_record(record)
                values[spec.group_id]["bytes"].append(
                    (record.bytes, record.clock_ms, record.record_seq)
                )
                values[spec.group_id]["packets"].append(
                    (record.packets, record.clock_ms, record.record_seq)
                )
                records_verified += 1
        compact_values: dict[str, dict[str, list[int]]] = {}
        for group_id, fields in values.items():
            compact_values[group_id] = {}
            for field, triples in fields.items():
                triples.sort(key=lambda item: (item[0], item[1]))
                compact_values[group_id][field] = [item[0] for item in triples]
        return truth_groups, compact_values, {
            "batches": len(batches),
            "records": records_verified,
        }

    def verify(self, manifest_path: str | Path) -> dict[str, Any]:
        with self.session():
            assert self.store is not None
            groups = self.store.all_groups()
            if not groups:
                raise fail("E_NO_DATA", "no committed aggregate groups to verify")
            truth_groups, truth_values, counts = self._build_truth(manifest_path)
            db_groups = {group.group_id: group for group in groups}
            first_divergence: dict[str, Any] | None = None

            for group_id in sorted(
                set(db_groups) | set(truth_groups),
                key=lambda key: (
                    db_groups[key].group_key_json
                    if key in db_groups
                    else truth_groups[key].group_key_json
                ),
            ):
                db_group = db_groups.get(group_id)
                truth_group = truth_groups.get(group_id)
                if db_group is None or truth_group is None:
                    exemplar = db_group or truth_group
                    assert exemplar is not None
                    first_divergence = {
                        "database_state": db_group.state.to_dict() if db_group else None,
                        "field": "group_presence",
                        "group_key": list(exemplar.values),
                        "spill_chain": list(db_group.spill_chain) if db_group else [],
                        "truth_state": truth_group.state.to_dict() if truth_group else None,
                    }
                    break
                if db_group.state.to_dict() != truth_group.state.to_dict():
                    state_fields = list(db_group.state.to_dict())
                    field = next(
                        name
                        for name in state_fields
                        if db_group.state.to_dict()[name] != truth_group.state.to_dict()[name]
                    )
                    first_divergence = {
                        "database_state": {
                            **db_group.state.to_dict(),
                            "mean_bytes": db_group.mean_bytes,
                            "mean_packets": db_group.mean_packets,
                        },
                        "field": field,
                        "group_key": list(db_group.values),
                        "spill_chain": list(db_group.spill_chain),
                        "truth_state": {
                            **truth_group.state.to_dict(),
                            "mean_bytes": truth_group.mean_bytes,
                            "mean_packets": truth_group.mean_packets,
                        },
                    }
                    break
                db_quantiles = quantiles_for_group(
                    self.store, group_id, db_group.state.count, self.plan
                )
                truth_source = TruthOrderSource(truth_values[group_id])
                truth_quantiles = quantiles_for_group(
                    truth_source, group_id, truth_group.state.count, self.plan
                )
                if db_quantiles != truth_quantiles:
                    first_divergence = {
                        "database_quantiles": db_quantiles,
                        "field": "quantiles",
                        "group_key": list(db_group.values),
                        "spill_chain": list(db_group.spill_chain),
                        "truth_quantiles": truth_quantiles,
                    }
                    break

            if first_divergence is not None:
                raise fail(
                    "E_VERIFY_MISMATCH",
                    "first diverging group found",
                    first_divergence=first_divergence,
                )

            spill_verification = self._validate_spill_chains(groups)
            report_groups = self.report_groups()
            state_hash = hashlib.sha256(
                canonical_json(report_groups).encode("utf-8")
            ).hexdigest()
            return {
                "batches_verified": counts["batches"],
                "command": "verify",
                "first_divergence": None,
                "groups": report_groups,
                "groups_verified": len(groups),
                "ok": True,
                "plan_id": self.plan.plan_id,
                "records_verified": counts["records"],
                "spill_verification": spill_verification,
                "state_hash": state_hash,
                "status": self.store.status(),
            }

