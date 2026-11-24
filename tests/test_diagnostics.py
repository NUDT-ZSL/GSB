from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from conftest import make_sample
from vpn_diag.checkpoint import CheckpointManager
from vpn_diag.cli import main as cli_main
from vpn_diag.echo import UdpEchoTransport, unused_udp_port
from vpn_diag.engine import ForwardingEngine
from vpn_diag.fragmentation import calculate_inner_upper, plan_all
from vpn_diag.parser import parse_sample
from vpn_diag.runtime import execute_sample, verify_workspace
from vpn_diag.samplegen import build_sample


def test_invalid_records_are_rejected_but_parsing_continues():
    raw = {
        "profiles": [
            {"name": "ok", "rtt_ms": 1, "loss_rate": 0.1, "bandwidth_kbps": 10, "jitter_limit": 0.1},
            {"name": "bad-loss", "rtt_ms": 1, "loss_rate": 1.5, "bandwidth_kbps": 10, "jitter_limit": 0.1},
        ],
        "policy": {
            "fragment_threshold": 100,
            "concurrency_per_peer": 2,
            "round_budget_ms": 10,
            "retransmit_budget": 1,
            "tolerance_ms": 0.01,
            "clock_step_ms": 1,
        },
        "peers": [
            {
                "name": "ok",
                "echo_addr": "127.0.0.1:41001",
                "outer_mtu": 1200,
                "inner_payload_estimate_max": 100,
                "payload_lengths": [10],
                "profile": "ok",
            },
            {
                "name": "missing-address",
                "outer_mtu": 1200,
                "inner_payload_estimate_max": 100,
                "payload_lengths": [10],
                "profile": "ok",
            },
            {
                "name": "ok",
                "echo_addr": "127.0.0.1:41002",
                "outer_mtu": 1200,
                "inner_payload_estimate_max": 100,
                "payload_lengths": [10],
                "profile": "ok",
            },
            {
                "name": "duplicate-address",
                "echo_addr": "127.0.0.1:41001",
                "outer_mtu": 1200,
                "inner_payload_estimate_max": 100,
                "payload_lengths": [10],
                "profile": "ok",
            },
            {
                "name": "bad-mtu",
                "echo_addr": "127.0.0.1:41003",
                "outer_mtu": 32,
                "inner_payload_estimate_max": 128,
                "payload_lengths": [128],
                "profile": "ok",
            },
        ],
        "events": [{"type": "unknown", "peer": "ok", "at_clock_ms": 1}],
    }
    parsed = parse_sample(raw)
    codes = {item["code"] for item in parsed.rejections}
    assert "E_ECHO_ADDR_INVALID" in codes
    assert "E_PEER_NAME_DUPLICATE" in codes
    assert "E_ECHO_ADDR_DUPLICATE" in codes
    assert "E_MTU_SIZE" in codes
    assert "E_VALUE_RANGE" in codes
    assert "E_EVENT_TYPE_UNKNOWN" in codes
    assert all({"source_category", "record_index", "code"} <= set(item) for item in parsed.rejections)
    assert [peer.name for peer in parsed.peers] == ["ok"]


def test_mtu_infeasible_and_zero_upper_are_business_errors():
    upper, boundary = calculate_inner_upper(1400, 28, 512)
    assert (upper, boundary) == (512, 1372)
    with pytest.raises(Exception) as zero_exc:
        calculate_inner_upper(1400, 28, 0)
    assert zero_exc.value.code == "E_MTU_INFEASIBLE"
    with pytest.raises(Exception):
        calculate_inner_upper(64, 100, 10)

    raw = build_sample()
    parsed = parse_sample(raw)
    _plans, errors, _markers = plan_all(parsed.peers, parsed.policy)
    assert any(item["peer"] == "infeasible" and item["code"] == "E_MTU_INFEASIBLE" for item in errors)


def test_fixed_sample_sync_markers_match_and_tampering_is_reported():
    raw = build_sample()
    parsed = parse_sample(raw)
    plans, _errors, markers = plan_all(parsed.peers, parsed.policy)
    assert plans
    assert all(report["consistent"] for name, report in markers.items() if name != "infeasible")

    first_peer = next(peer for peer in raw["peers"] if peer.get("sync_markers"))
    first_peer["sync_markers"][0] = "SYNC:tampered"
    tampered = parse_sample(raw)
    _plans, errors, _markers = plan_all(tampered.peers, tampered.policy)
    assert any(item["code"] == "E_SYNC_MARKER_MISMATCH" for item in errors)


