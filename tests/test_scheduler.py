"""Requirements 4/5: parallel dispatch budgets, three outcome classes,
logical-clock-only queue evolution."""
from gateway import errors, models
from gateway.engine import Engine
from gateway.models import (Arrival, EngineConfig, FaultSpec, ServiceSpec,
                            Trace, TraceWindow)
from gateway.scheduler import RoundBudget, dispatch_service_window
from gateway.store import Store
from gateway.upstream import SimulatedUpstream

CFG = EngineConfig(switch_load_threshold=0.6, round_time_budget_ms=1000,
                   per_request_budget_ms=300, clock_rollback_threshold_ms=5,
                   max_retries=2, retry_backoff_base_ms=10,
                   per_service_concurrency_cap=2, batch_overhead_ms=5)

SVC = ServiceSpec(name="svc", model_version="v1", input_tokens=10,
                  output_tokens=10, compute_units=10, vram_mib=1024,
                  min_replicas=1, max_replicas=2, batch_window_ms=50,
                  cold_start_ms=0, topology_domain="any")

PLACED = {"service": "svc", "status": "placed",
          "replicas": [{"replica": 0, "node": "n1", "card": 0,
                        "vram_mib": 1024, "cold_start": False}],
          "rejected_candidates": [], "first_constraint": None}


def _window():
    return TraceWindow(index=0, start_ms=0, window_ms=1000, arrivals=())


def _arrival(rate, budget=10 ** 6, fault=None):
    return Arrival(service="svc", arrival_rate=rate, avg_seq_len=100,
                   expected_concurrency=1, token_budget=budget, fault=fault)


def test_round_time_budget_yields_not_started():
    # each request costs ceil(110/10)=11ms; budget 25ms -> 2 start, rest not
    cfg = CFG.__class__(**{**CFG.__dict__, "round_time_budget_ms": 25})
    budget = RoundBudget(cfg.round_time_budget_ms)
    result = dispatch_service_window(
        SVC, _arrival(10), _window(), cfg, PLACED,
        models.MODE_PER_REQUEST, budget, SimulatedUpstream())
    outcomes = [r["outcome"] for r in result["requests"]]
    assert outcomes.count(models.OUTCOME_SUCCESS) == 2
    assert outcomes.count(models.OUTCOME_NOT_STARTED) == 8
    assert all(r["error_code"] == errors.E_ROUND_TIME_BUDGET
               for r in result["requests"]
               if r["outcome"] == models.OUTCOME_NOT_STARTED)


def test_token_budget_yields_abandoned():
    # 10 requests x 110 tokens, budget 330 -> 3 dispatched, 7 abandoned
    budget = RoundBudget(10 ** 6)
    result = dispatch_service_window(
        SVC, _arrival(10, budget=330), _window(), CFG, PLACED,
        models.MODE_PER_REQUEST, budget, SimulatedUpstream())
    outcomes = [r["outcome"] for r in result["requests"]]
    assert outcomes.count(models.OUTCOME_SUCCESS) == 3
    assert outcomes.count(models.OUTCOME_ABANDONED) == 7
    assert all(r["error_code"] == errors.E_TOKEN_BUDGET
               for r in result["requests"]
               if r["outcome"] == models.OUTCOME_ABANDONED)
    assert result["tokens_consumed"] == 330


def test_persistent_fault_yields_failed_after_start():
    fault = FaultSpec(kind="conn_refused", every=1, transient=False)
    budget = RoundBudget(10 ** 6)
    result = dispatch_service_window(
        SVC, _arrival(3, fault=fault), _window(), CFG, PLACED,
        models.MODE_PER_REQUEST, budget, SimulatedUpstream())
    assert all(r["outcome"] == models.OUTCOME_FAILED_AFTER_START
               for r in result["requests"])
    assert all(r["error_code"] == errors.E_UPSTREAM_CONN_REFUSED
               for r in result["requests"])
    # started requests run all retry attempts, never truncated as timeout
    assert all(r["attempts"] == CFG.max_retries + 1
               for r in result["requests"])
    assert all(r["finish_ms"] is not None for r in result["requests"])


def test_transient_fault_recovers_on_retry():
    fault = FaultSpec(kind="digest_mismatch", every=2, transient=True)
    budget = RoundBudget(10 ** 6)
    result = dispatch_service_window(
        SVC, _arrival(4, fault=fault), _window(), CFG, PLACED,
        models.MODE_PER_REQUEST, budget, SimulatedUpstream())
    assert all(r["outcome"] == models.OUTCOME_SUCCESS
               for r in result["requests"])
    assert [r["attempts"] for r in result["requests"]] == [2, 1, 2, 1]


def test_batch_mode_stage_path_and_queue_wait():
    budget = RoundBudget(10 ** 6)
    result = dispatch_service_window(
        SVC, _arrival(10), _window(), CFG, PLACED,
        models.MODE_WINDOW_BATCH, budget, SimulatedUpstream())
    for r in result["requests"]:
        assert r["stage"] == models.STAGE_DISPATCHED
        assert models.STAGE_BATCHING in r["stage_path"]
        # batch members wait until the batch window closes
        assert r["queue_wait_ms"] > 0


def test_placement_failure_marks_not_started():
    failed = dict(PLACED, status="failed",
                  first_constraint=errors.E_PLACEMENT_VRAM, replicas=[])
    budget = RoundBudget(10 ** 6)
    result = dispatch_service_window(
        SVC, _arrival(3), _window(), CFG, failed,
        models.MODE_PER_REQUEST, budget, SimulatedUpstream())
    assert all(r["outcome"] == models.OUTCOME_NOT_STARTED
               for r in result["requests"])
    assert all(r["error_code"] == errors.E_PLACEMENT_VRAM
               for r in result["requests"])


def test_request_states_recovered_from_store_not_memory(loaded, tmp_path):
    # After a fresh engine instance reopens the DB, request stages come
    # from persisted rounds only.
    services, nodes, trace = loaded
    db = str(tmp_path / "r.db")
    store = Store(db)
    Engine(services, nodes, trace, store).run()
    store.close()
    store2 = Store(db)
    try:
        rounds = store2.committed_rounds()
        assert len(rounds) == len(trace.windows)
        for rnd in rounds:
            for req in rnd["requests"]:
                assert req["stage"] in (
                    models.STAGE_PENDING, models.STAGE_DISPATCHED,
                    models.STAGE_FAILED)
                assert store2.request_outcome(
                    rnd["version"], req["request_id"]) == req["outcome"]
    finally:
        store2.close()
