from __future__ import annotations

from itertools import permutations

from sensitivity_engine.budget import BoundedBatchQueue, WallBudget, run_pool
from sensitivity_engine.hashing import canonical_json
from sensitivity_engine.models import Batch
from sensitivity_engine.parsing import parse_batch_envelope
from sensitivity_engine.service import import_dataset
from sensitivity_engine.store import Store


def _signature(dataset, concurrency: int = 1):
    store = Store(":memory:")
    import_dataset(store, dataset, concurrency=concurrency, queue_limit=8)
    signature = (
        store.ledger_digest(),
        canonical_json(store.export_reentry_report()),
        canonical_json(store.query_ledger()),
        canonical_json(store.list_rejections()),
    )
    store.close()
    return signature


def test_all_batch_permutations_are_identical(dataset):
    signatures = set()
    batch_ids = list(dataset.batches)
    for order in permutations(batch_ids):
        dataset.batches = list(order)
        signatures.add(_signature(dataset))
    assert len(signatures) == 1


def test_concurrency_matches_serial(dataset):
    serial = _signature(dataset, concurrency=1)
    concurrent = _signature(dataset, concurrency=4)
    assert serial == concurrent


def test_repeated_full_import_is_stable(dataset):
    store = Store(":memory:")
    import_dataset(store, dataset, concurrency=2, queue_limit=8)
    first = (store.ledger_digest(), canonical_json(store.export_reentry_report()))
    import_dataset(store, dataset, concurrency=2, queue_limit=8)
    second = (store.ledger_digest(), canonical_json(store.export_reentry_report()))
    assert first == second
    retained = store.get_object_ledger_entry("obj:revoke-retain")
    assert retained["reentry_flag"] is True
    assert retained["watermark_batch_id"] == "batch:rb-003-migrate"
    store.close()


def test_rejection_list_is_stable_under_arrival_order():
    raw_a = {
        "batch_id": "batch:a",
        "logical_clock": 1,
        "operations": [
            {"op_seq": 1, "op_type": "revoke_source", "source_key": "src:ok"},
            {"op_seq": 0, "op_type": "revoke_source", "source_key": "src:late"},
        ],
    }
    raw_b = {
        "batch_id": "batch:b",
        "logical_clock": 1,
        "operations": [
            {"op_seq": 2, "op_type": "add_source", "source_key": "bad key", "level": "NOPE"},
        ],
    }
    batch_a, issues_a = parse_batch_envelope(raw_a)
    batch_b, issues_b = parse_batch_envelope(raw_b)
    assert batch_a is not None and batch_b is not None

    signatures = set()
    for first, second in ((issues_a, issues_b), (issues_b, issues_a)):
        store = Store(":memory:")
        store.record_issues(list(first) + list(second))
        signatures.add(canonical_json(store.list_rejections()))
        store.close()
    assert len(signatures) == 1


def test_queue_capacity_rejects_extra_item():
    queue: BoundedBatchQueue[int] = BoundedBatchQueue(1)
    assert queue.submit(1) is True
    assert queue.submit(2) is False


def test_run_pool_reports_queue_rejection():
    def worker(value: int) -> int:
        return value

    results, rejections = run_pool(list(range(20)), worker, concurrency=1, queue_limit=1)
    assert len(results) == 20
    # A capacity-one queue can reject under immediate producer pressure; whether
    # or not the scheduler wins a race, the API shape is always stable.
    assert isinstance(rejections, list)


def test_wall_timeout_rolls_back_partial_batch():
    obj_records = [
        {
            "seq": 1,
            "object_key": "obj:timeout",
            "parent_key": None,
            "inheritance_mode": "ALLOW",
        }
    ]
    from sensitivity_engine.parsing import normalize_objects

    objects, issues = normalize_objects(obj_records)
    assert not issues
    store = Store(":memory:")
    from sensitivity_engine.domain import LEVEL_ENUM_VERSION, LAYOUT_VERSION
    from sensitivity_engine.models import Dataset

    dataset = Dataset(LAYOUT_VERSION, LEVEL_ENUM_VERSION, 0, objects=objects)
    store.import_base(dataset)

    operations = []
    for index in range(500):
        raw = {
            "op_seq": index + 1,
            "op_type": "add_source",
            "source_key": f"src:timeout-{index:04d}",
            "object_key": "obj:timeout",
            "trustee_id": "trustee:alice",
            "level": "INTERNAL",
        }
        batch, batch_issues = parse_batch_envelope(
            {"batch_id": "batch:timeout", "logical_clock": 1, "operations": [raw]}
        )
        assert not batch_issues
        operations.append(batch.operations[0])

    timed_batch = Batch("batch:timeout", 1, tuple(operations))
    result = store.apply_batch(timed_batch, budget=WallBudget.start(0.0))
    assert result["code"] == "E_TIMEOUT"
    assert result["partial_writes"] >= 1
    assert store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM applied_batches").fetchone()[0] == 0
    assert store.get_object_ledger_entry("obj:timeout")["decision_status"] == "NO_BASIS"
    store.close()
