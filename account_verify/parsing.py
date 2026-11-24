from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .codes import (
    E_DUPLICATE_TOKEN,
    E_DUPLICATE_USER,
    E_INVALID_EFFECTIVE_TIME,
    E_INVALID_GENERATION,
    E_INVALID_ITERATIONS,
    E_INVALID_ONE_TIME,
    E_INVALID_POLICY_GENERATION,
    E_INVALID_POLICY_KEY,
    E_INVALID_SALT,
    E_INVALID_TIME,
    E_INVALID_TIME_RANGE,
    E_INVALID_TOMBSTONE,
    E_INVALID_TYPE,
    E_INVALID_USER_ID,
    E_INVALID_WHITELIST,
    E_JSON_PARSE,
    E_MISSING_FIELD,
    E_SCHEMA,
    E_SHARD_HASH_MISMATCH,
    E_SQLITE_METADATA,
    E_UNKNOWN_ALGORITHM,
)
from .digests import file_sha256
from .models import ALGORITHM_CATALOG, is_strict_int, pick, records_from_payload, rejection


class ParseBatch:
    def __init__(self) -> None:
        self.users: List[Dict[str, Any]] = []
        self.credentials: List[Dict[str, Any]] = []
        self.tokens: List[Dict[str, Any]] = []
        self.policy_v1: List[Dict[str, Any]] = []
        self.policy_v2: List[Dict[str, Any]] = []
        self.rejections: List[Dict[str, Any]] = []
        self.source_hashes: Dict[str, str] = {}

    @property
    def ok(self) -> bool:
        return not self.rejections

    def reject(self, item: Dict[str, Any]) -> None:
        self.rejections.append(item)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "users": self.users,
            "credentials": self.credentials,
            "tokens": self.tokens,
            "policy_v1": self.policy_v1,
            "policy_v2": self.policy_v2,
            "rejections": self.rejections,
            "source_hashes": self.source_hashes,
        }


def _load_json(path: Path, source_category: str, batch: ParseBatch) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        batch.reject(
            rejection(
                source_category,
                -1,
                E_JSON_PARSE,
                "input file was not found",
                path=str(path),
            )
        )
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        batch.reject(
            rejection(
                source_category,
                -1,
                E_JSON_PARSE,
                "input file is not valid UTF-8 JSON",
                path=str(path),
                error=str(exc),
            )
        )
    return None


def _require_nonempty_string(record: Dict[str, Any], field: str) -> Optional[str]:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def parse_users(payload: Any, batch: ParseBatch, source_category: str = "users") -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("users"), list):
        return
    seen = set()
    for index, record in enumerate(payload["users"]):
        if not isinstance(record, dict):
            batch.reject(rejection(source_category, index, E_INVALID_TYPE, "user record must be an object"))
            continue
        user_id = record.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            batch.reject(rejection(source_category, index, E_INVALID_USER_ID, "user_id must be a non-empty string"))
            continue
        if user_id in seen:
            batch.reject(rejection(source_category, index, E_DUPLICATE_USER, "duplicate user_id", user_id=user_id))
            continue
        seen.add(user_id)
        batch.users.append({"user_id": user_id, "display_name": record.get("display_name")})


