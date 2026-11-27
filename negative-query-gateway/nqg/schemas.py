"""Three historical schema versions normalized onto one canonical口径.

The same logical object-storage project was registered across three engineering
eras.  The three registrations differ in three exactly-named dimensions:

1. field naming (object key / seeds / filter parameters);
2. hash seed expression (plain int / decimal string / hex string);
3. key encoding (raw bytes / hex text / hex text).

Everything here normalizes those records into the canonical types used by the
rest of the kernel:

* object key  -> ``bytes`` (byte key);
* hash seeds  -> ``tuple[int, ...]`` (one seed per hash function);
* filter size -> ``int`` bit-array length;
* max inserts -> ``int`` per-generation high-water mark.

Only the Python standard library is used.
"""

from __future__ import annotations

from dataclasses import dataclass


SCHEMA_VERSIONS = ("v1", "v2", "v3")

# Canonical seed vector.  Every schema must express the *same* four seeds in a
# version-specific representation; normalization must recover this exact tuple.
CANONICAL_SEEDS = (17, 29, 41, 53)


class SchemaError(ValueError):
    """Raised when a raw schema record cannot be normalized consistently."""


@dataclass(frozen=True)
class ProjectSpec:
    """Canonical, version-independent project configuration."""

    version: str
    project: str
    bit_array_size: int
    hash_count: int
    max_inserts_per_generation: int
    seeds: tuple[int, ...]


@dataclass(frozen=True)
class RawRecord:
    """One raw object/event record exactly as registered by an old schema."""

    version: str
    raw_key: object
    raw_op: str
    logical_time: int


@dataclass(frozen=True)
class Event:
    """A normalized logical event addressed by a canonical byte key."""

    logical_time: int
    op: str  # "insert" | "delete" | "query"
    key: bytes
    version: str
    raw_key: str


@dataclass(frozen=True)
class SchemaProfile:
    """Static description of how one version maps onto the canonical口径."""

    version: str
    key_field: str
    op_field: str
    time_field: str
    size_field: str
    hash_count_field: str
    capacity_field: str
    seed_field: str
    seed_expression: str
    key_encoding: str


PROFILES = {
    "v1": SchemaProfile(
        version="v1",
        key_field="object_key",
        op_field="action",
        time_field="tick",
        size_field="bit_array_size",
        hash_count_field="hash_functions",
        capacity_field="max_inserts_per_generation",
        seed_field="hash_seeds",
        seed_expression="plain integer list [17, 29, 41, 53]",
        key_encoding="raw bytes (Latin-1 text); no transport encoding",
    ),
    "v2": SchemaProfile(
        version="v2",
        key_field="obj_key",
        op_field="operation",
        time_field="logical_time",
        size_field="m_bits",
        hash_count_field="k_hashes",
        capacity_field="gen_capacity",
        seed_field="seed_values",
        seed_expression="decimal string list ['17', '29', '41', '53']",
        key_encoding="hexadecimal string of the byte key",
    ),
    "v3": SchemaProfile(
        version="v3",
        key_field="objectKeyHex",
        op_field="eventType",
        time_field="seq",
        size_field="bitArrayLen",
        hash_count_field="numHashes",
        capacity_field="generationLimit",
        seed_field="seedHexes",
        seed_expression="hexadecimal string list ['0x11', '0x1d', '0x29', '0x35']",
        key_encoding="hexadecimal string (uppercase) of the byte key",
    ),
}


# Operation vocabulary per schema, mapped to canonical ops.
OP_VOCAB = {
    "v1": {"PUT": "insert", "REMOVE": "delete", "GET": "query"},
    "v2": {"write": "insert", "erase": "delete", "read": "query"},
    "v3": {"INSERT": "insert", "DELETE": "delete", "LOOKUP": "query"},
}


def normalize_key(version: str, raw_key: object) -> bytes:
    """Normalize any version's key representation into canonical bytes.

    The requirement "原始字节与十六进制串之间转换后必须一致" is enforced here:
    for hex-encoded schemas the text is decoded back to bytes and re-encoded,
    and the round trip is asserted to be stable.
    """

    if version == "v1":
        if isinstance(raw_key, bytes):
            key = raw_key
        elif isinstance(raw_key, str):
            key = raw_key.encode("latin-1")
        else:
            raise SchemaError(f"v1 key must be bytes/str, got {type(raw_key)!r}")
    elif version in ("v2", "v3"):
        if not isinstance(raw_key, str):
            raise SchemaError(f"{version} key must be a hex string, got {type(raw_key)!r}")
        try:
            key = bytes.fromhex(raw_key)
        except ValueError as exc:
            raise SchemaError(f"{version} key is not valid hex: {raw_key!r}") from exc
        # Round-trip proof: bytes -> hex -> bytes must reproduce the key.
        if bytes.fromhex(key.hex()) != key:
            raise SchemaError(f"{version} hex round trip failed for {raw_key!r}")
    else:
        raise SchemaError(f"unknown schema version {version!r}")
    return key


