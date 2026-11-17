"""Business error codes used across parsing, validation and persistence."""

# certificate description parsing
E_CERT_DUP_ID = "E_CERT_DUP_ID"
E_CERT_ISSUER_MISSING = "E_CERT_ISSUER_MISSING"
E_CERT_VALIDITY = "E_CERT_VALIDITY"
E_CERT_FINGERPRINT = "E_CERT_FINGERPRINT"
E_CERT_ID_FORMAT = "E_CERT_ID_FORMAT"
E_CERT_SCHEMA = "E_CERT_SCHEMA"

# chain claim parsing
E_CHAIN_SCHEMA = "E_CHAIN_SCHEMA"
E_CHAIN_EMPTY = "E_CHAIN_EMPTY"
E_CHAIN_DUP = "E_CHAIN_DUP"
E_CHAIN_REF_MISSING = "E_CHAIN_REF_MISSING"

# path validation
E_CHAIN_INTERMEDIATE_MISSING = "E_CHAIN_INTERMEDIATE_MISSING"
E_ROOT_MIDCHAIN = "E_ROOT_MIDCHAIN"
E_ROOT_UNTRUSTED = "E_ROOT_UNTRUSTED"
E_ROOT_MISMATCH = "E_ROOT_MISMATCH"

# snapshot management
E_SNAPSHOT_SCHEMA = "E_SNAPSHOT_SCHEMA"
E_SNAPSHOT_STRUCTURE_VERSION = "E_SNAPSHOT_STRUCTURE_VERSION"
E_SNAPSHOT_POLICY = "E_SNAPSHOT_POLICY"
E_POLICY_UNKNOWN = "E_POLICY_UNKNOWN"
E_SNAPSHOT_OBS_TIME = "E_SNAPSHOT_OBS_TIME"

# invalidation propagation
E_REVOKED_LEAF = "E_REVOKED_LEAF"
E_REVOKED_INTERMEDIATE = "E_REVOKED_INTERMEDIATE"
E_REVOKED_ROOT = "E_REVOKED_ROOT"
E_EXPIRED = "E_EXPIRED"
E_NOT_YET_VALID = "E_NOT_YET_VALID"

# persistence / concurrency
E_LOCK = "E_LOCK"
E_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
E_CHECKPOINT_VERSION = "E_CHECKPOINT_VERSION"
E_NO_CHECKPOINT = "E_NO_CHECKPOINT"
E_NO_SNAPSHOT = "E_NO_SNAPSHOT"

# cli
E_ARGS = "E_ARGS"


class CertTrustError(Exception):
    """Exception carrying a business error code."""

    def __init__(self, code, message, **context):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.context = context

    def to_record(self):
        record = {"code": self.code, "message": self.message}
        record.update(self.context)
        return record
