"""SQLite ledger with atomic import and per-case adjudication transactions."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .closure import compute_closure
from .codes import (
    E_BAD_SCHEMA,
    E_LEDGER_CORRUPT,
    E_NO_SNAPSHOT,
    SCHEMA_VERSION,
)
from .engine import adjudicate_bundle
from .mapping import reconcile_mapping
from .models import (
    AuditError,
    CaseConclusion,
    SnapshotBundle,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sample_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    rule_generation INTEGER NOT NULL,
    mapping_generation INTEGER NOT NULL,
    rule_fingerprint TEXT NOT NULL,
    mapping_fingerprint TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    closure_json TEXT NOT NULL,
    change_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    sync_marker TEXT,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_results (
    case_id TEXT PRIMARY KEY REFERENCES cases(case_id) ON DELETE CASCADE,
    result_json TEXT NOT NULL,
    revision_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS adjudication_hits (
    case_id TEXT NOT NULL REFERENCES case_results(case_id) ON DELETE CASCADE,
    turn TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    action TEXT,
    basis_generation INTEGER,
    PRIMARY KEY (case_id, turn, rule_key)
);

CREATE TABLE IF NOT EXISTS pending_cases (
    case_id TEXT PRIMARY KEY
);
"""


def _dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _loads(text: str | None, default: Any) -> Any:
    if text is None:
        return default
    return json.loads(text)


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA synchronous = FULL")
        with self.transaction():
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    self.connection.execute(statement)
            self._set_meta_default("change_seq", 0)
            self._set_meta_default("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _set_meta_default(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (key, _dumps(value)),
        )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return default if row is None else _loads(row["value"], default)

    def _set_meta(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, _dumps(value)),
        )

    def change_seq(self) -> int:
        return int(self.get_meta("change_seq", 0))

    def _bump_change_seq(self) -> int:
        sequence = self.change_seq() + 1
        self._set_meta("change_seq", sequence)
        return sequence

    def load_bundle(self) -> SnapshotBundle | None:
        row = self.connection.execute(
            "SELECT bundle_json FROM snapshot WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        try:
            return SnapshotBundle.from_dict(json.loads(row["bundle_json"]))
        except Exception as exc:
            raise AuditError(
                E_LEDGER_CORRUPT,
                f"stored snapshot cannot be decoded: {exc}",
                source_category="ledger",
            ) from exc

    def require_bundle(self) -> SnapshotBundle:
        bundle = self.load_bundle()
        if bundle is None:
            raise AuditError(
                E_NO_SNAPSHOT,
                "ledger contains no imported snapshot",
                source_category="ledger",
            )
        return bundle

    def snapshot_info(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT sample_id, snapshot_id, rule_generation, mapping_generation, "
            "rule_fingerprint, mapping_fingerprint, closure_json, change_seq "
            "FROM snapshot WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "sample_id": row["sample_id"],
            "snapshot_id": row["snapshot_id"],
            "rule_generation": int(row["rule_generation"]),
            "mapping_generation": int(row["mapping_generation"]),
            "rule_fingerprint": row["rule_fingerprint"],
            "mapping_fingerprint": row["mapping_fingerprint"],
            "closure": _loads(row["closure_json"], {}),
            "change_seq": int(row["change_seq"]),
        }

    def import_bundle(self, bundle: SnapshotBundle) -> dict[str, Any]:
        """Validate and import a complete replacement in one transaction.

        Parsing is completed by the caller before this method is entered. Any
        exception inside the transaction rolls the ledger back to its prior state.
        """

        with self.transaction():
            old = self.load_bundle()
            old_results = self.load_case_results()
            closure = compute_closure(old, bundle, old_results)
            replacement = closure["snapshot_replacement"]
            sequence = self.change_seq() + 1
            self._set_meta("change_seq", sequence)
            bundle_json = _dumps(bundle.to_dict())
            self.connection.execute(
                "INSERT INTO snapshot("
                "id, sample_id, snapshot_id, rule_generation, "
                "mapping_generation, rule_fingerprint, mapping_fingerprint, "
                "bundle_json, closure_json, change_seq"
                ") VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "sample_id=excluded.sample_id, "
                "snapshot_id=excluded.snapshot_id, "
                "rule_generation=excluded.rule_generation, "
                "mapping_generation=excluded.mapping_generation, "
                "rule_fingerprint=excluded.rule_fingerprint, "
                "mapping_fingerprint=excluded.mapping_fingerprint, "
                "bundle_json=excluded.bundle_json, "
                "closure_json=excluded.closure_json, "
                "change_seq=excluded.change_seq",
                (
                    bundle.sample_id,
                    bundle.snapshot_id,
                    bundle.rule_generation,
                    bundle.mapping_generation,
                    bundle.rule_set_fingerprint(),
                    bundle.mapping_fingerprint(),
                    bundle_json,
                    _dumps(closure),
                    sequence,
                ),
            )
            existing = {
                row["case_id"]: row["content_hash"]
                for row in self.connection.execute(
                    "SELECT case_id, content_hash FROM cases"
                )
            }
            new_ids = {case.case_id for case in bundle.cases}
            removed = sorted(set(existing) - new_ids)
            if removed:
                placeholders = ",".join("?" for _ in removed)
                self.connection.execute(
                    f"DELETE FROM cases WHERE case_id IN ({placeholders})",
                    removed,
                )
            for case in bundle.cases:
                case_hash = case.content_hash()
                changed = existing.get(case.case_id) != case_hash
                self.connection.execute(
                    "INSERT INTO cases(case_id, sync_marker, content_hash, payload_json) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(case_id) DO UPDATE SET "
                    "sync_marker=excluded.sync_marker, "
                    "content_hash=excluded.content_hash, "
                    "payload_json=excluded.payload_json",
                    (
                        case.case_id,
                        case.sync_marker,
                        case_hash,
                        _dumps(case.to_dict()),
                    ),
                )
                if changed:
                    self.connection.execute(
                        "DELETE FROM case_results WHERE case_id = ?",
                        (case.case_id,),
                    )
            self.connection.execute("DELETE FROM pending_cases")
            self.connection.executemany(
                "INSERT OR IGNORE INTO pending_cases(case_id) VALUES (?)",
                [(case_id,) for case_id in closure["affected_cases"]],
            )
            self._set_meta("last_recomputed_cases", [])
            self._set_meta("last_recompute_mode", None)
            self._set_meta("last_audit_report", None)
            self._set_meta("adjudication_state", "pending")
            return {
                "sequence": sequence,
                "closure": closure,
                "replacement": replacement,
                "pending_cases": list(closure["affected_cases"]),
            }

    def pending_cases(self) -> list[str]:
        return [
            row["case_id"]
            for row in self.connection.execute(
                "SELECT case_id FROM pending_cases ORDER BY case_id"
            )
        ]

    def load_case_results(self) -> dict[str, CaseConclusion]:
        results: dict[str, CaseConclusion] = {}
        for row in self.connection.execute(
            "SELECT case_id, result_json FROM case_results ORDER BY case_id"
        ):
            try:
                results[row["case_id"]] = CaseConclusion.from_dict(
                    json.loads(row["result_json"])
                )
            except Exception as exc:
                raise AuditError(
                    E_LEDGER_CORRUPT,
                    f"case result {row['case_id']!r} cannot be decoded: {exc}",
                    source_category="ledger",
                ) from exc
        return results

    def _replace_case_result(
        self,
        result: CaseConclusion,
        *,
        revision_seq: int,
    ) -> None:
        self.connection.execute(
            "DELETE FROM case_results WHERE case_id = ?", (result.case_id,)
        )
        self.connection.execute(
            "INSERT INTO case_results(case_id, result_json, revision_seq) "
            "VALUES (?, ?, ?)",
            (result.case_id, _dumps(result.to_dict()), revision_seq),
        )
        hit_rows: list[tuple[str, str, str, str | None, int | None]] = []
        for turn, side_result in (
            ("v1", result.v1),
            ("v2", result.v2),
            ("unified", result.unified),
        ):
            for hit in side_result.hits:
                hit_rows.append(
                    (
                        result.case_id,
                        turn,
                        str(hit["rule_key"]),
                        hit.get("action"),
                        hit.get("basis_generation"),
                    )
                )
        self.connection.executemany(
            "INSERT INTO adjudication_hits("
            "case_id, turn, rule_key, action, basis_generation"
            ") VALUES (?, ?, ?, ?, ?)",
            hit_rows,
        )

    def adjudicate(self, *, jobs: int = 1, full: bool = False) -> dict[str, Any]:
        """Recompute pending cases (or all cases when full=True)."""

        bundle = self.require_bundle()
        sequence = self.change_seq()
        snapshot_row = self.connection.execute(
            "SELECT change_seq FROM snapshot WHERE id = 1"
        ).fetchone()
        if snapshot_row is None or int(snapshot_row["change_seq"]) != sequence:
            raise AuditError(
                E_LEDGER_CORRUPT,
                "snapshot and ledger sequence are inconsistent",
                source_category="ledger",
            )
        selected = (
            [case.case_id for case in bundle.cases]
            if full
            else self.pending_cases()
        )
        selected = sorted(set(selected) & set(bundle.case_map()))
        computed, mapping = adjudicate_bundle(bundle, case_ids=selected, jobs=jobs)
        if self.change_seq() != sequence:
            raise AuditError(
                E_LEDGER_CORRUPT,
                "snapshot changed while cases were being computed",
                source_category="ledger",
            )
        kill_after = int(
            os.environ.get("FIREWALL_AUDIT_TEST_KILL_AFTER_CASE_COMMITS", "0") or 0
        )
        applied = 0
        for case_id in selected:
            with self.transaction():
                current = self.connection.execute(
                    "SELECT change_seq FROM snapshot WHERE id = 1"
                ).fetchone()
                if current is None or int(current["change_seq"]) != sequence:
                    raise AuditError(
                        E_LEDGER_CORRUPT,
                        "snapshot changed during adjudication",
                        source_category="ledger",
                    )
                self._replace_case_result(
                    computed[case_id],
                    revision_seq=sequence + 1,
                )
                self._set_meta("adjudication_progress", applied + 1)
            applied += 1
            if kill_after and applied >= kill_after:
                os._exit(137)
        with self.transaction():
            if self.change_seq() != sequence:
                raise AuditError(
                    E_LEDGER_CORRUPT,
                    "snapshot changed during adjudication",
                    source_category="ledger",
                )
            sequence = self._bump_change_seq()
            self.connection.execute(
                "UPDATE snapshot SET change_seq = ? WHERE id = 1",
                (sequence,),
            )
            self.connection.execute("DELETE FROM pending_cases")
            self._set_meta("last_recomputed_cases", selected)
            self._set_meta("last_recompute_mode", "full" if full else "incremental")
            self._set_meta("adjudication_state", "complete")
            self._set_meta("adjudication_progress", len(selected))
            self.connection.execute(
                "UPDATE case_results SET revision_seq = ? WHERE revision_seq = ?",
                (sequence, sequence - 1),
            )
        all_results = self.load_case_results()
        for case_id, result in computed.items():
            all_results[case_id] = result
        closure = (self.snapshot_info() or {}).get("closure", {})
        closure = dict(closure)
        closure["mode"] = "full" if full else "incremental"
        closure["recomputed_cases"] = selected
        closure["unaffected_cases"] = sorted(
            set(bundle.case_map()) - set(selected)
        )
        return {
            "bundle": bundle,
            "mapping": mapping,
            "results": all_results,
            "closure": closure,
            "recomputed_cases": selected,
            "mode": "full" if full else "incremental",
            "sequence": sequence,
        }

    def export_audit(self) -> dict[str, Any]:
        bundle = self.require_bundle()
        mapping = reconcile_mapping(
            bundle.rules,
            bundle.mapping_entries,
            declaration_generation=bundle.mapping_generation,
            conflict_policy=bundle.conflict_policy,
            direction=bundle.mapping_direction,
        )
        closure = dict((self.snapshot_info() or {}).get("closure", {}))
        recomputed = self.get_meta("last_recomputed_cases", [])
        closure["mode"] = self.get_meta("last_recompute_mode", None)
        closure["recomputed_cases"] = recomputed
        closure["unaffected_cases"] = sorted(
            set(bundle.case_map()) - set(recomputed)
        )
        return {
            "bundle": bundle,
            "mapping": mapping,
            "results": self.load_case_results(),
            "closure": closure,
            "recomputed_cases": recomputed,
            "mode": self.get_meta("last_recompute_mode", None),
            "sequence": self.change_seq(),
        }

    def dump_state(self) -> dict[str, Any]:
        meta = {
            row["key"]: _loads(row["value"], None)
            for row in self.connection.execute(
                "SELECT key, value FROM meta ORDER BY key"
            )
        }
        snapshot_rows = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM snapshot ORDER BY id"
            )
        ]
        cases = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM cases ORDER BY case_id"
            )
        ]
        results = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM case_results ORDER BY case_id"
            )
        ]
        hits = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM adjudication_hits "
                "ORDER BY case_id, turn, rule_key"
            )
        ]
        pending = [
            row["case_id"]
            for row in self.connection.execute(
                "SELECT case_id FROM pending_cases ORDER BY case_id"
            )
        ]
        return {
            "meta": meta,
            "snapshot": snapshot_rows,
            "cases": cases,
            "case_results": results,
            "adjudication_hits": hits,
            "pending_cases": pending,
        }

    def replace_state(self, state: dict[str, Any]) -> None:
        required = {
            "meta",
            "snapshot",
            "cases",
            "case_results",
            "adjudication_hits",
            "pending_cases",
        }
        if not isinstance(state, dict) or not required <= set(state):
            raise AuditError(
                E_BAD_SCHEMA,
                "checkpoint state is missing required tables",
                source_category="checkpoint",
            )
        with self.transaction():
            self.connection.execute("DELETE FROM adjudication_hits")
            self.connection.execute("DELETE FROM case_results")
            self.connection.execute("DELETE FROM pending_cases")
            self.connection.execute("DELETE FROM cases")
            self.connection.execute("DELETE FROM snapshot")
            self.connection.execute("DELETE FROM meta")
            for key, value in sorted(state["meta"].items()):
                self.connection.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    (str(key), _dumps(value)),
                )
            for row in state["snapshot"]:
                self.connection.execute(
                    "INSERT INTO snapshot("
                    "id, sample_id, snapshot_id, rule_generation, "
                    "mapping_generation, rule_fingerprint, mapping_fingerprint, "
                    "bundle_json, closure_json, change_seq"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["id"],
                        row["sample_id"],
                        row["snapshot_id"],
                        row["rule_generation"],
                        row["mapping_generation"],
                        row["rule_fingerprint"],
                        row["mapping_fingerprint"],
                        row["bundle_json"],
                        row["closure_json"],
                        row["change_seq"],
                    ),
                )
            for row in state["cases"]:
                self.connection.execute(
                    "INSERT INTO cases(case_id, sync_marker, content_hash, payload_json) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        row["case_id"],
                        row["sync_marker"],
                        row["content_hash"],
                        row["payload_json"],
                    ),
                )
            for row in state["case_results"]:
                self.connection.execute(
                    "INSERT INTO case_results(case_id, result_json, revision_seq) "
                    "VALUES (?, ?, ?)",
                    (row["case_id"], row["result_json"], row["revision_seq"]),
                )
            for row in state["adjudication_hits"]:
                self.connection.execute(
                    "INSERT INTO adjudication_hits("
                    "case_id, turn, rule_key, action, basis_generation"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        row["case_id"],
                        row["turn"],
                        row["rule_key"],
                        row.get("action"),
                        row.get("basis_generation"),
                    ),
                )
            for case_id in state["pending_cases"]:
                self.connection.execute(
                    "INSERT INTO pending_cases(case_id) VALUES (?)", (case_id,)
                )