def parse_credentials(payload: Any, batch: ParseBatch, source_category: str = "credentials") -> None:
    records = records_from_payload(payload)
    if payload is not None and not records and not isinstance(payload, list):
        batch.reject(rejection(source_category, -1, E_SCHEMA, "credential payload has no record list"))
        return

    seen_user_generation = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            batch.reject(rejection(source_category, index, E_INVALID_TYPE, "credential record must be an object"))
            continue

        user_id = pick(record, "user_id", "userId")
        if not isinstance(user_id, str) or not user_id.strip():
            batch.reject(rejection(source_category, index, E_INVALID_USER_ID, "user_id must be a non-empty string"))
            continue

        generation = pick(record, "credential_generation", "generation", default=1)
        if not is_strict_int(generation) or generation < 0:
            batch.reject(rejection(source_category, index, E_INVALID_GENERATION, "credential generation must be a non-negative integer", user_id=user_id))
            continue

        salt = pick(record, "salt_hex", "salt")
        salt_length = pick(record, "salt_length_bytes", "salt_length")
        salt_bytes_len = None
        if not isinstance(salt, str) or not salt:
            batch.reject(rejection(source_category, index, E_INVALID_SALT, "salt must be a non-empty string", user_id=user_id))
            continue
        if "salt_hex" in record or all(c in "0123456789abcdefABCDEF" for c in salt) and len(salt) % 2 == 0:
            try:
                salt_bytes_len = len(bytes.fromhex(salt))
            except ValueError:
                salt_bytes_len = None
        if salt_bytes_len is None:
            salt_bytes_len = len(salt.encode("utf-8"))
        if salt_bytes_len <= 0:
            batch.reject(rejection(source_category, index, E_INVALID_SALT, "salt length must be positive", user_id=user_id))
            continue
        if salt_length is not None:
            if not is_strict_int(salt_length) or salt_length <= 0:
                batch.reject(rejection(source_category, index, E_INVALID_SALT, "salt_length must be a positive integer", user_id=user_id))
                continue
            if salt_length != salt_bytes_len:
                batch.reject(rejection(source_category, index, E_INVALID_SALT, "salt_length does not match salt", user_id=user_id, expected=salt_bytes_len, actual=salt_length))
                continue

        iterations = pick(record, "iterations", "iteration_count", "pbkdf2_iterations")
        if not is_strict_int(iterations) or iterations <= 0:
            batch.reject(rejection(source_category, index, E_INVALID_ITERATIONS, "iterations must be a positive integer", user_id=user_id))
            continue

        algorithm = pick(record, "algorithm", "algorithm_tier", "digest_algorithm")
        if algorithm not in ALGORITHM_CATALOG:
            batch.reject(rejection(source_category, index, E_UNKNOWN_ALGORITHM, "algorithm tier is outside the known whitelist", user_id=user_id, algorithm=algorithm))
            continue

        digest = pick(record, "digest", "digest_value", "summary_placeholder")
        if not isinstance(digest, str) or not digest:
            batch.reject(rejection(source_category, index, E_MISSING_FIELD, "digest placeholder must be a non-empty string", user_id=user_id))
            continue

        effective_at_ms = pick(record, "effective_at_ms", "migrated_at_ms", "created_at_ms")
        if effective_at_ms is not None and (not is_strict_int(effective_at_ms) or effective_at_ms < 0):
            batch.reject(rejection(source_category, index, E_INVALID_TIME, "credential effective_at_ms must be a non-negative integer", user_id=user_id))
            continue

        key = (user_id, generation)
        if key in seen_user_generation:
            batch.reject(
                rejection(
                    source_category,
                    index,
                    E_DUPLICATE_USER,
                    "duplicate user_id for the same credential generation",
                    user_id=user_id,
                    generation=generation,
                )
            )
            continue
        seen_user_generation.add(key)

        record_id = pick(record, "record_id", "credential_id", default=f"{user_id}#gen{generation}")
        batch.credentials.append(
            {
                "record_id": record_id,
                "user_id": user_id,
                "generation": generation,
                "salt": salt,
                "salt_length_bytes": salt_bytes_len,
                "iterations": iterations,
                "algorithm": algorithm,
                "digest": digest,
                "effective_at_ms": effective_at_ms,
                "source_category": source_category,
                "record_index": index,
            }
        )


def parse_tokens(payload: Any, batch: ParseBatch, source_category: str = "tokens") -> None:
    records = records_from_payload(payload)
    if payload is not None and not records and not isinstance(payload, list):
        batch.reject(rejection(source_category, -1, E_SCHEMA, "token payload has no record list"))
        return

    seen_ids = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            batch.reject(rejection(source_category, index, E_INVALID_TYPE, "token record must be an object"))
            continue

        token_id = _require_nonempty_string(record, "token_id") or _require_nonempty_string(record, "id")
        if not token_id:
            batch.reject(rejection(source_category, index, E_MISSING_FIELD, "token_id must be a non-empty string"))
            continue
        if token_id in seen_ids:
            batch.reject(rejection(source_category, index, E_DUPLICATE_TOKEN, "duplicate token_id", token_id=token_id))
            continue

        user_id = _require_nonempty_string(record, "user_id")
        if not user_id:
            batch.reject(rejection(source_category, index, E_INVALID_USER_ID, "user_id must be a non-empty string", token_id=token_id))
            continue

        issued_at_ms = pick(record, "issued_at_ms", "issue_at_ms", "issued_ms")
        expires_at_ms = pick(record, "expires_at_ms", "expire_at_ms", "expires_ms")
        if not is_strict_int(issued_at_ms) or issued_at_ms < 0:
            batch.reject(rejection(source_category, index, E_INVALID_TIME, "issued_at_ms must be a non-negative integer", token_id=token_id))
            continue
        if not is_strict_int(expires_at_ms) or expires_at_ms < 0:
            batch.reject(rejection(source_category, index, E_INVALID_TIME, "expires_at_ms must be a non-negative integer", token_id=token_id))
            continue
        if issued_at_ms >= expires_at_ms:
            batch.reject(rejection(source_category, index, E_INVALID_TIME_RANGE, "issued_at_ms must be earlier than expires_at_ms", token_id=token_id))
            continue

        use_count = pick(record, "use_count", "used_count", default=0)
        if not is_strict_int(use_count) or use_count < 0:
            batch.reject(rejection(source_category, index, E_INVALID_TYPE, "use_count must be a non-negative integer", token_id=token_id))
            continue

        one_time = pick(record, "one_time", "is_one_time", default=True)
        if not isinstance(one_time, bool):
            batch.reject(rejection(source_category, index, E_INVALID_ONE_TIME, "one_time must be a boolean", token_id=token_id))
            continue

        signature = pick(record, "old_digest_signature", "signature", "old_signature")
        if not isinstance(signature, str) or not signature:
            batch.reject(rejection(source_category, index, E_MISSING_FIELD, "old_digest_signature must be a non-empty string", token_id=token_id))
            continue

        signing_algorithm = pick(record, "signing_algorithm", "signature_algorithm", "issued_under_algorithm")
        if signing_algorithm is not None and signing_algorithm not in ALGORITHM_CATALOG:
            batch.reject(rejection(source_category, index, E_UNKNOWN_ALGORITHM, "signing algorithm is outside the known whitelist", token_id=token_id, algorithm=signing_algorithm))
            continue

        seen_ids.add(token_id)
        batch.tokens.append(
            {
                "token_id": token_id,
                "user_id": user_id,
                "issued_at_ms": issued_at_ms,
                "expires_at_ms": expires_at_ms,
                "use_count": use_count,
                "one_time": one_time,
                "signature": signature,
                "signing_algorithm": signing_algorithm,
                "source_category": source_category,
                "record_index": index,
            }
        )


