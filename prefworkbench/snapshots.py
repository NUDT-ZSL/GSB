"""Versioned, whole-snapshot replacement with per-category checksums."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import atomic_write_text, canonical_json, content_hash, read_json, write_json
from .errors import BusinessError, E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, E_SNAPSHOT_VERSION


CATEGORIES = ("manifest", "statistics", "error_spectrum")


def build_snapshot_categories(state: dict[str, Any]) -> dict[str, Any]:
    precision_results = state["precision_results"]
    manifest_payload = {
        "config_hash": state["config_hash"],
        "samples_sha256": state["samples_sha256"],
        "reference_precision": state["reference_precision"],
        "precision_manifests": {
            tier: {
                "precision": precision_results[tier]["precision"],
                "entries": precision_results[tier]["manifest"]["entries"],
                "selected_ids": precision_results[tier]["manifest"]["selected_ids"],
                "manifest_checksum": precision_results[tier]["manifest"]["manifest_checksum"],
                "estimated_bytes": precision_results[tier]["manifest"]["estimated_bytes"],
            }
            for tier in state["precision_order"]
        },
        "participation": {
            tier: precision_results[tier]["participation"] for tier in state["precision_order"]
        },
        "sampling": {
            tier: {
                "seed": precision_results[tier]["sampling_plan"]["sampling_seed"],
                "total_target": precision_results[tier]["sampling_plan"]["total_target"],
                "achieved_total": precision_results[tier]["sampling_plan"]["achieved_total"],
                "quota_results": precision_results[tier]["sampling_plan"]["quota_results"],
            }
            for tier in state["precision_order"]
        },
        "errors": state["errors"],
        "valid_pair_ids": state["valid_pair_ids"],
        "invalid_pair_ids": state["invalid_pair_ids"],
    }
    stats_payload = {
        "config": state["config"],
        "config_hash": state["config_hash"],
        "samples_sha256": state["samples_sha256"],
        "raw_sample_count": state["raw_sample_count"],
        "valid_sample_count": state["valid_sample_count"],
        "invalid_sample_count": state["invalid_sample_count"],
        "precision_order": state["precision_order"],
        "reference_precision": state["reference_precision"],
        "quality": {
            tier: precision_results[tier]["quality"] for tier in state["precision_order"]
        },
        "dedup": {
            tier: precision_results[tier]["dedup"] for tier in state["precision_order"]
        },
        "consistency": {
            tier: precision_results[tier]["consistency"] for tier in state["precision_order"]
        },
        "sampling": {
            tier: {
                "samples": precision_results[tier]["sampling"]["samples"],
                "selected_ids": precision_results[tier]["sampling"]["selected_ids"],
                "normalization_denominator": precision_results[tier]["sampling"][
                    "normalization_denominator"
                ],
                "denominator_protection_engaged": precision_results[tier]["sampling"][
                    "denominator_protection_engaged"
                ],
                "stage_width": precision_results[tier]["sampling"]["stage_width"],
                "sampling_plan": precision_results[tier]["sampling_plan"],
            }
            for tier in state["precision_order"]
        },
        "stage_keys": state["iteration"]["stage_keys"],
        "stage_widths": {
            tier: precision_results[tier]["stage_widths"] for tier in state["precision_order"]
        },
        "iteration": state["iteration"],
        "logic_digest": state["execution"]["logic_digest"],
        "execution_mode": state["execution"]["mode"],
        "invalidation": state["execution"]["invalidation"],
        "diagnostics": state["diagnostics"],
        "errors": state["errors"],
    }
    return {
        "manifest": manifest_payload,
        "statistics": stats_payload,
        "error_spectrum": state["error_spectrum"],
    }


class SnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.root / "latest.json"

    def _snapshot_path(self, version: int) -> Path:
        return self.root / f"snapshot-{version:06d}.json"

    def _read_latest_pointer(self) -> dict[str, Any] | None:
        if not self.latest_path.exists():
            return None
        try:
            pointer = read_json(self.latest_path)
        except (OSError, ValueError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"cannot read latest snapshot pointer: {exc}",
                stage="snapshot",
                details={"category": "latest"},
            ) from exc
        if not isinstance(pointer, dict) or not isinstance(pointer.get("version"), int):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "latest snapshot pointer is malformed",
                stage="snapshot",
                details={"category": "latest"},
            )
        return pointer

    @staticmethod
    def _payload_checksums(categories: dict[str, Any]) -> dict[str, str]:
        return {category: content_hash(categories[category]) for category in CATEGORIES}

    def commit(self, categories: dict[str, Any]) -> dict[str, Any]:
        missing = [category for category in CATEGORIES if category not in categories]
        if missing:
            raise BusinessError(
                E_SNAPSHOT_VERSION,
                "snapshot categories are incomplete",
                field="categories",
                stage="snapshot",
                details={"missing": missing},
            )
        pointer = self._read_latest_pointer()
        payload_checksums = self._payload_checksums(categories)
        if pointer is not None:
            try:
                latest = self.load(pointer["version"])
            except BusinessError:
                latest = None
            if latest is not None:
                latest_stats = latest["categories"]["statistics"]["payload"]
                new_stats = categories["statistics"]
                stable_categories_match = all(
                    latest["categories"][category]["payload_checksum"]
                    == payload_checksums[category]
                    for category in ("manifest", "error_spectrum")
                )
                if (
                    stable_categories_match
                    and latest_stats.get("logic_digest")
                    and latest_stats.get("logic_digest") == new_stats.get("logic_digest")
                    and latest_stats.get("config_hash") == new_stats.get("config_hash")
                    and latest_stats.get("samples_sha256") == new_stats.get("samples_sha256")
                ):
                    return {
                        "version": latest["version"],
                        "overall_checksum": latest["checksum"],
                        "category_checksums": {
                            category: latest["categories"][category]["checksum"]
                            for category in CATEGORIES
                        },
                        "replaced": False,
                        "path": str(self._snapshot_path(latest["version"])),
                        "changed_categories": [],
                        "idempotence_basis": "logic_digest",
                    }
                latest_payload_checksums = {
                    category: latest["categories"][category]["payload_checksum"]
                    for category in CATEGORIES
                }
                if latest_payload_checksums == payload_checksums:
                    return {
                        "version": latest["version"],
                        "overall_checksum": latest["checksum"],
                        "category_checksums": {
                            category: latest["categories"][category]["checksum"]
                            for category in CATEGORIES
                        },
                        "replaced": False,
                        "path": str(self._snapshot_path(latest["version"])),
                        "changed_categories": [],
                        "idempotence_basis": "category_payload_checksums",
                    }
        version = (pointer["version"] + 1) if pointer is not None else 1
        envelopes: dict[str, Any] = {}
        changed: list[str] = []
        for category in CATEGORIES:
            envelope = {
                "category": category,
                "version": version,
                "checksum": content_hash(
                    {"category": category, "version": version, "payload": categories[category]}
                ),
                "payload_checksum": payload_checksums[category],
                "payload": categories[category],
            }
            envelopes[category] = envelope
            changed.append(category)
        overall = content_hash(
            {
                "schema": 1,
                "version": version,
                "categories": {
                    category: envelopes[category]["checksum"] for category in CATEGORIES
                },
            }
        )
        snapshot = {
            "schema": 1,
            "version": version,
            "checksum": overall,
            "categories": envelopes,
        }
        path = self._snapshot_path(version)
        atomic_write_text(path, canonical_json(snapshot) + "\n")
        write_json(
            self.latest_path,
            {
                "version": version,
                "checksum": overall,
                "path": path.name,
            },
        )
        return {
            "version": version,
            "overall_checksum": overall,
            "category_checksums": {
                category: envelopes[category]["checksum"] for category in CATEGORIES
            },
            "replaced": True,
            "path": str(path),
            "changed_categories": changed,
            "idempotence_basis": None,
        }

    def load(self, version: int | None = None) -> dict[str, Any]:
        pointer = self._read_latest_pointer()
        if pointer is None:
            raise BusinessError(
                E_SNAPSHOT_VERSION,
                "no snapshot is available",
                stage="snapshot",
                details={"category": "latest"},
            )
        selected_version = int(version if version is not None else pointer["version"])
        path = self._snapshot_path(selected_version)
        if not path.exists():
            if version is None:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "latest snapshot pointer references a missing snapshot file",
                    stage="snapshot",
                    details={
                        "category": "snapshot",
                        "version": selected_version,
                        "latest_version": pointer["version"],
                    },
                )
            raise BusinessError(
                E_SNAPSHOT_VERSION,
                "requested snapshot version does not exist",
                stage="snapshot",
                details={
                    "requested_version": selected_version,
                    "latest_version": pointer["version"],
                    "category": "snapshot",
                },
            )
        try:
            snapshot = read_json(path)
        except (OSError, ValueError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"cannot parse snapshot: {exc}",
                stage="snapshot",
                details={"category": "snapshot", "version": selected_version, "path": str(path)},
            ) from exc
        if not isinstance(snapshot, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot root is not an object",
                stage="snapshot",
                details={"category": "snapshot", "version": selected_version},
            )
        if snapshot.get("version") != selected_version:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot version does not match the latest pointer",
                stage="snapshot",
                details={"category": "snapshot", "version": selected_version},
            )
        categories = snapshot.get("categories")
        if not isinstance(categories, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot categories are missing",
                stage="snapshot",
                details={"category": "categories", "version": selected_version},
            )
        for category in CATEGORIES:
            envelope = categories.get(category)
            if not isinstance(envelope, dict):
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot category is missing",
                    stage="snapshot",
                    details={"category": category, "version": selected_version},
                )
            if envelope.get("category") != category or envelope.get("version") != selected_version:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot category identity or version mismatch",
                    stage="snapshot",
                    details={"category": category, "version": selected_version},
                )
            expected = content_hash(
                {
                    "category": category,
                    "version": selected_version,
                    "payload": envelope.get("payload"),
                }
            )
            if envelope.get("checksum") != expected:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot category checksum mismatch",
                    stage="snapshot",
                    details={"category": category, "version": selected_version},
                )
            if envelope.get("payload_checksum") != content_hash(envelope.get("payload")):
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot category payload checksum mismatch",
                    stage="snapshot",
                    details={"category": category, "version": selected_version},
                )
        expected_overall = content_hash(
            {
                "schema": 1,
                "version": selected_version,
                "categories": {
                    category: categories[category]["checksum"] for category in CATEGORIES
                },
            }
        )
        if snapshot.get("checksum") != expected_overall:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot overall checksum mismatch",
                stage="snapshot",
                details={"category": "snapshot", "version": selected_version},
            )
        if selected_version == pointer["version"] and pointer.get("checksum") != snapshot.get("checksum"):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "latest pointer checksum does not match the snapshot",
                stage="snapshot",
                details={"category": "latest", "version": selected_version},
            )
        return snapshot

    def attempt_rollback(self, target_version: int) -> None:
        pointer = self._read_latest_pointer()
        current = pointer["version"] if pointer is not None else None
        raise BusinessError(
            E_SNAPSHOT_ROLLBACK,
            "snapshot rollback is disabled; commits replace the whole latest snapshot",
            stage="snapshot",
            details={
                "category": "latest",
                "target_version": target_version,
                "current_version": current,
            },
        )

    def previous_state(self) -> dict[str, Any] | None:
        pointer = self._read_latest_pointer()
        if pointer is None:
            return None
        snapshot = self.load()
        statistics = snapshot["categories"]["statistics"]["payload"]
        return {
            "iteration": statistics["iteration"],
            "execution": {
                "logic_digest": statistics["logic_digest"],
                "mode": statistics["execution_mode"],
            },
            "snapshot_version": snapshot["version"],
            "snapshot_checksum": snapshot["checksum"],
        }

