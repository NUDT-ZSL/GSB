from __future__ import annotations

import json
import sqlite3

import pytest

from ml_gateway.canonical import canonical_json, sha256_json
from ml_gateway.engine import GatewayEngine
from ml_gateway.errors import InputError, ReplayError, SnapshotError
from ml_gateway.logger import StructuredLogger
from ml_gateway.storage import SessionStore

from tests.conftest import (
    DATA_DIR,
    FakeInferenceClient,
    changed_alpha_declaration,
    make_engine,
)
from ml_gateway.declarations import load_requests_ndjson


def _requests(bundle):
    return load_requests_ndjson(DATA_DIR / "requests.ndjson", bundle.feature_schema)


def _effective_ids(store):
    return [row["request_id"] for row in store.list_ledger() if row["effective"] == 1]


def _stats_cache_by_backend(stats, backend_id):
    return {
        key: value
        for key, value in stats["cache"].items()
        if key.split("|")[1] == backend_id
    }


def test_routing_batch_order_and_deterministic_outputs(tmp_path, sample_bundle):
    requests = _requests(sample_bundle)
    store1, _, engine1, _ = make_engine(tmp_path / "one", sample_bundle)
    report1 = engine1.execute_requests(requests)
    response_bytes_1 = canonical_json(report1.responses).encode("utf-8")
    batches1 = [
        (batch["batch_id"], batch["rule_id"], batch["request_ids"])
        for batch in store1.list_batches()
    ]
    store1.close()

    store2, _, engine2, _ = make_engine(tmp_path / "two", sample_bundle)
    report2 = engine2.execute_requests(requests)
    response_bytes_2 = canonical_json(report2.responses).encode("utf-8")
    batches2 = [
        (batch["batch_id"], batch["rule_id"], batch["request_ids"])
        for batch in store2.list_batches()
    ]
    assert response_bytes_1 == response_bytes_2
    assert batches1 == batches2
    assert batches1 == [
        ("b-000001", "rule-gold-ocr", ["req-001", "req-004"]),
        ("b-000002", "rule-gold-ocr", ["req-008"]),
        ("b-000003", "rule-ocr", ["req-002", "req-006"]),
        ("b-000004", "rule-text", ["req-003", "req-005"]),
        ("b-000005", "rule-default", ["req-007"]),
    ]
    assert all(row["binding_valid"] for row in engine2.binding_rows() if row["effective"])
    assert len(_effective_ids(store2)) == len(requests)
    store2.close()


def test_engine_rejects_input_vector_length_before_ledger_writes(tmp_path, sample_bundle):
    requests = _requests(sample_bundle)
    broken = type(requests[0])(
        request_id=requests[0].request_id,
        features=requests[0].features,
        inputs=(1.0, 2.0, 3.0),
    )
    store, _, engine, _ = make_engine(tmp_path, sample_bundle)
    with pytest.raises(InputError) as exc:
        engine.execute_requests([broken])
    assert exc.value.path == "$[0].input"
    assert store.list_ledger() == []
    store.close()


