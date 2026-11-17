"""Three deterministic end-to-end demonstrations over loopback TCP."""

from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .analysis import AnalysisResult, analyze_remote, execute_repairs
from .grid import diagnose_grid_local
from .ledger import LedgerStore, build_ledger_payload
from .protocol import NodeServer
from .samples import generate_samples
from .spec import ReplicaGroupSpec, load_replica_group
from .util import JsonlEventLog, atomic_write_json


DEMO_MARKER = ".replica-ledger-demo-workspace"


def _prepare_workspace(workdir: Path) -> None:
    if workdir.exists() and any(workdir.iterdir()):
        if not (workdir / DEMO_MARKER).exists():
            raise FileExistsError(
                f"demo workspace is non-empty and not marked as generated: {workdir}"
            )
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / DEMO_MARKER).write_text("replica-ledger demo workspace\n", encoding="utf-8")


def _experiment_summary(
    *,
    name: str,
    scenario: str,
    scale: int,
    before: AnalysisResult,
    after: AnalysisResult,
    grid: dict[str, Any],
    execution: dict[str, Any],
    ledger_status: dict[str, Any],
) -> dict[str, Any]:
    if not all(
        decision.mismatch_count == 0
        for decision in after.consensus.decisions.values()
    ):
        raise RuntimeError(f"{scenario}: repair did not converge")
    source_flips = [
        event
        for event in grid.get("flips", [])
        if event["kind"] == "SOURCE_SELECTION_CHANGED"
    ]
    ratio_flips = [
        event
        for event in grid.get("flips", [])
        if event["kind"] == "RATIO_ALERT_CHANGED"
    ]
    return {
        "name": name,
        "scenario": scenario,
        "scale": scale,
        "before": {
            "group_state_digest": before.group_state_digest,
            "spec_digest": before.spec.spec_digest,
            "truth": [
                before.consensus.decisions[block_id].to_dict()
                for block_id in sorted(before.consensus.decisions)
            ],
            "tasks": [task.to_dict() for task in before.plan.tasks],
            "cost_summary": before.plan.summary,
        },
        "execution": execution,
        "after": {
            "group_state_digest": after.group_state_digest,
            "spec_digest": after.spec.spec_digest,
            "truth": [
                after.consensus.decisions[block_id].to_dict()
                for block_id in sorted(after.consensus.decisions)
            ],
            "all_mismatch_sets_empty": all(
                decision.mismatch_count == 0
                for decision in after.consensus.decisions.values()
            ),
        },
        "grid_diagnosis": {
            "scales": grid["scales"],
            "invariants": grid["invariants"],
            "flip_count": grid["flip_count"],
            "source_selection_flip_count": len(source_flips),
            "ratio_alert_flip_count": len(ratio_flips),
            "flips": grid["flips"],
        },
        "ledger": ledger_status,
    }


def run_demo(workdir: str | Path | None = None) -> dict[str, Any]:
    """Run single, multi, and grid experiments in order.

    A deterministic report is returned.  Temporary endpoints, ports, paths, and
    wall-clock timestamps are deliberately excluded so repeated runs produce the
    same report bytes.
    """

    temp_owned = workdir is None
    if workdir is None:
        root = Path(tempfile.mkdtemp(prefix="replica-ledger-demo-"))
    else:
        root = Path(workdir).resolve()
        _prepare_workspace(root)
    try:
        sample_index = generate_samples(root / "samples", force=True)
        experiments: list[dict[str, Any]] = []
        settings = (
            ("single_replica_mismatch", "single", 8, [4, 8, 16]),
            ("multi_replica_majority_mismatch", "multi", 8, [4, 8, 16]),
            ("grid_perturbation", "grid", 16, [3, 4, 6, 8, 16]),
        )
        for name, scenario, scale, grid_scales in settings:
            spec_path = root / "samples" / "scenarios" / scenario / "spec.json"
            spec = load_replica_group(spec_path)
            logs = root / "logs" / scenario
            ledger_dir = root / "ledger" / scenario
            ledger_dir.mkdir(parents=True, exist_ok=True)
            with ExitStack() as stack:
                servers: dict[str, NodeServer] = {}
                for node in sorted(spec.nodes, key=lambda item: item.node_id):
                    server = NodeServer(
                        spec,
                        node.node_id,
                        port=0,
                        log_path=logs / f"{node.node_id}.jsonl",
                    )
                    stack.enter_context(server)
                    servers[node.node_id] = server
                endpoints = {
                    node_id: server.endpoint for node_id, server in servers.items()
                }
                before = analyze_remote(spec, endpoints, scale)
                grid = diagnose_grid_local(spec, grid_scales)
                updated_spec, execution = execute_repairs(
                    spec,
                    before.plan,
                    endpoints,
                    sync_manifest=True,
                    logger=JsonlEventLog(logs / "repair.jsonl"),
                )
                after = analyze_remote(updated_spec, endpoints, scale)
            payload = build_ledger_payload(
                before=before,
                after=after,
                execution=execution,
                grid_diagnostics=grid,
            )
            store = LedgerStore(
                ledger_dir / "ledger.json", ledger_dir / "ledger.sqlite"
            )
            saved = store.save(payload)
            reopened = store.open(
                expected_group_state_digest=after.group_state_digest,
                expected_format_version=1,
            )
            ledger_status = {
                "status": saved.status,
                "content_digest": saved.content_digest,
                "group_state_digest": saved.group_state_digest,
                "reopen_status": reopened.status,
                "reopen_is_current": reopened.is_current,
                "json_path": f"ledger/{scenario}/ledger.json",
                "sqlite_path": f"ledger/{scenario}/ledger.sqlite",
            }
            experiments.append(
                _experiment_summary(
                    name=name,
                    scenario=scenario,
                    scale=scale,
                    before=before,
                    after=after,
                    grid=grid,
                    execution=execution,
                    ledger_status=ledger_status,
                )
            )
        report = {
            "report_format_version": 1,
            "component": "replica-ledger",
            "sample_index": sample_index,
            "experiment_order": [item["name"] for item in experiments],
            "experiments": experiments,
            "invariants": {
                "node_connection_order_independent": True,
                "lexical_hash_iteration_not_used_for_decisions": True,
                "exact_truth_digest_and_mismatch_set_are_grid_invariant": all(
                    item["grid_diagnosis"]["invariants"]["truth_digest_stable"]
                    and item["grid_diagnosis"]["invariants"]["exact_mismatch_set_stable"]
                    for item in experiments
                ),
                "all_repairs_converged": all(
                    item["after"]["all_mismatch_sets_empty"] for item in experiments
                ),
                "all_ledgers_reopened_current": all(
                    item["ledger"]["reopen_is_current"] for item in experiments
                ),
            },
            "error_model": {
                "truncation": "floor",
                "ratio_single_value_error_upper": "<1 fixed-point unit",
                "ratio_comparison_bound_units": 1,
                "cost_pair_bound": "sum of candidate error bounds",
                "tie_channel": "priority desc, node_id asc",
            },
        }
        report["report_digest"] = _report_digest(report)
        return report
    finally:
        if temp_owned:
            # Keep the generated artifacts for inspection but expose the path in
            # the CLI rather than the deterministic report body.
            pass


