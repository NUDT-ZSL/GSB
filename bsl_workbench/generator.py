from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .builder import (
    SegmentSpec,
    build_bsl,
    minimal_raw_file,
    patch_u64,
    raw_record_with_name_bytes,
)
from .codes import DEFAULT_HMAC_KEY
from .parser import ParseOptions, parse_file
from .util import atomic_write_json


DEFAULT_SEED = 20261125


def _ref_payload(*references: str) -> bytes:
    return json.dumps({"refs": list(references)}, separators=(",", ":")).encode("utf-8")


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def generate_tree(output: Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    output.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}

    alpha_payload = bytes(rng.bytes(48))
    beta_payload = b"deterministic-hmac-payload-" + bytes(rng.bytes(8))
    basic = build_bsl(
        [
            SegmentSpec("alpha", alpha_payload, 0x01, 1),
            SegmentSpec("beta", beta_payload, 0x01, 2),
            SegmentSpec("gamma", b"no-digest-required", 0x01, 0),
        ],
        hmac_key=DEFAULT_HMAC_KEY,
    )
    _write(output / "valid" / "valid_basic.bsl", basic.data)
    files["valid/valid_basic.bsl"] = {"expect_codes": [], "file_id": "valid_basic"}

    compact = build_bsl(
        [SegmentSpec("compact", b"header-offset-32", 0x01, 1)],
        table_offset=32,
    )
    _write(output / "valid" / "header_offset32_compat.bsl", compact.data)
    files["valid/header_offset32_compat.bsl"] = {
        "expect_codes": [],
        "file_id": "header32",
    }

    bad_magic = bytearray(basic.data)
    bad_magic[0:4] = b"XXXX"
    _write(output / "invalid" / "malformed_bad_magic.bsl", bytes(bad_magic))
    files["invalid/malformed_bad_magic.bsl"] = {"expect_codes": ["E_HEADER"]}

    bad_version = bytearray(basic.data)
    bad_version[4:6] = (9).to_bytes(2, "little")
    _write(output / "invalid" / "malformed_bad_version.bsl", bytes(bad_version))
    files["invalid/malformed_bad_version.bsl"] = {"expect_codes": ["E_HEADER"]}

    _write(output / "invalid" / "malformed_truncated.bsl", basic.data[:20])
    files["invalid/malformed_truncated.bsl"] = {"expect_codes": ["E_HEADER"]}

    invalid_name = minimal_raw_file(
        [raw_record_with_name_bytes(b"\xff\xfe-invalid", payload_length=0)]
    )
    _write(output / "invalid" / "invalid_utf8_name.bsl", invalid_name)
    files["invalid/invalid_utf8_name.bsl"] = {"expect_codes": ["E_HEADER"]}

    bad_type = build_bsl([SegmentSpec("weird-type", b"x", 0xEE, 0)])
    _write(output / "invalid" / "unknown_segment_type.bsl", bad_type.data)
    files["invalid/unknown_segment_type.bsl"] = {"expect_codes": ["E_HEADER"]}

    oob_built = build_bsl([SegmentSpec("oob", b"four", 0x01, 0)])
    oob = bytearray(oob_built.data)
    oob_record = oob_built.for_name("oob")
    patch_u64(oob, oob_record.payload_length_field, len(oob) + 4096)
    _write(output / "invalid" / "out_of_bounds_length.bsl", bytes(oob))
    files["invalid/out_of_bounds_length.bsl"] = {"expect_codes": ["E_BOUNDS"]}

    overflow = bytearray(oob_built.data)
    patch_u64(overflow, oob_record.payload_offset_field, (1 << 64) - 1)
    patch_u64(overflow, oob_record.payload_length_field, (1 << 64) - 1)
    _write(output / "invalid" / "out_of_bounds_overflow.bsl", bytes(overflow))
    files["invalid/out_of_bounds_overflow.bsl"] = {"expect_codes": ["E_BOUNDS"]}

    overlap_built = build_bsl(
        [
            SegmentSpec("alpha", bytes(rng.bytes(32)), 0x01, 0),
            SegmentSpec("beta", bytes(rng.bytes(32)), 0x01, 0),
        ]
    )
    overlap = bytearray(overlap_built.data)
    alpha_record = overlap_built.for_name("alpha")
    beta_record = overlap_built.for_name("beta")
    patch_u64(overlap, beta_record.payload_offset_field, alpha_record.payload_offset + 16)
    _write(output / "invalid" / "overlap_partial.bsl", bytes(overlap))
    files["invalid/overlap_partial.bsl"] = {"expect_codes": ["E_OVERLAP"]}

    containment = bytearray(overlap_built.data)
    patch_u64(
        containment, beta_record.payload_offset_field, alpha_record.payload_offset + 8
    )
    patch_u64(containment, beta_record.payload_length_field, 16)
    _write(output / "invalid" / "overlap_containment.bsl", bytes(containment))
    files["invalid/overlap_containment.bsl"] = {"expect_codes": ["E_OVERLAP"]}

    self_built = build_bsl([SegmentSpec("table_ptr", b"self-reference", 0x01, 0)])
    selfref = bytearray(self_built.data)
    self_record = self_built.for_name("table_ptr")
    patch_u64(selfref, self_record.payload_offset_field, self_built.table_offset)
    patch_u64(selfref, self_record.payload_length_field, 16)
    _write(output / "invalid" / "self_reference.bsl", bytes(selfref))
    files["invalid/self_reference.bsl"] = {"expect_codes": ["E_SELFREF"]}

    duplicate = build_bsl(
        [
            SegmentSpec("dup", b"first-truth", 0x01, 1),
            SegmentSpec("dup", b"second-conflict", 0x01, 1),
            SegmentSpec("other", b"x", 0x01, 0),
        ]
    )
    _write(output / "invalid" / "duplicate_name.bsl", duplicate.data)
    files["invalid/duplicate_name.bsl"] = {"expect_codes": ["E_DUP_NAME"]}

    checksum_bad = build_bsl(
        [
            SegmentSpec(
                "sha_bad",
                b"actual-content",
                0x01,
                1,
                digest=hashlib.sha256(b"declared-but-wrong").digest(),
            )
        ]
    )
    _write(output / "invalid" / "checksum_mismatch.bsl", checksum_bad.data)
    files["invalid/checksum_mismatch.bsl"] = {"expect_codes": ["E_CHECKSUM"]}

    digest_width = build_bsl(
        [SegmentSpec("sha_zero_digest", b"content", 0x01, 1, digest=bytes(32))]
    )
    _write(output / "invalid" / "digest_width.bsl", digest_width.data)
    files["invalid/digest_width.bsl"] = {"expect_codes": ["E_DIGEST_WIDTH"]}

    unknown_comp = build_bsl([SegmentSpec("future_compression", b"x", 0x01, 9)])
    _write(output / "invalid" / "unknown_compression.bsl", unknown_comp.data)
    files["invalid/unknown_compression.bsl"] = {"expect_codes": ["E_COMPRESSION"]}

    deep_data = _deep_nesting(segment_levels=6)
    _write(output / "nested" / "deep_nesting.bsl", deep_data)
    files["nested/deep_nesting.bsl"] = {"expect_codes": ["E_DEPTH"]}

    cycle_data = _cycle_file()
    _write(output / "nested" / "cycle_reference.bsl", cycle_data)
    files["nested/cycle_reference.bsl"] = {"expect_codes": ["E_CYCLE"]}

    expand_data = _expand_file(rng)
    _write(output / "nested" / "expand_limit.bsl", expand_data)
    files["nested/expand_limit.bsl"] = {
        "expect_codes": ["E_EXPAND_LIMIT"],
        "options": {"max_expanded_bytes": 256},
    }

    revision_paths = _write_revisions(output)
    files.update(revision_paths)

    recovery_paths = _write_recovery(output)
    files.update(recovery_paths)

    manifest = {
        "schema_version": "bsl-sample-manifest-v1",
        "seed": seed,
        "hmac_key_hex": DEFAULT_HMAC_KEY.hex(),
        "files": files,
        "revision_file_id": "revision_demo",
        "recovery_files": [
            "recovery/recovery_a.bsl",
            "recovery/recovery_b.bsl",
            "recovery/recovery_c.bsl",
        ],
    }
    _write_diagnostic_snapshots(output, manifest)
    manifest["snapshot_directory"] = "snapshots"
    atomic_write_json(output / "manifest.json", manifest)
    return manifest


