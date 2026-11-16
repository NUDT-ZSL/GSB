"""Business error codes used across the workbench."""


class SimError(Exception):
    """Base error carrying a stable business code."""

    code = "E_UNKNOWN"

    def __init__(self, message, entry=None, field=None):
        self.entry = entry
        self.field = field
        parts = [f"[{self.code}] {message}"]
        if entry is not None:
            parts.append(f"entry={entry}")
        if field is not None:
            parts.append(f"field={field}")
        super().__init__(" ".join(parts))

    def to_log(self):
        return {"code": self.code, "message": str(self),
                "entry": self.entry, "field": self.field}


class ManifestError(SimError):
    code = "E_MANIFEST"


class AffinityError(SimError):
    code = "E_AFFINITY"


class StatsError(SimError):
    code = "E_STATS"


class ReplayError(SimError):
    code = "E_REPLAY"