def _run_sample(raw, *, max_rounds=10, timeout_ms=100):
    parsed = parse_sample(raw)
    engine = ForwardingEngine(
        parsed,
        transport=UdpEchoTransport(timeout_ms),
    )
    return parsed, engine, engine.run(max_rounds=max_rounds)


def test_tolerance_ambiguous_send_decision(echo_server):
    raw = make_sample(
        echo_server.port,
        name="precision",
        lengths=[512],
        outer_mtu=512,
        crypto_overhead=0,
        policy={
            "fragment_threshold": 512,
            "concurrency_per_peer": 1,
            "round_budget_ms": 24.666,
            "retransmit_budget": 1,
            "tolerance_ms": 0.001,
            "clock_step_ms": 5,
        },
        profile={
            "name": "precision-profile",
            "rtt_ms": 23.333,
            "loss_rate": 0,
            "bandwidth_kbps": 3072,
            "jitter_limit": 0,
        },
    )
    _parsed, engine, state = _run_sample(raw, max_rounds=1)
    assert "E_TOLERANCE_AMBIGUOUS" in state["codes"]
    ambiguity = state["tolerance_ambiguities"][0]
    assert ambiguity["fixed_decision"] == "sendable"
    assert ambiguity["reference_decision"] == "deferred"
    assert ambiguity["fixed_margin_ms"] == 0
    assert ambiguity["reference_margin_ms"] < 0
    assert ambiguity["error_bound_q"] >= ambiguity["tolerance_q"]
    assert state["fragments"]["precision:p0000:f0000"]["status"] == "acknowledged"


def test_tolerance_ambiguous_retransmit_decision(echo_server):
    events = [{
        "type": "loss_segment",
        "peer": "peer-a",
        "at_clock_ms": 0,
        "until_clock_ms": 1,
    }]
    raw = make_sample(
        echo_server.port,
        lengths=[1],
        fragment_threshold=1,
        events=events,
        policy={
            "concurrency_per_peer": 1,
            "round_budget_ms": 100000,
            "retransmit_budget": 2,
            "tolerance_ms": 0.001,
            "clock_step_ms": 1,
            "max_rounds": 30,
        },
        profile={
            "name": "rto-edge",
            "rtt_ms": 21.334,
            "loss_rate": 0,
            "bandwidth_kbps": 3,
            "jitter_limit": 0,
        },
    )
    _parsed, _engine, state = _run_sample(raw, max_rounds=26)
    retransmit_ambiguities = [
        item for item in state["tolerance_ambiguities"]
        if item["decision_type"] == "retransmit"
    ]
    assert retransmit_ambiguities
    item = retransmit_ambiguities[0]
    assert item["fixed_decision"] == "due"
    assert item["reference_decision"] == "not_due"
    assert item["fixed_margin_ms"] == 0
    assert item["reference_margin_ms"] > 0
    peer = state["peers"]["peer-a"]
    assert peer["status"] == "completed"
    assert peer["retransmit_used"] == 1


def test_profile_switch_closure_recalculates_inflight_fragment(echo_server):
    events = [
        {"type": "loss_segment", "peer": "peer-a", "at_clock_ms": 0, "until_clock_ms": 5},
        {
            "type": "profile_change",
            "peer": "peer-a",
            "at_clock_ms": 5,
            "profile": {
                "name": "degraded",
                "rtt_ms": 10,
                "loss_rate": 0.1,
                "bandwidth_kbps": 200,
                "jitter_limit": 0.2,
            },
        },
    ]
    raw = make_sample(
        echo_server.port,
        lengths=[20],
        events=events,
        profile={"rtt_ms": 8, "loss_rate": 0, "bandwidth_kbps": 1000, "jitter_limit": 0},
    )
    _parsed, engine, state = _run_sample(raw, max_rounds=25)
    peer = state["peers"]["peer-a"]
    fragment = state["fragments"]["peer-a:p0000:f0000"]
    assert peer["status"] == "completed"
    assert peer["retransmit_used"] == 1
    assert len(state["closures"]) == 1
    closure = state["closures"][0]
    assert closure["fragment_keys"] == ["peer-a:p0000:f0000"]
    assert closure["to_profile"]["name"] == "degraded"
    assert closure["recalculations"][0]["new_rto_ms"] > closure["recalculations"][0]["old_rto_ms"]
    assert fragment["closure_ids"] == [closure["closure_id"]]


