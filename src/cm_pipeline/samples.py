from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    load_codebook,
    load_config,
    load_vocab,
    validate_codebook_against_config,
    validate_config,
)
from .errors import PipelineError

REQUIRED_FIELDS = (
    "pair_id",
    "image_seed",
    "image_shape",
    "text",
    "text_lang",
    "caption_revision",
    "embed_dim",
    "modality_mask",
)


@dataclass(frozen=True)
class InputBundle:
    config: dict[str, Any]
    config_info: dict[str, Any]
    vocab: dict[str, Any]
    codebook: dict[str, Any]
    samples: list[dict[str, Any]]

    @property
    def samples_by_id(self) -> dict[str, dict[str, Any]]:
        return {sample["pair_id"]: sample for sample in self.samples}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _input_error(
    code: str,
    message: str,
    *,
    field: str,
    pair_id: str | None,
    line: int | None = None,
) -> PipelineError:
    details = {"line": line} if line is not None else {}
    return PipelineError(
        code,
        message,
        stage="input_validation",
        field=field,
        pair_id=pair_id,
        details=details,
    )


def validate_sample_record(
    raw: dict[str, Any],
    *,
    config_info: dict[str, Any],
    vocab: dict[str, Any],
    line: int,
) -> dict[str, Any]:
    pair_id = raw.get("pair_id")
    pair_for_error = pair_id if isinstance(pair_id, str) and pair_id else None
    missing = [field for field in REQUIRED_FIELDS if field not in raw]
    if missing:
        raise _input_error(
            "E_CONFIG_INVALID",
            f"Missing required sample fields: {', '.join(missing)}",
            field=missing[0],
            pair_id=pair_for_error,
            line=line,
        )
    if not isinstance(pair_id, str) or not pair_id.strip():
        raise _input_error(
            "E_CONFIG_INVALID",
            "pair_id must be a non-empty string",
            field="pair_id",
            pair_id=pair_for_error,
            line=line,
        )
    embed_dim = raw.get("embed_dim")
    if _is_int(embed_dim) and embed_dim <= 0:
        raise _input_error(
            "E_CONFIG_INVALID",
            "embed_dim must be positive",
            field="embed_dim",
            pair_id=pair_id,
            line=line,
        )
    if not _is_int(embed_dim):
        raise _input_error(
            "E_CONFIG_INVALID",
            "embed_dim must be an integer",
            field="embed_dim",
            pair_id=pair_id,
            line=line,
        )
    expected_dim = int(config_info["image_dim"])
    if embed_dim != expected_dim:
        raise _input_error(
            "E_CONFIG_INVALID",
            f"embed_dim {embed_dim} does not match configured encoder dim {expected_dim}",
            field="embed_dim",
            pair_id=pair_id,
            line=line,
        )
    seed = raw.get("image_seed")
    if not _is_int(seed) or seed < 0:
        raise _input_error(
            "E_SEED_SHAPE_MISMATCH",
            "image_seed must be a non-negative integer consistent with image_shape",
            field="image_seed",
            pair_id=pair_id,
            line=line,
        )
    shape = raw.get("image_shape")
    expected_shape = [int(x) for x in config_info["shape"]]
    if (
        not isinstance(shape, list)
        or len(shape) != 3
        or not all(_is_int(x) and x > 0 for x in shape)
        or shape[2] != 3
        or shape != expected_shape
    ):
        raise _input_error(
            "E_SEED_SHAPE_MISMATCH",
            f"image_shape must equal configured shape {expected_shape}",
            field="image_shape",
            pair_id=pair_id,
            line=line,
        )
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _input_error(
            "E_TEXT_EMPTY",
            "text must be a non-empty string",
            field="text",
            pair_id=pair_id,
            line=line,
        )
    text_lang = raw.get("text_lang")
    languages = vocab["languages"]
    if not isinstance(text_lang, str) or text_lang not in languages:
        raise _input_error(
            "E_CONFIG_INVALID",
            f"text_lang must be one of {sorted(languages)}",
            field="text_lang",
            pair_id=pair_id,
            line=line,
        )
    mask = raw.get("modality_mask")
    if (
        not isinstance(mask, list)
        or not mask
        or len(mask) != len(set(mask))
        or any(item not in {"image", "text"} for item in mask)
    ):
        raise _input_error(
            "E_MASK_INVALID",
            "modality_mask must be a non-empty unique subset of ['image', 'text']",
            field="modality_mask",
            pair_id=pair_id,
            line=line,
        )
    revision = raw.get("caption_revision")
    if not _is_int(revision) or revision < 0:
        raise _input_error(
            "E_CONFIG_INVALID",
            "caption_revision must be a non-negative integer",
            field="caption_revision",
            pair_id=pair_id,
            line=line,
        )
    tokens = text.strip().split()
    unknown = sorted(
        token
        for token in set(tokens)
        if token not in languages[text_lang]["weights"]
    )
    if unknown:
        raise _input_error(
            "E_CONFIG_INVALID",
            f"text contains tokens outside the controlled vocabulary: {unknown}",
            field="text",
            pair_id=pair_id,
            line=line,
        )
    return {
        "pair_id": pair_id,
        "image_seed": seed,
        "image_shape": [int(x) for x in shape],
        "text": text.strip(),
        "text_lang": text_lang,
        "caption_revision": revision,
        "embed_dim": embed_dim,
        "modality_mask": sorted(mask),
    }


def load_samples(
    path: str | Path,
    *,
    config_info: dict[str, Any],
    vocab: dict[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    try:
        handle = Path(path).open("r", encoding="utf-8")
    except OSError as exc:
        raise PipelineError(
            "E_CONFIG_INVALID",
            f"Cannot open sample file {path}: {exc}",
            stage="input_validation",
            field="path",
        ) from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _input_error(
                    "E_CONFIG_INVALID",
                    f"Malformed JSONL record: {exc.msg}",
                    field="json",
                    pair_id=None,
                    line=line_number,
                ) from exc
            if not isinstance(raw, dict):
                raise _input_error(
                    "E_CONFIG_INVALID",
                    "Each JSONL record must be an object",
                    field="root",
                    pair_id=None,
                    line=line_number,
                )
            pair_id = raw.get("pair_id")
            if isinstance(pair_id, str) and pair_id in seen:
                raise _input_error(
                    "E_DUP_PAIR",
                    f"Duplicate pair_id {pair_id}; first seen on line {seen[pair_id]}",
                    field="pair_id",
                    pair_id=pair_id,
                    line=line_number,
                )
            if isinstance(pair_id, str):
                seen[pair_id] = line_number
            records.append(
                validate_sample_record(
                    raw,
                    config_info=config_info,
                    vocab=vocab,
                    line=line_number,
                )
            )
    records.sort(key=lambda item: item["pair_id"])
    return records


def load_bundle(
    data_path: str | Path,
    config_path: str | Path,
    vocab_path: str | Path,
    codebook_path: str | Path,
) -> InputBundle:
    config = load_config(config_path)
    config_info = validate_config(config)
    vocab = load_vocab(vocab_path)
    codebook = load_codebook(codebook_path)
    validate_codebook_against_config(codebook, config_info)
    samples = load_samples(data_path, config_info=config_info, vocab=vocab)
    return InputBundle(
        config=config,
        config_info=config_info,
        vocab=vocab,
        codebook=codebook,
        samples=samples,
    )

