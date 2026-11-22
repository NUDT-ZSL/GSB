from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Callable

from .engine import (
    build_cluster,
    candidate_hash,
    cluster_events,
    cluster_hash,
    root_candidates,
)
from .errors import AlertLabError, invalid
from .events import EventStream, event_stream_from_document, load_event_stream
from .rules import (
    MergedRule,
    load_and_merge,
    merged_rule_from_record,
    SEVERITIES,
)
from .store import Store
from .topology import Topology, load_topology
from .util import canonical_json, require_int, require_str_list, sha256_json


DEFAULT_WINDOW_MS = 60_000


class AlertLabService:
    def __init__(
        self,
        workspace: str | Path,
        *,
        database_path: str | Path | None = None,
        recover: bool = True,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.samples = self.workspace / "samples"
        self.data_dir = self.workspace / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        database = (
            Path(database_path)
            if database_path is not None
            else self.data_dir / "alertlab.sqlite3"
        )
        self.store = Store(database)
        self.store.initialize()
        if recover:
            self.recover()

    def _resolve(self, value: str | Path | None, default: str | Path | None) -> Path | None:
        if value is None:
            return None if default is None else Path(default).resolve()
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.workspace / path).resolve()

    def load(
        self,
        *,
        v1_path: str | Path | None = None,
        v2_path: str | Path | None = None,
        topology_path: str | Path | None = None,
        events_path: str | Path | None = None,
        logical_clock_ms: int | None = None,
        include_v1: bool = True,
        include_v2: bool = True,
        crash_hook: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        topology_file = self._resolve(
            topology_path, self.samples / "topology.json"
        )
        v1_file = self._resolve(v1_path, self.samples / "v1" / "rules_v1.json")
        v2_file = self._resolve(v2_path, self.samples / "v2" / "index.json")
        events_file = self._resolve(events_path, self.samples / "events" / "alerts.json")
        if topology_file is None:
            raise invalid("E_TOPOLOGY_FILE", "topology path is required")

        with self.store.locked():
            self._recover_locked()
            topology = load_topology(topology_file)
            stream: EventStream | None = None
            if events_file is not None and events_file.exists():
                stream = load_event_stream(events_file, topology)
            if logical_clock_ms is not None:
                if not require_int(logical_clock_ms, minimum=0):
                    raise invalid(
                        "E_CLOCK_INVALID",
                        "logical_clock_ms must be a non-negative integer",
                    )
                if stream is not None:
                    document = json.loads(canonical_json(stream.document))
                    document["injected_clock_ms"] = logical_clock_ms
                    stream = event_stream_from_document(document, topology)
            merged = load_and_merge(
                topology,
                v1_path=(
                    v1_file
                    if include_v1 and v1_file and v1_file.exists()
                    else None
                ),
                v2_path=(
                    v2_file
                    if include_v2 and v2_file and v2_file.exists()
                    else None
                ),
                injected_clock_ms=logical_clock_ms,
            )
            activation = self.store.activate_snapshot(
                merged, topology, crash_hook=crash_hook
            )

            ingested = False
            run_summary: dict[str, Any] | None = None
            if stream is not None:
                materialized, conflict = self.store.stream_materialization(stream)
                if conflict == "content_conflict":
                    raise invalid(
                        "E_EVENT_CONFLICT",
                        "event stream content conflicts with persisted events",
                        stream_id=stream.stream_id,
                    )
                if not (materialized and not activation["created"]):
                    view = self.store.get_active_view()
                    assert view is not None
                    rules = self._rules_for_view(view["view_id"])
                    run = self.store.register_ingest(
                        view["view_id"], stream, rules, topology
                    )
                    run_summary = self._resume_ingest_run_locked(
                        run, topology, crash_hook=crash_hook
                    )
                    ingested = True
                else:
                    run_summary = {"status": "complete", "reused": True, "created_clusters": 0}
            status = self.status()
            return {
                "ok": True,
                "load": {
                    "view_id": activation["view_id"],
                    "result_revision": activation["result_revision"],
                    "view_created": activation["created"],
                    "ingested": ingested,
                    "ingest_run": run_summary,
                    "snapshot_version": merged.snapshot_version,
                    "logical_clock_ms": merged.logical_clock_ms,
                    "injected_clock_ms": merged.injected_clock_ms,
                    "rule_count": len(merged.rules),
                    "active_rule_count": len(merged.active_rules),
                    "source_versions": [source["source_version"] for source in merged.sources],
                },
                "status": status,
            }

    def _rules_for_view(self, view_id: int) -> dict[str, MergedRule]:
        return {
            name: merged_rule_from_record(record)
            for name, record in self.store.get_rules(view_id).items()
        }

    @staticmethod
    def _decorate_events(
        events: list[dict[str, Any]],
        rules: dict[str, MergedRule],
        injected_clock_ms: int,
    ) -> list[dict[str, Any]]:
        decorated: list[dict[str, Any]] = []
        for source in events:
            event = dict(source)
            rule = rules.get(event["rule_name"])
            eligible = bool(
                rule is not None
                and not rule.tombstone
                and not event.get("late", 0)
                and event["instance_id"] in rule.instances
                and event["event_ts_ms"] <= injected_clock_ms
                and event["event_ts_ms"] >= rule.effective_at_ms
            )
            event["eligible_for_signal"] = 1 if eligible else 0
            decorated.append(event)
        return decorated

    def _resume_ingest_run_locked(
        self,
        run: dict[str, Any],
        topology: Topology,
        *,
        crash_hook: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        if run["status"] == "complete":
            return {
                "run_id": run["run_id"],
                "status": "complete",
                "reused": True,
                "created_clusters": 0,
            }
        view_id = run["view_id"]
        rules = self._rules_for_view(view_id)
        events = self.store.get_events()
        decorated = self._decorate_events(events, rules, run["injected_clock_ms"])
        clusters = cluster_events(
            decorated,
            rules,
            topology,
            window_ms=run["window_ms"],
            injected_clock_ms=run["injected_clock_ms"],
        )
        expected_ids = sorted(cluster["cluster_id"] for cluster in clusters)
        self.store.set_expected_clusters(run["run_id"], expected_ids)
        existing_hashes = self.store.get_cluster_hashes(run["result_revision"])
        created = 0
        for cluster in clusters:
            cluster_id = cluster["cluster_id"]
            if cluster_id in existing_hashes:
                if existing_hashes[cluster_id] != cluster_hash(cluster):
                    raise invalid(
                        "E_NONDETERMINISTIC",
                        "closed cluster content changed during replay",
                        cluster_id=cluster_id,
                    )
                continue
            rule = rules[cluster["rule_name"]]
            candidates = root_candidates(cluster, rule, topology, decorated)
            was_created = self.store.commit_cluster(
                run_id=run["run_id"],
                view_id=view_id,
                result_revision=run["result_revision"],
                cluster=cluster,
                candidates=candidates,
                crash_hook=crash_hook,
            )
            if was_created:
                created += 1
                existing_hashes[cluster_id] = cluster_hash(cluster)
        self.store.finalize_ingest(run["run_id"], crash_hook=crash_hook)
        return {
            "run_id": run["run_id"],
            "status": "complete",
            "reused": not created,
            "created_clusters": created,
            "expected_clusters": len(expected_ids),
            "late_events": sum(
                1 for event in events if event["stream_id"] == run["stream_id"] and event["late"]
            ),
        }

    def _normalize_patch(
        self,
        topology: Topology,
        rules: dict[str, MergedRule],
        patch: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        patch_id = patch.get("patch_id")
        clock = patch.get("logical_clock_ms")
        changes = patch.get("changes")
        if not isinstance(patch_id, str) or not patch_id:
            raise invalid("E_PATCH_SCHEMA", "patch_id must be a non-empty string")
        if not require_int(clock, minimum=0):
            raise invalid(
                "E_PATCH_SCHEMA", "logical_clock_ms must be a non-negative integer"
            )
        if not isinstance(changes, list) or not changes:
            raise invalid("E_PATCH_SCHEMA", "changes must be a non-empty list")
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        allowed_fields = {
            "upstream_dependencies",
            "silence_labels",
            "effective_at_ms",
        }
        for change in changes:
            if not isinstance(change, dict):
                raise invalid("E_PATCH_SCHEMA", "each change must be an object")
            rule_name = change.get("rule_name")
            values = change.get("set")
            if not isinstance(rule_name, str) or not rule_name:
                raise invalid("E_PATCH_SCHEMA", "change rule_name must be a string")
            if rule_name in seen:
                raise invalid(
                    "E_PATCH_SCHEMA",
                    "a patch may modify each rule only once",
                    rule_name=rule_name,
                )
            seen.add(rule_name)
            rule = rules.get(rule_name)
            if rule is None:
                raise invalid(
                    "E_PATCH_UNKNOWN_RULE",
                    "patch references an unknown rule",
                    rule_name=rule_name,
                )
            if rule.tombstone:
                raise invalid(
                    "E_PATCH_TOMBSTONE_RULE",
                    "tombstoned rules cannot be patched",
                    rule_name=rule_name,
                )
            if not isinstance(values, dict) or not values:
                raise invalid(
                    "E_PATCH_FIELD",
                    "patch set must be a non-empty object",
                    rule_name=rule_name,
                )
            unknown = sorted(set(values) - allowed_fields)
            if unknown:
                raise invalid(
                    "E_PATCH_FIELD",
                    "patch contains fields that are not incrementally supported",
                    rule_name=rule_name,
                    fields=unknown,
                )
            actual: dict[str, Any] = {}
            before: dict[str, Any] = {}
            if "upstream_dependencies" in values:
                dependencies = values["upstream_dependencies"]
                if not require_str_list(dependencies):
                    raise invalid(
                        "E_PATCH_FIELD",
                        "upstream_dependencies must be a unique string list",
                        rule_name=rule_name,
                    )
                for dependency in dependencies:
                    if dependency not in topology.services:
                        raise invalid(
                            "E_PATCH_DEPENDENCY_UNRESOLVED",
                            "patch dependency cannot be resolved in topology",
                            rule_name=rule_name,
                            dependency=dependency,
                        )
                    if dependency == rule.service:
                        raise invalid(
                            "E_PATCH_DEPENDENCY_UNRESOLVED",
                            "patch dependency cannot be the rule's own service",
                            rule_name=rule_name,
                            dependency=dependency,
                        )
                if tuple(dependencies) != rule.upstream_dependencies:
                    actual["upstream_dependencies"] = list(dependencies)
                    before["upstream_dependencies"] = list(rule.upstream_dependencies)
            if "silence_labels" in values:
                labels = values["silence_labels"]
                if not require_str_list(labels):
                    raise invalid(
                        "E_PATCH_FIELD",
                        "silence_labels must be a unique string list",
                        rule_name=rule_name,
                    )
                if tuple(labels) != rule.silence_labels:
                    actual["silence_labels"] = list(labels)
                    before["silence_labels"] = list(rule.silence_labels)
            if "effective_at_ms" in values:
                effective = values["effective_at_ms"]
                if not require_int(effective, minimum=0):
                    raise invalid(
                        "E_PATCH_FIELD",
                        "effective_at_ms must be an integer",
                        rule_name=rule_name,
                    )
                if effective != rule.effective_at_ms:
                    actual["effective_at_ms"] = effective
                    before["effective_at_ms"] = rule.effective_at_ms
            if actual:
                normalized.append(
                    {
                        "rule_name": rule_name,
                        "instances": list(rule.instances),
                        "set": actual,
                        "fields": sorted(actual),
                        "before": before,
                    }
                )
        if not normalized:
            return [], {}
        payload = {
            "patch_id": patch_id,
            "logical_clock_ms": clock,
            "changes": normalized,
        }
        return normalized, payload

    @staticmethod
    def _apply_changes(
        rules: dict[str, MergedRule], normalized: list[dict[str, Any]], patch_id: str
    ) -> list[MergedRule]:
        changed = {item["rule_name"]: item for item in normalized}
        result: list[MergedRule] = []
        for rule_name in sorted(rules):
            rule = rules[rule_name]
            item = changed.get(rule_name)
            if item is None:
                result.append(rule)
                continue
            changes = item["set"]
            adjudication = dict(rule.adjudication)
            adjudication["last_patch"] = {
                "patch_id": patch_id,
                "fields": item["fields"],
                "before": item["before"],
                "after": item["set"],
            }
            result.append(
                dataclasses.replace(
                    rule,
                    upstream_dependencies=tuple(
                        changes.get(
                            "upstream_dependencies",
                            list(rule.upstream_dependencies),
                        )
                    ),
                    silence_labels=tuple(
                        changes.get("silence_labels", list(rule.silence_labels))
                    ),
                    effective_at_ms=changes.get(
                        "effective_at_ms", rule.effective_at_ms
                    ),
                    adjudication=adjudication,
                )
            )
        return result

    def patch(
        self,
        patch: dict[str, Any],
        *,
        crash_hook: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        with self.store.locked():
            self._recover_locked()
            active = self.store.get_active_view()
            if active is None:
                raise invalid("E_NOT_FOUND", "load a rule snapshot before patching", http_status=404)
            basic = self._basic_patch_digest(patch)
            topology = self.store.get_topology()
            existing = self.store.get_patch(basic["patch_id"])
            if existing is not None:
                if existing["digest"] != basic["digest"]:
                    raise invalid(
                        "E_PATCH_CONFLICT",
                        "patch id was already used with different content",
                        patch_id=basic["patch_id"],
                    )
                if existing["status"] == "complete":
                    response = dict(existing["response"] or {})
                    response["idempotent"] = True
                    response["status"] = self.status()
                    return response
                patch_run = existing
                response = self._execute_patch_results_locked(
                    patch_run, topology, crash_hook=crash_hook
                )
                response["status"] = self.status()
                return response
            rules = self._rules_for_view(active["view_id"])
            normalized, canonical_patch = self._normalize_patch(topology, rules, patch)
            if not normalized:
                return {
                    "ok": True,
                    "idempotent": False,
                    "applied": False,
                    "reason": "patch contains no effective changes",
                    "rule_view": active,
                    "status": self.status(),
                }
            new_rules = self._apply_changes(
                rules, normalized, canonical_patch["patch_id"]
            )
            patch_run = self.store.create_patch_run(
                patch_id=canonical_patch["patch_id"],
                digest=basic["digest"],
                logical_clock_ms=canonical_patch["logical_clock_ms"],
                changes=normalized,
                changes_json=canonical_patch,
                new_rules=new_rules,
                base_view=active,
            )
            response = self._execute_patch_results_locked(
                patch_run, topology, crash_hook=crash_hook
            )
            response["status"] = self.status()
            return response

    @staticmethod
    def _basic_patch_digest(patch: dict[str, Any]) -> dict[str, str]:
        if not isinstance(patch, dict):
            raise invalid("E_PATCH_SCHEMA", "patch body must be an object")
        patch_id = patch.get("patch_id")
        clock = patch.get("logical_clock_ms")
        changes = patch.get("changes")
        if not isinstance(patch_id, str) or not patch_id:
            raise invalid("E_PATCH_SCHEMA", "patch_id must be a non-empty string")
        if not require_int(clock, minimum=0):
            raise invalid(
                "E_PATCH_SCHEMA", "logical_clock_ms must be a non-negative integer"
            )
        if not isinstance(changes, list) or not changes:
            raise invalid("E_PATCH_SCHEMA", "changes must be a non-empty list")
        normalized = []
        for change in changes:
            if not isinstance(change, dict):
                raise invalid("E_PATCH_SCHEMA", "each change must be an object")
            rule_name = change.get("rule_name")
            values = change.get("set")
            if not isinstance(rule_name, str) or not rule_name or not isinstance(values, dict):
                raise invalid(
                    "E_PATCH_SCHEMA",
                    "each change requires rule_name and a set object",
                )
            normalized.append(
                {"rule_name": rule_name, "set": dict(sorted(values.items()))}
            )
        normalized.sort(key=lambda item: item["rule_name"])
        return {
            "patch_id": patch_id,
            "digest": sha256_json(
                {
                    "patch_id": patch_id,
                    "logical_clock_ms": clock,
                    "changes": normalized,
                }
            ),
        }

    def _execute_patch_results_locked(
        self,
        patch_run: dict[str, Any],
        topology: Topology,
        *,
        crash_hook: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        if patch_run["status"] == "complete":
            response = dict(patch_run.get("response") or {})
            response["idempotent"] = True
            return response
        base_view_id = patch_run["base_view_id"]
        new_view_id = patch_run["new_view_id"]
        result_revision = patch_run["result_revision"]
        base_revision = self.store.get_view_result_revision(base_view_id)
        old_clusters = self.store.get_clusters(base_revision)
        old_candidates = {
            cluster["cluster_id"]: self.store.get_candidates(
                base_revision, cluster["cluster_id"]
            )
            for cluster in old_clusters
        }
        new_rules = self._rules_for_view(new_view_id)
        changes = patch_run["changes"]
        if isinstance(changes, dict):
            changes = changes.get("changes", [])
        changed_by_rule = {item["rule_name"]: item for item in changes}
        aggregation_changed = {
            item["rule_name"]
            for item in changes
            if set(item["fields"]) & {"silence_labels", "effective_at_ms"}
        }
        dependency_only = set(changed_by_rule) - aggregation_changed
        events = self._decorate_events(
            self.store.get_events(),
            new_rules,
            patch_run["logical_clock_ms"],
        )
        old_windows = {
            cluster["rule_name"]: cluster["window_ms"] for cluster in old_clusters
        }
        view_windows = self.store.get_view_windows(base_view_id)
        default_window = view_windows[0] if len(view_windows) == 1 else DEFAULT_WINDOW_MS

        reused_clusters: list[str] = []
        reused_candidates: list[str] = []
        candidate_changed_clusters: dict[str, list[str]] = {}
        final_cluster_map: dict[str, dict[str, Any]] = {}
        final_candidate_map: dict[str, list[dict[str, Any]]] = {}

        for cluster in old_clusters:
            cluster_id = cluster["cluster_id"]
            rule_name = cluster["rule_name"]
            if rule_name in aggregation_changed:
                continue
            if rule_name in changed_by_rule:
                reused_clusters.append(cluster_id)
                final_cluster_map[cluster_id] = cluster
                final_candidate_map[cluster_id] = root_candidates(
                    cluster, new_rules[rule_name], topology, events
                )
                continue
            fresh_candidates = root_candidates(
                cluster, new_rules[rule_name], topology, events
            )
            final_cluster_map[cluster_id] = cluster
            final_candidate_map[cluster_id] = fresh_candidates
            reused_clusters.append(cluster_id)
            if fresh_candidates == old_candidates[cluster_id]:
                reused_candidates.extend(
                    f"{cluster_id}/{candidate['candidate_id']}"
                    for candidate in old_candidates[cluster_id]
                )
            else:
                candidate_changed_clusters[cluster_id] = sorted(
                    {
                        signal["rule_name"]
                        for candidate in old_candidates[cluster_id]
                        for signal in candidate["signals"]
                    }
                )

        if aggregation_changed:
            regenerated = cluster_events(
                events,
                new_rules,
                topology,
                window_ms=default_window,
                injected_clock_ms=patch_run["logical_clock_ms"],
            )
            for cluster in regenerated:
                if cluster["rule_name"] not in aggregation_changed:
                    continue
                cluster_id = cluster["cluster_id"]
                final_cluster_map[cluster_id] = cluster
                final_candidate_map[cluster_id] = root_candidates(
                    cluster, new_rules[cluster["rule_name"]], topology, events
                )

        final_clusters = [final_cluster_map[key] for key in sorted(final_cluster_map)]
        final_clusters.sort(
            key=lambda cluster: (
                cluster["service"],
                cluster["rule_name"],
                cluster["first_event_ms"],
                cluster["cluster_id"],
            )
        )
        final_candidates = {
            key: sorted(value, key=lambda candidate: candidate["rank"])
            for key, value in final_candidate_map.items()
        }
        invalidations, invalidated_clusters, invalidated_candidates = self._build_invalidations(
            patch_id=patch_run["patch_id"],
            new_view_id=new_view_id,
            result_revision=result_revision,
            changed=changes,
            old_clusters=old_clusters,
            old_candidates=old_candidates,
            new_clusters=final_clusters,
            new_candidates=final_candidates,
            candidate_changed_clusters=candidate_changed_clusters,
            dependency_only=dependency_only,
            aggregation_changed=aggregation_changed,
        )
        response = {
            "ok": True,
            "idempotent": False,
            "applied": True,
            "patch_id": patch_run["patch_id"],
            "rule_view": {
                "view_id": new_view_id,
                "kind": "patch",
                "snapshot_version": self.store.get_view(new_view_id)["snapshot_version"],
                "logical_clock_ms": patch_run["logical_clock_ms"],
                "patch_id": patch_run["patch_id"],
            },
            "changed_rules": [
                {
                    "rule_name": item["rule_name"],
                    "fields": item["fields"],
                    "before": item["before"],
                    "after": item["set"],
                }
                for item in changes
            ],
            "invalidated": {
                "clusters": invalidated_clusters,
                "root_candidates": invalidated_candidates,
            },
            "reused": {
                "clusters": sorted(reused_clusters),
                "root_candidates": sorted(reused_candidates),
            },
            "invalidations": invalidations,
            "result_revision": result_revision,
        }
        self.store.commit_patch_results(
            patch=patch_run,
            clusters=final_clusters,
            candidates_by_cluster=final_candidates,
            invalidations=invalidations,
            response=response,
            crash_hook=crash_hook,
        )
        return response

    def _build_invalidations(
        self,
        *,
        patch_id: str,
        new_view_id: int,
        result_revision: int,
        changed: list[dict[str, Any]],
        old_clusters: list[dict[str, Any]],
        old_candidates: dict[str, list[dict[str, Any]]],
        new_clusters: list[dict[str, Any]],
        new_candidates: dict[str, list[dict[str, Any]]],
        candidate_changed_clusters: dict[str, list[str]],
        dependency_only: set[str],
        aggregation_changed: set[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        trigger_source = f"patch:{patch_id}"
        invalidations: list[dict[str, Any]] = []
        invalidated_clusters: list[dict[str, Any]] = []
        invalidated_candidates: list[dict[str, Any]] = []
        old_by_id = {cluster["cluster_id"]: cluster for cluster in old_clusters}
        new_by_id = {cluster["cluster_id"]: cluster for cluster in new_clusters}

        for item in changed:
            rule_name = item["rule_name"]
            fields = item["fields"]
            reason = "field_change:" + ",".join(fields)
            evidence = {"before": item["before"], "after": item["set"]}
            invalidations.append(
                {
                    "trigger_source": trigger_source,
                    "trigger_kind": "rule_patch",
                    "trigger_rule": rule_name,
                    "reason": reason,
                    "object_type": "rule",
                    "object_id": rule_name,
                    "path": f"rule:{rule_name}",
                    "action": "invalidate",
                    "evidence": evidence,
                }
            )
            for instance_id in item.get("instances", []):
                invalidations.append(
                    {
                        "trigger_source": trigger_source,
                        "trigger_kind": "rule_patch",
                        "trigger_rule": rule_name,
                        "reason": reason,
                        "object_type": "instance",
                        "object_id": instance_id,
                        "path": f"rule:{rule_name}/instance:{instance_id}",
                        "action": "invalidate",
                        "evidence": evidence,
                    }
                )
            for old_cluster in old_clusters:
                if old_cluster["rule_name"] != rule_name:
                    continue
                cluster_id = old_cluster["cluster_id"]
                action = (
                    (
                        "recompute"
                        if cluster_id in new_by_id
                        else "supersede"
                    )
                    if rule_name in aggregation_changed
                    else "reuse_summary_recompute_candidates"
                )
                invalidated_clusters.append(
                    {
                        "cluster_id": cluster_id,
                        "rule_name": rule_name,
                        "action": action,
                        "fields": fields,
                    }
                )
                invalidations.append(
                    {
                        "trigger_source": trigger_source,
                        "trigger_kind": "rule_patch",
                        "trigger_rule": rule_name,
                        "reason": reason,
                        "object_type": "cluster",
                        "object_id": cluster_id,
                        "path": f"rule:{rule_name}/cluster:{cluster_id}",
                        "action": action,
                        "evidence": {
                            **evidence,
                            "cluster_fields": [
                                "representative",
                                "suppressed_count",
                                "occurrence_count",
                            ],
                        },
                    }
                )
                for candidate in old_candidates.get(cluster_id, []):
                    candidate_id = candidate["candidate_id"]
                    candidate_action = (
                        "supersede"
                        if (
                            rule_name in aggregation_changed
                            and cluster_id not in new_by_id
                        )
                        else "recompute"
                    )
                    invalidated_candidates.append(
                        {
                            "cluster_id": cluster_id,
                            "candidate_id": candidate_id,
                            "action": candidate_action,
                            "fields": fields,
                        }
                    )
                    invalidations.append(
                        {
                            "trigger_source": trigger_source,
                            "trigger_kind": "rule_patch",
                            "trigger_rule": rule_name,
                            "reason": reason,
                            "object_type": "root_candidate",
                            "object_id": f"{cluster_id}/{candidate_id}",
                            "path": (
                                f"rule:{rule_name}/cluster:{cluster_id}"
                                f"/candidate:{candidate_id}"
                            ),
                            "action": candidate_action,
                            "evidence": evidence,
                        }
                    )
            for new_cluster in new_clusters:
                if new_cluster["rule_name"] != rule_name:
                    continue
                cluster_id = new_cluster["cluster_id"]
                if cluster_id not in old_by_id:
                    invalidated_clusters.append(
                        {
                            "cluster_id": cluster_id,
                            "rule_name": rule_name,
                            "action": "recompute",
                            "fields": fields,
                        }
                    )
                    invalidations.append(
                        {
                            "trigger_source": trigger_source,
                            "trigger_kind": "rule_patch",
                            "trigger_rule": rule_name,
                            "reason": reason,
                            "object_type": "cluster",
                            "object_id": cluster_id,
                            "path": f"rule:{rule_name}/cluster:{cluster_id}",
                            "action": "recompute",
                            "evidence": evidence,
                        }
                    )
                for candidate in new_candidates.get(cluster_id, []):
                    candidate_id = candidate["candidate_id"]
                    invalidated_candidates.append(
                        {
                            "cluster_id": cluster_id,
                            "candidate_id": candidate_id,
                            "action": "recompute",
                            "fields": fields,
                        }
                    )
                    invalidations.append(
                        {
                            "trigger_source": trigger_source,
                            "trigger_kind": "rule_patch",
                            "trigger_rule": rule_name,
                            "reason": reason,
                            "object_type": "root_candidate",
                            "object_id": f"{cluster_id}/{candidate_id}",
                            "path": (
                                f"rule:{rule_name}/cluster:{cluster_id}"
                                f"/candidate:{candidate_id}"
                            ),
                            "action": "recompute",
                            "evidence": evidence,
                        }
                    )

        for cluster_id in sorted(candidate_changed_clusters):
            changed_signal_rules = candidate_changed_clusters[cluster_id]
            reason = "candidate_evidence_changed"
            evidence = {"signal_rules_changed": changed_signal_rules}
            invalidated_clusters.append(
                {
                    "cluster_id": cluster_id,
                    "rule_name": old_by_id[cluster_id]["rule_name"],
                    "action": "reuse_summary_recompute_candidates",
                    "fields": ["root_candidate_evidence"],
                }
            )
            invalidations.append(
                {
                    "trigger_source": trigger_source,
                    "trigger_kind": "candidate_evidence_closure",
                    "trigger_rule": old_by_id[cluster_id]["rule_name"],
                    "reason": reason,
                    "object_type": "cluster",
                    "object_id": cluster_id,
                    "path": f"cluster:{cluster_id}",
                    "action": "reuse_summary_recompute_candidates",
                    "evidence": evidence,
                }
            )
            for candidate in new_candidates.get(cluster_id, []):
                candidate_id = candidate["candidate_id"]
                invalidated_candidates.append(
                    {
                        "cluster_id": cluster_id,
                        "candidate_id": candidate_id,
                        "action": "recompute",
                        "fields": ["root_candidate_evidence"],
                    }
                )
                invalidations.append(
                    {
                        "trigger_source": trigger_source,
                        "trigger_kind": "candidate_evidence_closure",
                        "trigger_rule": old_by_id[cluster_id]["rule_name"],
                        "reason": reason,
                        "object_type": "root_candidate",
                        "object_id": f"{cluster_id}/{candidate_id}",
                        "path": f"cluster:{cluster_id}/candidate:{candidate_id}",
                        "action": "recompute",
                        "evidence": evidence,
                    }
                )
        unique_clusters: dict[tuple[str, str], dict[str, Any]] = {}
        for item in invalidated_clusters:
            unique_clusters[(item["cluster_id"], item["action"])] = item
        unique_candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in invalidated_candidates:
            unique_candidates[
                (item["cluster_id"], item["candidate_id"], item["action"])
            ] = item
        unique_invalidations: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for item in invalidations:
            unique_invalidations[
                (
                    item["object_type"],
                    item["object_id"],
                    item["path"],
                    item["action"],
                )
            ] = item
        return (
            [unique_invalidations[key] for key in sorted(unique_invalidations)],
            [unique_clusters[key] for key in sorted(unique_clusters)],
            [unique_candidates[key] for key in sorted(unique_candidates)],
        )

    def recover(self) -> dict[str, Any]:
        with self.store.locked():
            return self._recover_locked()

    def _recover_locked(self) -> dict[str, Any]:
        recovered_runs: list[str] = []
        recovered_patches: list[str] = []
        active = self.store.get_active_view()
        if active is None:
            return {"recovered_runs": [], "recovered_patches": []}
        topology = (
            self.store.get_topology()
            if self.store.get_meta_state().get("topology_fingerprint")
            else None
        )
        if topology is None:
            return {"recovered_runs": [], "recovered_patches": []}
        for run in self.store.list_incomplete_ingest_runs():
            if run["view_id"] != active["view_id"]:
                continue
            stream = event_stream_from_document(run["stream_json"], topology)
            normalized_run = dict(run)
            normalized_run["injected_clock_ms"] = stream.injected_clock_ms
            normalized_run["window_ms"] = stream.window_ms
            normalized_run["allowed_lateness_ms"] = stream.allowed_lateness_ms
            self._resume_ingest_run_locked(normalized_run, topology)
            recovered_runs.append(run["run_id"])
        for patch_run in self.store.list_incomplete_patches():
            if patch_run["base_view_id"] != active["view_id"]:
                continue
            self._execute_patch_results_locked(patch_run, topology)
            recovered_patches.append(patch_run["patch_id"])
        return {
            "recovered_runs": sorted(recovered_runs),
            "recovered_patches": sorted(recovered_patches),
        }

    def status(self) -> dict[str, Any]:
        with self.store.locked():
            return self._status_locked()

    def _status_locked(self) -> dict[str, Any]:
        active = self.store.get_active_view()
        meta = self.store.get_meta_state()
        if active is None:
            return {
                "ok": True,
                "schema": "alertlab.status/v1",
                "loaded": False,
                "rule_view": None,
                "rule_version": None,
                "logical_clock_watermark_ms": meta["logical_clock_watermark_ms"],
                "event_watermark_ms": meta["event_watermark_ms"],
                "rules": [],
                "tombstones": [],
                "clusters": [],
                "late_events": [],
                "summary": {
                    "rule_count": 0,
                    "active_rule_count": 0,
                    "cluster_count": 0,
                    "candidate_count": 0,
                },
            }
        view_id = active["view_id"]
        result_revision = self.store.get_view_result_revision(view_id)
        rules = self.store.get_rules(view_id)
        active_rules = [
            record for _, record in sorted(rules.items()) if not record["tombstone"]
        ]
        tombstones = [
            record for _, record in sorted(rules.items()) if record["tombstone"]
        ]
        clusters = self.store.get_clusters(result_revision)
        candidates = self.store.get_candidates(result_revision)
        by_cluster: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            by_cluster.setdefault(candidate["cluster_id"], []).append(candidate)
        cluster_items = [
            {
                "cluster_id": cluster["cluster_id"],
                "service": cluster["service"],
                "rule_name": cluster["rule_name"],
                "rule_source_version": cluster["rule_source_version"],
                "rule_generation": cluster["rule_generation"],
                "rule_effective_at_ms": cluster["rule_effective_at_ms"],
                "rule_silence_labels": cluster["rule_silence_labels"],
                "window_ms": cluster["window_ms"],
                "first_event_ms": cluster["first_event_ms"],
                "last_event_ms": cluster["last_event_ms"],
                "representative": cluster["representative"],
                "representative_event": cluster["representative"],
                "occurrence_count": cluster["occurrence_count"],
                "suppressed_count": cluster["suppressed_count"],
                "silence_suppressed_count": cluster["silence_suppressed_count"],
                "dedup_suppressed_count": cluster["dedup_suppressed_count"],
                "silenced_event_count": cluster["silenced_event_count"],
                "affected_instances": cluster["affected_instances"],
                "event_uids": cluster["event_uids"],
                "events": cluster["events"],
                "root_candidates": sorted(
                    by_cluster.get(cluster["cluster_id"], []),
                    key=lambda candidate: (
                        candidate["rank"],
                        candidate["candidate_id"],
                    ),
                ),
            }
            for cluster in clusters
        ]
        late_events = [
            {
                "event_uid": event["event_uid"],
                "seq": event["seq"],
                "rule_name": event["rule_name"],
                "instance_id": event["instance_id"],
                "logical_clock_ms": event["event_ts_ms"],
                "status": event["status"],
            }
            for event in self.store.get_events()
            if event["late"]
        ]
        late_events.sort(key=lambda event: (event["seq"], event["event_uid"]))
        patches = self.store.get_patches()
        active_patch = next(
            (patch for patch in patches if patch["new_view_id"] == view_id), None
        )
        active_invalidations = (
            self.store.get_invalidations(active_patch["patch_id"])
            if active_patch and active_patch["status"] == "complete"
            else []
        )
        counts = self.store.status_counts(view_id, result_revision)
        return {
            "ok": True,
            "schema": "alertlab.status/v1",
            "loaded": True,
            "rule_view": {
                "view_id": view_id,
                "kind": active["kind"],
                "snapshot_version": active["snapshot_version"],
                "logical_clock_ms": active["logical_clock_ms"],
                "fingerprint": active["fingerprint"],
                "parent_view_id": active["parent_view_id"],
                "patch_id": active["patch_id"],
            },
            "rule_version": view_id,
            "logical_clock_watermark_ms": meta["logical_clock_watermark_ms"],
            "event_watermark_ms": meta["event_watermark_ms"],
            "result_revision": result_revision,
            "rules": active_rules,
            "tombstones": tombstones,
            "clusters": cluster_items,
            "late_events": late_events,
            "last_patch_invalidations": active_invalidations,
            "summary": {
                "rule_count": len(rules),
                "active_rule_count": len(active_rules),
                "tombstone_count": len(tombstones),
                **counts,
            },
        }

    def verify(self) -> dict[str, Any]:
        with self.store.locked():
            return self._verify_locked()

    def _verify_locked(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []

        def check(name: str, ok: bool, **detail: Any) -> None:
            item = {"name": name, "ok": bool(ok)}
            if detail:
                item["detail"] = detail
            checks.append(item)
            if not ok:
                mismatches.append(item)

        active = self.store.get_active_view()
        check("active_view_exists", active is not None)
        if active is None:
            return self._verify_response(checks, mismatches)
        view_id = active["view_id"]
        try:
            result_revision = self.store.get_view_result_revision(view_id)
            result_ok = True
        except AlertLabError:
            result_ok = False
            result_revision = -1
        check("result_pointer_exists", result_ok, result_revision=result_revision)
        if not result_ok:
            return self._verify_response(checks, mismatches)
        rules_raw = self.store.get_rules(view_id)
        rules = {
            name: merged_rule_from_record(record)
            for name, record in rules_raw.items()
        }
        clusters = self.store.get_clusters(result_revision)
        candidates = self.store.get_candidates(result_revision)
        check("rule_count_nonzero", bool(rules), rule_count=len(rules))
        check(
            "no_tombstone_clusters",
            all(not rules[cluster["rule_name"]].tombstone for cluster in clusters),
        )
        cluster_hashes = self.store.get_cluster_hashes(result_revision)
        hashes_ok = True
        for cluster in clusters:
            if cluster_hashes.get(cluster["cluster_id"]) != cluster_hash(cluster):
                hashes_ok = False
                mismatches.append(
                    {
                        "name": "cluster_hash",
                        "cluster_id": cluster["cluster_id"],
                        "ok": False,
                    }
                )
        check("cluster_hashes_match", hashes_ok, checked=len(clusters))
        candidate_hashes = self.store.get_candidate_hashes(result_revision)
        candidate_hash_ok = all(
            candidate_hashes.get(
                f"{candidate['cluster_id']}/{candidate['candidate_id']}"
            )
            == candidate_hash(candidate)
            for candidate in candidates
        )
        check(
            "candidate_hashes_match",
            candidate_hash_ok,
            checked=len(candidates),
        )
        ranks_ok = True
        by_cluster: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            by_cluster.setdefault(candidate["cluster_id"], []).append(candidate)
        for cluster_id, items in by_cluster.items():
            ordered = sorted(items, key=lambda item: item["rank"])
            ranks = [item["rank"] for item in ordered]
            if ranks != list(range(1, len(ordered) + 1)):
                ranks_ok = False
                mismatches.append(
                    {
                        "name": "candidate_rank",
                        "cluster_id": cluster_id,
                        "ok": False,
                    }
                )
        check("candidate_ranks_contiguous", ranks_ok, cluster_count=len(by_cluster))
        topology = self.store.get_topology()
        event_clock = self.store.get_meta_state()["logical_clock_watermark_ms"]
        events = self._decorate_events(
            self.store.get_events(), rules, event_clock
        )
        replay_ok = True
        for cluster in clusters:
            rule = rules.get(cluster["rule_name"])
            if rule is None:
                replay_ok = False
                continue
            event_map = {event["event_uid"]: event for event in events}
            cluster_events_input = [
                event_map[event_uid]
                for event_uid in cluster["event_uids"]
                if event_uid in event_map
            ]
            rebuilt = build_cluster(
                cluster_events_input, rule, topology, window_ms=cluster["window_ms"]
            )
            if rebuilt != cluster:
                replay_ok = False
                mismatches.append(
                    {
                        "name": "cluster_replay",
                        "cluster_id": cluster["cluster_id"],
                        "ok": False,
                    }
                )
            expected_candidates = root_candidates(cluster, rule, topology, events)
            stored = by_cluster.get(cluster["cluster_id"], [])
            if expected_candidates != stored:
                replay_ok = False
                mismatches.append(
                    {
                        "name": "candidate_replay",
                        "cluster_id": cluster["cluster_id"],
                        "ok": False,
                    }
                )
        check("cluster_and_candidate_replay", replay_ok, checked=len(clusters))
        windows = self.store.get_view_windows(view_id)
        full_replay_ok = True
        if len(windows) == 1:
            replayed = cluster_events(
                events,
                rules,
                topology,
                window_ms=windows[0],
                injected_clock_ms=event_clock,
            )
            if [cluster["cluster_id"] for cluster in replayed] != [
                cluster["cluster_id"] for cluster in clusters
            ]:
                full_replay_ok = False
                mismatches.append(
                    {"name": "full_cluster_replay", "ok": False}
                )
        check(
            "full_cluster_replay" if len(windows) == 1 else "full_cluster_replay_skipped",
            full_replay_ok,
            windows=windows,
        )
        state = self.store.get_meta_state()
        check(
            "watermarks_nonnegative",
            state["event_watermark_ms"] >= 0
            and state["logical_clock_watermark_ms"] >= 0,
            **state,
        )
        check(
            "no_incomplete_ingest_runs",
            not self.store.list_incomplete_ingest_runs(),
            count=len(self.store.list_incomplete_ingest_runs()),
        )
        check(
            "no_incomplete_patches",
            not self.store.list_incomplete_patches(),
            count=len(self.store.list_incomplete_patches()),
        )
        return self._verify_response(checks, mismatches)

    @staticmethod
    def _verify_response(
        checks: list[dict[str, Any]], mismatches: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "ok": all(check["ok"] for check in checks),
            "schema": "alertlab.verify/v1",
            "checks": checks,
            "mismatches": mismatches,
        }
