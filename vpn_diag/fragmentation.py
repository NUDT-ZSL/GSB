"""Deterministic inner-payload fragmentation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .codes import E_MTU_INFEASIBLE, E_SYNC_MARKER_MISMATCH
from .model import PeerConfig, Policy


class MtuInfeasibleError(Exception):
    code = E_MTU_INFEASIBLE

    def __init__(self, message: str, *, boundary: int, upper: int):
        super().__init__(message)
        self.boundary = boundary
        self.upper = upper


@dataclass(frozen=True)
class FragmentSpec:
    key: str
    marker: str
    peer_name: str
    payload_index: int
    fragment_index: int
    offset: int
    length: int
    wire_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "marker": self.marker,
            "peer": self.peer_name,
            "payload_index": self.payload_index,
            "fragment_index": self.fragment_index,
            "offset": self.offset,
            "length": self.length,
            "wire_size": self.wire_size,
        }


def calculate_inner_upper(
    outer_mtu: int, crypto_overhead: int, fragment_threshold: int
) -> tuple[int, int]:
    """Return ``(inner_upper, outer_boundary)`` using integer arithmetic."""
    boundary = int(outer_mtu) - int(crypto_overhead)
    upper = int(fragment_threshold)
    if boundary <= 0:
        raise MtuInfeasibleError(
            "outer MTU minus crypto placeholder overhead is not positive",
            boundary=boundary,
            upper=upper,
        )
    if upper == 0:
        raise MtuInfeasibleError(
            "computed single-fragment inner payload upper bound is zero",
            boundary=boundary,
            upper=upper,
        )
    if upper > boundary:
        raise MtuInfeasibleError(
            "fragment threshold crosses outer MTU minus overhead boundary",
            boundary=boundary,
            upper=upper,
        )
    return upper, boundary


def make_fragment_key(peer_name: str, payload_index: int, fragment_index: int) -> str:
    return f"{peer_name}:p{payload_index:04d}:f{fragment_index:04d}"


def make_marker(spec: FragmentSpec) -> str:
    return f"SYNC:{spec.key}:{spec.offset}:{spec.length}"


def plan_peer_fragments(peer: PeerConfig, policy: Policy) -> list[FragmentSpec]:
    threshold = peer.fragment_threshold
    if threshold is None:
        threshold = policy.fragment_threshold
    upper, _boundary = calculate_inner_upper(
        peer.outer_mtu, peer.crypto_overhead, threshold
    )
    specs: list[FragmentSpec] = []
    for payload_index, payload_length in enumerate(peer.payload_lengths):
        offset = 0
        fragment_index = 0
        while offset < payload_length:
            length = min(upper, payload_length - offset)
            key = make_fragment_key(peer.name, payload_index, fragment_index)
            spec = FragmentSpec(
                key=key,
                marker="",
                peer_name=peer.name,
                payload_index=payload_index,
                fragment_index=fragment_index,
                offset=offset,
                length=length,
                wire_size=length + peer.crypto_overhead,
            )
            object.__setattr__(spec, "marker", make_marker(spec))
            specs.append(spec)
            offset += length
            fragment_index += 1
    return specs


def compare_sync_markers(
    specs: list[FragmentSpec], declared_markers: list[str]
) -> dict[str, Any]:
    expected = [spec.marker for spec in specs]
    accepted_declarations = set(declared_markers)
    missing_declared = [marker for marker in expected if marker not in accepted_declarations
                        and not any(spec.key in accepted_declarations for spec in specs
                                    if spec.marker == marker)]
    declared_set = set(declared_markers)
    expected_set = set(expected)
    key_set = {spec.key for spec in specs}
    unmatched_declared = [
        marker for marker in declared_markers
        if marker not in expected_set and marker not in key_set
    ]
    matched = len(expected) - len(missing_declared)
    return {
        "expected_count": len(expected),
        "declared_count": len(declared_markers),
        "matched_count": matched,
        "missing_markers": missing_declared,
        "unmatched_declared_markers": unmatched_declared,
        "consistent": not missing_declared and not unmatched_declared,
    }


def plan_all(
    peers: list[PeerConfig], policy: Optional[Policy]
) -> tuple[dict[str, list[FragmentSpec]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    plans: dict[str, list[FragmentSpec]] = {}
    errors: list[dict[str, Any]] = []
    marker_reports: dict[str, dict[str, Any]] = {}
    if policy is None:
        return plans, errors, marker_reports
    for peer in peers:
        try:
            specs = plan_peer_fragments(peer, policy)
            plans[peer.name] = specs
            marker_reports[peer.name] = compare_sync_markers(specs, peer.sync_markers)
            if not marker_reports[peer.name]["consistent"]:
                errors.append({
                    "source_category": "sync_marker",
                    "record_index": peer.record_index,
                    "peer": peer.name,
                    "code": E_SYNC_MARKER_MISMATCH,
                    "detail": marker_reports[peer.name],
                })
        except MtuInfeasibleError as exc:
            plans[peer.name] = []
            errors.append({
                "source_category": "peer",
                "record_index": peer.record_index,
                "peer": peer.name,
                "code": E_MTU_INFEASIBLE,
                "message": str(exc),
                "boundary": exc.boundary,
                "upper": exc.upper,
            })
    return plans, errors, marker_reports
