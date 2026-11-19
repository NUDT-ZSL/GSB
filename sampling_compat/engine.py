"""Application service coordinating parsing, validation, sampling and snapshots."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from .adjudication import adjudicate_request
from .config_io import read_config, read_config_file
from .errors import BusinessError, ErrorCode
from .migration import downgrade_v2_to_v1, upgrade_v1_to_v2
from .models import InferenceRequest, SamplingConfig, VocabEntry
from .normalization import normalize_params
from .requests import load_requests_jsonl
from .sampler import ALGORITHM_VERSION, sample_tokens, token_sequence_checksum
from .snapshots import SnapshotRepository, probe_snapshot_file
from .utils import checksum_obj
from .vocabulary import (
    load_vocab_table,
    require_vocab_entry,
    vocab_table_snapshot_content,
)


class SamplingApplication:
    def __init__(
        self,
        *,
        samples_dir: Path | None = None,
        snapshot_path: Path | None = None,
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        self.root = root
        self.samples_dir = (samples_dir or root / "samples").resolve()
        self.vocabulary_path = self.samples_dir / "vocabularies.json"
        self.requests_path = self.samples_dir / "requests.jsonl"
        self.snapshot_path = (snapshot_path or root / "runtime" / "snapshots.json").resolve()
        self.snapshot_repo = SnapshotRepository(self.snapshot_path)
        self.vocab_table = load_vocab_table(self.vocabulary_path)
        self.requests = load_requests_jsonl(self.requests_path)
        self.errors: list[dict[str, Any]] = []
        self.decisions: dict[str, dict[str, Any]] = {}
        self.normalized_by_request: dict[str, dict[str, Any]] = {}
        self.migration_records: dict[str, dict[str, Any]] = {}

    def request_ids(self) -> list[str]:
        return list(self.requests)

    def get_request(self, request_id: str) -> InferenceRequest:
        request = self.requests.get(request_id)
        if request is None:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"request '{request_id}' does not exist",
                request_id=request_id,
                field="request_id",
            )
        return request

    def preferred_version(self, request: InferenceRequest) -> str:
        for version in ("v2", "v3", "v1"):
            if version in request.sampling_profiles:
                return version
        return request.profile_versions()[0]

    def record_error(self, error: BusinessError, *, context: str) -> None:
        entry = {
            "context": context,
            "error": error.to_dict(),
        }
        self.errors.append(entry)
        if len(self.errors) > 100:
            del self.errors[:-100]

    @property
    def last_error(self) -> dict[str, Any] | None:
        return self.errors[-1] if self.errors else None

    def inspect_profiles(
        self,
        request_id: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        request = self.get_request(request_id)
        selected = version or self.preferred_version(request)
        try:
            return self._inspect_profiles(request, selected)
        except BusinessError as error:
            self.record_error(error, context=f"inspect:{request_id}:{selected}")
            raise

    def _inspect_profiles(
        self, request: InferenceRequest, version: str
    ) -> dict[str, Any]:
        raw = request.sampling_profiles.get(version)
        if raw is None:
            raise BusinessError(
                ErrorCode.VERSION_UNSUPPORTED,
                f"request has no '{version}' sampling profile",
                request_id=request.request_id,
                field="sampling_profile",
                version=version,
            )
        config = read_config(raw, request_id=request.request_id)
        vocab_entry = self._validate_binding(request, config)
        normalized = normalize_params(
            config,
            vocabulary_size=vocab_entry.size,
            request_id=request.request_id,
        )
        return {
            "request_id": request.request_id,
            "view_version": version,
            "raw_config": raw,
            "parsed_config": config.to_dict(),
            "normalized": normalized.to_dict(),
            "normalized_checksum": checksum_obj(normalized.to_dict()),
            "vocab_version": vocab_entry.version,
            "vocab_size": vocab_entry.size,
            "available_versions": list(request.profile_versions()),
        }

    def run_request(
        self,
        request_id: str,
        version: str | None = None,
        *,
        seed: int | None = None,
        persist: bool = False,
        record_error: bool = True,
    ) -> dict[str, Any]:
        request = self.get_request(request_id)
        selected = version or self.preferred_version(request)
        try:
            decision = self._run_request(
                request,
                selected,
                seed=request.sampling_seed if seed is None else int(seed),
            )
        except BusinessError as error:
            if record_error:
                self.record_error(
                    error,
                    context=f"run:{request_id}:{selected}",
                )
            raise
        self.decisions[request_id] = decision
        self.normalized_by_request[request_id] = {
            "config_version": selected,
            "tokenizer_version": request.tokenizer_version,
            "normalized": decision["normalized_params"],
            "normalized_checksum": decision["normalized_params_checksum"],
        }
        if persist:
            self._persist_current_artifacts()
        return decision

    def _run_request(
        self,
        request: InferenceRequest,
        version: str,
        *,
        seed: int,
    ) -> dict[str, Any]:
        raw = request.sampling_profiles.get(version)
        if raw is None:
            raise BusinessError(
                ErrorCode.VERSION_UNSUPPORTED,
                f"request has no '{version}' sampling profile",
                request_id=request.request_id,
                field="sampling_profile",
                version=version,
            )
        config = read_config(raw, request_id=request.request_id)
        vocab_entry = self._validate_binding(request, config)
        normalized = normalize_params(
            config,
            vocabulary_size=vocab_entry.size,
            request_id=request.request_id,
        )
        self._validate_prompt(request, vocab_entry)
        logits = self._validate_logits(request, vocab_entry)

        if logits is None:
            token_ids: list[int] = []
            token_sha256 = token_sequence_checksum(token_ids)
            traces: list[dict[str, Any]] = []
            sampling_status = "NO_LOGITS"
        else:
            result = sample_tokens(
                request_id=request.request_id,
                prompt_tokens=request.prompt_tokens,
                logits=logits,
                params=normalized,
                seed=seed,
                config_version=version,
            )
            token_ids = result.token_ids
            token_sha256 = result.token_sha256
            traces = result.step_traces
            sampling_status = "SAMPLED"

        core = {
            "artifact_format": "sampling-decision/v1",
            "artifact_version": 1,
            "request_id": request.request_id,
            "config_version": version,
            "config_source": f"request.sampling_profile.{version}",
            "raw_config": raw,
            "parsed_config": config.to_dict(),
            "normalized_params": normalized.to_dict(),
            "normalized_params_checksum": checksum_obj(normalized.to_dict()),
            "sampling_seed": seed,
            "seed_source": request.seed_source,
            "sampling_status": sampling_status,
            "sampling_algorithm": ALGORITHM_VERSION,
            "float_dtype": "float64",
            "vocab_version": vocab_entry.version,
            "vocab_size": vocab_entry.size,
            "logits_steps": 0 if logits is None else int(logits.shape[0]),
            "token_ids": token_ids,
            "token_sha256": token_sha256,
            "step_traces": traces,
        }
        core["artifact_checksum"] = checksum_obj(core)
        return core

    def _validate_binding(
        self,
        request: InferenceRequest,
        config: SamplingConfig,
    ) -> VocabEntry:
        request_entry = require_vocab_entry(
            self.vocab_table,
            request.tokenizer_version,
            request_id=request.request_id,
            field_name="tokenizer_version",
        )
        if config.vocab_version is not None:
            require_vocab_entry(
                self.vocab_table,
                config.vocab_version,
                request_id=request.request_id,
                field_name="vocab_version",
            )
            if config.vocab_version != request.tokenizer_version:
                raise BusinessError(
                    ErrorCode.VOCAB_VERSION_MISMATCH,
                    "sampling configuration vocabulary binding differs from request tokenizer_version",
                    request_id=request.request_id,
                    field="vocab_version",
                    original_value=config.vocab_version,
                    details={
                        "request_tokenizer_version": request.tokenizer_version,
                        "config_version": config.version,
                    },
                )
        return request_entry

    def _validate_prompt(
        self,
        request: InferenceRequest,
        vocab_entry: VocabEntry,
    ) -> None:
        for index, token_id in enumerate(request.prompt_tokens):
            if token_id < 0 or token_id >= vocab_entry.size:
                raise BusinessError(
                    ErrorCode.TOKEN_RANGE,
                    f"prompt token id {token_id} is outside vocabulary range",
                    request_id=request.request_id,
                    field="prompt_tokens",
                    original_value=token_id,
                    details={
                        "token_index": index,
                        "vocab_version": vocab_entry.version,
                        "vocab_size": vocab_entry.size,
                        "valid_range": [0, vocab_entry.size - 1],
                    },
                )

    def _validate_logits(
        self,
        request: InferenceRequest,
        vocab_entry: VocabEntry,
    ) -> np.ndarray | None:
        if request.logits is None:
            return None
        if not isinstance(request.logits, list) or not request.logits:
            raise BusinessError(
                ErrorCode.LOGITS_SHAPE,
                "logits sample must be a non-empty step matrix",
                request_id=request.request_id,
                field="logits",
                step=0,
                details={"expected_shape": "[steps, " + str(vocab_entry.size) + "]"},
            )
        for step, row in enumerate(request.logits):
            if not isinstance(row, list) or len(row) != vocab_entry.size:
                actual = len(row) if isinstance(row, list) else "not-a-row"
                raise BusinessError(
                    ErrorCode.LOGITS_SHAPE,
                    f"logits row at step {step} does not match vocabulary width",
                    request_id=request.request_id,
                    field="logits",
                    step=step,
                    details={
                        "expected_width": vocab_entry.size,
                        "actual_width": actual,
                        "vocab_version": vocab_entry.version,
                    },
                )
        try:
            logits = np.asarray(request.logits, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                "logits sample contains non-numeric values",
                request_id=request.request_id,
                field="logits",
                step=0,
                details={"reason": str(exc)},
            ) from exc
        if logits.ndim != 2 or logits.shape[1] != vocab_entry.size:
            raise BusinessError(
                ErrorCode.LOGITS_SHAPE,
                "logits matrix has an invalid shape",
                request_id=request.request_id,
                field="logits",
                step=0,
                details={
                    "expected_shape": f"[steps, {vocab_entry.size}]",
                    "actual_shape": list(logits.shape),
                },
            )
        finite_rows = np.isfinite(logits).all(axis=1)
        if not bool(finite_rows.all()):
            bad_step = int(np.flatnonzero(~finite_rows)[0])
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                "logits sample contains NaN or infinity",
                request_id=request.request_id,
                field="logits",
                step=bad_step,
                details={"dtype": "float64"},
            )
        return logits

    def adjudicate(
        self,
        request_id: str,
        *,
        versions: tuple[str, ...] | None = None,
        record_error: bool = True,
    ) -> dict[str, Any]:
        request = self.get_request(request_id)
        try:
            request_entry = require_vocab_entry(
                self.vocab_table,
                request.tokenizer_version,
                request_id=request.request_id,
            )
            self._validate_prompt(request, request_entry)
            logits = self._validate_logits(request, request_entry)
            if logits is None:
                raise BusinessError(
                    ErrorCode.LOGITS_SHAPE,
                    "cross-version adjudication requires a logits sample",
                    request_id=request.request_id,
                    field="logits",
                    step=0,
                )
            return adjudicate_request(
                request,
                vocab_entry=request_entry,
                logits=logits,
                versions=versions,
            )
        except BusinessError as error:
            if record_error:
                self.record_error(error, context=f"adjudicate:{request_id}")
            raise

    def run_all(
        self,
        *,
        version: str | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        decisions: dict[str, dict[str, Any]] = {}
        for request_id, request in self.requests.items():
            selected = version or self.preferred_version(request)
            decisions[request_id] = self.run_request(
                request_id,
                selected,
                record_error=True,
            )

        migrations: dict[str, dict[str, Any]] = {}
        for request_id, request in self.requests.items():
            if "v1" in request.sampling_profiles and "v2" in request.sampling_profiles:
                migrations[request_id] = self._build_migration_record(request)
        self.migration_records = migrations

        snapshot_versions: dict[str, int] = {}
        if persist:
            committed = self._persist_current_artifacts()
            snapshot_versions = {
                kind: envelope["version"] for kind, envelope in committed.items()
            }
        return {
            "request_count": len(decisions),
            "decisions": decisions,
            "migrations": migrations,
            "snapshot_versions": snapshot_versions,
            "snapshot_path": str(self.snapshot_path),
        }

    def _persist_current_artifacts(self) -> dict[str, dict[str, Any]]:
        updates: dict[str, Any] = {
            "vocab_table": vocab_table_snapshot_content(self.vocab_table),
            "normalized_params": dict(self.normalized_by_request),
            "sampling_decisions": dict(self.decisions),
        }
        if self.migration_records:
            updates["migration_records"] = dict(self.migration_records)
        return self.snapshot_repo.commit_many(updates)

    def _build_migration_record(self, request: InferenceRequest) -> dict[str, Any]:
        record: dict[str, Any] = {
            "request_id": request.request_id,
            "tokenizer_version": request.tokenizer_version,
        }
        if "v1" in request.sampling_profiles:
            source_v1 = read_config(
                request.sampling_profiles["v1"],
                request_id=request.request_id,
            )
            upgrade = upgrade_v1_to_v2(
                source_v1,
                default_vocab_version=request.tokenizer_version,
                request_id=request.request_id,
            )
            record["upgrade"] = upgrade.to_dict()
        if "v2" in request.sampling_profiles:
            source_v2 = read_config(
                request.sampling_profiles["v2"],
                request_id=request.request_id,
            )
            downgrade = downgrade_v2_to_v1(
                source_v2,
                request_id=request.request_id,
            )
            record["downgrade"] = downgrade.to_dict()
        record["traceability"] = {
            "upgrade_added_fields": [
                item["field"] for item in record.get("upgrade", {}).get("added_fields", [])
            ],
            "downgrade_dropped_fields": [
                item["field"]
                for item in record.get("downgrade", {}).get("dropped_fields", [])
            ],
            "downgrade_quantized_fields": [
                item["field"]
                for item in record.get("downgrade", {}).get("quantized_fields", [])
            ],
            "upgrade_output_checksum": checksum_obj(
                record.get("upgrade", {}).get("output_config")
            )
            if "upgrade" in record
            else None,
            "downgrade_output_checksum": checksum_obj(
                record.get("downgrade", {}).get("output_config")
            )
            if "downgrade" in record
            else None,
        }
        return record

    def migrate_demo(
        self,
        request_id: str,
        *,
        record_error: bool = True,
    ) -> dict[str, Any]:
        request = self.get_request(request_id)
        try:
            record = self._build_migration_record(request)
        except BusinessError as error:
            if record_error:
                self.record_error(error, context=f"migrate:{request_id}")
            raise
        self.migration_records[request_id] = record
        return record

    def snapshot_summary(self) -> dict[str, Any]:
        snapshots = self.snapshot_repo.load_all()
        return {
            "path": str(self.snapshot_path),
            "reload_count": self.snapshot_repo.reload_count,
            "last_reload": self.snapshot_repo.last_reload,
            "entries": {
                kind: {
                    "version": envelope["version"],
                    "checksum": envelope["checksum"],
                }
                for kind, envelope in sorted(snapshots.items())
            },
        }

    def run_error_demo(self) -> list[dict[str, Any]]:
        errors_dir = self.samples_dir / "errors"
        cases: list[tuple[str, str, Callable[[], Any]]] = [
            (
                "truncated_v3_header",
                ErrorCode.FORMAT_CORRUPT,
                lambda: read_config_file(errors_dir / "configs" / "truncated_header_v3.txt"),
            ),
            (
                "unknown_version",
                ErrorCode.VERSION_UNSUPPORTED,
                lambda: read_config_file(errors_dir / "configs" / "unknown_version_v3.txt"),
            ),
            (
                "missing_top_p_v2",
                ErrorCode.FORMAT_CORRUPT,
                lambda: read_config_file(errors_dir / "configs" / "missing_top_p_v2.json"),
            ),
            (
                "temperature_range",
                ErrorCode.PARAM_RANGE,
                lambda: self._normalize_error_config(
                    errors_dir / "configs" / "out_of_range_temperature_v2.json",
                    32,
                ),
            ),
            (
                "top_p_range",
                ErrorCode.PARAM_RANGE,
                lambda: self._normalize_error_config(
                    errors_dir / "configs" / "top_p_out_of_range_v3.txt",
                    32,
                ),
            ),
            (
                "top_k_range",
                ErrorCode.PARAM_RANGE,
                lambda: self._normalize_error_config(
                    errors_dir / "configs" / "top_k_nonpositive_v3.txt",
                    32,
                ),
            ),
            (
                "repetition_penalty_range",
                ErrorCode.PARAM_RANGE,
                lambda: self._normalize_error_config(
                    errors_dir / "configs" / "nonpositive_repetition_v2.json",
                    32,
                ),
            ),
            (
                "token_range",
                ErrorCode.TOKEN_RANGE,
                lambda: self._run_error_request(
                    errors_dir / "requests" / "out_of_vocab_token.jsonl"
                ),
            ),
            (
                "logits_shape",
                ErrorCode.LOGITS_SHAPE,
                lambda: self._run_error_request(
                    errors_dir / "requests" / "logits_shape_mismatch.jsonl"
                ),
            ),
            (
                "vocab_version_mismatch",
                ErrorCode.VOCAB_VERSION_MISMATCH,
                lambda: self._run_error_request(
                    errors_dir / "requests" / "vocab_mismatch.jsonl"
                ),
            ),
            (
                "snapshot_checksum",
                ErrorCode.SNAPSHOT_CORRUPT,
                lambda: probe_snapshot_file(
                    errors_dir / "snapshots" / "corrupt_checksum.json"
                ),
            ),
            (
                "semantic_gap",
                ErrorCode.SEMANTIC_GAP,
                lambda: self.adjudicate("req-0002", record_error=False),
            ),
        ]

        outcomes: list[dict[str, Any]] = []
        for case_name, expected_code, action in cases:
            try:
                action()
            except BusinessError as error:
                actual_code = error.code
                payload = {
                    "case": case_name,
                    "expected_code": expected_code,
                    "actual_code": actual_code,
                    "passed": actual_code == expected_code,
                    "error": error.to_dict(),
                }
                self.record_error(error, context=f"error-demo:{case_name}")
            else:
                payload = {
                    "case": case_name,
                    "expected_code": expected_code,
                    "actual_code": "NO_ERROR",
                    "passed": False,
                    "error": None,
                }
            outcomes.append(payload)

        failed = [outcome for outcome in outcomes if not outcome["passed"]]
        if failed:
            raise BusinessError(
                ErrorCode.INCONSISTENT,
                "one or more error demo cases produced an unexpected code",
                details={"failed_cases": failed},
            )
        return outcomes

    def _normalize_error_config(self, path: Path, vocab_size: int) -> Any:
        config = read_config_file(path, request_id="err-param")
        return normalize_params(
            config,
            vocabulary_size=vocab_size,
            request_id="err-param",
        )

    def _run_error_request(self, path: Path) -> dict[str, Any]:
        requests = load_requests_jsonl(path)
        request = next(iter(requests.values()))
        version = self.preferred_version(request)
        return self._run_request(
            request,
            version,
            seed=request.sampling_seed,
        )
