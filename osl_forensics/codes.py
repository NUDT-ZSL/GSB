"""Stable business error codes used by reports, markers and tests."""


class Code:
    OK = "OK"
    E_HEADER = "E_HEADER"
    E_BOUNDS = "E_BOUNDS"
    E_OVERLAP = "E_OVERLAP"
    E_SELFREF = "E_SELFREF"
    E_DUP_NAME = "E_DUP_NAME"
    E_CHECKSUM = "E_CHECKSUM"
    E_CHECK_ALGO = "E_CHECK_ALGO"
    E_DEPTH = "E_DEPTH"
    E_CYCLE = "E_CYCLE"
    E_QUOTA = "E_QUOTA"
    E_TIMEOUT = "E_TIMEOUT"
    E_LOCK = "E_LOCK"
    E_QUEUE_FULL = "E_QUEUE_FULL"
    E_SNAPSHOT_VERSION = "E_SNAPSHOT_VERSION"
    E_INTERNAL = "E_INTERNAL"


CODES = tuple(
    value
    for name, value in vars(Code).items()
    if isinstance(value, str) and name.isupper()
)


FATAL_PARSE_CODES = frozenset(
    {
        Code.E_HEADER,
        Code.E_BOUNDS,
        Code.E_TIMEOUT,
        Code.E_QUOTA,
        Code.E_DEPTH,
    }
)
