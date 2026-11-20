from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import canonical_fingerprint, sha256_json
from .certs import CertPool, cert_fingerprint
from .errors import E_BAD_PATCH, E_FP_MISMATCH, PipelineError
from .models import Anchor, Snapshot


def _bad(message: str, **details: Any) -> PipelineError:
    return PipelineError(E_BAD_PATCH, message, details or None)


def _int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _bad(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise _bad(f"{field} must be >= {minimum}")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"{field} must be a boolean")
    return value


def read_patch(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise _bad("patch file does not exist", path=str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _bad("patch is not valid JSON", path=str(path)) from exc
    if not isinstance(value, dict):
        raise _bad("patch root must be an object")
    return value


def _resolve_cert_identity(
    pool: CertPool,
    *,
    cert_file: Any,
    fingerprint: Any,
    alias: str,
) -> tuple[str, str]:
    cert_name = cert_file
    declared = fingerprint
    if cert_name is not None:
        if not isinstance(cert_name, str) or Path(cert_name).name != cert_name:
            raise _bad("cert_file must be a basename", alias=alias)
        cert = pool.get_file(cert_name)
        actual = cert_fingerprint(cert)
        if declared is None:
            return cert_name, actual
        try:
            declared_fp = canonical_fingerprint(str(declared))
        except ValueError as exc:
            raise _bad("fingerprint must be a SHA-256 digest", alias=alias) from exc
        if declared_fp != actual:
            raise PipelineError(
                E_FP_MISMATCH,
                "patch fingerprint does not match certificate PEM",
                {"alias": alias, "declared": declared_fp, "actual": actual},
            )
        return cert_name, actual
    if declared is not None:
        try:
            declared_fp = canonical_fingerprint(str(declared))
        except ValueError as exc:
            raise _bad("fingerprint must be a SHA-256 digest", alias=alias) from exc
        cert = pool.get_fp(declared_fp)
        if cert is None:
            raise PipelineError(
                E_FP_MISMATCH,
                "patch fingerprint has no matching certificate in cert_dir",
                {"alias": alias, "fingerprint": declared_fp},
            )
        matching_files = [
            filename for filename, candidate in pool.certs_by_file.items() if candidate == cert
        ]
        if len(matching_files) != 1:
            raise _bad("cert_file is required when fingerprint is ambiguous", alias=alias)
        return matching_files[0], declared_fp
    raise _bad("add operation requires cert_file or fingerprint", alias=alias)


def build_patch_target(
    *,
    document: dict[str, Any],
    current: Snapshot,
    cert_dir: Path,
    apply_clock_ms: int,
) -> tuple[Snapshot, str]:
    if document.get("schema") != 1:
        raise _bad("patch schema must be 1")
    patch_id = document.get("patch_id")
    if not isinstance(patch_id, str) or not patch_id:
        raise _bad("patch_id must be a non-empty string")
    base_version = document.get("base_version")
    if not isinstance(base_version, str) or base_version != current.snapshot_version:
        raise _bad(
            "patch base_version does not match current snapshot",
            base_version=base_version,
            current_version=current.snapshot_version,
        )
    target_version = document.get("target_version")
    if not isinstance(target_version, str) or not target_version:
        raise _bad("target_version must be a non-empty string")
    if target_version == current.snapshot_version:
        raise _bad("target_version must differ from base_version")
    target_generation = _int(
        document.get("target_generation"), "target_generation", minimum=1
    )
    if target_generation != current.generation + 1:
        raise _bad(
            "target_generation must be current generation + 1",
            current_generation=current.generation,
            target_generation=target_generation,
        )
    effective_clock_ms = _int(
        document.get("effective_clock_ms"), "effective_clock_ms", minimum=0
    )
    if apply_clock_ms < effective_clock_ms:
        raise _bad(
            "patch cannot be applied before its effective clock",
            effective_clock_ms=effective_clock_ms,
            apply_clock_ms=apply_clock_ms,
        )
    operations = document.get("operations")
    if not isinstance(operations, list) or not operations:
        raise _bad("operations must be a non-empty array")
    pool = CertPool.load(cert_dir)
    anchors = {anchor.alias: anchor for anchor in current.anchors}
    for operation_index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise _bad("operation must be an object", index=operation_index)
        op = operation.get("op")
        alias = operation.get("alias")
        if not isinstance(alias, str) or not alias:
            raise _bad("operation alias must be a non-empty string", index=operation_index)
        if op == "delete":
            if alias not in anchors:
                raise _bad("delete target does not exist", alias=alias)
            del anchors[alias]
            continue
        if op == "add":
            if alias in anchors:
                raise _bad("add target already exists", alias=alias)
            cert_file, fingerprint = _resolve_cert_identity(
                pool,
                cert_file=operation.get("cert_file"),
                fingerprint=operation.get("fingerprint"),
                alias=alias,
            )
            not_before_ms = _int(
                operation.get("not_before_ms"),
                f"operations[{operation_index}].not_before_ms",
                minimum=0,
            )
            not_after_ms = _int(
                operation.get("not_after_ms"),
                f"operations[{operation_index}].not_after_ms",
                minimum=0,
            )
            tombstone = _bool(operation.get("tombstone", False), "tombstone")
            can_issue = _bool(
                operation.get("can_issue_intermediates", not tombstone),
                "can_issue_intermediates",
            )
            if not_before_ms > not_after_ms:
                raise _bad("anchor validity interval is inverted", alias=alias)
            if tombstone and can_issue:
                raise _bad("tombstoned anchor cannot issue intermediates", alias=alias)
            anchors[alias] = Anchor(
                alias=alias,
                fingerprint=fingerprint,
                cert_file=cert_file,
                not_before_ms=not_before_ms,
                not_after_ms=not_after_ms,
                revoked=tombstone,
                can_issue_intermediates=can_issue,
                trust_generation=target_generation,
                conversion={"source": "patch.absolute_ms", "rounding": "none"},
            )
            continue
        if op not in ("update", "tombstone"):
            raise _bad("unsupported operation", op=op, index=operation_index)
        if alias not in anchors:
            raise _bad(f"{op} target does not exist", alias=alias)
        old = anchors[alias]
        cert_file = old.cert_file
        fingerprint = old.fingerprint
        if "cert_file" in operation or "fingerprint" in operation:
            cert_file, fingerprint = _resolve_cert_identity(
                pool,
                cert_file=operation.get("cert_file", old.cert_file),
                fingerprint=operation.get("fingerprint", old.fingerprint),
                alias=alias,
            )
        not_before_ms = _int(
            operation.get("not_before_ms", old.not_before_ms),
            f"operations[{operation_index}].not_before_ms",
            minimum=0,
        )
        not_after_ms = _int(
            operation.get("not_after_ms", old.not_after_ms),
            f"operations[{operation_index}].not_after_ms",
            minimum=0,
        )
        if not_before_ms > not_after_ms:
            raise _bad("anchor validity interval is inverted", alias=alias)
        tombstone = old.revoked
        if op == "tombstone":
            tombstone = True
        elif "tombstone" in operation:
            tombstone = _bool(operation["tombstone"], "tombstone")
        can_issue = old.can_issue_intermediates
        if "can_issue_intermediates" in operation:
            can_issue = _bool(
                operation["can_issue_intermediates"], "can_issue_intermediates"
            )
        if tombstone:
            can_issue = False
        anchors[alias] = Anchor(
            alias=alias,
            fingerprint=fingerprint,
            cert_file=cert_file,
            not_before_ms=not_before_ms,
            not_after_ms=not_after_ms,
            revoked=tombstone,
            can_issue_intermediates=can_issue,
            trust_generation=target_generation,
            conversion={"source": "patch.absolute_ms", "rounding": "none"},
        )
    if not anchors:
        raise _bad("patch cannot leave an empty trust anchor set")
    target_anchors = tuple(sorted(anchors.values(), key=lambda anchor: anchor.alias))
    fingerprints = [anchor.fingerprint for anchor in target_anchors]
    if len(fingerprints) != len(set(fingerprints)):
        raise _bad("patch target contains duplicate anchor fingerprints")
    patch_digest = sha256_json(document)
    source_digest = sha256_json(
        {
            "kind": "patch",
            "base_digest": current.source_digest,
            "patch_digest": patch_digest,
            "anchors": [anchor.semantic_dict() for anchor in target_anchors],
        }
    )
    target = Snapshot(
        snapshot_version=target_version,
        generation=target_generation,
        created_clock_ms=effective_clock_ms,
        source_digest=source_digest,
        kind="patch",
        anchors=target_anchors,
    )
    return target, patch_digest

