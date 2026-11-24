"""Certificate-chain construction, handshake-time validation and propagation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import codes
from .budget import Deadline
from .models import (
    Certificate,
    ChainEvaluation,
    ConnectionResult,
    Diagnostic,
    ReassembledMessage,
    TrustEntity,
    TrustView,
    digest_value,
    normalize_fingerprint,
)


_CLOCK_KEYS = ("observed_time_ms", "observed_clock_ms", "clock_ms", "timestamp_ms")


def _first(mapping: Dict[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return None
    return None


def determine_observed_clock(
    messages: Sequence[ReassembledMessage],
    records: Sequence[Any],
    connection_id: str,
) -> Tuple[Optional[int], Optional[str], List[Diagnostic]]:
    """Choose the sample-declared observation clock.  Never uses system time."""

    issues: List[Diagnostic] = []
    candidates: List[Tuple[int, str, int]] = []
    for message in messages:
        if message.payload is None:
            continue
        value = _as_int(_first(message.payload, _CLOCK_KEYS))
        if value is not None:
            candidates.append((value, f"message:{message.message_id}", message.first_seq))
    if candidates:
        distinct = sorted({c[0] for c in candidates})
        if len(distinct) > 1:
            issues.append(
                Diagnostic(
                    code=codes.E_CLOCK_CONFLICT,
                    message="messages declare different observation clocks; first handshake message wins",
                    severity="warning",
                    connection_id=connection_id,
                    message_seq=candidates[0][2],
                    detail={
                        "values": {str(value): [c[1] for c in candidates if c[0] == value] for value in distinct}
                    },
                )
            )
        chosen = candidates[0]
        for message in messages:
            if message.kind == "handshake" and message.payload:
                value = _as_int(_first(message.payload, _CLOCK_KEYS))
                if value is not None:
                    chosen = (value, f"handshake_message:{message.message_id}", message.first_seq)
                    break
        return chosen[0], chosen[1], issues
    if records:
        timestamps = [int(r.timestamp_ms) for r in records]
        return max(timestamps), "record_timestamp_max", issues
    return None, None, issues


def _parse_certificate(
    raw: Any,
    index: int,
    *,
    connection_id: str,
    message_seq: Optional[int],
    issues: List[Diagnostic],
) -> Optional[Certificate]:
    if not isinstance(raw, dict):
        issues.append(
            Diagnostic(
                code=codes.E_CERT_FIELD,
                message="certificate entry must be an object",
                connection_id=connection_id,
                message_seq=message_seq,
                detail={"certificate_index": index},
            )
        )
        return None
    subject = raw.get("subject")
    issuer = raw.get("issuer")
    if not isinstance(subject, str) or not subject.strip():
        issues.append(
            Diagnostic(
                code=codes.E_CERT_FIELD,
                message="certificate subject must be a non-empty string",
                connection_id=connection_id,
                message_seq=message_seq,
                detail={"certificate_index": index, "field": "subject"},
            )
        )
        return None
    if not isinstance(issuer, str) or not issuer.strip():
        issues.append(
            Diagnostic(
                code=codes.E_CERT_FIELD,
                message="certificate issuer must be a non-empty string",
                connection_id=connection_id,
                message_seq=message_seq,
                detail={"certificate_index": index, "field": "issuer"},
            )
        )
        return None
    fp_raw = _first(
        raw, ("public_key_fingerprint", "fingerprint", "public_key_fp", "key_fingerprint")
    )
    fingerprint = normalize_fingerprint(fp_raw)
    if fingerprint is None:
        issues.append(
            Diagnostic(
                code=codes.E_CERT_FP,
                message="certificate public key fingerprint must be sha256:<64 hex>",
                connection_id=connection_id,
                message_seq=message_seq,
                detail={"certificate_index": index, "value": fp_raw},
            )
        )
        return None
    not_before = _as_int(_first(raw, ("not_before_ms", "valid_from_ms", "not_before")))
    not_after = _as_int(_first(raw, ("not_after_ms", "valid_to_ms", "not_after")))
    if not_before is None or not_after is None:
        issues.append(
            Diagnostic(
                code=codes.E_CERT_TIME,
                message="certificate validity bounds must be integer milliseconds",
                connection_id=connection_id,
                message_seq=message_seq,
                detail={"certificate_index": index},
            )
        )
        return None
    if not_before > not_after:
        issues.append(
            Diagnostic(
                code=codes.E_CERT_TIME,
                message="certificate not_before_ms is later than not_after_ms",
                connection_id=connection_id,
                message_seq=message_seq,
                detail={"certificate_index": index},
            )
        )
        return None
    serial_raw = raw.get("serial_number", raw.get("serial", ""))
    serial = str(serial_raw) if serial_raw is not None else ""
    signature_raw = raw.get("signature", raw.get("signature_digest", ""))
    signature = str(signature_raw) if signature_raw is not None else ""
    revoked = bool(raw.get("revoked", False))
    revoked_at = _as_int(raw.get("revoked_at_ms"))
    return Certificate(
        index=index,
        subject=subject,
        issuer=issuer,
        serial=serial,
        not_before_ms=not_before,
        not_after_ms=not_after,
        public_key_fingerprint=fingerprint,
        signature=signature,
        revoked=revoked,
        revoked_at_ms=revoked_at,
        raw=dict(raw),
    )


@dataclass
class CandidateDraft:
    certificates: List[Certificate]
    message_seq: Optional[int]
    source_message_id: Optional[int] = None
    issues: List[Diagnostic] = field(default_factory=list)

    @property
    def fingerprints(self) -> List[str]:
        return [c.public_key_fingerprint for c in self.certificates]

    @property
    def fingerprint(self) -> str:
        return digest_value(self.fingerprints)[:20]


def _resolve_chain_entries(
    entries: Sequence[Any],
    *,
    pool: Dict[str, Certificate],
    message_certificates: Sequence[Certificate],
    connection_id: str,
    message_seq: Optional[int],
    issues: List[Diagnostic],
) -> Optional[List[Certificate]]:
    chain: List[Certificate] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, dict):
            cert = _parse_certificate(
                entry,
                index,
                connection_id=connection_id,
                message_seq=message_seq,
                issues=issues,
            )
        elif isinstance(entry, int) and not isinstance(entry, bool):
            cert = message_certificates[entry] if 0 <= entry < len(message_certificates) else None
            if cert is None:
                issues.append(
                    Diagnostic(
                        code=codes.E_CERT_FIELD,
                        message="candidate chain index is out of range",
                        connection_id=connection_id,
                        message_seq=message_seq,
                        detail={"index": entry},
                    )
                )
        elif isinstance(entry, str):
            fingerprint = normalize_fingerprint(entry)
            cert = pool.get(fingerprint) if fingerprint else None
            if cert is None:
                issues.append(
                    Diagnostic(
                        code=codes.E_CERT_FP,
                        message="candidate chain fingerprint is not defined by a certificate message",
                        connection_id=connection_id,
                        message_seq=message_seq,
                        detail={"fingerprint": entry},
                    )
                )
        else:
            cert = None
            issues.append(
                Diagnostic(
                    code=codes.E_CERT_FIELD,
                    message="candidate chain entry must be an object, fingerprint, or index",
                    connection_id=connection_id,
                    message_seq=message_seq,
                )
            )
        if cert is None:
            return None
        chain.append(cert)
    return chain


def extract_candidate_chains(
    messages: Sequence[ReassembledMessage], connection_id: str
) -> Tuple[List[CandidateDraft], List[Diagnostic]]:
    issues: List[Diagnostic] = []
    pool: Dict[str, Certificate] = {}
    message_certs: Dict[int, List[Certificate]] = {}

    # First pass: define the certificate pool from every certificate message.
    for message in messages:
        if message.kind != "certificate" or message.payload is None:
            continue
        raw_certs = _first(message.payload, ("certificates", "certs", "chain"))
        if raw_certs is None:
            raw_certs = []
        if not isinstance(raw_certs, list):
            issues.append(
                Diagnostic(
                    code=codes.E_CERT_FIELD,
                    message="certificate message certificates field must be a list",
                    connection_id=connection_id,
                    message_seq=message.first_seq,
                    detail={"message_id": message.message_id},
                )
            )
            raw_certs = []
        parsed: List[Certificate] = []
        for index, raw in enumerate(raw_certs):
            cert = _parse_certificate(
                raw,
                index,
                connection_id=connection_id,
                message_seq=message.first_seq,
                issues=issues,
            )
            if cert is not None:
                parsed.append(cert)
                pool.setdefault(cert.public_key_fingerprint, cert)
        message_certs[message.message_id] = parsed

    drafts: List[CandidateDraft] = []
    seen: set[Tuple[str, ...]] = set()

    def add(certificates: Optional[List[Certificate]], seq: Optional[int], message_id: Optional[int]) -> None:
        if certificates is None or not certificates:
            return
        key = tuple(c.public_key_fingerprint for c in certificates)
        if key in seen:
            return
        seen.add(key)
        drafts.append(
            CandidateDraft(
                certificates=certificates,
                message_seq=seq,
                source_message_id=message_id,
            )
        )

    # Certificate messages are themselves ordered chain candidates.
    for message in messages:
        if message.kind == "certificate" and message.payload is not None:
            certs = list(message_certs.get(message.message_id, []))
            add(certs, message.first_seq, message.message_id)
            explicit = _first(message.payload, ("candidate_chains", "chains"))
            if isinstance(explicit, list):
                for chain_index, entries in enumerate(explicit):
                    if not isinstance(entries, list):
                        issues.append(
                            Diagnostic(
                                code=codes.E_CERT_FIELD,
                                message="candidate_chains entry must be a list",
                                connection_id=connection_id,
                                message_seq=message.first_seq,
                                detail={"chain_index": chain_index},
                            )
                        )
                        continue
                    chain = _resolve_chain_entries(
                        entries,
                        pool=pool,
                        message_certificates=certs,
                        connection_id=connection_id,
                        message_seq=message.first_seq,
                        issues=issues,
                    )
                    add(chain, message.first_seq, message.message_id)

    # Handshake messages may carry explicit fingerprint-based candidates.
    for message in messages:
        if message.kind != "handshake" or message.payload is None:
            continue
        explicit = _first(message.payload, ("candidate_chains", "chains"))
        if isinstance(explicit, list):
            for chain_index, entries in enumerate(explicit):
                if not isinstance(entries, list):
                    issues.append(
                        Diagnostic(
                            code=codes.E_CERT_FIELD,
                            message="candidate_chains entry must be a list",
                            connection_id=connection_id,
                            message_seq=message.first_seq,
                            detail={"chain_index": chain_index},
                        )
                    )
                    continue
                chain = _resolve_chain_entries(
                    entries,
                    pool=pool,
                    message_certificates=[],
                    connection_id=connection_id,
                    message_seq=message.first_seq,
                    issues=issues,
                )
                add(chain, message.first_seq, message.message_id)

    return drafts, issues


def _certificate_revoked(cert: Certificate, observed_ms: Optional[int]) -> bool:
    if not cert.revoked:
        return False
    if cert.revoked_at_ms is None or observed_ms is None:
        return True
    return cert.revoked_at_ms <= observed_ms


def _anchor_revoked(entity: TrustEntity, observed_ms: Optional[int]) -> bool:
    return entity.effective_revoked(observed_ms)


def _role(index: int, length: int) -> str:
    if index == 0:
        return "leaf"
    if index == length - 1:
        return "root"
    return "intermediate"


_PRIMARY_PRIORITY = [
    codes.E_CHAIN_EMPTY,
    codes.E_CHAIN_BROKEN,
    codes.E_REVOKED_LEAF,
    codes.E_REVOKED_INTERMEDIATE,
    codes.E_ROOT_REVOKED,
    codes.E_CERT_EXPIRED,
    codes.E_ROOT_UNTRUSTED,
    codes.E_ROOT_INACTIVE,
    codes.E_OBSERVED_CLOCK,
    codes.E_CHAIN_LOOP,
]


def evaluate_chain(
    draft: CandidateDraft,
    trust_view: TrustView,
    observed_ms: Optional[int],
    connection_id: str,
    deadline: Optional[Deadline] = None,
) -> ChainEvaluation:
    issues: List[Diagnostic] = list(draft.issues)
    certificates = draft.certificates
    fingerprints = [c.public_key_fingerprint for c in certificates]
    root = certificates[-1] if certificates else None
    evaluation = ChainEvaluation(
        candidate_id=f"chain-{draft.fingerprint}",
        certificates=certificates,
        fingerprints=fingerprints,
        root_fingerprint=root.public_key_fingerprint if root else None,
        message_seq=draft.message_seq,
    )
    if not certificates:
        issues.append(
            Diagnostic(
                code=codes.E_CHAIN_EMPTY,
                message="certificate chain is empty",
                connection_id=connection_id,
                message_seq=draft.message_seq,
            )
        )
        evaluation.conclusion = "empty_chain"
    else:
        for index in range(len(certificates) - 1):
            if deadline is not None:
                deadline.check("chain_link", draft.message_seq)
            if certificates[index].issuer != certificates[index + 1].subject:
                issues.append(
                    Diagnostic(
                        code=codes.E_CHAIN_BROKEN,
                        message="certificate issuer does not match the next certificate subject",
                        connection_id=connection_id,
                        message_seq=draft.message_seq,
                        detail={
                            "certificate_index": index,
                            "issuer": certificates[index].issuer,
                            "next_subject": certificates[index + 1].subject,
                        },
                    )
                )
        if len(set(fingerprints)) != len(fingerprints):
            issues.append(
                Diagnostic(
                    code=codes.E_CHAIN_LOOP,
                    message="certificate chain repeats a public-key fingerprint",
                    connection_id=connection_id,
                    message_seq=draft.message_seq,
                    detail={"fingerprints": fingerprints},
                )
            )
        if observed_ms is None:
            issues.append(
                Diagnostic(
                    code=codes.E_OBSERVED_CLOCK,
                    message="sample does not declare an observation clock",
                    connection_id=connection_id,
                    message_seq=draft.message_seq,
                )
            )
        else:
            for index, cert in enumerate(certificates):
                if deadline is not None:
                    deadline.check("certificate_validity", draft.message_seq)
                if observed_ms < cert.not_before_ms or observed_ms > cert.not_after_ms:
                    reason = (
                        "not_yet_valid" if observed_ms < cert.not_before_ms else "expired"
                    )
                    issues.append(
                        Diagnostic(
                            code=codes.E_CERT_EXPIRED,
                            message="certificate is outside its validity window at the declared observation clock",
                            connection_id=connection_id,
                            message_seq=draft.message_seq,
                            detail={
                                "certificate_index": index,
                                "role": _role(index, len(certificates)),
                                "reason": reason,
                                "observed_time_ms": observed_ms,
                                "not_before_ms": cert.not_before_ms,
                                "not_after_ms": cert.not_after_ms,
                            },
                        )
                    )
                    evaluation.expired_roles.append(
                        {
                            "certificate_index": index,
                            "role": _role(index, len(certificates)),
                            "reason": reason,
                            "fingerprint": cert.public_key_fingerprint,
                        }
                    )

        assert root is not None
        entity = trust_view.entities.get(root.public_key_fingerprint)
        root_trusted = False
        if entity is None:
            evaluation.root_status = "missing"
            issues.append(
                Diagnostic(
                    code=codes.E_ROOT_UNTRUSTED,
                    message="root public-key fingerprint is not present in the merged trust view",
                    connection_id=connection_id,
                    message_seq=draft.message_seq,
                    detail={"fingerprint": root.public_key_fingerprint},
                )
            )
        else:
            evaluation.root_alias = entity.canonical_alias
            evaluation.root_generation = entity.generation
            evaluation.root_effective_ms = entity.effective_ms
            if entity.tombstone:
                evaluation.root_status = "tombstoned"
                issues.append(
                    Diagnostic(
                        code=codes.E_ROOT_UNTRUSTED,
                        message="root trust anchor has been tombstoned",
                        connection_id=connection_id,
                        message_seq=draft.message_seq,
                        detail={"fingerprint": root.public_key_fingerprint},
                    )
                )
            elif not entity.enabled:
                evaluation.root_status = "disabled"
                issues.append(
                    Diagnostic(
                        code=codes.E_ROOT_UNTRUSTED,
                        message="root trust anchor is disabled",
                        connection_id=connection_id,
                        message_seq=draft.message_seq,
                        detail={"fingerprint": root.public_key_fingerprint},
                    )
                )
            elif _anchor_revoked(entity, observed_ms):
                evaluation.root_status = "revoked"
                issues.append(
                    Diagnostic(
                        code=codes.E_ROOT_REVOKED,
                        message="root trust anchor is revoked",
                        connection_id=connection_id,
                        message_seq=draft.message_seq,
                        detail={"fingerprint": root.public_key_fingerprint},
                    )
                )
            elif observed_ms is not None and entity.effective_ms > observed_ms:
                evaluation.root_status = "not_yet_effective"
                issues.append(
                    Diagnostic(
                        code=codes.E_ROOT_INACTIVE,
                        message="root trust anchor is not effective at the declared observation clock",
                        connection_id=connection_id,
                        message_seq=draft.message_seq,
                        detail={
                            "fingerprint": root.public_key_fingerprint,
                            "effective_ms": entity.effective_ms,
                            "observed_time_ms": observed_ms,
                        },
                    )
                )
            else:
                evaluation.root_status = "trusted"
                root_trusted = True

        # Revocation may come from the certificate itself or from the merged
        # trust-anchor view.  The effective revocation time is honoured.
        for index, cert in enumerate(certificates):
            if deadline is not None:
                deadline.check("revocation", draft.message_seq)
            entity_for_cert = trust_view.entities.get(cert.public_key_fingerprint)
            by_cert = _certificate_revoked(cert, observed_ms)
            by_anchor = (
                _anchor_revoked(entity_for_cert, observed_ms)
                if entity_for_cert is not None
                else False
            )
            if not by_cert and not by_anchor:
                continue
            role = _role(index, len(certificates))
            if role == "root":
                code = codes.E_ROOT_REVOKED
            elif role == "leaf":
                code = codes.E_REVOKED_LEAF
            else:
                code = codes.E_REVOKED_INTERMEDIATE
            sources = []
            if by_cert:
                sources.append("certificate")
            if by_anchor:
                sources.append("trust_anchor")
            # The root-anchor check above may already have emitted
            # E_ROOT_REVOKED.  Keep the propagation role but avoid duplicate
            # issue entries for the same certificate.
            already_emitted = any(
                issue.code == code
                and issue.detail.get("fingerprint") == cert.public_key_fingerprint
                for issue in issues
            )
            if not already_emitted:
                issues.append(
                    Diagnostic(
                        code=code,
                        message=f"{role} certificate is revoked",
                        connection_id=connection_id,
                        message_seq=draft.message_seq,
                        detail={
                            "certificate_index": index,
                            "role": role,
                            "fingerprint": cert.public_key_fingerprint,
                            "revocation_sources": sources,
                        },
                    )
                )
            if not any(
                entry.get("fingerprint") == cert.public_key_fingerprint
                and entry.get("role") == role
                for entry in evaluation.revoked_roles
            ):
                evaluation.revoked_roles.append(
                    {
                        "certificate_index": index,
                        "role": role,
                        "fingerprint": cert.public_key_fingerprint,
                        "code": code,
                        "sources": sources,
                    }
                )
        if root_trusted and not any(
            i.code in (codes.E_ROOT_REVOKED,) for i in issues
        ):
            evaluation.root_status = "trusted"

    present = {i.code for i in issues if i.severity != "warning"}
    for code in _PRIMARY_PRIORITY:
        if code in present:
            evaluation.primary_code = code
            break
    evaluation.valid = not any(i.severity == "error" for i in issues)
    evaluation.trusted = evaluation.valid and evaluation.root_status == "trusted"
    if evaluation.trusted:
        evaluation.conclusion = "trusted"
    elif any(r["role"] == "leaf" for r in evaluation.revoked_roles):
        evaluation.conclusion = "revoked_leaf"
    elif any(r["role"] == "intermediate" for r in evaluation.revoked_roles):
        evaluation.conclusion = "revoked_intermediate"
    elif any(r["role"] == "root" for r in evaluation.revoked_roles):
        evaluation.conclusion = "root_revoked"
    elif any(i.code == codes.E_CERT_EXPIRED for i in issues):
        evaluation.conclusion = "expired"
    elif any(i.code == codes.E_CHAIN_BROKEN for i in issues):
        evaluation.conclusion = "broken_chain"
    elif any(i.code == codes.E_CHAIN_EMPTY for i in issues):
        evaluation.conclusion = "empty_chain"
    elif evaluation.root_status in ("missing", "tombstoned", "disabled"):
        evaluation.conclusion = "untrusted_root"
    elif evaluation.root_status == "not_yet_effective":
        evaluation.conclusion = "root_inactive"
    elif evaluation.root_status == "revoked":
        evaluation.conclusion = "root_revoked"
    elif any(i.code == codes.E_CHAIN_LOOP for i in issues):
        evaluation.conclusion = "loop"
    else:
        evaluation.conclusion = "untrusted"
    evaluation.issues = issues
    return evaluation


def _rank_key(evaluation: ChainEvaluation) -> Tuple[Any, ...]:
    return (
        0 if evaluation.trusted else 1,
        0 if not evaluation.revoked_roles else 1,
        0 if not evaluation.expired_roles else 1,
        0 if evaluation.valid else 1,
        -evaluation.root_generation,
        -evaluation.root_effective_ms,
        -len(evaluation.certificates),
        tuple(evaluation.fingerprints),
        evaluation.candidate_id,
    )


def evaluate_messages(
    messages: Sequence[ReassembledMessage],
    records: Sequence[Any],
    trust_view: TrustView,
    connection_id: str,
    deadline: Optional[Deadline] = None,
) -> Tuple[
    Optional[int],
    Optional[str],
    List[ChainEvaluation],
    Optional[ChainEvaluation],
    List[Diagnostic],
    str,
    Optional[str],
]:
    observed_ms, clock_source, clock_issues = determine_observed_clock(
        messages, records, connection_id
    )
    drafts, extraction_issues = extract_candidate_chains(messages, connection_id)
    issues: List[Diagnostic] = list(clock_issues) + list(extraction_issues)
    evaluations: List[ChainEvaluation] = []
    for draft in drafts:
        if deadline is not None:
            deadline.check("evaluate_chain", draft.message_seq)
        evaluations.append(
            evaluate_chain(draft, trust_view, observed_ms, connection_id, deadline=deadline)
        )
    evaluations.sort(key=_rank_key)
    for rank, evaluation in enumerate(evaluations):
        evaluation.rank = rank

    selected = evaluations[0] if evaluations else None
    if selected is None:
        issues.append(
            Diagnostic(
                code=codes.E_CHAIN_EMPTY,
                message="connection yielded no certificate-chain candidate",
                connection_id=connection_id,
                message_seq=(
                    min((m.first_seq for m in messages), default=None)
                    if messages
                    else None
                ),
            )
        )
        return (
            observed_ms,
            clock_source,
            evaluations,
            None,
            issues,
            "empty_chain",
            codes.E_CHAIN_EMPTY,
        )

    for evaluation in evaluations:
        if evaluation is selected:
            continue
        for issue in evaluation.issues:
            issue.severity = "warning"
    issues.extend(selected.issues)
    for evaluation in evaluations:
        if evaluation is not selected:
            issues.extend(evaluation.issues)
    if selected.trusted:
        return (
            observed_ms,
            clock_source,
            evaluations,
            selected,
            issues,
            "trusted",
            None,
        )
    return (
        observed_ms,
        clock_source,
        evaluations,
        selected,
        issues,
        selected.conclusion,
        selected.primary_code,
    )


def compute_propagation(
    results: Sequence[ConnectionResult],
) -> List[Dict[str, Any]]:
    """Compute deterministic revocation/expiry propagation across connections."""

    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for result in sorted(results, key=lambda r: r.connection_id):
        chain = result.selected_chain
        if chain is None or not chain.certificates:
            continue
        observed = result.observed_time_ms
        for index, cert in enumerate(chain.certificates):
            role = _role(index, len(chain.certificates))
            revoked_sources: List[str] = []
            if _certificate_revoked(cert, observed):
                revoked_sources.append("certificate")
            # Trust-view revocation is available in the chain issue detail only
            # indirectly; the selected evaluation carries the authoritative
            # role list.  We therefore use revoked_roles below for grouping.
            for entry in chain.revoked_roles:
                if entry.get("fingerprint") == cert.public_key_fingerprint:
                    revoked_sources = sorted(
                        set(revoked_sources) | set(entry.get("sources", []))
                    )
            expired = any(
                entry.get("fingerprint") == cert.public_key_fingerprint
                for entry in chain.expired_roles
            )
            if revoked_sources:
                code = (
                    codes.E_ROOT_REVOKED
                    if role == "root"
                    else codes.E_REVOKED_LEAF
                    if role == "leaf"
                    else codes.E_REVOKED_INTERMEDIATE
                )
                key = (cert.public_key_fingerprint, role, "revoked")
                group = groups.setdefault(
                    key,
                    {
                        "fingerprint": cert.public_key_fingerprint,
                        "role": role,
                        "reason": "revoked",
                        "code": code,
                        "sources": set(),
                        "affected_connections": set(),
                    },
                )
                group["sources"].update(revoked_sources)
                group["affected_connections"].add(result.connection_id)
            if expired:
                key = (cert.public_key_fingerprint, role, "expired")
                group = groups.setdefault(
                    key,
                    {
                        "fingerprint": cert.public_key_fingerprint,
                        "role": role,
                        "reason": "expired",
                        "code": codes.E_CERT_EXPIRED,
                        "sources": {"certificate_validity"},
                        "affected_connections": set(),
                    },
                )
                group["affected_connections"].add(result.connection_id)

    output: List[Dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        output.append(
            {
                "fingerprint": group["fingerprint"],
                "role": group["role"],
                "reason": group["reason"],
                "code": group["code"],
                "sources": sorted(group["sources"]),
                "affected_connections": sorted(group["affected_connections"]),
            }
        )
    return output


def attach_propagation(
    results: Sequence[ConnectionResult], propagation: Sequence[Dict[str, Any]]
) -> None:
    for result in results:
        result.revocation_propagation = [
            entry
            for entry in propagation
            if result.connection_id in entry.get("affected_connections", [])
        ]
        result.compute_digest()


def own_propagation(evaluation: Optional[ChainEvaluation]) -> List[Dict[str, Any]]:
    """Stable per-connection propagation entries (no cross-connection lists)."""

    if evaluation is None:
        return []
    output: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    for entry in evaluation.revoked_roles:
        key = (
            str(entry.get("fingerprint")),
            str(entry.get("role")),
            "revoked",
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(
            {
                "fingerprint": entry.get("fingerprint"),
                "role": entry.get("role"),
                "reason": "revoked",
                "code": entry.get("code"),
                "sources": sorted(entry.get("sources", [])),
            }
        )
    for entry in evaluation.expired_roles:
        key = (
            str(entry.get("fingerprint")),
            str(entry.get("role")),
            "expired",
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(
            {
                "fingerprint": entry.get("fingerprint"),
                "role": entry.get("role"),
                "reason": "expired",
                "code": codes.E_CERT_EXPIRED,
                "sources": ["certificate_validity"],
                "expiry_reason": entry.get("reason"),
            }
        )
    output.sort(
        key=lambda x: (
            str(x.get("fingerprint")),
            str(x.get("role")),
            str(x.get("reason")),
        )
    )
    return output
