import json
import os

import pytest

from configdist.converge import check_convergence
from configdist.errors import (E_CONVERGE, E_MANIFEST, E_NODE, E_REPLAY,
                               E_STATE, PlatformError)
from configdist.manifest import load_manifest, validate_manifest
from configdist.planner import compile_plan
from configdist.registry import load_registry
from configdist.replay import replay_check
from configdist.session import Session, SimulatedInterrupt
from configdist.statemachine import StateMachine
from configdist.store import Store
from configdist.verify import verify_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "samples", "registry.json")
REGISTRY_IDS = load_registry(REGISTRY)


def manifest_body(**over):
    body = {
        "version": 1,
        "items": {"feature.a": "on", "feature.b": "off"},
        "targets": ["node-a", "node-b"],
        "strategy": {"mode": "full"},
    }
    body.update(over)
    return body


def write_manifest(tmp_path, body, name="m.json"):
    path = tmp_path / name
    path.write_text(json.dumps(body))
    return str(path)


# -- manifest validation (E_MANIFEST) ---------------------------------------

def test_manifest_valid():
    m = validate_manifest(manifest_body(), REGISTRY_IDS)
    assert m.version == 1 and m.targets == ["node-a", "node-b"]


@pytest.mark.parametrize("over,loc", [
    ({"version": 0}, "$.version"),
    ({"items": {}}, "$.items"),
    ({"items": {"9bad": "x"}}, "$.items.9bad"),
    ({"targets": []}, "$.targets"),
    ({"targets": ["node-a", "ghost"]}, "$.targets[1]"),
    ({"targets": ["node-a", "node-a"]}, "$.targets[1]"),
    ({"strategy": {"mode": "shard"}}, "$.strategy.shards"),
    ({"strategy": {"mode": "weird"}}, "$.strategy.mode"),
    ({"strategy": None}, "$.strategy"),
])
def test_manifest_rejected_with_location(over, loc):
    with pytest.raises(PlatformError) as exc:
        validate_manifest(manifest_body(**over), REGISTRY_IDS)
    assert exc.value.code == E_MANIFEST
    assert exc.value.details["location"] == loc


def test_manifest_version_monotonic():
    with pytest.raises(PlatformError) as exc:
        validate_manifest(manifest_body(version=1), REGISTRY_IDS, min_version=1)
    assert exc.value.code == E_MANIFEST


def test_manifest_file_not_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(PlatformError) as exc:
        load_manifest(str(path), REGISTRY_IDS)
    assert exc.value.code == E_MANIFEST


# -- planner ------------------------------------------------------------------

def test_plan_full():
    m = validate_manifest(manifest_body(), REGISTRY_IDS)
    plan = compile_plan(m)
    assert plan["assignments"]["node-a"] == m.items
    assert plan["assignments"]["node-b"] == m.items


def test_plan_shard_covers_all_items():
    body = manifest_body(
        items={f"key.{i}": i for i in range(5)},
        targets=["node-a", "node-b", "node-c"],
        strategy={"mode": "shard", "shards": 2},
    )
    m = validate_manifest(body, REGISTRY_IDS)
    plan = compile_plan(m)
    merged = {}
    for items in plan["assignments"].values():
        merged.update(items)
    assert merged == m.items
    assert set(plan["assignments"]) == set(m.targets)


# -- state machine --------------------------------------------------------------

