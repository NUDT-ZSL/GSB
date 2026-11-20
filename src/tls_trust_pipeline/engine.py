from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .canonical import sha256_json
from .certs import CertPool, build_chain, cert_fingerprint, chain_result_to_dict
from .errors import (
    E_BAD_PATCH,
    E_BAD_REQUEST,
    E_CERT_INVALID,
    E_CLOCK_ROLLBACK,
    E_FP_MISMATCH,
    E_PATCH_CONFLICT,
    E_STATE,
    PipelineError,
)
from .models import ChainRequest, Snapshot
from .patches import build_patch_target, read_patch
from .revocation import RevocationEvidence
from .snapshots import parse_v1_snapshot, parse_v2_snapshot
from .store import Store


def _int_field(value: Any, field: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise PipelineError(E_BAD_REQUEST, f"{field} must be an integer >= {minimum}")
    return value


def parse_requests(path: Path) -> list[ChainRequest]:
    if not path.is_file():
        raise PipelineError(E_BAD_REQUEST, "request file does not exist", {"path": str(path)})
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(E_BAD_REQUEST, "request file is not valid JSON") from exc
    if not isinstance(document, dict):
        raise PipelineError(E_BAD_REQUEST, "request document root must be an object")
    if document.get("schema") != 1 or not isinstance(document.get("requests"), list):
        raise PipelineError(E_BAD_REQUEST, "unsupported request document")
    requests = []
    seen_ids: set[str] = set()
    for index, item in enumerate(document["requests"]):
        if not isinstance(item, dict):
            raise PipelineError(E_BAD_REQUEST, "request must be an object", {"index": index})
        request_id = item.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in seen_ids:
            raise PipelineError(E_BAD_REQUEST, "request_id must be unique and non-empty")
        seen_ids.add(request_id)
        hostname = item.get("hostname")
        purpose = item.get("purpose")
        leaf_file = item.get("leaf_file")
        require_revocation = item.get("require_revocation")
        if not isinstance(hostname, str) or not hostname:
            raise PipelineError(E_BAD_REQUEST, "hostname must be non-empty")
        if purpose not in ("tls-server", "tls-client"):
            raise PipelineError(E_BAD_REQUEST, "unsupported purpose", {"purpose": purpose})
        if not isinstance(leaf_file, str) or Path(leaf_file).name != leaf_file:
            raise PipelineError(E_BAD_REQUEST, "leaf_file must be a basename")
        if not isinstance(require_revocation, bool):
            raise PipelineError(E_BAD_REQUEST, "require_revocation must be a boolean")
        requests.append(
            ChainRequest(
                request_id=request_id,
                hostname=hostname,
                purpose=purpose,
                logical_clock_ms=_int_field(
                    item.get("logical_clock_ms"), "logical_clock_ms"
                ),
                require_revocation=require_revocation,
                leaf_file=leaf_file,
            )
        )
    return sorted(requests, key=lambda request: request.request_id)


def request_to_dict(request: ChainRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "hostname": request.hostname,
        "purpose": request.purpose,
        "logical_clock_ms": request.logical_clock_ms,
        "require_revocation": request.require_revocation,
        "leaf_file": request.leaf_file,
    }


def _maybe_crash(stage: str) -> None:
    if os.environ.get("TLS_TRUST_CRASH_POINT") == stage:
        os._exit(99)


def _revocation_row_to_dict(row) -> dict[str, Any]:
    return {
        "certificate_fingerprint": row["cert_fp"],
        "serial_hex": row["serial_hex"],
        "status": row["status"],
        "reason": row["reason"],
        "source": row["source"],
        "evidence_sha256": row["evidence_sha256"],
        "record_sha256": row["record_sha256"],
        "checked_clock_ms": row["checked_clock_ms"],
        "revoked_at_ms": row["revoked_at_ms"],
    }


class PipelineEngine:
    def __init__(
        self,
        store: Store,
        cert_dir: Path,
        revocation_evidence_path: Path | None,
        clock_tolerance_ms: int = 300_000,
    ) -> None:
        self.store = store
        self.cert_dir = cert_dir
        self.revocation_evidence_path = revocation_evidence_path
        self.clock_tolerance_ms = clock_tolerance_ms

    def load(
        self,
        *,
        v1_path: Path | None,
        v2_path: Path | None,
        logical_clock_ms: int,
    ) -> dict[str, Any]:
        _int_field(logical_clock_ms, "logical_clock_ms")
        if v1_path is None and v2_path is None:
            raise PipelineError(E_BAD_REQUEST, "load requires --v1 and/or --v2")
        snapshots = []
        if v1_path is not None:
            snapshots.append(
                parse_v1_snapshot(v1_path, self.cert_dir, logical_clock_ms)
            )
        if v2_path is not None:
            snapshots.append(parse_v2_snapshot(v2_path, self.cert_dir))
        versions = [snapshot.snapshot_version for snapshot in snapshots]
        generations = [snapshot.generation for snapshot in snapshots]
        if len(set(versions)) != len(versions) or len(set(generations)) != len(generations):
            raise PipelineError(
                E_BAD_REQUEST,
                "loaded snapshots must have unique versions and generations",
            )
        self.store.initialize()
        result = self.store.load_snapshots(
            snapshots,
            logical_clock_ms=logical_clock_ms,
            tolerance_ms=self.clock_tolerance_ms,
        )
        result.update(
            {
                "ok": True,
                "validated_versions": sorted(versions),
                "clock_precision_ms": 1,
            }
        )
        return result

    def _current_snapshot_read(self, clock_ms: int) -> Snapshot:
        with self.store.read() as connection:
            self.store.check_clock(connection, clock_ms, self.clock_tolerance_ms)
            current = self.store.current_snapshot(connection)
            if current is None:
                raise PipelineError(E_STATE, "no current snapshot has been loaded")
            return current

    @staticmethod
    def _validate_snapshot_material(snapshot: Snapshot, pool: CertPool) -> None:
        for anchor in snapshot.anchors:
            cert = pool.get_fp(anchor.fingerprint)
            if cert is None:
                raise PipelineError(
                    E_CERT_INVALID,
                    "trust anchor certificate is missing from the certificate directory",
                    {"alias": anchor.alias, "fingerprint": anchor.fingerprint},
                )
            if cert_fingerprint(pool.get_file(anchor.cert_file)) != anchor.fingerprint:
                raise PipelineError(
                    E_CERT_INVALID,
                    "trust anchor certificate material changed after snapshot load",
                    {"alias": anchor.alias, "cert_file": anchor.cert_file},
                )

    def apply_patch_document(
        self,
        document: dict[str, Any],
        *,
        logical_clock_ms: int,
        resume_job_id: str | None = None,
    ) -> dict[str, Any]:
        _int_field(logical_clock_ms, "logical_clock_ms")
        current = self._current_snapshot_read(logical_clock_ms)
        self._validate_snapshot_material(current, CertPool.load(self.cert_dir))
        patch_digest = sha256_json(document)
        payload = {
            "kind": "apply",
            "patch": document,
            "cert_dir": str(self.cert_dir.resolve()),
            "logical_clock_ms": logical_clock_ms,
            "clock_tolerance_ms": self.clock_tolerance_ms,
        }
        with self.store.read() as connection:
            existing_patch = connection.execute(
                "SELECT * FROM patches WHERE patch_digest = ?", (patch_digest,)
            ).fetchone()
            if existing_patch is not None:
                existing_snapshot = self.store.snapshot_by_version(
                    connection, existing_patch["target_version"]
                )
                if existing_snapshot is None:
                    raise PipelineError(E_STATE, "applied patch target snapshot is missing")
                result = {
                    "ok": True,
                    "status": "idempotent",
                    "version": existing_snapshot.snapshot_version,
                    "generation": existing_snapshot.generation,
                }
                if resume_job_id is None:
                    job_id, _ = self.store.begin_job(
                        "apply",
                        payload,
                        logical_clock_ms=logical_clock_ms,
                        tolerance_ms=self.clock_tolerance_ms,
                    )
                else:
                    job_id = resume_job_id
                self.store.finish_job(
                    job_id,
                    result,
                    logical_clock_ms=logical_clock_ms,
                    tolerance_ms=self.clock_tolerance_ms,
                )
                return result
            patch_id_value = document.get("patch_id")
            if isinstance(patch_id_value, str):
                patch_id_row = connection.execute(
                    "SELECT patch_digest FROM patches WHERE patch_id = ?",
                    (patch_id_value,),
                ).fetchone()
                if patch_id_row is not None:
                    raise PipelineError(
                        E_PATCH_CONFLICT,
                        "patch_id was already applied with different content",
                        {"patch_id": patch_id_value},
                    )
            target_version = document.get("target_version")
            if isinstance(target_version, str):
                target_row = connection.execute(
                    "SELECT source_digest FROM snapshots WHERE snapshot_version = ?",
                    (target_version,),
                ).fetchone()
                if target_row is not None:
                    raise PipelineError(
                        E_PATCH_CONFLICT,
                        "patch target version already exists",
                        {"target_version": target_version},
                    )
        new_snapshot, patch_digest = build_patch_target(
            document=document,
            current=current,
            cert_dir=self.cert_dir,
            apply_clock_ms=logical_clock_ms,
        )
        if resume_job_id is None:
            job_id, _ = self.store.begin_job(
                "apply",
                payload,
                logical_clock_ms=logical_clock_ms,
                tolerance_ms=self.clock_tolerance_ms,
            )
        else:
            job_id = resume_job_id
        _maybe_crash("apply_before_commit")
        result = self.store.apply_patch(
            patch=document,
            new_snapshot=new_snapshot,
            patch_digest=patch_digest,
            logical_clock_ms=logical_clock_ms,
            tolerance_ms=self.clock_tolerance_ms,
            job_id=job_id,
        )
        result["ok"] = True
        return result

    def apply_patch_file(self, path: Path, *, logical_clock_ms: int) -> dict[str, Any]:
        document = read_patch(path)
        return self.apply_patch_document(document, logical_clock_ms=logical_clock_ms)

    def _chain_invalidation_reason(
        self,
        connection,
        *,
        hostname: str,
        purpose: str,
        generation: int,
    ) -> str | None:
        row = connection.execute(
            """
            SELECT action, reason FROM invalidations
            WHERE to_generation = ? AND conclusion_kind = 'chain'
              AND conclusion_key LIKE ?
            ORDER BY id DESC LIMIT 1
            """,
            (generation, f"{hostname}|{purpose}|%"),
        ).fetchone()
        return None if row is None else f"{row['action']}:{row['reason']}"

    def _revocation_invalidation_reason(
        self,
        connection,
        *,
        cert_fp: str,
        generation: int,
    ) -> str | None:
        row = connection.execute(
            """
            SELECT action, reason FROM invalidations
            WHERE to_generation = ? AND conclusion_kind = 'revocation'
              AND conclusion_key = ?
            ORDER BY id DESC LIMIT 1
            """,
            (generation, cert_fp),
        ).fetchone()
        return None if row is None else f"{row['action']}:{row['reason']}"

    def evaluate_request(
        self,
        request: ChainRequest,
        *,
        mode: str,
        forced_job_id: str | None = None,
    ) -> dict[str, Any]:
        if mode not in ("chain", "verify"):
            raise PipelineError(E_BAD_REQUEST, "mode must be chain or verify")
        pool = CertPool.load(self.cert_dir)
        evidence = RevocationEvidence(self.revocation_evidence_path, pool)
        leaf_cert = pool.get_file(request.leaf_file)
        leaf_fingerprint = cert_fingerprint(leaf_cert)
        last_error: PipelineError | None = None
        for _attempt in range(4):
            snapshot = self._current_snapshot_read(request.logical_clock_ms)
            self._validate_snapshot_material(snapshot, pool)
            payload = {
                "kind": "verify",
                "mode": mode,
                "snapshot_version": snapshot.snapshot_version,
                "snapshot_generation": snapshot.generation,
                "request": request_to_dict(request),
                "cert_dir": str(self.cert_dir.resolve()),
                "revocation_evidence": (
                    str(self.revocation_evidence_path.resolve())
                    if self.revocation_evidence_path is not None
                    else None
                ),
                "clock_tolerance_ms": self.clock_tolerance_ms,
            }
            if forced_job_id is None:
                job_id, _job_status = self.store.begin_job(
                    "verify",
                    payload,
                    logical_clock_ms=request.logical_clock_ms,
                    tolerance_ms=self.clock_tolerance_ms,
                )
            else:
                job_id = forced_job_id
                forced_job_id = None
            snapshot_changed = False
            with self.store.read() as connection:
                current = self.store.current_snapshot(connection)
                if current is None or current.snapshot_version != snapshot.snapshot_version:
                    snapshot_changed = True
                else:
                    chain_row = self.store.chain_cache(
                        connection,
                        hostname=request.hostname,
                        purpose=request.purpose,
                        generation=snapshot.generation,
                    )
                    chain_hit = (
                        chain_row is not None
                        and chain_row["request_clock_ms"] == request.logical_clock_ms
                        and chain_row["leaf_file"] == request.leaf_file
                        and chain_row["leaf_fingerprint"] == leaf_fingerprint
                    )
                    if chain_hit:
                        chain_doc = json.loads(chain_row["evidence_json"])
                        cache_chain_action = (
                            "reused" if chain_row["cache_origin"] == "reused" else "hit"
                        )
                        cache_chain_reason = (
                            "incremental_anchor_unchanged"
                            if cache_chain_action == "reused"
                            else "generation_snapshot_cache"
                        )
                    else:
                        result = build_chain(request, pool, snapshot.anchors)
                        chain_doc = chain_result_to_dict(result)
                        chain_doc["leaf_file"] = request.leaf_file
                        chain_doc["leaf_fingerprint"] = leaf_fingerprint
                        invalidation = self._chain_invalidation_reason(
                            connection,
                            hostname=request.hostname,
                            purpose=request.purpose,
                            generation=snapshot.generation,
                        )
                        if invalidation and invalidation.startswith("invalidate:"):
                            cache_chain_action = "invalidated"
                            cache_chain_reason = invalidation.split(":", 1)[1]
                        elif (
                            chain_row is not None
                            and chain_row["request_clock_ms"] != request.logical_clock_ms
                        ):
                            cache_chain_action = "invalidated"
                            cache_chain_reason = "logical_clock_changed"
                        else:
                            cache_chain_action = "computed"
                            cache_chain_reason = "no_safe_cached_conclusion"
                    hostname_row = self.store.hostname_cache(
                        connection,
                        leaf_fp=leaf_fingerprint,
                        hostname=request.hostname,
                        purpose=request.purpose,
                    )
                    hostname_action = "hit" if hostname_row is not None else "computed"
                    revocation_rows = {}
                    revocation_actions = {}
                    chain_fingerprints = [
                        item["fingerprint"] for item in chain_doc["chain"]
                    ]
                    for fingerprint in chain_fingerprints:
                        if fingerprint == chain_doc.get("anchor_fingerprint"):
                            continue
                        cached = self.store.revocation_cache(
                            connection,
                            cert_fp=fingerprint,
                            generation=snapshot.generation,
                        )
                        usable = (
                            cached is not None
                            and cached["checked_clock_ms"] == request.logical_clock_ms
                            and cached["evidence_sha256"] == evidence.digest
                        )
                        if usable:
                            revocation_rows[fingerprint] = _revocation_row_to_dict(cached)
                            revocation_actions[fingerprint] = {
                                "action": "hit",
                                "reason": "same_generation_and_evidence",
                            }
                        else:
                            cert = pool.get_fp(fingerprint)
                            if cert is None:
                                raise PipelineError(
                                    E_STATE,
                                    "chain fingerprint is missing from certificate pool",
                                    {"fingerprint": fingerprint},
                                )
                            revocation_rows[fingerprint] = evidence.check(
                                cert, request.logical_clock_ms
                            )
                            reason = self._revocation_invalidation_reason(
                                connection,
                                cert_fp=fingerprint,
                                generation=snapshot.generation,
                            )
                            revocation_actions[fingerprint] = {
                                "action": (
                                    "invalidated"
                                    if reason and reason.startswith("invalidate:")
                                    else "computed"
                                ),
                                "reason": (
                                    reason.split(":", 1)[1]
                                    if reason and reason.startswith("invalidate:")
                                    else "no_safe_cached_conclusion"
                                ),
                            }
            if snapshot_changed:
                self.store.abort_job(job_id, "snapshot_changed_before_evaluation")
                time.sleep(0.02)
                continue
            revocations = [
                revocation_rows[key] for key in chain_fingerprints if key in revocation_rows
            ]
            structural_status = chain_doc["status"]
            structural_reason = chain_doc["reason_code"]
            handshake_status = structural_status
            handshake_reason = structural_reason
            revocation_policy_enforced = mode == "verify" and request.require_revocation
            if revocation_policy_enforced and structural_status == "VALID":
                revoked = next(
                    (item for item in revocations if item["status"] == "REVOKED"), None
                )
                unknown = next(
                    (item for item in revocations if item["status"] == "UNKNOWN"), None
                )
                if revoked is not None:
                    handshake_status = "INVALID"
                    handshake_reason = "E_REVOKED"
                elif unknown is not None:
                    handshake_status = "INVALID"
                    handshake_reason = "E_REVOCATION_UNKNOWN"
                else:
                    handshake_reason = "OK"
            elif structural_status == "VALID":
                handshake_reason = "OK"
            output = {
                "request_id": request.request_id,
                "hostname": request.hostname,
                "purpose": request.purpose,
                "logical_clock_ms": request.logical_clock_ms,
                "require_revocation": request.require_revocation,
                "leaf_file": request.leaf_file,
                "leaf_fingerprint": leaf_fingerprint,
                "anchor_alias": chain_doc.get("anchor_alias"),
                "anchor_fingerprint": chain_doc.get("anchor_fingerprint"),
                "structural": {
                    "status": structural_status,
                    "reason_code": structural_reason,
                    "reason": chain_doc["reason"],
                },
                "handshake": {
                    "status": handshake_status,
                    "reason_code": handshake_reason,
                    "revocation_policy_enforced": revocation_policy_enforced,
                },
                "hostname_check": chain_doc["hostname_check"],
                "chain": chain_doc["chain"],
                "revocations": revocations,
                "cache": {
                    "chain": {"action": cache_chain_action, "reason": cache_chain_reason},
                    "hostname": {"action": hostname_action},
                    "revocations": {
                        key: revocation_actions[key]
                        for key in chain_fingerprints
                        if key in revocation_actions
                    },
                },
            }
            stable_result = {
                "ok": True,
                "request_id": request.request_id,
                "snapshot_version": snapshot.snapshot_version,
                "snapshot_generation": snapshot.generation,
                "structural_status": structural_status,
                "handshake_status": handshake_status,
                "handshake_reason_code": handshake_reason,
            }
            _maybe_crash("verify_before_commit")
            try:
                self.store.persist_verification(
                    job_id=job_id,
                    expected_version=snapshot.snapshot_version,
                    logical_clock_ms=request.logical_clock_ms,
                    tolerance_ms=self.clock_tolerance_ms,
                    chain=chain_doc,
                    hostname=chain_doc["hostname_check"],
                    revocations=revocations,
                    result=stable_result,
                )
                return output
            except PipelineError as exc:
                if exc.code != E_STATE:
                    raise
                last_error = exc
                self.store.abort_job(job_id, "snapshot_changed_retry")
                time.sleep(0.02)
                continue
        if last_error is not None:
            raise PipelineError(
                E_STATE,
                "snapshot changed repeatedly during verification",
                {"last_error": last_error.message},
            )
        raise PipelineError(E_STATE, "verification could not obtain a stable snapshot")

    def evaluate_requests(
        self,
        requests: list[ChainRequest],
        *,
        mode: str,
    ) -> dict[str, Any]:
        results = [
            self.evaluate_request(request, mode=mode)
            for request in sorted(requests, key=lambda item: item.request_id)
        ]
        with self.store.read() as connection:
            current = self.store.current_snapshot(connection)
            if current is None:
                raise PipelineError(E_STATE, "no current snapshot")
            watermark = self.store._meta_int(connection, "logical_clock_watermark_ms")
        summary = {
            "ok": True,
            "mode": mode,
            "snapshot_version": current.snapshot_version,
            "snapshot_generation": current.generation,
            "clock_precision_ms": 1,
            "logical_clock_watermark_ms": watermark,
            "results": results,
        }
        summary["result_digest"] = sha256_json(
            {key: value for key, value in summary.items() if key != "result_digest"}
        )
        return summary

    def resume(self) -> dict[str, Any]:
        jobs = self.store.running_jobs()
        resumed = []
        aborted = []
        for job in jobs:
            payload = json.loads(job["payload_json"])
            if job["kind"] == "verify":
                request_doc = payload["request"]
                request = ChainRequest(
                    request_id=request_doc["request_id"],
                    hostname=request_doc["hostname"],
                    purpose=request_doc["purpose"],
                    logical_clock_ms=request_doc["logical_clock_ms"],
                    require_revocation=request_doc["require_revocation"],
                    leaf_file=request_doc["leaf_file"],
                )
                with self.store.read() as connection:
                    current = self.store.current_snapshot(connection)
                if (
                    current is None
                    or current.snapshot_version != payload["snapshot_version"]
                ):
                    self.store.abort_job(job["job_id"], "snapshot_changed_before_resume")
                    aborted.append(job["job_id"])
                    continue
                resume_cert_dir = Path(payload.get("cert_dir", self.cert_dir))
                evidence_value = payload.get(
                    "revocation_evidence",
                    str(self.revocation_evidence_path)
                    if self.revocation_evidence_path is not None
                    else None,
                )
                resume_evidence = Path(evidence_value) if evidence_value else None
                resume_engine = PipelineEngine(
                    self.store,
                    cert_dir=resume_cert_dir,
                    revocation_evidence_path=resume_evidence,
                    clock_tolerance_ms=int(
                        payload.get("clock_tolerance_ms", self.clock_tolerance_ms)
                    ),
                )
                try:
                    output = resume_engine.evaluate_request(
                        request,
                        mode=payload["mode"],
                        forced_job_id=job["job_id"],
                    )
                except PipelineError as exc:
                    if exc.code == E_CLOCK_ROLLBACK:
                        self.store.abort_job(job["job_id"], "clock_rollback")
                        aborted.append(job["job_id"])
                        continue
                    if exc.code in (E_BAD_REQUEST, E_CERT_INVALID):
                        self.store.abort_job(job["job_id"], "invalid_replay_input")
                        aborted.append(job["job_id"])
                        continue
                    raise
                resumed.append(
                    {
                        "job_id": job["job_id"],
                        "kind": "verify",
                        "request_id": request.request_id,
                        "handshake_status": output["handshake"]["status"],
                    }
                )
            elif job["kind"] == "apply":
                resume_engine = PipelineEngine(
                    self.store,
                    cert_dir=Path(payload["cert_dir"]),
                    revocation_evidence_path=None,
                    clock_tolerance_ms=int(
                        payload.get("clock_tolerance_ms", self.clock_tolerance_ms)
                    ),
                )
                try:
                    result = resume_engine.apply_patch_document(
                        payload["patch"],
                        logical_clock_ms=payload["logical_clock_ms"],
                        resume_job_id=job["job_id"],
                    )
                except PipelineError as exc:
                    if exc.code == E_CLOCK_ROLLBACK:
                        self.store.abort_job(job["job_id"], "clock_rollback")
                        aborted.append(job["job_id"])
                        continue
                    if exc.code in (
                        E_BAD_PATCH,
                        E_PATCH_CONFLICT,
                        E_FP_MISMATCH,
                        E_CERT_INVALID,
                    ):
                        self.store.abort_job(job["job_id"], "invalid_replay_input")
                        aborted.append(job["job_id"])
                        continue
                    raise
                resumed.append(
                    {
                        "job_id": job["job_id"],
                        "kind": "apply",
                        "status": result["status"],
                        "version": result["version"],
                    }
                )
            else:
                self.store.abort_job(job["job_id"], "unknown_job_kind")
                aborted.append(job["job_id"])
        output = {
            "ok": True,
            "running_before": len(jobs),
            "resumed": resumed,
            "aborted": sorted(aborted),
        }
        output["resume_digest"] = sha256_json(
            {key: value for key, value in output.items() if key != "resume_digest"}
        )
        return output

