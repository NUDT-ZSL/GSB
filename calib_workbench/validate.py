"""Manifest validation with structured business codes (requirement 8)."""

from __future__ import annotations

from .errors import (
    E_DUP_SAMPLE_ID,
    E_FEATURE_LENGTH,
    E_GROUP_MISSING,
    E_MANIFEST_INVALID,
    WorkbenchError,
)

STAGE = "validate"


def validate_manifest(rows: list[dict], cfg: dict) -> None:
    if not rows:
        raise WorkbenchError(E_MANIFEST_INVALID, STAGE, "manifest is empty", field="manifest")

    num_feats = list(cfg["data"]["numeric_features"])
    cat_feats = list(cfg["data"]["categorical_features"].keys())
    known_groups = set(cfg["data"]["groups"])

    seen = set()
    for row in rows:
        sid = row.get("sample_id")
        if sid is None:
            raise WorkbenchError(E_MANIFEST_INVALID, STAGE, "missing sample_id",
                                 field="sample_id", details={"line": row.get("_line_no")})
        if sid in seen:
            raise WorkbenchError(E_DUP_SAMPLE_ID, STAGE, f"duplicate sample_id {sid!r}",
                                 field="sample_id", details={"sample_id": sid})
        seen.add(sid)

        numeric = row.get("numeric")
        if not isinstance(numeric, dict) or sorted(numeric.keys()) != sorted(num_feats):
            raise WorkbenchError(
                E_FEATURE_LENGTH, STAGE,
                f"sample {sid}: numeric feature vector must have exactly "
                f"{len(num_feats)} entries {num_feats}",
                field="numeric",
                details={"sample_id": sid, "expected": num_feats,
                         "actual": sorted(numeric.keys()) if isinstance(numeric, dict) else None},
            )

        categorical = row.get("categorical")
        if not isinstance(categorical, dict) or sorted(categorical.keys()) != sorted(cat_feats):
            raise WorkbenchError(
                E_FEATURE_LENGTH, STAGE,
                f"sample {sid}: categorical feature vector must have exactly "
                f"{len(cat_feats)} entries {cat_feats}",
                field="categorical",
                details={"sample_id": sid, "expected": cat_feats,
                         "actual": sorted(categorical.keys()) if isinstance(categorical, dict) else None},
            )

        group = row.get("group")
        if group is None or group == "":
            raise WorkbenchError(E_GROUP_MISSING, STAGE, f"sample {sid}: group label missing",
                                 field="group", details={"sample_id": sid})
        if group not in known_groups:
            raise WorkbenchError(E_GROUP_MISSING, STAGE,
                                 f"sample {sid}: group {group!r} not declared in config data.groups",
                                 field="group", details={"sample_id": sid, "group": group})

        if row.get("label") not in (0, 1):
            raise WorkbenchError(E_MANIFEST_INVALID, STAGE, f"sample {sid}: label must be 0 or 1",
                                 field="label", details={"sample_id": sid})
        score = row.get("raw_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise WorkbenchError(E_MANIFEST_INVALID, STAGE,
                                 f"sample {sid}: raw_score must be numeric",
                                 field="raw_score", details={"sample_id": sid})
