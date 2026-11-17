"""Trust-path validation and invalidation (revocation/expiry) propagation.

Validation is a pure function of (claim, certs, effective snapshot); it
never reads the system clock. The observation time declared by the
snapshot is the single source of truth for expiry decisions.
"""

from __future__ import annotations

from . import errors
from .models import Conclusion, ValidationError

TRUSTED = "TRUSTED"
UNTRUSTED = "UNTRUSTED"
PARTIAL = "PARTIAL"


def _err(code, position, message):
    return ValidationError(code, position, message)


def _structural_errors(claim, certs, snapshot):
    """Check path structure leaf -> root; returns list[ValidationError]."""
    chain = claim.chain
    errs = []
    if len(chain) == 0:
        errs.append(_err(errors.E_CHAIN_EMPTY, 0, "chain length is zero"))
        return errs
    for i, cid in enumerate(chain):
        if cid not in certs:
            errs.append(_err(errors.E_CHAIN_REF_MISSING, i,
                             f"certificate {cid!r} is not described"))
    if errs:
        return errs
    for i in range(len(chain) - 1):
        cur = certs[chain[i]]
        nxt = chain[i + 1]
        if cur.is_self_issued:
            errs.append(_err(errors.E_ROOT_MIDCHAIN, i,
                             f"self-issued certificate {cur.cert_id!r} appears "
                             f"mid-chain; nothing may follow a root"))
        elif cur.issuer != nxt:
            errs.append(_err(errors.E_CHAIN_INTERMEDIATE_MISSING, i,
                             f"issuer {cur.issuer!r} of {cur.cert_id!r} is not "
                             f"the next chain element {nxt!r}; an intermediate "
                             f"certificate is missing"))
    if errs:
        return errs
    terminal = certs[chain[-1]]
    last = len(chain) - 1
    if terminal.is_self_issued:
        if terminal.cert_id not in snapshot.trusted_roots:
            errs.append(_err(errors.E_ROOT_UNTRUSTED, last,
                             f"root {terminal.cert_id!r} is not in the trusted "
                             f"set of snapshot v{snapshot.snapshot_version}"))
    else:
        if terminal.issuer in snapshot.trusted_roots:
            errs.append(_err(errors.E_ROOT_MISMATCH, last,
                             f"chain ends at {terminal.cert_id!r} but its issuer "
                             f"{terminal.issuer!r} is the trusted root; the root "
                             f"itself is missing from the chain"))
        else:
            errs.append(_err(errors.E_CHAIN_INTERMEDIATE_MISSING, last,
                             f"chain does not reach a root: issuer "
                             f"{terminal.issuer!r} of terminal certificate "
                             f"{terminal.cert_id!r} is absent"))
    return errs


def _invalidation_errors(claim, certs, snapshot):
    """Revocation & expiry propagation along the chain."""
    errs = []
    if snapshot.observed_at_ms is None:
        errs.append(_err(errors.E_SNAPSHOT_OBS_TIME, None,
                         "snapshot declares no observation time; expiry cannot "
                         "be decided"))
        return errs
    now = snapshot.observed_at_ms
    revocation_map = snapshot.revocation_map()
    last = len(claim.chain) - 1
    for i, cid in enumerate(claim.chain):
        cert = certs[cid]
        if now < cert.not_before_ms:
            errs.append(_err(errors.E_NOT_YET_VALID, i,
                             f"certificate {cid!r} is not valid before "
                             f"{cert.not_before_ms} (observed at {now})"))
        elif now >= cert.not_after_ms:
            errs.append(_err(errors.E_EXPIRED, i,
                             f"certificate {cid!r} expired at "
                             f"{cert.not_after_ms} (observed at {now})"))
        revoked_at = revocation_map.get(cid)
        if cert.revoked or (revoked_at is not None and revoked_at <= now):
            if i == 0:
                code, role = errors.E_REVOKED_LEAF, "leaf"
            elif i == last:
                code, role = errors.E_REVOKED_ROOT, "root"
            else:
                code, role = errors.E_REVOKED_INTERMEDIATE, "intermediate"
            errs.append(_err(code, i,
                             f"{role} certificate {cid!r} is revoked; the "
                             f"invalidation propagates to the whole path"))
    return errs


def _explain(claim, certs, snapshot, verdict):
    hops = []
    for cid in claim.chain:
        cert = certs.get(cid)
        if cert is None:
            hops.append(f"{cid}(missing)")
        else:
            hops.append(f"{cid}[issued-by:{cert.issuer}]")
    path_str = " --> ".join(hops)
    return (f"claim {claim.claim_id}: {path_str} | anchor v"
            f"{snapshot.snapshot_version} (policy {snapshot.policy_version}, "
            f"key {snapshot.stable_key[:12]}...) => {verdict}")


def validate_claim(claim, certs, snapshot) -> Conclusion:
    """Validate one claim against exactly one snapshot version."""
    structural = _structural_errors(claim, certs, snapshot)
    if structural:
        verdict, found = UNTRUSTED, structural
    else:
        found = _invalidation_errors(claim, certs, snapshot)
        verdict = PARTIAL if found else TRUSTED
    return Conclusion(
        claim_id=claim.claim_id,
        verdict=verdict,
        anchor_version=snapshot.snapshot_version,
        anchor_key=snapshot.stable_key,
        policy_version=snapshot.policy_version,
        errors=found,
        path=list(claim.chain),
        explanation=_explain(claim, certs, snapshot, verdict),
    )


def revocation_propagation(conclusions):
    """Aggregate invalidation propagation: cert id -> affected claim ids.

    Distinguishes leaf / intermediate / root revocation and expiry.
    """
    propagation = {}
    for concl in conclusions:
        for err in concl.errors:
            if err.code in (errors.E_REVOKED_LEAF, errors.E_REVOKED_INTERMEDIATE,
                            errors.E_REVOKED_ROOT, errors.E_EXPIRED,
                            errors.E_NOT_YET_VALID):
                cid = concl.path[err.position] if err.position is not None else None
                if cid is None:
                    continue
                entry = propagation.setdefault(cid, {
                    "cert_id": cid,
                    "kinds": set(),
                    "propagates_to": [],
                })
                entry["kinds"].add(err.code)
                if concl.claim_id not in entry["propagates_to"]:
                    entry["propagates_to"].append(concl.claim_id)
    result = []
    for cid in sorted(propagation):
        entry = propagation[cid]
        result.append({
            "cert_id": cid,
            "kinds": sorted(entry["kinds"]),
            "propagates_to": sorted(entry["propagates_to"]),
        })
    return result