def _parse_policy_record(
    record: Any,
    index: int,
    batch: ParseBatch,
    *,
    source_category: str,
    format_name: str,
) -> Optional[Dict[str, Any]]:
    if not isinstance(record, dict):
        batch.reject(rejection(source_category, index, E_INVALID_TYPE, "policy record must be an object"))
        return None

    policy_key = pick(record, "policy_key", "key")
    if not isinstance(policy_key, str) or not policy_key.strip():
        batch.reject(rejection(source_category, index, E_INVALID_POLICY_KEY, "policy_key must be a non-empty string"))
        return None

    generation = pick(record, "policy_generation", "generation", default=0)
    if not is_strict_int(generation) or generation < 0:
        batch.reject(rejection(source_category, index, E_INVALID_POLICY_GENERATION, "policy_generation must be a non-negative integer", policy_key=policy_key))
        return None

    tombstone = pick(record, "tombstone", "is_tombstone", default=False)
    if not isinstance(tombstone, bool):
        batch.reject(rejection(source_category, index, E_INVALID_TOMBSTONE, "tombstone must be a boolean", policy_key=policy_key))
        return None

    allowed = pick(record, "allowed_algorithms", "algorithm_whitelist", "whitelist")
    min_iterations = pick(record, "min_iterations", "minimum_iterations", "iteration_floor")

    if not tombstone:
        if not isinstance(allowed, list) or not allowed:
            batch.reject(rejection(source_category, index, E_INVALID_WHITELIST, "allowed_algorithms must be a non-empty list", policy_key=policy_key))
            return None
        bad = [item for item in allowed if item not in ALGORITHM_CATALOG]
        if bad:
            batch.reject(rejection(source_category, index, E_INVALID_WHITELIST, "whitelist contains unknown algorithm tiers", policy_key=policy_key, bad=bad))
            return None
        if not is_strict_int(min_iterations) or min_iterations <= 0:
            batch.reject(rejection(source_category, index, E_INVALID_ITERATIONS, "min_iterations must be a positive integer", policy_key=policy_key))
            return None

    effective_relative_seconds = None
    effective_at_ms = None
    if format_name == "v1":
        effective_relative_seconds = pick(record, "effective_relative_seconds", "effective_after_seconds", "relative_effective_seconds")
        if not is_strict_int(effective_relative_seconds) or effective_relative_seconds < 0:
            batch.reject(rejection(source_category, index, E_INVALID_EFFECTIVE_TIME, "v1 effective relative seconds must be a non-negative integer", policy_key=policy_key))
            return None
    else:
        effective_at_ms = pick(record, "absolute_effective_ms", "effective_at_ms", "effective_ms")
        if not is_strict_int(effective_at_ms) or effective_at_ms < 0:
            batch.reject(rejection(source_category, index, E_INVALID_EFFECTIVE_TIME, "v2 absolute effective milliseconds must be a non-negative integer", policy_key=policy_key))
            return None

    return {
        "policy_key": policy_key,
        "generation": generation,
        "tombstone": tombstone,
        "allowed_algorithms": sorted(allowed or []),
        "min_iterations": min_iterations if not tombstone else None,
        "effective_relative_seconds": effective_relative_seconds,
        "effective_at_ms": effective_at_ms,
        "format": format_name,
        "source_category": source_category,
        "record_index": index,
    }


