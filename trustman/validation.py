"""Pure deterministic certificate-chain validation and invalidation reports."""

from __future__ import annotations

import time
from typing import Callable, Iterable

from .errors import (
    E_CHAIN_BROKEN,
    E_CHAIN_EMPTY,
    E_EXPIRED_INTERMEDIATE,
    E_EXPIRED_LEAF,
    E_EXPIRED_ROOT,
    E_MISSING_CERT,
    E_MISSING_INTERMEDIATE,
    E_NOT_YET_VALID_INTERMEDIATE,
    E_NOT_YET_VALID_LEAF,
    E_NOT_YET_VALID_ROOT,
    E_NO_SNAPSHOT,
    E_REVOKED_INTERMEDIATE,
    E_REVOKED_LEAF,
    E_REVOKED_ROOT,
    E_ROOT_UNTRUSTED,
    TaskTimeout,
)
from .models import Certificate, ChainDeclaration, Snapshot


TRUSTED = "trusted"
UNTRUSTED = "untrusted"
PARTIALLY_TRUSTED = "partially_trusted"

StepHook = Callable[[str, int, str], None]


def _error(
    code: str,
    *,
    position: int | None = None,
    cert_id: str | None = None,
    reason: str,
    message: str,
) -> dict:
    return {
        "code": code,
        "position": position,
        "cert_id": cert_id,
        "reason": reason,
        "message": message,
    }


def _summary(conclusion: str, snapshot: Snapshot | None) -> str:
    version = str(snapshot.version) if snapshot is not None else "none"
    policy = snapshot.policy_version if snapshot is not None else "none"
    return f"{conclusion}:snapshot={version}:policy={policy}"


def _kind(position: int, length: int) -> str:
    if position == 0:
        return "leaf"
    if position == length - 1:
        return "root"
    return "intermediate"


def _error_suffix(kind: str) -> str:
    return {
        "leaf": "LEAF",
        "intermediate": "INTERMEDIATE",
        "root": "ROOT",
    }[kind]


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.perf_counter() >= deadline:
        raise TaskTimeout()


