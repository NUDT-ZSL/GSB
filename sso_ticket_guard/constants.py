"""Stable constants shared by the library, CLI, and tests."""

SCHEMA_VERSION = 1
CANONICAL_DECIMALS = 6
SUPPORTED_PLATFORMS = frozenset({"android", "ios", "web"})
COUNT_QUOTA_VERSION = "device-window-seconds/v1"
INTERVAL_QUOTA_VERSION = "ticket-interval-millis/v1"
QUOTA_PROFILE_VERSION = f"{COUNT_QUOTA_VERSION}+{INTERVAL_QUOTA_VERSION}"

SECONDS_TO_MILLIS_STEP = 1_000
SECONDS_TO_MILLIS_ERROR_MS = SECONDS_TO_MILLIS_STEP // 2

# The count window has two second-derived endpoints: its anchor and its end.
WINDOW_ENDPOINT_ERROR_MS = SECONDS_TO_MILLIS_ERROR_MS * 2

BATCH_SIZE = 3

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ARGUMENT = 2

BUSINESS_CODES = frozenset(
    {
        "E_CHECKPOINT_CORRUPT",
        "E_DEVICE_MISMATCH",
        "E_FP_AMBIGUOUS",
        "E_GENERATION_EXCEEDED",
        "E_GENERATION_INCONSISTENT",
        "E_ID_DUPLICATE",
        "E_INVALID_RECORD",
        "E_LOCK",
        "E_NO_CHECKPOINT",
        "E_NOT_YET_VALID",
        "E_PLATFORM",
        "E_QUOTA_AMBIGUOUS",
        "E_QUOTA_COUNT",
        "E_QUOTA_INTERVAL",
        "E_TICKET_CYCLE",
        "E_TICKET_EXPIRED",
        "E_TICKET_PERIOD",
        "E_UNKNOWN_DEVICE",
        "E_UNKNOWN_TICKET",
    }
)

