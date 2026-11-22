from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import diff_paths, digest_json
from .db import CrashInjector, Database
from .engine import Engine, initial_state
from .errors import (
    PATCH_BASE_001,
    PATCH_CONFLICT_001,
    PATCH_VERSION_001,
    VERIFY_001,
    error,
)
from .patching import apply_patch_in_memory, apply_patch_ops_to_state
from .validation import (
    load_json,
    normalize_scenario,
    validate_patch_document,
)


def _loads(value: str) -> Any:
    return json.loads(value)


def _first_state_difference(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    frame_seq: int,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    differences = diff_paths(expected, actual)
    if not differences:
        return None
    first = differences[0]
    return {
        "frame_seq": frame_seq,
        "path": first["path"],
        "persisted": first["before"],
        "recomputed": first["after"],
        "observation": context,
    }


class DiagnosticService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        crash: CrashInjector | None = None,
    ) -> None:
        self.db = Database(db_path, crash=crash)

    def initialize(
        self,
        scenario_document: dict[str, Any],
        *,
        preflight: bool = True,
    ) -> dict[str, Any]:
        scenario = normalize_scenario(scenario_document, preflight=preflight)
        state = initial_state(scenario)
        result = self.db.initialize(scenario, state)
        if result["duplicate"]:
            result["state"] = _loads(result["state"])
        else:
            result["state"] = state
        result["scenario_digest"] = digest_json(scenario)
        return result

    def initialize_from_file(self, path: str | Path) -> dict[str, Any]:
        return self.initialize(load_json(path, kind="scenario"))

    def run(
        self,
        connection_id: str,
        *,
        max_frames: int | None = None,
        until_clock: int | None = None,
        expected_patch_version: int | None = None,
    ) -> dict[str, Any]:
        if max_frames is not None and max_frames < 0:
            raise error(VERIFY_001, "max_frames must be non-negative")
        processed = 0
        duplicates = 0
        while max_frames is None or processed + duplicates < max_frames:
            connection_row, scenario_row = self.db.get_current(connection_id)
            state = _loads(connection_row["state_json"])
            scenario = _loads(scenario_row["scenario_json"])
            bound_patch_version = int(connection_row["current_version"])
            if expected_patch_version is not None:
                if bound_patch_version != expected_patch_version:
                    raise error(
                        PATCH_BASE_001,
                        "run is bound to a patch version that is no longer current",
                        details={
                            "expected": expected_patch_version,
                            "current": bound_patch_version,
                        },
                        http_status=409,
                        category="version",
                    )
            committed_seq = int(state["committed_seq"])
            if committed_seq >= len(scenario["events"]):
                break
            event = scenario["events"][committed_seq]
            if until_clock is not None and int(event["clock_ms"]) > until_clock:
                break
            result = self.db.commit_frame(
                connection_id=connection_id,
                event=event,
                expected_patch_version=bound_patch_version,
            )
            if result["duplicate"]:
                duplicates += 1
            else:
                processed += 1
            if max_frames == 0:
                break
        return {
            "connection_id": connection_id,
            "processed": processed,
            "duplicates": duplicates,
            "status": self.status(connection_id),
        }

    def resume(
        self,
        connection_id: str,
        *,
        max_frames: int | None = None,
        until_clock: int | None = None,
    ) -> dict[str, Any]:
        intents = self.db.open_intents(connection_id)
        abandoned = self.db.abandon_intents(connection_id)
        run_result = self.run(
            connection_id,
            max_frames=max_frames,
            until_clock=until_clock,
        )
        run_result["abandoned_intents"] = abandoned
        run_result["half_committed_intents"] = intents
        run_result["recovery_source"] = "persisted-sqlite-state-only"
        return run_result

    def _idempotent_patch(
        self,
        connection_id: str,
        patch: dict[str, Any],
        current_version: int,
    ) -> dict[str, Any] | None:
        patches = self.db.list_patch_commits(connection_id)
        for row in patches:
            if int(row["version"]) == int(patch["target_version"]):
                if row["patch_digest"] != patch["_computed_digest"]:
                    raise error(
                        PATCH_CONFLICT_001,
                        "target version already exists with different patch content",
                        details={
                            "target_version": patch["target_version"],
                            "stored_digest": row["patch_digest"],
                            "request_digest": patch["_computed_digest"],
                        },
                        http_status=409,
                        category="version",
                    )
                return {
                    "connection_id": connection_id,
                    "version": int(row["version"]),
                    "kind": row["classified_kind"],
                    "duplicate": True,
                    "manifest": _loads(row["manifest_json"]),
                    "state": _loads(row["after_state_json"]),
                }
        return None

    def apply_patch(
        self,
        connection_id: str,
        patch_document: dict[str, Any],
    ) -> dict[str, Any]:
        patch = validate_patch_document(patch_document)
        connection_row, scenario_row = self.db.get_current(connection_id)
        current_version = int(connection_row["current_version"])
        target_version = int(patch["target_version"])
        if target_version < current_version:
            raise error(
                PATCH_VERSION_001,
                "patch target version would roll the scenario back",
                details={"current": current_version, "target": target_version},
                http_status=409,
                category="version",
            )
        if target_version == current_version:
            duplicate = self._idempotent_patch(connection_id, patch, current_version)
            if duplicate is not None:
                duplicate["scenario_digest"] = scenario_row["digest"]
                return duplicate
            raise error(
                PATCH_CONFLICT_001,
                "target version equals the current version but content differs",
                details={"current": current_version, "target": target_version},
                http_status=409,
                category="version",
            )
        for existing_patch in self.db.list_patch_commits(connection_id):
            if existing_patch["patch_id"] != patch["patch_id"]:
                continue
            raise error(
                PATCH_CONFLICT_001,
                "patch_id has already been committed with different target content",
                details={
                    "patch_id": patch["patch_id"],
                    "existing_version": int(existing_patch["version"]),
                    "requested_version": target_version,
                },
                http_status=409,
                category="version",
            )
        if int(patch["base_version"]) != current_version:
            raise error(
                PATCH_BASE_001,
                "patch base_version does not match the current scenario version",
                details={
                    "base_version": int(patch["base_version"]),
                    "current_version": current_version,
                },
                http_status=409,
                category="version",
            )
        old_scenario = _loads(scenario_row["scenario_json"])
        state_before = _loads(connection_row["state_json"])
        scenario_after, state_after, kind, manifest = apply_patch_in_memory(
            old_scenario,
            state_before,
            patch,
            committed_seq=int(connection_row["committed_seq"]),
        )
        if kind == "noop":
            return {
                "connection_id": connection_id,
                "version": current_version,
                "kind": "noop",
                "duplicate": False,
                "manifest": manifest,
                "state": state_before,
                "scenario_digest": scenario_row["digest"],
            }
        projected = Engine(scenario_after, state_after)
        projected.run_events(
            scenario_after["events"][int(state_after["committed_seq"]) :]
        )
        projected.check_expected()
        result = self.db.commit_patch(
            connection_id=connection_id,
            patch=patch,
            scenario_after=scenario_after,
            state_after=state_after,
            manifest=manifest,
            classified_kind=kind,
            expected_current_digest=connection_row["state_digest"],
            expected_current_version=current_version,
            expected_committed_seq=int(connection_row["committed_seq"]),
        )
        result["duplicate"] = False
        result["scenario_digest"] = digest_json(scenario_after)
        return result

    def status(self, connection_id: str) -> dict[str, Any]:
        data = self.db.get_status_data(connection_id)
        connection_row = data["connection"]
        scenario_row = data["scenario_version"]
        state = _loads(connection_row["state_json"])
        scenario = _loads(scenario_row["scenario_json"])
        streams: list[dict[str, Any]] = []
        for stream_id in sorted(state["streams"], key=int):
            stream = state["streams"][stream_id]
            streams.append(
                {
                    "stream_id": stream["stream_id"],
                    "initiator": stream["initiator"],
                    "state": stream["state"],
                    "weight": stream["weight"],
                    "schedule_rank": stream["schedule_rank"],
                    "send_window": stream["send_window"],
                    "recv_window": stream["recv_window"],
                    "max_send_window": stream["max_send_window"],
                    "max_recv_window": stream["max_recv_window"],
                    "send_in_flight": stream["send_in_flight"],
                    "recv_in_flight": stream["recv_in_flight"],
                    "bytes_sent": stream["bytes_sent"],
                    "bytes_received": stream["bytes_received"],
                    "local_end": stream["local_end"],
                    "remote_end": stream["remote_end"],
                    "reset": stream["reset"],
                    "reclaimed_to_connection": stream["reclaimed_to_connection"],
                    "header_digest": stream["header_digest"],
                    "header_size": stream["header_size"],
                    "header_end_stream": stream["header_end_stream"],
                    "expected_final_state": stream["expected_final_state"],
                }
            )
        return {
            "ok": True,
            "connection_id": state["connection_id"],
            "scenario_id": state["scenario_id"],
            "patch_version": state["patch_version"],
            "committed_seq": state["committed_seq"],
            "clock_ms": state["clock_ms"],
            "event_count": state["event_count"],
            "scene_event_count": len(scenario["events"]),
            "connection": state["connection"],
            "streams": streams,
            "scheduler": state["scheduler"],
            "headers": state["headers"],
            "settings": state["settings"],
            "settings_ack_pending_send": state["settings_ack_pending_send"],
            "settings_ack_pending_recv": state["settings_ack_pending_recv"],
            "ping": state["ping"],
            "last_frame": state["last_frame"],
            "state_digest": state["state_digest"],
            "chain_digest": state["chain_digest"],
            "scenario_digest": scenario_row["digest"],
        }

    def verify(self, connection_id: str) -> dict[str, Any]:
        data = self.db.verification_data(connection_id)
        connection_row = data["connection"]
        current_version = int(connection_row["current_version"])
        epoch_version = int(connection_row["epoch_version"])
        current_state = _loads(connection_row["state_json"])
        versions = {
            int(row["version"]): row
            for row in data["scenario_versions"]
        }
        all_patch_rows = data["patch_commits"]
        patch_rows = [
            row for row in all_patch_rows if int(row["version"]) > epoch_version
        ]
        epoch_patch_row = next(
            (
                row
                for row in all_patch_rows
                if int(row["version"]) == epoch_version
            ),
            None,
        )
        commits = data["state_commits"]
        frame_commits: dict[tuple[int, int], dict[str, Any]] = {}
        for row in commits:
            if row["kind"] == "frame":
                frame_commits[(int(row["patch_version"]), int(row["frame_seq"]))] = row

        active_version = epoch_version
        active_scenario = _loads(versions[epoch_version]["scenario_json"])
        replay = initial_state(active_scenario, patch_version=epoch_version)
        checked_frames = 0
        first_divergence: dict[str, Any] | None = None
        scope_checks: list[dict[str, Any]] = []

        if epoch_patch_row is not None:
            epoch_scope = self._verify_manifest(
                epoch_patch_row,
                _loads(epoch_patch_row["after_state_json"]),
            )
            scope_checks.append(epoch_scope)
            if not epoch_scope["ok"]:
                first_divergence = {
                    "frame_seq": int(epoch_patch_row["applied_at_seq"]),
                    "path": epoch_scope["path"],
                    "persisted": epoch_scope.get("persisted"),
                    "recomputed": epoch_scope.get("recomputed"),
                    "observation": {
                        "patch_version": int(epoch_patch_row["version"]),
                        "patch_id": epoch_patch_row["patch_id"],
                        "observation": "replacement-invalidation-manifest",
                    },
                }

        def compare(
            persisted: dict[str, Any],
            observed: dict[str, Any],
            *,
            frame_seq: int,
            context: dict[str, Any],
        ) -> None:
            nonlocal first_divergence
            if first_divergence is not None:
                return
            difference = _first_state_difference(
                persisted, observed, frame_seq=frame_seq, context=context
            )
            if difference is not None:
                first_divergence = difference

        def process_until(boundary: int, scenario: dict[str, Any]) -> None:
            nonlocal replay, checked_frames
            while int(replay["committed_seq"]) < boundary:
                seq = int(replay["committed_seq"]) + 1
                if seq - 1 >= len(scenario["events"]):
                    compare(
                        current_state,
                        replay,
                        frame_seq=seq,
                        context={"observation": "scenario-exhausted"},
                    )
                    return
                event = scenario["events"][seq - 1]
                replay = Engine(scenario, replay).process(event)
                persist_key = (int(replay["patch_version"]), seq)
                if persist_key in frame_commits and first_divergence is None:
                    persisted = _loads(frame_commits[persist_key]["state_json"])
                    compare(
                        persisted,
                        replay,
                        frame_seq=seq,
                        context={
                            "patch_version": int(replay["patch_version"]),
                            "event_digest": replay["last_frame"]["event_digest"],
                            "observation": "frame-state-replay",
                        },
                    )
                checked_frames += 1

        for patch_row in patch_rows:
            if first_divergence is not None:
                break
            boundary = int(patch_row["applied_at_seq"])
            process_until(boundary, active_scenario)
            if first_divergence is not None:
                break
            target_version = int(patch_row["version"])
            target_scenario = _loads(versions[target_version]["scenario_json"])
            patch_for_replay = {
                "patch_id": patch_row["patch_id"],
                "base_version": int(patch_row["parent_version"]),
                "target_version": target_version,
                "mode": patch_row["mode"],
                "operations": _loads(patch_row["operations_json"]),
                "clock_ms": int(patch_row["applied_at_clock_ms"]),
            }
            replay, _ = apply_patch_ops_to_state(
                replay,
                patch_for_replay,
                scenario_after=target_scenario,
            )
            persisted_after = _loads(patch_row["after_state_json"])
            compare(
                persisted_after,
                replay,
                frame_seq=boundary,
                context={
                    "patch_version": target_version,
                    "patch_id": patch_row["patch_id"],
                    "observation": "patch-boundary-replay",
                },
            )
            if first_divergence is None:
                scope_check = self._verify_manifest(patch_row, persisted_after)
                scope_checks.append(scope_check)
                if not scope_check["ok"]:
                    first_divergence = {
                        "frame_seq": boundary,
                        "path": scope_check["path"],
                        "persisted": scope_check.get("persisted"),
                        "recomputed": scope_check.get("recomputed"),
                        "observation": {
                            "patch_version": target_version,
                            "patch_id": patch_row["patch_id"],
                            "observation": "invalidation-manifest",
                        },
                    }
            active_version = target_version
            active_scenario = target_scenario

        if first_divergence is None:
            process_until(int(current_state["committed_seq"]), active_scenario)
        if first_divergence is None:
            compare(
                current_state,
                replay,
                frame_seq=int(current_state["committed_seq"]),
                context={"observation": "final-state-replay"},
            )

        report: dict[str, Any] = {
            "ok": first_divergence is None,
            "connection_id": connection_id,
            "checked_frames": checked_frames,
            "patch_scope_checks": scope_checks,
            "persisted_state_digest": current_state["state_digest"],
            "recomputed_state_digest": replay["state_digest"],
            "first_divergence": first_divergence,
            "current_version": current_version,
            "epoch_version": epoch_version,
            "observation_sources": [
                "scenario_versions",
                "patch_commits",
                "state_commits",
                "frames",
            ],
        }
        report["report_digest"] = digest_json(report)
        return report

    def _verify_manifest(
        self,
        patch_row: dict[str, Any],
        persisted_after: dict[str, Any],
    ) -> dict[str, Any]:
        before = _loads(patch_row["before_state_json"])
        after = _loads(patch_row["after_state_json"])
        manifest = _loads(patch_row["manifest_json"])
        recomputed = set(manifest.get("recomputed", []))
        changed = {item["path"] for item in diff_paths(before, after)}
        missing = sorted(changed - recomputed)
        if missing:
            return {
                "ok": False,
                "path": missing[0],
                "persisted": "outside recomputed closure",
                "recomputed": "required path",
            }
        if manifest.get("classification") != patch_row["classified_kind"]:
            return {
                "ok": False,
                "path": "classification",
                "persisted": patch_row["classified_kind"],
                "recomputed": manifest.get("classification"),
            }
        affected = {int(value) for value in manifest.get("affected_streams", [])}
        for stream_id, expected_digest in manifest.get("reused_stream_digests", {}).items():
            if int(stream_id) in affected:
                continue
            stream = after["streams"].get(str(stream_id))
            if stream is None:
                return {
                    "ok": False,
                    "path": f"streams.{stream_id}.missing",
                    "persisted": "declared",
                    "recomputed": "missing",
                }
            observed_digest = digest_json(
                {
                    "state": stream["state"],
                    "weight": stream["weight"],
                    "send_window": stream["send_window"],
                    "recv_window": stream["recv_window"],
                    "local_end": stream["local_end"],
                    "remote_end": stream["remote_end"],
                    "header_digest": stream["header_digest"],
                }
            )
            if observed_digest != expected_digest:
                return {
                    "ok": False,
                    "path": f"streams.{stream_id}.reuse_digest",
                    "persisted": expected_digest,
                    "recomputed": observed_digest,
                }
        for stream_id, decision in manifest.get("header_decisions", {}).items():
            stream = after["streams"].get(str(stream_id))
            if stream is None:
                continue
            if decision.get("digest") != stream["header_digest"]:
                return {
                    "ok": False,
                    "path": f"headers.{stream_id}.digest",
                    "persisted": decision.get("digest"),
                    "recomputed": stream["header_digest"],
                }
        if persisted_after["state_digest"] != after["state_digest"]:
            return {
                "ok": False,
                "path": "state_digest",
                "persisted": persisted_after["state_digest"],
                "recomputed": after["state_digest"],
            }
        return {
            "ok": True,
            "version": int(patch_row["version"]),
            "classification": manifest.get("classification"),
            "recomputed_paths": len(recomputed),
            "changed_paths": len(changed),
            "affected_streams": sorted(affected),
        }
