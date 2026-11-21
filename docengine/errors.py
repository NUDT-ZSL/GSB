"""Business error codes. Each failure class maps to a distinct stable code."""


class ErrorCode:
    # v1 snapshot validation
    E_V1_DUPLICATE_DOC = "E_V1_DUPLICATE_DOC"
    E_V1_FIELD_TYPE = "E_V1_FIELD_TYPE"
    E_V1_SPAN_RANGE = "E_V1_SPAN_RANGE"
    E_V1_INVERTED_ORPHAN = "E_V1_INVERTED_ORPHAN"
    # v2 snapshot validation
    E_V2_SHARD_HASH = "E_V2_SHARD_HASH"
    E_V2_VERSION_REGRESS = "E_V2_VERSION_REGRESS"
    E_V2_FRAGMENT_CHECKSUM = "E_V2_FRAGMENT_CHECKSUM"
    E_V2_TOMBSTONE_ORPHAN = "E_V2_TOMBSTONE_ORPHAN"
    # query / fragment fetch
    E_FRAGMENT_CHECKSUM = "E_FRAGMENT_CHECKSUM"
    E_PREDICATE_TYPE = "E_PREDICATE_TYPE"
    E_QUERY_SYNTAX = "E_QUERY_SYNTAX"
    # state / clock
    E_CLOCK_REGRESSION = "E_CLOCK_REGRESSION"
    E_STATE_DIGEST_MISMATCH = "E_STATE_DIGEST_MISMATCH"
    E_BATCH_CONFLICT = "E_BATCH_CONFLICT"
    E_NO_INDEX = "E_NO_INDEX"


class BusinessError(Exception):
    """Error carrying a stable business code and diagnostic fields."""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        return {
            "error_code": self.code,
            "message": self.message,
            "details": self.details,
        }