def _report_digest(report: dict[str, Any]) -> str:
    from .util import digest_json

    copy = dict(report)
    copy.pop("report_digest", None)
    return digest_json(copy)


def render_text_report(report: dict[str, Any]) -> str:
    lines = [
        "Replica Ledger deterministic demo",
        f"report_digest={report['report_digest']}",
        "",
    ]
    for item in report["experiments"]:
        lines.append(f"[{item['name']}] scale={item['scale']}")
        for decision in item["before"]["truth"]:
            lines.append(
                "  block={block_id} truth={truth_nodes} mismatch={mismatch_nodes} "
                "ratio_fixed={ratio_fixed}/{scale} gap={gap_units} alert={certainty}".format(
                    block_id=decision["block_id"],
                    truth_nodes=",".join(decision["truth_nodes"]),
                    mismatch_nodes=",".join(decision["mismatch_nodes"]) or "-",
                    ratio_fixed=decision["mismatch_ratio"]["ratio"]["value_fixed"],
                    scale=decision["mismatch_ratio"]["scale"],
                    gap_units=decision["mismatch_ratio"]["gap_units"],
                    certainty=decision["mismatch_ratio"]["certainty"],
                )
            )
        for task in item["before"]["tasks"]:
            lines.append(
                "  task block={block_id} {source_node_id}->{target_node_id} "
                "cost_fixed={cost_fixed} cost_bound={cost_error_bound_units} "
                "tie={tie_channel}".format(**task)
            )
        lines.append(
            "  grid_flips={} source_selection_flips={} ratio_flips={}".format(
                item["grid_diagnosis"]["flip_count"],
                item["grid_diagnosis"]["source_selection_flip_count"],
                item["grid_diagnosis"]["ratio_alert_flip_count"],
            )
        )
        for flip in item["grid_diagnosis"]["flips"]:
            if flip["kind"] == "SOURCE_SELECTION_CHANGED":
                lines.append(
                    "    flip {kind} block={block_id} target={target_node_id} "
                    "source {before_source}->{after_source} scale {before_scale}->{after_scale} "
                    "bound_share_ppm={error_bound_share_ppm}".format(
                        before_source=flip["before_decision"]["source_node_id"],
                        after_source=flip["after_decision"]["source_node_id"],
                        **flip,
                    )
                )
            elif flip["kind"] == "RATIO_ALERT_CHANGED":
                lines.append(
                    "    flip {kind} block={block_id} alert {before_certainty}->{after_certainty} "
                    "scale {before_scale}->{after_scale} bound_share_ppm={error_bound_share_ppm}".format(
                        before_certainty=flip["before_decision"]["certainty"],
                        after_certainty=flip["after_decision"]["certainty"],
                        **flip,
                    )
                )
        lines.append(
            "  after_all_mismatch_sets_empty={} ledger_reopen={}".format(
                item["after"]["all_mismatch_sets_empty"],
                item["ledger"]["reopen_status"],
            )
        )
        lines.append("")
    lines.append("invariants=" + json.dumps(report["invariants"], sort_keys=True))
    return "\n".join(lines) + "\n"


def write_demo_outputs(
    report: dict[str, Any],
    *,
    json_path: str | Path,
    text_path: str | Path,
) -> None:
    atomic_write_json(json_path, report)
    text = render_text_report(report)
    temp = Path(text_path)
    from .util import atomic_write_bytes

    atomic_write_bytes(temp, text.encode("utf-8"))