def validate_chain(
    chain: ChainDeclaration,
    certificates: dict[str, Certificate],
    snapshot: Snapshot | None,
    *,
    deadline_monotonic: float | None = None,
    step_hook: StepHook | None = None,
) -> dict:
    """Validate one declared path without mutating shared state.

    Trust is three-valued:
    - trusted: complete issuer path reaches a trusted anchor and is currently valid.
    - partially_trusted: the path and anchor are sound, but a cert is expired or revoked.
    - untrusted: the path or trust anchor itself is not sound.
    """

    errors: list[dict] = []
    invalidations: list[dict] = []
    ids = list(chain.certificate_ids)
    basis = {
        "snapshot_version": snapshot.version if snapshot is not None else None,
        "policy_version": snapshot.policy_version if snapshot is not None else None,
        "observed_at_ms": snapshot.observed_at_ms if snapshot is not None else None,
        "snapshot_fingerprint": (
            snapshot.content_fingerprint if snapshot is not None else None
        ),
    }

    _check_deadline(deadline_monotonic)

    if snapshot is None:
        errors.append(
            _error(
                E_NO_SNAPSHOT,
                reason="no_effective_snapshot",
                message="no trust-anchor snapshot is available",
            )
        )
        conclusion = UNTRUSTED
        actual_summary = _summary(conclusion, snapshot)
        return {
            "conclusion": conclusion,
            "errors": errors,
            "invalidations": invalidations,
            "basis": basis,
            "actual_summary": actual_summary,
            "matches_expected": actual_summary == chain.expected_summary,
        }

    if not ids:
        errors.append(
            _error(
                E_CHAIN_EMPTY,
                position=0,
                reason="empty_chain",
                message="certificate_ids must contain at least one identifier",
            )
        )
        conclusion = UNTRUSTED
        actual_summary = _summary(conclusion, snapshot)
        return {
            "conclusion": conclusion,
            "errors": errors,
            "invalidations": invalidations,
            "basis": basis,
            "actual_summary": actual_summary,
            "matches_expected": actual_summary == chain.expected_summary,
        }

    missing_positions: list[int] = []
    for position, cert_id in enumerate(ids):
        _check_deadline(deadline_monotonic)
        if step_hook is not None:
            step_hook(chain.chain_id, position, cert_id)
        _check_deadline(deadline_monotonic)
        if cert_id not in certificates:
            missing_positions.append(position)
            code = (
                E_MISSING_INTERMEDIATE
                if 0 < position < len(ids) - 1
                else E_MISSING_CERT
            )
            errors.append(
                _error(
                    code,
                    position=position,
                    cert_id=cert_id,
                    reason="certificate_not_found",
                    message=f"certificate '{cert_id}' is not available",
                )
            )

    path_sound = not missing_positions
    if path_sound:
        for position in range(len(ids) - 1):
            _check_deadline(deadline_monotonic)
            child = certificates[ids[position]]
            expected_parent = ids[position + 1]
            if child.issuer_id != expected_parent:
                if (
                    child.issuer_id in certificates
                    and child.issuer_id not in ids
                    and expected_parent in snapshot.trusted_roots
                ):
                    errors.append(
                        _error(
                            E_MISSING_INTERMEDIATE,
                            position=position + 1,
                            cert_id=child.issuer_id,
                            reason="issuer_omitted_from_chain",
                            message=(
                                f"issuer '{child.issuer_id}' is omitted between "
                                f"'{ids[position]}' and '{expected_parent}'"
                            ),
                        )
                    )
                else:
                    errors.append(
                        _error(
                            E_CHAIN_BROKEN,
                            position=position,
                            cert_id=ids[position],
                            reason="issuer_mismatch",
                            message=(
                                f"certificate '{ids[position]}' declares issuer "
                                f"'{child.issuer_id}', expected '{expected_parent}'"
                            ),
                        )
                    )
                path_sound = False

        terminal_id = ids[-1]
        if path_sound and terminal_id not in snapshot.trusted_roots:
            errors.append(
                _error(
                    E_ROOT_UNTRUSTED,
                    position=len(ids) - 1,
                    cert_id=terminal_id,
                    reason="root_not_trusted",
                    message=f"terminal certificate '{terminal_id}' is not a trusted root",
                )
            )
            path_sound = False

    invalidation_count_before = len(invalidations)
    if not missing_positions:
        revoked_by_id = snapshot.revoked_by_id
        for position, cert_id in enumerate(ids):
            _check_deadline(deadline_monotonic)
            if step_hook is not None:
                step_hook(chain.chain_id, position, cert_id)
            _check_deadline(deadline_monotonic)
            certificate = certificates[cert_id]
            kind = _kind(position, len(ids))
            suffix = _error_suffix(kind)
            if snapshot.observed_at_ms < certificate.valid_from_ms:
                code = {
                    "leaf": E_NOT_YET_VALID_LEAF,
                    "intermediate": E_NOT_YET_VALID_INTERMEDIATE,
                    "root": E_NOT_YET_VALID_ROOT,
                }[kind]
                error = _error(
                    code,
                    position=position,
                    cert_id=cert_id,
                    reason="not_yet_valid_at_observation",
                    message=(
                        f"certificate becomes valid at "
                        f"{certificate.valid_from_ms}ms, snapshot observed at "
                        f"{snapshot.observed_at_ms}ms"
                    ),
                )
                errors.append(error)
                invalidations.append(
                    {
                        **error,
                        "kind": kind,
                        "invalid_at_ms": snapshot.observed_at_ms,
                    }
                )
            if snapshot.observed_at_ms >= certificate.valid_to_ms:
                code = {
                    "leaf": E_EXPIRED_LEAF,
                    "intermediate": E_EXPIRED_INTERMEDIATE,
                    "root": E_EXPIRED_ROOT,
                }[kind]
                error = _error(
                    code,
                    position=position,
                    cert_id=cert_id,
                    reason="expired_at_observation",
                    message=(
                        f"certificate expired at {certificate.valid_to_ms}ms, "
                        f"snapshot observed at {snapshot.observed_at_ms}ms"
                    ),
                )
                errors.append(error)
                invalidations.append(
                    {
                        **error,
                        "kind": kind,
                        "invalid_at_ms": snapshot.observed_at_ms,
                    }
                )

            snapshot_revocation = revoked_by_id.get(cert_id)
            if snapshot_revocation is not None or certificate.revoked:
                code = {
                    "leaf": E_REVOKED_LEAF,
                    "intermediate": E_REVOKED_INTERMEDIATE,
                    "root": E_REVOKED_ROOT,
                }[kind]
                source = (
                    "both"
                    if snapshot_revocation is not None and certificate.revoked
                    else (
                        "snapshot"
                        if snapshot_revocation is not None
                        else "certificate"
                    )
                )
                revoked_at = (
                    snapshot_revocation.revoked_at_ms
                    if snapshot_revocation is not None
                    else None
                )
                error = _error(
                    code,
                    position=position,
                    cert_id=cert_id,
                    reason=f"revoked_{suffix.lower()}",
                    message=f"certificate '{cert_id}' is revoked ({source})",
                )
                errors.append(error)
                invalidations.append(
                    {
                        **error,
                        "kind": kind,
                        "revocation_source": source,
                        "revoked_at_ms": revoked_at,
                    }
                )

    structural_failure = any(
        error["code"]
        in {
            E_CHAIN_BROKEN,
            E_CHAIN_EMPTY,
            E_MISSING_CERT,
            E_MISSING_INTERMEDIATE,
            E_ROOT_UNTRUSTED,
            E_NO_SNAPSHOT,
        }
        for error in errors
    )
    if structural_failure:
        conclusion = UNTRUSTED
    elif len(invalidations) > invalidation_count_before:
        conclusion = PARTIALLY_TRUSTED
    else:
        conclusion = TRUSTED

    actual_summary = _summary(conclusion, snapshot)
    return {
        "conclusion": conclusion,
        "errors": errors,
        "invalidations": invalidations,
        "basis": basis,
        "actual_summary": actual_summary,
        "matches_expected": actual_summary == chain.expected_summary,
    }


