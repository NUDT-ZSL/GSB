"""Document shard loading, normalization, and deterministic validation."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from .errors import E_CHAR_LEN_MISMATCH, E_DUP_DOC, PipelineError
from .util import read_jsonl, sha256_text


def _invalid(message: str, details: dict[str, Any]) -> PipelineError:
    return PipelineError("E_INPUT_INVALID", message, details)


def load_documents(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Load every shard, validate it, and return documents sorted by doc_id.

    Validation happens before normalization.  The normalized text is stored so
    all later character offsets refer to exactly one, stable Unicode string.
    """

    normalization = config["normalization"]
    seen: dict[str, str] = {}
    documents: list[dict[str, Any]] = []

    for path_text in config["_resolved_paths"]["shards"]:
        path = Path(path_text)
        try:
            rows = read_jsonl(path)
        except (OSError, ValueError) as exc:
            raise _invalid("cannot read shard", {"path": path.name, "reason": str(exc)})
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise _invalid(
                    "shard row must be an object",
                    {"path": path.name, "row_index": row_index},
                )
            missing = [
                field
                for field in ("shard_id", "doc_id", "para_no", "text", "char_len")
                if field not in row
            ]
            if missing:
                raise _invalid(
                    "shard row is missing required fields",
                    {"path": path.name, "row_index": row_index, "missing": missing},
                )
            if not isinstance(row["doc_id"], str) or not row["doc_id"]:
                raise _invalid(
                    "doc_id must be a non-empty string",
                    {"path": path.name, "row_index": row_index},
                )
            doc_id = row["doc_id"]
            if doc_id in seen:
                raise PipelineError(
                    E_DUP_DOC,
                    "duplicate doc_id",
                    {
                        "doc_id": doc_id,
                        "first_location": seen[doc_id],
                        "duplicate_location": f"{path.name}:{row_index + 1}",
                    },
                )
            seen[doc_id] = f"{path.name}:{row_index + 1}"

            if (
                not isinstance(row["shard_id"], str)
                or not row["shard_id"]
                or not isinstance(row["para_no"], int)
                or isinstance(row["para_no"], bool)
                or row["para_no"] < 1
                or not isinstance(row["text"], str)
                or not isinstance(row["char_len"], int)
                or isinstance(row["char_len"], bool)
            ):
                raise _invalid(
                    "shard row has invalid field types",
                    {
                        "doc_id": doc_id,
                        "shard_id": row.get("shard_id"),
                        "para_no": row.get("para_no"),
                        "char_len": row.get("char_len"),
                    },
                )

            if normalization == "NONE":
                text = row["text"]
            else:
                text = unicodedata.normalize(normalization, row["text"])
            actual_len = len(text)
            if actual_len != row["char_len"]:
                raise PipelineError(
                    E_CHAR_LEN_MISMATCH,
                    "char_len does not match Unicode code-point length",
                    {
                        "doc_id": doc_id,
                        "declared_char_len": row["char_len"],
                        "actual_char_len": actual_len,
                        "normalization": normalization,
                    },
                )
            documents.append(
                {
                    "doc_id": doc_id,
                    "shard_id": row["shard_id"],
                    "para_no": row["para_no"],
                    "text": text,
                    "char_len": actual_len,
                    "text_checksum": sha256_text(text),
                }
            )

    documents.sort(key=lambda doc: doc["doc_id"])
    return documents

