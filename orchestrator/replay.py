"""Consistency replay of completed stages after snapshot recovery."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional

from .dag_loader import dag_fingerprint, parse_dag, resolve_stage_script
from .errors import ReplayError
from .executor import (
    SubprocessStageRunner,
    build_stage_env,
    hash_output_file,
)
from .snapshot import Snapshot


@dataclass
class ReplayItem:
    stage_id: str
    ok: bool
    reason: str
    snapshot_sha256: Optional[str]
    replay_sha256: Optional[str]

    def to_dict(self) -> dict:
        return {
            "stage_id": self.stage_id,
            "ok": self.ok,
            "reason": self.reason,
            "snapshot_sha256": self.snapshot_sha256,
            "replay_sha256": self.replay_sha256,
        }


@dataclass
class ReplayReport:
    ok: bool
    dag_version: str
    items: List[ReplayItem]

    def to_dict(self) -> dict:
        return {
            "code": "OK" if self.ok else "E_REPLAY",
            "ok": self.ok,
            "dag_version": self.dag_version,
            "items": [item.to_dict() for item in self.items],
        }


def replay_snapshot(
    snapshot: Snapshot,
    base_dir: str,
    replay_root: str,
    runner: Optional[SubprocessStageRunner] = None,
    clock=None,
    dag_path: Optional[str] = None,
) -> ReplayReport:
    runner = runner or SubprocessStageRunner()
    dag = parse_dag(snapshot.dag_definition)
    dag.source_path = dag_path or os.path.join(base_dir, "dags", "__replay__.json")
    current_fingerprint = dag_fingerprint(dag, base_dir)

    precheck: List[ReplayItem] = []
    if snapshot.dag_fingerprint != current_fingerprint:
        for stage_id in dag.topological_order():
            saved = snapshot.states.get(stage_id)
            precheck.append(
                ReplayItem(
                    stage_id=stage_id,
                    ok=False,
                    reason="dag_or_script_content_changed_fingerprint_mismatch",
                    snapshot_sha256=saved.output_sha256 if saved else None,
                    replay_sha256=None,
                )
            )
        report = ReplayReport(False, snapshot.dag_version, precheck)
        raise ReplayError(
            "DAG version or script content changed; replay is not reproducible",
            [report.to_dict()],
        )

    if os.path.isdir(replay_root):
        shutil.rmtree(replay_root)
    os.makedirs(replay_root, exist_ok=True)

    replayed_outputs: Dict[str, str] = {}
    items: List[ReplayItem] = []
    from .clock import Clock

    clock = clock or Clock()
    for stage_id in dag.topological_order():
        saved = snapshot.states.get(stage_id)
        if saved is None or saved.state != "succeeded":
            continue
        stage = dag.stages[stage_id]
        output_abs = os.path.join(replay_root, f"{stage_id}.jsonl")
        input_map = {
            upstream: replayed_outputs[upstream]
            for upstream in stage.strong_deps + stage.weak_deps
            if upstream in replayed_outputs
        }
        script_abs = resolve_stage_script(dag, stage.script_path)
        env = build_stage_env(
            stage_id=stage_id,
            output_path=output_abs,
            input_map=input_map,
            missing_upstreams=saved.missing_upstreams,
            attempt_no=1,
            script_dir=os.path.dirname(script_abs),
        )
        outcome = runner.run_attempt(
            script_abs=script_abs,
            env=env,
            timeout_ms=stage.timeout_ms or snapshot.default_timeout_ms,
            clock=clock,
        )
        if outcome.exit_code != 0 or outcome.timed_out or not os.path.isfile(output_abs):
            items.append(
                ReplayItem(
                    stage_id,
                    False,
                    f"replay_attempt_failed:{outcome.reason}",
                    saved.output_sha256,
                    None,
                )
            )
            continue
        replay_summary = hash_output_file(output_abs)
        replayed_outputs[stage_id] = output_abs
        if replay_summary.sha256 != saved.output_sha256:
            items.append(
                ReplayItem(
                    stage_id,
                    False,
                    "recomputed_sha256_mismatch",
                    saved.output_sha256,
                    replay_summary.sha256,
                )
            )
        else:
            items.append(
                ReplayItem(
                    stage_id,
                    True,
                    "sha256_match",
                    saved.output_sha256,
                    replay_summary.sha256,
                )
            )

    ok = all(item.ok for item in items)
    report = ReplayReport(ok, snapshot.dag_version, items)
    if not ok:
        raise ReplayError(
            f"replay mismatch for {sum(not item.ok for item in items)} stage(s)",
            [report.to_dict()],
        )
    return report
