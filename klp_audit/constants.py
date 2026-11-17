"""Protocol, policy, and report constants.

The binary format is intentionally small and self-indexing:

    file header (20 bytes)
    record directory (20 bytes per record)
    record payloads

All integers are big-endian. The directory lets the parser skip a damaged
payload and continue with subsequent records, which is required for batch
audit semantics.
"""

from __future__ import annotations

import struct

FILE_MAGIC = b"KLPB"
FILE_VERSION = 1
HEADER_SIZE = 20
DIRECTORY_ENTRY_SIZE = 20

FILE_HEADER = struct.Struct(">4sHHIII")
DIRECTORY_ENTRY = struct.Struct(">QIII")
RECORD_HEADER = struct.Struct(">IQHBBIIIII")

FILE_HEADER_SIZE = FILE_HEADER.size
DIRECTORY_ENTRY_SIZE = DIRECTORY_ENTRY.size
RECORD_HEADER_SIZE = RECORD_HEADER.size

MAX_RECORD_COUNT = 1_000_000
MAX_SESSION_ID_BYTES = 128
MAX_SALT_BYTES = 65_536
MAX_OUTPUT_BYTES = 16_777_216
MAX_LEVEL = 64

STAGES = ("negotiate", "derive", "rotate", "seal", "destroy")
STAGE_TO_ID = {name: index + 1 for index, name in enumerate(STAGES)}
ID_TO_STAGE = {index + 1: name for index, name in enumerate(STAGES)}

ALGORITHMS = ("HMAC-SHA256", "HKDF-SHA256", "PBKDF2-SHA256")
ALGORITHM_TO_ID = {name: index + 1 for index, name in enumerate(ALGORITHMS)}
ID_TO_ALGORITHM = {index + 1: name for index, name in enumerate(ALGORITHMS)}

ALGORITHM_WHITELIST_VERSION = "1"
CHECKPOINT_FORMAT_VERSION = 2
REPORT_SCHEMA_VERSION = "1.0"
TRUTH_SCHEMA_VERSION = "klp-truth-v1"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2