def _write_diagnostic_snapshots(output: Path, manifest: dict[str, Any]) -> None:
    snapshot_dir = output / "snapshots"
    for relative, meta in manifest["files"].items():
        options = ParseOptions(**meta.get("options", {}))
        report = parse_file(
            output / relative,
            file_id=meta.get("file_id", Path(relative).stem),
            options=options,
        )
        report["file"]["path"] = relative
        snapshot_name = relative.replace("/", "__").replace(".bsl", ".json")
        atomic_write_json(snapshot_dir / snapshot_name, report)


def _deep_nesting(segment_levels: int) -> bytes:
    current = build_bsl([SegmentSpec("leaf", b"leaf", 0x01, 0)]).data
    for level in range(segment_levels):
        name = f"n{segment_levels - 1 - level}"
        current = build_bsl([SegmentSpec(name, current, 0x10, 1)]).data
    return current


def _cycle_file() -> bytes:
    inner = build_bsl(
        [
            SegmentSpec(
                "back",
                _ref_payload("anchor"),
                0x20,
                0,
            ),
            SegmentSpec("inner_data", b"inner-payload", 0x01, 1),
        ]
    ).data
    return build_bsl(
        [
            SegmentSpec("anchor", inner, 0x10, 1),
            SegmentSpec("outer_other", b"outer", 0x01, 0),
        ]
    ).data


