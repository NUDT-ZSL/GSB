from __future__ import annotations

from typing import Any

from .canonical import key_token, stable_mod


def partition_for_key(key: Any, partitions: int) -> int:
    return stable_mod(key_token(key), partitions)

