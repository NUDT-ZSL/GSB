from __future__ import annotations

import itertools
import time

import pytest

from sensitive_guard.engine import EngineOptions, analyze_dataset
from sensitive_guard.generator import build_samples

from conftest import make_dataset, make_record, semantic_result


def _sample(dataset_id: str) -> dict:
    return next(
        dataset
        for dataset in build_samples()
        if dataset["dataset_id"] == dataset_id
    )


def test_v1_stops_after_one_hop_but_child_own_marker_continues() -> None:
    records = [
        make_record("a", [], "plaintext", version="v1"),
        make_record("b", ["a"], "masked", transitive=True),
        make_record("c", ["b"], "masked", transitive=True),
    ]
    result = analyze_dataset(make_dataset(records))
    fields = {field["path"]: field for field in result["fields"]}

    assert fields["b"]["effective_status"] == "plaintext"
    assert fields["c"]["effective_status"] == "masked"
    assert fields["c"]["truth_sources"] == ["b", "c"]
    assert result["codes"] == ["E_DOWNGRADE"]


def test_v2_true_extends_and_v2_false_is_direct_only() -> None:
    deep = [
        make_record("a", [], "plaintext", transitive=True),
        make_record("b", ["a"], "masked", transitive=True),
        make_record("c", ["b"], "masked", transitive=True),
    ]
    direct = [
        make_record("a", [], "plaintext", transitive=False),
        make_record("b", ["a"], "masked", transitive=True),
        make_record("c", ["b"], "masked", transitive=True),
    ]

    deep_fields = {
        field["path"]: field
        for field in analyze_dataset(make_dataset(deep))["fields"]
    }
    direct_fields = {
        field["path"]: field
        for field in analyze_dataset(make_dataset(direct))["fields"]
    }

    assert deep_fields["c"]["effective_status"] == "plaintext"
    assert deep_fields["c"]["truth_sources"] == ["a"]
    assert direct_fields["c"]["effective_status"] == "masked"


def test_conflicting_source_statuses_use_fixed_priority() -> None:
    records = [
        make_record("plain", [], "plaintext"),
        make_record("masked", [], "masked"),
        make_record("hidden", [], "invisible"),
        make_record("out", ["hidden", "masked", "plain"], "masked"),
    ]
    result = analyze_dataset(make_dataset(records))
    field = next(item for item in result["fields"] if item["path"] == "out")

    assert field["propagated_status"] == "plaintext"
    assert field["effective_status"] == "plaintext"
    assert field["truth_sources"] == ["plain"]
    assert [item["code"] for item in result["violations"]].count("E_DOWNGRADE") == 1
    assert [item["code"] for item in result["violations"]].count("E_LEAK") == 1


def test_order_independence_across_all_record_permutations() -> None:
    dataset = _sample("order_independence")
    baseline = analyze_dataset(dataset)
    assert baseline["sync_marker_ok"] is True
    baseline_semantics = semantic_result(baseline)

    for permutation in itertools.permutations(dataset["records"]):
        permuted = dict(dataset)
        permuted["records"] = [
            {**record, "sources": list(reversed(record["sources"]))}
            for record in permutation
        ]
        result = analyze_dataset(permuted)
        assert result["sync_marker_ok"] is True
        assert semantic_result(result) == baseline_semantics

    field = next(
        item for item in baseline["fields"] if item["path"] == "order[0].leaf"
    )
    assert field["effective_status"] == "plaintext"
    assert baseline["duplicate_edges"] == [
        {
            "target": "order[0].masked",
            "source": "order[0].seed",
            "occurrences": 2,
            "duplicates": 1,
        }
    ]


@pytest.mark.parametrize("length", [1, 2, 5, 12])
def test_cycles_are_detected_with_stable_canonical_path(length: int) -> None:
    if length == 1:
        records = [make_record("loop.value", ["loop.value"], "masked")]
    else:
        records = [
            make_record(
                f"loop[{index}].value",
                [f"loop[{(index + 1) % length}].value"],
                "masked",
            )
            for index in range(length)
        ]
    dataset = make_dataset(
        records,
        injection={"force_cycle_length": length},
    )
    first = analyze_dataset(dataset)
    assert "E_CYCLE" in first["codes"]
    assert len(first["cycles"]) == 1
    cycle = first["cycles"][0]
    assert cycle["length"] == length
    assert cycle["path"][0] == cycle["path"][-1]
    assert len(cycle["path"]) == length + 1
    assert cycle["path"][0] == min(cycle["path"][:-1])

    variants = []
    for index, permutation in enumerate(itertools.permutations(records)):
        if index >= 12:
            break
        variants.append(list(permutation))
    variants.extend(
        [
            list(reversed(records)),
            records[1:] + records[:1],
            records[::2] + records[1::2],
        ]
    )

    for permutation in variants:
        permuted = dict(dataset)
        permuted["records"] = list(permutation)
        result = analyze_dataset(permuted)
        assert result["cycles"] == first["cycles"]


