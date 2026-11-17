from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .config import ContentState, Policy
from .controller import CacheController, NodeEndpoint
from .errors import EdgeError, StateError, error_payload, exit_code_for
from .ledger import Ledger
from .orchestrator import PROJECT_ROOT, LocalOrchestrator
from .tcp import rpc
from .util import read_json_file, utc_now_text


def _seed_requests() -> list[dict[str, Any]]:
    return [
        {"path": "/index.html", "key_values": {"tenant": "public", "encoding": "identity"}},
        {"path": "/assets/base.css", "key_values": {"tenant": "public", "encoding": "identity"}},
        {"path": "/assets/theme.css", "key_values": {"tenant": "public", "encoding": "identity"}},
        {"path": "/assets/app.css", "key_values": {"tenant": "public", "encoding": "identity"}},
        {"path": "/assets/app.manifest.json", "key_values": {"tenant": "public", "encoding": "identity"}},
        {"path": "/assets/base.css", "key_values": {"tenant": "compact", "encoding": "identity"}},
    ]


def _entry_signature(states: list[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    signature: list[tuple[Any, ...]] = []
    for state in states:
        for entry in state.get("entries", []):
            signature.append(
                (
                    state["node_id"],
                    entry["cache_key"],
                    entry["path"],
                    entry["etag"],
                    entry["entry_version"],
                )
            )
    return sorted(signature)


def _assertion(items: list[dict[str, Any]], name: str, condition: bool, details: Any) -> None:
    items.append({"name": name, "passed": bool(condition), "details": details})
    if not condition:
        raise StateError(
            "demo verification assertion failed",
            location=f"assertions[{name}]",
            details=details,
        )


def _load_validated(policy_path: str, content_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    policy_doc = read_json_file(policy_path)
    content_doc = read_json_file(content_path)
    content = ContentState.from_doc(content_doc)
    Policy.from_doc(policy_doc, content.paths)
    return policy_doc, content_doc


def _scenario_policy_failure(policy_path: str, content_path: str) -> None:
    _load_validated(policy_path, content_path)
    raise StateError("broken policy unexpectedly validated")


def _scenario_cycle(content_path: str, policy_path: str) -> None:
    _load_validated(policy_path, content_path)
    raise StateError("cyclic content unexpectedly validated")


def _scenario_port_occupied(args: argparse.Namespace) -> dict[str, Any]:
    orch = LocalOrchestrator(args.state_dir, args.base_port)
    orch.prepare(reset=True)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(("127.0.0.1", orch.source_port))
        blocker.listen(1)
        try:
            orch.start_source(args.policy, args.content)
        except EdgeError as exc:
            if exc.code != "E_PORT":
                raise
            return {
                "ok": True,
                "scenario": "port-occupied",
                "injected": {"occupied": f"127.0.0.1:{orch.source_port}"},
                "returned_code": exc.code,
                "error": exc.to_dict(),
                "structured_log": str(orch.log_dir / "source.jsonl"),
                "stderr_log": str(orch.log_dir / "source.stderr.log"),
                "alternate_port_retry_attempted": False,
                "processes": orch.process_report(),
            }
        raise StateError("source unexpectedly started on an occupied port")
    finally:
        blocker.close()
        orch.kill_all()


def _run_normal(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    policy_doc, content_doc = _load_validated(args.policy, args.content)
    policy_v2 = read_json_file(args.policy_v2)
    orch = LocalOrchestrator(args.state_dir, args.base_port)
    orch.prepare(reset=True)
    orch.preflight_ports()
    assertions: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "ok": True,
        "scenario": "normal",
        "started_at": utc_now_text(),
        "state_dir": str(orch.state_dir),
        "ports": {
            "source": orch.source_port,
            **orch.node_ports,
        },
        "samples": {
            "policy_v1": str(Path(args.policy).resolve()),
            "policy_v2": str(Path(args.policy_v2).resolve()),
            "content": str(Path(args.content).resolve()),
        },
    }
    shutdowns: list[dict[str, Any]] = []
    try:
        orch.start_all(args.policy, args.content)
        ledger = Ledger(orch.db_path)
        controller = CacheController(
            ledger,
            "127.0.0.1",
            orch.source_port,
            [
                NodeEndpoint(node_id, "127.0.0.1", port)
                for node_id, port in orch.node_ports.items()
            ],
            str(orch.snapshot_dir),
        )
        source_initial = controller.source_status()

        # Stage 1: cold start and deterministic fills, including over-capacity evictions.
        stage1_fills = controller.fill_all(_seed_requests())
        time.sleep(0.03)
        stage1_hits = controller.query_all("/assets/app.css", {"tenant": "public"})
        stage1_states = controller.collect_node_states()
        stage1_evictions = {
            state["node_id"]: len(state["evictions"]) for state in stage1_states
        }
        _assertion(
            assertions,
            "stage1_capacity_and_eviction",
            all(state["entry_count"] == 4 for state in stage1_states)
            and all(count == 2 for count in stage1_evictions.values()),
            {"entries": {state["node_id"]: state["entry_count"] for state in stage1_states},
             "evictions": stage1_evictions},
        )
        _assertion(
            assertions,
            "stage1_hit_and_ttl_decrement",
            all(item["hit"] for item in stage1_hits)
            and all(
                item["entry"]["remaining_ttl_ms"] < item["entry"]["ttl_ms"]
                for item in stage1_hits
            ),
            stage1_hits,
        )
        result["stage1_cold_start"] = {
            "ledger_version": controller.version,
            "source": source_initial,
            "fill_summary": [
                {
                    "node_id": item["node_id"],
                    "filled": item["filled"],
                    "eviction_count": item["eviction_count"],
                }
                for item in stage1_fills
            ],
            "hits": stage1_hits,
            "nodes": stage1_states,
        }

        # Stage 2: evolve an upstream base object and propagate through the whole DAG.
        evolution = controller.evolve_content(
            "/assets/base.css",
            etag='"base-v2"',
            body=":root{--brand:#d1495b;--surface:#fff8f6}\nbody{margin:0;color:#17202a;background:var(--surface)}\n",
        )
        stage2_states = controller.collect_node_states()
        expected_paths = [
            "/assets/base.css",
            "/assets/app.css",
            "/assets/app.manifest.json",
        ]
        _assertion(
            assertions,
            "stage2_stable_topological_propagation",
            evolution["ordered_paths"] == expected_paths,
            evolution["ordered_paths"],
        )
        base_entries = [
            entry
            for state in stage2_states
            for entry in state["entries"]
            if entry["path"] == "/assets/base.css"
        ]
        _assertion(
            assertions,
            "stage2_entry_versions_incremented",
            bool(base_entries) and all(entry["entry_version"] == 2 for entry in base_entries),
            [entry["entry_version"] for entry in base_entries],
        )
        _assertion(
            assertions,
            "stage2_all_nodes_replaced_affected_entries",
            all(item["replaced_entries"] > 0 for item in evolution["node_results"]),
            [
                {
                    "node_id": item["node_id"],
                    "replaced_entries": item["replaced_entries"],
                }
                for item in evolution["node_results"]
            ],
        )
        result["stage2_derived_invalidation"] = {
            "ledger_version": evolution["plan"]["ledger_version"],
            "batch_id": evolution["plan"]["batch_id"],
            "trigger_path": evolution["plan"]["trigger_path"],
            "old_etag": evolution["plan"]["old_etag"],
            "new_etag": evolution["plan"]["new_etag"],
            "ordered_affected": evolution["plan"]["affected"],
            "node_results": evolution["node_results"],
            "nodes": stage2_states,
        }

        checkpoint = controller.snapshot(f"checkpoint-ledger-{controller.version}.json")
        result["snapshot_checkpoint"] = checkpoint
        checkpoint_signature = _entry_signature(stage2_states)

        # Stage 3: whole policy replacement (new key schema, TTLs, capacity and FIFO eviction).
        stage3 = controller.apply_policy(policy_v2, _seed_requests())
        stage3_states = controller.collect_node_states()
        _assertion(
            assertions,
            "stage3_policy_full_replacement",
            all(state["entry_count"] <= 2 for state in stage3_states)
            and all(state["policy_version"] == "policy-v2" for state in stage3_states),
            {
                "entries": {state["node_id"]: state["entry_count"] for state in stage3_states},
                "policy_versions": {
                    state["node_id"]: state["policy_version"] for state in stage3_states
                },
            },
        )
        _assertion(
            assertions,
            "stage3_all_old_entries_removed",
            sum(item["removed_entries"] for item in stage3["resets"]) == 12,
            stage3["resets"],
        )
        result["stage3_policy_replacement"] = {
            "ledger_version": stage3["ledger_version"],
            "policy_version": stage3["policy_version"],
            "resets": stage3["resets"],
            "fills": [
                {
                    "node_id": item["node_id"],
                    "filled": item["filled"],
                    "eviction_count": item["eviction_count"],
                }
                for item in stage3["fills"]
            ],
            "nodes": stage3_states,
        }

        # Extra consistency proof: restore the phase-2 checkpoint as a new ledger
        # version, replacing every active cache entry and derivation as a whole.
        restore = controller.restore_snapshot(checkpoint["path"])
        post_restore_states = controller.collect_node_states()
        post_restore_signature = _entry_signature(post_restore_states)
        _assertion(
            assertions,
            "snapshot_restore_detected_version_difference",
            restore["version_differences"]["changed"] is True
            and restore["replacement"]["incremental_merge_used"] is False,
            restore["version_differences"],
        )
        _assertion(
            assertions,
            "snapshot_restore_whole_cache_signature",
            post_restore_signature == checkpoint_signature,
            {
                "checkpoint_entries": len(checkpoint_signature),
                "restored_entries": len(post_restore_signature),
            },
        )
        _assertion(
            assertions,
            "snapshot_replay_verified",
            restore["replay"]["verified_traces"] > 0
            and restore["replay"]["verified_batches"] > 0,
            restore["replay"],
        )
        result["snapshot_restore"] = {
            **restore,
            "post_restore_ledger_version": controller.version,
            "post_restore_policy_version": ledger.active_state().policy_version,
            "nodes": post_restore_states,
        }

        # Forced-termination recovery: an uncommitted write intent becomes unknown;
        # a force-killed source restarts from the persisted ledger only.
        active_version = controller.version
        edge_c = controller.require_node("edge-c")
        orphan = rpc(
            edge_c.host,
            edge_c.port,
            {
                "op": "simulate_orphan_write",
                "ledger_version": active_version,
                "path": "/assets/app.css",
                "key_values": {"tenant": "public"},
            },
        )
        node_kill = orch.kill("edge-c", reason="simulated_abrupt_node_termination")
        orch.start_node("edge-c")
        node_restarted = rpc(
            edge_c.host,
            edge_c.port,
            {"op": "status"},
            timeout=10,
        )
        node_after = rpc(
            edge_c.host,
            edge_c.port,
            {"op": "state", "ledger_version": active_version},
            timeout=10,
        )
        _assertion(
            assertions,
            "forced_node_recovery_marks_unknown",
            node_restarted["recovery_report"]["rerun_required"] is True
            and any(
                item["intent_id"] == orphan["intent_id"]
                for item in node_restarted["recovery_report"]["unknown_intents"]
            ),
            {
                "intent_id": orphan["intent_id"],
                "recovery_report": node_restarted["recovery_report"],
            },
        )
        _assertion(
            assertions,
            "forced_node_recovery_keeps_committed_entries",
            node_after["entry_count"] > 0,
            {"entry_count": node_after["entry_count"]},
        )

        source_version_before_kill = ledger.current_version()
        source_kill = orch.kill("source", reason="simulated_abrupt_source_termination")
        ignored_policy = str(orch.log_dir / "restart-policy-must-not-be-read.json")
        ignored_content = str(orch.log_dir / "restart-content-must-not-be-read.json")
        orch.start_source(ignored_policy, ignored_content)
        source_restarted = rpc(
            "127.0.0.1",
            orch.source_port,
            {"op": "status"},
            timeout=10,
        )
        source_version_after_restart = ledger.current_version()
        _assertion(
            assertions,
            "forced_source_recovery_from_ledger",
            source_restarted["ledger_version"] == source_version_before_kill
            and source_version_after_restart == source_version_before_kill
            and not Path(ignored_policy).exists()
            and not Path(ignored_content).exists(),
            {
                "before": source_version_before_kill,
                "after": source_version_after_restart,
                "nonexistent_bootstrap_files_ignored": [
                    ignored_policy,
                    ignored_content,
                ],
            },
        )
        _assertion(
            assertions,
            "forced_termination_no_exit_report",
            node_kill["exit_report_generated"] is False
            and source_kill["exit_report_generated"] is False,
            {"node": node_kill, "source": source_kill},
        )
        result["forced_recovery"] = {
            "ledger_version": active_version,
            "orphan_intent": orphan,
            "node_kill": node_kill,
            "node_restarted": node_restarted,
            "node_state_after_restart": node_after,
            "source_kill": source_kill,
            "source_restarted": source_restarted,
            "source_ledger_version_before": source_version_before_kill,
            "source_ledger_version_after": source_version_after_restart,
            "nonexistent_bootstrap_files_ignored": [ignored_policy, ignored_content],
        }

        final_state = controller.system_report()
        result["final_state"] = final_state
        result["assertions"] = assertions
    finally:
        if orch.active:
            try:
                shutdowns = orch.shutdown_all()
            except Exception:
                shutdowns = orch.kill_all()
    result["graceful_shutdowns"] = shutdowns
    result["processes"] = orch.process_report()
    result["finished_at"] = utc_now_text()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["logs"] = {
        "source": str(orch.log_dir / "source.jsonl"),
        "nodes": {
            node_id: str(orch.log_dir / f"{node_id}.jsonl") for node_id in orch.node_ports
        },
    }
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repeatable local edge-cache consistency demo")
    parser.add_argument("--base-port", type=int, default=28180)
    parser.add_argument("--state-dir", default=str(PROJECT_ROOT / "runtime" / "demo"))
    parser.add_argument(
        "--scenario",
        choices=("normal", "bad-policy", "cycle", "port-occupied"),
        default="normal",
    )
    parser.add_argument("--policy")
    parser.add_argument("--policy-v2", default=str(PROJECT_ROOT / "samples" / "policy_v2.json"))
    parser.add_argument("--content")
    parser.add_argument("--json-out")
    return parser.parse_args(argv)


def run_scenario(args: argparse.Namespace) -> dict[str, Any]:
    if args.policy is None:
        args.policy = str(
            PROJECT_ROOT
            / "samples"
            / ("policy_broken.json" if args.scenario == "bad-policy" else "policy.json")
        )
    if args.content is None:
        args.content = str(
            PROJECT_ROOT
            / "samples"
            / ("content_cycle.json" if args.scenario == "cycle" else "content.json")
        )
    if args.scenario == "normal":
        return _run_normal(args)
    if args.scenario == "bad-policy":
        _scenario_policy_failure(args.policy, args.content)
    if args.scenario == "cycle":
        _scenario_cycle(args.content, args.policy)
    if args.scenario == "port-occupied":
        return _scenario_port_occupied(args)
    raise StateError("unknown scenario", location="--scenario")


def _write_output(payload: Mapping[str, Any], path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(text)
    sys.stdout.flush()
    if path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = run_scenario(args)
        _write_output(result, args.json_out)
        returned_code = result.get("returned_code")
        if isinstance(returned_code, str):
            return exit_code_for(EdgeError(returned_code, "injected scenario returned a business code"))
        return 0
    except EdgeError as exc:
        scope = {
            "bad-policy": str(args.policy),
            "cycle": str(args.content),
            "port-occupied": f"127.0.0.1:{args.base_port}",
        }.get(args.scenario, args.state_dir)
        payload = {
            "ok": False,
            "scenario": args.scenario,
            "scope": scope,
            "returned_code": exc.code,
            "error": exc.to_dict(),
        }
        _write_output(payload, args.json_out)
        return exit_code_for(exc)
    except Exception as exc:  # pragma: no cover - process boundary
        payload = {
            "ok": False,
            "scenario": args.scenario,
            "returned_code": "E_INTERNAL",
            "error": error_payload(exc),
        }
        _write_output(payload, args.json_out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
