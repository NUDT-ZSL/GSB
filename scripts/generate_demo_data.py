"""Generate the committed deterministic demo fixtures.

Run from the repository root:

    python scripts/generate_demo_data.py

The output is byte-stable for a fixed Python/PyArrow release.  JSONL, specs and
statistics are written with sorted keys; Parquet is only a secondary local
columnar representation of the same rows.
"""

from __future__ import annotations

import copy
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ctr_cvr_joint.hashing import canonical_json, sha256_bytes, sha256_obj
from ctr_cvr_joint.migration import migrate_spec
from ctr_cvr_joint.models import Sample
from ctr_cvr_joint.samples import write_samples_jsonl, write_samples_parquet
from ctr_cvr_joint.specs import load_spec_bytes, serialize_spec
from ctr_cvr_joint.statistics import parse_statistics


SEED = 20261124
N_SAMPLES = 12
SAMPLE_FIELDS = [
    "request_id",
    "timestamp_ms",
    "user_id",
    "item_id",
    "numeric_feats",
    "categorical_feats",
    "label_click",
    "label_convert",
]

V1_SPEC = """version=1
N|ctr_base|0.42|0.16|0.55
N|price|88|24|0.22
N|dwell|13.5|4.2|0.18
N|quality|0.67|0.13|0.16
C|device|0.18|32
C|slot|0.14|24
C|city|0.12|20
X|device_slot|device|slot|code,affinity|0.20
X|price_quality|price|quality|product,affinity|0.10
S|ctr|0.18|8
S|cvr|0.07|6
"""

V2_SPEC = {
    "format_version": 2,
    "features": {
        "numeric": [
            {
                "name": "ctr_base",
                "mean": 0.42,
                "std": 0.16,
                "weight": 0.55,
                "missing_strategy": "mean",
            },
            {
                "name": "price",
                "mean": 88.0,
                "std": 24.0,
                "weight": 0.22,
                "missing_strategy": "mean",
            },
            {
                "name": "dwell",
                "mean": 13.5,
                "std": 4.2,
                "weight": 0.18,
                "missing_strategy": "zero",
            },
            {
                "name": "quality",
                "mean": 0.67,
                "std": 0.13,
                "weight": 0.16,
                "missing_strategy": "mean",
            },
        ],
        "categorical": [
            {
                "name": "device",
                "weight": 0.18,
                "buckets": 32,
                "missing_strategy": "default",
            },
            {
                "name": "slot",
                "weight": 0.14,
                "buckets": 24,
                "missing_strategy": "default",
            },
            {
                "name": "city",
                "weight": 0.12,
                "buckets": 20,
                "missing_strategy": "default",
            },
        ],
        "cross": [
            {
                "name": "device_slot",
                "left": "device",
                "right": "slot",
                "weight": 0.20,
                "slots": ["code", "affinity"],
                "missing_strategy": "zero",
            },
            {
                "name": "price_quality",
                "left": "price",
                "right": "quality",
                "weight": 0.10,
                "slots": ["product", "affinity"],
                "missing_strategy": "zero",
            },
        ],
    },
    "statistics": {
        "ctr": {"prior": 0.18, "smoothing": 8.0},
        "cvr": {"prior": 0.07, "smoothing": 6.0},
    },
}

V3_SPEC = {
    "format_version": 3,
    "numeric_features": {
        "ctr_base": {
            "stats": {"mean": 0.42, "std": 0.16},
            "weight": 0.55,
            "default_value": 0.42,
            "missing": {"strategy": "default"},
        },
        "price": {
            "stats": {"mean": 88.0, "std": 24.0},
            "weight": 0.22,
            "default_value": 88.0,
            "missing": {"strategy": "default"},
        },
        "dwell": {
            "stats": {"mean": 13.5, "std": 4.2},
            "weight": 0.18,
            "default_value": 0.0,
            "missing": {"strategy": "default"},
        },
        "quality": {
            "stats": {"mean": 0.67, "std": 0.13},
            "weight": 0.16,
            "default_value": 0.67,
            "missing": {"strategy": "default"},
        },
    },
    "categorical_features": {
        "device": {
            "encoding": {"kind": "hash_mod", "buckets": 32},
            "weight": 0.18,
            "default_value": "unknown",
            "missing": {"strategy": "default"},
        },
        "slot": {
            "encoding": {"kind": "hash_mod", "buckets": 24},
            "weight": 0.14,
            "default_value": "unknown",
            "missing": {"strategy": "default"},
        },
        "city": {
            "encoding": {"kind": "hash_mod", "buckets": 20},
            "weight": 0.12,
            "default_value": "unknown",
            "missing": {"strategy": "default"},
        },
    },
    "cross_features": {
        "device_slot": {
            "left": "device",
            "right": "slot",
            "weight": 0.20,
            "slots": {"code": {"kind": "hash_mod"}, "affinity": {"kind": "numeric"}},
            "missing": {"strategy": "zero"},
        },
        "price_quality": {
            "left": "price",
            "right": "quality",
            "weight": 0.10,
            "slots": {"product": {"kind": "numeric"}, "affinity": {"kind": "numeric"}},
            "missing": {"strategy": "zero"},
        },
    },
    "statistics": {
        "ctr": {"prior": 0.18, "smoothing": 8.0},
        "cvr": {"prior": 0.07, "smoothing": 6.0},
    },
    "stats_semantics": {"name": "snapshot_bayes_decay", "version": 3, "decay": 1.0},
}

