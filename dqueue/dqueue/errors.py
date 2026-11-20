"""Business error codes and the single exception type of the package."""


class ErrorCode:
    E_PARSE = "E_PARSE"
    E_TASK_DUPLICATE = "E_TASK_DUPLICATE"
    E_TASK_NOT_FOUND = "E_TASK_NOT_FOUND"
    E_TOPIC_INVALID = "E_TOPIC_INVALID"
    E_CLOCK_INVALID = "E_CLOCK_INVALID"
    E_RETRIES_INVALID = "E_RETRIES_INVALID"
    E_POLICY_NOT_FOUND = "E_POLICY_NOT_FOUND"
    E_POLICY_INVALID = "E_POLICY_INVALID"
    E_PAYLOAD_DIGEST_MISMATCH = "E_PAYLOAD_DIGEST_MISMATCH"
    E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
    E_MODE_VERSION_ROLLBACK = "E_MODE_VERSION_ROLLBACK"
    E_PULL_NOT_FOUND = "E_PULL_NOT_FOUND"
    E_FOREIGN_CONSUMER = "E_FOREIGN_CONSUMER"
    E_TASK_NOT_RUNNING = "E_TASK_NOT_RUNNING"
    E_DEAD_LETTER = "E_DEAD_LETTER"
    E_STATE_CORRUPT = "E_STATE_CORRUPT"
    E_VERIFY = "E_VERIFY"


class DQError(Exception):
    """Business exception carrying a stable code and diagnostic fields."""

    def __init__(self, code, message, **diag):
        super().__init__(message)
        self.code = code
        self.message = message
        self.diag = diag

    def to_dict(self):
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "diag": self.diag,
        }


# Distinct process exit status per business code, keeps the demo repeatable.
EXIT_STATUS = {
    ErrorCode.E_PARSE: 20,
    ErrorCode.E_TASK_DUPLICATE: 21,
    ErrorCode.E_TASK_NOT_FOUND: 22,
    ErrorCode.E_TOPIC_INVALID: 23,
    ErrorCode.E_CLOCK_INVALID: 24,
    ErrorCode.E_RETRIES_INVALID: 25,
    ErrorCode.E_POLICY_NOT_FOUND: 26,
    ErrorCode.E_POLICY_INVALID: 27,
    ErrorCode.E_PAYLOAD_DIGEST_MISMATCH: 28,
    ErrorCode.E_CLOCK_ROLLBACK: 29,
    ErrorCode.E_MODE_VERSION_ROLLBACK: 30,
    ErrorCode.E_PULL_NOT_FOUND: 31,
    ErrorCode.E_FOREIGN_CONSUMER: 32,
    ErrorCode.E_TASK_NOT_RUNNING: 33,
    ErrorCode.E_DEAD_LETTER: 34,
    ErrorCode.E_STATE_CORRUPT: 35,
    ErrorCode.E_VERIFY: 36,
}


def exit_status_for(code):
    return EXIT_STATUS.get(code, 1)
