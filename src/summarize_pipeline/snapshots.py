from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import ErrorCode, PipelineError
from .models import PipelineConfig
from .utils import read_json, sha256_object, write_json_atomic

SNAPSHOT_SECTIONS = (
    "scoring_config",
    "smoothing_threshold_config",
    "error_spectrum_float64",
    "error_spectrum_float32",
    "error_spectrum_fixed",
)


def _snapshot_error(
    code: str,
    message: str,
    **detail: Any,
) -> PipelineError:
    return PipelineError(code, "snapshot", "*", message, detail)


def _snapshot_payload_without_checksum(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "checksum"}


def _make_section(payload: dict[str, Any], version: int) -> dict[str, Any]:
    return {
        "version": version,
        "payload": payload,
        "checksum": sha256_object(payload),
    }


def build_snapshot(
    report: dict[str, Any],
    config: PipelineConfig,
    version: int,
) -> dict[str, Any]:
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise _snapshot_error(
            ErrorCode.INVALID_CONFIG,
            "snapshot version must be a positive integer",
            snapshot_version=version,
        )
    spectra = report.get("error_spectra", {})
    sections = {
        "scoring_config": _make_section(config.scoring_payload(), version),
        "smoothing_threshold_config": _make_section(
            config.smoothing_threshold_payload(), version
        ),
        "error_spectrum_float64": _make_section(
            spectra.get("float64", {}), version
        ),
        "error_spectrum_float32": _make_section(
            spectra.get("float32", {}), version
        ),
        "error_spectrum_fixed": _make_section(
            spectra.get("fixed", {}), version
        ),
    }
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "snapshot_version": version,
        "config_version": config.version,
        "config_checksum": sha256_object(config.config_payload()),
        "run_hash": report.get("run_hash"),
        "sections": sections,
    }
    snapshot["checksum"] = sha256_object(_snapshot_payload_without_checksum(snapshot))
    return snapshot


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise _snapshot_error(
            ErrorCode.SNAPSHOT_CORRUPT, "snapshot root must be an object"
        )
    checksum = snapshot.get("checksum")
    if not isinstance(checksum, str):
        raise _snapshot_error(
            ErrorCode.SNAPSHOT_CORRUPT, "snapshot checksum is missing"
        )
    expected = sha256_object(_snapshot_payload_without_checksum(snapshot))
    if checksum != expected:
        raise _snapshot_error(
            ErrorCode.SNAPSHOT_CORRUPT,
            "snapshot checksum mismatch",
            expected=expected,
            actual=checksum,
        )
    version = snapshot.get("snapshot_version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise _snapshot_error(
            ErrorCode.SNAPSHOT_CORRUPT,
            "snapshot_version must be a positive integer",
        )
    sections = snapshot.get("sections")
    if not isinstance(sections, dict):
        raise _snapshot_error(
            ErrorCode.SNAPSHOT_CORRUPT, "snapshot sections are missing"
        )
    for name in SNAPSHOT_SECTIONS:
        section = sections.get(name)
        if not isinstance(section, dict):
            raise _snapshot_error(
                ErrorCode.SNAPSHOT_CORRUPT,
                f"snapshot section {name} is missing",
                section=name,
            )
        section_version = section.get("version")
        if (
            isinstance(section_version, bool)
            or not isinstance(section_version, int)
            or section_version != version
        ):
            raise _snapshot_error(
                ErrorCode.SNAPSHOT_CORRUPT,
                f"section {name} version must equal snapshot_version",
                section=name,
                section_version=section_version,
                snapshot_version=version,
            )
        payload = section.get("payload")
        section_checksum = section.get("checksum")
        if not isinstance(payload, dict) or not isinstance(section_checksum, str):
            raise _snapshot_error(
                ErrorCode.SNAPSHOT_CORRUPT,
                f"section {name} payload/checksum is malformed",
                section=name,
            )
        expected_section = sha256_object(payload)
        if section_checksum != expected_section:
            raise _snapshot_error(
                ErrorCode.SNAPSHOT_CORRUPT,
                f"section {name} checksum mismatch",
                section=name,
                expected=expected_section,
                actual=section_checksum,
            )
    return snapshot