STATS_V1 = {
    "format_version": 1,
    "stats_version": 1,
    "global": {
        "ctr": {"prior": 0.18, "smoothing": 8.0},
        "cvr": {"prior": 0.07, "smoothing": 6.0},
    },
    "numeric": {
        "ctr_base": {
            "count": 1200,
            "mean": 0.42,
            "std": 0.16,
            "click_rate": 0.24,
            "convert_rate": 0.065,
        },
        "price": {
            "count": 1200,
            "mean": 88.0,
            "std": 24.0,
            "click_rate": 0.15,
            "convert_rate": 0.035,
        },
        "dwell": {
            "count": 1200,
            "mean": 13.5,
            "std": 4.2,
            "click_rate": 0.21,
            "convert_rate": 0.08,
        },
        "quality": {
            "count": 1200,
            "mean": 0.67,
            "std": 0.13,
            "click_rate": 0.27,
            "convert_rate": 0.075,
        },
    },
    "categorical": {
        "device": {
            "__ALL__": {"count": 1200, "click_rate": 0.19, "convert_rate": 0.07},
            "mobile": {"count": 700, "click_rate": 0.23, "convert_rate": 0.08},
            "desktop": {"count": 350, "click_rate": 0.13, "convert_rate": 0.05},
            "tablet": {"count": 150, "click_rate": 0.10, "convert_rate": 0.03},
        },
        "slot": {
            "__ALL__": {"count": 1200, "click_rate": 0.18, "convert_rate": 0.065},
            "feed": {"count": 500, "click_rate": 0.16, "convert_rate": 0.05},
            "hero": {"count": 350, "click_rate": 0.20, "convert_rate": 0.08},
            "reco": {"count": 350, "click_rate": 0.22, "convert_rate": 0.07},
        },
        "city": {
            "__ALL__": {"count": 1200, "click_rate": 0.18, "convert_rate": 0.07},
            "shanghai": {"count": 330, "click_rate": 0.21, "convert_rate": 0.08},
            "beijing": {"count": 300, "click_rate": 0.19, "convert_rate": 0.07},
            "shenzhen": {"count": 280, "click_rate": 0.17, "convert_rate": 0.06},
            "chengdu": {"count": 290, "click_rate": 0.16, "convert_rate": 0.05},
        },
    },
    "cross": {
        "device_slot": {"count": 1200, "click_rate": 0.20, "convert_rate": 0.07},
        "price_quality": {"count": 1200, "click_rate": 0.19, "convert_rate": 0.065},
    },
    "series_versions": {},
}


def build_samples() -> list[Sample]:
    rng = random.Random(SEED)
    devices = ["mobile", "desktop", "tablet"]
    slots = ["feed", "hero", "reco"]
    cities = ["shanghai", "beijing", "shenzhen", "chengdu"]
    missing_patterns = {
        1: {"price"},
        2: {"dwell", "quality"},
        4: {"ctr_base"},
        6: {"device"},
        8: {"price", "slot"},
        10: {"ctr_base", "city"},
    }
    samples: list[Sample] = []
    for index in range(N_SAMPLES):
        missing = missing_patterns.get(index, set())
        numeric = {
            "ctr_base": round(rng.uniform(0.08, 0.78), 3),
            "price": round(rng.uniform(18.0, 180.0), 2),
            "dwell": round(rng.uniform(2.0, 32.0), 2),
            "quality": round(rng.uniform(0.35, 0.98), 3),
        }
        for name in missing:
            numeric.pop(name, None)
        categorical = {
            "device": devices[index % len(devices)],
            "slot": slots[index % len(slots)],
            "city": cities[index % len(cities)],
        }
        for name in missing:
            categorical.pop(name, None)
        click = 1 if rng.random() < 0.58 else 0
        convert = 1 if click and rng.random() < 0.42 else 0
        samples.append(
            Sample(
                request_id=f"req-{index + 1:04d}",
                timestamp_ms=1_700_000_000_000 + index * 37 + rng.randint(0, 5),
                user_id=f"user-{index + 1:03d}",
                item_id=f"item-{101 + index * 7:04d}",
                numeric_feats=numeric,
                categorical_feats=categorical,
                label_click=click,
                label_convert=convert,
            )
        )
    return samples


