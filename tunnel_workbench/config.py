from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import BusinessError, fail
from .framing import HEADER_SIZE
from .util import canonical_json, load_json_strict, require_int, require_number, sha256_hex

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class TunnelSpec:
    name: str
    listen_host: str
    listen_port: int
    transport: str
    outer_mtu: int
    encapsulation_overhead: int
    link_weight: float
    payload_lengths: tuple[int, ...]

    @property
    def listen_address(self) -> str:
        host = f"[{self.listen_host}]" if ":" in self.listen_host else self.listen_host
        return f"{host}:{self.listen_port}"

    @property
    def payload_capacity(self) -> int:
        return self.outer_mtu - HEADER_SIZE - self.encapsulation_overhead


@dataclass(frozen=True)
class LinkProfile:
    tunnel: str
    version: int
    rtt_ms: float
    loss_rate: float
    bandwidth_kbps: float
    jitter_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "tunnel": self.tunnel,
            "version": self.version,
            "rtt_ms": self.rtt_ms,
            "loss_rate": self.loss_rate,
            "bandwidth_kbps": self.bandwidth_kbps,
            "jitter_ms": self.jitter_ms,
        }

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_json(self.as_dict()))


@dataclass(frozen=True)
class ForwardPolicy:
    fragment_threshold: int
    per_round_concurrency: int
    logical_budget_ms: int
    retransmission_budget: int
    tolerance: float
    clock_step_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "fragment_threshold": self.fragment_threshold,
            "per_round_concurrency": self.per_round_concurrency,
            "logical_budget_ms": self.logical_budget_ms,
            "retransmission_budget": self.retransmission_budget,
            "tolerance": self.tolerance,
            "clock_step_ms": self.clock_step_ms,
        }


@dataclass(frozen=True)
class WorkbenchConfig:
    tunnels: tuple[TunnelSpec, ...]
    profiles: tuple[LinkProfile, ...]
    policy: ForwardPolicy
    source: str = "<memory>"

    @property
    def tunnel_map(self) -> dict[str, TunnelSpec]:
        return {tunnel.name: tunnel for tunnel in self.tunnels}

    @property
    def current_profiles(self) -> dict[str, LinkProfile]:
        result: dict[str, LinkProfile] = {}
        for profile in self.profiles:
            current = result.get(profile.tunnel)
            if current is None or profile.version > current.version:
                result[profile.tunnel] = profile
        return result

    @property
    def content_digest(self) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "tunnels": [
                        {
                            "name": tunnel.name,
                            "listen_host": tunnel.listen_host,
                            "listen_port": tunnel.listen_port,
                            "transport": tunnel.transport,
                            "outer_mtu": tunnel.outer_mtu,
                            "encapsulation_overhead": tunnel.encapsulation_overhead,
                            "link_weight": tunnel.link_weight,
                            "payload_lengths": list(tunnel.payload_lengths),
                        }
                        for tunnel in self.tunnels
                    ],
                    "profiles": [profile.as_dict() for profile in self.profiles],
                    "policy": self.policy.as_dict(),
                }
            )
        )


def _get(mapping: Mapping[str, Any], field: str, code: str) -> Any:
    if field not in mapping:
        fail(code, f"missing required field {field}", details={"field": field})
    return mapping[field]


def _parse_listen(mapping: Mapping[str, Any]) -> tuple[str, int]:
    address_value = mapping.get(
        "listen",
        mapping.get("listen_address", mapping.get("loopback_listen")),
    )
    if address_value is not None:
        address = address_value
        if not isinstance(address, str) or ":" not in address:
            fail(
                "E_LISTEN_ADDR_INVALID",
                "listen address must be HOST:PORT",
                details={"listen": address},
            )
        host, port_text = address.rsplit(":", 1)
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        try:
            port = int(port_text, 10)
        except ValueError:
            fail(
                "E_LISTEN_ADDR_INVALID",
                "listen port must be an integer",
                details={"listen": address},
            )
    else:
        host = _get(mapping, "listen_host", "E_LISTEN_ADDR_INVALID")
        port = _get(mapping, "listen_port", "E_LISTEN_ADDR_INVALID")

    if not isinstance(host, str) or not host:
        fail("E_LISTEN_ADDR_INVALID", "listen host must be a non-empty string")
    if host == "localhost":
        host = "127.0.0.1"
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        fail(
            "E_LISTEN_ADDR_INVALID",
            "listen host must be a numeric IP address",
            details={"host": host},
        )
    if not parsed.is_loopback:
        fail(
            "E_LISTEN_NOT_LOOPBACK",
            "listen host must be a loopback address",
            details={"host": host},
        )
    if type(port) is not int or not (1 <= port <= 65535):
        fail(
            "E_LISTEN_ADDR_INVALID",
            "listen port must be in 1..65535",
            details={"port": port},
        )
    return str(parsed), port


