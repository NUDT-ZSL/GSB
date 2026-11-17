"""JSON + SQLite repair ledger with whole-view replacement semantics."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .analysis import AnalysisResult
from .consensus import distribution_by_node
from .errors import StoredLedgerError
from .util import (
    atomic_write_json,
    canonical_json_text,
    digest_json,
    is_positive_int,
    set_read_only,
)


LEDGER_FORMAT_VERSION = 1
PRODUCER = "replica-ledger/1.0"


def build_ledger_payload(
    *,
    before: AnalysisResult,
    after: AnalysisResult,
    execution: Mapping[str, Any] | None = None,
    grid_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if before.spec.group_id != after.spec.group_id:
        raise StoredLedgerError(
            "修复前后副本组标识不一致", "ledger.group_id"
        )
    truth_decisions = [
        before.consensus.decisions[block_id].to_dict()
        for block_id in sorted(before.consensus.decisions)
    ]
    post_truth_decisions = [
        after.consensus.decisions[block_id].to_dict()
        for block_id in sorted(after.consensus.decisions)
    ]
    identity = {
        "group_id": before.spec.group_id,
        "before_state_digest": before.group_state_digest,
        "after_state_digest": after.group_state_digest,
        "scale": before.scale,
    }
    payload = {
        "ledger_format_version": LEDGER_FORMAT_VERSION,
        "ledger_id": "ledger-" + digest_json(identity)[:24],
        "producer": PRODUCER,
        "group_id": before.spec.group_id,
        "grid_scale": before.scale,
        "spec_digest_before": before.spec.spec_digest,
        "spec_digest_after": after.spec.spec_digest,
        "group_state_digest_before": before.group_state_digest,
        "group_state_digest": after.group_state_digest,
        "before_distribution": distribution_by_node(before.consensus.replicas),
        "truth_decisions": truth_decisions,
        "repair_tasks": [task.to_dict() for task in before.plan.tasks],
        "cost_summary": before.plan.summary,
        "repair_after_distribution": distribution_by_node(after.consensus.replicas),
        "post_repair_truth_decisions": post_truth_decisions,
        "post_repair_complete": all(
            decision.mismatch_count == 0
            for decision in after.consensus.decisions.values()
        ),
        "execution": dict(execution or {}),
        "grid_diagnostics": dict(grid_diagnostics or {}),
        "fixed_point_error_model": {
            "truncation": "floor",
            "single_term_error_bound_units": 1,
            "ratio_comparison_error_bound_units": 1,
            "candidate_cost_bound_units": (
                "sum(weight * 1 for each non-divisible quantized term)"
            ),
            "pair_ranking_bound_units": "candidate_bound_a + candidate_bound_b",
            "tie_policy": "priority desc, node_id asc",
        },
    }
    return payload


def seal_ledger(payload: Mapping[str, Any]) -> dict[str, Any]:
    payload_dict = dict(payload)
    if payload_dict.get("ledger_format_version") != LEDGER_FORMAT_VERSION:
        raise StoredLedgerError(
            "账本载荷格式版本不支持",
            "ledger.payload.ledger_format_version",
            details={
                "expected": LEDGER_FORMAT_VERSION,
                "actual": payload_dict.get("ledger_format_version"),
            },
        )
    if not isinstance(payload_dict.get("group_state_digest"), str):
        raise StoredLedgerError(
            "账本载荷缺少 group_state_digest", "ledger.payload.group_state_digest"
        )
    return {
        "format_version": LEDGER_FORMAT_VERSION,
        "content_digest": digest_json(payload_dict),
        "payload": payload_dict,
    }


def verify_sealed_ledger(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise StoredLedgerError("账本 JSON 顶层必须是对象", "ledger")
    format_version = document.get("format_version")
    if not is_positive_int(format_version):
        raise StoredLedgerError("账本格式版本必须是正整数", "ledger.format_version")
    content_digest = document.get("content_digest")
    payload = document.get("payload")
    if not isinstance(content_digest, str) or not isinstance(payload, dict):
        raise StoredLedgerError("账本缺少 content_digest 或 payload", "ledger")
    if payload.get("ledger_format_version") != format_version:
        raise StoredLedgerError(
            "账本内外格式版本不一致", "ledger.format_version"
        )
    computed = digest_json(payload)
    if computed != content_digest:
        raise StoredLedgerError(
            "账本内容摘要校验失败",
            "ledger.content_digest",
            details={"stored": content_digest, "computed": computed},
        )
    return document


@dataclass(frozen=True)
class LedgerView:
    status: str
    is_current: bool
    format_version: int | None
    content_digest: str | None
    group_state_digest: str | None
    payload: dict[str, Any] | None
    backup_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        summary = None
        if self.payload is not None:
            summary = {
                "ledger_id": self.payload.get("ledger_id"),
                "group_id": self.payload.get("group_id"),
                "grid_scale": self.payload.get("grid_scale"),
                "repair_task_count": len(self.payload.get("repair_tasks", [])),
                "post_repair_complete": self.payload.get("post_repair_complete"),
            }
        return {
            "status": self.status,
            "is_current": self.is_current,
            "format_version": self.format_version,
            "content_digest": self.content_digest,
            "group_state_digest": self.group_state_digest,
            "backup_paths": list(self.backup_paths),
            "payload_summary": summary,
        }


class LedgerStore:
    def __init__(
        self,
        json_path: str | Path,
        sqlite_path: str | Path,
        *,
        format_version: int = LEDGER_FORMAT_VERSION,
    ) -> None:
        self.json_path = Path(json_path)
        self.sqlite_path = Path(sqlite_path)
        self.format_version = int(format_version)
        if self.format_version != LEDGER_FORMAT_VERSION:
            # Persisted documents are written with the module version.  This
            # guard catches callers attempting to silently mislabel a view.
            raise StoredLedgerError(
                "LedgerStore 的格式版本与实现不一致",
                "ledger.format_version",
                details={
                    "requested": self.format_version,
                    "supported": LEDGER_FORMAT_VERSION,
                },
            )

    def exists(self) -> bool:
        return self.json_path.exists() and self.sqlite_path.exists()

    def _read_json_document(self) -> dict[str, Any]:
        try:
            raw = self.json_path.read_bytes()
        except OSError as exc:
            raise StoredLedgerError(
                "无法读取账本 JSON",
                "ledger.json",
                details={"path": str(self.json_path), "os_error": str(exc)},
            ) from exc
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoredLedgerError(
                "账本 JSON 损坏",
                "ledger.json",
                details={"path": str(self.json_path), "error": str(exc)},
            ) from exc
        return verify_sealed_ledger(document)

    def _verify_sqlite(self, document: Mapping[str, Any]) -> None:
        if not self.sqlite_path.is_file():
            raise StoredLedgerError(
                "账本 SQLite 文件缺失", "ledger.sqlite"
            )
        try:
            connection = sqlite3.connect(str(self.sqlite_path))
        except sqlite3.Error as exc:
            raise StoredLedgerError(
                "无法打开账本 SQLite", "ledger.sqlite", details={"error": str(exc)}
            ) from exc
        try:
            row = connection.execute(
                "SELECT format_version, content_digest, group_state_digest, payload_json "
                "FROM ledger_current WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoredLedgerError(
                "账本 SQLite 结构损坏", "ledger.sqlite", details={"error": str(exc)}
            ) from exc
        finally:
            connection.close()
        if row is None:
            raise StoredLedgerError("账本 SQLite 中无当前视图", "ledger.sqlite")
        payload = document["payload"]
        expected = (
            document["format_version"],
            document["content_digest"],
            payload["group_state_digest"],
        )
        actual = (row[0], row[1], row[2])
        if expected != actual:
            raise StoredLedgerError(
                "账本 JSON 与 SQLite 视图不一致",
                "ledger.sqlite",
                details={"json": list(expected), "sqlite": list(actual)},
            )
        if row[3] != canonical_json_text(payload):
            raise StoredLedgerError(
                "账本 JSON 与 SQLite 载荷内容不一致", "ledger.sqlite.payload_json"
            )

    def open(
        self,
        *,
        expected_group_state_digest: str | None = None,
        expected_format_version: int | None = None,
    ) -> LedgerView:
        """Open the persisted view without merging it into any other state.

        A version/content mismatch yields ``status='stale'`` so the caller can
        explicitly reconcile by supplying a freshly built whole-view payload.
        """

        if not self.json_path.exists() and not self.sqlite_path.exists():
            return LedgerView("absent", False, None, None, None, None)
        if not self.json_path.exists() or not self.sqlite_path.exists():
            raise StoredLedgerError(
                "账本 JSON 与 SQLite 必须同时存在", "ledger"
            )
        document = self._read_json_document()
        self._verify_sqlite(document)
        payload = document["payload"]
        version = int(document["format_version"])
        state_digest = str(payload["group_state_digest"])
        expected_version = (
            self.format_version if expected_format_version is None else expected_format_version
        )
        is_current = version == expected_version and (
            expected_group_state_digest is None
            or state_digest == expected_group_state_digest
        )
        return LedgerView(
            status="current" if is_current else "stale",
            is_current=is_current,
            format_version=version,
            content_digest=str(document["content_digest"]),
            group_state_digest=state_digest,
            payload=payload,
        )

    def save(
        self,
        payload: Mapping[str, Any],
        *,
        backup_readonly: bool = True,
    ) -> LedgerView:
        document = seal_ledger(payload)
        new_digest = str(document["payload"]["group_state_digest"])
        new_content = str(document["content_digest"])
        backups: list[Path] = []
        replacing = False
        any_existing = self.json_path.exists() or self.sqlite_path.exists()
        if any_existing:
            try:
                if not (self.json_path.exists() and self.sqlite_path.exists()):
                    raise StoredLedgerError(
                        "incomplete existing ledger", "ledger"
                    )
                old_document = self._read_json_document()
                self._verify_sqlite(old_document)
                old_version = int(old_document["format_version"])
                old_state = str(old_document["payload"]["group_state_digest"])
                replacing = old_version != self.format_version or old_state != new_digest
            except StoredLedgerError:
                replacing = True
            if replacing:
                backups.extend(self._backup_existing(readonly=backup_readonly))
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.json_path, document)
        self._atomic_write_sqlite(document)
        self._verify_sqlite(document)
        return LedgerView(
            status="replaced" if replacing else "saved",
            is_current=True,
            format_version=self.format_version,
            content_digest=new_content,
            group_state_digest=new_digest,
            payload=document["payload"],
            backup_paths=tuple(str(path) for path in backups),
        )

    def reconcile(
        self,
        current_payload: Mapping[str, Any],
        *,
        expected_format_version: int | None = None,
    ) -> LedgerView:
        """Honor an explicit whole-view replacement when versions differ."""

        sealed = seal_ledger(current_payload)
        expected_state = str(sealed["payload"]["group_state_digest"])
        expected_version = (
            self.format_version if expected_format_version is None else expected_format_version
        )
        if not self.json_path.exists() and not self.sqlite_path.exists():
            saved = self.save(current_payload)
            return LedgerView(
                "replaced",
                True,
                saved.format_version,
                saved.content_digest,
                saved.group_state_digest,
                saved.payload,
                saved.backup_paths,
            )
        if not (self.json_path.exists() and self.sqlite_path.exists()):
            return self.save(current_payload)
        existing = self.open(
            expected_group_state_digest=expected_state,
            expected_format_version=expected_version,
        )
        if existing.is_current:
            return existing
        return self.save(current_payload)

    def reopen_or_replace(self, current_payload: Mapping[str, Any]) -> LedgerView:
        """Explicit reopen contract used by integration callers."""

        return self.reconcile(current_payload)

    def _backup_existing(self, *, readonly: bool) -> list[Path]:
        backups: list[Path] = []
        tag = "unknown"
        try:
            old = self._read_json_document()
            tag = str(old["content_digest"])[:12]
        except StoredLedgerError:
            pass
        for source, suffix in (
            (self.json_path, ".json"),
            (self.sqlite_path, ".sqlite"),
        ):
            if not source.exists():
                continue
            base = source.parent / f"ledger.backup-{tag}{suffix}"
            destination = base
            counter = 1
            while destination.exists():
                destination = source.parent / f"ledger.backup-{tag}-{counter}{suffix}"
                counter += 1
            shutil.copy2(source, destination)
            if readonly:
                set_read_only(destination)
            backups.append(destination)
        return backups

    def _atomic_write_sqlite(self, document: Mapping[str, Any]) -> None:
        payload = document["payload"]
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.sqlite_path.name}.tmp-",
            dir=str(self.sqlite_path.parent),
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            connection = sqlite3.connect(str(temp_path))
            try:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                self._create_schema(connection)
                self._insert_payload(connection, document)
                connection.commit()
            finally:
                connection.close()
            os.replace(temp_path, self.sqlite_path)
        except BaseException:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE ledger_current (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                format_version INTEGER NOT NULL,
                content_digest TEXT NOT NULL,
                group_state_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE replica_distribution (
                phase TEXT NOT NULL,
                node_id TEXT NOT NULL,
                block_count INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                PRIMARY KEY (phase, node_id)
            );
            CREATE TABLE distribution_blocks (
                phase TEXT NOT NULL,
                node_id TEXT NOT NULL,
                block_id TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                digest TEXT NOT NULL,
                PRIMARY KEY (phase, node_id, block_id)
            );
            CREATE TABLE truth_decisions (
                phase TEXT NOT NULL,
                block_id TEXT NOT NULL,
                truth_digest TEXT NOT NULL,
                truth_nodes_json TEXT NOT NULL,
                mismatch_nodes_json TEXT NOT NULL,
                replica_count INTEGER NOT NULL,
                strict_majority INTEGER NOT NULL,
                ratio_fixed INTEGER NOT NULL,
                grid_scale INTEGER NOT NULL,
                ratio_error_bound_units INTEGER NOT NULL,
                PRIMARY KEY (phase, block_id)
            );
            CREATE TABLE repair_tasks (
                task_id TEXT PRIMARY KEY,
                block_id TEXT NOT NULL,
                source_node_id TEXT NOT NULL,
                target_node_id TEXT NOT NULL,
                truth_digest TEXT NOT NULL,
                repair_amount_fixed INTEGER NOT NULL,
                repair_amount_error_bound_units INTEGER NOT NULL,
                workload_fixed INTEGER NOT NULL,
                workload_error_bound_units INTEGER NOT NULL,
                cost_fixed INTEGER NOT NULL,
                cost_error_bound_units INTEGER NOT NULL,
                tie_channel INTEGER NOT NULL
            );
            CREATE TABLE repair_costs (
                task_id TEXT NOT NULL,
                term_name TEXT NOT NULL,
                weighted_value INTEGER NOT NULL,
                weighted_error_bound_units INTEGER NOT NULL,
                PRIMARY KEY (task_id, term_name)
            );
            CREATE TABLE grid_flips (
                event_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                block_id TEXT NOT NULL,
                target_node_id TEXT NOT NULL,
                before_scale INTEGER NOT NULL,
                after_scale INTEGER NOT NULL,
                error_bound_units INTEGER NOT NULL,
                error_bound_share_ppm INTEGER NOT NULL,
                event_json TEXT NOT NULL
            );
            """
        )

    def _insert_payload(
        self, connection: sqlite3.Connection, document: Mapping[str, Any]
    ) -> None:
        payload = document["payload"]
        connection.execute(
            "INSERT INTO ledger_current VALUES (1, ?, ?, ?, ?)",
            (
                document["format_version"],
                document["content_digest"],
                payload["group_state_digest"],
                canonical_json_text(payload),
            ),
        )
        for phase, key in (
            ("before", "before_distribution"),
            ("after", "repair_after_distribution"),
        ):
            for node in payload.get(key, []):
                connection.execute(
                    "INSERT INTO replica_distribution VALUES (?, ?, ?, ?)",
                    (
                        phase,
                        node["node_id"],
                        node["block_count"],
                        node["total_bytes"],
                    ),
                )
                for block in node.get("blocks", []):
                    connection.execute(
                        "INSERT INTO distribution_blocks VALUES (?, ?, ?, ?, ?)",
                        (
                            phase,
                            node["node_id"],
                            block["block_id"],
                            block["byte_size"],
                            block["digest"],
                        ),
                    )
        for phase, key in (
            ("before", "truth_decisions"),
            ("after", "post_repair_truth_decisions"),
        ):
            for decision in payload.get(key, []):
                ratio = decision["mismatch_ratio"]
                connection.execute(
                    "INSERT INTO truth_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        phase,
                        decision["block_id"],
                        decision["truth_digest"],
                        canonical_json_text(decision["truth_nodes"]),
                        canonical_json_text(decision["mismatch_nodes"]),
                        decision["replica_count"],
                        1 if decision["strict_majority"] else 0,
                        ratio["ratio"]["value_fixed"],
                        ratio["scale"],
                        ratio["gap_error_bound_units"],
                    ),
                )
        for task in payload.get("repair_tasks", []):
            connection.execute(
                "INSERT INTO repair_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task["task_id"],
                    task["block_id"],
                    task["source_node_id"],
                    task["target_node_id"],
                    task["truth_digest"],
                    task["repair_amount_fixed"],
                    task["repair_amount_error_bound_units"],
                    task["workload"]["value_fixed"],
                    task["workload"]["error_bound_units"],
                    task["cost_fixed"],
                    task["cost_error_bound_units"],
                    1 if task["tie_channel"] else 0,
                ),
            )
            for term in task["source_selection"]["winner"]["terms"]:
                connection.execute(
                    "INSERT INTO repair_costs VALUES (?, ?, ?, ?)",
                    (
                        task["task_id"],
                        term["name"],
                        term["weighted_value"],
                        term["weighted_error_bound_units"],
                    ),
                )
        for event in payload.get("grid_diagnostics", {}).get("flips", []):
            if not isinstance(event, dict):
                continue
            connection.execute(
                "INSERT INTO grid_flips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["event_id"],
                    event["kind"],
                    event["block_id"],
                    event["target_node_id"],
                    event["before_scale"],
                    event["after_scale"],
                    event["error_bound_units"],
                    event["error_bound_share_ppm"],
                    canonical_json_text(event),
                ),
            )


def restore_or_replace_ledger(
    store: LedgerStore,
    current_payload: Mapping[str, Any],
) -> LedgerView:
    return store.reconcile(current_payload)


def reopen_or_replace_ledger(
    store: LedgerStore,
    current_payload: Mapping[str, Any],
) -> LedgerView:
    """Explicit reopen operation: whole replacement when stale, never a merge."""

    return store.reconcile(current_payload)