def _series_versions() -> dict[str, int]:
    versions = {"global": 1}
    for feature in ("ctr_base", "price", "dwell", "quality"):
        versions[f"numeric.{feature}"] = 1
    for feature in ("device", "slot", "city"):
        versions[f"categorical.{feature}"] = 1
    for feature, values in {
        "device": ("mobile", "desktop", "tablet"),
        "slot": ("feed", "hero", "reco"),
        "city": ("shanghai", "beijing", "shenzhen", "chengdu"),
    }.items():
        for value in values:
            versions[f"categorical.{feature}.{value}"] = 1
    for feature in ("device_slot", "price_quality"):
        versions[f"cross.{feature}"] = 1
    return versions


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    data = Path(os.environ.get("CTR_CVR_JOINT_DATA_DIR", str(root / "data"))).resolve()
    data.mkdir(parents=True, exist_ok=True)
    samples = build_samples()

    (data / "spec_v1.txt").write_text(V1_SPEC, encoding="utf-8")
    _write_json(data / "spec_v2.json", V2_SPEC)
    _write_json(data / "spec_v3.json", V3_SPEC)
    v3_revision = copy.deepcopy(V3_SPEC)
    v3_revision["numeric_features"]["ctr_base"]["stats"]["mean"] = 0.44
    v3_revision["numeric_features"]["ctr_base"]["stats"]["std"] = 0.17
    v3_revision["numeric_features"]["ctr_base"]["default_value"] = 0.44
    _write_json(data / "spec_v3_rev2.json", v3_revision)

    stats_v1 = copy.deepcopy(STATS_V1)
    stats_v1["series_versions"] = _series_versions()
    _write_json(data / "stats_v1.json", stats_v1)
    stats_v2 = copy.deepcopy(stats_v1)
    stats_v2["stats_version"] = 2
    stats_v2["categorical"]["city"]["shenzhen"] = {
        "count": 280,
        "click_rate": 0.24,
        "convert_rate": 0.10,
    }
    stats_v2["series_versions"]["categorical.city.shenzhen"] = 2
    _write_json(data / "stats_v2.json", stats_v2)

    write_samples_jsonl(data / "requests.jsonl", samples)
    parquet_info = write_samples_parquet(data / "requests.parquet", samples)
    parquet_info["path"] = "data/requests.parquet"

    v1_loaded = load_spec_bytes((data / "spec_v1.txt").read_bytes())
    v2_loaded = load_spec_bytes((data / "spec_v2.json").read_bytes())
    v3_loaded = load_spec_bytes((data / "spec_v3.json").read_bytes())
    parsed_stats_v1 = parse_statistics(stats_v1)
    parsed_stats_v2 = parse_statistics(stats_v2)
    migration_v1_v2 = migrate_spec(v1_loaded, 2)
    migration_v1_v3 = migrate_spec(v1_loaded, 3)
    migration_v3_v2 = migrate_spec(v3_loaded, 2)
    migration_v3_v1 = migrate_spec(v3_loaded, 1)
    migration_v2_v3 = migrate_spec(v2_loaded, 3)

    file_checksums = {}
    for path in (
        data / "requests.jsonl",
        data / "requests.parquet",
        data / "spec_v1.txt",
        data / "spec_v2.json",
        data / "spec_v3.json",
        data / "spec_v3_rev2.json",
        data / "stats_v1.json",
        data / "stats_v2.json",
    ):
        file_checksums[path.name] = sha256_bytes(path.read_bytes())

    manifest = {
        "generator": "scripts/generate_demo_data.py",
        "seed": SEED,
        "sample_count": N_SAMPLES,
        "sample_fields": SAMPLE_FIELDS,
        "spec_versions": {
            "v1": v1_loaded.checksum,
            "v2": v2_loaded.checksum,
            "v3": v3_loaded.checksum,
        },
        "statistics_versions": {
            "v1": parsed_stats_v1.checksum,
            "v2": parsed_stats_v2.checksum,
        },
        "migrations": {
            "v1_to_v2": {
                "target_checksum": migration_v1_v2.target_checksum,
                "fills": len(migration_v1_v2.fills),
                "drops": len(migration_v1_v2.drops),
                "replay_ok": migration_v1_v2.replay_ok,
            },
            "v1_to_v3": {
                "target_checksum": migration_v1_v3.target_checksum,
                "fills": len(migration_v1_v3.fills),
                "drops": len(migration_v1_v3.drops),
                "replay_ok": migration_v1_v3.replay_ok,
            },
            "v2_to_v3": {
                "target_checksum": migration_v2_v3.target_checksum,
                "fills": len(migration_v2_v3.fills),
                "drops": len(migration_v2_v3.drops),
                "replay_ok": migration_v2_v3.replay_ok,
            },
            "v3_to_v2": {
                "target_checksum": migration_v3_v2.target_checksum,
                "fills": len(migration_v3_v2.fills),
                "drops": len(migration_v3_v2.drops),
                "replay_ok": migration_v3_v2.replay_ok,
            },
            "v3_to_v1": {
                "target_checksum": migration_v3_v1.target_checksum,
                "fills": len(migration_v3_v1.fills),
                "drops": len(migration_v3_v1.drops),
                "replay_ok": migration_v3_v1.replay_ok,
            },
        },
        "parquet": parquet_info,
        "files": file_checksums,
        "manifest_self_checksum": "",
    }
    manifest["manifest_self_checksum"] = sha256_obj(
        {key: value for key, value in manifest.items() if key != "manifest_self_checksum"}
    )
    _write_json(data / "MANIFEST.json", manifest)
    print(canonical_json(manifest))


if __name__ == "__main__":
    main()