def test_evolution_invalidates_old_bindings_and_recomputes_only_affected_stats(
    tmp_path, sample_bundle, evolved_bundle
):
    requests = _requests(sample_bundle)
    store, logger, engine, _ = make_engine(tmp_path, sample_bundle)
    report1 = engine.execute_requests(requests)
    old_stats = dict(engine.state["stats"])
    beta_cache_before = _stats_cache_by_backend(old_stats, "be-beta")
    store.close()

    store = SessionStore(tmp_path / "session.db")
    logger = StructuredLogger(store, tmp_path / "gateway.jsonl", fresh=False)
    engine2 = GatewayEngine(
        store=store,
        bundle=evolved_bundle,
        logger=logger,
        client=FakeInferenceClient(),
    )
    assert engine2.evolution["reason"] == "FINGERPRINT_EVOLUTION"
    assert engine2.evolution["affected_backends"] == ["be-alpha"]
    assert engine2.evolution["invalidated_entry_count"] == 5
    assert set(engine2.evolution["affected_rules"]) == {"rule-gold-ocr", "rule-ocr"}
    assert all(
        not row["effective"]
        for row in engine2.state["ledger"]
        if row["backend_id"] == "be-alpha"
    )
    beta_cache_after_evolution = _stats_cache_by_backend(engine2.state["stats"], "be-beta")
    assert beta_cache_before == beta_cache_after_evolution
    assert all(
        "backend_hits:be-beta" in engine2.evolution["unchanged_stat_keys"]
        or "cache:" in key
        for key in engine2.evolution["unchanged_stat_keys"]
        if key.startswith("backend_hits:")
    )
    old_beta_hits = engine2.state["stats"]["backend_hits"]["be-beta"]

    report2 = engine2.execute_requests(requests)
    assert engine2.state["stats"]["backend_hits"]["be-beta"] == old_beta_hits
    alpha_outputs_1 = [
        response["output_summary"]
        for response in report1.responses
        if response["backend_id"] == "be-alpha"
    ]
    alpha_outputs_2 = [
        response["output_summary"]
        for response in report2.responses
        if response["backend_id"] == "be-alpha"
    ]
    assert alpha_outputs_1 != alpha_outputs_2
    assert all(row["binding_valid"] for row in engine2.binding_rows() if row["effective"])
    versions = store.list_stats_versions()
    assert any(version["readonly"] for version in versions)
    assert "backend_hits:be-beta" in engine2.evolution["unchanged_stat_keys"]
    assert not store.get_stats_version(int(engine2.state["stats_version"]))["readonly"]
    assert len(_effective_ids(store)) == len(requests)
    store.close()


def test_inflight_reload_voids_batch_and_redispatches_with_new_fingerprint(
    tmp_path, sample_bundle
):
    requests = _requests(sample_bundle)
    store, _, engine, fake = make_engine(tmp_path, sample_bundle)
    original_fingerprint = engine.states["be-alpha"].fingerprint
    new_decl = changed_alpha_declaration(sample_bundle)
    fired = {"value": False}

    def hook(meta):
        if fired["value"] or meta["backend_id"] != "be-alpha":
            return
        fired["value"] = True
        engine.reload_backend(backend_id="be-alpha", declaration=new_decl)

    report = engine.execute_requests(requests, execution_hook=hook)
    assert fired["value"]
    assert report.void_batch_ids == ["b-000001"]
    assert report.redispatched_batch_ids == ["b-000002"]
    old_trace = [
        item for item in report.trace if item["request_id"] == "req-001"
    ]
    assert [item["void"] for item in old_trace] == [True, False]
    old_batch = next(
        batch for batch in store.list_batches() if batch["batch_id"] == "b-000001"
    )
    new_batch = next(
        batch for batch in store.list_batches() if batch["batch_id"] == "b-000002"
    )
    assert old_batch["status"] == "VOID"
    assert old_batch["output_summary"] is None
    assert new_batch["redispatch_of"] == "b-000001"
    assert old_batch["superseded_by"] == "b-000002"
    assert new_batch["output_summary"] is not None
    assert len(_effective_ids(store)) == len(requests)
    assert all(row["binding_valid"] for row in engine.binding_rows() if row["effective"])
    assert engine.states["be-alpha"].load_sequence == 1
    assert engine.states["be-alpha"].fingerprint != original_fingerprint
    store.close()


def test_explicit_reload_outside_execution_immediately_recomputes_stats(
    tmp_path, sample_bundle
):
    requests = _requests(sample_bundle)
    store, _, engine, _ = make_engine(tmp_path, sample_bundle)
    engine.execute_requests(requests)
    previous_stats_version = int(engine.state["stats_version"])
    summary = engine.reload_backend(
        backend_id="be-alpha", declaration=changed_alpha_declaration(sample_bundle)
    )
    assert summary["stats_version"] > previous_stats_version
    assert engine.state["stats_dirty"] is False
    assert engine.state["stats"]["backend_hits"]["be-alpha"] == 0
    assert engine.state["stats"]["backend_hits"]["be-beta"] == 3
    assert all(
        not row["effective"]
        for row in store.list_ledger()
        if row["backend_id"] == "be-alpha"
    )
    current = store.get_stats_version(int(engine.state["stats_version"]))
    assert current is not None and not current["readonly"]
    assert any(event["code"] == "BACKEND_RELOADED" for event in store.list_log_events())
    store.close()


