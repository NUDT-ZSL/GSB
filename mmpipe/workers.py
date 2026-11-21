"""Top-level process-pool workers.

Workers are module-level functions so they pickle cleanly with the ``spawn``
start method. Each worker processes one deterministic chunk and returns either
the produced arrays or a :class:`PairFailure`-shaped dict; a failing pair is
attributed by id and never dropped. Fault injection is local and explicit via
environment variables, used only to exercise recovery.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Tuple

import numpy as np

from .config import PipelineConfig
from .errors import PairFailure, PipeError
from .manifest import PairRecord
from .stages import (
    FUSION_STAGE,
    IMAGE_STAGE,
    SCORE_STAGE,
    TEXT_STAGE,
    fuse,
    image_encode,
    load_pixels,
    score,
    text_encode,
)
from .weights import FixedWeights


def _maybe_hard_kill(stage: str, pair_id: str) -> None:
    """Local forced-termination hook for recovery testing."""
    target_stage = os.environ.get("MMPIPE_KILL_STAGE", "")
    target_pair = os.environ.get("MMPIPE_KILL_PAIR", "")
    if target_stage == stage and target_pair == pair_id:
        # SIGKILL-equivalent: the process dies inside the stage, before commit,
        # leaving only the uncommitted staging directory behind.
        os._exit(137)


def _records_by_id(records: Tuple[dict, ...]) -> Dict[str, PairRecord]:
    return {raw["pair_id"]: PairRecord.from_dict(raw) for raw in records}


def image_worker(
    chunk: Tuple[str, ...],
    records: Tuple[dict, ...],
    images_dir: str,
    config_kwargs: dict,
) -> dict:
    config = PipelineConfig(**config_kwargs)
    weights = FixedWeights(config)
    by_id = _records_by_id(records)
    outputs, failures = {}, []
    for pair_id in chunk:
        try:
            _maybe_hard_kill(IMAGE_STAGE, pair_id)
            record = by_id[pair_id]
            pixels = load_pixels(record, Path(images_dir))
            outputs[pair_id] = image_encode(pixels, config, weights)
        except PipeError as err:
            failures.append(PairFailure.from_error(err, IMAGE_STAGE, pair_id).to_dict())
    return {"stage": IMAGE_STAGE, "outputs": outputs, "failures": failures}


def text_worker(
    chunk: Tuple[str, ...],
    records: Tuple[dict, ...],
    images_dir: str,
    config_kwargs: dict,
) -> dict:
    config = PipelineConfig(**config_kwargs)
    weights = FixedWeights(config)
    by_id = _records_by_id(records)
    outputs, failures = {}, []
    for pair_id in chunk:
        try:
            _maybe_hard_kill(TEXT_STAGE, pair_id)
            record = by_id[pair_id]
            outputs[pair_id] = text_encode(record, config, weights)
        except PipeError as err:
            failures.append(PairFailure.from_error(err, TEXT_STAGE, pair_id).to_dict())
    return {"stage": TEXT_STAGE, "outputs": outputs, "failures": failures}

def _load_inputs(pair_id: str, input_dirs: Dict[str, str]) -> Tuple:
    image_emb = np.load(Path(input_dirs[IMAGE_STAGE]) / "embeddings" / f"{pair_id}.npy")
    text_emb = np.load(Path(input_dirs[TEXT_STAGE]) / "embeddings" / f"{pair_id}.npy")
    return image_emb, text_emb


def fusion_worker(
    chunk: Tuple[str, ...],
    records: Tuple[dict, ...],
    images_dir: str,
    config_kwargs: dict,
    input_dirs: Dict[str, str],
) -> dict:
    config = PipelineConfig(**config_kwargs)
    weights = FixedWeights(config)
    by_id = _records_by_id(records)
    outputs, failures = {}, []
    for pair_id in chunk:
        try:
            _maybe_hard_kill(FUSION_STAGE, pair_id)
            record = by_id[pair_id]
            image_emb, text_emb = _load_inputs(pair_id, input_dirs)
            outputs[pair_id] = fuse(record, image_emb, text_emb, config, weights)
        except PipeError as err:
            failures.append(PairFailure.from_error(err, FUSION_STAGE, pair_id).to_dict())
    return {"stage": FUSION_STAGE, "outputs": outputs, "failures": failures}


def score_worker(
    chunk: Tuple[str, ...],
    records: Tuple[dict, ...],
    images_dir: str,
    config_kwargs: dict,
    input_dirs: Dict[str, str],
) -> dict:
    config = PipelineConfig(**config_kwargs)
    weights = FixedWeights(config)
    outputs, failures = {}, []
    for pair_id in chunk:
        try:
            _maybe_hard_kill(SCORE_STAGE, pair_id)
            import numpy as np

            fused = np.load(Path(input_dirs[FUSION_STAGE]) / "embeddings" / f"{pair_id}.npy")
            outputs[pair_id] = score(fused, config, weights)
        except PipeError as err:
            failures.append(PairFailure.from_error(err, SCORE_STAGE, pair_id).to_dict())
    return {"stage": SCORE_STAGE, "outputs": outputs, "failures": failures}


WORKER_REGISTRY = {
    IMAGE_STAGE: image_worker,
    TEXT_STAGE: text_worker,
    FUSION_STAGE: fusion_worker,
    SCORE_STAGE: score_worker,
}