def test_retransmit_budget_exhaustion_returns_stall(echo_server):
    events = [{
        "type": "loss_segment",
        "peer": "peer-a",
        "at_clock_ms": 0,
        "until_clock_ms": 100000,
    }]
    raw = make_sample(
        echo_server.port,
        lengths=[12],
        events=events,
        policy={"retransmit_budget": 1, "clock_step_ms": 1, "round_budget_ms": 1000},
        profile={"rtt_ms": 1, "bandwidth_kbps": 1000},
    )
    _parsed, engine, state = _run_sample(raw, max_rounds=10)
    peer = state["peers"]["peer-a"]
    assert peer["status"] == "stalled"
    assert peer["retransmit_used"] == 1
    assert peer["stall"]["code"] == "E_TRANSFER_STALL"
    assert peer["stall"]["last_valid_progress"]["acknowledged_fragments"] == 0
    assert peer["stall"]["pending_fragment_keys"] == ["peer-a:p0000:f0000"]
    assert isinstance(peer["retransmit_used"], int)


def test_missing_echo_uses_real_timeout_and_business_code():
    port = unused_udp_port()
    raw = make_sample(port, lengths=[8])
    started = time.monotonic()
    _parsed, _engine, state = _run_sample(raw, max_rounds=1, timeout_ms=40)
    elapsed = time.monotonic() - started
    round_report = state["rounds"][0]
    assert "E_ECHO_UNREACHABLE" in state["codes"]
    assert round_report["echo_unreachable"] is True
    assert elapsed >= 0.035


def test_silent_udp_endpoint_forces_full_socket_timeout():
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    try:
        transport = UdpEchoTransport(timeout_ms=40)
        started = time.monotonic()
        result = transport.exchange_one(("127.0.0.1", port), "k", 1, b"x", timeout_ms=40)
        elapsed = time.monotonic() - started
        assert result["acknowledged"] is False
        assert result["code"] == "E_ECHO_UNREACHABLE"
        assert elapsed >= 0.035
    finally:
        listener.close()


@pytest.mark.parametrize("cap,expected", [(1, 1), (2, 2)])
def test_concurrency_cap_changes_first_round_selection(echo_server, cap, expected):
    raw = make_sample(
        echo_server.port,
        lengths=[12, 12, 12],
        policy={"concurrency_per_peer": cap, "round_budget_ms": 100000},
    )
    _parsed, _engine, state = _run_sample(raw, max_rounds=1)
    selected = state["rounds"][0]["peers"]["peer-a"]["selected"]
    assert len(selected) == expected


def _deterministic_projection(report):
    return {
        "summary": {
            key: report["summary"][key]
            for key in (
                "peers_completed",
                "peers_stalled",
                "rounds_completed",
                "final_logical_clock_ms",
                "fragments_acknowledged",
                "divergence_count",
                "tolerance_ambiguity_count",
                "profile_closure_count",
            )
        },
        "rounds": [
            {
                "round": item["round"],
                "clock_ms": item["clock_ms"],
                "codes": item["codes"],
                "peers": {
                    name: {
                        "selected": entry["selected"],
                        "acknowledged": entry["acknowledged"],
                        "logical_drops": entry["logical_drops"],
                        "codes": entry["codes"],
                    }
                    for name, entry in item["peers"].items()
                },
            }
            for item in report["rounds"]
        ],
        "peers": [
            {
                "name": peer["config"]["name"],
                "status": peer["progress"]["status"],
                "ack": peer["progress"]["acknowledged_fragments"],
                "retransmit_used": peer["progress"]["retransmit_used"],
                "fragments": [
                    {
                        "key": fragment["key"],
                        "status": fragment["status"],
                        "attempts": fragment["attempts"],
                    }
                    for fragment in peer["fragments"]
                ],
            }
            for peer in report["peers"]
        ],
        "divergences": report["divergences"],
        "closures": [
            {"peer": item["peer"], "fragment_keys": item["fragment_keys"]}
            for item in report["profile_switch_closures"]
        ],
    }


