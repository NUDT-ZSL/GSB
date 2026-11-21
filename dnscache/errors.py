"""Business error codes. Every validation/operational failure raises BizError
with a stable code so callers (and tests) can assert on it."""


class BizError(Exception):
    def __init__(self, code, message, context=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.context = context or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "context": self.context}


E_V1_SCHEMA = "E1001_V1_SCHEMA"
E_V1_DUPLICATE_DOMAIN = "E1002_V1_DUPLICATE_DOMAIN"
E_V1_TTL_INVALID = "E1003_V1_TTL_INVALID"
E_V1_CLOCK_INVALID = "E1004_V1_CLOCK_INVALID"

E_V2_SCHEMA = "E2001_V2_SCHEMA"
E_V2_SHARD_HASH_MISMATCH = "E2002_V2_SHARD_HASH_MISMATCH"
E_V2_GENERATION_REGRESSION = "E2003_V2_GENERATION_REGRESSION"
E_V2_DUPLICATE_ENTRY = "E2004_V2_DUPLICATE_ENTRY"
E_V2_EXPIRY_INVALID = "E2005_V2_EXPIRY_INVALID"

E_SNAPSHOT_VERSION_ROLLBACK = "E3001_SNAPSHOT_VERSION_ROLLBACK"
E_REPLACE_CONFLICT = "E3002_REPLACE_CONFLICT"

E_UPSTREAM_MALFORMED = "E4001_UPSTREAM_MALFORMED"
E_UPSTREAM_TIMEOUT = "E4002_UPSTREAM_TIMEOUT"
E_UPSTREAM_UNREACHABLE = "E4003_UPSTREAM_UNREACHABLE"

E_JOURNAL_CORRUPT = "E5001_JOURNAL_CORRUPT"

E_NAME_INVALID = "E9001_NAME_INVALID"
E_NOT_FOUND = "E9002_NOT_FOUND"
