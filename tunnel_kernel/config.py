"""Strict parsing and validation for peers, links and forwarding policy."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .codec import HEADER_SIZE
from .errors import (
    E_BAD_CONFIG,
    E_BAD_ENDPOINT,
    E_BAD_LINK,
    E_BAD_MTU,
    E_BAD_PAYLOAD,
    E_BAD_POLICY,
    E_BAD_TIMELINE,
    E_DUPLICATE_PEER,
    E_NO_PROFILE,
    BusinessError,
    fail,
)


MAX_UDP_PAYLOAD = 65_507


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_mapping(value: Any, *, field: str, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(code, f"{field} must be an object", details={"field": field})
    return value


def _require_list(value: Any, *, field: str, code: str) -> list[Any]:
    if not isinstance(value, list):
        fail(code, f"{field} must be an array", details={"field": field})
    return value


def _require_int(
    value: Any,
    *,
    field: str,
    code: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if not _is_int(value):
        fail(code, f"{field} must be an integer", details={"field": field})
    if minimum is not None and value < minimum:
        fail(
            code,
            f"{field} must be >= {minimum}",
            details={"field": field, "minimum": minimum, "actual": value},
        )
    if maximum is not None and value > maximum:
        fail(
            code,
            f"{field} must be <= {maximum}",
            details={"field": field, "maximum": maximum, "actual": value},
        )
    return value


def _require_number(
    value: Any,
    *,
    field: str,
    code: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(code, f"{field} must be numeric", details={"field": field})
    number = float(value)
    if not math.isfinite(number):
        fail(code, f"{field} must be finite", details={"field": field})
    if minimum is not None and number < minimum:
        fail(
            code,
            f"{field} must be >= {minimum}",
            details={"field": field, "minimum": minimum, "actual": number},
        )
    if maximum is not None and number > maximum:
        fail(
            code,
            f"{field} must be <= {maximum}",
            details={"field": field, "maximum": maximum, "actual": number},
        )
    return number


def _require_str(value: Any, *, field: str, code: str) -> str:
    if not isinstance(value, str) or not value:
        fail(code, f"{field} must be a non-empty string", details={"field": field})
    return value


def _validate_loopback(host: str, *, field: str, port: int) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        fail(
            E_BAD_ENDPOINT,
            f"{field} must be a literal loopback IP address",
            details={"field": field, "host": host},
        )
    if not address.is_loopback:
        fail(
            E_BAD_ENDPOINT,
            f"{field} must be a loopback address",
            details={"field": field, "host": host},
        )
    if not 1 <= port <= 65_535:
        fail(
            E_BAD_ENDPOINT,
            f"{field}.port must be in [1, 65535]",
            details={"field": f"{field}.port", "port": port},
        )


@dataclass(frozen=True, slots=True)
class Peer:
    name: str
    protocol: str
    host: str
    port: int
    outer_mtu: int
    payload_lengths: tuple[int, ...]
    link_weight: float
    encrypted: bool

    def max_inner_payload(self, crypto_overhead_bytes: int) -> int:
        return self.outer_mtu - HEADER_SIZE - self.crypto_overhead(crypto_overhead_bytes)

    def crypto_overhead(self, crypto_overhead_bytes: int) -> int:
        return crypto_overhead_bytes if self.encrypted else 0

    def fragment_capacity(self, *, fragment_threshold_bytes: int, crypto_overhead_bytes: int) -> int:
        packet_budget = min(self.outer_mtu, fragment_threshold_bytes)
        return packet_budget - HEADER_SIZE - self.crypto_overhead(crypto_overhead_bytes)


@dataclass(frozen=True, slots=True)
class LinkProfile:
    rtt_ms: float
    loss_rate: float
    bandwidth_mbps: float
    jitter_ms: float

    def as_dict(self) -> dict[str, float]:
        return {
            "bandwidth_mbps": self.bandwidth_mbps,
            "jitter_ms": self.jitter_ms,
            "loss_rate": self.loss_rate,
            "rtt_ms": self.rtt_ms,
        }


@dataclass(frozen=True, slots=True)
class LinkChange:
    at_ms: int
    peer: str
    rtt_ms: float | None = None
    loss_rate: float | None = None
    bandwidth_mbps: float | None = None
    jitter_ms: float | None = None
    mtu: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"at_ms": self.at_ms, "peer": self.peer}
        for key in ("rtt_ms", "loss_rate", "bandwidth_mbps", "jitter_ms", "mtu"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class Tolerance:
    loss_rate_trigger: float
    rtt_ms_trigger: float
    loss_rate_delta: float
    rtt_ms_delta: float
    mtu_down_step: int
    min_mtu: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "loss_rate_delta": self.loss_rate_delta,
            "loss_rate_trigger": self.loss_rate_trigger,
            "min_mtu": self.min_mtu,
            "mtu_down_step": self.mtu_down_step,
            "rtt_ms_delta": self.rtt_ms_delta,
            "rtt_ms_trigger": self.rtt_ms_trigger,
        }


@dataclass(frozen=True, slots=True)
class Policy:
    fragment_threshold_bytes: int
    max_concurrency: int
    round_budget_ms: int
    retransmit_budget: int
    logical_clock_step_ms: int
    clock_start_ms: int
    io_timeout_ms: int
    retransmit_backoff_ms: int
    max_backoff_ms: int
    crypto_overhead_bytes: int
    sample_points: tuple[int, ...]
    tolerance: Tolerance

    def as_dict(self) -> dict[str, Any]:
        return {
            "clock_start_ms": self.clock_start_ms,
            "crypto_overhead_bytes": self.crypto_overhead_bytes,
            "fragment_threshold_bytes": self.fragment_threshold_bytes,
            "io_timeout_ms": self.io_timeout_ms,
            "logical_clock_step_ms": self.logical_clock_step_ms,
            "max_backoff_ms": self.max_backoff_ms,
            "max_concurrency": self.max_concurrency,
            "retransmit_backoff_ms": self.retransmit_backoff_ms,
            "retransmit_budget": self.retransmit_budget,
            "round_budget_ms": self.round_budget_ms,
            "sample_points": list(self.sample_points),
            "tolerance": self.tolerance.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    changes: tuple[LinkChange, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"changes": [change.as_dict() for change in self.changes]}


@dataclass(frozen=True, slots=True)
class ConfigBundle:
    peers: Mapping[str, Peer]
    links: Mapping[str, LinkProfile]
    scenarios: Mapping[str, Scenario]
    policy: Policy

    def as_dict(self) -> dict[str, Any]:
        return {
            "links": {
                name: profile.as_dict()
                for name, profile in sorted(self.links.items())
            },
            "peers": {
                name: {
                    "encrypted": peer.encrypted,
                    "endpoint": {
                        "host": peer.host,
                        "port": peer.port,
                        "protocol": peer.protocol,
                    },
                    "link_weight": peer.link_weight,
                    "name": peer.name,
                    "outer_mtu": peer.outer_mtu,
                    "payload_lengths": list(peer.payload_lengths),
                }
                for name, peer in sorted(self.peers.items())
            },
            "policy": self.policy.as_dict(),
            "scenarios": {
                name: scenario.as_dict()
                for name, scenario in sorted(self.scenarios.items())
            },
        }


def _parse_peer(
    raw: Any,
    *,
    index: int,
    policy: Policy,
) -> Peer:
    field = f"peers[{index}]"
    data = _require_mapping(raw, field=field, code=E_BAD_CONFIG)

    name = _require_str(data.get("name"), field=f"{field}.name", code=E_BAD_CONFIG)
    protocol = _require_str(
        data.get("protocol"), field=f"{field}.protocol", code=E_BAD_CONFIG
    ).lower()
    if protocol not in {"udp", "tcp"}:
        fail(
            E_BAD_ENDPOINT,
            f"{field}.protocol must be udp or tcp",
            details={"field": f"{field}.protocol", "actual": protocol},
        )

    listen = _require_mapping(
        data.get("listen"), field=f"{field}.listen", code=E_BAD_ENDPOINT
    )
    host = _require_str(
        listen.get("host"), field=f"{field}.listen.host", code=E_BAD_ENDPOINT
    )
    port = _require_int(
        listen.get("port"),
        field=f"{field}.listen.port",
        code=E_BAD_ENDPOINT,
        minimum=1,
        maximum=65_535,
    )
    _validate_loopback(host, field=f"{field}.listen", port=port)

    outer_mtu = _require_int(
        data.get("outer_mtu"),
        field=f"{field}.outer_mtu",
        code=E_BAD_MTU,
        minimum=1,
        maximum=MAX_UDP_PAYLOAD,
    )
    encrypted = data.get("encrypted", False)
    if not isinstance(encrypted, bool):
        fail(
            E_BAD_CONFIG,
            f"{field}.encrypted must be boolean",
            details={"field": f"{field}.encrypted"},
        )
    overhead = policy.crypto_overhead_bytes if encrypted else 0
    minimum_feasible_mtu = HEADER_SIZE + overhead + 1
    if outer_mtu < minimum_feasible_mtu:
        fail(
            E_BAD_MTU,
            f"{field}.outer_mtu is too small for headers and crypto overhead",
            details={
                "actual": outer_mtu,
                "minimum": minimum_feasible_mtu,
                "field": f"{field}.outer_mtu",
            },
        )

    payload_raw = _require_list(
        data.get("payload_lengths"),
        field=f"{field}.payload_lengths",
        code=E_BAD_PAYLOAD,
    )
    if not payload_raw:
        fail(
            E_BAD_PAYLOAD,
            f"{field}.payload_lengths must not be empty",
            details={"field": f"{field}.payload_lengths"},
        )
    payload_lengths: list[int] = []
    max_inner = outer_mtu - HEADER_SIZE - overhead
    for px, value in enumerate(payload_raw):
        length = _require_int(
            value,
            field=f"{field}.payload_lengths[{px}]",
            code=E_BAD_PAYLOAD,
            minimum=1,
        )
        if length > max_inner:
            fail(
                E_BAD_PAYLOAD,
                "inner payload exceeds the outer MTU encapsulation limit",
                details={
                    "actual": length,
                    "field": f"{field}.payload_lengths[{px}]",
                    "maximum": max_inner,
                },
            )
        payload_lengths.append(length)

    link_weight = _require_number(
        data.get("link_weight", 1.0),
        field=f"{field}.link_weight",
        code=E_BAD_CONFIG,
        minimum=0.0,
    )
    if link_weight <= 0:
        fail(
            E_BAD_CONFIG,
            f"{field}.link_weight must be > 0",
            details={"field": f"{field}.link_weight"},
        )

    if policy.fragment_threshold_bytes > max_inner:
        fail(
            E_BAD_POLICY,
            "fragment threshold exceeds a peer's encapsulation limit",
            details={
                "field": "policy.fragment_threshold_bytes",
                "maximum": max_inner,
                "peer": name,
            },
        )
    fragment_capacity = (
        min(outer_mtu, policy.fragment_threshold_bytes) - HEADER_SIZE - overhead
    )
    if fragment_capacity <= 0:
        fail(
            E_BAD_POLICY,
            "fragment threshold leaves no payload capacity for a peer",
            details={
                "field": "policy.fragment_threshold_bytes",
                "fragment_capacity": fragment_capacity,
                "peer": name,
            },
        )

    return Peer(
        name=name,
        protocol=protocol,
        host=host,
        port=port,
        outer_mtu=outer_mtu,
        payload_lengths=tuple(payload_lengths),
        link_weight=link_weight,
        encrypted=encrypted,
    )


def _parse_link(raw: Any, *, name: str) -> LinkProfile:
    field = f"links.{name}"
    data = _require_mapping(raw, field=field, code=E_BAD_LINK)
    bandwidth_mbps = _require_number(
        data.get("bandwidth_mbps"),
        field=f"{field}.bandwidth_mbps",
        code=E_BAD_LINK,
        minimum=0.0,
    )
    if bandwidth_mbps <= 0:
        fail(
            E_BAD_LINK,
            f"{field}.bandwidth_mbps must be > 0",
            details={"field": f"{field}.bandwidth_mbps"},
        )
    return LinkProfile(
        rtt_ms=_require_number(
            data.get("rtt_ms"),
            field=f"{field}.rtt_ms",
            code=E_BAD_LINK,
            minimum=0.0,
        ),
        loss_rate=_require_number(
            data.get("loss_rate"),
            field=f"{field}.loss_rate",
            code=E_BAD_LINK,
            minimum=0.0,
            maximum=1.0,
        ),
        bandwidth_mbps=bandwidth_mbps,
        jitter_ms=_require_number(
            data.get("jitter_ms", 0.0),
            field=f"{field}.jitter_ms",
            code=E_BAD_LINK,
            minimum=0.0,
        ),
    )


def _parse_tolerance(raw: Any) -> Tolerance:
    field = "policy.tolerance"
    data = _require_mapping(raw, field=field, code=E_BAD_POLICY)
    tolerance = Tolerance(
        loss_rate_trigger=_require_number(
            data.get("loss_rate_trigger"),
            field=f"{field}.loss_rate_trigger",
            code=E_BAD_POLICY,
            minimum=0.0,
            maximum=1.0,
        ),
        rtt_ms_trigger=_require_number(
            data.get("rtt_ms_trigger"),
            field=f"{field}.rtt_ms_trigger",
            code=E_BAD_POLICY,
            minimum=0.0,
        ),
        loss_rate_delta=_require_number(
            data.get("loss_rate_delta"),
            field=f"{field}.loss_rate_delta",
            code=E_BAD_POLICY,
            minimum=0.0,
            maximum=1.0,
        ),
        rtt_ms_delta=_require_number(
            data.get("rtt_ms_delta"),
            field=f"{field}.rtt_ms_delta",
            code=E_BAD_POLICY,
            minimum=0.0,
        ),
        mtu_down_step=_require_int(
            data.get("mtu_down_step"),
            field=f"{field}.mtu_down_step",
            code=E_BAD_POLICY,
            minimum=1,
        ),
        min_mtu=_require_int(
            data.get("min_mtu"),
            field=f"{field}.min_mtu",
            code=E_BAD_POLICY,
            minimum=HEADER_SIZE + 1,
        ),
    )
    if tolerance.min_mtu > MAX_UDP_PAYLOAD:
        fail(
            E_BAD_POLICY,
            f"{field}.min_mtu exceeds UDP maximum",
            details={"field": f"{field}.min_mtu"},
        )
    return tolerance


def _parse_policy(raw: Any) -> Policy:
    field = "policy"
    data = _require_mapping(raw, field=field, code=E_BAD_POLICY)
    sample_points_raw = _require_list(
        data.get("sample_points"), field=f"{field}.sample_points", code=E_BAD_POLICY
    )
    sample_points = tuple(
        _require_int(
            value,
            field=f"{field}.sample_points[{index}]",
            code=E_BAD_POLICY,
            minimum=1,
        )
        for index, value in enumerate(sample_points_raw)
    )
    if not sample_points:
        fail(
            E_BAD_POLICY,
            f"{field}.sample_points must not be empty",
            details={"field": f"{field}.sample_points"},
        )
    if list(sample_points) != sorted(set(sample_points)):
        fail(
            E_BAD_POLICY,
            f"{field}.sample_points must be strictly increasing and unique",
            details={"field": f"{field}.sample_points"},
        )

    policy = Policy(
        fragment_threshold_bytes=_require_int(
            data.get("fragment_threshold_bytes"),
            field=f"{field}.fragment_threshold_bytes",
            code=E_BAD_POLICY,
            minimum=HEADER_SIZE + 1,
        ),
        max_concurrency=_require_int(
            data.get("max_concurrency"),
            field=f"{field}.max_concurrency",
            code=E_BAD_POLICY,
            minimum=1,
        ),
        round_budget_ms=_require_int(
            data.get("round_budget_ms"),
            field=f"{field}.round_budget_ms",
            code=E_BAD_POLICY,
            minimum=1,
        ),
        retransmit_budget=_require_int(
            data.get("retransmit_budget"),
            field=f"{field}.retransmit_budget",
            code=E_BAD_POLICY,
            minimum=0,
        ),
        logical_clock_step_ms=_require_int(
            data.get("logical_clock_step_ms"),
            field=f"{field}.logical_clock_step_ms",
            code=E_BAD_POLICY,
            minimum=1,
        ),
        clock_start_ms=_require_int(
            data.get("clock_start_ms", 0),
            field=f"{field}.clock_start_ms",
            code=E_BAD_POLICY,
            minimum=0,
        ),
        io_timeout_ms=_require_int(
            data.get("io_timeout_ms", 250),
            field=f"{field}.io_timeout_ms",
            code=E_BAD_POLICY,
            minimum=1,
        ),
        retransmit_backoff_ms=_require_int(
            data.get("retransmit_backoff_ms", 8),
            field=f"{field}.retransmit_backoff_ms",
            code=E_BAD_POLICY,
            minimum=1,
        ),
        max_backoff_ms=_require_int(
            data.get("max_backoff_ms", 64),
            field=f"{field}.max_backoff_ms",
            code=E_BAD_POLICY,
            minimum=1,
        ),
        crypto_overhead_bytes=_require_int(
            data.get("crypto_overhead_bytes", 0),
            field=f"{field}.crypto_overhead_bytes",
            code=E_BAD_POLICY,
            minimum=0,
        ),
        sample_points=sample_points,
        tolerance=_parse_tolerance(data.get("tolerance")),
    )
    if policy.retransmit_backoff_ms > policy.max_backoff_ms:
        fail(
            E_BAD_POLICY,
            "retransmit_backoff_ms must not exceed max_backoff_ms",
            details={"field": f"{field}.retransmit_backoff_ms"},
        )
    if policy.fragment_threshold_bytes > MAX_UDP_PAYLOAD:
        fail(
            E_BAD_POLICY,
            "fragment_threshold_bytes exceeds the UDP maximum",
            details={"field": f"{field}.fragment_threshold_bytes"},
        )
    minimum_adaptive_mtu = (
        HEADER_SIZE + policy.crypto_overhead_bytes + 1
    )
    if policy.tolerance.min_mtu < minimum_adaptive_mtu:
        fail(
            E_BAD_POLICY,
            "tolerance.min_mtu leaves no payload capacity after crypto overhead",
            details={
                "field": f"{field}.tolerance.min_mtu",
                "minimum": minimum_adaptive_mtu,
            },
        )
    return policy


def _parse_scenarios(
    raw: Any,
    *,
    peers: Mapping[str, Peer],
    known_fields: set[str],
    crypto_overhead_bytes: int,
) -> dict[str, Scenario]:
    if raw is None:
        return {}
    data = _require_mapping(raw, field="scenarios", code=E_BAD_TIMELINE)
    scenarios: dict[str, Scenario] = {}
    for name, raw_scenario in data.items():
        field = f"scenarios.{name}"
        if isinstance(raw_scenario, list):
            raw_changes = raw_scenario
        else:
            scenario_obj = _require_mapping(raw_scenario, field=field, code=E_BAD_TIMELINE)
            unknown = set(scenario_obj) - {"changes"}
            if unknown:
                fail(
                    E_BAD_TIMELINE,
                    "scenario contains unknown fields",
                    details={"field": field, "unknown": sorted(unknown)},
                )
            raw_changes = _require_list(
                scenario_obj.get("changes"),
                field=f"{field}.changes",
                code=E_BAD_TIMELINE,
            )
        changes: list[LinkChange] = []
        for index, raw_change in enumerate(raw_changes):
            change_field = f"{field}.changes[{index}]"
            change_obj = _require_mapping(
                raw_change, field=change_field, code=E_BAD_TIMELINE
            )
            unknown = set(change_obj) - known_fields
            if unknown:
                fail(
                    E_BAD_TIMELINE,
                    "link change contains unknown fields",
                    details={"field": change_field, "unknown": sorted(unknown)},
                )
            peer = _require_str(
                change_obj.get("peer"),
                field=f"{change_field}.peer",
                code=E_BAD_TIMELINE,
            )
            if peer not in peers:
                fail(
                    E_BAD_TIMELINE,
                    "link change references an unknown peer",
                    details={"field": f"{change_field}.peer", "peer": peer},
                )
            at_ms = _require_int(
                change_obj.get("at_ms"),
                field=f"{change_field}.at_ms",
                code=E_BAD_TIMELINE,
                minimum=0,
            )
            rtt_ms = (
                _require_number(
                    change_obj["rtt_ms"],
                    field=f"{change_field}.rtt_ms",
                    code=E_BAD_TIMELINE,
                    minimum=0.0,
                )
                if "rtt_ms" in change_obj
                else None
            )
            loss_rate = (
                _require_number(
                    change_obj["loss_rate"],
                    field=f"{change_field}.loss_rate",
                    code=E_BAD_TIMELINE,
                    minimum=0.0,
                    maximum=1.0,
                )
                if "loss_rate" in change_obj
                else None
            )
            bandwidth_mbps = (
                _require_number(
                    change_obj["bandwidth_mbps"],
                    field=f"{change_field}.bandwidth_mbps",
                    code=E_BAD_TIMELINE,
                )
                if "bandwidth_mbps" in change_obj
                else None
            )
            jitter_ms = (
                _require_number(
                    change_obj["jitter_ms"],
                    field=f"{change_field}.jitter_ms",
                    code=E_BAD_TIMELINE,
                    minimum=0.0,
                )
                if "jitter_ms" in change_obj
                else None
            )
            mtu = (
                _require_int(
                    change_obj["mtu"],
                    field=f"{change_field}.mtu",
                    code=E_BAD_MTU,
                    minimum=1,
                    maximum=MAX_UDP_PAYLOAD,
                )
                if "mtu" in change_obj
                else None
            )
            if mtu is not None:
                minimum_mtu = (
                    HEADER_SIZE
                    + peers[peer].crypto_overhead(crypto_overhead_bytes)
                    + 1
                )
                if mtu < minimum_mtu:
                    fail(
                        E_BAD_MTU,
                        "scenario MTU leaves no payload capacity for the peer",
                        details={
                            "field": f"{change_field}.mtu",
                            "minimum": minimum_mtu,
                            "peer": peer,
                        },
                    )
            changes.append(
                LinkChange(
                    at_ms=at_ms,
                    peer=peer,
                    rtt_ms=rtt_ms,
                    loss_rate=loss_rate,
                    bandwidth_mbps=bandwidth_mbps,
                    jitter_ms=jitter_ms,
                    mtu=mtu,
                )
            )
        changes.sort(key=lambda item: (item.at_ms, item.peer))
        scenarios[name] = Scenario(name=name, changes=tuple(changes))
    return scenarios


def validate_documents(
    peers_doc: Any,
    links_doc: Any,
    policy_doc: Any,
) -> ConfigBundle:
    """Validate already-decoded JSON documents without touching persistence."""

    policy = _parse_policy(policy_doc)

    peers_root = _require_mapping(peers_doc, field="peers document", code=E_BAD_CONFIG)
    raw_peers = _require_list(
        peers_root.get("peers"), field="peers", code=E_BAD_CONFIG
    )
    peers: dict[str, Peer] = {}
    for index, raw_peer in enumerate(raw_peers):
        peer = _parse_peer(raw_peer, index=index, policy=policy)
        if peer.name in peers:
            fail(
                E_DUPLICATE_PEER,
                "peer names must be unique",
                details={"peer": peer.name},
            )
        peers[peer.name] = peer
    if not peers:
        fail(E_BAD_CONFIG, "peer inventory must not be empty")

    links_root = _require_mapping(links_doc, field="links document", code=E_BAD_LINK)
    if set(links_root) - {"profiles", "scenarios"}:
        fail(
            E_BAD_LINK,
            "links document contains unknown fields",
            details={"unknown": sorted(set(links_root) - {"profiles", "scenarios"})},
        )
    raw_profiles = _require_mapping(
        links_root.get("profiles"), field="links.profiles", code=E_BAD_LINK
    )
    links: dict[str, LinkProfile] = {}
    for name, raw_profile in raw_profiles.items():
        if name not in peers:
            fail(
                E_NO_PROFILE,
                "link profile references an unknown peer",
                details={"peer": name},
            )
        links[name] = _parse_link(raw_profile, name=name)
    missing = sorted(set(peers) - set(links))
    if missing:
        fail(
            E_NO_PROFILE,
            "every peer must have a link profile",
            details={"missing": missing},
        )

    known_change_fields = {
        "at_ms",
        "peer",
        "rtt_ms",
        "loss_rate",
        "bandwidth_mbps",
        "jitter_ms",
        "mtu",
    }
    scenarios = _parse_scenarios(
        links_root.get("scenarios"),
        peers=peers,
        known_fields=known_change_fields,
        crypto_overhead_bytes=policy.crypto_overhead_bytes,
    )
    return ConfigBundle(peers=peers, links=links, scenarios=scenarios, policy=policy)


def _read_json(path: str | Path, *, code: str) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BusinessError(code, f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            code,
            f"invalid JSON: {path}",
            details={"line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise BusinessError(code, f"cannot read file: {path}") from exc


def load_config(
    inventory_path: str | Path,
    links_path: str | Path,
    policy_path: str | Path,
) -> ConfigBundle:
    """Load and strictly validate the three local sample documents."""

    return validate_documents(
        _read_json(inventory_path, code=E_BAD_CONFIG),
        _read_json(links_path, code=E_BAD_LINK),
        _read_json(policy_path, code=E_BAD_POLICY),
    )


def synthetic_payload(peer_name: str, size: int, seed: int = 0) -> bytes:
    """Create deterministic test payload bytes without random process state."""

    if not _is_int(size) or size <= 0:
        fail(E_BAD_PAYLOAD, "payload size must be a positive integer")
    if not _is_int(seed):
        fail(E_BAD_PAYLOAD, "payload seed must be an integer")
    chunks: list[bytes] = []
    counter = 0
    prefix = peer_name.encode("utf-8") + b"\x00" + str(seed).encode("ascii") + b"\x00"
    while sum(len(chunk) for chunk in chunks) < size:
        chunks.append(hashlib.sha256(prefix + counter.to_bytes(8, "big")).digest())
        counter += 1
    return b"".join(chunks)[:size]


def validate_payload_against_peer(payload: bytes, peer: Peer, policy: Policy) -> None:
    if not payload:
        fail(E_BAD_PAYLOAD, "payload must not be empty")
    maximum = peer.max_inner_payload(policy.crypto_overhead_bytes)
    if len(payload) > maximum:
        fail(
            E_BAD_PAYLOAD,
            "payload exceeds peer encapsulation limit",
            details={"actual": len(payload), "maximum": maximum, "peer": peer.name},
        )


def choose_peer(
    peers: Mapping[str, Peer],
    *,
    peer_name: str | None = None,
    payload_length: int | None = None,
    crypto_overhead_bytes: int = 0,
) -> Peer:
    if peer_name is not None:
        try:
            return peers[peer_name]
        except KeyError as exc:
            raise BusinessError(
                E_NOT_FOUND,
                "peer not found",
                details={"peer": peer_name},
            ) from exc
    candidates = sorted(peers.values(), key=lambda peer: peer.name)
    if payload_length is not None:
        candidates = [
            peer
            for peer in candidates
            if payload_length <= peer.max_inner_payload(crypto_overhead_bytes)
        ]
    if not candidates:
        fail(E_NOT_FOUND, "no suitable peer")
    return max(candidates, key=lambda peer: (peer.link_weight, peer.name))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def iter_peers(bundle: ConfigBundle) -> Iterable[Peer]:
    for name in sorted(bundle.peers):
        yield bundle.peers[name]