def test_statemachine_commit_blocked_and_idempotent(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    sm = StateMachine(store)
    sm.transition("prepared", 1, [])
    sm.transition("distributing", 1, [])
    blocked = sm.commit(1, [], False, ["missing receipts: node-b"])
    assert not blocked["ok"] and blocked["code"] == E_STATE
    assert "missing receipts" in blocked["reason"]
    ok = sm.commit(1, ["node-a"], True, [])
    assert ok["ok"] and not ok["idempotent"]
    again = sm.commit(1, ["node-a"], True, [])
    assert again["ok"] and again["idempotent"]
    store.close()


def test_statemachine_illegal_transition(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    sm = StateMachine(store)
    with pytest.raises(PlatformError) as exc:
        sm.transition("committed", 1, [])
    assert exc.value.code == E_STATE
    store.close()


# -- store / snapshot -------------------------------------------------------------

def test_committed_version_never_regresses(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    assert store.set_committed_version(3)
    assert not store.set_committed_version(2)
    assert not store.set_committed_version(3)
    assert store.committed_version() == 3
    store.close()


def test_snapshot_whole_replace(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    m1 = manifest_body()
    m1 = dict(m1)
    plan1 = {"version": 1, "strategy": {"mode": "full"},
             "assignments": {"node-a": {"feature.a": "on"}}}
    store.replace_snapshot(m1, plan1)
    store.add_receipt("node-a", 1, 1, 1, "ack", "d", ["feature.a"])
    store.set_committed_version(1)
    m2 = manifest_body(version=2)
    plan2 = {"version": 2, "strategy": {"mode": "full"},
             "assignments": {"node-a": {"feature.a": "off"}}}
    store.replace_snapshot(m2, plan2)
    assert store.snapshot_version() == 2
    assert store.receipts() == []          # old session state fully replaced
    assert store.get_manifest(1) is None
    assert store.committed_version() == 1  # committed version preserved
    store.close()


# -- live sessions with real child processes ---------------------------------------

def run_session(tmp_path, body, fail_modes=None, name="s.db", **kw):
    store = Store(str(tmp_path / name))
    session = Session(store, REGISTRY)
    session.prepare(write_manifest(tmp_path, body))
    result = session.distribute(fail_modes=fail_modes or {}, **kw)
    return store, session, result


def test_session_normal_commit(tmp_path):
    store, _, result = run_session(tmp_path, manifest_body(),
                                   concurrency=2, timeout=2.0, max_retries=1)
    assert result["ok"]
    assert store.committed_version() == 1
    receipts = store.receipts(1)
    assert {r["node"] for r in receipts} == {"node-a", "node-b"}
    obs = store.observations(1)
    for node in ("node-a", "node-b"):
        assert any(o["event"] == "dispatch_start" and o["node"] == node
                   for o in obs)
        assert any(o["event"] == "dispatch_end" and o["node"] == node
                   for o in obs)
    store.close()


def test_session_timeout_retry_recorded(tmp_path):
    store, _, result = run_session(
        tmp_path, manifest_body(targets=["node-a"]),
        fail_modes={"node-a": "drop-first"},
        concurrency=1, timeout=0.3, max_retries=2)
    assert result["ok"]
    kinds = {r["kind"] for r in store.receipts(1)}
    assert "retry" in kinds  # retry receipt distinguished from first attempt
    assert any(o["event"] == "dispatch_retry" for o in store.observations(1))
    store.close()


def test_session_node_crash_degraded(tmp_path):
    store, _, result = run_session(
        tmp_path, manifest_body(),
        fail_modes={"node-b": "crash"},
        concurrency=2, timeout=1.0, max_retries=1)
    assert not result["ok"]
    assert result["code"] == E_CONVERGE  # missing receipt blocks completion
    assert "node-b" in result["node_errors"]
    assert result["node_errors"]["node-b"]["code"] == E_NODE
    assert store.node_statuses()["node-b"] == "degraded"
    assert store.node_statuses()["node-a"] == "ok"
    assert store.committed_version() == 0  # partial success never completes
    store.close()


def test_session_node_bad_message(tmp_path):
    store, _, result = run_session(
        tmp_path, manifest_body(targets=["node-a"]),
        fail_modes={"node-a": "badmsg"},
        concurrency=1, timeout=1.0, max_retries=1)
    assert not result["ok"]
    assert result["node_errors"]["node-a"]["code"] == E_NODE
    assert store.node_statuses()["node-a"] == "degraded"
    store.close()


def test_session_digest_mismatch(tmp_path):
    store, _, result = run_session(
        tmp_path, manifest_body(targets=["node-a"]),
        fail_modes={"node-a": "wrongdigest"},
        concurrency=1, timeout=1.0, max_retries=1)
    assert not result["ok"]
    assert result["code"] == E_CONVERGE
    assert result["convergence"]["mismatched"] == ["node-a"]
    dev = result["convergence"]["deviations"][0]
    assert dev["node"] == "node-a" and dev["version"] == 1
    store.close()


# -- recovery / replay --------------------------------------------------------------

def test_interrupt_and_recover_consistent_view(tmp_path):
    db = str(tmp_path / "s.db")
    store = Store(db)
    session = Session(store, REGISTRY)
    session.prepare(write_manifest(tmp_path, manifest_body()))
    with pytest.raises(SimulatedInterrupt):
        session.distribute(fail_modes={"node-b": "crash"},
                           concurrency=2, timeout=1.0, max_retries=1,
                           interrupt_after_dispatch=True)
    store.close()

    store2 = Store(db)
    session2 = Session(store2, REGISTRY)
    view = session2.recover()
    assert view["state"] == "distributing"
    assert view["node_status"]["node-b"] == "degraded"
    # recovered derived-state view equals a fresh recomputation
    plan = store2.get_plan(view["snapshot_version"])
    recomputed = check_convergence(plan, store2.receipts(plan["version"]))
    assert view["convergence"] == recomputed
    result = session2.resume(concurrency=2, timeout=1.0, max_retries=1)
    assert not result["ok"] and result["code"] == E_CONVERGE
    assert result["convergence"]["missing"] == ["node-b"]
    assert store2.committed_version() == 0
    store2.close()


def test_replay_ok_and_failures(tmp_path):
    store, _, result = run_session(tmp_path, manifest_body(),
                                   concurrency=2, timeout=2.0, max_retries=1)
    assert result["ok"]
    replay = replay_check(store)
    assert replay["ok"] and replay["version"] == 1

    store.set_node_status("node-b", "degraded")
    replay = replay_check(store)
    assert not replay["ok"] and replay["code"] == E_REPLAY
    problem = next(p for p in replay["problems"]
                   if p["kind"] == "node_degraded")
    assert problem["nodes"] == ["node-b"]
    assert problem["items"] == sorted(manifest_body()["items"])

    replay = replay_check(store, version=99)
    assert not replay["ok"] and replay["code"] == E_REPLAY
    assert replay["problems"][0]["kind"] == "version_missing"
    store.close()


def test_upgrade_replaces_snapshot_and_replay(tmp_path):
    db = str(tmp_path / "s.db")
    store = Store(db)
    session = Session(store, REGISTRY)
    session.prepare(write_manifest(tmp_path, manifest_body()))
    r1 = session.distribute(concurrency=2, timeout=2.0, max_retries=1)
    assert r1["ok"]

    body2 = manifest_body(version=2, targets=["node-a", "node-c"],
                          strategy={"mode": "shard", "shards": 2})
    session2 = Session(store, REGISTRY)
    prep = session2.prepare(write_manifest(tmp_path, body2, name="m2.json"))
    assert prep["snapshot_replaced"]
    r2 = session2.distribute(concurrency=2, timeout=2.0, max_retries=1)
    assert r2["ok"]
    assert store.committed_version() == 2
    replay = replay_check(store)
    assert replay["ok"] and replay["version"] == 2
    old = replay_check(store, version=1)
    assert not old["ok"] and old["code"] == E_REPLAY
    store.close()


# -- verify tool / web --------------------------------------------------------------

def test_verify_tool_ok_and_fault_codes(tmp_path):
    store, _, result = run_session(tmp_path, manifest_body(),
                                   name="ok.db",
                                   concurrency=2, timeout=2.0, max_retries=1)
    assert result["ok"]
    store.close()
    summary = verify_db(str(tmp_path / "ok.db"))
    assert summary["ok"]
    assert summary["committed_version"] == 1

    store, _, result = run_session(
        tmp_path, manifest_body(), name="bad.db",
        fail_modes={"node-b": "crash"},
        concurrency=2, timeout=1.0, max_retries=1)
    assert not result["ok"]
    store.close()
    summary = verify_db(str(tmp_path / "bad.db"))
    assert not summary["ok"]
    codes = {c["code"] for c in summary["checks"] if c["code"]}
    assert E_CONVERGE in codes and E_NODE in codes


def test_web_status(tmp_path):
    from configdist.web import create_app

    store, _, result = run_session(tmp_path, manifest_body(),
                                   concurrency=2, timeout=2.0, max_retries=1)
    assert result["ok"]
    store.close()
    app = create_app(str(tmp_path / "s.db"))
    client = app.test_client()
    status = client.get("/status").get_json()
    assert status["committed_version"] == 1
    assert status["state"] == "committed"
    assert client.get("/verify").get_json()["ok"]