def test_version_conflict_lists_all_conflicting_source_keys() -> None:
    result = analyze_dataset(_sample("version_mix"))
    conflict = next(
        item
        for item in result["violations"]
        if item["code"] == "E_VERSION_CONFLICT"
    )

    assert "E_VERSION_CONFLICT" in result["codes"]
    assert conflict["field"] == "mix[0].out"
    assert conflict["source_keys"] == ["mix[0].legacy", "mix[0].modern"]
    assert conflict["versions"] == ["v1", "v2"]


def test_downgrade_and_leak_have_source_diagnostics() -> None:
    result = analyze_dataset(_sample("downgrade_leak"))
    downgrade = next(
        item for item in result["violations"] if item["code"] == "E_DOWNGRADE"
    )
    leak = next(item for item in result["violations"] if item["code"] == "E_LEAK")

    assert downgrade["field"] == "policy[0].view"
    assert downgrade["sources"] == [{"path": "policy[0].secret", "status": "plaintext"}]
    assert leak["sources"] == [{"path": "policy[0].closed", "status": "invisible"}]


def test_deep_chain_has_order_independent_truth_source() -> None:
    dataset = _sample("deep_chain")
    baseline = analyze_dataset(dataset)
    fields = {field["path"]: field for field in baseline["fields"]}
    leaf = fields["deep[15].value"]

    assert leaf["effective_status"] == "plaintext"
    assert leaf["truth_sources"] == ["deep[0].value"]
    assert baseline["observations"]["inconsistent"] == 0

    reversed_records = list(reversed(dataset["records"]))
    reversed_dataset = dict(dataset)
    reversed_dataset["records"] = reversed_records
    assert semantic_result(analyze_dataset(reversed_dataset)) == semantic_result(
        baseline
    )


def test_invalid_records_are_rejected_without_stopping_remaining_records() -> None:
    records = [
        make_record("valid.root", [], "masked"),
        make_record("valid.child", ["valid.root"], "masked"),
        make_record("dup", [], "masked"),
        make_record("dup", [], "masked"),
        make_record("bad.status", [], "public"),
        make_record("bad.version", [], "masked", version="v3"),
        make_record("missing.source", ["nowhere"], "masked"),
        make_record("cascade", ["missing.source"], "masked"),
    ]
    result = analyze_dataset(make_dataset(records))

    assert "E_RECORD" in result["codes"]
    assert result["processed_field_count"] == 2
    assert {field["path"] for field in result["fields"]} == {
        "valid.child",
        "valid.root",
    }
    rejected = {item["record_index"]: item for item in result["rejected_records"]}
    assert set(rejected) == {3, 4, 5, 6, 7, 8}
    for item in rejected.values():
        assert all(reason["code"] == "E_RECORD" for reason in item["reasons"])
    assert any(
        reason["detail"] == "duplicate_path" for reason in rejected[3]["reasons"]
    )
    assert any(
        reason["detail"] == "source_target_missing"
        for reason in rejected[8]["reasons"]
    )


def test_chinese_status_names_are_accepted_and_normalized() -> None:
    records = [
        make_record("root", [], "明文"),
        make_record("child", ["root"], "脱敏"),
        make_record("hidden", [], "不可见"),
    ]
    result = analyze_dataset(make_dataset(records))
    statuses = {
        field["path"]: (field["declared_status"], field["effective_status"])
        for field in result["fields"]
    }
    assert statuses["root"] == ("plaintext", "plaintext")
    assert statuses["child"] == ("masked", "plaintext")
    assert statuses["hidden"] == ("invisible", "invisible")


def test_sync_marker_mismatch_is_a_business_failure() -> None:
    dataset = make_dataset(
        [make_record("root", [], "masked")],
        dataset_id="marker-test",
    )
    dataset["records"][0]["masking"] = "plaintext"

    result = analyze_dataset(dataset)

    assert "E_SYNC" in result["codes"]
    assert result["sync_marker_ok"] is False
    assert result["status"] == "failed"
    assert result["observations"]["inconsistent"] >= 1


def test_field_limit_and_real_time_timeout() -> None:
    records = [make_record(f"f[{index}]", [], "masked") for index in range(4)]
    dataset = make_dataset(records, injection={"simulate_delay_ms": 80})

    limited = analyze_dataset(dataset, EngineOptions(max_fields=3))
    assert limited["codes"] == ["E_FIELDS"]
    assert limited["processed_field_count"] == 0

    started = time.monotonic()
    timed_out = analyze_dataset(
        dataset,
        EngineOptions(timeout_ms=10, max_fields=100),
    )
    elapsed = time.monotonic() - started

    assert timed_out["codes"] == ["E_TIMEOUT"]
    assert timed_out["observations"]["inconsistent"] == 0
    assert elapsed < 1.0