def _parse_tunnel(raw: Any, threshold: int) -> TunnelSpec:
    if not isinstance(raw, Mapping):
        fail("E_CONFIG_INVALID", "each tunnel entry must be an object")
    name = _get(raw, "name", "E_NAME_INVALID")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        fail(
            "E_NAME_INVALID",
            "tunnel name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}",
            details={"name": name},
        )
    host, port = _parse_listen(raw)
    transport = raw.get("transport", "udp")
    if transport not in ("udp", "tcp"):
        fail(
            "E_TRANSPORT_INVALID",
            "transport must be udp or tcp",
            details={"transport": transport},
        )
    outer_mtu = require_int(
        _get(raw, "outer_mtu", "E_MTU_INVALID"), "E_MTU_INVALID", "outer_mtu", minimum=1
    )
    overhead = require_int(
        raw.get("encapsulation_overhead", raw.get("encryption_overhead", 0)),
        "E_OVERHEAD_INVALID",
        "encapsulation_overhead",
        minimum=0,
    )
    if outer_mtu <= HEADER_SIZE + overhead:
        fail(
            "E_MTU_INVALID",
            "outer MTU must exceed fragment header plus encapsulation overhead",
            details={
                "outer_mtu": outer_mtu,
                "header_size": HEADER_SIZE,
                "encapsulation_overhead": overhead,
            },
        )
    capacity = outer_mtu - HEADER_SIZE - overhead
    lengths = raw.get(
        "payload_lengths",
        raw.get(
            "inner_payload_length_distribution",
            raw.get(
                "inner_payload_lengths",
                raw.get("payload_length_distribution"),
            ),
        ),
    )
    if not isinstance(lengths, list) or not lengths:
        fail(
            "E_PAYLOAD_LENGTH_INVALID",
            "payload length distribution must be a non-empty array",
        )
    parsed_lengths: list[int] = []
    for index, length in enumerate(lengths):
        value = require_int(
            length,
            "E_PAYLOAD_LENGTH_INVALID",
            f"payload_lengths[{index}]",
            minimum=1,
        )
        if value > capacity:
            fail(
                "E_PAYLOAD_TOO_LARGE",
                "inner payload exceeds the outer MTU carryable capacity",
                details={
                    "tunnel": name,
                    "payload_length": value,
                    "capacity": capacity,
                    "outer_mtu": outer_mtu,
                    "header_size": HEADER_SIZE,
                    "encapsulation_overhead": overhead,
                },
            )
        parsed_lengths.append(value)
    weight = require_number(
        raw.get("link_weight", 1.0), "E_LINK_WEIGHT_INVALID", "link_weight", minimum=0.0
    )
    if weight == 0.0:
        fail(
            "E_LINK_WEIGHT_INVALID",
            "link_weight must be positive",
            details={"tunnel": name},
        )
    if threshold <= 0:
        fail("E_FRAGMENT_THRESHOLD_INVALID", "fragment threshold must be positive")
    return TunnelSpec(
        name=name,
        listen_host=host,
        listen_port=port,
        transport=transport,
        outer_mtu=outer_mtu,
        encapsulation_overhead=overhead,
        link_weight=weight,
        payload_lengths=tuple(parsed_lengths),
    )


def parse_profile(raw: Mapping[str, Any], tunnel_names: set[str] | None = None) -> LinkProfile:
    if not isinstance(raw, Mapping):
        fail("E_CONFIG_INVALID", "each link profile must be an object")
    tunnel = _get(raw, "tunnel", "E_PROFILE_INVALID")
    if not isinstance(tunnel, str) or not tunnel:
        fail("E_PROFILE_INVALID", "profile tunnel must be a non-empty string")
    if tunnel_names is not None and tunnel not in tunnel_names:
        fail(
            "E_PROFILE_TUNNEL_UNKNOWN",
            "profile references an unknown tunnel",
            details={"tunnel": tunnel},
        )
    version = require_int(
        _get(raw, "version", "E_PROFILE_VERSION_INVALID"),
        "E_PROFILE_VERSION_INVALID",
        "profile version",
        minimum=1,
    )
    rtt = require_number(
        _get(raw, "rtt_ms", "E_RTT_INVALID"), "E_RTT_INVALID", "rtt_ms", minimum=0.001
    )
    loss = require_number(
        _get(raw, "loss_rate", "E_LOSS_RATE_INVALID"),
        "E_LOSS_RATE_INVALID",
        "loss_rate",
        minimum=0.0,
        maximum=1.0,
    )
    bandwidth = require_number(
        _get(raw, "bandwidth_kbps", "E_BANDWIDTH_INVALID"),
        "E_BANDWIDTH_INVALID",
        "bandwidth_kbps",
        minimum=0.001,
    )
    jitter = require_number(
        _get(raw, "jitter_ms", "E_JITTER_INVALID"),
        "E_JITTER_INVALID",
        "jitter_ms",
        minimum=0.0,
        maximum=rtt,
    )
    return LinkProfile(
        tunnel=tunnel,
        version=version,
        rtt_ms=rtt,
        loss_rate=loss,
        bandwidth_kbps=bandwidth,
        jitter_ms=jitter,
    )