class SnapshotStore:
    """Whole-snapshot store with a monotonic high-water mark.

    A snapshot is never patched section by section. A newer version replaces
    the complete in-memory snapshot and current.json atomically.
    """

    def __init__(self, directory: str | Path, *, autoload: bool = True) -> None:
        self.directory = Path(directory)
        self.history_directory = self.directory / "history"
        self.current_path = self.directory / "current.json"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.history_directory.mkdir(parents=True, exist_ok=True)
        self.current: dict[str, Any] | None = None
        self.high_water_version = self._discover_high_water()
        if autoload:
            self._load_initial_current()

    def _discover_high_water(self) -> int:
        highest = 0
        for path in self.history_directory.glob("v*.json"):
            match = re.fullmatch(r"v(\d+)\.json", path.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest

    def _load_initial_current(self) -> None:
        if self.current_path.exists():
            snapshot = self._read_and_validate(self.current_path)
            self.current = snapshot
            self.high_water_version = max(
                self.high_water_version, int(snapshot["snapshot_version"])
            )
            return
        if self.high_water_version > 0:
            latest = self.history_directory / f"v{self.high_water_version:08d}.json"
            if latest.exists():
                snapshot = self._read_and_validate(latest)
                self.current = snapshot
                write_json_atomic(self.current_path, snapshot, pretty=False)

    def _read_and_validate(self, path: Path) -> dict[str, Any]:
        try:
            payload = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise _snapshot_error(
                ErrorCode.SNAPSHOT_CORRUPT,
                f"cannot read snapshot: {exc}",
                path=str(path),
            ) from exc
        return validate_snapshot(payload)

    def _check_candidate(
        self, snapshot: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | None]:
        version = int(snapshot["snapshot_version"])
        current = self.current
        current_version = (
            int(current["snapshot_version"]) if current is not None else 0
        )
        high_water = max(self.high_water_version, current_version)
        if version < high_water:
            raise _snapshot_error(
                ErrorCode.SNAPSHOT_ROLLBACK,
                "snapshot version is older than the stored high-water mark",
                attempted_version=version,
                high_water_version=high_water,
            )
        if version == high_water and current is not None:
            if snapshot["checksum"] != current["checksum"]:
                raise _snapshot_error(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    "same version with different content is not an in-place update",
                    version=version,
                )
            return False, current
        if version == high_water and current is None and self.high_water_version > 0:
            historical = self.history_directory / f"v{version:08d}.json"
            if historical.exists():
                old = self._read_and_validate(historical)
                if old["checksum"] != snapshot["checksum"]:
                    raise _snapshot_error(
                        ErrorCode.SNAPSHOT_CORRUPT,
                        "same version with different content is not an in-place update",
                        version=version,
                    )
        return True, None

    def _persist(self, snapshot: dict[str, Any]) -> None:
        version = int(snapshot["snapshot_version"])
        history_path = self.history_directory / f"v{version:08d}.json"
        write_json_atomic(history_path, snapshot, pretty=False)
        write_json_atomic(self.current_path, snapshot, pretty=False)
        self.current = snapshot
        self.high_water_version = max(self.high_water_version, version)

    def commit(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        validated = validate_snapshot(snapshot)
        should_replace, existing = self._check_candidate(validated)
        if not should_replace:
            return existing if existing is not None else validated
        self._persist(validated)
        return validated

    def load(self, path: str | Path) -> dict[str, Any]:
        snapshot = self._read_and_validate(Path(path))
        should_replace, existing = self._check_candidate(snapshot)
        if not should_replace:
            return existing if existing is not None else snapshot
        self._persist(snapshot)
        return snapshot

    def get_section(self, name: str) -> dict[str, Any]:
        if self.current is None:
            raise _snapshot_error(
                ErrorCode.SNAPSHOT_CORRUPT, "no current snapshot is loaded"
            )
        section = self.current["sections"].get(name)
        if section is None:
            raise _snapshot_error(
                ErrorCode.SNAPSHOT_CORRUPT,
                f"snapshot section {name} is missing",
                section=name,
            )
        return section

