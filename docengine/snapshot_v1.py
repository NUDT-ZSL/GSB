"""v1 snapshot: single JSON file with documents, spans and a flat inverted
list. Validation is total: any failure raises a BusinessError and the caller
must not write any persistent state."""

import json

from .errors import BusinessError, ErrorCode
from .stablejson import file_sha256

SNAPSHOT_ID = "v1"


def _check_value_type(doc_id, field, value):
    # bool must be rejected explicitly: documents carry str/int/float only.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise BusinessError(
            ErrorCode.E_V1_FIELD_TYPE,
            "illegal field value type in v1 snapshot",
            snapshot_version=SNAPSHOT_ID,
            doc_id=doc_id,
            field=field,
            value_type=type(value).__name__,
        )


def load_v1(path, fragment_store):
    """Parse and fully validate a v1 snapshot file."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    docs = {}
    for doc in raw.get("documents", []):
        doc_id = doc["doc_id"]
        if doc_id in docs:
            raise BusinessError(
                ErrorCode.E_V1_DUPLICATE_DOC,
                "duplicate document id in v1 snapshot",
                snapshot_version=SNAPSHOT_ID,
                doc_id=doc_id,
            )
        for field, value in doc.get("fields", {}).items():
            _check_value_type(doc_id, field, value)
        docs[doc_id] = doc

    # Span ranges must stay inside the referenced fragment.
    for doc in raw.get("documents", []):
        for span in doc.get("spans", []):
            size = fragment_store.size_of(span["fragment"])
            if (
                size is None
                or span["offset"] < 0
                or span["length"] < 0
                or span["offset"] + span["length"] > size
            ):
                raise BusinessError(
                    ErrorCode.E_V1_SPAN_RANGE,
                    "v1 span out of fragment bounds",
                    snapshot_version=SNAPSHOT_ID,
                    doc_id=doc["doc_id"],
                    field=span["field"],
                    offset=span["offset"],
                    length=span["length"],
                    fragment=span["fragment"],
                    fragment_size=size,
                )

    # Every flat inverted entry must trace back to a declared doc+field.
    inverted = []
    for entry in raw.get("inverted", []):
        doc_id = entry["doc_id"]
        field = entry["field"]
        value = entry["value"]
        doc = docs.get(doc_id)
        if doc is None or doc.get("fields", {}).get(field) != value:
            raise BusinessError(
                ErrorCode.E_V1_INVERTED_ORPHAN,
                "v1 inverted entry does not trace to a declared document field",
                snapshot_version=SNAPSHOT_ID,
                doc_id=doc_id,
                field=field,
                value=value,
            )
        inverted.append({"doc_id": doc_id, "field": field, "value": value})

    return {
        "snapshot_id": raw.get("snapshot_id", SNAPSHOT_ID),
        "format": SNAPSHOT_ID,
        "clock_ms": int(raw.get("clock_ms", 0)),
        "source_sha256": file_sha256(path),
        "documents": docs,
        "inverted": inverted,
    }
