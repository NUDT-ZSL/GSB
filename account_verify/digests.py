from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_digest(value: Any) -> str:
    return sha256_hex(canonical_json(value).encode("utf-8"))


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def derive_pbkdf2_digest(
    *,
    algorithm: str,
    password: str,
    salt_hex: str,
    iterations: int,
) -> str:
    hash_names = {
        "legacy-pbkdf2-sha1": "sha1",
        "pbkdf2-sha256": "sha256",
    }
    hash_name = hash_names[algorithm]
    derived = hashlib.pbkdf2_hmac(
        hash_name,
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        int(iterations),
    )
    return f"{algorithm}${hash_name}${iterations}${salt_hex}${derived.hex()}"
