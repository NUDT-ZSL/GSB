"""Observation snapshots, whole-view restoration and replay verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .errors import FormatError, ReplayError, SnapshotError, StateError
from .estimator import (
    ESTIMATOR_PARAMETERS_VERSION,
    EstimatorParameters,
    ReplayParameters,
    estimate_delays,
    estimator_parameters_from_dict,
)
from .replay import build_replay_plan
from .segment import (
    Segment,
    collect_records,
    iter_segment_paths,
    parse_segment,
    read_segment,
    segment_set_digest,
)
from .storage import ProgressStore
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    ensure_dir,
    mark_read_only,
    read_json,
    sha256_bytes,
    stable_json,
)

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_RE = re.compile(r"^snap-(\d{6})$")


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _comparison(item: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "item": item,
        "expected": expected,
        "actual": actual,
        "match": expected == actual,
    }


def _sample_projection(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_index": int(sample["sample_index"]),
        "commit_seq": int(sample["commit_seq"]),
        "batch_id": int(sample["batch_id"]),
        "commit_version": int(sample["commit_version"]),
        "commit_ts_ms": sample.get("commit_ts_ms"),
        "completion_ts_ms": int(sample["completion_ts_ms"]),
        "delay_ms": sample.get("delay_ms"),
        "status": sample["status"],
    }


def _sequence_digest(samples: Sequence[dict[str, Any]]) -> str:
    return hashlib.sha256(
        canonical_json_bytes([_sample_projection(sample) for sample in samples])
    ).hexdigest()


def compare_segment_entries(
    snapshot_entries: Sequence[dict[str, Any]],
    current_entries: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    expected = {entry["filename"]: entry for entry in snapshot_entries}
    actual = {entry["filename"]: entry for entry in current_entries}
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    shared = sorted(set(expected) & set(actual))
    digest_changed = [
        name
        for name in shared
        if expected[name].get("digest") != actual[name].get("digest")
    ]
    version_changed = [
        name
        for name in shared
        if expected[name].get("format_version") != actual[name].get("format_version")
        or expected[name].get("versions") != actual[name].get("versions")
    ]
    expected_versions = sorted(
        {
            int(version)
            for entry in snapshot_entries
            for version in entry.get("versions", [])
        }
    )
    actual_versions = sorted(
        {
            int(version)
            for entry in current_entries
            for version in entry.get("versions", [])
        }
    )
    expected_digest = segment_set_digest(snapshot_entries)
    actual_digest = segment_set_digest(current_entries)
    return {
        "segment_set_equal": expected_digest == actual_digest,
        "expected_segment_set_digest": expected_digest,
        "actual_segment_set_digest": actual_digest,
        "format_versions_equal": expected_versions == actual_versions,
        "expected_format_versions": expected_versions,
        "actual_format_versions": actual_versions,
        "added_segments": added,
        "removed_segments": removed,
        "digest_changed_segments": digest_changed,
        "version_changed_segments": version_changed,
        "changed_segments": sorted(set(added) | set(removed) | set(digest_changed) | set(version_changed)),
        "view_matches": expected_digest == actual_digest and expected_versions == actual_versions,
    }


class SnapshotManager:
    def __init__(
        self,
        *,
        root: Path,
        log_dir: Path,
        archive_dir: Path,
        store: ProgressStore,
        session_id: str = "default",
        estimator_parameters: EstimatorParameters | None = None,
    ) -> None:
        self.root = root
        self.log_dir = log_dir
        self.snapshot_dir = ensure_dir(root / "snapshots")
        self.archive_dir = ensure_dir(archive_dir)
        self.store = store
        self.session_id = session_id
        self.estimator_parameters = estimator_parameters or EstimatorParameters()

    def _next_snapshot_id(self) -> str:
        used: set[int] = set()
        for item in self.store.list_snapshots():
            match = SNAPSHOT_RE.match(item["snapshot_id"])
            if match:
                used.add(int(match.group(1)))
        for path in self.snapshot_dir.iterdir():
            match = SNAPSHOT_RE.match(path.name)
            if match:
                used.add(int(match.group(1)))
        number = max(used, default=0) + 1
        return f"snap-{number:06d}"

    def _current_segments(self) -> list[Segment]:
        return [read_segment(path) for path in iter_segment_paths(self.log_dir)]

    def _current_entries_lenient(self) -> list[dict[str, Any]]:
        """Describe the on-disk view without preventing repair of a corrupt file."""

        entries: list[dict[str, Any]] = []
        for path in iter_segment_paths(self.log_dir):
            data = path.read_bytes()
            try:
                segment = parse_segment(data, filename=path.name, path=str(path))
            except FormatError as exc:
                entries.append(
                    {
                        "filename": path.name,
                        "digest": sha256_bytes(data),
                        "segment_id": None,
                        "format_version": None,
                        "versions": [],
                        "record_count": 0,
                        "parse_error": exc.to_dict(),
                    }
                )
            else:
                entry = segment.to_manifest_entry()
                entry["parse_error"] = None
                entries.append(entry)
        return entries

    def _read_bundle(self, snapshot_path: Path, manifest: dict[str, Any]) -> list[Segment]:
        bundle_dir = snapshot_path / "segments"
        segments: list[Segment] = []
        for entry in sorted(manifest["segments"], key=lambda item: item["filename"]):
            path = bundle_dir / entry["filename"]
            if not path.exists():
                raise SnapshotError(
                    "snapshot bundle is missing a segment",
                    details={"snapshot": snapshot_path.name, "filename": entry["filename"]},
                )
            data = path.read_bytes()
            if sha256_bytes(data) != entry["digest"]:
                raise SnapshotError(
                    "snapshot bundle segment digest mismatch",
                    details={"snapshot": snapshot_path.name, "filename": entry["filename"]},
                )
            try:
                segment = parse_segment(
                    data, filename=entry["filename"], path=str(path)
                )
            except FormatError as exc:
                raise SnapshotError(
                    "snapshot bundle contains an invalid segment",
                    details={"snapshot": snapshot_path.name, "format_error": exc.to_dict()},
                ) from exc
            segments.append(segment)
        return segments

    def save(self) -> dict[str, Any]:
        segments = self._current_segments()
        snapshot_id = self._next_snapshot_id()
        snapshot_path = self.snapshot_dir / snapshot_id
        bundle_dir = snapshot_path / "segments"
        ensure_dir(bundle_dir)
        entries = [segment.to_manifest_entry() for segment in segments]
        versions = sorted(
            {version for segment in segments for version in segment.versions}
        )
        progress = self.store.progress_snapshot(self.session_id)
        samples = self.store.get_samples(self.session_id)
        if progress is not None:
            state = progress["state"]
            replay_parameters = ReplayParameters(
                replay_rate=int(state["replay_rate"]),
                concurrency=int(state["concurrency"]),
                clock_origin_ms=int(state["clock_origin_ms"]),
            )
            statistics = estimate_delays(
                samples,
                parameters=self.estimator_parameters,
                replay_parameters=replay_parameters,
                incomplete_unknown_batches=int(state["unknown_batch_count"]),
            )
        else:
            replay_parameters = None
            statistics = estimate_delays(
                [],
                parameters=self.estimator_parameters,
                replay_parameters=None,
            )

        for segment in segments:
            target = bundle_dir / segment.filename
            atomic_write_bytes(target, segment.data, mode=0o444)
            mark_read_only(target)

        manifest = {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "created_utc": _utc_now_text(),
            "session_id": self.session_id,
            "segments": entries,
            "record_count": sum(segment.record_count for segment in segments),
            "segment_set_digest": segment_set_digest(entries),
            "format_version_set": versions,
            "replay": {
                "progress": progress,
                "samples": samples,
                "replay_parameters": replay_parameters.to_dict()
                if replay_parameters is not None
                else None,
            },
            "estimator_parameters": self.estimator_parameters.to_dict(),
            "statistics": statistics,
        }
        atomic_write_json(snapshot_path / "manifest.json", manifest)
        self.store.register_snapshot(snapshot_id, snapshot_path, manifest)
        return manifest

    def load(self, snapshot_id: str) -> tuple[Path, dict[str, Any], list[Segment]]:
        record = self.store.get_snapshot(snapshot_id)
        if record is None:
            raise SnapshotError(
                "snapshot is not registered",
                details={"snapshot_id": snapshot_id},
            )
        path = Path(record["path"])
        manifest_path = path / "manifest.json"
        if not manifest_path.exists():
            raise SnapshotError(
                "snapshot manifest is missing",
                details={"snapshot_id": snapshot_id, "path": str(manifest_path)},
            )
        manifest = read_json(manifest_path)
        if manifest.get("snapshot_schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotError(
                "unsupported snapshot schema version",
                details={
                    "snapshot_id": snapshot_id,
                    "found": manifest.get("snapshot_schema_version"),
                    "supported": SNAPSHOT_SCHEMA_VERSION,
                },
            )
        segments = self._read_bundle(path, manifest)
        return path, manifest, segments

    def list(self) -> list[dict[str, Any]]:
        result = []
        for record in self.store.list_snapshots():
            manifest = record["manifest"]
            statistics = manifest.get("statistics", {})
            result.append(
                {
                    "snapshot_id": record["snapshot_id"],
                    "path": record["path"],
                    "created_utc": manifest.get("created_utc"),
                    "record_count": manifest.get("record_count", 0),
                    "format_version_set": manifest.get("format_version_set", []),
                    "segment_set_digest": manifest.get("segment_set_digest"),
                    "sample_count": statistics.get("sample_count", 0),
                    "estimate_status": statistics.get("estimate_status", "INSUFFICIENT"),
                }
            )
        return sorted(result, key=lambda item: item["snapshot_id"])

    def compare_with_disk(self, snapshot_id: str) -> dict[str, Any]:
        _, manifest, _ = self.load(snapshot_id)
        current_entries = self._current_entries_lenient()
        comparison = compare_segment_entries(manifest["segments"], current_entries)
        comparison["snapshot_id"] = snapshot_id
        return comparison

    def _archive_current_segments(self, snapshot_id: str) -> Path:
        base = self.archive_dir / f"restore-{snapshot_id}"
        suffix = 0
        while True:
            candidate = base if suffix == 0 else Path(f"{base}-{suffix}")
            if not candidate.exists():
                ensure_dir(candidate)
                break
            suffix += 1
        for path in sorted(self.log_dir.glob("*.rlog"), key=lambda item: item.name):
            atomic_write_bytes(candidate / path.name, path.read_bytes())
            mark_read_only(candidate / path.name)
        return candidate

    def restore(self, snapshot_id: str) -> dict[str, Any]:
        snapshot_path, manifest, segments = self.load(snapshot_id)
        current_entries = self._current_entries_lenient()
        comparison = compare_segment_entries(manifest["segments"], current_entries)
        archive_path: Path | None = None
        if not comparison["view_matches"]:
            archive_path = self._archive_current_segments(snapshot_id)
            expected_names = {entry["filename"] for entry in manifest["segments"]}
            for path in list(self.log_dir.glob("*.rlog")):
                if path.name not in expected_names:
                    path.unlink()
            bundle_dir = snapshot_path / "segments"
            for entry in sorted(manifest["segments"], key=lambda item: item["filename"]):
                data = (bundle_dir / entry["filename"]).read_bytes()
                atomic_write_bytes(self.log_dir / entry["filename"], data)
            self.store.replace_operations(
                [read_segment(self.log_dir / entry["filename"]) for entry in manifest["segments"]]
            )
        replay = manifest.get("replay", {})
        self.store.replace_session_snapshot(
            self.session_id,
            progress=replay.get("progress"),
            samples=replay.get("samples", []),
        )
        result = {
            "business_code": None,
            "snapshot_id": snapshot_id,
            "replace_mode": "WHOLE_VIEW_REPLACED"
            if not comparison["view_matches"]
            else "PROGRESS_AND_SAMPLES_REPLACED",
            "segments_replaced": not comparison["view_matches"],
            "archive_path": str(archive_path) if archive_path is not None else None,
            "comparison": comparison,
            "restored_sample_count": len(replay.get("samples", [])),
            "restored_progress": replay.get("progress") is not None,
            "restored_format_versions": manifest.get("format_version_set", []),
            "estimator_parameters": manifest.get("estimator_parameters"),
            "content_digest_verified": True,
        }
        return result

    def diff(self, left_id: str, right_id: str) -> dict[str, Any]:
        _, left, _ = self.load(left_id)
        _, right, _ = self.load(right_id)
        comparison = compare_segment_entries(left["segments"], right["segments"])
        left_replay = left.get("replay", {})
        right_replay = right.get("replay", {})
        left_samples = left_replay.get("samples", [])
        right_samples = right_replay.get("samples", [])
        left_stats = left.get("statistics", {})
        right_stats = right.get("statistics", {})
        statistic_fields = [
            "sample_count",
            "known_delay_count",
            "unknown_delay_count",
            "mean_fixed",
            "p95_ms",
            "max_delay_ms",
            "estimate_status",
            "confidence",
        ]
        return {
            "left_snapshot_id": left_id,
            "right_snapshot_id": right_id,
            "segment_diff": comparison,
            "version_diff": {
                "left": left.get("format_version_set", []),
                "right": right.get("format_version_set", []),
                "changed": left.get("format_version_set", [])
                != right.get("format_version_set", []),
            },
            "replay_parameter_diff": {
                "left": left_replay.get("replay_parameters"),
                "right": right_replay.get("replay_parameters"),
                "changed": left_replay.get("replay_parameters")
                != right_replay.get("replay_parameters"),
            },
            "estimator_parameter_diff": {
                "left": left.get("estimator_parameters"),
                "right": right.get("estimator_parameters"),
                "changed": left.get("estimator_parameters")
                != right.get("estimator_parameters"),
            },
            "statistic_diff": [
                {
                    "item": field,
                    "left": left_stats.get(field),
                    "right": right_stats.get(field),
                    "changed": left_stats.get(field) != right_stats.get(field),
                }
                for field in statistic_fields
            ],
            "sample_sequence_diff": {
                "left_count": len(left_samples),
                "right_count": len(right_samples),
                "left_digest": _sequence_digest(left_samples),
                "right_digest": _sequence_digest(right_samples),
                "changed": _sequence_digest(left_samples)
                != _sequence_digest(right_samples),
            },
        }

    def verify(
        self,
        snapshot_id: str,
        *,
        replay_rate: int | None = None,
        concurrency: int | None = None,
    ) -> dict[str, Any]:
        _, manifest, snapshot_segments = self.load(snapshot_id)
        current_entries = self._current_entries_lenient()
        segment_comparison = compare_segment_entries(
            manifest["segments"], current_entries
        )
        saved_params = manifest.get("replay", {}).get("replay_parameters")
        if saved_params is None:
            raise StateError("snapshot has no replay parameters to reproduce")
        effective_rate = (
            replay_rate if replay_rate is not None else int(saved_params["replay_rate"])
        )
        effective_concurrency = (
            concurrency
            if concurrency is not None
            else int(saved_params["concurrency"])
        )
        origin = int(saved_params["clock_origin_ms"])
        records = collect_records(snapshot_segments)
        plan = build_replay_plan(
            records,
            replay_rate=effective_rate,
            concurrency=effective_concurrency,
            clock_origin_ms=origin,
        )
        replayed_samples = list(plan.samples)
        saved_samples = manifest.get("replay", {}).get("samples", [])
        saved_estimator_data = manifest.get("estimator_parameters", {})
        try:
            saved_estimator = estimator_parameters_from_dict(saved_estimator_data)
        except Exception as exc:
            raise SnapshotError(
                "snapshot estimator parameters are not supported",
                details={
                    "snapshot_id": snapshot_id,
                    "estimator_parameters": saved_estimator_data,
                    "error": str(exc),
                },
            ) from exc
        replayed_stats = estimate_delays(
            replayed_samples,
            parameters=saved_estimator,
            replay_parameters=plan.parameters,
            incomplete_unknown_batches=0,
        )
        saved_stats = manifest.get("statistics", {})

        comparisons: list[dict[str, Any]] = []
        comparisons.append(
            _comparison(
                "segment_set_digest",
                manifest.get("segment_set_digest"),
                segment_comparison["actual_segment_set_digest"],
            )
        )
        comparisons.append(
            _comparison(
                "format_version_set",
                manifest.get("format_version_set", []),
                segment_comparison["actual_format_versions"],
            )
        )
        comparisons.append(
            _comparison(
                "replay_rate",
                int(saved_params["replay_rate"]),
                effective_rate,
            )
        )
        comparisons.append(
            _comparison(
                "concurrency",
                int(saved_params["concurrency"]),
                effective_concurrency,
            )
        )
        comparisons.append(
            _comparison(
                "clock_origin_ms",
                origin,
                plan.parameters.clock_origin_ms,
            )
        )
        comparisons.append(
            _comparison(
                "estimator_parameters",
                manifest.get("estimator_parameters"),
                saved_estimator.to_dict(),
            )
        )
        comparisons.append(
            _comparison(
                "estimator_contract_version",
                saved_estimator_data.get("version"),
                ESTIMATOR_PARAMETERS_VERSION,
            )
        )
        comparisons.append(
            _comparison(
                "sample_sequence_digest",
                _sequence_digest(saved_samples),
                _sequence_digest(replayed_samples),
            )
        )
        comparisons.append(
            _comparison("sample_count", len(saved_samples), len(replayed_samples))
        )
        for field in (
            "sample_count",
            "known_delay_count",
            "unknown_delay_count",
            "incomplete_unknown_batches",
            "mean_fixed",
            "p95_ms",
            "max_delay_ms",
            "estimate_status",
            "confidence",
            "schedule_error_upper_bound_ms",
            "total_estimate_error_upper_bound",
        ):
            comparisons.append(
                _comparison(f"statistics.{field}", saved_stats.get(field), replayed_stats.get(field))
            )
        mismatches = [item["item"] for item in comparisons if not item["match"]]
        if segment_comparison["added_segments"]:
            mismatches.append("segments.added:" + ",".join(segment_comparison["added_segments"]))
        if segment_comparison["removed_segments"]:
            mismatches.append("segments.removed:" + ",".join(segment_comparison["removed_segments"]))
        if segment_comparison["digest_changed_segments"]:
            mismatches.append(
                "segments.digest_changed:"
                + ",".join(segment_comparison["digest_changed_segments"])
            )
        if segment_comparison["version_changed_segments"]:
            mismatches.append(
                "segments.version_changed:"
                + ",".join(segment_comparison["version_changed_segments"])
            )
        # Preserve stable order while removing duplicates.
        mismatches = sorted(dict.fromkeys(mismatches))
        ok = not mismatches
        report = {
            "business_code": None if ok else "E_REPLAY",
            "ok": ok,
            "snapshot_id": snapshot_id,
            "comparisons": comparisons,
            "mismatch_items": mismatches,
            "segment_comparison": segment_comparison,
            "expected_replay_parameters": saved_params,
            "actual_replay_parameters": plan.parameters.to_dict(),
            "saved_sample_count": len(saved_samples),
            "replayed_sample_count": len(replayed_samples),
            "saved_statistics": saved_stats,
            "replayed_statistics": replayed_stats,
            "stable_order": [
                "segment_set_digest",
                "format_version_set",
                "replay_rate",
                "concurrency",
                "clock_origin_ms",
                "estimator_parameters",
                "sample_sequence_digest",
                "sample_count",
                "statistics",
            ],
        }
        if not ok:
            report["error"] = ReplayError(
                "snapshot observation is not reproducible",
                details={
                    "snapshot_id": snapshot_id,
                    "mismatch_items": mismatches,
                    "comparisons": [item for item in comparisons if not item["match"]],
                },
            ).to_dict()
        return report


def write_report(path: Path, report: dict[str, Any]) -> None:
    atomic_write_bytes(path, stable_json(report).encode("utf-8") + b"\n")
