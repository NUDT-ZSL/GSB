"""Context feature catalog and observation validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import (
    E_CONTEXT_RANGE_INVALID,
    E_FEATURE_UNKNOWN,
    E_WEIGHT_NEGATIVE,
    PipelineError,
)
from .util import overlap_chars, read_json


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_context_features(
    config: dict[str, Any],
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    path = Path(config["_resolved_paths"]["context_features"])
    try:
        raw = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(
            E_CONTEXT_RANGE_INVALID,
            "cannot read context feature file",
            {"path": path.name, "reason": str(exc)},
        )
    if not isinstance(raw, dict):
        raise PipelineError(
            E_CONTEXT_RANGE_INVALID,
            "context feature file must be an object",
            {"path": path.name},
        )
    version = raw.get("context_version")
    if not isinstance(version, str) or not version:
        raise PipelineError(
            E_CONTEXT_RANGE_INVALID,
            "context_version is required",
            {"path": path.name},
        )
    if config.get("context_version") not in (None, version):
        raise PipelineError(
            E_CONTEXT_RANGE_INVALID,
            "configured context version does not match the context file",
            {"configured": config.get("context_version"), "actual": version},
        )
    catalog_rows = raw.get("catalog")
    if not isinstance(catalog_rows, list) or not catalog_rows:
        raise PipelineError(
            E_CONTEXT_RANGE_INVALID,
            "context catalog must be a non-empty list",
            {"path": path.name},
        )
    catalog: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(catalog_rows):
        if not isinstance(item, dict):
            raise PipelineError(
                E_CONTEXT_RANGE_INVALID,
                "context catalog item must be an object",
                {"catalog_index": index},
            )
        feature_id = item.get("feature_id")
        if not isinstance(feature_id, str) or not feature_id:
            raise PipelineError(
                E_CONTEXT_RANGE_INVALID,
                "feature_id must be a non-empty string",
                {"catalog_index": index},
            )
        if feature_id in catalog:
            raise PipelineError(
                E_FEATURE_UNKNOWN,
                "duplicate context feature_id",
                {"feature_id": feature_id},
            )
        weight = item.get("weight")
        if not _number(weight):
            raise PipelineError(
                E_WEIGHT_NEGATIVE,
                "feature weight must be numeric",
                {"feature_id": feature_id, "weight": weight},
            )
        if weight < 0:
            raise PipelineError(
                E_WEIGHT_NEGATIVE,
                "feature weight must not be negative",
                {"feature_id": feature_id, "weight": weight},
            )
        catalog[feature_id] = {
            "feature_id": feature_id,
            "description": str(item.get("description", "")),
            "weight": float(weight),
        }

    docs_by_id = {doc["doc_id"]: doc for doc in documents}
    observation_rows = raw.get("observations", [])
    if not isinstance(observation_rows, list):
        raise PipelineError(
            E_CONTEXT_RANGE_INVALID,
            "context observations must be a list",
            {"path": path.name},
        )
    observations: list[dict[str, Any]] = []
    for index, item in enumerate(observation_rows):
        if not isinstance(item, dict):
            raise PipelineError(
                E_CONTEXT_RANGE_INVALID,
                "context observation must be an object",
                {"observation_index": index},
            )
        doc_id = item.get("doc_id")
        if not isinstance(doc_id, str) or doc_id not in docs_by_id:
            raise PipelineError(
                E_CONTEXT_RANGE_INVALID,
                "context observation references an unknown doc_id",
                {"observation_index": index, "doc_id": doc_id},
            )
        doc = docs_by_id[doc_id]
        feature_id = item.get("feature_id")
        if feature_id not in catalog:
            raise PipelineError(
                E_FEATURE_UNKNOWN,
                "context observation references an unknown feature",
                {
                    "observation_index": index,
                    "doc_id": doc_id,
                    "feature_id": feature_id,
                },
            )
        start = item.get("start")
        end = item.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end < start
            or end > doc["char_len"]
        ):
            raise PipelineError(
                E_CONTEXT_RANGE_INVALID,
                "context feature range is invalid for the document",
                {
                    "doc_id": doc_id,
                    "feature_id": feature_id,
                    "start": start,
                    "end": end,
                    "char_len": doc["char_len"],
                },
            )
        para_no = item.get("para_no", doc["para_no"])
        if (
            not isinstance(para_no, int)
            or isinstance(para_no, bool)
            or para_no != doc["para_no"]
        ):
            raise PipelineError(
                E_CONTEXT_RANGE_INVALID,
                "context observation para_no must match the document",
                {
                    "doc_id": doc_id,
                    "para_no": para_no,
                    "document_para_no": doc["para_no"],
                },
            )
        weight = item.get("weight", catalog[feature_id]["weight"])
        if not _number(weight):
            raise PipelineError(
                E_WEIGHT_NEGATIVE,
                "context observation weight must be numeric",
                {"doc_id": doc_id, "feature_id": feature_id, "weight": weight},
            )
        if weight < 0:
            raise PipelineError(
                E_WEIGHT_NEGATIVE,
                "context observation weight must not be negative",
                {
                    "doc_id": doc_id,
                    "feature_id": feature_id,
                    "weight": weight,
                },
            )
        observations.append(
            {
                "doc_id": doc_id,
                "para_no": para_no,
                "feature_id": feature_id,
                "start": start,
                "end": end,
                "value": str(item.get("value", "")),
                "weight": float(weight),
            }
        )

    observations.sort(
        key=lambda row: (
            row["doc_id"],
            row["para_no"],
            row["start"],
            row["end"],
            row["feature_id"],
        )
    )
    by_doc: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        by_doc.setdefault(observation["doc_id"], []).append(observation)
    return {
        "context_version": version,
        "catalog": catalog,
        "observations": observations,
        "by_doc": by_doc,
    }


def observations_in_window(
    features: dict[str, Any],
    doc_id: str,
    para_no: int,
    window_start: int,
    window_end: int,
) -> list[dict[str, Any]]:
    result = []
    for observation in features["by_doc"].get(doc_id, []):
        if observation["para_no"] != para_no:
            continue
        if (
            overlap_chars(
                observation["start"],
                observation["end"],
                window_start,
                window_end,
            )
            > 0
        ):
            result.append(observation)
    return result


def document_feature_counts(
    documents: list[dict[str, Any]],
    features: dict[str, Any],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for doc in documents:
        counts[doc["doc_id"]] = len(
            {
                observation["feature_id"]
                for observation in features["by_doc"].get(doc["doc_id"], [])
            }
        )
    return counts

