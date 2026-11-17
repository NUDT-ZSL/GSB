"""Parsing & normalization layer for certificate descriptions and chain claims.

Every input entry is validated independently; invalid entries produce a
reject record carrying the file index and a business error code, and
processing continues with the remaining entries.
"""

from __future__ import annotations

import json
import re

from . import errors
from .models import CertDesc, ChainClaim

CERT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FINGERPRINT_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


def _reject(file_index, entry_index, code, message, **extra):
    record = {
        "file_index": file_index,
        "entry_index": entry_index,
        "code": code,
        "message": message,
    }
    record.update(extra)
    return record


def _load_entries(path, file_index):
    """Load a JSON file containing one object or a list of objects."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return None, [_reject(file_index, None, errors.E_CERT_SCHEMA,
                              f"unreadable JSON file {path}: {exc}")]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None, [_reject(file_index, None, errors.E_CERT_SCHEMA,
                              f"top-level JSON must be an object or list in {path}")]
    return data, []


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_cert(raw, file_index, entry_index):
    """Validate one certificate description; return (CertDesc | None, reject | None)."""
    if not isinstance(raw, dict):
        return None, _reject(file_index, entry_index, errors.E_CERT_SCHEMA,
                             "certificate entry must be an object")
    cert_id = raw.get("id")
    if not isinstance(cert_id, str) or not CERT_ID_RE.match(cert_id):
        return None, _reject(file_index, entry_index, errors.E_CERT_ID_FORMAT,
                             f"invalid certificate id: {cert_id!r}")
    issuer = raw.get("issuer")
    if not isinstance(issuer, str) or not CERT_ID_RE.match(issuer):
        return None, _reject(file_index, entry_index, errors.E_CERT_ID_FORMAT,
                             f"invalid issuer id: {issuer!r}", cert_id=cert_id)
    fp = raw.get("fingerprint")
    match = FINGERPRINT_RE.match(fp) if isinstance(fp, str) else None
    if not match:
        return None, _reject(file_index, entry_index, errors.E_CERT_FINGERPRINT,
                             f"invalid public-key fingerprint: {fp!r}", cert_id=cert_id)
    fingerprint = "sha256:" + match.group(1).lower()
    nb, na = raw.get("not_before_ms"), raw.get("not_after_ms")
    if not _is_int(nb) or not _is_int(na) or nb >= na:
        return None, _reject(file_index, entry_index, errors.E_CERT_VALIDITY,
                             "validity must be integer milliseconds with "
                             "not_before_ms < not_after_ms", cert_id=cert_id)
    revoked = raw.get("revoked", False)
    if not isinstance(revoked, bool):
        return None, _reject(file_index, entry_index, errors.E_CERT_SCHEMA,
                             "revoked must be a boolean", cert_id=cert_id)
    return CertDesc(cert_id, issuer, fingerprint, nb, na, revoked), None


def parse_cert_files(paths):
    """Parse certificate description files.

    Returns (certs: dict[id, CertDesc], rejects: list). Uniqueness of
    identifiers and issuer references are checked across all files.
    """
    certs = {}
    rejects = []
    pending = []
    for file_index, path in enumerate(paths):
        entries, load_rejects = _load_entries(path, file_index)
        rejects.extend(load_rejects)
        if entries is None:
            continue
        for entry_index, raw in enumerate(entries):
            cert, reject = normalize_cert(raw, file_index, entry_index)
            if reject:
                rejects.append(reject)
                continue
            if cert.cert_id in certs:
                rejects.append(_reject(file_index, entry_index, errors.E_CERT_DUP_ID,
                                       f"duplicate certificate id {cert.cert_id!r}",
                                       cert_id=cert.cert_id))
                continue
            certs[cert.cert_id] = cert
            pending.append((cert, file_index, entry_index))
    for cert, file_index, entry_index in pending:
        if cert.issuer not in certs:
            rejects.append(_reject(file_index, entry_index, errors.E_CERT_ISSUER_MISSING,
                                   f"issuer {cert.issuer!r} of {cert.cert_id!r} "
                                   f"has no certificate description",
                                   cert_id=cert.cert_id))
            del certs[cert.cert_id]
    return certs, rejects


def normalize_claim(raw, file_index, entry_index):
    """Validate one chain claim; return (ChainClaim | None, reject | None)."""
    if not isinstance(raw, dict):
        return None, _reject(file_index, entry_index, errors.E_CHAIN_SCHEMA,
                             "chain claim entry must be an object")
    claim_id = raw.get("claim_id")
    if not isinstance(claim_id, str) or not CERT_ID_RE.match(claim_id):
        return None, _reject(file_index, entry_index, errors.E_CHAIN_SCHEMA,
                             f"invalid claim id: {claim_id!r}")
    chain = raw.get("chain")
    if not isinstance(chain, list) or not all(isinstance(c, str) for c in chain):
        return None, _reject(file_index, entry_index, errors.E_CHAIN_SCHEMA,
                             "chain must be a list of certificate identifiers",
                             claim_id=claim_id)
    if len(chain) == 0:
        return None, _reject(file_index, entry_index, errors.E_CHAIN_EMPTY,
                             "chain length is zero", claim_id=claim_id)
    seen = set()
    for position, cid in enumerate(chain):
        if cid in seen:
            return None, _reject(file_index, entry_index, errors.E_CHAIN_DUP,
                                 f"duplicate identifier {cid!r} in chain",
                                 claim_id=claim_id, position=position)
        seen.add(cid)
    return ChainClaim(claim_id, tuple(chain)), None


def parse_chain_files(paths, certs):
    """Parse chain claim files; references are checked against `certs`.

    Returns (claims: dict[claim_id, ChainClaim], rejects: list).
    """
    claims = {}
    rejects = []
    for file_index, path in enumerate(paths):
        entries, load_rejects = _load_entries(path, file_index)
        rejects.extend(load_rejects)
        if entries is None:
            continue
        for entry_index, raw in enumerate(entries):
            claim, reject = normalize_claim(raw, file_index, entry_index)
            if reject:
                rejects.append(reject)
                continue
            if claim.claim_id in claims:
                rejects.append(_reject(file_index, entry_index, errors.E_CHAIN_SCHEMA,
                                       f"duplicate claim id {claim.claim_id!r}",
                                       claim_id=claim.claim_id))
                continue
            missing = None
            for position, cid in enumerate(claim.chain):
                if cid not in certs:
                    missing = (position, cid)
                    break
            if missing is not None:
                rejects.append(_reject(file_index, entry_index,
                                       errors.E_CHAIN_REF_MISSING,
                                       f"chain references unknown certificate "
                                       f"{missing[1]!r}",
                                       claim_id=claim.claim_id, position=missing[0]))
                continue
            claims[claim.claim_id] = claim
    return claims, rejects
