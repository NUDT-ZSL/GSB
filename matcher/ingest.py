"""Deterministic sample ingestion and validation (stage 1 of the pipeline)."""
import json

from .errors import BusinessError, E_BAD_BOX, E_DUP_IMAGE, E_FORMAT_CORRUPT
from .models import Candidate, GTBox, canonical_json, norm_float

_REQUIRED_CAND = ("image_id", "box_idx", "x1", "y1", "x2", "y2", "score", "class_id")
_REQUIRED_GT = (
    "image_id",
    "box_idx",
    "x1",
    "y1",
    "x2",
    "y2",
    "label",
    "difficulty",
    "occluded",
    "revision",
)


def _read_jsonl(path):
    records = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "unparseable JSON at %s:%d" % (path, lineno),
                    field="line",
                    context={"path": path, "lineno": lineno},
                ) from exc
    return records


def _check_number(rec, name, path):
    value = rec.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessError(
            E_BAD_BOX,
            "field %r is not a number: %r" % (name, value),
            field=name,
            context={"path": path, "image_id": rec.get("image_id")},
        )
    return float(value)


def _check_int(rec, name, path):
    value = rec.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessError(
            E_BAD_BOX,
            "field %r is not an integer: %r" % (name, value),
            field=name,
            context={"path": path, "image_id": rec.get("image_id")},
        )
    return int(value)


def _check_box(rec, classes, path):
    for name in ("x1", "y1", "x2", "y2"):
        value = _check_number(rec, name, path)
        if value < 0:
            raise BusinessError(
                E_BAD_BOX,
                "negative coordinate %r: %r" % (name, value),
                field=name,
                context={"path": path, "image_id": rec.get("image_id")},
            )
    if not rec["x2"] > rec["x1"]:
        raise BusinessError(
            E_BAD_BOX,
            "degenerate box: x2 (%r) must be > x1 (%r)" % (rec["x2"], rec["x1"]),
            field="x2",
            context={"path": path, "image_id": rec.get("image_id")},
        )
    if not rec["y2"] > rec["y1"]:
        raise BusinessError(
            E_BAD_BOX,
            "degenerate box: y2 (%r) must be > y1 (%r)" % (rec["y2"], rec["y1"]),
            field="y2",
            context={"path": path, "image_id": rec.get("image_id")},
        )


def _check_required(rec, required, path):
    for name in required:
        if name not in rec:
            raise BusinessError(
                E_BAD_BOX,
                "missing required field %r" % name,
                field=name,
                context={"path": path, "image_id": rec.get("image_id")},
            )


def _check_class(value, classes, path, image_id, field):
    if value not in classes:
        raise BusinessError(
            E_BAD_BOX,
            "%s %r outside controlled label set %r" % (field, value, classes),
            field=field,
            context={"path": path, "image_id": image_id},
        )


def load_candidates(path, classes):
    cands = []
    seen = set()
    for rec in _read_jsonl(path):
        _check_required(rec, _REQUIRED_CAND, path)
        image_id = str(rec["image_id"])
        box_idx = _check_int(rec, "box_idx", path)
        key = (image_id, box_idx)
        if key in seen:
            raise BusinessError(
                E_DUP_IMAGE,
                "duplicate candidate identity %s#%d" % key,
                field="image_id",
                context={"path": path, "image_id": image_id, "box_idx": box_idx},
            )
        seen.add(key)
        _check_box(rec, classes, path)
        class_id = _check_int(rec, "class_id", path)
        _check_class(class_id, classes, path, image_id, "class_id")
        cands.append(
            Candidate(
                image_id=image_id,
                box_idx=box_idx,
                x1=norm_float(_check_number(rec, "x1", path)),
                y1=norm_float(_check_number(rec, "y1", path)),
                x2=norm_float(_check_number(rec, "x2", path)),
                y2=norm_float(_check_number(rec, "y2", path)),
                score=norm_float(_check_number(rec, "score", path)),
                class_id=class_id,
            )
        )
    return cands


def load_ground_truth(path, classes):
    gts = []
    seen = set()
    for rec in _read_jsonl(path):
        _check_required(rec, _REQUIRED_GT, path)
        image_id = str(rec["image_id"])
        box_idx = _check_int(rec, "box_idx", path)
        key = (image_id, box_idx)
        if key in seen:
            raise BusinessError(
                E_DUP_IMAGE,
                "duplicate ground-truth identity %s#%d" % key,
                field="image_id",
                context={"path": path, "image_id": image_id, "box_idx": box_idx},
            )
        seen.add(key)
        _check_box(rec, classes, path)
        label = _check_int(rec, "label", path)
        _check_class(label, classes, path, image_id, "label")
        revision = _check_int(rec, "revision", path)
        if revision < 0:
            raise BusinessError(
                E_BAD_BOX,
                "negative revision %r" % revision,
                field="revision",
                context={"path": path, "image_id": image_id},
            )
        gts.append(
            GTBox(
                image_id=image_id,
                box_idx=box_idx,
                x1=norm_float(_check_number(rec, "x1", path)),
                y1=norm_float(_check_number(rec, "y1", path)),
                x2=norm_float(_check_number(rec, "x2", path)),
                y2=norm_float(_check_number(rec, "y2", path)),
                label=label,
                difficulty=_check_int(rec, "difficulty", path),
                occluded=bool(rec["occluded"]),
                revision=revision,
            )
        )
    return gts


def build_intermediate(cands, gts, classes):
    """Stable sort + byte-identical canonical intermediate representation."""
    cands = sorted(cands, key=lambda c: (c.image_id, c.box_idx))
    gts = sorted(gts, key=lambda g: (g.image_id, g.box_idx))
    images = {}
    for cand in cands:
        images.setdefault(cand.image_id, {"candidates": [], "ground_truth": []})
        images[cand.image_id]["candidates"].append(cand)
    for gt in gts:
        images.setdefault(gt.image_id, {"candidates": [], "ground_truth": []})
        images[gt.image_id]["ground_truth"].append(gt)
    doc = {
        "format": "match-ir",
        "classes": sorted(classes),
        "images": [
            {
                "image_id": image_id,
                "candidates": [c.content() | {"box_idx": c.box_idx} for c in images[image_id]["candidates"]],
                "ground_truth": [g.content() | {"box_idx": g.box_idx} for g in images[image_id]["ground_truth"]],
            }
            for image_id in sorted(images)
        ],
    }
    return images, canonical_json(doc)
