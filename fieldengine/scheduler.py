from __future__ import annotations

import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .errors import ErrorCode, EngineError, fail
from .execution import execute_shard, finalize_results
from .model import Experiment
from .planning import RunPlan, ShardPlan
from .store import Store


class Scheduler:
    def __init__(self, store: Store, experiment: Experiment, plan: RunPlan) -> None:
        self.store = store
        self.experiment = experiment
        self.plan = plan

    def prepare_new(self) -> dict[str, Any]:
        summary = self.store.initialize(self.experiment, self.plan)
        if summary["run"]["status"] == "COMPLETE":
            return summary
        if summary["run"]["status"] != "INITIALIZED":
            raise fail(
                ErrorCode.RUN_EXISTS_INCOMPLETE,
                "an incomplete run already exists; use the resume subcommand",
                status=summary["run"]["status"],
                db_path=str(self.store.path),
            )
        return summary

    def prepare_resume(self) -> dict[str, Any]:
        self.store.verify_identity(self.experiment, self.plan)
        summary = self.store.initialize(self.experiment, self.plan)
        reclaimed = self.store.recover_orphans()
        if reclaimed:
            summary["recovered_orphan_shards"] = reclaimed
        return summary

    def _perform_actions(self, actions: list[dict[str, Any]]) -> None:
        for action in actions:
            kind = action.get("action")
            if kind == "delay":
                time.sleep(int(action.get("milliseconds", 0)) / 1000.0)
            elif kind == "kill":
                os.kill(os.getpid(), signal.SIGKILL)
            elif kind == "fail":
                raise EngineError(
                    code=str(action.get("error_code", "E_INJECTED_SHARD_FAILURE")),
                    message=f"injected failure {action.get('id')}",
                    details={"injection_id": action.get("id")},
                    exit_code=6,
                )

    def _execute_one(self, shard: ShardPlan) -> list[dict[str, Any]]:
        try:
            self._perform_actions(self.store.claim_shard_injections(shard))
            partial = execute_shard(self.experiment, self.plan, shard)
            return self.store.commit_shard(self.experiment, partial)
        except EngineError as exc:
            try:
                self.store.mark_shard_failed(
                    self.experiment, shard.shard_id, exc.code, exc.message
                )
            except Exception:
                pass
            return []
        except BaseException as exc:
            try:
                self.store.mark_shard_failed(
                    self.experiment,
                    shard.shard_id,
                    ErrorCode.INTERNAL,
                    f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            return []

    def execute(self) -> dict[str, Any]:
        while True:
            if self.store.all_shards_committed():
                partials = self.store.get_committed_partials()
                groups, windows = finalize_results(partials, self.experiment, self.plan)
                return self.store.complete_run(self.experiment, groups, windows)
            if self.store.has_terminal_failures() and not self.store.pending_or_running():
                code = (
                    ErrorCode.RUN_ABANDONED
                    if self.store.has_abandoned()
                    else ErrorCode.RUN_INCOMPLETE
                )
                self.store.mark_incomplete(
                    code,
                    "one or more shards were abandoned or failed; no final result was committed",
                )
                summary = self.store.run_summary()
                summary["ok"] = False
                return summary
            claims = self.store.claim_shards(
                self.experiment,
                self.plan,
                limit=self.experiment.config.parallel_limit,
            )
            if not claims:
                if self.store.pending_or_running():
                    time.sleep(0.02)
                    continue
                if self.store.has_terminal_failures():
                    continue
                self.store.mark_incomplete(
                    ErrorCode.RUN_INCOMPLETE,
                    "scheduler found no claimable shards and no running shards",
                )
                summary = self.store.run_summary()
                summary["ok"] = False
                return summary
            actions: list[dict[str, Any]] = []
            with ThreadPoolExecutor(
                max_workers=min(self.experiment.config.parallel_limit, len(claims)),
                thread_name_prefix="field-shard",
            ) as executor:
                futures = {
                    executor.submit(self._execute_one, shard): shard for shard in claims
                }
                for future in as_completed(futures):
                    actions.extend(future.result())
            self._perform_actions(actions)

    def execute_new(self) -> dict[str, Any]:
        self.prepare_new()
        return self.execute()

    def execute_resume(self) -> dict[str, Any]:
        self.prepare_resume()
        return self.execute()
