from __future__ import annotations

from osl_forensics.canonical import segment_key
from osl_forensics.parser import parse_osl
from osl_forensics.policy import Policy
from osl_forensics.writer import SegmentSpec, build_osl


def test_sha256_and_hmac_valid_digests_pass():
    sha = build_osl([SegmentSpec("sha", b"abc", compression=1, algo=1)])
    sha_result = parse_osl(sha.data, policy=Policy())
    assert sha_result.ok
    assert sha_result.segments[0].digest_valid is True

    hmac_sample = build_osl([SegmentSpec("hmac", b"abc", compression=2, algo=2)])
    hmac_result = parse_osl(hmac_sample.data, policy=Policy())
    assert hmac_result.ok
    assert hmac_result.segments[0].digest_valid is True


def test_checksum_mismatch_and_algorithm_width_are_reported():
    built = build_osl([SegmentSpec("sha", b"abc", compression=1, algo=1)])
    offset = int(built.record("sha")["digest_offset"])
    blob = bytearray(built.data)
    blob[offset] ^= 1
    result = parse_osl(bytes(blob))
    assert result.primary_code == "E_CHECKSUM"
    assert result.error_offset == offset

    width_built = build_osl([
        SegmentSpec(
            "width",
            b"abc",
            compression=1,
            algo=1,
            digest=b"\x11" * 16,
            digest_declared_length=16,
        )
    ])
    width_result = parse_osl(width_built.data)
    assert width_result.primary_code == "E_CHECK_ALGO"
    assert width_result.error_offset == int(width_built.record("width")["digest_length_offset"])

    unknown_built = build_osl([SegmentSpec("unknown", b"x", algo=99, digest=b"")])
    unknown_result = parse_osl(unknown_built.data)
    assert unknown_result.primary_code == "E_CHECK_ALGO"
    assert "unknown checksum algorithm 99" in unknown_result.primary.message


def test_nested_osl_expands_canonical_paths():
    child = build_osl([SegmentSpec("inner", b"payload", compression=1, algo=1)])
    outer = build_osl([
        SegmentSpec("outer", child.data, compression=0x10, segment_type=4)
    ])
    result = parse_osl(outer.data)
    assert result.ok
    assert segment_key(["outer", "inner"]) in {segment.key for segment in result.segments}
    assert ("/outer", "/outer/inner") in result.edges


def test_depth_limit_and_cycle_keys():
    leaf = build_osl([SegmentSpec("leaf", b"leaf")]).data
    current = build_osl([SegmentSpec("n4", leaf, compression=0x10, segment_type=4)])
    blob = current.data
    for level in range(3, -1, -1):
        blob = build_osl([
            SegmentSpec(f"n{level}", blob, compression=0x10, segment_type=4)
        ]).data

    depth_result = parse_osl(blob, policy=Policy(max_depth=4))
    assert depth_result.primary_code == "E_DEPTH"

    child = build_osl([
        SegmentSpec("ref", segment_type=3, refs=[segment_key("root")])
    ])
    cycle_sample = build_osl([
        SegmentSpec("root", child.data, compression=0x10, segment_type=4)
    ])
    cycle_result = parse_osl(cycle_sample.data)
    assert cycle_result.primary_code == "E_CYCLE"
    cycle = cycle_result.primary.details["cycle"]
    assert "/root" in cycle
    assert "/root/ref" in cycle


def test_expanded_byte_quota_is_cumulative():
    child = build_osl([SegmentSpec("child", b"q" * 128)])
    parent = build_osl([
        SegmentSpec("outer", child.data, compression=0x10, segment_type=4)
    ])
    quota_result = parse_osl(
        parent.data,
        policy=Policy(max_expanded_bytes=len(parent.data)),
    )
    assert quota_result.primary_code == "E_QUOTA"
    assert quota_result.error_offset == int(parent.record("outer")["physical_payload_offset"])
