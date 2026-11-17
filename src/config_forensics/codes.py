"""Stable business error codes used by reports and tests."""


class Code:
    JSON_INVALID = "E_JSON_INVALID"
    FILE_READ = "E_FILE_READ"
    SCHEMA_INVALID = "E_SCHEMA_INVALID"

    HOST_INVALID = "E_HOST_INVALID"
    SEQUENCE_INVALID = "E_SEQUENCE_INVALID"
    PATH_INVALID = "E_PATH_INVALID"
    VALUE_INVALID = "E_VALUE_INVALID"
    EVIDENCE_ID_MISSING = "E_EVIDENCE_ID_MISSING"
    EVIDENCE_ID_DUPLICATE = "E_EVIDENCE_ID_DUPLICATE"

    ALGORITHM_UNSUPPORTED = "E_ALGORITHM_UNSUPPORTED"
    ALGORITHM_DECLARATION_MISMATCH = "E_ALGORITHM_DECLARATION_MISMATCH"
    DIGEST_WIDTH_MISMATCH = "E_DIGEST_WIDTH_MISMATCH"
    DIGEST_INVALID = "E_DIGEST_INVALID"
    HMAC_KEY_MISSING = "E_HMAC_KEY_MISSING"
    TAMPERED = "E_TAMPERED"

    EVIDENCE_CONFLICT = "E_EVIDENCE_CONFLICT"
    LINK_INVALID = "E_LINK_INVALID"
    LINK_TARGET_MISSING = "E_LINK_TARGET_MISSING"
    LINK_TARGET_TAMPERED = "E_LINK_TARGET_TAMPERED"
    LINK_TARGET_CONFLICT = "E_LINK_TARGET_CONFLICT"
    LINK_CROSS_CHAIN = "E_LINK_CROSS_CHAIN"
    LINK_SEQUENCE_ORDER = "E_LINK_SEQUENCE_ORDER"
    SELF_LINK = "E_SELF_LINK"
    LINK_CYCLE = "E_LINK_CYCLE"
    CHAIN_FORK = "E_CHAIN_FORK"
    CHAIN_BROKEN = "E_CHAIN_BROKEN"
    CHAIN_MULTIPLE_ROOTS = "E_CHAIN_MULTIPLE_ROOTS"

    BASELINE_INVALID_RANGE = "E_BASELINE_INVALID_RANGE"
    BASELINE_INVALID_INTERVAL = "E_BASELINE_INVALID_INTERVAL"
    BASELINE_DUPLICATE = "E_BASELINE_DUPLICATE"
    BASELINE_MISSING = "E_BASELINE_MISSING"
    BASELINE_NOT_EFFECTIVE = "E_BASELINE_NOT_EFFECTIVE"

    FILE_TIMEOUT = "E_FILE_TIMEOUT"
    QUEUE_FULL = "E_QUEUE_FULL"

    CHECKPOINT_LOCK = "E_CHECKPOINT_LOCK"
    CHECKPOINT_CONFLICT = "E_CHECKPOINT_CONFLICT"
    CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
    CHECKPOINT_VERSION = "E_CHECKPOINT_VERSION"
    CHECKPOINT_CONFIG_CHANGED = "E_CHECKPOINT_CONFIG_CHANGED"
    OUTPUT_LOCK = "E_OUTPUT_LOCK"
    OUTPUT_WRITE = "E_OUTPUT_WRITE"


MESSAGES = {
    Code.JSON_INVALID: "file is not valid UTF-8 JSON",
    Code.FILE_READ: "file could not be read",
    Code.SCHEMA_INVALID: "declaration does not match the supported schema",
    Code.HOST_INVALID: "host identifier is missing or invalid",
    Code.SEQUENCE_INVALID: "observation sequence is missing or invalid",
    Code.PATH_INVALID: "configuration path is invalid",
    Code.VALUE_INVALID: "observed value is missing or not finite JSON",
    Code.EVIDENCE_ID_MISSING: "evidence id is missing",
    Code.EVIDENCE_ID_DUPLICATE: "evidence id maps to more than one observation",
    Code.ALGORITHM_UNSUPPORTED: "digest algorithm is not in the configured whitelist",
    Code.ALGORITHM_DECLARATION_MISMATCH: "digest prefix and algorithm field disagree",
    Code.DIGEST_WIDTH_MISMATCH: "digest bit width does not match the declared algorithm",
    Code.DIGEST_INVALID: "digest is not valid hexadecimal",
    Code.HMAC_KEY_MISSING: "hmac-sha256 requires a local key",
    Code.TAMPERED: "digest does not match normalized path and value",
    Code.EVIDENCE_CONFLICT: "duplicate host/sequence deliveries have different digests",
    Code.LINK_INVALID: "previous-evidence link has an invalid type",
    Code.LINK_TARGET_MISSING: "previous-evidence link target does not exist",
    Code.LINK_TARGET_TAMPERED: "previous-evidence link target was excluded as tampered",
    Code.LINK_TARGET_CONFLICT: "previous-evidence link target was excluded as conflicting",
    Code.LINK_CROSS_CHAIN: "previous-evidence link points to another host or config path",
    Code.LINK_SEQUENCE_ORDER: "previous-evidence link does not point to an earlier sequence",
    Code.SELF_LINK: "evidence links to itself",
    Code.LINK_CYCLE: "previous-evidence links form a cycle",
    Code.CHAIN_FORK: "more than one observation links to the same previous observation",
    Code.CHAIN_BROKEN: "observation chain is broken",
    Code.CHAIN_MULTIPLE_ROOTS: "one observation chain contains multiple roots",
    Code.BASELINE_INVALID_RANGE: "baseline value range endpoints are invalid",
    Code.BASELINE_INVALID_INTERVAL: "baseline effective interval is invalid",
    Code.BASELINE_DUPLICATE: "duplicate baseline declaration",
    Code.BASELINE_MISSING: "no baseline declaration exists for host/configuration path",
    Code.BASELINE_NOT_EFFECTIVE: "no baseline applies at any observed sequence",
    Code.FILE_TIMEOUT: "file exceeded its measured processing time budget",
    Code.QUEUE_FULL: "file admission queue is full",
    Code.CHECKPOINT_LOCK: "checkpoint lock is held by another writer",
    Code.CHECKPOINT_CONFLICT: "checkpoint generation changed before atomic replacement",
    Code.CHECKPOINT_CORRUPT: "checkpoint is incomplete or structurally invalid",
    Code.CHECKPOINT_VERSION: "checkpoint structure version changed",
    Code.CHECKPOINT_CONFIG_CHANGED: "digest whitelist or evidence context changed",
    Code.OUTPUT_LOCK: "output file lock is held by another writer",
    Code.OUTPUT_WRITE: "output could not be atomically written",
}


def message_for(code: str) -> str:
    return MESSAGES.get(code, code)
