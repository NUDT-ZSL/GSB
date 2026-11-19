from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from flash_attn_cache.models import AttentionCase
from flash_attn_cache.samples import array_paths


DEFAULT_CASES: tuple[dict, ...] = (
    {
        "case_id": "basic_causal",
        "seq_len": 10,
        "head_dim": 4,
        "n_heads": 2,
        "block_size": 4,
        "causal": True,
        "dtype": "float64",
        "seed": 101,
        "kv_revision": 0,
    },
    {
        "case_id": "dense_float32",
        "seq_len": 8,
        "head_dim": 3,
        "n_heads": 1,
        "block_size": 4,
        "causal": False,
        "dtype": "float32",
        "seed": 202,
        "kv_revision": [0, 0],
    },
    {
        "case_id": "partial_tail",
        "seq_len": 13,
        "head_dim": 5,
        "n_heads": 2,
        "block_size": 4,
        "causal": True,
        "dtype": "float64",
        "seed": 303,
        "kv_revision": [2, 2, 2, 2],
    },
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate(samples_dir: Path) -> dict:
    samples_dir.mkdir(parents=True, exist_ok=True)
    arrays_dir = samples_dir / "arrays"
    arrays_dir.mkdir(parents=True, exist_ok=True)
    cases_path = samples_dir / "cases.jsonl"
    checksums: dict[str, str] = {}
    with cases_path.open("w", encoding="utf-8", newline="\n") as handle:
        for payload in DEFAULT_CASES:
            case = AttentionCase.from_dict(payload)
            handle.write(
                json.dumps(
                    case.to_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            rng = np.random.default_rng(case.seed)
            dtype = np.dtype(case.dtype)
            shape = (case.n_heads, case.seq_len, case.head_dim)
            q = rng.standard_normal(shape, dtype=dtype)
            k = rng.standard_normal(shape, dtype=dtype)
            v = rng.standard_normal(shape, dtype=dtype)
            q_path, k_path, v_path = array_paths(samples_dir, case.case_id)
            np.save(q_path, q, allow_pickle=False)
            np.save(k_path, k, allow_pickle=False)
            np.save(v_path, v, allow_pickle=False)
            for name, path in (
                ("q", q_path),
                ("k", k_path),
                ("v", v_path),
            ):
                checksums[f"{case.case_id}.{name}"] = _sha256(path)
    manifest = {
        "generator": "scripts/generate_samples.py",
        "array_layout": {
            "shape": ["n_heads", "seq_len", "head_dim"],
            "storage": "one deterministic .npy file per Q/K/V tensor",
        },
        "cases_sha256": _sha256(cases_path),
        "arrays": dict(sorted(checksums.items())),
    }
    manifest_path = samples_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="samples",
        help="sample directory (default: samples)",
    )
    args = parser.parse_args()
    manifest = generate(Path(args.output))
    print(
        json.dumps(
            {
                "output": str(Path(args.output)),
                "cases": len(DEFAULT_CASES),
                "array_files": len(manifest["arrays"]),
                "cases_sha256": manifest["cases_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
