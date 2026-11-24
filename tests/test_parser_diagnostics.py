from __future__ import annotations

import json
from pathlib import Path

import pytest

from bsl_workbench.parser import ParseOptions, parse_bytes, parse_file


def _parse_manifest_file(sample_root: Path, manifest: dict, relative: str, **options):
    file_meta = manifest["files"][relative]
    return parse_file(
        sample_root / relative,
        file_id=file_meta.get("file_id", "test-file"),
        options=ParseOptions(**options),
    )


def test_every_manifest_sample_reports_expected_code(
    sample_root: Path, manifest: dict
) -> None:
    failures = []
    for relative, meta in manifest["files"].items():
        options = meta.get("options", {})
        report = parse_file(
            sample_root / relative,
            file_id=meta.get("file_id", "manifest-file"),
            options=ParseOptions(**options),
        )
        codes = report["summary"]["error_codes"]
        for expected in meta.get("expect_codes", []):
            if expected not in codes:
                failures.append((relative, expected, codes))
        if not meta.get("expect_codes") and codes:
            failures.append((relative, None, codes))
    assert failures == []


def test_valid_basic_segments_and_digests(sample_root: Path, manifest: dict) -> None:
    report = _parse_manifest_file(
        sample_root, manifest, "valid/valid_basic.bsl"
    )
    assert report["ok"] is True
    assert [segment["name"] for segment in report["segments"]] == [
        "alpha",
        "beta",
        "gamma",
    ]
    statuses = {segment["name"]: segment["digest_status"] for segment in report["segments"]}
    assert statuses["alpha"] == "verified"
    assert statuses["beta"] == "verified"
    assert statuses["gamma"] == "not_verified"


@pytest.mark.parametrize(
    "relative,code",
    [
        ("invalid/malformed_bad_magic.bsl", "E_HEADER"),
        ("invalid/malformed_bad_version.bsl", "E_HEADER"),
        ("invalid/malformed_truncated.bsl", "E_HEADER"),
        ("invalid/invalid_utf8_name.bsl", "E_HEADER"),
        ("invalid/unknown_segment_type.bsl", "E_HEADER"),
        ("invalid/out_of_bounds_length.bsl", "E_BOUNDS"),
        ("invalid/out_of_bounds_overflow.bsl", "E_BOUNDS"),
    ],
)
def test_header_and_bounds_errors_have_offsets(
    sample_root: Path, manifest: dict, relative: str, code: str
) -> None:
    report = _parse_manifest_file(sample_root, manifest, relative)
    diagnostics = [item for item in report["diagnostics"] if item["code"] == code]
    assert diagnostics
    assert all(isinstance(item["offset"], int) for item in diagnostics)
    if code == "E_HEADER":
        assert report["preflight_failed"] is True


def test_overlap_reports_conflict_pair_and_kind(
    sample_root: Path, manifest: dict
) -> None:
    partial = _parse_manifest_file(sample_root, manifest, "invalid/overlap_partial.bsl")
    diagnostic = next(item for item in partial["diagnostics"] if item["code"] == "E_OVERLAP")
    assert diagnostic["details"]["segment_pair"] == ["alpha", "beta"]
    assert diagnostic["details"]["overlap_kind"] == "partial"

    containment = _parse_manifest_file(
        sample_root, manifest, "invalid/overlap_containment.bsl"
    )
    kinds = {
        item["details"]["overlap_kind"]
        for item in containment["diagnostics"]
        if item["code"] == "E_OVERLAP"
    }
    assert "contains" in kinds or "contained_by" in kinds


def test_self_reference_points_to_segment_table(
    sample_root: Path, manifest: dict
) -> None:
    report = _parse_manifest_file(sample_root, manifest, "invalid/self_reference.bsl")
    diagnostic = next(item for item in report["diagnostics"] if item["code"] == "E_SELFREF")
    assert diagnostic["details"]["segment_pair"][0] == "table_ptr"
    assert diagnostic["details"]["table_offset"] <= diagnostic["details"]["payload_offset"]


def test_duplicate_name_uses_first_offset(sample_root: Path, manifest: dict) -> None:
    report = _parse_manifest_file(sample_root, manifest, "invalid/duplicate_name.bsl")
    diagnostic = next(item for item in report["diagnostics"] if item["code"] == "E_DUP_NAME")
    details = diagnostic["details"]
    assert details["first_offset"] < details["duplicate_offset"] == diagnostic["offset"]
    assert len(report["segments"]) == 2
    assert report["segments"][0]["name"] == "dup"


def test_digest_and_compression_codes(sample_root: Path, manifest: dict) -> None:
    checksum = _parse_manifest_file(sample_root, manifest, "invalid/checksum_mismatch.bsl")
    assert any(item["code"] == "E_CHECKSUM" for item in checksum["diagnostics"])

    width = _parse_manifest_file(sample_root, manifest, "invalid/digest_width.bsl")
    width_diag = next(item for item in width["diagnostics"] if item["code"] == "E_DIGEST_WIDTH")
    assert width_diag["details"]["expected_bits"] == 256
    assert width_diag["details"]["declared_bits"] == 0

    compression = _parse_manifest_file(
        sample_root, manifest, "invalid/unknown_compression.bsl"
    )
    assert any(item["code"] == "E_COMPRESSION" for item in compression["diagnostics"])


def test_deep_nesting_depth(sample_root: Path, manifest: dict) -> None:
    report = _parse_manifest_file(sample_root, manifest, "nested/deep_nesting.bsl")
    diagnostic = next(item for item in report["diagnostics"] if item["code"] == "E_DEPTH")
    assert diagnostic["details"]["child_depth"] > diagnostic["details"]["max_depth"]


def test_cycle_lists_stable_name_sequence(sample_root: Path, manifest: dict) -> None:
    report = _parse_manifest_file(sample_root, manifest, "nested/cycle_reference.bsl")
    diagnostic = next(item for item in report["diagnostics"] if item["code"] == "E_CYCLE")
    assert diagnostic["details"]["cycle"] == ["anchor", "back", "anchor"]
    assert diagnostic["details"]["cycle_keys"] == [
        "/anchor",
        "/anchor/back",
        "/anchor",
    ]


def test_expand_quota(sample_root: Path, manifest: dict) -> None:
    report = _parse_manifest_file(
        sample_root,
        manifest,
        "nested/expand_limit.bsl",
        max_expanded_bytes=256,
    )
    diagnostic = next(
        item for item in report["diagnostics"] if item["code"] == "E_EXPAND_LIMIT"
    )
    assert diagnostic["details"]["attempted_total"] > diagnostic["details"]["max_expanded_bytes"]


def test_reports_are_json_reproducible(sample_root: Path, manifest: dict) -> None:
    path = sample_root / "valid/valid_basic.bsl"
    first = parse_file(path, file_id="repro")
    second = parse_bytes(path.read_bytes(), file_id="repro", path=str(path))
    first_json = json.dumps(first, sort_keys=True, ensure_ascii=False, indent=2)
    second_json = json.dumps(second, sort_keys=True, ensure_ascii=False, indent=2)
    assert first_json == second_json
