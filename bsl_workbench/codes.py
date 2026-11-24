"""Stable business codes and format policy constants."""

from __future__ import annotations

MAGIC = b"BSL1"
SUPPORTED_VERSIONS = (1, 2)

# The prose lists 4 + 2 + 4 + 8 + 8 + 8 = 34 bytes while also calling the
# header "32-byte".  The parser therefore parses the fixed-width field prefix
# at the stated little-endian offsets and accepts a segment table at offset 32
# (compact header) or 34 (full eight-byte reserved field).
HEADER_PREFIX_FORMAT = "<4sHIQQ"
HEADER_PREFIX_SIZE = 26
DOCUMENTED_HEADER_SIZE = 32
RESERVED_SIZE = 8
LITERAL_HEADER_SIZE = HEADER_PREFIX_SIZE + RESERVED_SIZE

RECORD_FIXED_FORMAT = "<BBQQ32s"
RECORD_FIXED_SIZE = 50
UINT16_MAX = (1 << 16) - 1
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

SEGMENT_TYPE_BLOB = 0x01
SEGMENT_TYPE_NESTED = 0x10
SEGMENT_TYPE_REFS = 0x20
KNOWN_SEGMENT_TYPES = {
    SEGMENT_TYPE_BLOB: "blob",
    SEGMENT_TYPE_NESTED: "nested_bsl",
    SEGMENT_TYPE_REFS: "reference_list",
}

COMPRESSION_NONE = 0
COMPRESSION_SHA256 = 1
COMPRESSION_HMAC_SHA256 = 2
KNOWN_COMPRESSION = {
    COMPRESSION_NONE: "none",
    COMPRESSION_SHA256: "sha256",
    COMPRESSION_HMAC_SHA256: "hmac-sha256",
}

DEFAULT_HMAC_KEY = b"bsl-workbench-local-hmac-key-v1"
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_EXPANDED_BYTES = 16 * 1024 * 1024

LEDGER_LAYOUT_VERSION = 3
POLICY_VERSION = "segment-types=1,0x01/0x10/0x20;checks=1;digest-width=sha256-32"

E_HEADER = "E_HEADER"
E_BOUNDS = "E_BOUNDS"
E_OVERLAP = "E_OVERLAP"
E_SELFREF = "E_SELFREF"
E_DUP_NAME = "E_DUP_NAME"
E_CHECKSUM = "E_CHECKSUM"
E_DIGEST_WIDTH = "E_DIGEST_WIDTH"
E_COMPRESSION = "E_COMPRESSION"
E_DEPTH = "E_DEPTH"
E_CYCLE = "E_CYCLE"
E_EXPAND_LIMIT = "E_EXPAND_LIMIT"
E_LOCK = "E_LOCK"
E_CHECKPOINT = "E_CHECKPOINT"
E_LEDGER = "E_LEDGER"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_LOCK = 3