def test_force_termination_resume_replays_same_result(echo_server, tmp_path):
    raw = make_sample(
        echo_server.port,
        lengths=[180, 260, 340, 120],
        policy={"fragment_threshold": 100, "concurrency_per_peer": 1, "clock_step_ms": 1},
    )
    sample_path = tmp_path / "sample.json"
    sample_path.write_text(json.dumps(raw), encoding="utf-8")

    continuous_ws = tmp_path / "continuous"
    continuous = execute_sample(sample_path, continuous_ws, start_echo=False)

    interrupted_ws = tmp_path / "interrupted"
    first = execute_sample(
        sample_path, interrupted_ws, start_echo=False, max_rounds=2
    )
    assert first["summary"]["rounds_completed"] == 2
    resumed = execute_sample(sample_path, interrupted_ws, start_echo=False, resume=True)

    assert resumed["run_meta"]["resumed_from_checkpoint"] is True
    assert resumed["run_meta"]["resumed_watermark"] == 2
    assert _deterministic_projection(resumed) == _deterministic_projection(continuous)

    verification = verify_workspace(interrupted_ws, sample_path=sample_path)
    assert verification["valid"] is True
    assert verification["replay_consistency"]["consistent"] is True

    residue = interrupted_ws / "snapshots" / ".checkpoint-999999.partial.tmp"
    residue.write_text("{partial", encoding="utf-8")
    with_residue = verify_workspace(interrupted_ws, sample_path=sample_path)
    assert any("partial.tmp" in item for item in with_residue["residue"])
    assert with_residue["valid"] is True


def test_checkpoint_version_mismatch_requires_rebuild(tmp_path):
    workspace = tmp_path / "versioned"
    manager = CheckpointManager(workspace)
    manager.initialize()
    manager.conn.execute(
        "UPDATE meta SET value = 'old-fp' WHERE key = 'fixed_scale_version'"
    )
    manager.conn.commit()
    manager.close()

    verification = verify_workspace(workspace)
    assert verification["valid"] is False
    assert verification["code"] == "E_CHECKPOINT_VERSION"

    rebuilt, info = CheckpointManager.prepare(workspace)
    try:
        assert info["rebuilt"] is True
        verification = rebuilt.verify()
        assert verification["valid"] is False
        assert verification["code"] == "E_CHECKPOINT_NOT_FOUND"
    finally:
        rebuilt.close()


def test_cli_five_subcommands_end_to_end_with_ephemeral_echo(echo_server, tmp_path, capsys):
    raw = make_sample(echo_server.port, lengths=[40, 80], policy={"fragment_threshold": 32})
    sample = tmp_path / "ephemeral.json"
    sample.write_text(json.dumps(raw), encoding="utf-8")
    workspace = tmp_path / "ws"
    report = tmp_path / "report.json"

    assert cli_main(["import-sample", "-i", str(sample), "-w", str(workspace)]) == 0
    assert cli_main([
        "run", "-s", str(sample), "-w", str(workspace),
        "--no-start-echo", "--max-rounds", "8",
    ]) == 0
    assert cli_main(["progress", "-w", str(workspace), "-p", "peer-a"]) == 0
    assert cli_main(["export-report", "-w", str(workspace), "-o", str(report)]) == 0
    assert cli_main(["verify-checkpoint", "-w", str(workspace), "-s", str(sample)]) == 0

    exported = json.loads(report.read_text(encoding="utf-8"))
    assert exported["summary"]["peers_completed"] == 1
    assert exported["summary"]["fragments_total"] == 5
    out = capsys.readouterr().out
    assert "peer-a" in out


def test_generated_fixed_sample_cli_runs_with_internal_echo(tmp_path):
    sample = tmp_path / "fixed.json"
    assert cli_main(["generate-sample", "-o", str(sample)]) == 0
    workspace = tmp_path / "fixed-ws"
    report_path = tmp_path / "fixed-report.json"
    assert cli_main(["import-sample", "-i", str(sample), "-w", str(workspace)]) == 0
    code = cli_main([
        "run", "-s", str(sample), "-w", str(workspace),
        "--start-echo", "--max-rounds", "24", "--report", str(report_path),
    ])
    assert code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "E_TOLERANCE_AMBIGUOUS" in report["business_codes"]
    assert "E_TRANSFER_STALL" in report["business_codes"]
    assert any(item["peer"] == "shift" and item["fragment_keys"] for item in report["profile_switch_closures"])
