"""Cross-signed certificate path enumeration and validation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .canon import sha256_obj
from .errors import (
    E_CERT_EXPIRED,
    E_CERT_NOT_YET_VALID,
    E_CHAIN_CYCLE,
    E_INTERMEDIATE_REVOKED,
    E_ISSUER_BREAK,
    E_LEAF_REVOKED,
    E_MISSING_INTERMEDIATE,
    E_PATH_LENGTH,
    E_ROOT_REVOKED,
    E_ROOT_UNTRUSTED,
    E_SIGNATURE_MISMATCH,
    E_TRUST_CONFLICT,
    W_PATH_ENUM_CAPPED,
    W_PATH_ENUM_TRUNCATED,
)
from .model import (
    CandidatePath,
    Certificate,
    ChainDeclaration,
    ChainResult,
    TrustView,
)


MAX_DEPTH = 32
HARD_ENUMERATION_CAP = 10_000


def chain_key(chain: ChainDeclaration) -> str:
    return "chain:" + sha256_obj(
        {
            "chain_id": chain.chain_id,
            "certificate_ids": list(chain.certificate_ids),
        }
    )


def _append_code(codes: list[str], code: str) -> None:
    if code not in codes:
        codes.append(code)


def _role(index: int, count: int) -> str:
    if index == 0:
        return "leaf"
    if index == count - 1:
        return "root"
    return "intermediate"


def _unique_details(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in details:
        key = sha256_obj(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _invalidation_entries(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for detail in details:
        code = detail.get("code")
        if code not in {
            E_LEAF_REVOKED,
            E_INTERMEDIATE_REVOKED,
            E_ROOT_REVOKED,
            E_CERT_EXPIRED,
            E_CERT_NOT_YET_VALID,
        }:
            continue
        kind = (
            "revocation"
            if "REVOKED" in code
            else "expiry"
            if code == E_CERT_EXPIRED
            else "not_yet_valid"
        )
        result.append(
            {
                "code": code,
                "kind": kind,
                "certificate_id": detail.get("certificate_id"),
                "role": detail.get("role"),
            }
        )
    return _unique_details(result)


def _verify_path(
    path: list[Certificate],
    view: TrustView,
) -> CandidatePath:
    codes: list[str] = []
    details: list[dict[str, Any]] = []
    count = len(path)
    now = view.observation_clock_ms

    for index, cert in enumerate(path):
        role = _role(index, count)
        if now < cert.not_before_ms:
            _append_code(codes, E_CERT_NOT_YET_VALID)
            details.append(
                {
                    "code": E_CERT_NOT_YET_VALID,
                    "certificate_id": cert.certificate_id,
                    "role": role,
                    "field": "not_before_ms",
                    "observation_clock_ms": now,
                    "value": cert.not_before_ms,
                }
            )
        if now >= cert.not_after_ms:
            _append_code(codes, E_CERT_EXPIRED)
            details.append(
                {
                    "code": E_CERT_EXPIRED,
                    "certificate_id": cert.certificate_id,
                    "role": role,
                    "field": "not_after_ms",
                    "observation_clock_ms": now,
                    "value": cert.not_after_ms,
                }
            )
        if cert.revoked and role != "root":
            code = E_LEAF_REVOKED if role == "leaf" else E_INTERMEDIATE_REVOKED
            _append_code(codes, code)
            details.append(
                {
                    "code": code,
                    "certificate_id": cert.certificate_id,
                    "role": role,
                    "field": "revoked",
                }
            )
        elif cert.revoked:
            _append_code(codes, E_ROOT_REVOKED)
            details.append(
                {
                    "code": E_ROOT_REVOKED,
                    "certificate_id": cert.certificate_id,
                    "role": role,
                    "field": "revoked",
                }
            )

    for index in range(count - 1):
        child = path[index]
        issuer = path[index + 1]
        if child.issuer_id != issuer.subject_id:
            _append_code(codes, E_ISSUER_BREAK)
            details.append(
                {
                    "code": E_ISSUER_BREAK,
                    "child_certificate_id": child.certificate_id,
                    "issuer_certificate_id": issuer.certificate_id,
                    "child_issuer_id": child.issuer_id,
                    "issuer_subject_id": issuer.subject_id,
                    "edge_index": index,
                }
            )
        if child.signature_digest != issuer.public_key_fingerprint:
            _append_code(codes, E_SIGNATURE_MISMATCH)
            details.append(
                {
                    "code": E_SIGNATURE_MISMATCH,
                    "child_certificate_id": child.certificate_id,
                    "issuer_certificate_id": issuer.certificate_id,
                    "signature_digest": child.signature_digest,
                    "issuer_public_key_fingerprint": issuer.public_key_fingerprint,
                    "edge_index": index,
                }
            )

    root_cert = path[-1]
    intermediate_count = max(0, count - 2)
    anchor = view.anchors.get(root_cert.public_key_fingerprint)
    pending = False
    if root_cert.subject_id == root_cert.issuer_id:
        if root_cert.signature_digest != root_cert.public_key_fingerprint:
            _append_code(codes, E_SIGNATURE_MISMATCH)
            details.append(
                {
                    "code": E_SIGNATURE_MISMATCH,
                    "child_certificate_id": root_cert.certificate_id,
                    "issuer_certificate_id": root_cert.certificate_id,
                    "signature_digest": root_cert.signature_digest,
                    "issuer_public_key_fingerprint": root_cert.public_key_fingerprint,
                    "edge_index": count - 1,
                    "self_issued_root": True,
                }
            )
    if anchor is None:
        _append_code(codes, E_ROOT_UNTRUSTED)
        details.append(
            {
                "code": E_ROOT_UNTRUSTED,
                "certificate_id": root_cert.certificate_id,
                "root_fingerprint": root_cert.public_key_fingerprint,
                "reason": "root fingerprint is absent from the merged trust view",
            }
        )
    else:
        pending = anchor.pending_confirmation
        if anchor.revoked:
            _append_code(codes, E_ROOT_REVOKED)
            details.append(
                {
                    "code": E_ROOT_REVOKED,
                    "certificate_id": root_cert.certificate_id,
                    "root_fingerprint": root_cert.public_key_fingerprint,
                    "source": anchor.source,
                    "generation": anchor.generation,
                }
            )
        if anchor.tombstone or not anchor.trusted:
            _append_code(codes, E_ROOT_UNTRUSTED)
            details.append(
                {
                    "code": E_ROOT_UNTRUSTED,
                    "certificate_id": root_cert.certificate_id,
                    "root_fingerprint": root_cert.public_key_fingerprint,
                    "source": anchor.source,
                    "generation": anchor.generation,
                    "reason": (
                        "tombstone record is authoritative"
                        if anchor.tombstone
                        else "authoritative trust state is not trusted"
                    ),
                    "pending_confirmation": pending,
                }
            )
        if (
            anchor.path_length is not None
            and intermediate_count > anchor.path_length
        ):
            _append_code(codes, E_PATH_LENGTH)
            details.append(
                {
                    "code": E_PATH_LENGTH,
                    "root_fingerprint": root_cert.public_key_fingerprint,
                    "intermediate_count": intermediate_count,
                    "path_length_limit": anchor.path_length,
                    "source": anchor.source,
                    "generation": anchor.generation,
                }
            )

    failed = bool(codes)
    if pending:
        _append_code(codes, E_TRUST_CONFLICT)
        details.append(
            {
                "code": E_TRUST_CONFLICT,
                "root_fingerprint": root_cert.public_key_fingerprint,
                "reason": "merged trust view requires confirmation",
            }
        )
    status = "invalid" if failed else "pending" if pending else "trusted"
    return CandidatePath(
        certificate_ids=[item.certificate_id for item in path],
        fingerprint_sequence=[item.public_key_fingerprint for item in path],
        intermediate_count=intermediate_count,
        root_fingerprint=root_cert.public_key_fingerprint,
        root_source=anchor.source if anchor is not None else None,
        root_trust_generation=anchor.generation if anchor is not None else None,
        status=status,
        valid=not failed,
        pending_confirmation=pending,
        business_codes=codes,
        details=_unique_details(details),
    )


def adjudicate_chain(
    chain: ChainDeclaration,
    certificates: list[Certificate],
    view: TrustView,
) -> ChainResult:
    cert_by_id = {item.certificate_id: item for item in certificates}
    by_subject: dict[str, list[Certificate]] = defaultdict(list)
    for cert in certificates:
        by_subject[cert.subject_id].append(cert)
    for values in by_subject.values():
        values.sort(key=lambda item: (item.public_key_fingerprint, item.certificate_id))

    chain_codes: list[str] = []
    enumeration_gaps: list[dict[str, Any]] = []
    missing_ids = [
        cert_id
        for cert_id in chain.certificate_ids
        if cert_id not in cert_by_id
    ]
    if missing_ids:
        _append_code(chain_codes, E_MISSING_INTERMEDIATE)
        enumeration_gaps.append(
            {
                "code": E_MISSING_INTERMEDIATE,
                "reason": "certificate IDs in the chain declaration are absent",
                "missing_certificate_ids": sorted(missing_ids),
            }
        )

    for index in range(len(chain.certificate_ids) - 1):
        child = cert_by_id.get(chain.certificate_ids[index])
        issuer = cert_by_id.get(chain.certificate_ids[index + 1])
        if child is None or issuer is None:
            continue
        if child.issuer_id != issuer.subject_id:
            _append_code(chain_codes, E_ISSUER_BREAK)
            enumeration_gaps.append(
                {
                    "code": E_ISSUER_BREAK,
                    "reason": "declared adjacent certificates do not connect",
                    "edge_index": index,
                    "child_certificate_id": child.certificate_id,
                    "issuer_certificate_id": issuer.certificate_id,
                    "child_issuer_id": child.issuer_id,
                    "issuer_subject_id": issuer.subject_id,
                }
            )

    declared_last = cert_by_id.get(chain.certificate_ids[-1])
    if declared_last is not None:
        last_is_anchor = declared_last.public_key_fingerprint in view.anchors
        last_is_self_issued = declared_last.subject_id == declared_last.issuer_id
        if not last_is_anchor and not last_is_self_issued:
            _append_code(chain_codes, E_MISSING_INTERMEDIATE)
            enumeration_gaps.append(
                {
                    "code": E_MISSING_INTERMEDIATE,
                    "reason": (
                        "declared sequence does not end at a trust anchor "
                        "or self-issued root"
                    ),
                    "declared_last_certificate_id": declared_last.certificate_id,
                    "declared_last_fingerprint": (
                        declared_last.public_key_fingerprint
                    ),
                }
            )

    structural: dict[tuple[str, ...], list[Certificate]] = {}
    enumerated = 0
    capped = False

    def remember(path: list[Certificate]) -> None:
        key = tuple(item.certificate_id for item in path)
        previous = structural.get(key)
        if previous is None:
            structural[key] = list(path)

    def explore(path: list[Certificate]) -> None:
        nonlocal enumerated, capped
        if capped:
            return
        enumerated += 1
        if enumerated > HARD_ENUMERATION_CAP:
            capped = True
            return
        current = path[-1]
        is_anchor = current.public_key_fingerprint in view.anchors
        is_self_issued = current.subject_id == current.issuer_id
        if is_anchor or is_self_issued:
            remember(path)
            return
        parents = by_subject.get(current.issuer_id, [])
        if not parents:
            enumeration_gaps.append(
                {
                    "code": E_MISSING_INTERMEDIATE,
                    "reason": "no certificate exists for the required issuer subject",
                    "required_subject_id": current.issuer_id,
                    "from_certificate_id": current.certificate_id,
                    "prefix_fingerprints": [
                        item.public_key_fingerprint for item in path
                    ],
                }
            )
            return
        for parent in parents:
            if any(
                item.certificate_id == parent.certificate_id for item in path
            ):
                _append_code(chain_codes, E_CHAIN_CYCLE)
                enumeration_gaps.append(
                    {
                        "code": E_CHAIN_CYCLE,
                        "reason": "certificate ID repeats along an issuer edge",
                        "certificate_id": parent.certificate_id,
                    }
                )
                continue
            if any(item.subject_id == parent.subject_id for item in path):
                _append_code(chain_codes, E_CHAIN_CYCLE)
                enumeration_gaps.append(
                    {
                        "code": E_CHAIN_CYCLE,
                        "reason": "subject repeats along an issuer edge",
                        "subject_id": parent.subject_id,
                    }
                )
                continue
            if len(path) >= MAX_DEPTH:
                _append_code(chain_codes, E_CHAIN_CYCLE)
                enumeration_gaps.append(
                    {
                        "code": E_CHAIN_CYCLE,
                        "reason": "maximum path depth exceeded",
                        "certificate_id": parent.certificate_id,
                    }
                )
                continue
            explore(path + [parent])

    leaf = cert_by_id.get(chain.leaf_id)
    if leaf is not None:
        explore([leaf])
    else:
        enumeration_gaps.append(
            {
                "code": E_MISSING_INTERMEDIATE,
                "reason": "leaf certificate is absent",
                "missing_certificate_ids": [chain.leaf_id],
                "required_subject_id": None,
                "from_certificate_id": None,
                "prefix_fingerprints": [],
            }
        )

    all_candidates = [
        _verify_path(path, view)
        for path in sorted(
            structural.values(),
            key=lambda values: (
                tuple(item.public_key_fingerprint for item in values),
                tuple(item.certificate_id for item in values),
            ),
        )
    ]
    shown = all_candidates[: chain.enumeration_limit]
    hidden = all_candidates[chain.enumeration_limit :]
    truncated = bool(hidden)
    truncated_has_valid = any(item.valid for item in hidden)

    usable = [item for item in all_candidates if item.valid]
    usable.sort(
        key=lambda item: (
            -(item.root_trust_generation or -1),
            tuple(item.fingerprint_sequence),
            tuple(item.certificate_ids),
        )
    )
    selected = usable[0] if usable else None

    business_codes = list(chain_codes)
    for candidate in all_candidates:
        for code in candidate.business_codes:
            _append_code(business_codes, code)
    for gap in enumeration_gaps:
        _append_code(business_codes, gap["code"])
    if truncated:
        _append_code(business_codes, W_PATH_ENUM_TRUNCATED)
    if capped:
        _append_code(business_codes, W_PATH_ENUM_CAPPED)

    if chain_codes:
        conclusion = "untrusted"
        selected = None
    elif selected is None:
        conclusion = "untrusted"
    elif selected.pending_confirmation:
        conclusion = "partial"
    else:
        conclusion = "trusted"

    invalidations: list[dict[str, Any]] = []
    for candidate in all_candidates:
        invalidations.extend(_invalidation_entries(candidate.details))
    invalidations = _unique_details(invalidations)
    invalidations.sort(
        key=lambda item: (
            item.get("certificate_id") or "",
            item.get("code") or "",
            item.get("role") or "",
        )
    )
    remaining = sorted(
        (
            candidate.fingerprint_sequence
            for candidate in all_candidates
            if candidate.valid
        )
    )
    enumeration_gaps = _unique_details(enumeration_gaps)
    enumeration_gaps.sort(
        key=lambda item: (
            item.get("code") or "",
            sha256_obj(item),
        )
    )

    return ChainResult(
        chain_key=chain_key(chain),
        chain_id=chain.chain_id,
        declaration=list(chain.certificate_ids),
        conclusion=conclusion,
        selected_path=selected,
        candidates=shown,
        candidates_total=len(all_candidates),
        truncated=truncated,
        truncated_count=len(hidden),
        truncated_has_valid=truncated_has_valid,
        business_codes=business_codes,
        invalidations=invalidations,
        remaining_available_paths=remaining,
        enumeration_gaps=enumeration_gaps,
    )


def aggregate_invalidation_propagation(
    results: list[ChainResult],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for result in results:
        for item in result.invalidations:
            key = (
                str(item.get("code") or ""),
                str(item.get("kind") or ""),
                str(item.get("certificate_id") or ""),
                str(item.get("role") or ""),
            )
            groups[key].add(result.chain_id)
    output = []
    for (code, kind, certificate_id, role), chains in sorted(groups.items()):
        output.append(
            {
                "code": code,
                "kind": kind,
                "certificate_id": certificate_id,
                "role": role,
                "affected_chains": sorted(chains),
            }
        )
    return output
