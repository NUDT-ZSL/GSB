"""High-level controller operations shared by the CLI and tests."""

from __future__ import annotations

from typing import Any

from .cluster import environment_apps, load_cluster_state
from .errors import GitOpsError
from .manifests import load_sources
from .merge import merge_sources
from .planning import plan_environment
from .project import Project
from .store import Store
from .util import canonical_json


class Controller:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.store = Store(project)

    def _known_clock_watermark(self) -> int:
        candidates = [self.store.max_known_clock_ms(), self.project.initial_clock_ms]
        for environment in self.project.environments:
            candidates.append(self.store.get_env_clock(environment))
        return max(candidates)

    def load(
        self,
        *,
        clock_ms: int | None = None,
        tolerance_ms: int | None = None,
    ) -> dict[str, Any]:
        tolerance = (
            self.project.clock_tolerance_ms if tolerance_ms is None else tolerance_ms
        )
        explicit_clock = clock_ms is not None
        injected_clock = self.project.initial_clock_ms if clock_ms is None else clock_ms
        if not isinstance(injected_clock, int) or isinstance(injected_clock, bool):
            raise GitOpsError(
                "E_CLOCK_ROLLBACK",
                "logical clock must be an integer millisecond value",
                {"clock_ms": injected_clock},
            )
        watermark_before = self._known_clock_watermark()
        if explicit_clock and injected_clock + tolerance < watermark_before:
            raise GitOpsError(
                "E_CLOCK_ROLLBACK",
                "injected logical clock moved backwards beyond tolerance",
                {
                    "clock_ms": injected_clock,
                    "tolerance_ms": tolerance,
                    "watermark_ms": watermark_before,
                },
            )

        v1, v2_shards = load_sources(self.project)
        desired_states = merge_sources(
            self.project,
            v1,
            v2_shards,
            injected_clock,
        )
        decisions = []
        for environment in self.project.environments:
            decisions.extend(desired_states[environment]["migration_decisions"])
        self.store.register_merge(desired_states, decisions, injected_clock)
        watermark_after = self._known_clock_watermark()

        environment_output: dict[str, Any] = {}
        for environment in self.project.environments:
            payload = desired_states[environment]
            shard = v2_shards[environment]
            environment_output[environment] = {
                "active_apps": payload["summary"]["active_apps"],
                "apps": payload["summary"]["apps"],
                "by_source": payload["summary"]["by_source"],
                "merge_key": payload["merge_key"],
                "migration_decisions": payload["summary"]["migration_decisions"],
                "persisted_migration_decisions": self.store.migration_count(environment),
                "tombstones": payload["summary"]["tombstones"],
                "v2_generation": shard.generation,
                "v2_logical_clock_ms": shard.logical_clock_ms,
                "v2_shard_file": shard.shard_file,
                "v2_source_id": shard.source_id,
            }
        return {
            "clock": {
                "injected_ms": injected_clock,
                "source": "explicit" if explicit_clock else "project_default",
                "tolerance_ms": tolerance,
                "watermark_ms": watermark_after,
            },
            "environments": environment_output,
            "format": "load-result-v1",
            "kind": "load",
            "ok": True,
            "project": self.project.name,
            "schema_version": 1,
            "sources": {
                "v1": {
                    "applications": len(v1.apps),
                    "generation": v1.generation,
                    "logical_clock_ms": v1.logical_clock_ms,
                    "sha256": v1.source_id,
                },
                "v2": {
                    environment: {
                        "generation": v2_shards[environment].generation,
                        "sha256": v2_shards[environment].source_id,
                    }
                    for environment in self.project.environments
                },
            },
        }

    def ensure_loaded(self) -> dict[str, Any]:
        missing = [
            environment
            for environment in self.project.environments
            if self.store.get_desired(environment) is None
        ]
        if missing:
            return self.load()
        return {
            "format": "load-result-v1",
            "kind": "load",
            "ok": True,
            "project": self.project.name,
            "schema_version": 1,
        }

    def plan(self, environment: str, *, clock_ms: int | None = None) -> dict[str, Any]:
        self.load(clock_ms=clock_ms)
        return plan_environment(self.project, self.store, environment)

    def _app_status(
        self,
        environment: str,
        entry: dict[str, Any],
        actual: dict[str, Any] | None,
    ) -> dict[str, Any]:
        target_revision = int(entry["manifest_revision"])
        cluster_revision = (
            int(actual.get("committed_manifest_revision", 0)) if actual else 0
        )
        db_revision = self.store.max_committed_revision(environment, entry["app"]) or 0
        committed = max(cluster_revision, db_revision)
        version_ok = target_revision >= committed
        if entry["tombstone"]:
            state = "removed" if actual is None else "pending_removal"
        elif actual is None:
            state = "missing"
        elif (
            actual.get("image_version") == entry["image_version"]
            and actual.get("replicas") == entry["replicas"]
            and version_ok
        ):
            state = "converged"
        else:
            state = "drifted"
        return {
            "actual": None
            if actual is None
            else {
                "committed_manifest_revision": actual.get(
                    "committed_manifest_revision", 0
                ),
                "image_version": actual.get("image_version"),
                "replicas": actual.get("replicas"),
            },
            "app": entry["app"],
            "converged": state in ("converged", "removed") and version_ok,
            "desired": {
                "image_version": entry["image_version"],
                "manifest_revision": target_revision,
                "replicas": entry["replicas"],
                "tombstone": entry["tombstone"],
            },
            "source_format": entry["source_format"],
            "state": state if version_ok else "version_regression",
            "version_check": {
                "accepted": version_ok,
                "committed_manifest_revision": committed,
                "target_manifest_revision": target_revision,
            },
        }

    def _run_summary(self, run: dict[str, Any] | None) -> dict[str, Any] | None:
        if run is None:
            return None
        operations = self.store.get_operations(run["run_id"])
        return {
            "clock_end_ms": run["clock_end_ms"],
            "clock_start_ms": run["clock_start_ms"],
            "operation_counts": {
                status: sum(1 for op in operations if op["status"] == status)
                for status in (
                    "abandoned",
                    "committed",
                    "failed",
                    "not_started",
                    "pending",
                    "running",
                )
            },
            "plan_id": run["plan_id"],
            "run_id": run["run_id"],
            "status": run["status"],
        }

    def status(self, environments: list[str] | None = None) -> dict[str, Any]:
        selected = environments or list(self.project.environments)
        cluster_state = load_cluster_state(self.project)
        output: dict[str, Any] = {}
        global_clock = 0
        for environment in selected:
            desired = self.store.get_desired(environment)
            if desired is None:
                raise GitOpsError(
                    "E_NOT_LOADED",
                    "environment has no persisted desired state",
                    {"environment": environment},
                )
            actual_apps = environment_apps(cluster_state, environment)
            apps: dict[str, Any] = {}
            for entry in sorted(desired["entries"], key=lambda item: item["app"]):
                apps[entry["app"]] = self._app_status(
                    environment,
                    entry,
                    actual_apps.get(entry["app"]),
                )
            env_state = self.store.get_environment_state(environment) or {}
            clock = int(
                env_state.get("logical_clock_ms", self.project.initial_clock_ms)
            )
            global_clock = max(global_clock, clock)
            converged = (
                all(app["converged"] for app in apps.values())
                and self.store.open_operation_count(environment) == 0
            )
            state_counts = {
                state: sum(1 for app in apps.values() if app["state"] == state)
                for state in (
                    "converged",
                    "drifted",
                    "missing",
                    "pending_removal",
                    "removed",
                    "version_regression",
                )
            }
            output[environment] = {
                "apps": apps,
                "converged": converged,
                "last_run": self._run_summary(
                    self.store.get_latest_run(environment)
                ),
                "logical_clock_ms": clock,
                "merge_key": desired["merge_key"],
                "state_counts": state_counts,
            }
        return {
            "environments": output,
            "format": "status-result-v1",
            "global_clock_watermark_ms": global_clock,
            "kind": "status",
            "ok": True,
            "schema_version": 1,
        }

    def verify(self, environments: list[str] | None = None) -> dict[str, Any]:
        selected = environments or list(self.project.environments)
        stored = {environment: self.store.get_desired(environment) for environment in selected}
        missing = sorted(environment for environment, value in stored.items() if value is None)
        if missing:
            raise GitOpsError(
                "E_NOT_LOADED",
                "environment has no persisted desired state",
                {"environments": missing},
            )
        injected_clock = next(iter(stored.values()))["injected_logical_clock_ms"]
        v1, v2_shards = load_sources(self.project)
        recomputed = merge_sources(self.project, v1, v2_shards, injected_clock)
        cluster_state = load_cluster_state(self.project)
        status = self.status(selected)
        output: dict[str, Any] = {}
        all_ok = True
        for environment in selected:
            desired = stored[environment]
            checks = []
            recomputed_matches = canonical_json(desired) == canonical_json(
                recomputed[environment]
            )
            checks.append(
                {
                    "details": {
                        "persisted_merge_key": desired["merge_key"],
                        "recomputed_merge_key": recomputed[environment]["merge_key"],
                    },
                    "name": "persisted_merge_matches_sources",
                    "ok": recomputed_matches,
                }
            )
            status_env = status["environments"][environment]
            checks.append(
                {
                    "details": status_env["state_counts"],
                    "name": "cluster_matches_desired_state",
                    "ok": status_env["converged"],
                }
            )
            open_operations = self.store.open_operation_count(environment)
            checks.append(
                {
                    "details": {"open_operations": open_operations},
                    "name": "no_unfinished_operations",
                    "ok": open_operations == 0,
                }
            )
            expected_decisions = desired["summary"]["migration_decisions"]
            persisted_decisions = self.store.migration_count(environment)
            checks.append(
                {
                    "details": {
                        "expected_at_least": expected_decisions,
                        "persisted": persisted_decisions,
                    },
                    "name": "time_migration_decisions_persisted",
                    "ok": persisted_decisions >= expected_decisions,
                }
            )
            version_regressions = [
                app
                for app in status_env["apps"].values()
                if not app["version_check"]["accepted"]
            ]
            checks.append(
                {
                    "details": {
                        "apps": sorted(
                            app["app"] for app in version_regressions
                        )
                    },
                    "name": "no_version_regression",
                    "ok": not version_regressions,
                }
            )
            actual_apps = environment_apps(cluster_state, environment)
            desired_names = {
                entry["app"] for entry in desired["entries"] if not entry["tombstone"]
            }
            extra_apps = sorted(set(actual_apps) - desired_names)
            checks.append(
                {
                    "details": {"apps": extra_apps},
                    "name": "no_unmanaged_cluster_apps",
                    "ok": not extra_apps,
                }
            )
            env_ok = all(check["ok"] for check in checks)
            all_ok = all_ok and env_ok
            output[environment] = {
                "checks": checks,
                "converged": status_env["converged"],
                "ok": env_ok,
            }
        return {
            "environments": output,
            "format": "verify-result-v1",
            "kind": "verify",
            "ok": all_ok,
            "schema_version": 1,
        }
