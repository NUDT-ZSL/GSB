from __future__ import annotations

from typing import Any

from .canonical import canonical_json, sha256_bytes, sha256_json
from .models import BackendDecl, BackendState


FINGERPRINT_DOMAIN = b"ml-gateway-fingerprint-v1\x00"
CONTENT_DOMAIN = b"ml-gateway-content-v1\x00"

FINGERPRINT_QUANTIZATION = {
    "profile": "ml-gateway-fp1",
    "float": "IEEE-754 binary64",
    "byte_order": "big-endian",
    "slot_encoding": "16 lowercase hex characters per parameter",
    "negative_zero": "normalized to positive zero",
    "non_finite": "rejected",
    "vector_representation": "length-prefixed bit string list, no decimal float formatting",
    "config_digest": "SHA-256 of canonical JSON backend config (parameter file path excluded)",
    "load_sequence": "backend-local non-negative generation; first load is 0, reload increments",
    "combination": "SHA-256(domain || canonical vector || length || config digest || load sequence || content signature)",
    "fingerprint_format": "fp1:<64 lowercase hex characters>",
}


def backend_config_digest(decl: BackendDecl) -> str:
    return sha256_json(decl.config_summary())


def backend_content_signature(decl: BackendDecl) -> str:
    payload = canonical_json(
        {
            "config_digest": backend_config_digest(decl),
            "parameter_length": decl.parameter_length,
            "vector": decl.canonical_vector(),
        }
    ).encode("utf-8")
    return sha256_bytes(CONTENT_DOMAIN + payload)


def derive_fingerprint(decl: BackendDecl, load_sequence: int) -> tuple[str, str, str]:
    if load_sequence < 0:
        raise ValueError("load_sequence must be non-negative")
    config_digest = backend_config_digest(decl)
    content_signature = backend_content_signature(decl)
    payload: dict[str, Any] = {
        "config_digest": config_digest,
        "content_signature": content_signature,
        "load_sequence": load_sequence,
        "parameter_length": decl.parameter_length,
        "vector": decl.canonical_vector(),
    }
    digest = sha256_bytes(FINGERPRINT_DOMAIN + canonical_json(payload).encode("utf-8"))
    return f"fp1:{digest}", config_digest, content_signature


def materialize_backend_states(
    backends: tuple[BackendDecl, ...],
    previous: dict[str, BackendState] | None = None,
) -> dict[str, BackendState]:
    """Materialize states in stable backend-id order.

    A backend-local load sequence is preserved only while its parameter vector and
    effective backend config are byte-for-byte equivalent. This makes initial
    batch loads independent of declaration order and worker scheduling.
    """

    previous = previous or {}
    states: dict[str, BackendState] = {}
    for decl in sorted(backends, key=lambda item: item.backend_id):
        old = previous.get(decl.backend_id)
        content_signature = backend_content_signature(decl)
        if old is not None and old.content_signature == content_signature:
            load_sequence = old.load_sequence
            fingerprint = derive_fingerprint(decl, load_sequence)[0]
            if fingerprint != old.fingerprint:
                raise ValueError("fingerprint derivation is not deterministic")
            config_digest = old.config_digest
        else:
            load_sequence = 0 if old is None else old.load_sequence + 1
            fingerprint, config_digest, content_signature = derive_fingerprint(
                decl, load_sequence
            )
        states[decl.backend_id] = BackendState(
            backend_id=decl.backend_id,
            model_id=decl.model_id,
            host=decl.host,
            port=decl.port,
            max_concurrency=decl.max_concurrency,
            parameter_length=decl.parameter_length,
            load_sequence=load_sequence,
            content_signature=content_signature,
            config_digest=config_digest,
            fingerprint=fingerprint,
            canonical_vector=decl.canonical_vector(),
        )
    return states


def fingerprint_map(states: dict[str, BackendState] | dict[str, dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for backend_id, state in states.items():
        if isinstance(state, BackendState):
            result[backend_id] = state.fingerprint
        else:
            result[backend_id] = str(state["fingerprint"])
    return dict(sorted(result.items()))


def fingerprint_difference(
    previous: dict[str, BackendState], current: dict[str, BackendState]
) -> dict[str, Any]:
    previous_map = fingerprint_map(previous)
    current_map = fingerprint_map(current)
    previous_ids = set(previous_map)
    current_ids = set(current_map)
    common = previous_ids & current_ids
    changed = sorted(
        backend_id
        for backend_id in common
        if previous_map[backend_id] != current_map[backend_id]
    )
    return {
        "changed": changed,
        "added": sorted(current_ids - previous_ids),
        "removed": sorted(previous_ids - current_ids),
        "affected": sorted(set(changed) | (previous_ids - current_ids) | (current_ids - previous_ids)),
        "previous": previous_map,
        "current": current_map,
    }