def parse_policy_v1(payload: Any, batch: ParseBatch, source_category: str = "policy_v1") -> None:
    records = records_from_payload(payload)
    default_generation = 1
    if isinstance(payload, dict):
        gen = payload.get("snapshot_generation") or payload.get("policy_generation")
        if is_strict_int(gen) and gen >= 0:
            default_generation = gen
    for index, raw in enumerate(records):
        if isinstance(raw, dict) and "policy_generation" not in raw and "generation" not in raw:
            raw = dict(raw)
            raw["policy_generation"] = default_generation
        parsed = _parse_policy_record(raw, index, batch, source_category=source_category, format_name="v1")
        if parsed:
            batch.policy_v1.append(parsed)


def parse_policy_v2_shard(payload: Any, batch: ParseBatch, source_category: str) -> None:
    records = records_from_payload(payload)
    for index, raw in enumerate(records):
        parsed = _parse_policy_record(raw, index, batch, source_category=source_category, format_name="v2")
        if parsed:
            batch.policy_v2.append(parsed)


def load_policy_v2_sqlite(db_path: Path, batch: ParseBatch) -> None:
    db_path = Path(db_path)
    source_category = "policy_v2_sqlite"
    if not db_path.exists():
        batch.reject(rejection(source_category, -1, E_SQLITE_METADATA, "v2 metadata SQLite database was not found", path=str(db_path)))
        return

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        batch.reject(rejection(source_category, -1, E_SQLITE_METADATA, "cannot open v2 metadata database", error=str(exc)))
        return

    try:
        table = None
        for candidate in ("policy_shards", "shards"):
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (candidate,)).fetchone()
            if row:
                table = candidate
                break
        if table is None:
            batch.reject(rejection(source_category, -1, E_SQLITE_METADATA, "v2 metadata database has no shard table"))
            return
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY shard_id").fetchall()
        for row in rows:
            row_map = dict(row)
            shard_id = row_map.get("shard_id") or row_map.get("id") or table
            relative_path = row_map.get("path") or row_map.get("shard_path")
            expected_hash = row_map.get("sha256") or row_map.get("file_sha256")
            if not relative_path:
                batch.reject(rejection(source_category, -1, E_SQLITE_METADATA, "shard metadata is missing path", shard_id=shard_id))
                continue
            shard_path = Path(relative_path)
            if not shard_path.is_absolute():
                shard_path = db_path.parent / shard_path
            if not shard_path.exists():
                batch.reject(rejection(f"policy_v2_shard:{shard_id}", -1, E_JSON_PARSE, "shard file was not found", path=str(shard_path)))
                continue
            if expected_hash:
                actual_hash = file_sha256(shard_path)
                if actual_hash != expected_hash:
                    batch.reject(rejection(f"policy_v2_shard:{shard_id}", -1, E_SHARD_HASH_MISMATCH, "shard hash does not match SQLite metadata", expected=expected_hash, actual=actual_hash))
                    continue
            payload = _load_json(shard_path, f"policy_v2_shard:{shard_id}", batch)
            if payload is None:
                continue
            parse_policy_v2_shard(payload, batch, f"policy_v2_shard:{shard_id}")
    except sqlite3.Error as exc:
        batch.reject(rejection(source_category, -1, E_SQLITE_METADATA, "failed reading v2 metadata database", error=str(exc)))
    finally:
        conn.close()


def parse_sample_sources(
    *,
    credentials_path: Optional[Path] = None,
    tokens_path: Optional[Path] = None,
    policy_v1_path: Optional[Path] = None,
    policy_v2_db_path: Optional[Path] = None,
) -> ParseBatch:
    batch = ParseBatch()

    if credentials_path:
        credentials_path = Path(credentials_path)
        batch.source_hashes["credentials"] = file_sha256(credentials_path) if credentials_path.exists() else None
        payload = _load_json(credentials_path, "credentials", batch)
        if payload is not None:
            parse_users(payload, batch)
            parse_credentials(payload, batch)

    if tokens_path:
        tokens_path = Path(tokens_path)
        batch.source_hashes["tokens"] = file_sha256(tokens_path) if tokens_path.exists() else None
        payload = _load_json(tokens_path, "tokens", batch)
        if payload is not None:
            parse_tokens(payload, batch)

    if policy_v1_path:
        policy_v1_path = Path(policy_v1_path)
        batch.source_hashes["policy_v1"] = file_sha256(policy_v1_path) if policy_v1_path.exists() else None
        payload = _load_json(policy_v1_path, "policy_v1", batch)
        if payload is not None:
            parse_policy_v1(payload, batch)

    if policy_v2_db_path:
        policy_v2_db_path = Path(policy_v2_db_path)
        if policy_v2_db_path.exists():
            batch.source_hashes["policy_v2_db"] = file_sha256(policy_v2_db_path)
        load_policy_v2_sqlite(policy_v2_db_path, batch)

    return batch
