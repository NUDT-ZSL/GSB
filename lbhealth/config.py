"""Strict local JSON configuration loading for the health checker."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PoolValidationError

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PROTOCOLS = {"http", "tcp"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class MemberConfig:
    id: str
    address: str
    port: int
    protocol: str
    weight: int
    probe_path: str = "/health"

    def identity(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "address": self.address,
            "port": self.port,
            "protocol": self.protocol,
            "weight": self.weight,
            "probe_path": self.probe_path,
        }

    def endpoint(self) -> str:
        if ":" in self.address:
            return f"[{self.address}]:{self.port}"
        return f"{self.address}:{self.port}"


@dataclass(frozen=True)
class PoolConfig:
    probe_protocol: str
    interval_ms: int
    timeout_ms: int
    success_threshold: int
    failure_threshold: int
    slow_start_weight_table: tuple[int, ...]
    members: tuple[MemberConfig, ...]

    @property
    def full_weight_sum(self) -> int:
        return sum(member.weight for member in self.members)


@dataclass(frozen=True)
class Config:
    schema_version: int
    config_version: str
    pool: PoolConfig
    raw: dict[str, Any]

    @property
    def members(self) -> tuple[MemberConfig, ...]:
        return self.pool.members

    @property
    def member_by_id(self) -> dict[str, MemberConfig]:
        return {member.id: member for member in self.pool.members}

    @property
    def member_digest(self) -> str:
        payload = [member.identity() for member in sorted(self.pool.members, key=lambda item: item.id)]
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    @property
    def config_digest(self) -> str:
        stable = {
            "schema_version": self.schema_version,
            "config_version": self.config_version,
            "pool": {
                "probe_protocol": self.pool.probe_protocol,
                "interval_ms": self.pool.interval_ms,
                "timeout_ms": self.pool.timeout_ms,
                "success_threshold": self.pool.success_threshold,
                "failure_threshold": self.pool.failure_threshold,
                "slow_start_weight_table": list(self.pool.slow_start_weight_table),
                "members": [member.identity() for member in self.pool.members],
            },
        }
        return hashlib.sha256(_canonical_json(stable).encode("utf-8")).hexdigest()

    def canonical_pool(self) -> dict[str, Any]:
        return {
            "probe_protocol": self.pool.probe_protocol,
            "interval_ms": self.pool.interval_ms,
            "timeout_ms": self.pool.timeout_ms,
            "success_threshold": self.pool.success_threshold,
            "failure_threshold": self.pool.failure_threshold,
            "slow_start_weight_table": list(self.pool.slow_start_weight_table),
            "members": [member.identity() for member in self.pool.members],
        }


class _Validator:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self.errors.append({"code": "E_POOL", "path": path, "message": message})

    def require_object(self, value: Any, path: str) -> bool:
        if not isinstance(value, dict):
            self.add(path, "must be a JSON object")
            return False
        return True

    def require_nonempty_string(self, value: Any, path: str) -> bool:
        if not isinstance(value, str) or not value.strip():
            self.add(path, "must be a non-empty string")
            return False
        return True

    def require_positive_int(self, value: Any, path: str) -> bool:
        if not _is_int(value) or value <= 0:
            self.add(path, "must be a positive integer")
            return False
        return True


def _parse_member(
    raw: Any,
    index: int,
    default_protocol: str | None,
    validator: _Validator,
) -> MemberConfig | None:
    base = f"$.pool.members[{index}]"
    if not validator.require_object(raw, base):
        return None

    before = len(validator.errors)
    member_id = raw.get("id")
    if not validator.require_nonempty_string(member_id, f"{base}.id"):
        member_id = ""
    elif not _ID_RE.fullmatch(member_id):
        validator.add(f"{base}.id", "must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

    address = raw.get("address")
    if not validator.require_nonempty_string(address, f"{base}.address"):
        address = ""
    else:
        try:
            parsed_address = ipaddress.ip_address(address)
            if not parsed_address.is_loopback:
                validator.add(f"{base}.address", "must be a loopback IP literal (127.0.0.0/8 or ::1)")
        except ValueError:
            validator.add(f"{base}.address", "must be an IP literal, not a hostname")

    port = raw.get("port")
    if not _is_int(port) or not 1 <= port <= 65535:
        validator.add(f"{base}.port", "must be an integer from 1 to 65535")

    protocol = raw.get("protocol", default_protocol)
    if protocol not in _PROTOCOLS:
        validator.add(f"{base}.protocol", "must be one of: http, tcp")

    weight = raw.get("weight")
    if not _is_int(weight) or weight < 0:
        validator.add(f"{base}.weight", "must be a non-negative integer")

    probe_path = raw.get("probe_path", "/health")
    if (
        not isinstance(probe_path, str)
        or not probe_path.startswith("/")
        or any(character in probe_path for character in (" ", "\r", "\n", "\t"))
    ):
        validator.add(
            f"{base}.probe_path",
            "must be an HTTP path beginning with '/' without whitespace or control characters",
        )

    if len(validator.errors) != before:
        return None
    return MemberConfig(
        id=str(member_id),
        address=str(address),
        port=int(port),
        protocol=str(protocol),
        weight=int(weight),
        probe_path=str(probe_path),
    )


def parse_config(raw: Any) -> Config:
    """Validate a decoded JSON object; all configuration failures are E_POOL."""

    validator = _Validator()
    if not validator.require_object(raw, "$"):
        raise PoolValidationError(validator.errors)

    schema_version = raw.get("schema_version", 1)
    if schema_version != 1 or not _is_int(schema_version):
        validator.add("$.schema_version", "must be integer 1")

    config_version = raw.get("config_version")
    if not validator.require_nonempty_string(config_version, "$.config_version"):
        config_version = ""

    pool_raw = raw.get("pool")
    if not validator.require_object(pool_raw, "$.pool"):
        raise PoolValidationError(validator.errors)

    pool_protocol = pool_raw.get("probe_protocol")
    if pool_protocol not in _PROTOCOLS:
        validator.add("$.pool.probe_protocol", "must be one of: http, tcp")

    interval_ms = pool_raw.get("interval_ms")
    timeout_ms = pool_raw.get("timeout_ms")
    interval_ok = validator.require_positive_int(interval_ms, "$.pool.interval_ms")
    timeout_ok = validator.require_positive_int(timeout_ms, "$.pool.timeout_ms")
    if interval_ok and timeout_ok and timeout_ms >= interval_ms:
        validator.add("$.pool.timeout_ms", "must be less than $.pool.interval_ms")

    success_threshold = pool_raw.get("success_threshold")
    failure_threshold = pool_raw.get("failure_threshold")
    validator.require_positive_int(success_threshold, "$.pool.success_threshold")
    validator.require_positive_int(failure_threshold, "$.pool.failure_threshold")

    table = pool_raw.get("slow_start_weight_table")
    if not isinstance(table, list) or not table:
        validator.add("$.pool.slow_start_weight_table", "must be a non-empty integer array")
    else:
        for index, value in enumerate(table):
            if not _is_int(value) or not 0 <= value <= 100:
                validator.add(
                    f"$.pool.slow_start_weight_table[{index}]",
                    "must be an integer from 0 to 100",
                )
        valid_table = all(_is_int(value) and 0 <= value <= 100 for value in table)
        if valid_table:
            if any(left >= right for left, right in zip(table, table[1:])):
                validator.add(
                    "$.pool.slow_start_weight_table",
                    "must be strictly increasing (linear slow-start stages)",
                )
            if table[-1] != 100:
                validator.add(
                    "$.pool.slow_start_weight_table",
                    "must end at 100 so recovery reaches the full weight",
                )

    members_raw = pool_raw.get("members")
    indexed_members: list[tuple[int, MemberConfig]] = []
    if not isinstance(members_raw, list) or not members_raw:
        validator.add("$.pool.members", "must be a non-empty array")
    else:
        for index, member_raw in enumerate(members_raw):
            member = _parse_member(member_raw, index, pool_protocol, validator)
            if member is not None:
                indexed_members.append((index, member))
        seen_ids: set[str] = set()
        for index, member_raw in enumerate(members_raw):
            if not isinstance(member_raw, dict):
                continue
            raw_id = member_raw.get("id")
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            if raw_id in seen_ids:
                validator.add(f"$.pool.members[{index}].id", f"duplicate member id: {raw_id}")
            seen_ids.add(raw_id)
        if indexed_members and sum(member.weight for _index, member in indexed_members) <= 0:
            validator.add("$.pool.members", "member weights must not all be zero")

    if validator.errors:
        raise PoolValidationError(validator.errors)

    pool = PoolConfig(
        probe_protocol=str(pool_protocol),
        interval_ms=int(interval_ms),
        timeout_ms=int(timeout_ms),
        success_threshold=int(success_threshold),
        failure_threshold=int(failure_threshold),
        slow_start_weight_table=tuple(int(value) for value in table),
        members=tuple(member for _index, member in indexed_members),
    )
    return Config(
        schema_version=int(schema_version),
        config_version=str(config_version),
        pool=pool,
        raw=raw,
    )


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PoolValidationError(
            [{"code": "E_POOL", "path": "$", "message": f"cannot read config: {exc}"}]
        ) from exc
    except json.JSONDecodeError as exc:
        raise PoolValidationError(
            [
                {
                    "code": "E_POOL",
                    "path": "$",
                    "message": f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                }
            ]
        ) from exc
    return parse_config(data)
