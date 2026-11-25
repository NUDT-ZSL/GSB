"""Business errors and stable machine-readable error codes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass
class ErrorLocation:
    instance_id: str | None = None
    level: int | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.instance_id is not None:
            result["instance_id"] = self.instance_id
        if self.level is not None:
            result["level"] = self.level
        if self.path is not None:
            result["path"] = self.path
        return result


class HSMError(Exception):
    code = "HSM-9000"
    exit_code = 2

    def __init__(
        self,
        message: str,
        *,
        location: ErrorLocation | Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
        code: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if exit_code is not None:
            self.exit_code = exit_code
        if location is None:
            self.location = ErrorLocation()
        elif isinstance(location, ErrorLocation):
            self.location = location
        else:
            self.location = ErrorLocation(**dict(location))
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "location": self.location.to_dict(),
                "details": self.details,
            },
        }


class RegistryError(HSMError):
    code = "HSM-1000"


class InputParseError(HSMError):
    code = "HSM-1000"


class DuplicateServiceName(RegistryError):
    code = "HSM-1001"


class DuplicateInstanceId(RegistryError):
    code = "HSM-1002"


class InvalidLoopbackEndpoint(RegistryError):
    code = "HSM-1003"


class InvalidLevelSet(RegistryError):
    code = "HSM-1004"


class DuplicateLevel(RegistryError):
    code = "HSM-1005"


class InvalidGeneration(RegistryError):
    code = "HSM-1006"


class ObservationError(HSMError):
    code = "HSM-1100"


class UnknownObservationInstance(ObservationError):
    code = "HSM-1101"


class UnknownObservationLevel(ObservationError):
    code = "HSM-1102"


class ObservationSequenceError(ObservationError):
    code = "HSM-1103"


class InvalidLogicalClock(ObservationError):
    code = "HSM-1104"


class InvalidCount(ObservationError):
    code = "HSM-1105"


class InvalidLatency(ObservationError):
    code = "HSM-1106"


class ThresholdV1Error(HSMError):
    code = "HSM-1200"


class V1ThresholdLengthMismatch(ThresholdV1Error):
    code = "HSM-1201"


class V1ThresholdValueInvalid(ThresholdV1Error):
    code = "HSM-1202"


class V1TimeValueInvalid(ThresholdV1Error):
    code = "HSM-1203"


class V1UnknownInstance(ThresholdV1Error):
    code = "HSM-1204"


class ThresholdV2Error(HSMError):
    code = "HSM-1300"


class V2ShardHashMismatch(ThresholdV2Error):
    code = "HSM-1301"


class V2GenerationNotMonotonic(ThresholdV2Error):
    code = "HSM-1302"


class InvalidTombstone(ThresholdV2Error):
    code = "HSM-1303"


class V2ShardMissing(ThresholdV2Error):
    code = "HSM-1304"


class MergeError(HSMError):
    code = "HSM-2000"


class NoActiveThreshold(MergeError):
    code = "HSM-2001"


class ProbeError(HSMError):
    code = "HSM-3000"


class InvalidStateTransition(HSMError):
    code = "HSM-4001"


class RecoveryMismatch(HSMError):
    code = "HSM-5001"


class PatchError(HSMError):
    code = "HSM-6000"


class InvalidPatch(PatchError):
    code = "HSM-6001"


class ConcurrencyError(HSMError):
    code = "HSM-7001"
    exit_code = 5


class VerificationMismatch(HSMError):
    code = "HSM-8001"
    exit_code = 6


class StorageError(HSMError):
    code = "HSM-9100"


def error_payload(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, HSMError):
        return exc.to_dict()
    return {
        "ok": False,
        "error": {
            "code": "HSM-9999",
            "message": str(exc) or exc.__class__.__name__,
            "location": {},
            "details": {"type": exc.__class__.__name__},
        },
    }
