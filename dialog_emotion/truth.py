"""Versioned per-frame truth with time-scoped corrections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import content_hash, quantize
from .errors import fail

TRUTH_FIELDS = (
    "frame_index",
    "speaker_id",
    "valence",
    "arousal",
    "is_speech",
)


@dataclass(frozen=True)
class TruthCorrection:
    correction_id: str
    dialog_id: str
    start_frame: int
    end_frame: int
    start_ms: float
    end_ms: float
    valence: float
    arousal: float
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "correction_id": self.correction_id,
            "dialog_id": self.dialog_id,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "valence": self.valence,
            "arousal": self.arousal,
            "version": self.version,
        }


class TruthStore:
    """Stores immutable base truth variants plus append-only corrections."""

    def __init__(
        self,
        base_truths: dict[str, dict[str, list[dict[str, Any]]]],
        corrections: Iterable[TruthCorrection] | None = None,
    ) -> None:
        self._base_truths = {
            dialog_id: {
                variant: [dict(row) for row in rows]
                for variant, rows in variants.items()
            }
            for dialog_id, variants in base_truths.items()
        }
        self._corrections: list[TruthCorrection] = list(corrections or [])
        self._next_version = 1 + max(
            (correction.version for correction in self._corrections), default=0
        )

    @property
    def corrections(self) -> list[TruthCorrection]:
        return list(self._corrections)

    def copy(self) -> "TruthStore":
        return TruthStore(self._base_truths, self._corrections)

    def base_rows(self, dialog_id: str, variant: str) -> list[dict[str, Any]]:
        try:
            rows = self._base_truths[dialog_id][variant]
        except KeyError:
            fail(
                "E_FRAME_MISMATCH",
                "no truth variant matches frame configuration",
                dialog_id=dialog_id,
                truth_variant=variant,
                available_variants=sorted(
                    self._base_truths.get(dialog_id, {}).keys()
                ),
            )
        return [dict(row) for row in rows]

    def rows(
        self,
        dialog_id: str,
        variant: str,
        start_frame: int | None = None,
        end_frame: int | None = None,
    ) -> list[dict[str, Any]]:
        base = self.base_rows(dialog_id, variant)
        corrected = [
            self._apply_corrections(dialog_id, row, variant) for row in base
        ]
        if start_frame is None and end_frame is None:
            return corrected
        start = 0 if start_frame is None else start_frame
        end = len(corrected) if end_frame is None else end_frame
        return corrected[start:end]

    def scope_hash(
        self,
        dialog_id: str,
        variant: str,
        start_frame: int,
        end_frame: int,
    ) -> str:
        rows = self.rows(dialog_id, variant, start_frame, end_frame)
        return content_hash(
            {
                "dialog_id": dialog_id,
                "variant": variant,
                "rows": rows,
                "applicable_corrections": [
                    correction.to_dict()
                    for correction in self._corrections
                    if correction.dialog_id == dialog_id
                    and self._correction_overlaps_rows(
                        correction, rows, dialog_id, variant
                    )
                ],
            }
        )

    def correction_version(self, dialog_id: str | None = None) -> str:
        payload = [
            correction.to_dict()
            for correction in self._corrections
            if dialog_id is None or correction.dialog_id == dialog_id
        ]
        return content_hash(payload)

    def add_correction(
        self,
        dialog_id: str,
        start_frame: int,
        end_frame: int,
        hop_ms: float,
        valence: float,
        arousal: float,
    ) -> TruthCorrection:
        if not 0.0 <= valence <= 1.0 or not 0.0 <= arousal <= 1.0:
            fail(
                "E_TRUTH_INVALID",
                "corrected emotion values must be in [0, 1]",
                dialog_id=dialog_id,
                valence=valence,
                arousal=arousal,
            )
        if start_frame < 0 or end_frame <= start_frame:
            fail(
                "E_TRUTH_INVALID",
                "truth correction frame interval is invalid",
                dialog_id=dialog_id,
                start_frame=start_frame,
                end_frame=end_frame,
            )
        start_ms = quantize(start_frame * hop_ms)
        end_ms = quantize(end_frame * hop_ms)
        version = self._next_version
        correction_id = content_hash(
            {
                "dialog_id": dialog_id,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "valence": quantize(valence),
                "arousal": quantize(arousal),
                "version": version,
            }
        )
        correction = TruthCorrection(
            correction_id=correction_id,
            dialog_id=dialog_id,
            start_frame=start_frame,
            end_frame=end_frame,
            start_ms=start_ms,
            end_ms=end_ms,
            valence=quantize(valence),
            arousal=quantize(arousal),
            version=version,
        )
        self._corrections.append(correction)
        self._next_version += 1
        return correction

    def _apply_corrections(
        self,
        dialog_id: str,
        row: dict[str, Any],
        variant: str,
    ) -> dict[str, Any]:
        result = dict(row)
        time_ms = _row_time_ms(row, variant)
        for correction in self._corrections:
            if correction.dialog_id != dialog_id:
                continue
            if correction.start_ms <= time_ms < correction.end_ms:
                result["valence"] = correction.valence
                result["arousal"] = correction.arousal
                result["truth_correction_version"] = correction.version
        return result

    def _correction_overlaps_rows(
        self,
        correction: TruthCorrection,
        rows: list[dict[str, Any]],
        dialog_id: str,
        variant: str,
    ) -> bool:
        if correction.dialog_id != dialog_id:
            return False
        for row in rows:
            time_ms = _row_time_ms(row, variant)
            if correction.start_ms <= time_ms < correction.end_ms:
                return True
        return False


def _row_time_ms(row: dict[str, Any], variant: str) -> float:
    if "time_ms" in row:
        return float(row["time_ms"])
    frame_ms_text, hop_ms_text = variant.split("/", 1)
    frame_ms = float(row.get("frame_ms", frame_ms_text))
    hop_ms = float(row.get("hop_ms", hop_ms_text))
    return float(row["frame_index"]) * hop_ms + frame_ms / 2.0
