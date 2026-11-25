"""Stage 6: bidirectional replay verification of ledger rows and images."""
from .errors import BusinessError, E_REPLAY_MISMATCH
from .match import iou_detail, match_image
from .models import norm_float

_TOL = 1e-6


def _close(a, b):
    return abs(float(a) - float(b)) <= _TOL


def replay_row(row, cand, gt, threshold):
    """Recompute one TP ledger row from box content + recorded config."""
    if row["status"] != "TP":
        raise BusinessError(
            E_REPLAY_MISMATCH,
            "row-level replay requires a TP row, got %s" % row["status"],
            field="status",
        )
    if cand is None or gt is None:
        raise BusinessError(
            E_REPLAY_MISMATCH,
            "replay chain broken: candidate or ground truth missing",
            field="content",
            context={"row": {k: row[k] for k in ("image_id", "cand_idx", "gt_idx")}},
        )
    if row.get("cand_hash") is not None and cand.hash() != row["cand_hash"]:
        raise BusinessError(
            E_REPLAY_MISMATCH,
            "candidate content hash mismatch during replay",
            field="cand_hash",
        )
    if row.get("gt_revision") is not None and gt.revision != row["gt_revision"]:
        raise BusinessError(
            E_REPLAY_MISMATCH,
            "ground-truth revision mismatch during replay",
            field="gt_revision",
        )
    inter, union, iou = iou_detail(cand, gt)
    if not (_close(inter, row["inter"]) and _close(union, row["union"])
            and _close(iou, row["iou"])):
        raise BusinessError(
            E_REPLAY_MISMATCH,
            "IoU detail mismatch during row replay",
            field="iou",
            context={"recorded": row["iou"], "recomputed": iou},
        )
    status = "TP" if iou >= threshold else "FP"
    if status != row["status"]:
        raise BusinessError(
            E_REPLAY_MISMATCH,
            "status mismatch during row replay",
            field="status",
        )
    return {"status": status, "iou": iou, "inter": inter, "union": union}


def _row_signature(row):
    return (
        row["cand_idx"],
        row["gt_idx"],
        row["status"],
        norm_float(row["iou"]),
        norm_float(row.get("inter", 0.0)),
        norm_float(row.get("union", 0.0)),
    )


def replay_image(image_id, ledger_rows, cands, gts, threshold):
    """Recompute the full assignment for one image and diff row-by-row."""
    im = match_image(image_id, cands, gts, threshold)
    expected = [
        _row_signature(
            {
                "cand_idx": r.cand_idx,
                "gt_idx": r.gt_idx,
                "status": r.status,
                "iou": r.iou,
                "inter": r.inter,
                "union": r.union,
            }
        )
        for r in im.rows
    ]
    actual = [_row_signature(r) for r in ledger_rows]
    if actual != expected:
        raise BusinessError(
            E_REPLAY_MISMATCH,
            "image %s replay diverges from ledger rows" % image_id,
            field="rows",
            context={"image_id": image_id,
                     "ledger_rows": len(actual), "recomputed": len(expected)},
        )
    return im


def replay_verify(v1_rows, v2_rows, images, threshold):
    """Cross-version replay: both ledgers must reproduce the same assignment."""
    v1_by_image = {}
    for r in v1_rows:
        v1_by_image.setdefault(r["image_id"], []).append(r)
    v2_by_image = {}
    for r in v2_rows:
        v2_by_image.setdefault(r["image_id"], []).append(r)
    if sorted(v1_by_image) != sorted(v2_by_image):
        raise BusinessError(
            E_REPLAY_MISMATCH,
            "v1/v2 ledgers cover different image sets",
            field="image_id",
        )
    report = {"images": 0, "rows": 0}
    for image_id in sorted(v1_by_image):
        rows_v1 = v1_by_image[image_id]
        rows_v2 = v2_by_image[image_id]
        sig1 = [_row_signature(r) for r in rows_v1]
        sig2 = [_row_signature(r) for r in rows_v2]
        if sig1 != sig2:
            raise BusinessError(
                E_REPLAY_MISMATCH,
                "v1/v2 ledger rows diverge for image %s" % image_id,
                field="rows",
                context={"image_id": image_id},
            )
        bundle = images[image_id]
        replay_image(image_id, rows_v1, bundle["candidates"],
                     bundle["ground_truth"], threshold)
        report["images"] += 1
        report["rows"] += len(rows_v1)
    return report