def test_upstream_failure_is_recorded_and_triggers_binding_review(tmp_path, sample_bundle):
    requests = _requests(sample_bundle)
    store, _, engine, _ = make_engine(
        tmp_path, sample_bundle, client=FakeInferenceClient(failures={"be-alpha"})
    )
    report = engine.execute_requests(requests)
    alpha_responses = [
        response for response in report.responses if response["backend_id"] == "be-alpha"
    ]
    assert all(response["status"] == "E_UPSTREAM" for response in alpha_responses)
    error_batches = [
        batch for batch in store.list_batches() if batch["status"] == "ERROR"
    ]
    assert error_batches
    assert all(batch["error_code"] == "E_UPSTREAM" for batch in error_batches)
    events = store.list_log_events()
    assert any(event["code"] == "BINDING_REVIEW" for event in events)
    store.close()


def test_snapshot_restore_replaces_whole_view_and_replay_matches(
    tmp_path, sample_bundle
):
    requests = _requests(sample_bundle)
    store, _, engine, _ = make_engine(tmp_path, sample_bundle)
    engine.execute_requests(requests)
    snapshot = engine.save_snapshot("baseline")
    snapshot_ledger_count = len(engine.state["ledger"])
    drift = engine.execute_requests(requests[:2])
    assert len(engine.state["ledger"]) == snapshot_ledger_count + 2
    before = store.current_digest()
    engine.restore_snapshot(snapshot["snapshot_id"])
    assert store.current_digest() == snapshot["state_digest"]
    assert before != snapshot["state_digest"]
    assert len(engine.state["ledger"]) == snapshot_ledger_count
    assert not (set(drift.batch_ids) & {batch["batch_id"] for batch in engine.state["batches"]})
    replay = engine.replay_snapshot(snapshot["snapshot_id"])
    assert replay["matched"]
    assert replay["request_count"] == len(requests)
    store.close()


def test_replay_reports_first_mismatch_as_e_replay(tmp_path, sample_bundle):
    requests = _requests(sample_bundle)
    store, _, engine, _ = make_engine(tmp_path, sample_bundle)
    engine.execute_requests(requests)
    snapshot = engine.save_snapshot("baseline")
    expected = snapshot["replay_expected"]
    expected[0]["bound_fingerprint"] = "fp1:tampered"
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE snapshots SET replay_expected_json = ? WHERE snapshot_id = ?",
            (json.dumps(expected, sort_keys=True, separators=(",", ":")), snapshot["snapshot_id"]),
        )
    with pytest.raises(ReplayError) as exc:
        engine.replay_snapshot(snapshot["snapshot_id"])
    assert exc.value.code == "E_REPLAY"
    assert exc.value.details["field"] == "bound_fingerprint"
    assert exc.value.details["request_index"] == 0
    assert exc.value.details["request_ordinal"] == 1
    assert any(event["code"] == "E_REPLAY" for event in store.list_log_events())
    store.close()


def test_snapshot_conflict_fully_replaces_current_view(tmp_path, sample_bundle, evolved_bundle):
    requests = _requests(sample_bundle)
    store, _, engine_v1, _ = make_engine(tmp_path, sample_bundle)
    engine_v1.execute_requests(requests)
    snapshot = engine_v1.save_snapshot("v1")
    store.close()

    store = SessionStore(tmp_path / "session.db")
    logger = StructuredLogger(store, tmp_path / "gateway.jsonl", fresh=False)
    engine_v2 = GatewayEngine(
        store=store,
        bundle=evolved_bundle,
        logger=logger,
        client=FakeInferenceClient(),
    )
    with pytest.raises(SnapshotError) as exc:
        engine_v2.restore_snapshot(snapshot["snapshot_id"])
    assert exc.value.code == "E_SNAP"
    assert exc.value.details["fingerprint_diff"]["changed"] == ["be-alpha"]
    assert engine_v2.state["ledger"] == []
    assert engine_v2.state["batches"] == []
    assert engine_v2.state["backend_states"][0]["load_sequence"] == 1
    assert len(store.list_snapshots()) == 1
    assert engine_v2.state["stats_version"] > snapshot["stats_version"]
    store.close()
