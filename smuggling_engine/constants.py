"""Stable engine constants.

The strings in this module are part of the report/evidence contract.  They are
deliberately independent from localized human-readable messages.
"""

REPORT_SCHEMA_VERSION = "1.0"
CHECKPOINT_SCHEMA_VERSION = 1
MERGE_POLICY_VERSION = "rules-merge-v1"
ANALYSIS_POLICY_VERSION = "http-smuggling-analysis-v1"

BUSINESS_CL_TE = "SMUG_CL_TE"
BUSINESS_DUP_HEADER = "SMUG_DUP_HEADER"
BUSINESS_CHUNK_HEX = "SMUG_CHUNK_HEX"
BUSINESS_TRAILING_BYTES = "SMUG_TRAILING_BYTES"

AUX_HTTP_PARSE = "AUX_HTTP_PARSE"
AUX_INVALID_CL = "AUX_INVALID_CONTENT_LENGTH"
AUX_INCOMPLETE_BODY = "AUX_INCOMPLETE_BODY"
AUX_CHUNK_TERMINATOR = "AUX_CHUNK_TERMINATOR"

FINDING_PRIORITY = {
    BUSINESS_CL_TE: 0,
    BUSINESS_DUP_HEADER: 1,
    BUSINESS_CHUNK_HEX: 2,
    BUSINESS_TRAILING_BYTES: 3,
    AUX_INVALID_CL: 4,
    AUX_CHUNK_TERMINATOR: 5,
    AUX_INCOMPLETE_BODY: 6,
    AUX_HTTP_PARSE: 7,
}

CONNECTION_ERRORS = {
    "E_MSG_GAP",
    "E_MIXUP",
    "E_TIMEOUT",
    "E_MSGS",
    "E_BYTES",
    "E_LOCK",
    "E_INTERNAL",
}

SOURCE_RAW = "raw_message"
SOURCE_V1 = "rules_v1"
SOURCE_V2 = "rules_v2"
SOURCE_CHECKPOINT = "checkpoint"

DIRECTION_REQUEST = "request"
DIRECTION_RESPONSE = "response"
DIRECTION_BOTH = "both"

_DIRECTION_ALIASES = {
    "request": DIRECTION_REQUEST,
    "req": DIRECTION_REQUEST,
    "c2s": DIRECTION_REQUEST,
    "client-to-server": DIRECTION_REQUEST,
    "client_to_server": DIRECTION_REQUEST,
    "upstream": DIRECTION_REQUEST,
    "inbound": DIRECTION_REQUEST,
    "ingress": DIRECTION_REQUEST,
    "response": DIRECTION_RESPONSE,
    "resp": DIRECTION_RESPONSE,
    "s2c": DIRECTION_RESPONSE,
    "server-to-client": DIRECTION_RESPONSE,
    "server_to_client": DIRECTION_RESPONSE,
    "downstream": DIRECTION_RESPONSE,
    "outbound": DIRECTION_RESPONSE,
    "egress": DIRECTION_RESPONSE,
    "both": DIRECTION_BOTH,
    "any": DIRECTION_BOTH,
}


def normalize_direction(value):
    if not isinstance(value, str):
        return None
    return _DIRECTION_ALIASES.get(value.strip().casefold())

