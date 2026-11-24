"""Strict JSON sample parsing and normalization."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .codes import (
    E_ECHO_ADDR_DUPLICATE,
    E_ECHO_ADDR_INVALID,
    E_EVENT_TARGET_UNKNOWN,
    E_EVENT_TYPE_UNKNOWN,
    E_FIELD_REQUIRED,
    E_FIELD_TYPE,
    E_MTU_SIZE,
    E_PEER_NAME_DUPLICATE,
    E_PROFILE_UNKNOWN,
    E_VALUE_RANGE,
    E_VALIDATION,
)
from .fixedpoint import (
    FIXED_SCALE,
    FixedPointError,
    integer_field,
    parse_fixed,
    parse_positive_fixed,
    parse_unit_interval,
    q_to_float,
    to_decimal,
)
from .model import EventRecord, LinkProfile, ParseResult, PeerConfig, Policy


class RecordValidationError(Exception):
    def __init__(self, code: str, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def load_raw_sample(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle, parse_float=Decimal)


def loads_raw_sample(text: str) -> dict[str, Any]:
    return json.loads(text, parse_float=Decimal)


def _rejection(
    source_category: str,
    record_index: int,
    code: str,
    message: str,
    field: Optional[str] = None,
) -> dict[str, Any]:
    item = {
        "source_category": source_category,
        "record_index": record_index,
        "record_number": record_index + 1,
        "code": code,
        "message": message,
    }
    if field:
        item["field"] = field
    return item


def _require_dict(record: Any, category: str, index: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise RecordValidationError(E_FIELD_TYPE, f"{category} record must be an object")
    return record


def _parse_address(value: Any) -> tuple[str, int, str]:
    if isinstance(value, dict):
        host = value.get("host", value.get("ip", "127.0.0.1"))
        port = value.get("port")
    else:
        if not isinstance(value, str) or ":" not in value:
            raise RecordValidationError(
                E_ECHO_ADDR_INVALID,
                "echo_addr must be 'host:port' or an object",
                "echo_addr",
            )
        host, port_text = value.rsplit(":", 1)
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        try:
            port = int(port_text)
        except ValueError as exc:
            raise RecordValidationError(
                E_ECHO_ADDR_INVALID, "echo port must be an integer", "echo_addr"
            ) from exc
    if host == "localhost":
        host = "127.0.0.1"
    if host not in {"127.0.0.1", "::1"}:
        raise RecordValidationError(
            E_ECHO_ADDR_INVALID, "only loopback echo addresses are allowed", "echo_addr"
        )
    try:
        port = integer_field(port, "port", minimum=0)
    except FixedPointError as exc:
        raise RecordValidationError(E_ECHO_ADDR_INVALID, str(exc), "port") from exc
    if port > 65535:
        raise RecordValidationError(E_ECHO_ADDR_INVALID, "port must be <= 65535", "port")
    normalized_host = "127.0.0.1" if host == "::1" else host
    return normalized_host, port, f"{normalized_host}:{port}"


def _parse_lengths(record: dict[str, Any]) -> list[int]:
    raw_lengths: Any = None
    if "payload_lengths" in record:
        raw_lengths = record["payload_lengths"]
    elif "length_distribution" in record:
        distribution = record["length_distribution"]
        if isinstance(distribution, list):
            if distribution and all(isinstance(item, int) and not isinstance(item, bool) for item in distribution):
                raw_lengths = distribution
            else:
                expanded: list[int] = []
                for bucket in distribution:
                    if not isinstance(bucket, dict):
                        raise RecordValidationError(
                            E_FIELD_TYPE, "length distribution buckets must be objects"
                        )
                    length = integer_field(bucket.get("length"), "length", minimum=1)
                    count = integer_field(bucket.get("count", 1), "count", minimum=1)
                    expanded.extend([length] * count)
                raw_lengths = expanded
        elif isinstance(distribution, dict):
            if "lengths" in distribution:
                raw_lengths = distribution["lengths"]
            elif "buckets" in distribution:
                expanded = []
                for bucket in distribution["buckets"]:
                    length = integer_field(bucket.get("length"), "length", minimum=1)
                    count = integer_field(bucket.get("count", 1), "count", minimum=1)
                    expanded.extend([length] * count)
                raw_lengths = expanded
        else:
            raise RecordValidationError(E_FIELD_TYPE, "length_distribution has unsupported type")

    if not isinstance(raw_lengths, list) or not raw_lengths:
        raise RecordValidationError(
            E_FIELD_REQUIRED, "payload_lengths or a non-empty length_distribution is required",
            "payload_lengths",
        )
    lengths: list[int] = []
    for item in raw_lengths:
        try:
            lengths.append(integer_field(item, "payload length", minimum=1))
        except FixedPointError as exc:
            raise RecordValidationError(E_VALUE_RANGE, str(exc), "payload_lengths") from exc
    return lengths


def parse_profile(record: Any, index: int = 0, *, category: str = "profile") -> LinkProfile:
    record = _require_dict(record, category, index)
    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RecordValidationError(E_FIELD_REQUIRED, "profile name is required", "name")
    try:
        rtt_value = record.get("rtt_ms", record.get("rtt"))
        rtt_dec = to_decimal(rtt_value, "rtt_ms")
        if rtt_dec < 0:
            raise FixedPointError("rtt_ms must be >= 0")
        rtt_q = parse_fixed(rtt_dec, field_name="rtt_ms", minimum=0)
        loss_dec = to_decimal(record.get("loss_rate"), "loss_rate")
        loss_q = parse_unit_interval(loss_dec, "loss_rate")
        bandwidth_value = record.get(
            "bandwidth_kbps",
            record.get("available_bandwidth_kbps", record.get("bandwidth")),
        )
        bandwidth_dec = to_decimal(bandwidth_value, "bandwidth_kbps")
        if bandwidth_dec <= 0:
            raise FixedPointError("bandwidth_kbps must be positive")
        bandwidth_q = parse_positive_fixed(bandwidth_dec, "bandwidth_kbps")
        jitter_value = record.get(
            "jitter_limit",
            record.get("jitter_max", record.get("jitter_upper")),
        )
        jitter_dec = to_decimal(jitter_value, "jitter_limit")
        jitter_q = parse_unit_interval(jitter_dec, "jitter_limit")
    except FixedPointError as exc:
        code = E_VALUE_RANGE if "must" in str(exc) else E_FIELD_TYPE
        raise RecordValidationError(code, str(exc)) from exc

    return LinkProfile(
        name=name.strip(),
        rtt_q=rtt_q,
        rtt_f=float(rtt_dec),
        loss_q=loss_q,
        loss_f=float(loss_dec),
        bandwidth_q=bandwidth_q,
        bandwidth_f=float(bandwidth_dec),
        jitter_q=jitter_q,
        jitter_f=float(jitter_dec),
        record_index=index,
    )


def _parse_policy(raw_policy: Any, rejections: list[dict[str, Any]]) -> Optional[Policy]:
    if raw_policy is None:
        rejections.append(_rejection("policy", 0, E_FIELD_REQUIRED, "policy is required"))
        return None
    try:
        record = _require_dict(raw_policy, "policy", 0)
        threshold = integer_field(record.get("fragment_threshold"), "fragment_threshold", minimum=0)
        concurrency_value = record.get(
            "concurrency_per_peer",
            record.get("concurrency_limit", record.get("max_concurrency")),
        )
        concurrency = integer_field(concurrency_value, "concurrency_per_peer", minimum=1)
        retransmit_budget = integer_field(
            record.get("retransmit_budget"), "retransmit_budget", minimum=0
        )
        budget_value = record.get("round_budget_ms")
        budget_dec = to_decimal(budget_value, "round_budget_ms")
        if budget_dec <= 0:
            raise FixedPointError("round_budget_ms must be positive")
        budget_q = parse_positive_fixed(budget_dec, "round_budget_ms")

        tolerance_value = record.get("tolerance_ms", record.get("tolerance", 0))
        tolerance_dec = to_decimal(tolerance_value, "tolerance_ms")
        if tolerance_dec < 0:
            raise FixedPointError("tolerance_ms must be >= 0")
        tolerance_q = parse_fixed(tolerance_dec, field_name="tolerance_ms", minimum=0)

        step_value = record.get(
            "clock_step_ms",
            record.get("logical_clock_step_ms", record.get("clock_step")),
        )
        step_dec = to_decimal(step_value, "clock_step_ms")
        if step_dec <= 0:
            raise FixedPointError("clock_step_ms must be positive")
        step_q = parse_positive_fixed(step_dec, "clock_step_ms")

        max_rounds = record.get("max_rounds", 32)
        max_rounds = integer_field(max_rounds, "max_rounds", minimum=1)
        udp_timeout = record.get("udp_timeout_ms", 200)
        udp_timeout = integer_field(udp_timeout, "udp_timeout_ms", minimum=1)
        checkpoint_every = record.get("checkpoint_every", 1)
        checkpoint_every = integer_field(checkpoint_every, "checkpoint_every", minimum=1)
    except (RecordValidationError, FixedPointError) as exc:
        code = getattr(exc, "code", E_VALUE_RANGE)
        rejections.append(_rejection("policy", 0, code, str(exc), getattr(exc, "field", None)))
        return None

    return Policy(
        fragment_threshold=threshold,
        concurrency_per_peer=concurrency,
        round_budget_q=budget_q,
        round_budget_f=float(budget_dec),
        retransmit_budget=retransmit_budget,
        tolerance_q=tolerance_q,
        tolerance_f=float(tolerance_dec),
        clock_step_q=step_q,
        clock_step_f=float(step_dec),
        max_rounds=max_rounds,
        udp_timeout_ms=udp_timeout,
        checkpoint_every=checkpoint_every,
    )


def _parse_peer(
    raw_peer: Any,
    index: int,
    profiles: dict[str, LinkProfile],
    seen_names: set[str],
    seen_addresses: set[str],
) -> PeerConfig:
    record = _require_dict(raw_peer, "peer", index)
    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RecordValidationError(E_FIELD_REQUIRED, "peer name is required and must be a string", "name")
    name = name.strip()
    if name in seen_names:
        raise RecordValidationError(E_PEER_NAME_DUPLICATE, f"duplicate peer name {name!r}", "name")

    host, port, address = _parse_address(record.get("echo_addr", record.get("listen_addr")))
    if address in seen_addresses:
        raise RecordValidationError(E_ECHO_ADDR_DUPLICATE, f"duplicate loopback address {address}", "echo_addr")

    try:
        outer_mtu = integer_field(record.get("outer_mtu"), "outer_mtu", minimum=1)
        overhead = integer_field(record.get("crypto_overhead", 0), "crypto_overhead", minimum=0)
        weight_dec = to_decimal(record.get("link_weight", 1), "link_weight")
        if weight_dec <= 0:
            raise FixedPointError("link_weight must be positive")
        weight_q = parse_positive_fixed(weight_dec, "link_weight")
        lengths = _parse_lengths(record)
        estimate_max = record.get("inner_payload_estimate_max", record.get("estimate_max", max(lengths)))
        estimate_max = integer_field(estimate_max, "inner_payload_estimate_max", minimum=1)
        if estimate_max < max(lengths):
            raise RecordValidationError(
                E_VALUE_RANGE,
                "inner_payload_estimate_max must cover every declared payload length",
                "inner_payload_estimate_max",
            )
        if outer_mtu < estimate_max:
            raise RecordValidationError(
                E_MTU_SIZE,
                f"outer_mtu {outer_mtu} must be >= inner payload estimate upper bound {estimate_max}",
                "outer_mtu",
            )
    except FixedPointError as exc:
        raise RecordValidationError(E_VALUE_RANGE, str(exc)) from exc

    profile_value = record.get("profile", record.get("profile_name", "default"))
    if isinstance(profile_value, dict):
        inline = parse_profile(profile_value, index)
        profiles[inline.name] = inline
        profile_name = inline.name
    else:
        if not isinstance(profile_value, str) or not profile_value.strip():
            raise RecordValidationError(E_PROFILE_UNKNOWN, "profile name must be a string", "profile")
        profile_name = profile_value.strip()
        if profile_name not in profiles:
            raise RecordValidationError(
                E_PROFILE_UNKNOWN, f"profile {profile_name!r} is not declared", "profile"
            )

    markers_raw = record.get("sync_markers", [])
    if not isinstance(markers_raw, list) or not all(isinstance(item, (str, int)) for item in markers_raw):
        raise RecordValidationError(E_FIELD_TYPE, "sync_markers must be a list of strings", "sync_markers")
    markers = [str(item) for item in markers_raw]

    threshold = record.get("fragment_threshold")
    if threshold is not None:
        threshold = integer_field(threshold, "fragment_threshold", minimum=0)
    concurrency_limit = record.get("concurrency_limit")
    if concurrency_limit is not None:
        concurrency_limit = integer_field(concurrency_limit, "concurrency_limit", minimum=1)

    seen_names.add(name)
    seen_addresses.add(address)
    return PeerConfig(
        name=name,
        host=host,
        port=port,
        address=address,
        outer_mtu=outer_mtu,
        crypto_overhead=overhead,
        link_weight_q=weight_q,
        link_weight_f=float(weight_dec),
        estimate_max=estimate_max,
        payload_lengths=lengths,
        sync_markers=markers,
        profile_name=profile_name,
        record_index=index,
        fragment_threshold=threshold,
        concurrency_limit=concurrency_limit,
    )


def _parse_event(
    raw_event: Any,
    index: int,
    peer_names: set[str],
    profiles: dict[str, LinkProfile],
    policy: Optional[Policy],
) -> EventRecord:
    record = _require_dict(raw_event, "event", index)
    raw_type = record.get("type", record.get("event_type"))
    aliases = {
        "profile": "profile_change",
        "link_profile_change": "profile_change",
        "profile_change": "profile_change",
        "loss": "loss_segment",
        "loss_burst": "loss_segment",
        "loss_segment": "loss_segment",
        "retransmission": "retransmit",
        "inject_retransmit": "retransmit",
        "retransmit": "retransmit",
    }
    event_type = aliases.get(raw_type) if isinstance(raw_type, str) else None
    if event_type is None:
        raise RecordValidationError(E_EVENT_TYPE_UNKNOWN, f"unknown event type {raw_type!r}", "type")

    at_value = record.get("at_clock_ms", record.get("at_clock", record.get("clock_ms", record.get("at_ms", 0))))
    at_dec = to_decimal(at_value, "at_clock_ms")
    if at_dec < 0:
        raise RecordValidationError(E_VALUE_RANGE, "event clock must be nonnegative", "at_clock_ms")
    at_q = parse_fixed(at_dec, field_name="at_clock_ms", minimum=0)

    peer_name = record.get("peer", record.get("peer_name", record.get("target")))
    if not isinstance(peer_name, str) or peer_name not in peer_names:
        raise RecordValidationError(
            E_EVENT_TARGET_UNKNOWN, f"event target peer {peer_name!r} is not accepted", "peer"
        )

    data = {key: value for key, value in record.items() if key not in {"type", "event_type"}}
    profile: Optional[LinkProfile] = None

    if event_type == "profile_change":
        raw_profile = record.get("profile", record.get("to_profile"))
        profile_name = record.get("profile_name", record.get("to_profile_name"))
        if isinstance(raw_profile, dict):
            profile = parse_profile(raw_profile, index, category="event_profile")
        elif isinstance(profile_name, str) and profile_name in profiles:
            profile = profiles[profile_name]
        elif isinstance(raw_profile, str) and raw_profile in profiles:
            profile = profiles[raw_profile]
        else:
            raise RecordValidationError(
                E_PROFILE_UNKNOWN, "profile_change requires a valid profile object or profile_name",
                "profile",
            )
        data["profile_name"] = profile.name

    if event_type == "loss_segment":
        until = record.get("until_clock_ms", record.get("end_clock_ms", record.get("until")))
        duration = record.get("duration_ms")
        if until is not None:
            until_dec = to_decimal(until, "until_clock_ms")
            until_q = parse_fixed(until_dec, field_name="until_clock_ms", minimum=0)
        elif duration is not None:
            duration_q = parse_positive_fixed(to_decimal(duration, "duration_ms"), "duration_ms")
            until_q = at_q + duration_q
        else:
            if policy is None:
                until_q = at_q + FIXED_SCALE
            else:
                until_q = at_q + policy.clock_step_q
        if until_q <= at_q:
            raise RecordValidationError(E_VALUE_RANGE, "loss segment end must be after its start")
        data["start_q"] = at_q
        data["until_q"] = until_q

    if event_type == "retransmit":
        fragment_key = record.get("fragment_key", record.get("key"))
        if fragment_key is not None and not isinstance(fragment_key, str):
            raise RecordValidationError(E_FIELD_TYPE, "fragment_key must be a string")
        data["fragment_key"] = fragment_key

    return EventRecord(
        index=index,
        event_type=event_type,
        at_q=at_q,
        at_ms=q_to_float(at_q),
        peer_name=peer_name,
        data=data,
        profile=profile,
    )


def parse_sample(raw: Any) -> ParseResult:
    rejections: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        rejections.append(_rejection("sample", 0, E_FIELD_TYPE, "top-level sample must be an object"))
        return ParseResult([], {}, None, [], rejections)

    schema_version = raw.get("schema_version", 1)
    seed = raw.get("seed")
    if seed is not None and not isinstance(seed, int):
        rejections.append(_rejection("sample", 0, E_FIELD_TYPE, "seed must be an integer", "seed"))
        seed = None
    origin = raw.get("logical_clock_origin_ms", 0)
    try:
        origin_f = float(to_decimal(origin, "logical_clock_origin_ms"))
    except FixedPointError:
        origin_f = 0.0
        rejections.append(_rejection("sample", 0, E_FIELD_TYPE, "logical_clock_origin_ms must be numeric"))

    profiles: dict[str, LinkProfile] = {}
    raw_profiles = raw.get("profiles", [])
    if not isinstance(raw_profiles, list):
        rejections.append(_rejection("profile", 0, E_FIELD_TYPE, "profiles must be a list"))
    else:
        for index, item in enumerate(raw_profiles):
            try:
                profile = parse_profile(item, index)
                if profile.name in profiles:
                    raise RecordValidationError(
                        E_VALIDATION, f"duplicate profile name {profile.name!r}", "name"
                    )
                profiles[profile.name] = profile
            except RecordValidationError as exc:
                rejections.append(
                    _rejection("profile", index, exc.code, exc.message, exc.field)
                )
            except Exception as exc:  # defensive: one bad record cannot stop import
                rejections.append(_rejection("profile", index, E_VALIDATION, str(exc)))

    policy = _parse_policy(raw.get("policy"), rejections)

    peers: list[PeerConfig] = []
    seen_names: set[str] = set()
    seen_addresses: set[str] = set()
    raw_peers = raw.get("peers", [])
    if not isinstance(raw_peers, list):
        rejections.append(_rejection("peer", 0, E_FIELD_TYPE, "peers must be a list"))
    else:
        for index, item in enumerate(raw_peers):
            try:
                peer = _parse_peer(item, index, profiles, seen_names, seen_addresses)
                peers.append(peer)
            except RecordValidationError as exc:
                rejections.append(_rejection("peer", index, exc.code, exc.message, exc.field))
            except FixedPointError as exc:
                rejections.append(_rejection("peer", index, E_VALUE_RANGE, str(exc)))
            except Exception as exc:
                rejections.append(_rejection("peer", index, E_VALIDATION, str(exc)))

    events: list[EventRecord] = []
    raw_events = raw.get("events", [])
    if not isinstance(raw_events, list):
        rejections.append(_rejection("event", 0, E_FIELD_TYPE, "events must be a list"))
    else:
        peer_names = {peer.name for peer in peers}
        for index, item in enumerate(raw_events):
            try:
                event = _parse_event(item, index, peer_names, profiles, policy)
                events.append(event)
            except RecordValidationError as exc:
                rejections.append(_rejection("event", index, exc.code, exc.message, exc.field))
            except FixedPointError as exc:
                rejections.append(_rejection("event", index, E_VALUE_RANGE, str(exc)))

    events.sort(key=lambda event: (event.at_q, event.index))
    return ParseResult(
        peers=peers,
        profiles=profiles,
        policy=policy,
        events=events,
        rejections=rejections,
        schema_version=schema_version if isinstance(schema_version, int) else 1,
        seed=seed if isinstance(seed, int) else None,
        clock_origin_ms=origin_f,
        raw=raw if isinstance(raw, dict) else {},
    )


def parse_file(path: str | Path) -> ParseResult:
    return parse_sample(load_raw_sample(path))
