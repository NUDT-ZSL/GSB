"""Business error codes and internal control-flow exceptions."""

E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_BUDGET_EXHAUSTED = "E_BUDGET_EXHAUSTED"
E_TASK_TIMEOUT = "E_TASK_TIMEOUT"
E_TASK_FAILED = "E_TASK_FAILED"


class TaskTimeout(Exception):
    """Raised inside model loops at cooperative checkpoints when the
    per-task billed quota is exceeded. The training loop itself performs
    the check; no thread timeout is involved. Partial progress (e.g. the
    loss trajectory of completed epochs) is preserved by the caller."""

    def __init__(self, used_ms, quota_ms):
        super().__init__(
            "task quota exceeded: used=%.3fms quota=%.3fms" % (used_ms, quota_ms)
        )
        self.used_ms = used_ms
        self.quota_ms = quota_ms


class TaskExecutionError(Exception):
    """Wraps an unexpected exception raised in a task phase so the
    scheduler can record (task_key, phase, exc_type) and continue."""

    def __init__(self, phase, original):
        self.phase = phase
        self.original = original
        super().__init__("%s: %s: %s" % (phase, type(original).__name__, original))
