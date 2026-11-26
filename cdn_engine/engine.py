from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adjudication import adjudicate_window
from .closure import compute_closure, parse_patch_file
from .errors import EngineError, ErrorCode
from .hashing import sha256_hex, sha256_object
from .locking import file_lock, node_lock_path
from .merge import build_unified_view
from .origin import arbitrate_origin_digest
from .parser import parse_observation_batch, parse_sample, parse_v2_package
from .storage import Store


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class Engine:
    def __init__(self, store: Store) -> None:
        self.store = store

    @property
    def global_lock_path(self) -> Path:
        return Path(str(self.store.path) + ".locks") / "global.lock"

    def global_lock(self):
        return file_lock(self.global_lock_path)

    # ---------- load ----------

    def load_sample(self, sample_dir: str | Path, clock_ms: Optional[int] = None) -> Dict[str, Any]:
        sample = parse_sample(sample_dir)
        manifest_clock = sample["manifest"].get("default_clock_ms")
        if clock_ms is None:
            clock_ms = manifest_clock
        if not isinstance(clock_ms, int) or isinstance(clock_ms, bool):
            raise EngineError(ErrorCode.SCHEMA_VIOLATION, "load requires an integer injected clock")

        sample_digest = sha256_object(
            {
                "manifest": sample["manifest"],
                "v1": sample["v1"]["rules"],
                "v2": sample["v2"]["rules"],
                "nodes": sample["nodes"]["nodes"],
                "observations": sample["observations"],
            }
        )
        with self.global_lock(), self.store.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO loaded_samples(sample_digest, root_path, manifest_version, payload_json, loaded_clock_ms) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    sample_digest,
                    str(sample_dir),
                    sample["manifest"].get("manifest_version", 1),
                    dumps(sample),
                    clock_ms,
                ),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('active_sample_digest', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (sample_digest,),
            )
            for node in sample["nodes"]["nodes"]:
                conn.execute(
                    "INSERT OR REPLACE INTO edge_nodes(node_name, payload_json) VALUES(?, ?)",
                    (node["name"], dumps(node)),
                )
            for obs in sample["observations"]:
                row = conn.execute(
                    "SELECT payload_hash FROM input_observations WHERE seq=?",
                    (obs["seq"],),
                ).fetchone()
                if row and row["payload_hash"] != obs["payload_digest"]:
                    raise EngineError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "observation seq was already submitted with different payload",
                        details={"seq": obs["seq"]},
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO input_observations(seq, observation_id, node_name, rule_name, result, "
                    "content_digest, clock_ms, payload_hash, payload_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        obs["seq"],
                        obs["observation_id"],
                        obs["node"],
                        obs["rule_name"],
                        obs["result"],
                        obs["content_digest"],
                        obs["clock_ms"],
                        obs["payload_digest"],
                        dumps(obs),
                    ),
                )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('sample_manifest', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (dumps(sample["manifest"]),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('active_package_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(sample["manifest"].get("manifest_version", 1)),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('active_min_generation', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(sample["manifest"].get("min_generation", 1)),),
            )
            if sample["v2"].get("ruleset_digest"):
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('active_ruleset_digest', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (sample["v2"]["ruleset_digest"],),
                )
        return {
            "ok": True,
            "command": "load",
            "sample_digest": sample_digest,
            "rules": len(sample["v1"]["rules"]) + len(sample["v2"]["rules"]),
            "nodes": len(sample["nodes"]["nodes"]),
            "observations": len(sample["observations"]),
            "clock_ms": clock_ms,
        }

    def _loaded_sample(self) -> Dict[str, Any]:
        digest = self.store.get_meta("active_sample_digest")
        if not digest:
            raise EngineError(ErrorCode.INPUT_NOT_FOUND, "no sample loaded; run load --sample first")
        row = self.store.conn.execute(
            "SELECT payload_json FROM loaded_samples WHERE sample_digest=?", (digest,)
        ).fetchone()
        if not row:
            raise EngineError(ErrorCode.INPUT_NOT_FOUND, "active sample payload is missing")
        return loads(row["payload_json"])

    def _sample_manifest(self) -> Dict[str, Any]:
        return loads(self.store.get_meta("sample_manifest"), {})

    def resolve_clock(self, clock_ms: Optional[int]) -> int:
        if clock_ms is not None:
            if not isinstance(clock_ms, int) or isinstance(clock_ms, bool) or clock_ms < 0:
                raise EngineError(ErrorCode.CLOCK_ROLLBACK, "clock must be a non-negative integer")
            self.store.advance_watermark(clock_ms)
            return clock_ms
        watermark = self.store.watermark()
        if watermark is not None:
            return watermark
        manifest = self._sample_manifest()
        clock_ms = manifest.get("default_clock_ms")
        if not isinstance(clock_ms, int):
            raise EngineError(ErrorCode.SCHEMA_VIOLATION, "an injected --clock value is required")
        self.store.advance_watermark(clock_ms)
        return clock_ms

    # ---------- views ----------

    def merge(self, clock_ms: Optional[int] = None) -> Dict[str, Any]:
        sample = self._loaded_sample()
        if clock_ms is None:
            clock_ms = sample["manifest"].get("default_clock_ms")
        clock_ms = self.resolve_clock(clock_ms)
        view = build_unified_view(
            sample["v1"]["rules"],
            sample["v2"]["rules"],
            injected_clock_ms=clock_ms,
            created_by="merge",
            package_version=int(sample["manifest"].get("manifest_version", 1)),
        )
        existing = self.store.conn.execute(
            "SELECT view_id FROM view_versions WHERE input_digest=?", (view["input_digest"],)
        ).fetchone()
        if existing:
            view_id = existing["view_id"]
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('active_view_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(view_id),),
                )
            return self.merge_result(view_id, idempotent_replay=True)

        replayed = False
        with self.global_lock():
            with self.store.transaction() as conn:
                locked_existing = conn.execute(
                    "SELECT view_id FROM view_versions WHERE input_digest=?",
                    (view["input_digest"],),
                ).fetchone()
                if locked_existing:
                    view_id = locked_existing["view_id"]
                    replayed = True
                else:
                    next_version = conn.execute(
                        "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM view_versions"
                    ).fetchone()["n"]
                    view_id = self._insert_view(conn, view, next_version)
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('active_view_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(view_id),),
                )
        return self.merge_result(view_id, idempotent_replay=replayed)

    def _insert_view(self, conn: Any, view: Dict[str, Any], version_no: int) -> int:
        cursor = conn.execute(
            "INSERT INTO view_versions(view_uid, version_no, package_version, injected_clock_ms, input_digest, "
            "decision_digest, summary_json, created_by, patch_id, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (
                view["view_uid"],
                version_no,
                view.get("package_version"),
                view["injected_clock_ms"],
                view["input_digest"],
                view["decision_digest"],
                dumps(view["summary"]),
                view.get("created_by", "merge"),
                view.get("patch_id"),
            ),
        )
        view_id = cursor.lastrowid
        for rule in view["rules"]:
            conn.execute(
                "INSERT INTO unified_rules(view_id, name, source_format, generation, declared_clock_ms, "
                "effective_at_ms, tombstone, verify_type, content_digest, dependencies_json, effective_json, "
                "decision_json, rule_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    view_id,
                    rule["name"],
                    rule["source_format"],
                    rule["generation"],
                    rule["declared_clock_ms"],
                    rule["effective_at_ms"],
                    1 if rule["tombstone"] else 0,
                    rule["verify_type"],
                    rule.get("content_digest"),
                    dumps(rule["dependencies"]),
                    dumps(rule["effective_time"]),
                    dumps(rule["decision_basis"]),
                    dumps(rule),
                ),
            )
        return view_id

    def merge_result(self, view_id: int, *, idempotent_replay: bool = False) -> Dict[str, Any]:
        row = self.store.conn.execute("SELECT * FROM view_versions WHERE view_id=?", (view_id,)).fetchone()
        rules = self.get_rules(view_id)
        return {
            "ok": True,
            "command": "merge",
            "idempotent_replay": idempotent_replay,
            "view_id": view_id,
            "view_uid": row["view_uid"],
            "version_no": row["version_no"],
            "decision_digest": row["decision_digest"],
            "input_digest": row["input_digest"],
            "summary": loads(row["summary_json"]),
            "rules": [self._rule_output(rule) for rule in rules],
        }

    def _rule_output(self, rule: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": rule["name"],
            "source_version": rule.get("source_version", rule.get("version")),
            "source_format": rule["source_format"],
            "generation": rule["generation"],
            "declared_clock_ms": rule["declared_clock_ms"],
            "effective_at_ms": rule["effective_at_ms"],
            "original_time": rule.get("original_time"),
            "conversion": rule.get("conversion"),
            "verify_type": rule["verify_type"],
            "dependencies": rule["dependencies"],
            "tombstone": rule["tombstone"],
            "content_digest": rule.get("content_digest"),
            "decision_basis": rule.get("decision_basis"),
        }

    def get_rules(self, view_id: int) -> List[Dict[str, Any]]:
        rows = self.store.conn.execute(
            "SELECT rule_json FROM unified_rules WHERE view_id=? ORDER BY name", (view_id,)
        ).fetchall()
        return [loads(row["rule_json"]) for row in rows]

    def get_rule_map(self, view_id: int) -> Dict[str, Dict[str, Any]]:
        return {rule["name"]: rule for rule in self.get_rules(view_id)}

    def _rules_as_candidates(self, view_id: int) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for rule in self.get_rules(view_id):
            original = rule.get("original_time") or {}
            candidates.append(
                {
                    "name": rule["name"],
                    "version": rule.get("source_version", rule.get("version", "patch")),
                    "source_format": rule["source_format"],
                    "source_priority": rule.get("source_priority", 0),
                    "generation": rule["generation"],
                    "declared_clock_ms": rule["declared_clock_ms"],
                    "effective_at_ms": rule["effective_at_ms"],
                    "effective_time": rule.get("effective_time"),
                    "original_time": original,
                    "relative_effective_seconds": original.get("value", 0)
                    if original.get("type") == "relative_effective_seconds"
                    else 0,
                    "verify_type": rule["verify_type"],
                    "dependencies": rule["dependencies"],
                    "tombstone": rule["tombstone"],
                    "content_digest": rule.get("content_digest"),
                }
            )
        return candidates

    # ---------- patches and invalidation closure ----------

    def apply_patch_file(
        self, patch_path: str | Path, clock_ms: Optional[int] = None
    ) -> Dict[str, Any]:
        patch = parse_patch_file(patch_path, default_clock=clock_ms)
        if clock_ms is not None:
            patch["submitted_clock_ms"] = clock_ms
        patch_id = patch["patch_id"]

        with self.global_lock():
            existing_patch = self.store.conn.execute(
                "SELECT payload_hash, closure_id, view_id FROM patches WHERE patch_id=?",
                (patch_id,),
            ).fetchone()
            if existing_patch:
                if existing_patch["payload_hash"] != patch["payload_hash"]:
                    raise EngineError(
                        ErrorCode.PATCH_IDEMPOTENCY_CONFLICT,
                        "patch_id was already submitted with different content",
                        details={"patch_id": patch_id},
                    )
                closure_row = self.store.conn.execute(
                    "SELECT result_json FROM closures WHERE closure_id=?",
                    (existing_patch["closure_id"],),
                ).fetchone()
                result = loads(closure_row["result_json"])
                return {
                    "ok": True,
                    "command": "patch",
                    "idempotent_replay": True,
                    "active_view_id": self.store.active_view_id(),
                    "closure": result,
                }

            self.store.advance_watermark(patch["submitted_clock_ms"])
            base_view_id = self.store.require_active_view_id()
            old_rules = self.get_rules(base_view_id)
            closure = compute_closure(old_rules, patch)
            closure["base_view_id"] = base_view_id
            closure["conclusion_plan"] = self._build_conclusion_plan(
                base_view_id, closure["affected_rules"]
            )

            candidates = self._rules_as_candidates(base_view_id)
            new_rule_map = {rule["name"]: rule for rule in candidates}
            for change in patch["changes"]:
                if change["op"] == "upsert":
                    new_rule_map[change["name"]] = change["rule"]
                elif change["op"] == "delete":
                    new_rule_map.pop(change["name"], None)
                else:
                    target = new_rule_map.get(change["name"])
                    if target is not None:
                        target["tombstone"] = True

            view = build_unified_view(
                list(new_rule_map.values()),
                [],
                injected_clock_ms=patch["submitted_clock_ms"],
                created_by="patch",
            )
            view["patch_id"] = patch_id
            closure["view_uid"] = view["view_uid"]

            existing_view = self.store.conn.execute(
                "SELECT view_id FROM view_versions WHERE input_digest=?",
                (view["input_digest"],),
            ).fetchone()
            with self.store.transaction() as conn:
                if existing_view:
                    new_view_id = existing_view["view_id"]
                else:
                    next_version = conn.execute(
                        "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM view_versions"
                    ).fetchone()["n"]
                    new_view_id = self._insert_view(conn, view, next_version)
                conn.execute(
                    "INSERT INTO patches(patch_id, payload_hash, payload_json, submitted_clock_ms, "
                    "view_id, closure_id, status) VALUES(?, ?, ?, ?, ?, ?, 'committed')",
                    (
                        patch_id,
                        patch["payload_hash"],
                        dumps(patch["payload"]),
                        patch["submitted_clock_ms"],
                        new_view_id,
                        closure["closure_id"],
                    ),
                )
                conn.execute(
                    "INSERT INTO closures(closure_id, patch_id, view_id, payload_hash, result_json, "
                    "result_digest, created_clock_ms) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        closure["closure_id"],
                        patch_id,
                        new_view_id,
                        patch["payload_hash"],
                        dumps(closure),
                        closure["result_digest"],
                        patch["submitted_clock_ms"],
                    ),
                )
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('active_view_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(new_view_id),),
                )

            return {
                "ok": True,
                "command": "patch",
                "idempotent_replay": False,
                "base_view_id": base_view_id,
                "active_view_id": new_view_id,
                "view_uid": view["view_uid"],
                "decision_digest": view["decision_digest"],
                "closure": closure,
            }

    def _build_conclusion_plan(self, view_id: int, affected_rules: List[str]) -> Dict[str, Any]:
        if not affected_rules:
            return {
                "stale_conclusion_ids": [],
                "stale_windows": [],
                "stale_observation_seqs": [],
                "reusable_conclusion_ids": [],
            }
        placeholders = ",".join("?" for _ in affected_rules)
        conclusion_rows = self.store.conn.execute(
            f"SELECT * FROM conclusions WHERE view_id=? AND rule_name IN ({placeholders}) "
            "ORDER BY node_name, rule_name, start_ms",
            [view_id, *affected_rules],
        ).fetchall()
        reusable_rows = self.store.conn.execute(
            f"SELECT conclusion_id FROM conclusions WHERE view_id=? AND rule_name NOT IN ({placeholders}) "
            "ORDER BY conclusion_id",
            [view_id, *affected_rules],
        ).fetchall()
        stale_windows: List[Dict[str, Any]] = []
        stale_ids: List[int] = []
        stale_seqs: set[int] = set()
        for row in conclusion_rows:
            stale_ids.append(row["conclusion_id"])
            processed = self.store.conn.execute(
                "SELECT seq FROM processed_observations WHERE window_id=? ORDER BY seq",
                (row["window_id"],),
            ).fetchall()
            seqs = [item["seq"] for item in processed]
            stale_seqs.update(seqs)
            stale_windows.append(
                {
                    "source_conclusion_id": row["conclusion_id"],
                    "source_window_id": row["window_id"],
                    "node": row["node_name"],
                    "rule_name": row["rule_name"],
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "verdict_code": row["verdict_code"],
                    "observation_seqs": seqs,
                }
            )
        return {
            "stale_conclusion_ids": stale_ids,
            "stale_windows": stale_windows,
            "stale_observation_seqs": sorted(stale_seqs),
            "reusable_conclusion_ids": [row["conclusion_id"] for row in reusable_rows],
        }

    # ---------- rule package replacement ----------

    def load_package(
        self,
        package_dir: str | Path,
        clock_ms: Optional[int] = None,
        *,
        crash_before_commit: bool = False,
    ) -> Dict[str, Any]:
        package_dir = Path(package_dir)
        manifest_path = package_dir / "package-manifest.json"
        manifest = loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        v2 = parse_v2_package(package_dir, default_clock=clock_ms)
        incoming_version = int(manifest.get("package_version", v2["manifest"].get("package_version", 0)))
        incoming_min_generation = int(
            manifest.get("min_generation", v2["manifest"].get("min_generation", 1))
        )
        if clock_ms is None:
            clock_ms = manifest.get("activated_clock_ms", v2["manifest"].get("declared_clock_ms"))
        if not isinstance(clock_ms, int) or isinstance(clock_ms, bool):
            sample_manifest = self._sample_manifest()
            clock_ms = sample_manifest.get("default_clock_ms")
        clock_ms = self.resolve_clock(clock_ms)

        with self.global_lock():
            active_version = int(self.store.get_meta("active_package_version", "1"))
            active_min_generation = int(self.store.get_meta("active_min_generation", "1"))
            active_digest = self.store.get_meta("active_ruleset_digest")
            active_shards = self._active_shard_map()
            incoming_shards = {item["shard"]: item["sha256"] for item in v2["shards"]}
            shard_diff = {
                "added": sorted(set(incoming_shards) - set(active_shards)),
                "removed": sorted(set(active_shards) - set(incoming_shards)),
                "changed": sorted(
                    name
                    for name in set(active_shards) & set(incoming_shards)
                    if active_shards[name] != incoming_shards[name]
                ),
            }

            if incoming_version < active_version or incoming_min_generation < active_min_generation:
                raise EngineError(
                    ErrorCode.PACKAGE_ROLLBACK,
                    "rule package version or generation floor cannot move backwards",
                    details={
                        "active_package_version": active_version,
                        "incoming_package_version": incoming_version,
                        "active_min_generation": active_min_generation,
                        "incoming_min_generation": incoming_min_generation,
                    },
                )

            package_key = f"package-{incoming_version}-{v2['ruleset_digest'].split(':',1)[1][:12]}"
            if incoming_version == active_version:
                if (active_digest and active_digest != v2["ruleset_digest"]) or any(shard_diff.values()):
                    raise EngineError(
                        ErrorCode.PACKAGE_VERSION_CONFLICT,
                        "same package version has different shard or rule-set digest",
                        details={"package_version": incoming_version, "shard_diff": shard_diff},
                    )
                return {
                    "ok": True,
                    "command": "load_package",
                    "decision": "noop",
                    "package_version": incoming_version,
                    "ruleset_digest": v2["ruleset_digest"],
                    "shards": v2["shards"],
                    "shard_diff": shard_diff,
                }

            active_view_id = self.store.require_active_view_id()
            active_names = set(self.get_rule_map(active_view_id))
            incoming_names = {rule["name"] for rule in v2["rules"]}
            append_only = incoming_names.isdisjoint(active_names)
            requested_mode = manifest.get("update_mode")
            if requested_mode == "incremental_append" and not append_only:
                raise EngineError(
                    ErrorCode.PACKAGE_VERSION_CONFLICT,
                    "package declared incremental_append but contains existing rule names",
                    details={"overlapping_rules": sorted(incoming_names & active_names)},
                )
            if requested_mode in {"full_replace", "incremental_append"}:
                decision = requested_mode
            elif append_only:
                decision = "incremental_append"
            else:
                decision = "full_replace"

            if decision == "incremental_append":
                candidates = self._rules_as_candidates(active_view_id)
                view = build_unified_view(
                    candidates + v2["rules"],
                    [],
                    injected_clock_ms=clock_ms,
                    created_by="package_incremental_append",
                    package_version=incoming_version,
                )
            else:
                sample = self._loaded_sample()
                view = build_unified_view(
                    sample["v1"]["rules"],
                    v2["rules"],
                    injected_clock_ms=clock_ms,
                    created_by="package_full_replace",
                    package_version=incoming_version,
                )

            operation_id = f"package-switch-{incoming_version}-{view['view_uid']}"
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO operations(operation_id, kind, status, payload_json, clock_ms) "
                    "VALUES(?, 'package_switch', 'staging', ?, ?)",
                    (operation_id, dumps({"decision": decision, "package": package_key}), clock_ms),
                )

            self._stage_rules_in_temp_table(view["rules"])
            if crash_before_commit:
                os.kill(os.getpid(), signal.SIGKILL)
            with self.store.transaction() as conn:
                next_version = conn.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM view_versions"
                ).fetchone()["n"]
                new_view_id = self._insert_staged_view(conn, view, next_version)
                conn.execute(
                    "INSERT INTO packages(package_key, package_version, min_generation, ruleset_digest, "
                    "manifest_digest, decision, payload_json, loaded_clock_ms) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        package_key,
                        incoming_version,
                        incoming_min_generation,
                        v2["ruleset_digest"],
                        sha256_object(manifest),
                        decision,
                        dumps({"manifest": manifest, "shards": v2["shards"]}),
                        clock_ms,
                    ),
                )
                for key, value in (
                    ("active_view_id", str(new_view_id)),
                    ("active_package_version", str(incoming_version)),
                    ("active_min_generation", str(incoming_min_generation)),
                    ("active_ruleset_digest", v2["ruleset_digest"]),
                    ("active_package_key", package_key),
                ):
                    conn.execute(
                        "INSERT INTO meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )
                conn.execute(
                    "UPDATE operations SET status='committed', result_json=? WHERE operation_id=?",
                    (dumps({"view_id": new_view_id, "decision": decision}), operation_id),
                )

            return {
                "ok": True,
                "command": "load_package",
                "decision": decision,
                "package_version": incoming_version,
                "min_generation": incoming_min_generation,
                "ruleset_digest": v2["ruleset_digest"],
                "shards": v2["shards"],
                "shard_diff": shard_diff,
                "append_only": append_only,
                "active_view_id": new_view_id,
                "view_uid": view["view_uid"],
                "decision_digest": view["decision_digest"],
            }

    def _active_shard_map(self) -> Dict[str, str]:
        package_key = self.store.get_meta("active_package_key")
        if package_key:
            row = self.store.conn.execute(
                "SELECT payload_json FROM packages WHERE package_key=?", (package_key,)
            ).fetchone()
            if row:
                payload = loads(row["payload_json"])
                return {item["shard"]: item["sha256"] for item in payload.get("shards", [])}
        sample = self._loaded_sample()
        return {item["shard"]: item["sha256"] for item in sample["v2"].get("shards", [])}

    def _stage_rules_in_temp_table(self, rules: List[Dict[str, Any]]) -> None:
        conn = self.store.conn
        conn.execute("DROP TABLE IF EXISTS temp.staged_unified_rules")
        conn.execute(
            """
            CREATE TEMP TABLE staged_unified_rules (
                name TEXT, source_format TEXT, generation INTEGER, declared_clock_ms INTEGER,
                effective_at_ms INTEGER, tombstone INTEGER, verify_type TEXT, content_digest TEXT,
                dependencies_json TEXT, effective_json TEXT, decision_json TEXT, rule_json TEXT
            )
            """
        )
        for rule in rules:
            conn.execute(
                "INSERT INTO temp.staged_unified_rules VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule["name"],
                    rule["source_format"],
                    rule["generation"],
                    rule["declared_clock_ms"],
                    rule["effective_at_ms"],
                    1 if rule["tombstone"] else 0,
                    rule["verify_type"],
                    rule.get("content_digest"),
                    dumps(rule["dependencies"]),
                    dumps(rule["effective_time"]),
                    dumps(rule["decision_basis"]),
                    dumps(rule),
                ),
            )

    def _insert_staged_view(self, conn: Any, view: Dict[str, Any], version_no: int) -> int:
        cursor = conn.execute(
            "INSERT INTO view_versions(view_uid, version_no, package_version, injected_clock_ms, input_digest, "
            "decision_digest, summary_json, created_by, patch_id, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (
                view["view_uid"],
                version_no,
                view.get("package_version"),
                view["injected_clock_ms"],
                view["input_digest"],
                view["decision_digest"],
                dumps(view["summary"]),
                view.get("created_by", "package"),
                view.get("patch_id"),
            ),
        )
        view_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO unified_rules(
                view_id, name, source_format, generation, declared_clock_ms, effective_at_ms,
                tombstone, verify_type, content_digest, dependencies_json, effective_json,
                decision_json, rule_json)
            SELECT ?, name, source_format, generation, declared_clock_ms, effective_at_ms,
                tombstone, verify_type, content_digest, dependencies_json, effective_json,
                decision_json, rule_json
            FROM temp.staged_unified_rules
            """,
            (view_id,),
        )
        return view_id

    # ---------- probing and windows ----------

    def get_nodes(self) -> List[Dict[str, Any]]:
        rows = self.store.conn.execute("SELECT payload_json FROM edge_nodes ORDER BY node_name").fetchall()
        return [loads(row["payload_json"]) for row in rows]

    def import_observation_file(self, path: str | Path) -> Dict[str, Any]:
        observations = parse_observation_batch(path)
        node_names = {node["name"] for node in self.get_nodes()}
        view_id = self.store.active_view_id()
        if view_id is not None:
            rule_names = set(self.get_rule_map(view_id))
        else:
            sample = self._loaded_sample()
            rule_names = {rule["name"] for rule in sample["v1"]["rules"]} | {
                rule["name"] for rule in sample["v2"]["rules"]
            }

        previous_seq: Optional[int] = None
        for obs in observations:
            if previous_seq is not None and obs["seq"] <= previous_seq:
                raise EngineError(
                    ErrorCode.OBS_SEQUENCE_NOT_MONOTONIC,
                    "submitted observation batch must be strictly increasing",
                    details={"seq": obs["seq"], "previous_seq": previous_seq},
                )
            previous_seq = obs["seq"]
            if obs["node"] not in node_names:
                raise EngineError(
                    ErrorCode.OBS_REFERENCE_UNKNOWN,
                    "submitted observation references unknown node",
                    details={"seq": obs["seq"], "node": obs["node"]},
                )
            if obs["rule_name"] not in rule_names:
                raise EngineError(
                    ErrorCode.OBS_REFERENCE_UNKNOWN,
                    "submitted observation references unknown rule",
                    details={"seq": obs["seq"], "rule": obs["rule_name"]},
                )

        inserted = 0
        duplicate = 0
        with self.global_lock(), self.store.transaction() as conn:
            for obs in observations:
                existing = conn.execute(
                    "SELECT payload_hash FROM input_observations WHERE seq=?", (obs["seq"],)
                ).fetchone()
                if existing:
                    if existing["payload_hash"] != obs["payload_digest"]:
                        raise EngineError(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            "observation seq was already submitted with different payload",
                            details={"seq": obs["seq"]},
                        )
                    duplicate += 1
                    continue
                conn.execute(
                    "INSERT INTO input_observations(seq, observation_id, node_name, rule_name, result, "
                    "content_digest, clock_ms, payload_hash, payload_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        obs["seq"],
                        obs["observation_id"],
                        obs["node"],
                        obs["rule_name"],
                        obs["result"],
                        obs["content_digest"],
                        obs["clock_ms"],
                        obs["payload_digest"],
                        dumps(obs),
                    ),
                )
                inserted += 1
        return {
            "file": str(path),
            "submitted": len(observations),
            "inserted": inserted,
            "duplicate": duplicate,
        }

    def get_node(self, name: str) -> Dict[str, Any]:
        row = self.store.conn.execute(
            "SELECT payload_json FROM edge_nodes WHERE node_name=?", (name,)
        ).fetchone()
        if not row:
            raise EngineError(ErrorCode.OBS_REFERENCE_UNKNOWN, "unknown edge node", details={"node": name})
        return loads(row["payload_json"])

    @staticmethod
    def window_bounds(event_clock_ms: int, node: Dict[str, Any]) -> Dict[str, int]:
        size = int(node["window_size_ms"])
        offset = int(node.get("window_offset_ms", 0))
        start = ((int(event_clock_ms) - offset) // size) * size + offset
        return {"start_ms": start, "end_ms": start + size}

    @staticmethod
    def _window_id(
        view_id: int,
        node: str,
        rule_name: str,
        start_ms: int,
        recompute_of: Optional[str] = None,
    ) -> str:
        raw = f"{view_id}|{node}|{rule_name}|{start_ms}|{recompute_of or ''}"
        return "w-" + sha256_hex(raw.encode("utf-8"))[:24]

    def probe(
        self,
        clock_ms: Optional[int] = None,
        *,
        node_name: Optional[str] = None,
        rule_name: Optional[str] = None,
        crash_after_obs: Optional[int] = None,
        slow_ms: int = 0,
        origin_enabled: bool = True,
        emit_empty_windows: bool = True,
    ) -> Dict[str, Any]:
        clock_ms = self.resolve_clock(clock_ms)
        nodes = self.get_nodes()
        if node_name:
            nodes = [self.get_node(node_name)]
        results = []
        for node in nodes:
            with file_lock(node_lock_path(self.store.path, node["name"])):
                results.append(
                    self._probe_locked(
                        clock_ms,
                        node,
                        rule_name=rule_name,
                        crash_after_obs=crash_after_obs,
                        slow_ms=slow_ms,
                        origin_enabled=origin_enabled,
                        emit_empty_windows=emit_empty_windows,
                    )
                )
        return {
            "ok": True,
            "command": "probe",
            "clock_ms": clock_ms,
            "node_results": results,
            "processed_new": sum(item["processed_new"] for item in results),
            "skipped_duplicates": sum(item["skipped_duplicates"] for item in results),
            "closed_windows": sum(len(item["closed_conclusions"]) for item in results),
        }

    def _probe_locked(
        self,
        clock_ms: int,
        node: Dict[str, Any],
        *,
        rule_name: Optional[str],
        crash_after_obs: Optional[int],
        slow_ms: int,
        origin_enabled: bool,
        emit_empty_windows: bool,
    ) -> Dict[str, Any]:
        query = (
            "SELECT * FROM input_observations WHERE node_name=? "
            "AND (? IS NULL OR rule_name=?) "
            "AND (clock_ms IS NULL OR clock_ms <= ?) ORDER BY rule_name, seq"
        )
        rows = self.store.conn.execute(
            query,
            (node["name"], rule_name, rule_name, clock_ms),
        ).fetchall()
        processed_new = 0
        skipped = 0
        pending_crash = crash_after_obs

        for obs_row in rows:
            existing_original = self.store.conn.execute(
                """
                SELECT 1 FROM processed_observations p
                JOIN windows w ON w.window_id = p.window_id
                WHERE p.seq=? AND w.recompute_of_window_id IS NULL
                """,
                (obs_row["seq"],),
            ).fetchone()
            if existing_original:
                skipped += 1
                continue

            obs = loads(obs_row["payload_json"])
            event_clock = obs_row["clock_ms"] if obs_row["clock_ms"] is not None else clock_ms
            bounds = self.window_bounds(event_clock, node)
            with self.store.transaction() as conn:
                window = self._find_or_create_original_window(
                    conn,
                    node=node["name"],
                    rule_name=obs_row["rule_name"],
                    bounds=bounds,
                    clock_ms=clock_ms,
                )

            if window["status"] == "committed":
                already = self.store.conn.execute(
                    "SELECT 1 FROM processed_observations WHERE window_id=? AND seq=?",
                    (window["window_id"], obs_row["seq"]),
                ).fetchone()
                if already:
                    skipped += 1
                    continue
                raise EngineError(
                    ErrorCode.WINDOW_NOT_FOUND,
                    "observation arrives after its validation window was committed",
                    details={"seq": obs_row["seq"], "window_id": window["window_id"]},
                )

            view_id = window["view_id"]
            rule = self._rule_json(view_id, obs_row["rule_name"])
            origin_result = self._arbitrate_for_observation(
                obs=obs,
                rule=rule,
                node=node,
                clock_ms=clock_ms,
                origin_enabled=origin_enabled,
            )
            if slow_ms:
                time.sleep(slow_ms / 1000.0)

            with self.store.transaction() as conn:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO processed_observations(view_id, seq, observation_id, window_id, "
                    "node_name, rule_name, result, content_digest, event_clock_ms, payload_hash, origin_json, "
                    "processed_clock_ms) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        view_id,
                        obs_row["seq"],
                        obs_row["observation_id"],
                        window["window_id"],
                        obs_row["node_name"],
                        obs_row["rule_name"],
                        obs_row["result"],
                        obs_row["content_digest"],
                        event_clock,
                        obs_row["payload_hash"],
                        dumps(origin_result),
                        clock_ms,
                    ),
                )
                inserted = cursor.rowcount == 1
            if inserted:
                processed_new += 1
                if pending_crash is not None:
                    pending_crash -= 1
                    if pending_crash == 0:
                        os.kill(os.getpid(), signal.SIGKILL)
            else:
                skipped += 1

        empty_conclusions: List[Dict[str, Any]] = []
        if emit_empty_windows:
            empty_conclusions = self.generate_empty_windows(
                clock_ms, node_name=node["name"], rule_name=rule_name
            )
        closed = self.close_due_windows(clock_ms, node_name=node["name"], rule_name=rule_name)
        return {
            "node": node["name"],
            "processed_new": processed_new,
            "skipped_duplicates": skipped,
            "closed_conclusions": empty_conclusions + closed,
        }

    def _find_or_create_original_window(
        self,
        conn: Any,
        *,
        node: str,
        rule_name: str,
        bounds: Dict[str, int],
        clock_ms: int,
    ) -> Dict[str, Any]:
        row = conn.execute(
            """
            SELECT * FROM windows
            WHERE node_name=? AND rule_name=? AND start_ms=? AND end_ms=?
              AND recompute_of_window_id IS NULL
            ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, rowid DESC
            LIMIT 1
            """,
            (node, rule_name, bounds["start_ms"], bounds["end_ms"]),
        ).fetchone()
        if row:
            return dict(row)
        view_id = self.store.require_active_view_id()
        window_id = self._window_id(view_id, node, rule_name, bounds["start_ms"])
        conn.execute(
            "INSERT INTO windows(window_id, view_id, node_name, rule_name, start_ms, end_ms, status, bound_clock_ms) "
            "VALUES(?, ?, ?, ?, ?, ?, 'open', ?)",
            (window_id, view_id, node, rule_name, bounds["start_ms"], bounds["end_ms"], clock_ms),
        )
        return {
            "window_id": window_id,
            "view_id": view_id,
            "node_name": node,
            "rule_name": rule_name,
            "start_ms": bounds["start_ms"],
            "end_ms": bounds["end_ms"],
            "status": "open",
        }

    def _rule_json(self, view_id: int, name: str) -> Optional[Dict[str, Any]]:
        row = self.store.conn.execute(
            "SELECT rule_json FROM unified_rules WHERE view_id=? AND name=?",
            (view_id, name),
        ).fetchone()
        return loads(row["rule_json"]) if row else None

    def _view_row(self, view_id: int) -> Dict[str, Any]:
        row = self.store.conn.execute("SELECT * FROM view_versions WHERE view_id=?", (view_id,)).fetchone()
        return dict(row) if row else {"view_id": view_id, "view_uid": None}

    def _arbitrate_for_observation(
        self,
        *,
        obs: Dict[str, Any],
        rule: Optional[Dict[str, Any]],
        node: Dict[str, Any],
        clock_ms: int,
        origin_enabled: bool,
    ) -> Dict[str, Any]:
        verify_type = (rule or {}).get("verify_type", "status")
        if not origin_enabled:
            return {"arbitration": "disabled", "cache_hit": False, "digest_match": None}
        if "origin" not in verify_type:
            return {"arbitration": verify_type, "cache_hit": None, "digest_match": None}
        return arbitrate_origin_digest(
            base_url=node["origin_base_url"],
            rule_name=obs["rule_name"],
            observed_digest=obs.get("content_digest"),
            node_name=node["name"],
            clock_ms=clock_ms,
            timeout_ms=int(node.get("probe_timeout_ms", 1000)),
        )

    def generate_empty_windows(
        self,
        clock_ms: int,
        *,
        node_name: Optional[str] = None,
        rule_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        manifest = self._sample_manifest()
        horizon = manifest.get("window_horizon_ms")
        if not isinstance(horizon, int):
            return []
        view_id = self.store.active_view_id()
        if view_id is None:
            return []
        rules = [
            rule
            for rule in self.get_rules(view_id)
            if not rule.get("tombstone") and (rule_name is None or rule["name"] == rule_name)
        ]
        nodes = [node for node in self.get_nodes() if node_name is None or node["name"] == node_name]
        created: List[Dict[str, Any]] = []
        for node in nodes:
            size = int(node["window_size_ms"])
            offset = int(node.get("window_offset_ms", 0))
            first_start = ((horizon - offset) // size) * size + offset
            for rule in rules:
                start = first_start
                while start + size <= clock_ms:
                    end = start + size
                    existing = self.store.conn.execute(
                        """
                        SELECT window_id FROM windows
                        WHERE node_name=? AND rule_name=? AND start_ms=? AND end_ms=?
                          AND recompute_of_window_id IS NULL
                        """,
                        (node["name"], rule["name"], start, end),
                    ).fetchone()
                    obs_exists = self.store.conn.execute(
                        """
                        SELECT 1 FROM input_observations
                        WHERE node_name=? AND rule_name=?
                          AND (
                            (clock_ms IS NOT NULL AND clock_ms >= ? AND clock_ms < ?)
                            OR (clock_ms IS NULL AND ? >= ? AND ? < ?)
                          )
                        LIMIT 1
                        """,
                        (
                            node["name"], rule["name"], start, end,
                            clock_ms, start, clock_ms, end,
                        ),
                    ).fetchone()
                    if not existing and not obs_exists:
                        window_id = self._window_id(view_id, node["name"], rule["name"], start)
                        with self.store.transaction() as conn:
                            conn.execute(
                                "INSERT OR IGNORE INTO windows(window_id, view_id, node_name, rule_name, "
                                "start_ms, end_ms, status, bound_clock_ms) VALUES(?, ?, ?, ?, ?, ?, 'open', ?)",
                                (window_id, view_id, node["name"], rule["name"], start, end, clock_ms),
                            )
                        conclusion = self._close_window(window_id, clock_ms)
                        if conclusion:
                            created.append(conclusion)
                    start += size
        return created

    def close_due_windows(
        self,
        clock_ms: int,
        *,
        node_name: Optional[str] = None,
        rule_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        rows = self.store.conn.execute(
            """
            SELECT window_id FROM windows
            WHERE status='open' AND end_ms <= ?
              AND (? IS NULL OR node_name=?)
              AND (? IS NULL OR rule_name=?)
            ORDER BY end_ms, node_name, rule_name
            """,
            (clock_ms, node_name, node_name, rule_name, rule_name),
        ).fetchall()
        conclusions = []
        for row in rows:
            conclusion = self._close_window(row["window_id"], clock_ms)
            if conclusion:
                conclusions.append(conclusion)
        return conclusions

    def _close_window(self, window_id: str, clock_ms: int) -> Optional[Dict[str, Any]]:
        window_row = self.store.conn.execute(
            "SELECT * FROM windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if not window_row:
            return None
        window = dict(window_row)
        if window["status"] == "committed":
            row = self.store.conn.execute(
                "SELECT result_json FROM conclusions WHERE window_id=?", (window_id,)
            ).fetchone()
            return loads(row["result_json"]) if row else None

        processed_rows = self.store.conn.execute(
            "SELECT * FROM processed_observations WHERE window_id=? ORDER BY seq",
            (window_id,),
        ).fetchall()
        rule = self._rule_json(window["view_id"], window["rule_name"])
        view = self._view_row(window["view_id"])
        verdict = adjudicate_window(
            window=window,
            rule=rule,
            view=view,
            processed_rows=processed_rows,
        )
        verdict["committed_clock_ms"] = clock_ms
        verdict["result_digest"] = sha256_object(verdict)
        verdict["conclusion_uid"] = "concl-" + verdict["result_digest"].split(":", 1)[1][:20]
        consistent_value = -1 if verdict["consistent"] is None else int(bool(verdict["consistent"]))

        with self.store.transaction() as conn:
            current = conn.execute(
                "SELECT status FROM windows WHERE window_id=?", (window_id,)
            ).fetchone()
            if current and current["status"] == "committed":
                row = conn.execute("SELECT result_json FROM conclusions WHERE window_id=?", (window_id,)).fetchone()
                return loads(row["result_json"]) if row else None
            cursor = conn.execute(
                "INSERT INTO conclusions(conclusion_uid, view_id, window_id, node_name, rule_name, start_ms, end_ms, "
                "verdict_code, consistent, cache_hit, result_digest, result_json, committed_clock_ms, reuse_status) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'computed')",
                (
                    verdict["conclusion_uid"],
                    window["view_id"],
                    window_id,
                    window["node_name"],
                    window["rule_name"],
                    window["start_ms"],
                    window["end_ms"],
                    verdict["verdict_code"],
                    consistent_value,
                    int(bool(verdict["cache_hit"])),
                    verdict["result_digest"],
                    dumps(verdict),
                    clock_ms,
                ),
            )
            conn.execute(
                "UPDATE windows SET status='committed', committed_clock_ms=?, conclusion_id=? WHERE window_id=?",
                (clock_ms, cursor.lastrowid, window_id),
            )
            conn.execute(
                """
                INSERT INTO watermarks(name, clock_ms) VALUES('window', ?)
                ON CONFLICT(name) DO UPDATE SET clock_ms = MAX(watermarks.clock_ms, excluded.clock_ms)
                """,
                (window["end_ms"],),
            )
        return verdict

    # ---------- incremental recomputation ----------

    def recompute_closure(
        self,
        closure_id: str,
        clock_ms: Optional[int] = None,
        *,
        node_name: Optional[str] = None,
        origin_enabled: bool = True,
    ) -> Dict[str, Any]:
        row = self.store.conn.execute(
            "SELECT * FROM closures WHERE closure_id=?", (closure_id,)
        ).fetchone()
        if not row:
            raise EngineError(ErrorCode.INPUT_NOT_FOUND, "closure not found", details={"closure_id": closure_id})
        closure = loads(row["result_json"])
        view_id = row["view_id"]
        if clock_ms is None:
            clock_ms = self.store.watermark("logical_clock") or row["created_clock_ms"]
        clock_ms = self.resolve_clock(clock_ms)

        run_id = f"run-{closure_id}"
        if node_name:
            run_id += f"-node-{node_name}"
        existing_run = self.store.conn.execute(
            "SELECT result_json FROM recompute_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing_run:
            return {
                "ok": True,
                "command": "recompute",
                "idempotent_replay": True,
                **loads(existing_run["result_json"]),
            }

        base_view_id = closure.get("base_view_id")
        if base_view_id is not None:
            plan = self._build_conclusion_plan(base_view_id, closure["affected_rules"])
        else:
            plan = closure.get("conclusion_plan", {})
        stale_windows = plan.get("stale_windows", [])
        conclusions: List[Dict[str, Any]] = []

        for stale in stale_windows:
            if node_name and stale["node"] != node_name:
                continue
            old_window_id = stale["source_window_id"]
            new_window_id = self._window_id(
                view_id,
                stale["node"],
                stale["rule_name"],
                stale["start_ms"],
                recompute_of=old_window_id,
            )
            existing_window = self.store.conn.execute(
                "SELECT status FROM windows WHERE window_id=?", (new_window_id,)
            ).fetchone()
            if not existing_window:
                with self.store.transaction() as conn:
                    conn.execute(
                        "INSERT INTO windows(window_id, view_id, node_name, rule_name, start_ms, end_ms, status, "
                        "bound_clock_ms, recompute_of_window_id) VALUES(?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                        (
                            new_window_id,
                            view_id,
                            stale["node"],
                            stale["rule_name"],
                            stale["start_ms"],
                            stale["end_ms"],
                            clock_ms,
                            old_window_id,
                        ),
                    )

            node = self.get_node(stale["node"])
            for seq in stale.get("observation_seqs", []):
                already = self.store.conn.execute(
                    "SELECT 1 FROM processed_observations WHERE window_id=? AND seq=?",
                    (new_window_id, seq),
                ).fetchone()
                if already:
                    continue
                obs_row = self.store.conn.execute(
                    "SELECT * FROM input_observations WHERE seq=?", (seq,)
                ).fetchone()
                if not obs_row:
                    continue
                obs = loads(obs_row["payload_json"])
                rule = self._rule_json(view_id, stale["rule_name"])
                origin_result = self._arbitrate_for_observation(
                    obs=obs,
                    rule=rule,
                    node=node,
                    clock_ms=clock_ms,
                    origin_enabled=origin_enabled,
                )
                with self.store.transaction() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO processed_observations(view_id, seq, observation_id, window_id, "
                        "node_name, rule_name, result, content_digest, event_clock_ms, payload_hash, origin_json, "
                        "processed_clock_ms) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            view_id,
                            seq,
                            obs_row["observation_id"],
                            new_window_id,
                            stale["node"],
                            stale["rule_name"],
                            obs_row["result"],
                            obs_row["content_digest"],
                            obs_row["clock_ms"],
                            obs_row["payload_hash"],
                            dumps(origin_result),
                            clock_ms,
                        ),
                    )
            verdict = self._close_window(new_window_id, max(clock_ms, stale["end_ms"]))
            if verdict:
                conclusions.append(verdict)

        result = {
            "closure_id": closure_id,
            "view_id": view_id,
            "recomputed_windows": len(conclusions),
            "conclusions": conclusions,
            "reusable_conclusion_ids": plan.get("reusable_conclusion_ids", []),
        }
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO recompute_runs(run_id, closure_id, view_id, result_json, created_clock_ms) "
                "VALUES(?, ?, ?, ?, ?)",
                (run_id, closure_id, view_id, dumps(result), clock_ms),
            )
        return {"ok": True, "command": "recompute", "idempotent_replay": False, **result}

    # ---------- recovery and status ----------

    def resume(self, clock_ms: Optional[int] = None) -> Dict[str, Any]:
        report: Dict[str, Any] = {"ok": True, "command": "resume"}
        with self.global_lock():
            staging_rows = self.store.conn.execute(
                "SELECT operation_id, kind, payload_json FROM operations WHERE status='staging'"
            ).fetchall()
            aborted = [row["operation_id"] for row in staging_rows]
            with self.store.transaction() as conn:
                for operation_id in aborted:
                    conn.execute(
                        "UPDATE operations SET status='aborted_recovered' WHERE operation_id=?",
                        (operation_id,),
                    )

            open_before = [
                dict(row)
                for row in self.store.conn.execute(
                    "SELECT window_id, node_name, rule_name, start_ms, end_ms, view_id "
                    "FROM windows WHERE status='open' ORDER BY end_ms, node_name, rule_name"
                ).fetchall()
            ]
            report["aborted_staging_operations"] = aborted
            report["half_committed_open_windows"] = open_before

            effective_clock = clock_ms if clock_ms is not None else self.store.watermark()
            if effective_clock is not None:
                probe_result = self.probe(effective_clock)
                report["probe"] = {
                    "processed_new": probe_result["processed_new"],
                    "skipped_duplicates": probe_result["skipped_duplicates"],
                    "closed_windows": probe_result["closed_windows"],
                }
            else:
                report["probe"] = None

            open_after = self.store.conn.execute(
                "SELECT COUNT(*) AS count FROM windows WHERE status='open'"
            ).fetchone()["count"]
            report["open_windows_remaining"] = open_after
            report["recovered"] = True
        return report

    def status(self) -> Dict[str, Any]:
        view_id = self.store.active_view_id()
        view = None
        rules: List[Dict[str, Any]] = []
        if view_id is not None:
            row = self.store.conn.execute("SELECT * FROM view_versions WHERE view_id=?", (view_id,)).fetchone()
            view = dict(row) if row else None
            rules = self.get_rules(view_id)

        def count(sql: str, params: List[Any] | None = None) -> int:
            return self.store.conn.execute(sql, params or []).fetchone()["n"]

        verdict_rows = self.store.conn.execute(
            "SELECT verdict_code, COUNT(*) AS n FROM conclusions GROUP BY verdict_code ORDER BY verdict_code"
        ).fetchall()
        window_rows = self.store.conn.execute(
            "SELECT status, COUNT(*) AS n FROM windows GROUP BY status ORDER BY status"
        ).fetchall()
        latest_closure = self.store.conn.execute(
            "SELECT closure_id, patch_id, view_id, result_digest, created_clock_ms "
            "FROM closures ORDER BY created_clock_ms DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return {
            "ok": True,
            "command": "status",
            "state_path": str(self.store.path),
            "active_view_id": view_id,
            "active_view": {
                "view_uid": view["view_uid"],
                "version_no": view["version_no"],
                "decision_digest": view["decision_digest"],
                "injected_clock_ms": view["injected_clock_ms"],
                "created_by": view["created_by"],
            }
            if view
            else None,
            "active_package_version": self.store.get_meta("active_package_version"),
            "clock_watermark_ms": self.store.watermark(),
            "window_watermark_ms": self.store.watermark("window"),
            "counts": {
                "rules_in_active_view": len(rules),
                "input_observations": count("SELECT COUNT(*) AS n FROM input_observations"),
                "processed_observations": count("SELECT COUNT(*) AS n FROM processed_observations"),
                "windows": count("SELECT COUNT(*) AS n FROM windows"),
                "conclusions": count("SELECT COUNT(*) AS n FROM conclusions"),
                "patches": count("SELECT COUNT(*) AS n FROM patches"),
                "closures": count("SELECT COUNT(*) AS n FROM closures"),
                "staging_operations": count(
                    "SELECT COUNT(*) AS n FROM operations WHERE status='staging'"
                ),
            },
            "windows_by_status": {row["status"]: row["n"] for row in window_rows},
            "conclusions_by_verdict": {row["verdict_code"]: row["n"] for row in verdict_rows},
            "latest_closure": dict(latest_closure) if latest_closure else None,
        }

    def list_conclusions(
        self, *, node_name: Optional[str] = None, rule_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        rows = self.store.conn.execute(
            """
            SELECT result_json FROM conclusions
            WHERE (? IS NULL OR node_name=?) AND (? IS NULL OR rule_name=?)
            ORDER BY start_ms, node_name, rule_name, conclusion_id
            """,
            (node_name, node_name, rule_name, rule_name),
        ).fetchall()
        return [loads(row["result_json"]) for row in rows]
