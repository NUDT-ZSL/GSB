"""Stable constants and exit codes for the memory-region guard."""

COMPONENT_NAME = "memguard"
VERSION = "1.0.0"

PROT_CALIBER_VERSION = "prot-caliber-rwx-v1"
CREDENTIAL_VERSION = "credential-v1"
POLICY_ALGEBRA_VERSION = "policy-algebra-v2"
LEDGER_SCHEMA_VERSION = "ledger-schema-v3"
LAYOUT_VERSION = "memguard-layout-2026-11-25"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_LOCK = 3
EXIT_INTEGRITY = 4

CANONICAL_MAP_KINDS = ("image", "anon", "stack")

PROT_NONE = 0
PROT_READ = 1 << 0
PROT_WRITE = 1 << 1
PROT_EXEC = 1 << 2
PROT_GUARD = 1 << 3
PROT_NOCACHE = 1 << 4
KNOWN_PROT_BITS = (
    PROT_READ
    | PROT_WRITE
    | PROT_EXEC
    | PROT_GUARD
    | PROT_NOCACHE
)
PERMISSION_BITS = PROT_READ | PROT_WRITE | PROT_EXEC

CODE_OK = "E_OK"
CODE_PARAM = "E_PARAM"
CODE_LOCK = "E_LOCK"
CODE_JSON = "E_JSON_SYNTAX"
CODE_FIELD_TYPE = "E_FIELD_TYPE"
CODE_FIELD_VALUE = "E_FIELD_VALUE"
CODE_SEQ = "E_EVENT_SEQ_INVALID"
CODE_SEQ_DUPLICATE = "E_EVENT_SEQ_DUPLICATE"
CODE_ENDPOINT = "E_ENDPOINT_ID_INVALID"
CODE_REGION_DUPLICATE = "E_REGION_ID_DUPLICATE"
CODE_ADDR_RANGE = "E_ADDR_RANGE_INVALID"
CODE_ADDR_OVERFLOW = "E_ADDR_OVERFLOW"
CODE_MAP_KIND = "E_MAP_KIND_UNSUPPORTED"
CODE_PROT = "E_PROT_REPRESENTATION_INVALID"
CODE_TIME = "E_TIME_VALUE_INVALID"
CODE_FLAG = "E_FLAG_INVALID"
CODE_POLICY = "E_POLICY_RECORD_INVALID"
CODE_V2_META = "E_V2_META_MISMATCH"
CODE_REGION_UNDECLARED = "E_REGION_UNDECLARED"
CODE_VIEW_MISMATCH = "E_REGION_VIEW_MISMATCH"
CODE_POLICY_MISSING = "E_POLICY_MISSING"
CODE_POLICY_TOMBSTONED = "E_POLICY_TOMBSTONED"
CODE_POLICY_DISABLED = "E_POLICY_DISABLED"
CODE_POLICY_CONFLICT = "E_POLICY_CONFLICT"
CODE_UNCERTAIN = "E_ADJUDICATION_UNCERTAIN"
CODE_OWNER_DENIED = "E_OWNER_POLICY_DENIED"
CODE_EXTERNAL_INJECTION = "E_EXTERNAL_INJECTION"
CODE_CRED_CYCLE = "E_CRED_CYCLE"
CODE_HALF_WRITE = "E_HALF_WRITE_RESIDUE"
CODE_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
CODE_REPLAY_DIVERGED = "E_REPLAY_DIVERGED"
CODE_LAYOUT = "E_LEDGER_LAYOUT_CHANGED"


class GuardError(Exception):
    code = "E_GUARD"
    exit_code = EXIT_INTEGRITY

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class UsageError(GuardError):
    code = CODE_PARAM
    exit_code = EXIT_USAGE


class LockConflict(GuardError):
    code = CODE_LOCK
    exit_code = EXIT_LOCK


class HalfWriteError(GuardError):
    code = CODE_HALF_WRITE
    exit_code = EXIT_INTEGRITY


class CheckpointCorrupt(GuardError):
    code = CODE_CHECKPOINT_CORRUPT
    exit_code = EXIT_INTEGRITY


class ReplayDiverged(GuardError):
    code = CODE_REPLAY_DIVERGED
    exit_code = EXIT_INTEGRITY


class CredentialCycle(GuardError):
    code = CODE_CRED_CYCLE
    exit_code = EXIT_INTEGRITY

    def __init__(self, message, cycles, **kwargs):
        super().__init__(message, details={"cycles": cycles, **kwargs})
        self.cycles = cycles
