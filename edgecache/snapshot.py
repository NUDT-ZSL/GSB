from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ContentState, Policy
from .errors import EdgeError, ReplayError, StateError, error_payload, exit_code_for
from .ledger import Ledger
from .util import canonical_json, emit_json_line, monotonic_ms, read_json_file, stable_hash, utc_now_text


SNAPSHOT_SCHEMA_VERSION = 1


def _without_checksum(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(snapshot))
    payload.pop("checksum", None)
    return payload


def snapshot_checksum(snapshot: Mapping[str, Any]) -> str:
    return stable_hash(_without_checksum(snapshot))


class SnapshotService:
    def __init__(self, ledger: Ledger, snapshot_dir: str | Path) -> None:
        self.ledger = ledger
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def build_payload(self) -> dict[str, Any]:
        rows = self.ledger.export_snapshot_rows()
        active = rows["active"]
        derivations = {
            path: {
                "sources": list(value.sources),
                "rule": value.rule,
            }
            for path, value in sorted(active.content.effective.items())
            if value.sources
        }
        payload: dict[str, Any] = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "created_at": utc_now_text(),
            "ledger_version": active.ledger_version,
            "policy_version": active.policy_version,
            "policy_hash": active.policy_hash,
            "content_set_hash": active.content_set_hash,
            "content_versions": active.content.versions,
            "policy": active.policy.to_doc(),
            "content": active.content.to_doc(),
            "derivations": derivations,
            "path_versions": rows["path_versions"],
            "cache_entries": rows["cache_entries"],
            "evictions": rows["evictions"],
            "cache_events": rows["cache_events"],
            "write_intents": rows["write_intents"],
            "ledger_history": rows["ledger_history"],
            "propagation_batches": rows["propagation_batches"],
            "propagation_traces": rows["propagation_traces"],
            "propagation_node_results": rows["propagation_node_results"],
        }
        payload["checksum"] = snapshot_checksum(payload)
        return payload

    def save(self, filename: str = "latest.json") -> dict[str, Any]:
        payload = self.build_payload()
        target = self.snapshot_dir / filename
        backup_path: Path | None = None
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = self.snapshot_dir / f"backup-ledger-{payload['ledger_version']}-{stamp}.json"
            shutil.copyfile(target, backup_path)
            os.chmod(backup_path, 0o444)
        temp_path = target.with_suffix(target.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temp_path, target)
        try:
            os.chmod(target, 0o644)
        except OSError:
            pass
        return {
            "path": str(target.resolve()),
            "backup_path": str(backup_path.resolve()) if backup_path else None,
            "backup_read_only": bool(backup_path),
            "ledger_version": payload["ledger_version"],
            "policy_version": payload["policy_version"],
            "content_set_hash": payload["content_set_hash"],
            "checksum": payload["checksum"],
            "cache_entries": len(payload["cache_entries"]),
            "propagation_batches": len(payload["propagation_batches"]),
            "propagation_traces": len(payload["propagation_traces"]),
            "evictions": len(payload["evictions"]),
        }

    def load(self, path: str | Path) -> dict[str, Any]:
        snapshot_path = Path(path)
        try:
            with snapshot_path.open("r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except FileNotFoundError as exc:
            raise ReplayError("snapshot file not found", location=str(snapshot_path)) from exc
        except json.JSONDecodeError as exc:
            raise ReplayError(
                "snapshot is not valid JSON",
                location=f"{snapshot_path}:{exc.lineno}:{exc.colno}",
                parser_error=exc.msg,
            ) from exc
        except OSError as exc:
            raise ReplayError(
                "snapshot file cannot be read",
                location=str(snapshot_path),
                os_error=str(exc),
            ) from exc
        if not isinstance(snapshot, dict):
            raise ReplayError("snapshot root must be a JSON object", location=str(snapshot_path))
        if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ReplayError(
                "snapshot schema_version is unsupported",
                location=f"{snapshot_path}:schema_version",
                expected=SNAPSHOT_SCHEMA_VERSION,
                actual=snapshot.get("schema_version"),
            )
        expected = snapshot.get("checksum")
        actual = snapshot_checksum(snapshot)
        if not isinstance(expected, str) or expected != actual:
            raise ReplayError(
                "snapshot checksum mismatch",
                location=f"{snapshot_path}:checksum",
                expected=expected,
                actual=actual,
            )
        return snapshot

    @staticmethod
    def _materialize(snapshot: Mapping[str, Any]) -> tuple[ContentState, Policy]:
        content = ContentState.from_doc(snapshot.get("content"))
        policy = Policy.from_doc(snapshot.get("policy"), content.paths)
        if snapshot.get("policy_hash") != policy.policy_hash:
            raise ReplayError(
                "snapshot policy hash mismatch",
                location="snapshot.policy_hash",
                expected=policy.policy_hash,
                actual=snapshot.get("policy_hash"),
            )
        if snapshot.get("content_set_hash") != content.content_set_hash:
            raise ReplayError(
                "snapshot content set hash mismatch",
                location="snapshot.content_set_hash",
                expected=content.content_set_hash,
                actual=snapshot.get("content_set_hash"),
            )
        if snapshot.get("content_versions") != content.versions:
            raise ReplayError(
                "snapshot content version set cannot be reproduced from content declaration",
                location="snapshot.content_versions",
                expected=content.versions,
                actual=snapshot.get("content_versions"),
            )
        return content, policy

    @staticmethod
    def _expected_relation(depth: int) -> str:
        if depth == 0:
            return "trigger"
        if depth == 1:
            return "direct-dependent"
        return "indirect-dependent"

    def verify_replay(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Recompute propagation sets/order and compare them item by item."""

        content, _ = self._materialize(snapshot)
        traces = list(snapshot.get("propagation_traces") or [])
        batches = list(snapshot.get("propagation_batches") or [])
        results = list(snapshot.get("propagation_node_results") or [])
        batch_by_id = {str(batch["batch_id"]): batch for batch in batches}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for trace in traces:
            batch_id = str(trace.get("batch_id"))
            if batch_id not in batch_by_id:
                raise ReplayError(
                    "propagation trace has no batch record",
                    location="snapshot.propagation_traces",
                    batch_id=batch_id,
                    trace=trace,
                )
            grouped.setdefault(batch_id, []).append(dict(trace))

        verified_batches: list[dict[str, Any]] = []
        result_paths: dict[str, set[str]] = {}
        for result in results:
            result_paths.setdefault(str(result.get("batch_id")), set()).add(
                str(result.get("affected_path"))
            )

        for batch in sorted(batches, key=lambda item: (int(item["ledger_version"]), str(item["batch_id"]))):
            batch_id = str(batch["batch_id"])
            trigger_path = str(batch["trigger_path"])
            if trigger_path not in content.effective:
                raise ReplayError(
                    "propagation batch trigger path is absent from snapshot content",
                    location="snapshot.propagation_batches.trigger_path",
                    batch_id=batch_id,
                    trigger_path=trigger_path,
                )
            expected = list(content.affected_order(trigger_path))
            actual_traces = sorted(grouped.get(batch_id, []), key=lambda item: int(item["seq"]))
            actual = [
                (str(trace["affected_path"]), int(trace["depth"])) for trace in actual_traces
            ]
            expected_paths = [path for path, _ in expected]
            actual_paths = [path for path, _ in actual]
            seqs = [int(trace["seq"]) for trace in actual_traces]
            if seqs != list(range(len(actual_traces))):
                raise ReplayError(
                    "propagation trace sequence is not contiguous",
                    location=f"snapshot.propagation_traces[{batch_id}]",
                    actual_seq=seqs,
                )
            if actual != expected:
                raise ReplayError(
                    "propagation replay set/order does not reproduce saved traces",
                    location=f"snapshot.propagation_traces[{batch_id}]",
                    basis={
                        "trigger_path": trigger_path,
                        "expected": expected_paths,
                        "actual": actual_paths,
                        "missing": sorted(set(expected_paths) - set(actual_paths)),
                        "extra": sorted(set(actual_paths) - set(expected_paths)),
                        "order_matches": expected_paths == actual_paths,
                    },
                )
            for trace in actual_traces:
                if str(trace.get("trigger_path")) != trigger_path:
                    raise ReplayError(
                        "propagation trace trigger path differs from batch",
                        location=f"snapshot.propagation_traces[{batch_id}].trigger_path",
                        expected=trigger_path,
                        actual=trace.get("trigger_path"),
                    )
                expected_relation = self._expected_relation(int(trace["depth"]))
                if trace.get("relation") != expected_relation:
                    raise ReplayError(
                        "propagation relation label cannot be reproduced",
                        location=f"snapshot.propagation_traces[{batch_id}].relation",
                        expected=expected_relation,
                        actual=trace.get("relation"),
                    )
            if result_paths.get(batch_id):
                missing_results = sorted(set(actual_paths) - result_paths[batch_id])
                if missing_results:
                    raise ReplayError(
                        "saved propagation node results omit affected paths",
                        location=f"snapshot.propagation_node_results[{batch_id}]",
                        missing_affected_paths=missing_results,
                    )
            verified_batches.append(
                {
                    "batch_id": batch_id,
                    "trigger_path": trigger_path,
                    "ordered_paths": [path for path, _ in expected],
                    "traces_verified": len(actual_traces),
                }
            )

        path_versions = {str(item["path"]): str(item["etag"]) for item in snapshot.get("path_versions") or []}
        if path_versions != content.versions:
            missing = sorted(set(content.versions) - set(path_versions))
            extra = sorted(set(path_versions) - set(content.versions))
            changed = sorted(
                path
                for path in set(content.versions) & set(path_versions)
                if content.versions[path] != path_versions[path]
            )
            raise ReplayError(
                "snapshot path version collection differs from reproduced content versions",
                location="snapshot.path_versions",
                missing=missing,
                extra=extra,
                changed=changed,
            )
        for entry in snapshot.get("cache_entries") or []:
            path = str(entry.get("path"))
            if path not in content.effective:
                raise ReplayError(
                    "snapshot cache entry references missing path",
                    location="snapshot.cache_entries.path",
                    path=path,
                )
            if entry.get("etag") != content.effective[path].etag:
                raise ReplayError(
                    "snapshot cache entry ETag cannot be reproduced",
                    location="snapshot.cache_entries.etag",
                    node_id=entry.get("node_id"),
                    path=path,
                    expected=content.effective[path].etag,
                    actual=entry.get("etag"),
                )
        return {
            "verified_batches": len(verified_batches),
            "verified_traces": len(traces),
            "verified_cache_entries": len(snapshot.get("cache_entries") or []),
            "verified_content_paths": len(content.paths),
            "basis": verified_batches,
        }

    def version_differences(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        active = self.ledger.active_state()
        snapshot_content, _ = self._materialize(snapshot)
        current_versions = active.content.versions
        snapshot_versions = snapshot_content.versions
        current_paths = set(current_versions)
        snapshot_paths = set(snapshot_versions)
        changed = [
            {
                "path": path,
                "current_etag": current_versions[path],
                "snapshot_etag": snapshot_versions[path],
            }
            for path in sorted(current_paths & snapshot_paths)
            if current_versions[path] != snapshot_versions[path]
        ]
        differences = {
            "ledger_version": {
                "current": active.ledger_version,
                "snapshot": int(snapshot["ledger_version"]),
                "changed": active.ledger_version != int(snapshot["ledger_version"]),
            },
            "policy_version": {
                "current": active.policy_version,
                "snapshot": snapshot["policy_version"],
                "changed": active.policy_version != snapshot["policy_version"]
                or active.policy_hash != snapshot["policy_hash"],
            },
            "content_set_hash": {
                "current": active.content_set_hash,
                "snapshot": snapshot["content_set_hash"],
                "changed": active.content_set_hash != snapshot["content_set_hash"],
            },
            "content_paths_added": sorted(snapshot_paths - current_paths),
            "content_paths_removed": sorted(current_paths - snapshot_paths),
            "content_paths_changed": changed,
        }
        changed_any = (
            differences["ledger_version"]["changed"]
            or differences["policy_version"]["changed"]
            or differences["content_set_hash"]["changed"]
            or bool(differences["content_paths_added"])
            or bool(differences["content_paths_removed"])
            or bool(changed)
        )
        differences["changed"] = changed_any
        return differences

    def restore(self, path: str | Path) -> dict[str, Any]:
        snapshot = self.load(path)
        replay = self.verify_replay(snapshot)
        differences = self.version_differences(snapshot)
        current_entries = self.ledger.list_entries()
        content, policy = self._materialize(snapshot)
        restore_stats = self.ledger.restore_snapshot_state(
            policy_doc=policy.to_doc(),
            content_doc=content.to_doc(),
            entries=snapshot.get("cache_entries") or [],
            path_versions=snapshot.get("path_versions") or [],
            propagation_batches=snapshot.get("propagation_batches") or [],
            propagation_traces=snapshot.get("propagation_traces") or [],
            propagation_node_results=snapshot.get("propagation_node_results") or [],
            source_snapshot_ledger_version=int(snapshot["ledger_version"]),
            now_ms=monotonic_ms(),
            detail={
                "snapshot_path": str(Path(path).resolve()),
                "snapshot_checksum": snapshot["checksum"],
                "version_differences": differences,
                "replay_verified_batches": replay["verified_batches"],
            },
        )
        return {
            "snapshot_path": str(Path(path).resolve()),
            "original_snapshot_ledger_version": int(snapshot["ledger_version"]),
            "previous_current_ledger_version": restore_stats["previous_ledger_version"],
            "restored_ledger_version": restore_stats["new_ledger_version"],
            "versions_match_before_restore": not differences["changed"],
            "version_differences": differences,
            "replacement": {
                "strategy": "full_replace",
                "removed_current_entries": restore_stats["removed_entries"],
                "inserted_snapshot_entries": restore_stats["inserted_entries"],
                "replaced_entries": len(current_entries) + restore_stats["inserted_entries"],
                "derivations_replaced": True,
                "incremental_merge_used": False,
            },
            "replay": replay,
            "restored_from_snapshot": {
                "policy_version": policy.policy_version,
                "content_set_hash": content.content_set_hash,
                "cache_entries": len(snapshot.get("cache_entries") or []),
                "evictions_recorded_in_snapshot": len(snapshot.get("evictions") or []),
                "propagation_traces": len(snapshot.get("propagation_traces") or []),
            },
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.snapshot_dir.glob("*.json")):
            stat = path.stat()
            mode = stat.st_mode & 0o777
            items.append(
                {
                    "path": str(path),
                    "size": stat.st_size,
                    "read_only": not bool(mode & 0o200),
                    "mode": oct(mode),
                    "backup": path.name.startswith("backup-"),
                }
            )
        return items


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Snapshot capture/verify/restore")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--db", required=True)
    capture.add_argument("--snapshot-dir", required=True)
    capture.add_argument("--name", default="latest.json")
    verify = sub.add_parser("verify")
    verify.add_argument("--db", required=True)
    verify.add_argument("--snapshot-dir", required=True)
    verify.add_argument("--path", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--db", required=True)
    restore.add_argument("--snapshot-dir", required=True)
    restore.add_argument("--path", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        service = SnapshotService(Ledger(args.db), args.snapshot_dir)
        if args.command == "capture":
            result = service.save(args.name)
        elif args.command == "verify":
            snapshot = service.load(args.path)
            result = service.verify_replay(snapshot)
        else:
            result = service.restore(args.path)
        emit_json_line({"ok": True, "result": result})
        return 0
    except EdgeError as exc:
        emit_json_line({"ok": False, "error": exc.to_dict()}, sys.stderr)
        return exit_code_for(exc)
    except Exception as exc:  # pragma: no cover
        emit_json_line({"ok": False, "error": error_payload(exc)}, sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