def parse_policy(raw: Mapping[str, Any]) -> ForwardPolicy:
    if not isinstance(raw, Mapping):
        fail("E_CONFIG_INVALID", "policy must be an object")
    policy = ForwardPolicy(
        fragment_threshold=require_int(
            _get(raw, "fragment_threshold", "E_FRAGMENT_THRESHOLD_INVALID"),
            "E_FRAGMENT_THRESHOLD_INVALID",
            "fragment_threshold",
            minimum=1,
        ),
        per_round_concurrency=require_int(
            _get(raw, "per_round_concurrency", "E_CONCURRENCY_INVALID"),
            "E_CONCURRENCY_INVALID",
            "per_round_concurrency",
            minimum=1,
        ),
        logical_budget_ms=require_int(
            _get(raw, "logical_budget_ms", "E_BUDGET_INVALID"),
            "E_BUDGET_INVALID",
            "logical_budget_ms",
            minimum=1,
        ),
        retransmission_budget=require_int(
            _get(raw, "retransmission_budget", "E_RETRANSMIT_BUDGET_INVALID"),
            "E_RETRANSMIT_BUDGET_INVALID",
            "retransmission_budget",
            minimum=0,
        ),
        tolerance=require_number(
            raw.get("tolerance", 0),
            "E_TOLERANCE_INVALID",
            "tolerance",
            minimum=0.0,
        ),
        clock_step_ms=require_int(
            _get(raw, "clock_step_ms", "E_CLOCK_STEP_INVALID"),
            "E_CLOCK_STEP_INVALID",
            "clock_step_ms",
            minimum=1,
        ),
    )
    minimum_budget = policy.clock_step_ms + 2
    if policy.logical_budget_ms < minimum_budget:
        fail(
            "E_BUDGET_INVALID",
            "logical budget is too small for one logical clock step",
            details={
                "logical_budget_ms": policy.logical_budget_ms,
                "minimum": minimum_budget,
            },
        )
    return policy


def parse_config(raw: Any, source: str = "<memory>") -> WorkbenchConfig:
    if not isinstance(raw, Mapping):
        fail("E_CONFIG_INVALID", "configuration root must be an object")
    policy = parse_policy(raw.get("policy", raw.get("forwarding_policy", {})))
    raw_tunnels = raw.get("tunnels", raw.get("peers"))
    if not isinstance(raw_tunnels, list) or not raw_tunnels:
        fail("E_CONFIG_INVALID", "tunnels must be a non-empty array")
    tunnels: list[TunnelSpec] = []
    seen_names: set[str] = set()
    for raw_tunnel in raw_tunnels:
        tunnel = _parse_tunnel(raw_tunnel, policy.fragment_threshold)
        if tunnel.name in seen_names:
            fail(
                "E_NAME_DUPLICATE",
                "tunnel names must be unique",
                details={"name": tunnel.name},
            )
        seen_names.add(tunnel.name)
        tunnels.append(tunnel)

    raw_profiles = raw.get("profiles", raw.get("link_profiles"))
    if not isinstance(raw_profiles, list) or not raw_profiles:
        fail("E_CONFIG_INVALID", "profiles must be a non-empty array")
    profiles: list[LinkProfile] = []
    profile_keys: dict[tuple[str, int], LinkProfile] = {}
    for raw_profile in raw_profiles:
        profile = parse_profile(raw_profile, seen_names)
        key = (profile.tunnel, profile.version)
        if key in profile_keys:
            if profile_keys[key].digest != profile.digest:
                fail(
                    "E_PROFILE_VERSION_CONFLICT",
                    "the same tunnel/profile version has conflicting content",
                    details={"tunnel": profile.tunnel, "version": profile.version},
                )
            continue
        profile_keys[key] = profile
        profiles.append(profile)
    profile_tunnels = {profile.tunnel for profile in profiles}
    missing_profiles = sorted(seen_names - profile_tunnels)
    if missing_profiles:
        fail(
            "E_PROFILE_MISSING",
            "every tunnel must have at least one link profile",
            details={"tunnels": missing_profiles},
        )
    return WorkbenchConfig(
        tunnels=tuple(tunnels),
        profiles=tuple(sorted(profiles, key=lambda value: (value.tunnel, value.version))),
        policy=policy,
        source=source,
    )


def load_config(path: str | Path) -> WorkbenchConfig:
    raw = load_json_strict(path)
    try:
        return parse_config(raw, source=str(path))
    except BusinessError:
        raise


def parse_profile_update(raw: Any, tunnel: str) -> LinkProfile:
    if not isinstance(raw, Mapping):
        fail("E_PROFILE_INVALID", "profile update must be an object")
    value = dict(raw)
    value["tunnel"] = tunnel
    return parse_profile(value, {tunnel})

