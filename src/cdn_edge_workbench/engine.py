from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError, ErrorCode, SimulatedCrash
from .index import (
    algorithm_records,
    arbitrate_candidates,
    arbitrate_snapshot_entries,
    candidate_rank_key,
    evict_node,
    freshness_at,
    resolve_key_winners,
    select_revalidation_entries,
)
from .models import CacheEntry, FreshnessResult, Injection, Policy, SourceResource, ValidatedSamples
from .origin import (
    LocalOriginServer,
    OriginRequestError,
    behavior_for_injection,
    parse_cache_control_header,
    request_origin_with_retries,
)
from .persistence import StateStore, state_process_lock
from .snapshots import load_samples
from .validation import canonical_json, normalize_path, parse_etag, sha256_hex


_STAGE_ORDER = {"parse": 0, "merge": 1, "freshness": 2, "origin": 3}


class WorkbenchEngine:
    def __init__(
        self,
        *,
        samples_dir: str | Path,
        state_dir: str | Path,
        origin_base_url: str | None = None,
    ) -> None:
        self.samples_dir = str(Path(samples_dir).resolve())
        self.state_dir = str(Path(state_dir).resolve())
        self.origin_base_url = origin_base_url

    def _default_run_id(self, loaded: ValidatedSamples, logical_clock: int) -> str:
        sample_hashes: dict[str, str] = {}
        root = Path(self.samples_dir)
        for path in sorted(root.rglob("*")):
            if path.is_file():
                sample_hashes[str(path.relative_to(root))] = sha256_hex(path.read_bytes())
        seed = {
            "logical_clock": logical_clock,
            "v1_generation": loaded.v1_generation,
            "v2_generation": loaded.v2_generation,
            "sample_hashes": sample_hashes,
            "candidates": [
                entry.candidate_dict()
                for entry in sorted(
                    loaded.snapshot_candidates,
                    key=lambda item: (item.node, item.path, item.content_version),
                )
            ],
            "injections": [injection.to_dict() for injection in loaded.injections],
            "source_files": loaded.source_files,
        }
        digest = sha256_hex(canonical_json(seed))[:16]
        return f"run-{logical_clock}-{digest}"

    def _injection_targets(
        self,
        injection: Injection,
        candidates: list[CacheEntry],
    ) -> list[CacheEntry]:
        return sorted(
            [
                entry
                for entry in candidates
                if entry.path == injection.path
                and (injection.node is None or entry.node == injection.node)
            ],
            key=candidate_rank_key,
        )

    def _tamper_digest(self, entry: CacheEntry) -> str:
        return hashlib.new(
            entry.digest_algorithm,
            ("injected-tamper:" + entry.content_digest).encode("utf-8"),
        ).hexdigest()

    def _conflict_etag(self, entry: CacheEntry) -> str:
        digest = hashlib.new(
            entry.digest_algorithm,
            ("injected-etag-conflict:" + entry.etag).encode("utf-8"),
        ).hexdigest()
        return f'"{entry.digest_algorithm}:{digest}"'

    def _audit(
        self,
        *,
        path: str,
        node: str | None,
        stage: str,
        injection: Injection | None,
        cache_generation: int | None,
        code: ErrorCode,
        type_name: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "path": path,
            "node": node,
            "stage": stage,
            "injection_seq": injection.seq if injection else None,
            "cache_generation": cache_generation,
            "code": code.value,
            "type": type_name,
            "message": message,
            "details": details or {},
        }

    def _seconds_to_ticks(self, seconds: int, policy: Policy) -> tuple[int, dict[str, Any]]:
        ticks = int(math.ceil(seconds / policy.origin_seconds_per_tick))
        return ticks, {
            "kind": "relative_seconds",
            "raw_seconds": seconds,
            "scale_seconds_per_tick": policy.origin_seconds_per_tick,
            "rounding": "ceil",
            "converted_ticks": ticks,
        }

    def _apply_pre_stage_injections(
        self,
        loaded: ValidatedSamples,
        candidates: list[CacheEntry],
        active_entries: list[CacheEntry],
    ) -> tuple[list[CacheEntry], list[CacheEntry], set[str], list[dict[str, Any]], set[int]]:
        candidate_list = list(candidates)
        active_list = list(active_entries)
        blocked_paths: set[str] = set()
        audits: list[dict[str, Any]] = []
        applied: set[int] = set()
        for injection in sorted(loaded.injections, key=lambda item: item.seq):
            if injection.stage not in {"parse", "merge"}:
                continue
            targets = self._injection_targets(injection, candidates)
            if not targets:
                targets = self._injection_targets(injection, active_entries)
            target = targets[0] if targets else None
            generation = target.cache_generation if target else None
            node = injection.node or (target.node if target else None)
            injected_type = injection.type
            if injected_type in {"content_tamper", "content_tamper_same_version"}:
                code = ErrorCode.INJECTED_CONTENT_TAMPER
                message = "content was tampered while the content version stayed unchanged"
                if target is not None:
                    mutated = target.with_changes(content_digest=self._tamper_digest(target))
                    for index, candidate in enumerate(candidate_list):
                        if candidate.candidate_dict() == target.candidate_dict():
                            candidate_list[index] = mutated
                            break
                    details = {
                        "original_digest": target.content_digest,
                        "injected_digest": mutated.content_digest,
                        "content_version": target.content_version,
                        "sample_files_modified": False,
                    }
                else:
                    details = {"target_entry_found": False, "sample_files_modified": False}
            elif injected_type == "etag_conflict":
                code = ErrorCode.INJECTED_ETAG_CONFLICT
                message = "candidate ETag conflicts with the indexed content digest"
                if target is not None:
                    mutated = target.with_changes(etag=self._conflict_etag(target))
                    for index, candidate in enumerate(candidate_list):
                        if candidate.candidate_dict() == target.candidate_dict():
                            candidate_list[index] = mutated
                            break
                    details = {
                        "original_etag": target.etag,
                        "injected_etag": mutated.etag,
                        "content_version": target.content_version,
                        "sample_files_modified": False,
                    }
                else:
                    details = {"target_entry_found": False, "sample_files_modified": False}
            elif injected_type == "clock_rollback":
                code = ErrorCode.INJECTED_CLOCK_ROLLBACK
                message = "clock rollback injection was applied in the merge stage"
                details = {
                    "rollback_ticks": injection.rollback_ticks,
                    "sample_files_modified": False,
                }
            else:
                code = ErrorCode.INJECTION_INVALID
                message = f"injection type {injected_type} is not meaningful in stage {injection.stage}"
                details = {"sample_files_modified": False}
            audits.append(
                self._audit(
                    path=injection.path,
                    node=node,
                    stage=injection.stage,
                    injection=injection,
                    cache_generation=generation,
                    code=code,
                    type_name=injected_type,
                    message=message,
                    details=details,
                )
            )
            blocked_paths.add(injection.path)
            applied.add(injection.seq)
        return (
            candidate_list,
            active_list,
            blocked_paths,
            audits,
            applied,
        )

    def _apply_freshness_injections(
        self,
        loaded: ValidatedSamples,
        active_entries: list[CacheEntry],
        blocked_paths: set[str],
    ) -> tuple[dict[str, Injection], list[dict[str, Any]], set[str], set[int]]:
        path_injections: dict[str, Injection] = {}
        audits: list[dict[str, Any]] = []
        blocked = set(blocked_paths)
        applied: set[int] = set()
        for injection in sorted(loaded.injections, key=lambda item: item.seq):
            if injection.stage != "freshness":
                continue
            targets = self._injection_targets(injection, active_entries)
            target = targets[0] if targets else None
            generation = target.cache_generation if target else None
            node = injection.node or (target.node if target else None)
            if injection.type == "clock_rollback":
                exceeded = (
                    injection.rollback_ticks
                    > loaded.policy.clock_rollback_tolerance_ticks
                )
                code = ErrorCode.INJECTED_CLOCK_ROLLBACK
                message = (
                    "injected logical clock rollback exceeds tolerance and terminates the path"
                    if exceeded
                    else "injected logical clock rollback is within tolerance and terminates the path"
                )
                details = {
                    "rollback_ticks": injection.rollback_ticks,
                    "tolerance_ticks": loaded.policy.clock_rollback_tolerance_ticks,
                    "exceeded_tolerance": exceeded,
                    "sample_files_modified": False,
                }
            elif injection.type in {"content_tamper", "content_tamper_same_version"}:
                code = ErrorCode.INJECTED_CONTENT_TAMPER
                message = "freshness-stage content tamper terminated the path"
                details = {"content_version": target.content_version if target else None}
            elif injection.type == "etag_conflict":
                code = ErrorCode.INJECTED_ETAG_CONFLICT
                message = "freshness-stage ETag conflict terminated the path"
                details = {}
            else:
                code = ErrorCode.INJECTION_INVALID
                message = f"injection type {injection.type} is not meaningful in stage freshness"
                details = {}
            audits.append(
                self._audit(
                    path=injection.path,
                    node=node,
                    stage="freshness",
                    injection=injection,
                    cache_generation=generation,
                    code=code,
                    type_name=injection.type,
                    message=message,
                    details=details,
                )
            )
            path_injections[injection.path] = injection
            blocked.add(injection.path)
            applied.add(injection.seq)
        return path_injections, audits, blocked, applied

    def _origin_injection_for(
        self,
        injections: Iterable[Injection],
        *,
        path: str,
        node: str,
    ) -> Injection | None:
        matches = [
            injection
            for injection in injections
            if injection.stage == "origin"
            and injection.path == path
            and (injection.node is None or injection.node == node)
        ]
        return sorted(matches, key=lambda item: item.seq)[0] if matches else None

    def _source_mismatch(
        self,
        entry: CacheEntry,
        resource: SourceResource | None,
    ) -> dict[str, Any] | None:
        if resource is None:
            return {
                "reason": "source_resource_missing",
                "cached_version": entry.content_version,
                "cached_digest": entry.content_digest,
            }
        if (
            resource.content_version != entry.content_version
            or resource.content_digest != entry.content_digest
        ):
            return {
                "reason": "source_entity_differs_from_cache",
                "cached_version": entry.content_version,
                "source_version": resource.content_version,
                "cached_digest": entry.content_digest,
                "source_digest": resource.content_digest,
            }
        return None

    def _process_origin_response(
        self,
        *,
        entry: CacheEntry,
        response: Any,
        resource: SourceResource | None,
        policy: Policy,
        logical_clock: int,
        injection: Injection | None,
        audits: list[dict[str, Any]],
    ) -> tuple[CacheEntry, dict[str, Any], dict[str, Any] | None]:
        current = entry
        if response.status == 304:
            mismatch = self._source_mismatch(entry, resource)
            response_etag = response.headers.get("etag")
            if response_etag is not None and response_etag != entry.etag:
                mismatch = {
                    **(mismatch or {}),
                    "reason": "304_etag_differs_from_cached_etag",
                    "response_etag": response_etag,
                    "cached_etag": entry.etag,
                }
            if mismatch is not None:
                audit = self._audit(
                    path=entry.path,
                    node=entry.node,
                    stage="origin",
                    injection=injection,
                    cache_generation=entry.cache_generation,
                    code=ErrorCode.ORIGIN_STALE_304,
                    type_name="stale_304",
                    message="origin returned 304 although the source entity differs from the cache",
                    details=mismatch,
                )
                audits.append(audit)
                return (
                    entry,
                    {
                        "status": "failed",
                        "outcome": "stale_304",
                        "error_code": ErrorCode.ORIGIN_STALE_304.value,
                        "attempts": response.attempts,
                    },
                    None,
                )
            cache_control = parse_cache_control_header(response.headers.get("cache-control"))
            ttl_seconds = cache_control.effective_ttl_seconds
            if "cache-control" not in response.headers and resource is not None:
                ttl_seconds = resource.cache_control.effective_ttl_seconds
            ttl_ticks, conversion = self._seconds_to_ticks(ttl_seconds, policy)
            new_expiry = logical_clock + ttl_ticks
            raw_time = dict(entry.raw_time)
            history = list(raw_time.get("refresh_history", []))
            history.append(
                {
                    "kind": "origin_304",
                    "logical_clock": logical_clock,
                    "ttl_conversion": conversion,
                    "previous_last_verified_clock": entry.last_verified_clock,
                    "previous_expires_at_clock": entry.expires_at_clock,
                }
            )
            raw_time["refresh_history"] = history
            updated = entry.with_changes(
                last_verified_clock=logical_clock,
                expires_at_clock=new_expiry,
                effective_ttl_seconds=ttl_seconds,
                raw_time=raw_time,
            )
            return (
                updated,
                {
                    "status": "refreshed",
                    "outcome": "valid_304",
                    "old_last_verified_clock": entry.last_verified_clock,
                    "new_last_verified_clock": logical_clock,
                    "old_expires_at_clock": entry.expires_at_clock,
                    "new_expires_at_clock": new_expiry,
                    "cache_generation": entry.cache_generation,
                    "attempts": response.attempts,
                    "ttl_conversion": conversion,
                },
                None,
            )

        etag_header = response.headers.get("etag")
        version_header = response.headers.get("x-content-version")
        if not etag_header or version_header is None:
            audits.append(
                self._audit(
                    path=entry.path,
                    node=entry.node,
                    stage="origin",
                    injection=injection,
                    cache_generation=entry.cache_generation,
                    code=ErrorCode.ORIGIN_BAD_RESPONSE,
                    type_name="bad_response",
                    message="200 response is missing ETag or X-Content-Version",
                    details={
                        "etag_present": etag_header is not None,
                        "content_version_present": version_header is not None,
                    },
                )
            )
            return (
                entry,
                {
                    "status": "failed",
                    "outcome": "bad_response",
                    "error_code": ErrorCode.ORIGIN_BAD_RESPONSE.value,
                    "attempts": response.attempts,
                },
                None,
            )
        try:
            response_version = int(version_header)
            if response_version <= 0:
                raise ValueError
        except ValueError:
            audits.append(
                self._audit(
                    path=entry.path,
                    node=entry.node,
                    stage="origin",
                    injection=injection,
                    cache_generation=entry.cache_generation,
                    code=ErrorCode.ORIGIN_BAD_RESPONSE,
                    type_name="bad_response",
                    message="200 response has a non-positive X-Content-Version",
                    details={"x_content_version": version_header},
                )
            )
            return (
                entry,
                {
                    "status": "failed",
                    "outcome": "bad_response",
                    "error_code": ErrorCode.ORIGIN_BAD_RESPONSE.value,
                    "attempts": response.attempts,
                },
                None,
            )
        etag_same = etag_header == entry.etag
        version_same = response_version == entry.content_version
        if etag_same and version_same:
            audits.append(
                self._audit(
                    path=entry.path,
                    node=entry.node,
                    stage="origin",
                    injection=injection,
                    cache_generation=entry.cache_generation,
                    code=ErrorCode.ORIGIN_UNCHANGED_200,
                    type_name="unchanged_200",
                    message="origin returned 200 although ETag and content version are unchanged",
                    details={
                        "etag": etag_header,
                        "content_version": response_version,
                    },
                )
            )
            return (
                entry,
                {
                    "status": "failed",
                    "outcome": "unchanged_200",
                    "error_code": ErrorCode.ORIGIN_UNCHANGED_200.value,
                    "attempts": response.attempts,
                },
                None,
            )
        try:
            algorithm, body_digest, _parsed_etag, weak = parse_etag(etag_header)
        except BusinessError as exc:
            audits.append(
                self._audit(
                    path=entry.path,
                    node=entry.node,
                    stage="origin",
                    injection=injection,
                    cache_generation=entry.cache_generation,
                    code=ErrorCode.ORIGIN_BAD_RESPONSE,
                    type_name="bad_response",
                    message="200 response contains a malformed ETag",
                    details={"etag": etag_header, "validation_error": exc.to_dict()},
                )
            )
            return (
                entry,
                {
                    "status": "failed",
                    "outcome": "bad_response",
                    "error_code": ErrorCode.ORIGIN_BAD_RESPONSE.value,
                    "attempts": response.attempts,
                },
                None,
            )
        actual_body_digest = hashlib.new(algorithm, response.body).hexdigest()
        body_matches_etag = actual_body_digest == body_digest
        if version_same and (not etag_same or body_digest != entry.content_digest):
            audits.append(
                self._audit(
                    path=entry.path,
                    node=entry.node,
                    stage="origin",
                    injection=injection,
                    cache_generation=entry.cache_generation,
                    code=ErrorCode.INJECTED_CONTENT_TAMPER,
                    type_name="content_tamper_same_version",
                    message="origin changed content while the content version stayed unchanged",
                    details={
                        "cached_etag": entry.etag,
                        "origin_etag": etag_header,
                        "content_version": entry.content_version,
                        "cached_digest": entry.content_digest,
                        "origin_digest": body_digest,
                        "body_matches_origin_etag": body_matches_etag,
                    },
                )
            )
            return (
                entry,
                {
                    "status": "failed",
                    "outcome": "content_tamper_same_version",
                    "error_code": ErrorCode.INJECTED_CONTENT_TAMPER.value,
                    "attempts": response.attempts,
                },
                None,
            )
        if not body_matches_etag:
            audits.append(
                self._audit(
                    path=entry.path,
                    node=entry.node,
                    stage="origin",
                    injection=injection,
                    cache_generation=entry.cache_generation,
                    code=ErrorCode.ORIGIN_BAD_RESPONSE,
                    type_name="etag_body_mismatch",
                    message="origin ETag does not match the response body digest",
                    details={
                        "etag_digest": body_digest,
                        "body_digest": actual_body_digest,
                    },
                )
            )
            return (
                entry,
                {
                    "status": "failed",
                    "outcome": "bad_response",
                    "error_code": ErrorCode.ORIGIN_BAD_RESPONSE.value,
                    "attempts": response.attempts,
                },
                None,
            )
        if etag_same != version_same:
            audits.append(
                self._audit(
                    path=entry.path,
                    node=entry.node,
                    stage="origin",
                    injection=injection,
                    cache_generation=entry.cache_generation,
                    code=ErrorCode.ORIGIN_PARTIAL_CHANGE,
                    type_name="partial_change",
                    message="only one of ETag and content version changed",
                    details={
                        "etag_changed": not etag_same,
                        "version_changed": not version_same,
                        "cached_etag": entry.etag,
                        "origin_etag": etag_header,
                        "cached_version": entry.content_version,
                        "origin_version": response_version,
                    },
                )
            )
            return (
                entry,
                {
                    "status": "failed",
                    "outcome": "partial_change",
                    "error_code": ErrorCode.ORIGIN_PARTIAL_CHANGE.value,
                    "attempts": response.attempts,
                },
                None,
            )
        cache_control = parse_cache_control_header(response.headers.get("cache-control"))
        ttl_seconds = cache_control.effective_ttl_seconds
        if "cache-control" not in response.headers and resource is not None:
            ttl_seconds = resource.cache_control.effective_ttl_seconds
        ttl_ticks, conversion = self._seconds_to_ticks(ttl_seconds, policy)
        new_expiry = logical_clock + ttl_ticks
        new_generation = entry.cache_generation + 1
        raw_time = {
            "kind": "origin_200",
            "logical_clock": logical_clock,
            "ttl_conversion": conversion,
            "response_headers": {
                "etag": etag_header,
                "x_content_version": response_version,
            },
            "prior_entry_raw_time": entry.raw_time,
            "prior_last_verified_clock": entry.last_verified_clock,
            "prior_expires_at_clock": entry.expires_at_clock,
        }
        updated = entry.with_changes(
            digest_algorithm=algorithm,
            content_digest=body_digest,
            content_version=response_version,
            etag=etag_header,
            last_verified_clock=logical_clock,
            expires_at_clock=new_expiry,
            cache_generation=new_generation,
            source_version="origin-200",
            source_ref=f"http-origin:{entry.path}",
            raw_time=raw_time,
            effective_ttl_seconds=ttl_seconds,
            weak_etag=weak,
        )
        replacement = {
            "node": entry.node,
            "path": entry.path,
            "cache_generation": new_generation,
            "from_generation": entry.cache_generation,
            "result_hash": sha256_hex(canonical_json(updated.candidate_dict())),
        }
        return (
            updated,
            {
                "status": "replaced",
                "outcome": "full_replacement_200",
                "old_etag": entry.etag,
                "new_etag": etag_header,
                "old_content_version": entry.content_version,
                "new_content_version": response_version,
                "old_cache_generation": entry.cache_generation,
                "new_cache_generation": new_generation,
                "new_expires_at_clock": new_expiry,
                "attempts": response.attempts,
                "ttl_conversion": conversion,
            },
            replacement,
        )

    def _origin_error_audit(
        self,
        *,
        entry: CacheEntry,
        error: OriginRequestError,
        injection: Injection | None,
    ) -> dict[str, Any]:
        details = dict(error.details)
        details.update(
            {
                "attempts": error.attempts,
                "retry_budget": None,
                "retry_exhausted": error.retry_exhausted,
            }
        )
        return self._audit(
            path=entry.path,
            node=entry.node,
            stage="origin",
            injection=injection,
            cache_generation=entry.cache_generation,
            code=error.code,
            type_name=error.code.value.split("_", 1)[1].lower(),
            message=error.message,
            details=details,
        )

    def _run_origin_checks(
        self,
        *,
        loaded: ValidatedSamples,
        active_entries: list[CacheEntry],
        selected: list[CacheEntry],
        logical_clock: int,
        blocked_paths: set[str],
        audits: list[dict[str, Any]],
        origin_base_url: str,
        applied_injections: set[int],
    ) -> tuple[dict[tuple[str, str], CacheEntry], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        updated_by_key = {entry.key: entry for entry in active_entries}
        records: list[dict[str, Any]] = []
        replacements: list[dict[str, Any]] = []
        watermarks: list[dict[str, Any]] = []
        origin_injections = [
            injection for injection in loaded.injections if injection.stage == "origin"
        ]
        for entry in sorted(selected, key=lambda item: (item.node, item.path)):
            injection = self._origin_injection_for(
                origin_injections,
                path=entry.path,
                node=entry.node,
            )
            resource = loaded.resources.get(entry.path)
            if resource is None:
                audits.append(
                    self._audit(
                        path=entry.path,
                        node=entry.node,
                        stage="origin",
                        injection=injection,
                        cache_generation=entry.cache_generation,
                        code=ErrorCode.ORIGIN_BAD_RESPONSE,
                        type_name="missing_source_resource",
                        message="conditional revalidation cannot find a source manifest entry",
                        details={},
                    )
                )
                records.append(
                    {
                        "node": entry.node,
                        "path": entry.path,
                        "status": "failed",
                        "outcome": "missing_source_resource",
                        "error_code": ErrorCode.ORIGIN_BAD_RESPONSE.value,
                        "cache_generation": entry.cache_generation,
                    }
                )
                blocked_paths.add(entry.path)
                continue
            if not resource.cacheable or not entry.cacheable:
                records.append(
                    {
                        "node": entry.node,
                        "path": entry.path,
                        "status": "skipped",
                        "outcome": "uncacheable",
                        "cache_generation": entry.cache_generation,
                    }
                )
                continue
            try:
                response = request_origin_with_retries(
                    base_url=origin_base_url,
                    entry=entry,
                    policy=loaded.policy,
                )
            except OriginRequestError as error:
                audit = self._origin_error_audit(
                    entry=entry,
                    error=error,
                    injection=injection,
                )
                audit["details"]["retry_budget"] = loaded.policy.origin_retry_budget
                audits.append(audit)
                if injection is not None:
                    applied_injections.add(injection.seq)
                records.append(
                    {
                        "node": entry.node,
                        "path": entry.path,
                        "status": "failed",
                        "outcome": error.code.value,
                        "error_code": error.code.value,
                        "attempts": error.attempts,
                        "retry_budget": loaded.policy.origin_retry_budget,
                        "retry_exhausted": error.retry_exhausted,
                        "cache_generation": entry.cache_generation,
                    }
                )
                watermarks.append(
                    {
                        "node": entry.node,
                        "path": entry.path,
                        "last_logical_clock": logical_clock,
                        "last_status": error.code.value,
                        "last_origin_etag": None,
                        "last_result_hash": sha256_hex(
                            canonical_json(
                                {
                                    "node": entry.node,
                                    "path": entry.path,
                                    "code": error.code.value,
                                    "attempts": error.attempts,
                                }
                            )
                        ),
                    }
                )
                blocked_paths.add(entry.path)
                continue
            try:
                updated, record, replacement = self._process_origin_response(
                    entry=entry,
                    response=response,
                    resource=resource,
                    policy=loaded.policy,
                    logical_clock=logical_clock,
                    injection=injection,
                    audits=audits,
                )
            except OriginRequestError as error:
                audit = self._origin_error_audit(
                    entry=entry,
                    error=error,
                    injection=injection,
                )
                audits.append(audit)
                records.append(
                    {
                        "node": entry.node,
                        "path": entry.path,
                        "status": "failed",
                        "outcome": error.code.value,
                        "error_code": error.code.value,
                        "attempts": response.attempts,
                        "cache_generation": entry.cache_generation,
                    }
                )
                blocked_paths.add(entry.path)
                continue
            if injection is not None:
                applied_injections.add(injection.seq)
            updated_by_key[updated.key] = updated
            record.update(
                {
                    "node": entry.node,
                    "path": entry.path,
                    "cache_generation": updated.cache_generation,
                    "content_version": updated.content_version,
                    "injection_seq": injection.seq if injection else None,
                    "injected_type": injection.type if injection else None,
                }
            )
            records.append(record)
            if replacement is not None:
                replacements.append(replacement)
            watermarks.append(
                {
                    "node": updated.node,
                    "path": updated.path,
                    "last_logical_clock": logical_clock,
                    "last_status": record["outcome"],
                    "last_origin_etag": updated.etag,
                    "last_result_hash": sha256_hex(canonical_json(updated.candidate_dict())),
                }
            )
            if record["status"] == "failed":
                blocked_paths.add(entry.path)
        return updated_by_key, records, replacements, watermarks, audits

    def _receipts(self, entries: Iterable[CacheEntry]) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        for entry in sorted(entries, key=lambda item: (item.node, item.path)):
            result_hash = sha256_hex(canonical_json(entry.candidate_dict()))
            receipt_key = sha256_hex(
                canonical_json(
                    {
                        "node": entry.node,
                        "path": entry.path,
                        "cache_generation": entry.cache_generation,
                        "content_version": entry.content_version,
                        "content_digest": entry.content_digest,
                        "result_hash": result_hash,
                    }
                )
            )
            receipts.append(
                {
                    "receipt_key": receipt_key,
                    "node": entry.node,
                    "path": entry.path,
                    "cache_generation": entry.cache_generation,
                    "result_hash": result_hash,
                }
            )
        return receipts

    def _finish_audits(self, audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(
            audits,
            key=lambda audit: (
                _STAGE_ORDER.get(audit["stage"], 9),
                audit.get("injection_seq")
                if audit.get("injection_seq") is not None
                else 10**9,
                audit.get("node") or "",
                audit["path"],
                audit["code"],
                audit["message"],
            ),
        )
        for index, audit in enumerate(ordered, start=1):
            audit["seq"] = index
        return ordered

    def _unreached_injection_audits(
        self,
        loaded: ValidatedSamples,
        applied: set[int],
    ) -> list[dict[str, Any]]:
        audits: list[dict[str, Any]] = []
        for injection in sorted(loaded.injections, key=lambda item: item.seq):
            if injection.seq in applied:
                continue
            audits.append(
                self._audit(
                    path=injection.path,
                    node=injection.node,
                    stage=injection.stage,
                    injection=injection,
                    cache_generation=None,
                    code=ErrorCode.INJECTION_INVALID,
                    type_name=injection.type,
                    message="injection target was not reached; the path was not adjudicated",
                    details={
                        "reason": "no matching entry within the stage or validation budget",
                        "sample_files_modified": False,
                    },
                )
            )
        return audits

    def _compute(
        self,
        *,
        loaded: ValidatedSamples,
        run_id: str,
        base_version: int,
        planned_version: int,
        logical_clock: int,
        persisted_entries: list[CacheEntry],
        origin_base_url: str,
    ) -> dict[str, Any]:
        snapshot_candidates = list(loaded.snapshot_candidates)
        active_entries = list(persisted_entries)
        candidates, active_entries, blocked_paths, audits, applied = (
            self._apply_pre_stage_injections(
                loaded,
                snapshot_candidates,
                active_entries,
            )
        )
        snapshot_blocked_paths = set(blocked_paths)
        snapshot_arbitrations = [
            decision
            for decision in arbitrate_snapshot_entries(candidates)
            if decision["target"].split(":", 1)[-1] not in blocked_paths
            and decision["target"] not in blocked_paths
        ]
        if not persisted_entries:
            key_winners = resolve_key_winners(candidates)
            active_entries = [
                entry
                for key, entry in sorted(key_winners.items())
                if entry.path not in blocked_paths
            ]
        active_by_key = {entry.key: entry for entry in active_entries}
        freshness_injections, freshness_audits, blocked_paths, freshness_applied = (
            self._apply_freshness_injections(
                loaded,
                active_entries,
                blocked_paths,
            )
        )
        applied |= freshness_applied
        audits.extend(freshness_audits)
        freshness_records: list[dict[str, Any]] = []
        for entry in sorted(active_by_key.values(), key=lambda item: (item.node, item.path)):
            if entry.path in blocked_paths:
                continue
            result = freshness_at(
                entry,
                logical_clock,
                loaded.policy.freshness_tolerance_ticks,
            )
            freshness_records.append(
                {
                    "node": entry.node,
                    "path": entry.path,
                    "cache_generation": entry.cache_generation,
                    "content_version": entry.content_version,
                    "next_validation_due_clock": (
                        entry.last_verified_clock + loaded.policy.validation_cycle_ticks
                    ),
                    **result.to_dict(),
                }
            )
        selected, skipped = select_revalidation_entries(
            active_by_key.values(),
            nodes=loaded.nodes,
            logical_clock=logical_clock,
            tolerance=loaded.policy.freshness_tolerance_ticks,
            blocked_paths=blocked_paths,
        )
        (
            updated_by_key,
            revalidations,
            replacements,
            origin_watermarks,
            audits,
        ) = self._run_origin_checks(
            loaded=loaded,
            active_entries=list(active_by_key.values()),
            selected=selected,
            logical_clock=logical_clock,
            blocked_paths=blocked_paths,
            audits=audits,
            origin_base_url=origin_base_url,
            applied_injections=applied,
        )
        algorithm_selections, checked_entries, algorithm_issues = algorithm_records(
            updated_by_key.values(),
            snapshot_candidates=[
                entry
                for entry in candidates
                if entry.path not in snapshot_blocked_paths
            ],
            policy=loaded.policy,
        )
        by_node: dict[str, list[CacheEntry]] = defaultdict(list)
        for entry in updated_by_key.values():
            by_node[entry.node].append(entry)
        evictions: list[dict[str, Any]] = []
        retention: list[dict[str, Any]] = []
        retained_entries: list[CacheEntry] = []
        for node_name in sorted(loaded.nodes):
            kept, node_evictions, node_retention = evict_node(
                loaded.nodes[node_name],
                by_node.get(node_name, []),
                logical_clock=logical_clock,
                tolerance=loaded.policy.freshness_tolerance_ticks,
            )
            retained_entries.extend(kept)
            evictions.extend(node_evictions)
            retention.extend(node_retention)
        active_path_groups: dict[str, list[CacheEntry]] = defaultdict(list)
        for entry in retained_entries:
            active_path_groups[entry.path].append(entry)
        active_arbitrations = [
            arbitrate_candidates("active_path", path, group)
            for path, group in sorted(active_path_groups.items())
            if len(group) > 1
        ]
        audits.extend(self._unreached_injection_audits(loaded, applied))
        audits = self._finish_audits(audits)
        receipts = self._receipts(retained_entries)
        code_counts = Counter(audit["code"] for audit in audits)
        summary = {
            "index_version": planned_version,
            "entries": len(retained_entries),
            "snapshot_candidates": len(candidates),
            "snapshot_arbitrations": len(snapshot_arbitrations),
            "active_arbitrations": len(active_arbitrations),
            "freshness_records": len(freshness_records),
            "fresh": sum(1 for record in freshness_records if record["status"] == "fresh"),
            "boundary": sum(1 for record in freshness_records if record["status"] == "boundary"),
            "expired": sum(1 for record in freshness_records if record["status"] == "expired"),
            "revalidated": len(revalidations),
            "replaced": sum(1 for record in revalidations if record["status"] == "replaced"),
            "refreshed_304": sum(
                1
                for record in revalidations
                if record.get("outcome") == "valid_304"
            ),
            "failed_origin": sum(1 for record in revalidations if record["status"] == "failed"),
            "validation_budget_skipped": len(skipped),
            "evicted": len(evictions),
            "audits": len(audits),
            "algorithm_exact": sum(
                1 for record in algorithm_selections if record["mode"] == "exact_per_entry"
            ),
            "algorithm_sampled": sum(
                1
                for record in algorithm_selections
                if record["mode"] == "sampled_digest_buckets"
            ),
            "algorithm_issues": len(algorithm_issues),
        }
        report = {
            "ok": True,
            "run_id": run_id,
            "status": "committed",
            "logical_clock": logical_clock,
            "logical_clock_step": loaded.policy.logical_clock_step,
            "recommended_next_logical_clock": logical_clock + loaded.policy.logical_clock_step,
            "base_index_version": base_version,
            "index_version": planned_version,
            "snapshot_generations": {
                "v1": loaded.v1_generation,
                "v2": loaded.v2_generation,
            },
            "source_files": loaded.source_files,
            "policy": loaded.policy.to_dict(),
            "summary": summary,
            "error_code_counts": dict(sorted(code_counts.items())),
            "entries": [
                entry.candidate_dict()
                for entry in sorted(retained_entries, key=lambda item: (item.node, item.path))
            ],
            "freshness": sorted(
                freshness_records,
                key=lambda record: (record["node"], record["path"]),
            ),
            "snapshot_arbitrations": snapshot_arbitrations,
            "active_arbitrations": active_arbitrations,
            "revalidations": sorted(
                revalidations,
                key=lambda record: (record["node"], record["path"]),
            ),
            "validation_budget_skipped": sorted(
                skipped,
                key=lambda record: (record["node"], record["path"]),
            ),
            "algorithm_selections": sorted(
                algorithm_selections,
                key=lambda record: (record["scope"], record["target"]),
            ),
            "algorithm_issues": algorithm_issues,
            "evictions": sorted(
                evictions,
                key=lambda record: (record["node"], record["path"]),
            ),
            "retention": sorted(
                retention,
                key=lambda record: (record["node"], record["path"]),
            ),
            "audits": audits,
            "injections": [injection.to_dict() for injection in loaded.injections],
            "origin_watermarks": sorted(
                origin_watermarks,
                key=lambda record: (record["node"], record["path"]),
            ),
        }
        result = {
            "run_id": run_id,
            "plan_hash": None,
            "base_version": base_version,
            "planned_version": planned_version,
            "logical_clock": logical_clock,
            "clock_rollback_tolerance_ticks": loaded.policy.clock_rollback_tolerance_ticks,
            "snapshot_generation": loaded.snapshot_generation,
            "entries": [entry.candidate_dict() for entry in retained_entries],
            "audits": audits,
            "arbitrations": snapshot_arbitrations + active_arbitrations,
            "evictions": evictions,
            "retention": retention,
            "replacements": replacements,
            "receipts": receipts,
            "origin_watermarks": origin_watermarks,
            "report": report,
        }
        return result

    def run(
        self,
        *,
        logical_clock: int | None = None,
        run_id: str | None = None,
        fault_point: str | None = None,
    ) -> dict[str, Any]:
        loaded = load_samples(self.samples_dir)
        clock = loaded.policy.logical_clock if logical_clock is None else logical_clock
        if isinstance(clock, bool) or not isinstance(clock, int) or clock < 0:
            raise BusinessError(
                ErrorCode.INVALID_ARGUMENT,
                "logical_clock must be a non-negative integer",
                details={"logical_clock": clock},
            )
        selected_run_id = run_id or self._default_run_id(loaded, clock)
        if fault_point not in {None, "after_prepare", "during_apply"}:
            raise BusinessError(
                ErrorCode.INVALID_ARGUMENT,
                "unsupported fault point",
                details={"fault_point": fault_point},
            )
        with state_process_lock(self.state_dir):
            store = StateStore(self.state_dir)
            store.initialize()
            existing = store.get_run(selected_run_id)
            if existing is not None and existing["status"] == "committed":
                report = dict(existing["result"]["report"])
                report["status"] = "idempotent_existing_commit"
                report["index_version"] = existing["planned_version"]
                return report
            if existing is not None and existing["status"] == "prepared":
                result = store.apply_prepared(selected_run_id)
                report = dict(result["report"])
                report["status"] = "recovered_prepared_commit"
                return report
            last_clock = store.logical_watermark()
            if (
                last_clock is not None
                and clock < last_clock - loaded.policy.clock_rollback_tolerance_ticks
            ):
                raise BusinessError(
                    ErrorCode.CLOCK_ROLLBACK,
                    "logical clock rollback exceeds the configured tolerance",
                    stage="freshness",
                    details={
                        "last_logical_clock": last_clock,
                        "requested_logical_clock": clock,
                        "tolerance_ticks": loaded.policy.clock_rollback_tolerance_ticks,
                    },
                )
            recovery = store.recover_locked()
            base_version = store.current_version()
            planned_version = base_version + 1
            if existing is not None and existing["status"] == "planned":
                planned_version = int(existing["planned_version"])
                if planned_version != base_version + 1:
                    raise BusinessError(
                        ErrorCode.INDEX_VERSION_ROLLBACK,
                        "recovered state no longer permits the planned index version",
                        details={
                            "planned_version": planned_version,
                            "base_version": base_version,
                        },
                    )
            plan = {
                "run_id": selected_run_id,
                "logical_clock": clock,
                "base_version": base_version,
                "planned_version": planned_version,
                "samples_dir": self.samples_dir,
                "source_files": loaded.source_files,
                "v1_generation": loaded.v1_generation,
                "v2_generation": loaded.v2_generation,
                "injections": [injection.to_dict() for injection in loaded.injections],
                "policy": loaded.policy.to_dict(),
                "origin_mode": "external-loopback" if self.origin_base_url else "embedded-loopback",
            }
            plan_hash = sha256_hex(canonical_json(plan))
            store.record_plan(
                run_id=selected_run_id,
                base_version=base_version,
                planned_version=planned_version,
                plan=plan,
            )
            committed = store.read_committed_snapshot()
            persisted_entries = [
                CacheEntry.from_mapping(row) for row in committed["entries"]
            ]
            if self.origin_base_url:
                result = self._compute(
                    loaded=loaded,
                    run_id=selected_run_id,
                    base_version=base_version,
                    planned_version=planned_version,
                    logical_clock=clock,
                    persisted_entries=persisted_entries,
                    origin_base_url=self.origin_base_url,
                )
                result["plan_hash"] = plan_hash
                result["report"]["recovery"] = recovery
            else:
                with LocalOriginServer(loaded.resources, policy=loaded.policy) as server:
                    for injection in sorted(loaded.injections, key=lambda item: item.seq):
                        if injection.stage != "origin":
                            continue
                        server.set_behavior(
                            injection.path,
                            behavior_for_injection(injection.type),
                            node=injection.node,
                            delay_ms=injection.delay_ms,
                            details=injection.details,
                        )
                    result = self._compute(
                        loaded=loaded,
                        run_id=selected_run_id,
                        base_version=base_version,
                        planned_version=planned_version,
                        logical_clock=clock,
                        persisted_entries=persisted_entries,
                        origin_base_url=server.base_url,
                    )
                result["plan_hash"] = plan_hash
                result["report"]["recovery"] = recovery
            result["report"]["origin_mode"] = plan["origin_mode"]
            store.prepare_result(result)
            if fault_point == "after_prepare":
                raise SimulatedCrash("simulated crash after prepared result was journaled")
            applied = store.apply_prepared(
                selected_run_id,
                fault_point="during_apply" if fault_point == "during_apply" else None,
            )
            return applied["report"]

    def recover(self) -> dict[str, Any]:
        store = StateStore(self.state_dir)
        recovery = store.recover()
        return {
            "ok": True,
            **recovery,
            "snapshot": store.read_committed_snapshot(),
        }

    def inspect(self) -> dict[str, Any]:
        store = StateStore(self.state_dir)
        return {"ok": True, **store.inspect()}


def normalize_optional_path(value: str | None) -> str | None:
    return normalize_path(value) if value is not None else None