def _expand_file(rng: np.random.Generator) -> bytes:
    blob = bytes(rng.bytes(2048))
    inner = build_bsl([SegmentSpec("large_blob", blob, 0x01, 0)]).data
    return build_bsl([SegmentSpec("large_nested", inner, 0x10, 1)]).data


def _write_revisions(output: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for batch, base_value in (("v1", b"base-content-v1"), ("v2", b"base-content-v2")):
        data = build_bsl(
            [
                SegmentSpec("top", _ref_payload("mid"), 0x20, 0),
                SegmentSpec("mid", _ref_payload("base"), 0x20, 0),
                SegmentSpec("base", base_value, 0x01, 1),
            ]
        ).data
        relative = f"revisions/{batch}/revision_demo.bsl"
        _write(output / relative, data)
        result[relative] = {
            "expect_codes": [],
            "file_id": "revision_demo",
            "revision_batch": batch,
        }
    return result


def _write_recovery(output: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for letter in ("a", "b", "c"):
        data = build_bsl(
            [
                SegmentSpec(f"{letter}_one", f"payload-{letter}-1".encode(), 0x01, 1),
                SegmentSpec(f"{letter}_two", f"payload-{letter}-2".encode(), 0x01, 2),
            ],
            hmac_key=DEFAULT_HMAC_KEY,
        ).data
        relative = f"recovery/recovery_{letter}.bsl"
        _write(output / relative, data)
        result[relative] = {"expect_codes": [], "file_id": f"recovery_{letter}"}
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic local BSL verification samples."
    )
    parser.add_argument("--output", default="samples/generated", help="output directory")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="fixed RNG seed")
    args = parser.parse_args(argv)
    manifest = generate_tree(Path(args.output), seed=args.seed)
    print(
        json.dumps(
            {
                "generated": len(manifest["files"]),
                "output": args.output,
                "seed": args.seed,
                "manifest": str(Path(args.output) / "manifest.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
