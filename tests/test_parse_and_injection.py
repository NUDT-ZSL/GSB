from __future__ import annotations

from transformer_fusion import execute, inject_diagnose
from transformer_fusion.generate_data import demo_cases, generate_dataset


def test_generator_is_byte_deterministic(tmp_path):
    first = generate_dataset(tmp_path / "first")
    second = generate_dataset(tmp_path / "second")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    assert (first_root / "manifest.jsonl").read_bytes() == (
        second_root / "manifest.jsonl"
    ).read_bytes()
    assert (first_root / "inputs.npz").read_bytes() == (second_root / "inputs.npz").read_bytes()
    for path in (first_root / "cases").glob("*.npz"):
        assert path.read_bytes() == (second_root / "cases" / path.name).read_bytes()
    assert first["input_content_sha256"] == second["input_content_sha256"]


def test_unknown_op_and_invalid_axis_have_business_codes(dataset, stores):
    data_dir, _ = dataset
    tasks = demo_cases()
    unknown = tasks[0].evolved(op_chain=tasks[0].op_chain + ("__mystery_op__",))
    invalid_axis = tasks[1].evolved(axis_order=(0, 0, 1))
    report = execute(
        [unknown, invalid_axis],
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
        run_selfcheck=False,
    )
    by_case = report.case_map
    assert any(
        d.code == "E_OP_UNKNOWN" and d.stage == "parse"
        for d in by_case[unknown.case_id].diagnostics
    )
    assert any(
        d.code == "E_AXIS_INVALID" and d.stage == "feasibility"
        for d in by_case[invalid_axis.case_id].diagnostics
    )


def test_four_stage_injections_are_isolated(dataset, stores):
    data_dir, manifest = dataset
    result = inject_diagnose(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
    )
    assert result["ok"]
    assert result["all_expected_codes_observed"]
    assert result["all_unaffected_cases_unchanged"]
    assert {"parse", "feasibility", "fusion_kernel", "adjudication"} <= set(result["stages"])
    by_kind = {record["kind"]: record for record in result["records"]}
    assert by_kind["unknown_op"]["business_code"] == "E_OP_UNKNOWN"
    assert by_kind["duplicate_axis"]["business_code"] == "E_AXIS_INVALID"
    assert by_kind["non_finite_input"]["business_code"] == "E_NON_FINITE_INPUT"
    assert by_kind["non_positive_tolerance"]["business_code"] == "E_TOLERANCE_CONFIG"
    tight = by_kind["tight_tolerance"]["record"]
    assert tight["code"] == "E_TOLERANCE_EXCEEDED"
    assert tight["details"]["locations"]
    for record in result["records"]:
        assert record["unaffected_cases_unchanged"]
        assert record["record"]["stage"] in {"parse", "feasibility", "fusion_kernel", "adjudication"}
