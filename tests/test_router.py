from graymesh.clock import LogicalClock
from graymesh.controlplane import ControlPlane
from graymesh.router import DecisionEngine

from tests.conftest import bootstrap


def make_objects(f):
    return [
        f["member"]("m1", 9001, 3), f["member"]("m2", 9002, 1),
        f["member"]("m3", 9003, 1),
        f["group"]("g1", ["m1", "m2"]), f["group"]("g2", ["m3"]),
        f["route"]("r1", "g1", gray_percent=70),
        f["route"]("r2", "g2", gray_percent=30,
                   matches=[{"headers": {"x-api": "extra"}}]),
        f["virtual_service"]("vs1", ["r1", "r2"]),
    ]


TRACE = [
    {"seq": i, "key": "user-%02d" % i, "path": "/ok",
     "headers": {"x-api": "extra"} if i % 3 == 0 else {}}
    for i in range(1, 51)
]


def test_deterministic_across_runs(store, object_factory):
    """Same trace, two independent engines/clocks -> bit-identical chains."""
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))
    snapshot = store.load_snapshot()

    def run():
        clock = LogicalClock(start_ms=12345, step_ms=3)
        engine = DecisionEngine(snapshot, clock)
        return [engine.decide(req) for req in TRACE]

    first, second = run(), run()
    assert first == second


def test_no_wallclock_dependency(store, object_factory):
    """Decisions depend only on the injected clock, not on the wall clock."""
    import time
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))
    snapshot = store.load_snapshot()

    engine_a = DecisionEngine(snapshot, LogicalClock(start_ms=0, step_ms=1))
    chain_a = engine_a.decide(TRACE[0])
    time.sleep(0.02)
    engine_b = DecisionEngine(snapshot, LogicalClock(start_ms=0, step_ms=1))
    chain_b = engine_b.decide(TRACE[0])
    assert chain_a == chain_b


def test_gray_window_split_is_stable(store, object_factory):
    """The canary share is exactly the set of keys whose bucket < 30, and each
    key always lands on the same route."""
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))
    snapshot = store.load_snapshot()
    engine = DecisionEngine(snapshot, LogicalClock())

    canary_keys, stable_keys = set(), set()
    for i in range(1, 201):
        req = {"seq": i, "key": "k-%d" % i, "path": "/",
               "headers": {"x-api": "extra"}}
        chain = engine.decide(req)
        # r1 window is [0,70) and matches every request; r2 window is [70,100)
        # and requires the x-api header, so canary == bucket in [70,100)
        if chain["route_id"] == "r2":
            assert chain["route_selected_by"] == "gray_window"
            assert 70 <= chain["gray_bucket"] < 100
            canary_keys.add(req["key"])
        else:
            assert chain["route_id"] == "r1"
            assert chain["gray_bucket"] < 70
            stable_keys.add(req["key"])
    assert canary_keys and stable_keys
    assert not (canary_keys & stable_keys)
    assert 0 < len(canary_keys) < 200


def test_weighted_member_pick_fixed_order(store, object_factory):
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))
    snapshot = store.load_snapshot()
    engine = DecisionEngine(snapshot, LogicalClock())
    picks = set()
    for i in range(1, 101):
        chain = engine.decide({"seq": i, "key": "m-%d" % i, "path": "/", "headers": {}})
        assert chain["route_id"] == "r1"
        picks.add(chain["member_id"])
    assert picks <= {"m1", "m2"}
    assert "m1" in picks  # weight 3 of 4 must win sometimes
