"""V1 flat-JSON and V2 sharded-JSON + SQLite threshold model migration."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable

from .errors import ThresholdFormatError
from .io_utils import atomic_write_json, payload_checksum, read_json
from .thresholds import (
    EFFECTIVE_TIER,
    GLOBAL_SEGMENT,
    QUANTILE_PROBS,
    ThresholdModel,
    ThresholdRow,
    prob_label,
)


V1_FORMAT = "ae-threshold-v1"
V2_FORMAT = "ae-threshold-v2"


def load_threshold_model(path: str | Path) -> ThresholdModel:
    target = Path(path)
    if target.is_dir():
        return read_v2(target)
    data = read_json(target)
    if isinstance(data, dict) and data.get("format") == V2_FORMAT:
        return read_v2(target.parent)
    model, _ = read_v1(target)
    return model


def write_v1(path: str | Path, model: ThresholdModel) -> Path:
    target = Path(path)
    flat: dict[str, float] = {}
    sources = list(model.migration_sources)

    global_score = model.row(GLOBAL_SEGMENT, "score", EFFECTIVE_TIER)
    if global_score is None:
        global_score = next(
            (row for row in model.rows if row.tier == EFFECTIVE_TIER and row.metric == "score"),
            None,
        )

    if global_score is not None:
        for label, value in sorted(global_score.quantiles.items()):
            flat[label] = float(value)

    for row in sorted(model.rows, key=lambda item: (item.segment_id, item.metric, item.tier)):
        if row.tier != EFFECTIVE_TIER:
            continue
        for label, value in sorted(row.quantiles.items()):
            if row.segment_id == GLOBAL_SEGMENT and row.metric == "score":
                continue
            key = f"segment.{row.segment_id}.{row.metric}.{label}"
            flat[key] = float(value)

    core = {
        "format": V1_FORMAT,
        "schema_version": "1.0",
        "model_version": int(model.model_version),
        "default_metric": "score",
        "global_segment": GLOBAL_SEGMENT,
        "quantile_probs": [float(p) for p in model.quantile_probs],
        "default_granularity": model.default_granularity,
        "tolerance_ratio": float(model.tolerance_ratio),
        "quantiles": flat,
        "calibration_path": list(model.calibration_path),
        "migration_sources": sources,
    }
    core["checksum"] = payload_checksum({k: v for k, v in core.items() if k != "checksum"})
    return atomic_write_json(target, core)


def read_v1(path_or_data: str | Path | dict) -> tuple[ThresholdModel, list[dict]]:
    if isinstance(path_or_data, (str, Path)):
        data = read_json(path_or_data)
        source_name = str(path_or_data)
    else:
        data = dict(path_or_data)
        source_name = "<dict>"

    if not isinstance(data, dict):
        raise ThresholdFormatError("v1 threshold document must be a JSON object")
    if data.get("format") not in (None, V1_FORMAT):
        raise ThresholdFormatError(f"unsupported v1 format marker: {data.get('format')!r}")

    stored_checksum = data.get("checksum")
    if stored_checksum:
        expected = payload_checksum({k: v for k, v in data.items() if k != "checksum"})
        if expected != stored_checksum:
            from .errors import ChecksumMismatchError

            raise ChecksumMismatchError(
                "v1 threshold checksum mismatch",
                details={"path": source_name, "expected": expected, "stored": stored_checksum},
            )

    raw_quantiles = data.get("quantiles", data)
    if not isinstance(raw_quantiles, dict):
        raise ThresholdFormatError("v1 quantiles must be a flat object")

    grouped: dict[tuple[str, str], dict[str, float]] = {}
    sources: list[dict] = []
    for raw_key, raw_value in raw_quantiles.items():
        key = str(raw_key)
        if key in {"format", "checksum", "model_version"} or not isinstance(raw_value, (int, float)):
            continue
        segment, metric, label = _parse_v1_key(key)
        if label is None:
            continue
        grouped.setdefault((segment, metric), {})[label] = float(raw_value)
        sources.append(
            {
                "direction": "v1_read",
                "source": f"{source_name}:quantiles.{key}",
                "target": f"canonical:{segment}:{EFFECTIVE_TIER}:{metric}:{label}",
                "action": "parse_flat_quantile",
            }
        )

    rows = []
    for (segment, metric), quantiles in sorted(grouped.items()):
        row = ThresholdRow(
            segment_id=segment,
            metric=metric,
            tier=EFFECTIVE_TIER,
            granularity="flat" if segment == GLOBAL_SEGMENT else str(data.get("default_granularity", "window_segment")),
            quantiles=quantiles,
            sample_count=0,
        )
        row.recompute_checksum()
        rows.append(row)

    model = ThresholdModel(
        model_version=int(data.get("model_version", 1)),
        rows=rows,
        quantile_probs=[float(p) for p in data.get("quantile_probs", QUANTILE_PROBS)],
        default_granularity=str(data.get("default_granularity", "window_segment")),
        tolerance_ratio=float(data.get("tolerance_ratio", 0.02)),
        calibration_path=list(data.get("calibration_path", [])),
        migration_sources=sources,
    )
    model.recompute_checksum()
    return model, sources


def upgrade_v1_to_v2(
    v1_path: str | Path,
    v2_dir: str | Path,
    known_segments: Iterable[str] | None = None,
) -> ThresholdModel:
    model, sources = read_v1(v1_path)
    upgrade_sources = list(sources)
    global_score = model.row(GLOBAL_SEGMENT, "score", EFFECTIVE_TIER)
    if global_score is None:
        any_score = next((row for row in model.rows if row.metric == "score"), None)
        if any_score is None:
            raise ThresholdFormatError("v1 does not contain score quantiles")
        global_score = ThresholdRow(
            segment_id=GLOBAL_SEGMENT,
            metric="score",
            tier=EFFECTIVE_TIER,
            granularity="flat",
            quantiles=dict(any_score.quantiles),
        )
        global_score.recompute_checksum()
        model.rows.append(global_score)
        upgrade_sources.append(
            {
                "direction": "v1_to_v2",
                "source": "v1:flat_score_quantiles",
                "target": f"v2:segment={GLOBAL_SEGMENT}",
                "action": "create_global_row",
            }
        )

    for segment in sorted(set(known_segments or [])):
        if model.row(segment, "score", EFFECTIVE_TIER) is not None:
            continue
        inherited = ThresholdRow(
            segment_id=segment,
            metric="score",
            tier=EFFECTIVE_TIER,
            granularity=model.default_granularity,
            quantiles=dict(global_score.quantiles),
            sample_count=global_score.sample_count,
            inherited_from=GLOBAL_SEGMENT,
        )
        inherited.recompute_checksum()
        model.rows.append(inherited)
        upgrade_sources.append(
            {
                "direction": "v1_to_v2",
                "source": f"v1:quantiles[{global_score.segment_id}]",
                "target": f"v2:segment={segment},tier={EFFECTIVE_TIER},granularity={model.default_granularity}",
                "action": "fill_default_segment_quantiles",
            }
        )

    for row in model.rows:
        if not row.granularity:
            row.granularity = model.default_granularity
            upgrade_sources.append(
                {
                    "direction": "v1_to_v2",
                    "source": f"v2:segment={row.segment_id}",
                    "target": "granularity",
                    "action": "fill_default_granularity",
                    "value": row.granularity,
                }
            )
        old_checksum = row.checksum
        row.recompute_checksum()
        if old_checksum != row.checksum:
            upgrade_sources.append(
                {
                    "direction": "v1_to_v2",
                    "source": f"row:{row.segment_id}:{row.tier}:{row.metric}",
                    "target": "row.checksum",
                    "action": "complete_checksum",
                    "checksum": row.checksum,
                }
            )

    model.migration_sources = upgrade_sources
    model.calibration_path = sorted(set(model.calibration_path + ["migration:v1_to_v2"]))
    model.recompute_checksum()
    write_v2(v2_dir, model)
    return model


def write_v2(directory: str | Path, model: ThresholdModel) -> Path:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)

    for old_shard in out.glob("shard_*.json"):
        old_shard.unlink()

    shard_manifests = []
    rows_by_segment: dict[str, list[ThresholdRow]] = {}
    for row in model.rows:
        row.recompute_checksum()
        rows_by_segment.setdefault(row.segment_id, []).append(row)

    for segment, rows in sorted(rows_by_segment.items()):
        shard_payload = {
            "format": f"{V2_FORMAT}-shard",
            "segment_id": segment,
            "rows": [row.to_dict() for row in sorted(rows, key=lambda r: (r.tier, r.metric))],
        }
        shard_payload["checksum"] = payload_checksum({k: v for k, v in shard_payload.items() if k != "checksum"})
        shard_name = f"shard_{_safe_name(segment)}.json"
        atomic_write_json(out / shard_name, shard_payload)
        shard_manifests.append(
            {"segment_id": segment, "file": shard_name, "checksum": shard_payload["checksum"]}
        )

    db_tmp = out / "thresholds.sqlite.tmp"
    if db_tmp.exists():
        db_tmp.unlink()
    _write_sqlite(db_tmp, model)
    db_target = out / "thresholds.sqlite"
    import os

    os.replace(db_tmp, db_target)

    manifest = {
        "format": V2_FORMAT,
        "schema_version": "2.0",
        "model_version": model.model_version,
        "default_granularity": model.default_granularity,
        "quantile_probs": model.quantile_probs,
        "tolerance_ratio": model.tolerance_ratio,
        "checksum": model.recompute_checksum(),
        "shards": shard_manifests,
        "sqlite": "thresholds.sqlite",
        "calibration_path": model.calibration_path,
        "migration_sources": model.migration_sources,
    }
    atomic_write_json(out / "model.json", manifest)
    return out / "model.json"


def read_v2(directory: str | Path) -> ThresholdModel:
    root = Path(directory)
    manifest_path = root / "model.json"
    if not manifest_path.exists():
        raise ThresholdFormatError(f"v2 manifest not found: {manifest_path}")
    manifest = read_json(manifest_path)
    if manifest.get("format") != V2_FORMAT:
        raise ThresholdFormatError(f"not a v2 threshold model: {manifest_path}")

    for shard_info in manifest.get("shards", []):
        shard_path = root / shard_info["file"]
        shard = read_json(shard_path)
        expected = payload_checksum({k: v for k, v in shard.items() if k != "checksum"})
        if expected != shard.get("checksum") or expected != shard_info.get("checksum"):
            from .errors import ChecksumMismatchError

            raise ChecksumMismatchError("v2 shard checksum mismatch", details={"shard": shard_info["file"]})

    db_path = root / "thresholds.sqlite"
    if not db_path.exists():
        raise ThresholdFormatError(f"v2 SQLite database not found: {db_path}")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM model_meta")}
        db_rows = [
            ThresholdRow(
                segment_id=row["segment_id"],
                metric=row["metric"],
                tier=row["tier"],
                granularity=row["granularity"],
                quantiles=__import__("json").loads(row["quantiles_json"]),
                sample_count=row["sample_count"],
                condition=__import__("json").loads(row["condition_json"]) if row["condition_json"] else None,
                inherited_from=row["inherited_from"],
                checksum=row["checksum"],
            )
            for row in conn.execute("SELECT * FROM quantiles ORDER BY segment_id,tier,metric")
        ]
        sources = [
            {k: __import__("json").loads(v) if k == "detail_json" and v else v for k, v in dict(row).items()}
            for row in conn.execute("SELECT direction,source,target,action,detail_json FROM migration_sources")
        ]

    for row in db_rows:
        expected_checksum = payload_checksum(
            {
                "segment_id": row.segment_id,
                "metric": row.metric,
                "tier": row.tier,
                "granularity": row.granularity,
                "quantiles": row.quantiles,
                "condition": row.condition,
                "inherited_from": row.inherited_from,
            }
        )
        if row.checksum and expected_checksum != row.checksum:
            from .errors import ChecksumMismatchError

            raise ChecksumMismatchError(
                "v2 row checksum mismatch",
                details={"segment": row.segment_id, "tier": row.tier, "metric": row.metric},
            )

    model = ThresholdModel(
        model_version=int(meta.get("model_version", manifest.get("model_version", 1))),
        rows=db_rows,
        quantile_probs=manifest.get("quantile_probs", list(QUANTILE_PROBS)),
        default_granularity=manifest.get("default_granularity", "window_segment"),
        tolerance_ratio=float(manifest.get("tolerance_ratio", 0.02)),
        calibration_path=__import__("json").loads(meta.get("calibration_path_json", "[]")),
        migration_sources=sources or manifest.get("migration_sources", []),
    )
    expected_model_checksum = model.recompute_checksum()
    if manifest.get("checksum") and manifest["checksum"] != expected_model_checksum:
        from .errors import ChecksumMismatchError

        raise ChecksumMismatchError(
            "v2 model checksum mismatch",
            details={"expected": expected_model_checksum, "manifest": manifest["checksum"]},
        )
    return model


def downgrade_v2_to_v1(v2_path: str | Path | ThresholdModel, v1_path: str | Path) -> tuple[Path, list[dict]]:
    model = v2_path if isinstance(v2_path, ThresholdModel) else read_v2(v2_path)
    records: list[dict] = []
    kept_rows: list[ThresholdRow] = []

    for row in model.rows:
        if row.tier != EFFECTIVE_TIER or row.condition is not None:
            records.append(
                {
                    "direction": "v2_to_v1",
                    "source": f"v2:{row.segment_id}:{row.tier}:{row.metric}",
                    "target": "v1:dropped",
                    "action": "drop_layered_condition",
                    "reason": "v1_flat_quantiles_cannot_express_tier_or_condition",
                    "tier": row.tier,
                    "condition": row.condition,
                }
            )
            continue
        kept = ThresholdRow.from_dict(row.to_dict())
        kept_rows.append(kept)
        for label in row.quantiles:
            if row.segment_id == GLOBAL_SEGMENT and row.metric == "score":
                target = f"v1:quantiles.{label}"
            else:
                target = f"v1:quantiles[segment.{row.segment_id}.{row.metric}.{label}]"
            records.append(
                {
                    "direction": "v2_to_v1",
                    "source": f"v2:{row.segment_id}:{row.tier}:{row.metric}:{label}",
                    "target": target,
                    "action": "flatten_effective_quantile",
                }
            )

    v1_model = ThresholdModel(
        model_version=model.model_version,
        rows=kept_rows,
        quantile_probs=model.quantile_probs,
        default_granularity=model.default_granularity,
        tolerance_ratio=model.tolerance_ratio,
        calibration_path=sorted(set(model.calibration_path + ["migration:v2_to_v1"])),
        migration_sources=model.migration_sources + records,
    )
    v1_model.recompute_checksum()
    path = write_v1(v1_path, v1_model)
    return path, records


def _parse_v1_key(key: str) -> tuple[str, str, str | None]:
    if key.startswith("segment.") or key.startswith("seg."):
        parts = key.split(".")
        if len(parts) >= 4:
            segment = parts[1]
            label = parts[-1]
            metric = ".".join(parts[2:-1]) or "score"
            return segment, metric, label
    if key.startswith("segment:"):
        remainder = key.split(":", 1)[1]
        pieces = re.split(r"[:|]", remainder)
        if len(pieces) >= 2:
            return pieces[0], "score", pieces[-1]
    if re.fullmatch(r"p\d{2,3}", key):
        return GLOBAL_SEGMENT, "score", key
    if re.fullmatch(r"0?\.\d+|1\.0", key):
        return GLOBAL_SEGMENT, "score", prob_label(float(key))
    return GLOBAL_SEGMENT, "score", None


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def _write_sqlite(path: Path, model: ThresholdModel) -> None:
    import json

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE model_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE segments (
                segment_id TEXT PRIMARY KEY,
                granularity TEXT NOT NULL,
                default_tier TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE quantiles (
                segment_id TEXT NOT NULL,
                granularity TEXT NOT NULL,
                tier TEXT NOT NULL,
                metric TEXT NOT NULL,
                quantiles_json TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                condition_json TEXT,
                inherited_from TEXT,
                checksum TEXT NOT NULL,
                PRIMARY KEY (segment_id, tier, metric)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE migration_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                action TEXT NOT NULL,
                detail_json TEXT
            )
            """
        )
        conn.executemany(
            "INSERT OR REPLACE INTO model_meta(key,value) VALUES(?,?)",
            [
                ("format", V2_FORMAT),
                ("model_version", str(model.model_version)),
                ("default_granularity", model.default_granularity),
                ("calibration_path_json", json.dumps(model.calibration_path, sort_keys=True)),
                ("checksum", model.checksum or model.recompute_checksum()),
            ],
        )
        segment_granularity = {row.segment_id: row.granularity for row in model.rows}
        conn.executemany(
            "INSERT OR REPLACE INTO segments(segment_id,granularity,default_tier) VALUES(?,?,?)",
            [(segment, granularity, EFFECTIVE_TIER) for segment, granularity in segment_granularity.items()],
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO quantiles(
                segment_id,granularity,tier,metric,quantiles_json,sample_count,
                condition_json,inherited_from,checksum
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    row.segment_id,
                    row.granularity,
                    row.tier,
                    row.metric,
                    json.dumps(row.quantiles, sort_keys=True),
                    int(row.sample_count),
                    json.dumps(row.condition, sort_keys=True) if row.condition is not None else None,
                    row.inherited_from,
                    row.checksum or row.recompute_checksum(),
                )
                for row in model.rows
            ],
        )
        conn.executemany(
            """
            INSERT INTO migration_sources(direction,source,target,action,detail_json)
            VALUES(?,?,?,?,?)
            """,
            [
                (
                    str(item.get("direction", "unknown")),
                    str(item.get("source", "")),
                    str(item.get("target", "")),
                    str(item.get("action", "")),
                    json.dumps(
                        {k: v for k, v in item.items() if k not in {"direction", "source", "target", "action"}},
                        sort_keys=True,
                        default=str,
                    ),
                )
                for item in model.migration_sources
            ],
        )
        conn.commit()