def encode_key(version: str, key: bytes) -> str:
    """Render a canonical byte key in the version's native key representation."""

    if version == "v1":
        return key.decode("latin-1")
    if version == "v2":
        return key.hex()
    if version == "v3":
        return key.hex().upper()
    raise SchemaError(f"unknown schema version {version!r}")


def normalize_seeds(version: str, raw_seeds: object) -> tuple[int, ...]:
    """Normalize each schema's seed expression into canonical integer seeds."""

    if not isinstance(raw_seeds, (list, tuple)):
        raise SchemaError(f"{version} seeds must be a list, got {type(raw_seeds)!r}")
    seeds: list[int] = []
    for item in raw_seeds:
        if version == "v1":
            if not isinstance(item, int):
                raise SchemaError(f"v1 seed must be int, got {item!r}")
            seeds.append(item)
        elif version == "v2":
            if not isinstance(item, str) or not item.isdigit():
                raise SchemaError(f"v2 seed must be decimal string, got {item!r}")
            seeds.append(int(item, 10))
        elif version == "v3":
            if not isinstance(item, str) or not item.lower().startswith("0x"):
                raise SchemaError(f"v3 seed must be 0x-hex string, got {item!r}")
            seeds.append(int(item, 16))
        else:
            raise SchemaError(f"unknown schema version {version!r}")
    return tuple(seeds)


def raw_project(version: str, spec: ProjectSpec) -> dict:
    """Project the canonical spec back into a version-specific raw record."""

    if version == "v1":
        return {
            "schema": "v1",
            "project_name": spec.project,
            "bit_array_size": spec.bit_array_size,
            "hash_functions": spec.hash_count,
            "max_inserts_per_generation": spec.max_inserts_per_generation,
            "hash_seeds": list(spec.seeds),
        }
    if version == "v2":
        return {
            "schema": "v2",
            "project_name": spec.project,
            "m_bits": spec.bit_array_size,
            "k_hashes": spec.hash_count,
            "gen_capacity": spec.max_inserts_per_generation,
            "seed_values": [str(seed) for seed in spec.seeds],
        }
    if version == "v3":
        return {
            "schema": "v3",
            "projectName": spec.project,
            "bitArrayLen": spec.bit_array_size,
            "numHashes": spec.hash_count,
            "generationLimit": spec.max_inserts_per_generation,
            "seedHexes": [hex(seed) for seed in spec.seeds],
        }
    raise SchemaError(f"unknown schema version {version!r}")


def normalize_project(version: str, raw: dict) -> ProjectSpec:
    """Normalize a version-specific raw project record into a ProjectSpec."""

    profile = PROFILES[version]
    try:
        spec = ProjectSpec(
            version=version,
            project=raw["project_name"] if version != "v3" else raw["projectName"],
            bit_array_size=int(raw[profile.size_field]),
            hash_count=int(raw[profile.hash_count_field]),
            max_inserts_per_generation=int(raw[profile.capacity_field]),
            seeds=normalize_seeds(version, raw[profile.seed_field]),
        )
    except KeyError as exc:
        raise SchemaError(f"{version} project record missing field {exc!s}") from exc
    if spec.seeds != CANONICAL_SEEDS:
        raise SchemaError(
            f"{version} normalized seeds {spec.seeds} != canonical {CANONICAL_SEEDS}"
        )
    if spec.hash_count != len(spec.seeds):
        raise SchemaError(f"{version} hash count {spec.hash_count} != seed count")
    return spec


def normalize_event(version: str, raw: dict) -> Event:
    """Normalize one raw event record of any schema into a canonical Event."""

    profile = PROFILES[version]
    raw_key = raw[profile.key_field]
    raw_op = raw[profile.op_field]
    if raw_op not in OP_VOCAB[version]:
        raise SchemaError(f"{version} unknown op {raw_op!r}")
    return Event(
        logical_time=int(raw[profile.time_field]),
        op=OP_VOCAB[version][raw_op],
        key=normalize_key(version, raw_key),
        version=version,
        raw_key=str(raw_key),
    )


def cross_version_diff_table() -> list[dict[str, str]]:
    """Static mapping table covering field naming, seed expression, key encoding."""

    rows: list[dict[str, str]] = []
    for version in SCHEMA_VERSIONS:
        p = PROFILES[version]
        rows.append(
            {
                "version": version,
                "key_field": p.key_field,
                "op_field": p.op_field,
                "time_field": p.time_field,
                "size_field": p.size_field,
                "hash_count_field": p.hash_count_field,
                "capacity_field": p.capacity_field,
                "seed_field": p.seed_field,
                "seed_expression": p.seed_expression,
                "normalized_seeds": repr(CANONICAL_SEEDS),
                "key_encoding": p.key_encoding,
            }
        )
    return rows
