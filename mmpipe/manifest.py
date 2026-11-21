"""Deterministic local sample generation and manifest validation.

A sample is a JSONL record with: ``pair_id``, ``image_seed``, ``image_shape``,
``text_tokens`` (controlled vocabulary), ``modality_mask`` (1=image, 2=text,
3=both), ``stage_hint`` and ``priority``. Pixel arrays live next to the
manifest as ``.npy`` files generated solely from the image seed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import numpy as np

from .config import VALID_HINTS, VALID_MASKS, PipelineConfig
from .errors import (
    DupPairError,
    HintInvalidError,
    MaskInvalidError,
    PairIdInvalidError,
    SeedShapeError,
    TextEmptyError,
)
from .weights import expected_image_shape, generate_image

PAIR_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class PairRecord:
    pair_id: str
    image_seed: int
    image_shape: tuple
    text_tokens: tuple
    modality_mask: int
    stage_hint: str
    priority: int

    @classmethod
    def from_dict(cls, raw: dict) -> "PairRecord":
        return cls(
            pair_id=str(raw["pair_id"]),
            image_seed=int(raw["image_seed"]),
            image_shape=tuple(int(v) for v in raw["image_shape"]),
            text_tokens=tuple(raw["text_tokens"]),
            modality_mask=int(raw["modality_mask"]),
            stage_hint=str(raw["stage_hint"]),
            priority=int(raw["priority"]),
        )

    def to_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "image_seed": self.image_seed,
            "image_shape": list(self.image_shape),
            "text_tokens": list(self.text_tokens),
            "modality_mask": self.modality_mask,
            "stage_hint": self.stage_hint,
            "priority": self.priority,
        }


def token_name(index: int) -> str:
    return f"tok{index:02d}"


def token_index(token: str) -> int:
    return int(token.split("tok", 1)[1])


def generate_manifest(
    out_dir: Path,
    config: PipelineConfig,
    *,
    num_pairs: int = 12,
    generator_seed: int = 777,
) -> List[PairRecord]:
    """Generate a deterministic manifest plus pixel arrays under ``out_dir``."""
    out_dir = Path(out_dir)
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(generator_seed)

    records: List[PairRecord] = []
    for i in range(num_pairs):
        pair_id = f"pair_{i:04d}"
        image_seed = int(rng.integers(1_000, 1_000_000))
        shape = expected_image_shape(image_seed, config.block_size)
        pixels = generate_image(image_seed, shape)
        np.save(images_dir / f"{pair_id}.npy", pixels)

        token_count = int(rng.integers(2, 7))
        token_ids = rng.integers(0, config.vocab_size, size=token_count)
        text_tokens = [token_name(int(t)) for t in token_ids]
        modality_mask = int(VALID_MASKS[int(rng.integers(0, len(VALID_MASKS)))])
        stage_hint = VALID_HINTS[int(rng.integers(0, len(VALID_HINTS)))]
        priority = int(rng.integers(0, 5))
        records.append(
            PairRecord(
                pair_id=pair_id,
                image_seed=image_seed,
                image_shape=shape,
                text_tokens=tuple(text_tokens),
                modality_mask=modality_mask,
                stage_hint=stage_hint,
                priority=priority,
            )
        )

    manifest_path = out_dir / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return records


def load_manifest(manifest_path: Path) -> List[PairRecord]:
    records: List[PairRecord] = []
    with Path(manifest_path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(PairRecord.from_dict(json.loads(line)))
    return records


def validate_manifest(records: Iterable[PairRecord], config: PipelineConfig) -> List[PairRecord]:
    """Validate every record and the pair_id uniqueness.

    Order of checks: identity -> mask -> hint -> text -> seed/shape, so the
    first failure on a record carries the most specific business code.
    """
    seen = set()
    validated: List[PairRecord] = []
    for record in records:
        if not record.pair_id or not PAIR_ID_RE.match(record.pair_id):
            raise PairIdInvalidError(
                f"pair_id {record.pair_id!r} is missing or contains unsafe characters",
                pair_id=record.pair_id,
            )
        if record.pair_id in seen:
            raise DupPairError(
                f"duplicate pair_id {record.pair_id!r}", pair_id=record.pair_id
            )
        if record.modality_mask not in VALID_MASKS:
            raise MaskInvalidError(
                f"modality_mask {record.modality_mask} is not one of {VALID_MASKS}",
                pair_id=record.pair_id,
            )
        if record.stage_hint not in VALID_HINTS:
            raise HintInvalidError(
                f"stage_hint {record.stage_hint!r} is not one of {VALID_HINTS}",
                pair_id=record.pair_id,
            )
        # Empty text is a per-pair worker failure (E_TEXT_EMPTY) so it can be
        # attributed through the structured worker channel; structural checks
        # above stay as fast manifest-level rejections.
        for token in record.text_tokens:
            idx = token_index(token)
            if not 0 <= idx < config.vocab_size:
                raise TextEmptyError(
                    f"token {token!r} is outside the controlled vocabulary",
                    pair_id=record.pair_id,
                )
        expected = expected_image_shape(record.image_seed, config.block_size)
        if tuple(record.image_shape) != expected:
            raise SeedShapeError(
                f"image_shape {tuple(record.image_shape)} does not match seed-derived "
                f"shape {expected} for image_seed {record.image_seed}",
                pair_id=record.pair_id,
            )
        seen.add(record.pair_id)
        validated.append(record)
    return validated


def canonical_order(records: Iterable[PairRecord]) -> List[PairRecord]:
    """Stable, reproducible processing order (priority desc, then pair_id)."""
    return sorted(records, key=lambda r: (-r.priority, r.pair_id))

