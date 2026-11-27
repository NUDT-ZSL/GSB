"""Business error codes and the diagnostic :class:`CalibrationError`.

Every recoverable, domain-level failure in the dome calibration kernel raises
:class:`CalibrationError` carrying a stable string code plus a human readable
message.  The same codes are surfaced unchanged by both the command line and
the Tkinter GUI.
"""

from __future__ import annotations


# ---- stable business error codes ------------------------------------------
E_INSUFFICIENT_POINTS = "E1001_INSUFFICIENT_CONTROL_POINTS"
E_NEAR_COLLINEAR = "E1002_NEAR_COLLINEAR_POINTS"
E_NO_DOME_INTERSECTION = "E1003_PROJECTOR_DOME_NO_OVERLAP"
E_DISTORTION_OUT_OF_RANGE = "E1004_DISTORTION_OUT_OF_RANGE"
E_RESOLUTION_MISMATCH = "E1005_RESOLUTION_MISMATCH"
E_INVALID_PARAMETER = "E1006_INVALID_PARAMETER"
E_VERSION_CONFLICT = "E1007_STALE_VERSION_CONFLICT"
E_SNAPSHOT_IO = "E1008_SNAPSHOT_IO"
E_EMPTY_LAYOUT = "E1009_EMPTY_PROJECTOR_LAYOUT"
E_GEOMETRY_DEGRADED = "E1010_GEOMETRY_SOLVE_DEGRADED"


class CalibrationError(Exception):
    """Raised for diagnosable business failures.

    Attributes
    ----------
    code:
        One of the ``E####_*`` constants defined in this module.
    details:
        Optional structured context (projector id, offending value, ...).
    """

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


__all__ = [
    "CalibrationError",
    "E_INSUFFICIENT_POINTS",
    "E_NEAR_COLLINEAR",
    "E_NO_DOME_INTERSECTION",
    "E_DISTORTION_OUT_OF_RANGE",
    "E_RESOLUTION_MISMATCH",
    "E_INVALID_PARAMETER",
    "E_VERSION_CONFLICT",
    "E_SNAPSHOT_IO",
    "E_EMPTY_LAYOUT",
    "E_GEOMETRY_DEGRADED",
]
