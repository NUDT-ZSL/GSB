from __future__ import annotations

from sandbox_guard.closure import build_closure_report
from sandbox_guard.merge import build_unified_view
from sandbox_guard.parsing import parse_policy


def make_policy(**overrides):
    policy = {
        "observation_clock_ms": 1000,
        "default_evaluation_clock_ms": 1000,
        "profiles": [
            {
                "id": "p1",
                "executable": r"C:\guard\a.exe",
                "required_capabilities": ["file_read"],
                "capability_mask": 1,
            }
        ],
        "grants_v2": [],
        "processes": [],
    }
    policy.update(overrides)
    return parse_policy(policy)


def test_grant_dependency_cycle_returns_complete_cycle_keys():
    policy = make_policy(
        grants_v2=[
            {
                "key": "g-a",
                "profile_id": "p1",
                "generation": 1,
                "effective_at_ms": 0,
                "capabilities": ["file_read"],
                "capability_mask": 1,
                "dependencies": ["g-b"],
            },
            {
                "key": "g-b",
                "profile_id": "p1",
                "generation": 1,
                "effective_at_ms": 0,
                "capabilities": ["file_read"],
                "capability_mask": 1,
                "dependencies": ["g-a"],
            },
        ]
    )
    view = build_unified_view(policy, [])
    report = build_closure_report(policy, [], view, explicit_keys=["g-a"])
    assert report["ok"] is False
    assert report["code"] == "E_DEP_CYCLE"
    assert report["closure_grant_keys"] is None
    cycle = next(c for c in report["cycles"] if c["kind"] == "grant_dependency")
    assert cycle["keys"] == ["grant:g-a", "grant:g-b"]


def test_process_parent_cycle_is_reported():
    policy = make_policy(
        processes=[
            {"pid": 1, "profile_id": "p1", "parent_pid": 2, "status": "running"},
            {"pid": 2, "profile_id": "p1", "parent_pid": 1, "status": "running"},
        ]
    )
    view = build_unified_view(policy, [])
    report = build_closure_report(policy, [], view, explicit_keys=[])
    assert report["code"] == "E_DEP_CYCLE"
    cycle = next(c for c in report["cycles"] if c["kind"] == "process_parent")
    assert cycle["keys"] == ["process:1", "process:2"]


def test_profile_spawn_cycle_is_reported():
    policy = parse_policy(
        {
            "observation_clock_ms": 1000,
            "profiles": [
                {
                    "id": "p-a",
                    "executable": r"C:\guard\a.exe",
                    "capabilities": [],
                    "spawns": ["p-b"],
                },
                {
                    "id": "p-b",
                    "executable": r"C:\guard\b.exe",
                    "capabilities": [],
                    "spawns": ["p-a"],
                },
            ],
        }
    )
    view = build_unified_view(policy, [])
    report = build_closure_report(policy, [], view)
    assert report["code"] == "E_DEP_CYCLE"
    assert any(c["kind"] == "profile_spawn" for c in report["cycles"])
