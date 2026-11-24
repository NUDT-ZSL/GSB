"""Business error types and stable error codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_CODES = {
    "E_CONFIG": "Task or output configuration is invalid.",
    "E_INPUT": "Input descriptor is missing or invalid.",
    "E_INPUT_MISSING": "Input audio file does not exist.",
    "E_WAV": "WAV container or sample data is malformed.",
    "E_SR": "Sample rate is unsupported or contradicts the declaration.",
    "E_BIT_DEPTH": "WAV bit depth is not 16 or 24 bits.",
    "E_CHANNELS": "Channel count does not match the declared layout.",
    "E_LAYOUT": "Declared channel layout is invalid.",
    "E_WEIGHTS": "Channel weights are invalid.",
    "E_WINDOW": "Window or hop length is outside its domain.",
    "E_GATE": "Loudness gate parameter is outside its domain.",
    "E_DRANGE": "Dynamic-range bounds are invalid.",
    "E_BUDGET": "A single input or the scheduler budget is invalid.",
    "E_SNAPSHOT": "Criteria snapshot is malformed or internally inconsistent.",
    "E_DIGEST": "A required content digest is missing or does not match.",
    "E_SQLITE": "The v2 SQLite metadata database cannot be read.",
    "E_ANOMALY": "Anomaly injection manifest is invalid.",
    "E_IO": "A local file operation failed.",
    "E_INTERRUPTED": "Execution stopped at a safe boundary.",
    "E_RUNTIME": "An unexpected runtime failure occurred.",
    "E_SILENT": "All measured windows contain digital silence.",
    "E_BELOW_GATE": "Audio energy exists but no window passes the configured gates.",
}


@dataclass
class PipelineError(Exception):
    """A coded error with enough context to locate the offending object."""

    code: str
    message: str
    input_id: str | None = None
    source: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)
    recoverable: bool = True

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown pipeline error code: {self.code}")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.input_id is not None:
            result["input_id"] = self.input_id
        if self.source is not None:
            result["source"] = self.source
        if self.details:
            result["details"] = self.details
        return result


class InterruptedRun(PipelineError):
    """Raised only after a signal has reached a safe persistence boundary."""

    def __init__(self, signum: int, *, input_id: str | None = None,
                 boundary: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            "E_INTERRUPTED",
            f"interrupted by signal {signum} at {boundary}",
            input_id=input_id,
            details={"signal": signum, "boundary": boundary, **(details or {})},
            recoverable=False,
        )
        self.signum = signum
