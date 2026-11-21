"""业务错误码与异常类型。每个异常携带 clip_id 与行/切片定位信息。"""
from __future__ import annotations

E_FORMAT_VERSION_UNSUPPORTED = "E_FORMAT_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_TIME_INVALID = "E_TIME_INVALID"
E_DUP_UTTERANCE = "E_DUP_UTTERANCE"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_LINEAGE_STALE = "E_LINEAGE_STALE"
E_AUDIO_RANGE = "E_AUDIO_RANGE"
E_EMPTY_SLICE = "E_EMPTY_SLICE"
E_NO_ACTIVITY = "E_NO_ACTIVITY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"


class AsrGovError(Exception):
    code = "E_UNKNOWN"

    def __init__(self, message, *, clip_id=None, location=None, details=None):
        super().__init__(message)
        self.message = message
        self.clip_id = clip_id
        self.location = location
        self.details = dict(details or {})

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "clip_id": self.clip_id,
            "location": self.location,
            "details": self.details,
        }


class FormatVersionUnsupported(AsrGovError):
    code = E_FORMAT_VERSION_UNSUPPORTED


class FormatCorrupt(AsrGovError):
    code = E_FORMAT_CORRUPT


class TimeInvalid(AsrGovError):
    code = E_TIME_INVALID


class DupUtterance(AsrGovError):
    code = E_DUP_UTTERANCE


class SemanticDrift(AsrGovError):
    code = E_SEMANTIC_DRIFT


class LineageStale(AsrGovError):
    code = E_LINEAGE_STALE


class AudioRange(AsrGovError):
    code = E_AUDIO_RANGE


class EmptySlice(AsrGovError):
    code = E_EMPTY_SLICE


class NoActivity(AsrGovError):
    code = E_NO_ACTIVITY


class ResourceLimit(AsrGovError):
    code = E_RESOURCE_LIMIT


class SnapshotRollback(AsrGovError):
    code = E_SNAPSHOT_ROLLBACK


class SnapshotCorrupt(AsrGovError):
    code = E_SNAPSHOT_CORRUPT


class StageBusy(AsrGovError):
    code = E_STAGE_BUSY
