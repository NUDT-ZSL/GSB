"""Global and per-stage atomic snapshots with strict resume validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import read_json, sha256_obj, write_json_atomic
from .config import Config


SNAPSHOT_SCHEMA = "snapshot/v1"


class ConfigMismatchError(RuntimeError):
    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__("E_CONFIG_MISMATCH")
        self.code = "E_CONFIG_MISMATCH"
        self.detail = detail


class SnapshotStore:
    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.state_path = self.output_dir / "state" / "state.json"
        self.stage_dir = self.output_dir / "state" / "stages"
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "state").mkdir(parents=True, exist_ok=True)

    def load_state(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        try:
            value = read_json(self.state_path)
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        claimed = value.get("state_sha256")
        body = {key: item for key, item in value.items() if key != "state_sha256"}
        if not isinstance(claimed, str) or sha256_obj(body) != claimed:
            return None
        return value

    def load_stage(self, stage: str) -> dict[str, Any] | None:
        path = self.stage_dir / f"{stage}.json"
        if not path.exists():
            return None
        try:
            value = read_json(path)
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        claimed = value.get("stage_snapshot_sha256")
        body = {key: item for key, item in value.items() if key != "stage_snapshot_sha256"}
        if not isinstance(claimed, str) or sha256_obj(body) != claimed:
            return None
        return value

    def validate_for_resume(
        self,
        state: dict[str, Any] | None,
        *,
        config: Config,
        config_sha256: str,
        injection_sha256: str,
        explicit_resume: bool,
    ) -> dict[str, Any]:
        if state is None:
            if explicit_resume:
                raise ConfigMismatchError(
                    {
                        "reason": "resume_requested_but_no_complete_snapshot_manifest_exists",
                        "actual": {
                            "snapshot_schema": SNAPSHOT_SCHEMA,
                            "global_version": config.global_version,
                            "config_sha256": config_sha256,
                            "compatibility_config_sha256": config.compatibility_hash(),
                            "injection_sha256": injection_sha256,
                        },
                    }
                )
            return {"mode": "fresh", "resume": False}
        complete = bool(state.get("complete", False))
        prior_compatibility_hash = state.get(
            "compatibility_config_sha256", state.get("config_sha256")
        )
        if (
            state.get("snapshot_schema") != SNAPSHOT_SCHEMA
            or state.get("global_version") != config.global_version
        ):
            raise ConfigMismatchError(
                {
                    "reason": "snapshot_schema_or_global_version_mismatch",
                    "expected": {
                        "snapshot_schema": state.get("snapshot_schema"),
                        "global_version": state.get("global_version"),
                    },
                    "actual": {
                        "snapshot_schema": SNAPSHOT_SCHEMA,
                        "global_version": config.global_version,
                    },
                }
            )
        same = (
            state.get("global_version") == config.global_version
            and prior_compatibility_hash == config.compatibility_hash()
            and state.get("injection_sha256") == injection_sha256
            and state.get("snapshot_schema") == SNAPSHOT_SCHEMA
        )
        if explicit_resume and not same:
            raise ConfigMismatchError(
                {
                    "expected": {
                        "snapshot_schema": SNAPSHOT_SCHEMA,
                        "global_version": state.get("global_version"),
                        "config_sha256": state.get("config_sha256"),
                        "compatibility_config_sha256": prior_compatibility_hash,
                        "injection_sha256": state.get("injection_sha256"),
                    },
                    "actual": {
                        "snapshot_schema": SNAPSHOT_SCHEMA,
                        "global_version": config.global_version,
                        "config_sha256": config_sha256,
                        "compatibility_config_sha256": config.compatibility_hash(),
                        "injection_sha256": injection_sha256,
                    },
                }
            )
        if not complete and not same:
            raise ConfigMismatchError(
                {
                    "reason": "incomplete_snapshot_cannot_continue_under_different_config_or_manifest",
                    "expected": {
                        "global_version": state.get("global_version"),
                        "config_sha256": state.get("config_sha256"),
                        "compatibility_config_sha256": prior_compatibility_hash,
                        "injection_sha256": state.get("injection_sha256"),
                    },
                    "actual": {
                        "global_version": config.global_version,
                        "config_sha256": config_sha256,
                        "compatibility_config_sha256": config.compatibility_hash(),
                        "injection_sha256": injection_sha256,
                    },
                }
            )
        if not complete:
            return {"mode": "resume", "resume": True}
        return {"mode": "incremental", "resume": True}

    def write_stage(
        self,
        *,
        stage: str,
        stage_index: int,
        total_stages: int,
        complete: bool,
        cursor: dict[str, Any],
        products: dict[str, dict[str, str]],
        config: Config,
        config_sha256: str,
        injection_sha256: str,
        timings_ms: dict[str, int],
    ) -> dict[str, Any]:
        payload = {
            "snapshot_schema": SNAPSHOT_SCHEMA,
            "global_version": config.global_version,
            "config_sha256": config_sha256,
            "compatibility_config_sha256": config.compatibility_hash(),
            "injection_sha256": injection_sha256,
            "stage": stage,
            "stage_index": stage_index,
            "total_stages": total_stages,
            "complete": complete,
            "cursor": cursor,
            "products": products,
            "stage_timings_ms": timings_ms,
        }
        payload["stage_snapshot_sha256"] = sha256_obj(payload)
        write_json_atomic(self.stage_dir / f"{stage}.json", payload)
        return payload

    def write_state(
        self,
        *,
        config: Config,
        config_sha256: str,
        injection_sha256: str,
        completed_stages: list[str],
        stage_cursors: dict[str, Any],
        product_versions: dict[str, str],
        complete: bool,
        interrupted: bool,
        timings_ms: dict[str, int],
    ) -> dict[str, Any]:
        payload = {
            "snapshot_schema": SNAPSHOT_SCHEMA,
            "global_version": config.global_version,
            "config_sha256": config_sha256,
            "compatibility_config_sha256": config.compatibility_hash(),
            "injection_sha256": injection_sha256,
            "completed_stages": completed_stages,
            "stage_cursors": stage_cursors,
            "product_versions": product_versions,
            "complete": complete,
            "interrupted": interrupted,
            "stage_timings_ms": timings_ms,
        }
        payload["state_sha256"] = sha256_obj(payload)
        write_json_atomic(self.state_path, payload)
        return payload