def _occurrence_index(
    chains: Iterable[ChainDeclaration],
) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for chain in sorted(chains, key=lambda item: item.sort_key):
        length = len(chain.certificate_ids)
        for position, current_id in enumerate(chain.certificate_ids):
            index.setdefault(current_id, []).append(
                {
                    "chain_id": chain.chain_id,
                    "position": position,
                    "kind": _kind(position, length),
                }
            )
    return index


def build_revocation_propagation(
    chains: Iterable[ChainDeclaration],
    certificates: dict[str, Certificate],
    snapshot: Snapshot | None,
) -> list[dict]:
    if snapshot is None:
        return []
    chain_list = list(chains)
    occurrences_by_cert = _occurrence_index(chain_list)
    active: dict[str, dict] = {}
    for entry in snapshot.revoked_entries:
        active[entry.cert_id] = {
            "source": "snapshot",
            "revoked_at_ms": entry.revoked_at_ms,
            "reason": entry.reason,
        }
    for certificate in certificates.values():
        if not certificate.revoked:
            continue
        current = active.get(certificate.cert_id)
        if current is None:
            active[certificate.cert_id] = {
                "source": "certificate",
                "revoked_at_ms": None,
                "reason": "",
            }
        else:
            current["source"] = "both"

    propagation: list[dict] = []
    for cert_id in sorted(active):
        occurrences = occurrences_by_cert.get(cert_id, [])
        if not occurrences:
            continue
        item = {
            "cert_id": cert_id,
            "source": active[cert_id]["source"],
            "revoked_at_ms": active[cert_id]["revoked_at_ms"],
            "reason": active[cert_id]["reason"],
            "affected_chain_ids": sorted(
                {occurrence["chain_id"] for occurrence in occurrences}
            ),
            "occurrences": occurrences,
        }
        propagation.append(item)
    return propagation


def build_expiration_propagation(
    chains: Iterable[ChainDeclaration],
    certificates: dict[str, Certificate],
    snapshot: Snapshot | None,
) -> list[dict]:
    if snapshot is None:
        return []
    chain_list = list(chains)
    occurrences_by_cert = _occurrence_index(chain_list)
    propagation: list[dict] = []
    for cert_id in sorted(certificates):
        certificate = certificates[cert_id]
        if snapshot.observed_at_ms < certificate.valid_to_ms:
            continue
        occurrences = occurrences_by_cert.get(cert_id, [])
        if not occurrences:
            continue
        propagation.append(
            {
                "cert_id": cert_id,
                "valid_to_ms": certificate.valid_to_ms,
                "observed_at_ms": snapshot.observed_at_ms,
                "affected_chain_ids": sorted(
                    {occurrence["chain_id"] for occurrence in occurrences}
                ),
                "occurrences": occurrences,
            }
        )
    return propagation
