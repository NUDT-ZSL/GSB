"""High-level plan construction for library users."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .arbiter import AlgorithmDecision, decide_algorithm
from .codec import FragmentPacket, derive_message_id, fragment_payload, sha256_hex
from .config import LinkProfile, Peer, Policy, validate_payload_against_peer
from .store import tunnel_id_for


@dataclass(frozen=True, slots=True)
class TunnelPlan:
    tunnel_id: str
    payload: bytes
    payload_digest: str
    message_id: str
    fragments: tuple[FragmentPacket, ...]
    decision: AlgorithmDecision
    mtu: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.decision.as_dict(),
            "fragments": [
                {
                    "digest": packet.digest,
                    "final": packet.final,
                    "sequence": packet.sequence,
                    "wire_bytes": len(packet.raw),
                }
                for packet in self.fragments
            ],
            "message_id": self.message_id,
            "mtu": self.mtu,
            "payload_bytes": len(self.payload),
            "payload_digest": self.payload_digest,
            "tunnel_id": self.tunnel_id,
        }


def build_plan(
    *,
    peer: Peer,
    payload: bytes,
    policy: Policy,
    link: LinkProfile,
    mtu: int | None = None,
) -> TunnelPlan:
    validate_payload_against_peer(payload, peer, policy)
    effective_mtu = peer.outer_mtu if mtu is None else mtu
    payload_digest = sha256_hex(payload)
    tunnel_id = tunnel_id_for(peer.name, payload_digest)
    message_id = derive_message_id(tunnel_id, payload_digest)
    decision = decide_algorithm(
        peer=peer,
        policy=policy,
        link=link,
        payload_bytes=len(payload),
        mtu=effective_mtu,
    )
    fragments = tuple(
        fragment_payload(
            payload,
            message_id=message_id,
            mtu=effective_mtu,
            fragment_threshold_bytes=policy.fragment_threshold_bytes,
            encrypted=peer.encrypted,
            crypto_overhead_bytes=policy.crypto_overhead_bytes,
        )
    )
    return TunnelPlan(
        tunnel_id=tunnel_id,
        payload=payload,
        payload_digest=payload_digest,
        message_id=message_id,
        fragments=fragments,
        decision=decision,
        mtu=effective_mtu,
    )
