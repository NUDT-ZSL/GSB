"""Cross-format downgrade/upgrade with explicit information retention."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codes import E_SIDECAR_INVALID, E_TOKEN_TIME_INFERRED
from .formats import (
    SidecarError,
    read_sidecar,
    restore_tokens_from_sidecar,
    v2_content_hash,
)
from .models import Transcript
from .parsers import FailureRecord, tokenize_text
from .timeutil import PrecisionStats, allocate_by_char_counts


@dataclass(slots=True)
class UpgradeResult:
    transcript: Transcript
    sidecar_used: bool
    inferred_time_count: int
    diagnostic_codes: list[str] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    reason: str = ""


def _infer_tokens(
    transcript: Transcript, precision: PrecisionStats | None = None
) -> None:
    for utterance in transcript.utterances:
        texts = [token.text for token in utterance.tokens]
        if not texts:
            texts = tokenize_text(utterance.text)
        spans, boundary_count, max_error_us = allocate_by_char_counts(
            utterance.start_ms,
            utterance.end_ms,
            texts,
        )
        for token, (start_ms, end_ms) in zip(utterance.tokens, spans):
            token.start_ms = start_ms
            token.end_ms = end_ms
            token.inferred = True
        if not utterance.tokens:
            # This is only possible for a hand-crafted IR, not a parsed SRT.
            utterance.tokens = []
        if precision and boundary_count:
            precision.grid_rounding_count += boundary_count
            precision.grid_max_error_us = max(precision.grid_max_error_us, max_error_us)
    transcript.source_version = 1
    transcript.timing_origin = "v2-inferred"


def upgrade_v2(
    transcript_v2: Transcript,
    sidecar_path: str | Path | None,
    precision: PrecisionStats | None = None,
) -> UpgradeResult:
    """Upgrade sentence-level v2 data to token-level v1 data.

    A sidecar is trusted only when its stored v2 content hash equals the hash
    of the current SRT content and its token text reconstructs that content.
    Otherwise token times are inferred from character weights and explicitly
    marked.
    """

    working = transcript_v2.normalized_copy()
    failures: list[FailureRecord] = []
    diagnostic_codes: list[str] = []
    reason = ""
    if sidecar_path is not None:
        path = Path(sidecar_path)
        if path.exists():
            try:
                sidecar = read_sidecar(path)
                restored, reason = restore_tokens_from_sidecar(working, sidecar)
                if restored:
                    inferred = working.inferred_time_count
                    if inferred:
                        diagnostic_codes.append(E_TOKEN_TIME_INFERRED)
                    return UpgradeResult(
                        transcript=working,
                        sidecar_used=True,
                        inferred_time_count=inferred,
                        diagnostic_codes=diagnostic_codes,
                        failures=failures,
                        reason=reason,
                    )
                reason = f"sidecar not applied: {reason}"
            except (SidecarError, ValueError, TypeError) as exc:
                failures.append(
                    FailureRecord(
                        stage="upgrade",
                        source_file=str(path),
                        item_index=None,
                        code=E_SIDECAR_INVALID,
                        exception_type=type(exc).__name__,
                        message=str(exc),
                        line_number=None,
                    )
                )
                reason = f"malformed sidecar: {exc}"
        else:
            reason = "sidecar file does not exist"
    else:
        reason = "no sidecar path supplied"

    _infer_tokens(working, precision)
    diagnostic_codes.append(E_TOKEN_TIME_INFERRED)
    return UpgradeResult(
        transcript=working,
        sidecar_used=False,
        inferred_time_count=working.inferred_time_count,
        diagnostic_codes=diagnostic_codes,
        failures=failures,
        reason=reason,
    )


def validate_upgrade_retention(
    transcript: Transcript, sidecar: dict[str, Any] | None
) -> list[str]:
    """Return compliance notes used by diagnostics and tests."""

    notes: list[str] = []
    if sidecar is not None and sidecar.get("v2_content_hash") == v2_content_hash(
        transcript
    ):
        notes.append("sidecar content hash matches")
    if transcript.inferred_time_count:
        notes.append(
            f"{transcript.inferred_time_count} tokens retain explicit inferred timing"
        )
    return notes
