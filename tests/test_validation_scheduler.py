from __future__ import annotations

from qconv.canonical import canonical_json
from qconv.errors import DomainError, ErrorCode
from qconv.models import Budget, Job, SchedulerConfig
from qconv.scheduler import schedule_batch
from qconv.validation import validate_config, validate_job_records


def _codes(issues) -> set[str]:
    return {str(issue.code) for issue in issues}


def test_validation_business_codes() -> None:
    raw = [
        {
            "job_id": "dup",
            "input_shape": [1, 1, 4, 4],
            "kernel_shape": [1, 1, 1, 1],
            "stride": 1,
            "padding": 0,
            "quant_scale": 0.1,
            "zero_point": 0,
            "acc_dtype": "int32",
            "seed": 1,
            "priority": 1,
        },
        {
            "job_id": "dup",
            "input_shape": [1, 1, 4, 4],
            "kernel_shape": [1, 1, 1, 1],
            "stride": 1,
            "padding": 0,
            "quant_scale": 0.1,
            "zero_point": 0,
            "acc_dtype": "int32",
            "seed": 2,
            "priority": 1,
        },
        {
            "job_id": "shape",
            "input_shape": [1, 1, 4],
            "kernel_shape": [1, 1, 1, 1],
            "stride": 1,
            "padding": 0,
            "quant_scale": 0.1,
            "zero_point": 0,
            "acc_dtype": "int32",
            "seed": 3,
            "priority": 1,
        },
        {
            "job_id": "scale",
            "input_shape": [1, 1, 4, 4],
            "kernel_shape": [1, 1, 1, 1],
            "stride": 1,
            "padding": 0,
            "quant_scale": 0,
            "zero_point": 0,
            "acc_dtype": "int32",
            "seed": 4,
            "priority": 1,
        },
        {
            "job_id": "zp",
            "input_shape": [1, 1, 4, 4],
            "kernel_shape": [1, 1, 1, 1],
            "stride": 1,
            "padding": 0,
            "quant_scale": 0.1,
            "zero_point": 128,
            "acc_dtype": "int32",
            "seed": 5,
            "priority": 1,
        },
        {
            "job_id": "acc",
            "input_shape": [1, 1, 4, 4],
            "kernel_shape": [1, 1, 1, 1],
            "stride": 1,
            "padding": 0,
            "quant_scale": 0.1,
            "zero_point": 0,
            "acc_dtype": "float64",
            "seed": 6,
            "priority": 1,
        },
    ]
    jobs, issues = validate_job_records(raw)
    assert [job.job_id for job in jobs] == ["dup"]
    assert _codes(issues) == {
        ErrorCode.E_DUPLICATE_JOB_ID.value,
        ErrorCode.E_INVALID_INPUT_SHAPE.value,
        ErrorCode.E_INVALID_QUANT_SCALE.value,
        ErrorCode.E_INVALID_ZERO_POINT.value,
        ErrorCode.E_UNSUPPORTED_ACC_DTYPE.value,
    }


def test_invalid_budget_and_worker_config_constructors() -> None:
    for kwargs in (
        {"mac_budget": 0, "memory_budget_bytes": 10, "worker_slots": 1},
        {"mac_budget": 10, "memory_budget_bytes": -1, "worker_slots": 1},
        {"mac_budget": 10, "memory_budget_bytes": 10, "worker_slots": 0},
    ):
        try:
            Budget(**kwargs)
        except DomainError as exc:
            assert exc.code == ErrorCode.E_INVALID_BUDGET
        else:
            raise AssertionError("invalid budget was accepted")
    try:
        SchedulerConfig(max_workers_per_task=0)
    except DomainError as exc:
        assert exc.code == ErrorCode.E_INVALID_WORKER_CONFIG
    else:
        raise AssertionError("invalid worker config was accepted")


def test_cross_config_validation_returns_worker_code() -> None:
    from qconv.models import ToleranceConfig

    issues = validate_config(
        Budget(100, 100, 1),
        SchedulerConfig(max_workers_per_task=2),
        ToleranceConfig(),
    )
    assert [str(issue.code) for issue in issues] == [
        ErrorCode.E_INVALID_WORKER_CONFIG.value
    ]


def test_scheduler_reproducible_and_exact_coverage(
    small_job: Job, second_job: Job
) -> None:
    budget = Budget(1_000_000, 2_000_000, 2)
    config = SchedulerConfig(max_workers_per_task=2)
    first = schedule_batch([small_job, second_job], budget, config)
    second = schedule_batch([second_job, small_job], budget, config)
    assert canonical_json(first["plan"]) == canonical_json(second["plan"])
    assert first["plan"]["all_coverage_ok"]
    assert first["ledger"]["reconciled"]
    assert set(first["plan"]["admitted_job_ids"]) == {
        "unit_small",
        "unit_second",
    }


def test_three_budget_rejection_codes_and_no_residue(
    small_job: Job, second_job: Job
) -> None:
    tiny_mac = schedule_batch(
        [small_job],
        Budget(small_job.mac_count - 1, 2_000_000, 2),
        SchedulerConfig(max_workers_per_task=1),
    )
    assert tiny_mac["plan"]["decisions"][0]["code"] == ErrorCode.E_MAC_BUDGET

    tiny_memory = schedule_batch(
        [small_job],
        Budget(1_000_000, 100, 1),
        SchedulerConfig(max_workers_per_task=1),
    )
    assert tiny_memory["plan"]["decisions"][0]["code"] == ErrorCode.E_MEMORY_LIMIT

    no_slot = schedule_batch(
        [small_job, second_job],
        Budget(1_000_000, 2_000_000, 1),
        SchedulerConfig(max_workers_per_task=1),
    )
    decisions = {item["job_id"]: item for item in no_slot["plan"]["decisions"]}
    assert decisions["unit_small"]["status"] == "admitted"
    assert decisions["unit_second"]["code"] == ErrorCode.E_WORKER_SLOTS
    ledger = no_slot["ledger"]
    assert ledger["rejected_debits_zero"]
    rejected = [entry for entry in ledger["entries"] if entry["status"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["mac_debit"] == 0
    assert rejected[0]["memory_debit_bytes"] == 0
    assert rejected[0]["worker_debit"] == 0
    assert ledger["usage"]["workers_committed"] == 1


def test_coarse_tier_reduced_reduction_is_explicit() -> None:
    from qconv.samplegen import conflict_demo_jobs

    result = schedule_batch(
        conflict_demo_jobs(),
        Budget(2_000_000, 2_600_000, 3),
        SchedulerConfig(max_workers_per_task=2),
    )
    decisions = {item["job_id"]: item for item in result["plan"]["decisions"]}
    assert decisions["conflict_mac_heavy"]["code"] == ErrorCode.E_MAC_BUDGET
    assert decisions["conflict_fine_memory"]["code"] == ErrorCode.E_MEMORY_LIMIT
    assert decisions["conflict_slot_denied"]["code"] == ErrorCode.E_WORKER_SLOTS
    coarse = decisions["conflict_coarse_memory"]["plan"]
    assert coarse["quant_tier"] == "coarse"
    assert coarse["accumulator"]["merge_mode"] == "float32_chunk_reduce"
    assert any(
        item["code"] == "COARSE_QUANT_REDUCED_REDUCTION"
        for item in coarse["tradeoffs"]
    )

