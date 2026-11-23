"""Stable error codes and structured exception details."""

from __future__ import annotations

from typing import Any, Mapping

E_FACE_BUDGET_EXCEEDED = "E_FACE_BUDGET_EXCEEDED"
E_TEXTURE_BLOCK_MISMATCH = "E_TEXTURE_BLOCK_MISMATCH"
E_ERROR_SPECTRUM_INVALID = "E_ERROR_SPECTRUM_INVALID"
E_THRESHOLD_POLICY_INVALID = "E_THRESHOLD_POLICY_INVALID"
E_TOLERANCE_INVALID = "E_TOLERANCE_INVALID"
E_INCREMENTAL_INCONSISTENT = "E_INCREMENTAL_INCONSISTENT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SELF_CHECK_FAILED = "E_SELF_CHECK_FAILED"
E_VERIFICATION_FAILED = "E_VERIFICATION_FAILED"
E_SCENE_REVISION_ROLLBACK = "E_SCENE_REVISION_ROLLBACK"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"


class SceneOptError(Exception):
    """Base exception carrying a stable code and machine-readable details."""

    code = "E_SCENEOPT"

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        **context: Any,
    ) -> None:
        self.message = str(message)
        merged: dict[str, Any] = {}
        if details:
            merged.update(dict(details))
        merged.update(context)
        self.details = merged
        super().__init__(self.__str__())

    def __str__(self) -> str:
        if not self.details:
            return f"{self.code}: {self.message}"
        detail_text = ", ".join(
            f"{key}={value!r}" for key, value in sorted(self.details.items())
        )
        return f"{self.code}: {self.message} ({detail_text})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class FaceBudgetExceeded(SceneOptError):
    code = E_FACE_BUDGET_EXCEEDED

    def __init__(
        self,
        *,
        scene_id: str,
        over_budget_faces: int,
        faces_after: int,
        target_face_budget: int,
    ) -> None:
        super().__init__(
            f"scene {scene_id!r} exceeds its face budget by {over_budget_faces} faces",
            scene_id=scene_id,
            over_budget_faces=int(over_budget_faces),
            over_by=int(over_budget_faces),
            faces_after=int(faces_after),
            target_face_budget=int(target_face_budget),
        )


class TextureBlockMismatch(SceneOptError):
    code = E_TEXTURE_BLOCK_MISMATCH

    def __init__(
        self,
        *,
        first_pixel: tuple[int, int],
        deviation_spectrum: Mapping[str, Any],
        scene_id: str | None = None,
    ) -> None:
        row, col = (int(first_pixel[0]), int(first_pixel[1]))
        context: dict[str, Any] = {
            "first_pixel": [row, col],
            "first_pixel_coordinates": [row, col],
            "deviation_spectrum": dict(deviation_spectrum),
        }
        if scene_id is not None:
            context["scene_id"] = scene_id
        super().__init__(
            f"block texture differs from full reference at first pixel {(row, col)}",
            **context,
        )


class ErrorSpectrumInvalid(SceneOptError):
    code = E_ERROR_SPECTRUM_INVALID

    def __init__(self, field: str, message: str | None = None, **context: Any) -> None:
        super().__init__(
            message or f"invalid error spectrum field {field!r}",
            field=field,
            **context,
        )


class ThresholdPolicyInvalid(SceneOptError):
    code = E_THRESHOLD_POLICY_INVALID

    def __init__(self, field: str, policy: Any, **context: Any) -> None:
        super().__init__(
            f"invalid threshold policy {policy!r}",
            field=field,
            policy=policy,
            **context,
        )


class ToleranceInvalid(SceneOptError):
    code = E_TOLERANCE_INVALID

    def __init__(self, field: str, value: Any, **context: Any) -> None:
        super().__init__(
            f"invalid tolerance in field {field!r}: {value!r}",
            field=field,
            value=value,
            **context,
        )


class IncrementalInconsistent(SceneOptError):
    code = E_INCREMENTAL_INCONSISTENT

    def __init__(self, first_scene_id: str, **context: Any) -> None:
        super().__init__(
            f"incremental result differs from full replay for scene {first_scene_id!r}",
            first_scene_id=first_scene_id,
            first_deviating_scene_id=first_scene_id,
            **context,
        )


class StageBusy(SceneOptError):
    code = E_STAGE_BUSY

    def __init__(self, stage: str = "incremental_commit", **context: Any) -> None:
        super().__init__(
            f"stage {stage!r} is busy; no waiting is performed",
            stage=stage,
            **context,
        )


class SnapshotRollback(SceneOptError):
    code = E_SNAPSHOT_ROLLBACK

    def __init__(self, attempted_version: int, current_version: int, **context: Any) -> None:
        super().__init__(
            "snapshot version does not advance monotonically",
            attempted_version=int(attempted_version),
            current_version=int(current_version),
            **context,
        )


class SnapshotCorrupt(SceneOptError):
    code = E_SNAPSHOT_CORRUPT

    def __init__(self, reason: str, **context: Any) -> None:
        super().__init__(f"snapshot is corrupt: {reason}", reason=reason, **context)


class ResourceLimitExceeded(SceneOptError):
    code = E_RESOURCE_LIMIT

    def __init__(self, resource: str, limit: Any, actual: Any, **context: Any) -> None:
        super().__init__(
            f"resource limit exceeded for {resource!r}",
            resource=resource,
            limit=limit,
            actual=actual,
            **context,
        )


class SelfCheckFailed(SceneOptError):
    code = E_SELF_CHECK_FAILED

    def __init__(self, issues: list[str], **context: Any) -> None:
        super().__init__(
            "cross-stage self-check failed",
            issues=list(issues),
            **context,
        )


class VerificationFailed(SceneOptError):
    code = E_VERIFICATION_FAILED

    def __init__(self, issues: list[str], **context: Any) -> None:
        super().__init__(
            "snapshot verification failed",
            issues=list(issues),
            **context,
        )


class SceneRevisionRollback(SceneOptError):
    code = E_SCENE_REVISION_ROLLBACK

    def __init__(self, scene_id: str, attempted: int, current: int) -> None:
        super().__init__(
            f"scene {scene_id!r} revision would move backwards",
            scene_id=scene_id,
            attempted_revision=int(attempted),
            current_revision=int(current),
        )


class CacheCorrupt(SceneOptError):
    code = E_CACHE_CORRUPT

    def __init__(self, stage: str, key: str, reason: str) -> None:
        super().__init__(
            f"cache entry {stage}/{key} is corrupt: {reason}",
            stage=stage,
            key=key,
            reason=reason,
        )

