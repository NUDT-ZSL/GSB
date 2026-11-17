"""Workbench service: transaction lifecycle, queries, snapshots, and replay."""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from .errors import (
    E_CACHE,
    E_CHAIN,
    E_IO,
    E_REPLAY,
    E_SNAPSHOT,
    E_TXN,
    E_VISIBLE,
    E_VERSION,
    BusinessError,
)
from .index import SnapshotCache, VisibilityIndex
from .logging_utils import StructuredLogger
from .metrics import LatencyMetrics
from .storage import Storage
from .util import (
    atomic_write_json,
    canonical_json,
    load_json,
    perf_counter_ns,
    safe_snapshot_name,
    sha256_text,
    utc_now_iso,
)
from .visibility import evaluate_visibility, first_divergence


SNAPSHOT_FORMAT = "mvcc-workbench-snapshot/v1"


class Workbench:
    def __init__(
        self,
        session_dir: str | os.PathLike[str],
        *,
        budget_ms: float = 5.0,
        reset: bool = False,
    ) -> None:
        self.session_dir = Path(session_dir).resolve()
        if reset and self.session_dir.exists():
            shutil.rmtree(self.session_dir)
        self.snapshot_dir = self.session_dir / "snapshots"
        self.log_dir = self.session_dir / "logs"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.session_dir / "session.sqlite3"
        self.logger = StructuredLogger(self.log_dir / "events.jsonl")
        self.storage = Storage(self.db_path)
        self.storage.initialize()
        self.index = VisibilityIndex()
        self.cache = SnapshotCache()
        self.metrics = LatencyMetrics(budget_ms=budget_ms)
        self._lock = threading.RLock()

        recovery = self.storage.mark_all_active_rolled_back()
        self.index.rebuild(self.storage)
        current_version = self.storage.state_version()
        self.cache.invalidate(current_version, "startup")
        if recovery["recovered"]:
            self.logger.event(
                "forced_recovery",
                level="warning",
                recovered_txids=recovery["txids"],
                state_version=recovery["state_version"],
                exit_report_generated=False,
                note="only persisted state was used after unclean start",
            )
        else:
            self.logger.event("startup", state_version=current_version)

    # ------------------------------------------------------------- properties
    @property
    def state_version(self) -> int:
        return self.storage.state_version()

    def close(self) -> None:
        self.logger.event("session_closed", exit_report_generated=False)

    def _mutated(self, state_version: int, reason: str) -> None:
        self.cache.invalidate(state_version, reason)

    # ------------------------------------------------------------- lifecycle
    def create_table(self, table_name: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            result = self.storage.create_table(table_name, columns)
            if result["created"]:
                self.index.apply_table(table_name)
                self._mutated(int(result["state_version"]), "table_schema_changed")
            self.logger.event("table_declared", table=table_name, created=result["created"])
            return result

    def begin_transaction(self, label: str, expected_txid: int | None = None) -> dict[str, Any]:
        with self._lock:
            result = self.storage.begin_transaction(label, expected_txid)
            self.index.apply_event(
                result["txid"], "begin", result["begin_version"], result["begin_version"]
            )
            self._mutated(result["begin_version"], "transaction_begun")
            self.logger.event(
                "txn_begin",
                txid=result["txid"],
                label=label,
                active=result["active"],
                snapshot_xmin=result["snapshot_xmin"],
                snapshot_xmax=result["snapshot_xmax"],
                state_version=result["begin_version"],
            )
            return result

    def commit(self, txid: int) -> dict[str, Any]:
        with self._lock:
            result = self.storage.end_transaction(txid, "committed")
            self.index.apply_event(txid, "commit", result["end_version"], result["end_version"])
            self._mutated(result["end_version"], "transaction_committed")
            self.logger.event("txn_commit", txid=txid, state_version=result["end_version"])
            return result

    def rollback(self, txid: int) -> dict[str, Any]:
        with self._lock:
            result = self.storage.end_transaction(txid, "rolled_back")
            self.index.apply_event(txid, "rollback", result["end_version"], result["end_version"])
            self._mutated(result["end_version"], "transaction_rolled_back")
            self.logger.event("txn_rollback", txid=txid, state_version=result["end_version"])
            return result

    def write(
        self,
        txid: int,
        table_name: str,
        row_key: str,
        value: Any,
    ) -> dict[str, Any]:
        with self._lock:
            result = self.storage.write_version(
                txid=txid, table_name=table_name, row_key=row_key, value=value
            )
            self.index.apply_version(result)
            self._mutated(result["state_version"], "version_written")
            self.logger.event(
                "version_write",
                txid=txid,
                table=table_name,
                key=row_key,
                version_id=result["version_id"],
                chain_position=result["chain_position"],
                state_version=result["state_version"],
            )
            return result

    def delete(self, txid: int, table_name: str, row_key: str) -> dict[str, Any]:
        with self._lock:
            result = self.storage.write_version(
                txid=txid, table_name=table_name, row_key=row_key, value=None, tombstone=True
            )
            self.index.apply_version(result)
            self._mutated(result["state_version"], "tombstone_written")
            self.logger.event(
                "version_delete",
                txid=txid,
                table=table_name,
                key=row_key,
                version_id=result["version_id"],
                chain_position=result["chain_position"],
                state_version=result["state_version"],
            )
            return result

    def auto_commit_write(
        self,
        *,
        table_name: str,
        row_key: str,
        value: Any,
        label: str,
    ) -> dict[str, Any]:
        with self._lock:
            result = self.storage.auto_commit_write(
                table_name=table_name, row_key=row_key, value=value, label=label
            )
            version = result["state_version"]
            self.index.apply_event(result["txid"], "begin", version, version)
            self.index.apply_version(result["version"])
            self.index.apply_event(result["txid"], "commit", version, version)
            self._mutated(version, "background_transaction_committed")
            self.logger.event(
                "background_commit",
                txid=result["txid"],
                table=table_name,
                key=row_key,
                version_id=result["version"]["version_id"],
                state_version=version,
            )
            return result

    def transactions(self) -> list[dict[str, Any]]:
        with self._lock:
            return self.storage.list_transactions()

    def transaction(self, txid: int) -> dict[str, Any]:
        with self._lock:
            return self.storage.get_transaction(txid)

    # --------------------------------------------------------------- queries
    def _context(self, txid: int | None) -> dict[str, Any]:
        """Capture one view version and one coherent snapshot under the service lock."""

        meta = self.storage.meta()
        view_version = int(meta["state_version"])
        state_version = view_version
        if txid is None:
            states = self.index.tx_states_at(view_version)
            next_txid = self.index.next_txid_at(view_version)
            active = sorted(tid for tid, state in states.items() if state == "active")
            snapshot = {
                "xmin": min(active) if active else next_txid,
                "xmax": next_txid,
                "active": active,
            }
            return {
                "mode": "latest",
                "reader_txid": 0,
                "view_version": view_version,
                "state_version": state_version,
                "snapshot": snapshot,
            }
        tx = self.storage.get_transaction(int(txid))
        return {
            "mode": "transaction",
            "reader_txid": int(txid),
            "view_version": view_version,
            "state_version": state_version,
            "snapshot": {
                "xmin": int(tx["snapshot_xmin"]),
                "xmax": int(tx["snapshot_xmax"]),
                "active": sorted(int(value) for value in tx["active"]),
            },
            "transaction": tx,
        }

    def _fast_result(self, context: dict[str, Any], table: str, key: str) -> tuple[dict[str, Any], bool]:
        cache_key = self.cache.key_for(
            view_version=context["view_version"],
            snapshot=context["snapshot"],
            reader_txid=context["reader_txid"],
            table=table,
            key=key,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached, True
        versions = self.index.chain_at(table, key, context["view_version"])
        states = self.index.tx_states_at(context["view_version"])
        result = evaluate_visibility(
            table=table,
            key=key,
            reader_txid=context["reader_txid"],
            view_version=context["view_version"],
            snapshot=context["snapshot"],
            versions=versions,
            tx_states=states,
        )
        self.cache.put(cache_key, result)
        return result, False

    def _authoritative_result(self, context: dict[str, Any], table: str, key: str) -> dict[str, Any]:
        replay = self.storage.load_replay_view(table, key, context["view_version"])
        return evaluate_visibility(
            table=table,
            key=key,
            reader_txid=context["reader_txid"],
            view_version=context["view_version"],
            snapshot=context["snapshot"],
            versions=replay["chain"],
            tx_states=replay["tx_states"],
        )

    def _history_record(
        self,
        *,
        context: dict[str, Any],
        table: str,
        key: str,
        result: dict[str, Any],
        decision_source: str,
        latency_us: int | None,
        degradation_reason: str | None,
    ) -> dict[str, Any]:
        return {
            "txid": context["reader_txid"],
            "mode": context["mode"],
            "table": table,
            "key": key,
            "view_version": context["view_version"],
            "snapshot_xmin": context["snapshot"]["xmin"],
            "snapshot_xmax": context["snapshot"]["xmax"],
            "active": context["snapshot"]["active"],
            "result": result,
            "canonical": canonical_json(result),
            "decision_source": decision_source,
            "latency_us": latency_us,
            "degradation_reason": degradation_reason,
        }

    def query(
        self,
        table: str,
        key: str,
        *,
        txid: int | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        start = perf_counter_ns()
        with self._lock:
            context = self._context(txid)
            self.cache.ensure_version(context["state_version"])
            result, cache_hit = self._fast_result(context, table, key)
            end = perf_counter_ns()
            elapsed_us = self.metrics.elapsed_us(start, end)
            timing = self.metrics.record(elapsed_us)
            degradation = timing["degradation_reason"]
            if degradation:
                self.logger.event(
                    "query_degraded",
                    level="warning",
                    reason=degradation,
                    elapsed_us=elapsed_us,
                    budget_us=timing["budget_us"],
                    table=table,
                    key=key,
                    txid=context["reader_txid"],
                    cache_hit=cache_hit,
                )
            if record:
                self.storage.append_history(
                    self._history_record(
                        context=context,
                        table=table,
                        key=key,
                        result=result,
                        decision_source="fast",
                        latency_us=elapsed_us,
                        degradation_reason=degradation,
                    )
                )
            return {
                "path": "fast",
                "cache_hit": cache_hit,
                "result": result,
                "canonical": canonical_json(result),
                "canonical_sha256": sha256_text(canonical_json(result)),
                "timing": timing,
                "cache": self.cache.stats(),
            }

    def query_latest(self, table: str, key: str, *, record: bool = True) -> dict[str, Any]:
        return self.query(table, key, txid=None, record=record)

    def verify(
        self,
        table: str,
        key: str,
        *,
        txid: int | None = None,
        context: dict[str, Any] | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        start = perf_counter_ns()
        with self._lock:
            if context is None:
                context = self._context(txid)
            self.cache.ensure_version(context["state_version"])
            fast, cache_hit = self._fast_result(context, table, key)
            fast_end = perf_counter_ns()
            authoritative = self._authoritative_result(context, table, key)
            fast_us = self.metrics.elapsed_us(start, fast_end)
            timing = self.metrics.record(fast_us)
            degradation = timing["degradation_reason"]
            fast_canonical = canonical_json(fast)
            authoritative_canonical = canonical_json(authoritative)
            if fast_canonical != authoritative_canonical:
                divergence = first_divergence(fast, authoritative)
                self.logger.event(
                    "visibility_mismatch",
                    level="error",
                    code=E_VISIBLE,
                    table=table,
                    key=key,
                    txid=context["reader_txid"],
                    view_version=context["view_version"],
                    first_divergence=divergence,
                )
                raise BusinessError(
                    E_VISIBLE,
                    "fast visibility result differs from authoritative replay",
                    {
                        "table": table,
                        "key": key,
                        "txid": context["reader_txid"],
                        "view_version": context["view_version"],
                        "first_divergence": divergence,
                    },
                )
            if degradation:
                self.logger.event(
                    "query_degraded",
                    level="warning",
                    reason=degradation,
                    elapsed_us=fast_us,
                    budget_us=timing["budget_us"],
                    table=table,
                    key=key,
                    txid=context["reader_txid"],
                    cache_hit=cache_hit,
                )
            if record:
                self.storage.append_history(
                    self._history_record(
                        context=context,
                        table=table,
                        key=key,
                        result=authoritative,
                        decision_source="authoritative",
                        latency_us=fast_us,
                        degradation_reason=degradation,
                    )
                )
            return {
                "path": "verified",
                "cache_hit": cache_hit,
                "fast": fast,
                "authoritative": authoritative,
                "fast_canonical": fast_canonical,
                "authoritative_canonical": authoritative_canonical,
                "canonical_equal": True,
                "txid": context["reader_txid"],
                "view_version": context["view_version"],
                "timing": timing,
                "cache": self.cache.stats(),
                "metrics": self.metrics.aggregate(),
            }

    def verify_latest(self, table: str, key: str, *, record: bool = True) -> dict[str, Any]:
        return self.verify(table, key, txid=None, record=record)

    def metrics_summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "latency": self.metrics.aggregate(),
                "cache": self.cache.stats(),
                "state_version": self.storage.state_version(),
            }

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return self.storage.list_history(limit=limit)

    # --------------------------------------------------------------- snapshots
    def snapshot_path(self, name: str) -> Path:
        safe = safe_snapshot_name(name)
        return self.snapshot_dir / f"{safe}.mvccsnap.json"

    @staticmethod
    def _snapshot_payload(data: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in data.items() if key != "checksum"}

    def save_snapshot(self, name: str, *, overwrite: bool = False) -> dict[str, Any]:
        path = self.snapshot_path(name)
        with self._lock:
            if path.exists() and not overwrite:
                raise BusinessError(
                    E_SNAPSHOT,
                    "named snapshot already exists; snapshots are immutable unless --force is used",
                    {"name": name, "path": str(path)},
                )
            state_version = self.storage.state_version()
            state = self.storage.export_state()
            transactions = [dict(row) for row in state["transactions"]]
            for row in transactions:
                row["active"] = json.loads(row["active_json"])
            active_snapshots = [
                {
                    "txid": row["txid"],
                    "state": row["state"],
                    "snapshot_xmin": row["snapshot_xmin"],
                    "snapshot_xmax": row["snapshot_xmax"],
                    "active": json.loads(row["active_json"]),
                    "begin_version": row["begin_version"],
                }
                for row in transactions
                if row["state"] == "active"
            ]
            histories = self.storage.list_history()
            payload = {
                "format": SNAPSHOT_FORMAT,
                "name": safe_snapshot_name(name),
                "created_at_utc": utc_now_iso(),
                "session_version": state_version,
                "cache_version": self.cache.stats()["bound_state_version"],
                "db_schema_version": 1,
                "state": state,
                "active_snapshots": active_snapshots,
                "query_sequence": [
                    {
                        "query_id": history["query_id"],
                        "seq": history["seq"],
                        "txid": history["txid"],
                        "table": history["table_name"],
                        "key": history["row_key"],
                        "view_version": history["view_version"],
                    }
                    for history in histories
                ],
                "history_summary": {
                    "count": len(histories),
                    "decision_sources": sorted({history["decision_source"] for history in histories}),
                    "last_query_id": histories[-1]["query_id"] if histories else None,
                },
                "history": histories,
            }
            payload["checksum"] = sha256_text(canonical_json(self._snapshot_payload(payload)))
            atomic_write_json(path, payload)
            self.logger.event(
                "snapshot_saved",
                name=name,
                path=str(path),
                session_version=state_version,
                checksum=payload["checksum"],
                read_only=True,
            )
            return {
                "name": name,
                "path": str(path),
                "session_version": state_version,
                "checksum": payload["checksum"],
                "transaction_count": len(transactions),
                "version_count": len(state["versions"]),
                "history_count": len(histories),
                "active_snapshots": active_snapshots,
            }

    def load_snapshot_file(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(E_IO, "cannot read snapshot", {"path": str(path), "error": str(exc)})
        self._validate_snapshot(data, str(path))
        return data

    def _validate_snapshot(self, data: dict[str, Any], source: str) -> None:
        if data.get("format") != SNAPSHOT_FORMAT:
            raise BusinessError(E_SNAPSHOT, "unsupported snapshot format", {"source": source})
        checksum = data.get("checksum")
        expected = sha256_text(canonical_json(self._snapshot_payload(data)))
        if checksum != expected:
            raise BusinessError(
                E_SNAPSHOT,
                "snapshot checksum mismatch",
                {"source": source, "expected": expected, "actual": checksum},
            )
        required = {"state", "session_version", "cache_version", "history"}
        missing = sorted(required - set(data))
        if missing:
            raise BusinessError(E_SNAPSHOT, "snapshot is incomplete", {"source": source, "missing": missing})
        if int(data["cache_version"]) != int(data["session_version"]):
            raise BusinessError(
                E_CACHE,
                "snapshot cache version is not bound to the snapshot session version",
                {
                    "source": source,
                    "cache_version": int(data["cache_version"]),
                    "session_version": int(data["session_version"]),
                },
            )
        state = data["state"]
        for key in ("meta", "tables", "transactions", "txn_events", "versions", "row_heads", "history"):
            if key not in state:
                raise BusinessError(E_SNAPSHOT, "snapshot state section is missing", {"section": key})
        session_version = int(data["session_version"])
        events = sorted(state["txn_events"], key=lambda row: int(row["event_id"]))
        begin_txids = [int(row["txid"]) for row in events if row["event"] == "begin"]
        if begin_txids != sorted(begin_txids) or len(begin_txids) != len(set(begin_txids)):
            raise BusinessError(
                E_TXN,
                "transaction numbers roll back or repeat in the persisted event log",
                {"begin_txids": begin_txids, "source": source},
            )
        if any(int(row["state_version"]) > session_version for row in events):
            raise BusinessError(
                E_TXN,
                "event log contains a future transaction-number version",
                {"session_version": session_version, "source": source},
            )
        max_txid = max((int(row["txid"]) for row in state["transactions"]), default=0)
        next_txid = int(state["meta"].get("next_txid", "1"))
        if next_txid <= max_txid:
            raise BusinessError(
                E_TXN,
                "next transaction number has rolled back behind an existing transaction",
                {"next_txid": next_txid, "max_txid": max_txid, "source": source},
            )
        for row in state["versions"]:
            if int(row["created_version"]) > session_version:
                raise BusinessError(
                    E_TXN,
                    "version was created after the snapshot session version",
                    {"version_id": row["version_id"], "session_version": session_version},
                )
        self._validate_snapshot_chains(state, source)
        for record in state["history"]:
            try:
                if canonical_json(json.loads(record["result_json"])) != record["canonical"]:
                    raise ValueError("canonical mismatch")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise BusinessError(
                    E_SNAPSHOT,
                    "history canonical payload is invalid",
                    {"query_id": record.get("query_id"), "error": str(exc), "source": source},
                )

    @staticmethod
    def _validate_snapshot_chains(state: dict[str, Any], source: str) -> None:
        by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in state["versions"]:
            by_key.setdefault((row["table_name"], row["row_key"]), []).append(row)
        heads = {(row["table_name"], row["row_key"]): row for row in state["row_heads"]}
        if set(heads) != set(by_key):
            raise BusinessError(
                E_CHAIN,
                "snapshot row-head set does not match its version chains",
                {
                    "source": source,
                    "heads_without_versions": sorted(str(key) for key in set(heads) - set(by_key)),
                    "versions_without_heads": sorted(str(key) for key in set(by_key) - set(heads)),
                },
            )
        for row_key, rows in by_key.items():
            by_id = {int(row["version_id"]): row for row in rows}
            head = heads[row_key]
            current: int | None = int(head["head_version_id"])
            visited: list[int] = []
            seen: set[int] = set()
            position = 0
            while current is not None:
                position += 1
                if current in seen:
                    raise BusinessError(
                        E_CHAIN,
                        "snapshot contains a version-chain cycle",
                        {"table": row_key[0], "key": row_key[1], "version_id": current,
                         "chain_position": position, "source": source},
                    )
                if current not in by_id:
                    raise BusinessError(
                        E_CHAIN,
                        "snapshot chain points to a missing version",
                        {"table": row_key[0], "key": row_key[1], "version_id": current,
                         "chain_position": position, "source": source},
                    )
                seen.add(current)
                visited.append(current)
                row = by_id[current]
                prev_id = row["prev_version_id"]
                if prev_id is not None:
                    prev = by_id.get(int(prev_id))
                    if prev is None or prev["next_version_id"] != current:
                        raise BusinessError(
                            E_CHAIN,
                            "snapshot version pointers disagree",
                            {"table": row_key[0], "key": row_key[1], "version_id": current,
                             "prev_version_id": prev_id, "chain_position": position,
                             "source": source},
                        )
                current = int(prev_id) if prev_id is not None else None
            chain = [by_id[version_id] for version_id in reversed(visited)]
            if (
                len(chain) != len(rows)
                or int(head["tail_version_id"]) != int(chain[0]["version_id"])
                or int(head["chain_len"]) != len(chain)
            ):
                raise BusinessError(
                    E_CHAIN,
                    "snapshot chain metadata disagrees with its pointer traversal",
                    {"table": row_key[0], "key": row_key[1], "source": source},
                )
            for index in range(1, len(chain)):
                if int(chain[index - 1]["xmin"]) >= int(chain[index]["xmin"]):
                    raise BusinessError(
                        E_VERSION,
                        "transaction ids are not monotonic in the snapshot chain",
                        {"table": row_key[0], "key": row_key[1], "chain_position": index + 1,
                         "source": source},
                    )

    def restore_snapshot_path(self, path: str | os.PathLike[str], *, replay: bool = False) -> dict[str, Any]:
        data = self.load_snapshot_file(path)
        with self._lock:
            current_version = self.storage.state_version()
            snapshot_version = int(data["session_version"])
            self.storage.replace_state(data["state"], snapshot_version)
            self.index.rebuild(self.storage)
            new_version = self.storage.state_version()
            self.cache.invalidate(new_version, "snapshot_restore_whole_replacement")
            self.logger.event(
                "snapshot_restored",
                path=str(path),
                current_version_before=current_version,
                snapshot_version=snapshot_version,
                state_version_after=new_version,
                version_mismatch=current_version != snapshot_version,
                strategy="whole_replacement",
                old_snapshot_preserved_read_only=True,
            )
            result = {
                "path": str(path),
                "name": data.get("name"),
                "current_version_before": current_version,
                "snapshot_version": snapshot_version,
                "state_version_after": new_version,
                "version_mismatch": current_version != snapshot_version,
                "strategy": "whole_replacement",
                "active_snapshots": data.get("active_snapshots", []),
            }
            if replay:
                result["replay"] = self._replay_history_locked()
            return result

    def restore_snapshot(self, name: str, *, replay: bool = False) -> dict[str, Any]:
        return self.restore_snapshot_path(self.snapshot_path(name), replay=replay)

    # ----------------------------------------------------------------- replay
    @staticmethod
    def _history_snapshot(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "xmin": int(record["snapshot_xmin"]),
            "xmax": int(record["snapshot_xmax"]),
            "active": sorted(int(value) for value in json.loads(record["active_json"]))
            if isinstance(record.get("active_json"), str)
            else sorted(int(value) for value in record["active"]),
        }

    def _replay_history_locked(self) -> dict[str, Any]:
        histories = self.storage.list_history()
        checked = 0
        for record in histories:
            snapshot = self._history_snapshot(record)
            replay = self.storage.load_replay_view(
                record["table_name"], record["row_key"], int(record["view_version"])
            )
            result = evaluate_visibility(
                table=record["table_name"],
                key=record["row_key"],
                reader_txid=int(record["txid"]),
                view_version=int(record["view_version"]),
                snapshot=snapshot,
                versions=replay["chain"],
                tx_states=replay["tx_states"],
            )
            canonical = canonical_json(result)
            if canonical != record["canonical"]:
                try:
                    saved_result = json.loads(record["canonical"])
                except (TypeError, json.JSONDecodeError) as exc:
                    saved_result = {"invalid_saved_canonical": repr(exc)}
                divergence = first_divergence(saved_result, result)
                self.logger.event(
                    "history_replay_mismatch",
                    level="error",
                    code=E_REPLAY,
                    query_id=record["query_id"],
                    table=record["table_name"],
                    key=record["row_key"],
                    txid=record["txid"],
                    first_divergence=divergence,
                )
                raise BusinessError(
                    E_REPLAY,
                    "saved visibility decision cannot be reproduced",
                    {
                        "query_id": record["query_id"],
                        "table": record["table_name"],
                        "key": record["row_key"],
                        "txid": record["txid"],
                        "view_version": record["view_version"],
                        "first_divergence": divergence,
                    },
                )
            checked += 1
        summary = {
            "checked": checked,
            "matched": checked,
            "mismatched": 0,
            "state_version": self.storage.state_version(),
        }
        self.logger.event("history_replay_ok", **summary)
        return summary

    def replay_history(self) -> dict[str, Any]:
        with self._lock:
            return self._replay_history_locked()

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            meta = self.storage.meta()
            return {
                "session_dir": str(self.session_dir),
                "db_path": str(self.db_path),
                "state_version": int(meta["state_version"]),
                "next_txid": int(meta["next_txid"]),
                "schema_version": int(meta["schema_version"]),
                "meta": meta,
                "metrics": self.metrics.aggregate(),
                "cache": self.cache.stats(),
            }
