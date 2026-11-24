from __future__ import annotations

import ipaddress
import struct
from pathlib import Path

from firewall_audit.codes import (
    E_BAD_CIDR_SUFFIX,
    E_BAD_EFFECTIVE_TIME,
    E_BAD_PORT_RANGE,
    E_BAD_PREFIX,
    E_BAD_PROTOCOL,
    E_BAD_RULE_KEY,
    E_DUP_CASE_ID,
    E_DUP_RULE_KEY,
)
from firewall_audit.models import PROTOCOL_NUMBERS
from firewall_audit.parser import parse_sample
from firewall_audit.samplegen import DEFAULT_SEED, OBSERVED_CLOCK_MS
from firewall_audit.views import build_views

from .conftest import load_jsonl


def test_sample_parses_and_normalizes(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    assert bundle.sample_id == f"firewall-equivalence-{DEFAULT_SEED}"
    assert bundle.observed_clock_ms == OBSERVED_CLOCK_MS
    assert len(bundle.cases) == 9
    assert len(bundle.rules) == 18
    assert len(bundle.rejections) == 10

    by_key = {(rule.side, rule.rule_key): rule for rule in bundle.rules}
    shared_v1 = by_key[("v1", "shared-web")]
    shared_v2 = by_key[("v2", "shared-web")]
    assert str(shared_v1.src_network) == "10.20.30.0/24"
    assert shared_v1.effective_ms == OBSERVED_CLOCK_MS
    assert shared_v1.src_ports.lo == 10000
    assert shared_v1.src_ports.hi == 20000
    assert shared_v2.src_ports.lo == 10000
    assert shared_v2.src_ports.hi == 20000
    assert shared_v2.dst_ports.hi == 80
    assert shared_v2.generation == 2

    alias_v1 = by_key[("v1", "v1-alias-dns")]
    alias_v2 = by_key[("v2", "v2-alias-dns")]
    assert alias_v1.effective_ms == OBSERVED_CLOCK_MS + 5000
    assert alias_v1.src_ports == alias_v2.src_ports
    assert alias_v1.dst_ports == alias_v2.dst_ports

    _, v2_view, _ = build_views(bundle.rules)
    assert "shared-retired" not in v2_view.active
    assert v2_view.shadow["shared-retired"].generation == 3
    assert v2_view.shadow["shared-retired"].tombstone is True


def test_rejections_have_source_index_code(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    rejections = {(item.source_category, item.code) for item in bundle.rejections}
    assert ("case", E_DUP_CASE_ID) in rejections
    assert ("case", E_BAD_PROTOCOL) in rejections
    assert ("v1_rule", E_BAD_PREFIX) in rejections
    assert ("v1_rule", E_BAD_PROTOCOL) in rejections
    assert ("v1_rule", E_BAD_PORT_RANGE) in rejections
    assert ("v1_rule", E_BAD_EFFECTIVE_TIME) in rejections
    assert ("v1_rule", E_DUP_RULE_KEY) in rejections
    assert ("v2_meta", E_BAD_CIDR_SUFFIX) in rejections
    assert ("v2_shard", E_BAD_PORT_RANGE) in rejections
    assert ("mapping", E_BAD_RULE_KEY) in rejections
    for rejection in bundle.rejections:
        assert isinstance(rejection.record_index, int)
        assert rejection.record_index >= 1
        assert rejection.code.startswith("E_")


def test_truth_file_raw_bytes_match_case_sender_tuple(
    generated_sample: Path,
) -> None:
    cases = {
        row["sync_marker"]: row
        for row in load_jsonl(generated_sample / "cases.jsonl")
        if row.get("sync_marker")
    }
    truth = load_jsonl(generated_sample / "truth.jsonl")
    assert len(truth) == 9
    markers = set()
    for item in truth:
        assert item["sync_marker"] not in markers
        markers.add(item["sync_marker"])
        case = cases[item["sync_marker"]]
        assert case["case_id"] == item["case_id"]
        wire = bytes.fromhex(item["wire_hex"])
        (
            src_bytes,
            src_len,
            dst_bytes,
            dst_len,
            protocol_number,
            src_lo,
            src_hi,
            dst_lo,
            dst_hi,
        ) = struct.unpack(item["wire_format"], wire)
        src = ipaddress.ip_network(case["src_prefix"], strict=False)
        dst = ipaddress.ip_network(case["dst_prefix"], strict=False)
        assert src_bytes == src.network_address.packed
        assert src_len == src.prefixlen
        assert dst_bytes == dst.network_address.packed
        assert dst_len == dst.prefixlen
        assert protocol_number == PROTOCOL_NUMBERS[case["protocol"]]
        assert f"{src_lo}-{src_hi}" == case["src_port_range"]
        assert f"{dst_lo}-{dst_hi}" == case["dst_port_range"]


def test_generator_is_deterministic(
    generated_sample: Path,
    tmp_path: Path,
) -> None:
    from firewall_audit.samplegen import generate

    second = tmp_path / "second"
    generate(second, DEFAULT_SEED)
    text_files = [
        "sample_manifest.json",
        "cases.jsonl",
        "truth.jsonl",
        "v1.json",
        "mapping.json",
        "v2/manifest.json",
        "v2/shard-000.json",
        "v2/shard-001.json",
        "incremental/mapping-v4.json",
        "incremental/mapping-patch-v4.json",
        "incremental/sample_manifest.json",
    ]
    for relative in text_files:
        assert (generated_sample / relative).read_bytes() == (
            second / relative
        ).read_bytes()
    import sqlite3

    def meta_rows(root: Path) -> list[tuple]:
        connection = sqlite3.connect(str(root / "v2" / "meta.sqlite3"))
        try:
            return list(
                connection.execute(
                    "SELECT record_id, rule_key, generation, effective_ms, "
                    "tombstone, src_cidr_suffix, dst_cidr_suffix, "
                    "src_port_end_closed, dst_port_end_closed "
                    "FROM rule_meta ORDER BY record_id"
                )
            )
        finally:
            connection.close()

    assert meta_rows(generated_sample) == meta_rows(second)
