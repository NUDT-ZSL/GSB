"""End-to-end deterministic invalidation workbench."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .checkpoint import (
    CheckpointStore,
    WorkdirLock,
    checkpoint_payload,
)
from .constants import (
    CHECKPOINT_STRUCTURE_VERSION,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    GRAPH_STRUCTURE_VERSION,
    REPORT_STRUCTURE_VERSION,
)
from .diagnostics import Diagnostic, has_errors, sort_diagnostics
from .events import EventState, diagnostic_from_dict
from .graph import DependencyGraph, Propagation
from .parser import (
    EntryDraft,
    EventFileResult,
    PolicyParseResult,
    PolicyUpdate,
    RawEvent,
    apply_update_documents,
    parse_event_file,
    parse_policy_document,
    validate_entry_drafts,
)
from .report import (
    build_terminal_chain,
    render_mermaid,
    sort_chains,
    write_snapshots,
)


@dataclass(frozen=True)
class EventDocument:
    source: str
    raw: str | None = None
    load_error: Diagnostic | None = None


@dataclass
class RunResult:
    report: dict[str, Any]
    graph_data: dict[str, Any]
    dot_text: str
    mermaid_text: str
    diagnostics: list[Diagnostic]
    exit_code: int
    output_paths: dict[str, str] = field(default_factory=dict)


class Workbench:
    def __init__(
        self,
        policy_document: Any,
        *,
        source: str = "policy.json",
        workdir: Path | str = ".",
        output_dir: Path | str | None = None,
        checkpoint_enabled: bool = True,
        structure_version: str = CHECKPOINT_STRUCTURE_VERSION,
        initial_diagnostics: Iterable[Diagnostic] = (),
        checkpoint_every_event: bool = True,
        on_checkpoint: Callable[[dict[str, Any], str], None] | None = None,
    ) -> None:
        self.policy_document = policy_document
        self.source = source
        self.workdir = Path(workdir)
        self.output_dir = Path(output_dir) if output_dir is not None else self.workdir
        self.checkpoint_enabled = checkpoint_enabled
        self.structure_version = structure_version
        self.initial_diagnostics = list(initial_diagnostics)
        self.checkpoint_every_event = checkpoint_every_event
        self.on_checkpoint = on_checkpoint
        self.parse_result = parse_policy_document(policy_document, source=source)

    def run(self, event_documents: Iterable[EventDocument]) -> RunResult:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with WorkdirLock(self.workdir):
            return self._run_locked(list(event_documents))

    def _run_locked(self, event_documents: list[EventDocument]) -> RunResult:
        parse_result = self.parse_result
        updates = list(parse_result.updates)
        current_watermark = [update.checkpoint_record() for update in updates]
        store = CheckpointStore(self.workdir)
        checkpoint_diagnostics: list[Diagnostic] = []
        removed_temp_files: list[str] = []
        event_state_reset = False
        applied_updates: list[PolicyUpdate] = []

        if self.checkpoint_enabled:
            read_result = store.read(
                policy_version=parse_result.policy_version,
                policy_identity=parse_result.policy_identity,
                applied_updates=current_watermark,
                structure_version=self.structure_version,
            )
            checkpoint_status = read_result.status
            checkpoint_diagnostics.extend(read_result.diagnostics)
            removed_temp_files.extend(read_result.removed_temp_files)
            if read_result.status == "resumed":
                applied_updates = updates[: read_result.applied_count]
                event_state = EventState.from_checkpoint(
                    read_result.payload.get("event_state", {})
                )
            else:
                event_state = EventState()
        else:
            checkpoint_status = "disabled"
            event_state = EventState()

        pending_updates = updates[len(applied_updates) :]
        if pending_updates and event_state.processed_keys:
            event_state.reset()
            event_state_reset = True

        for update in pending_updates:
            applied_updates.append(update)
            if self.checkpoint_enabled:
                self._save_checkpoint(
                    store,
                    applied_updates=applied_updates,
                    event_state=event_state,
                    reason="update",
                    diagnostics=checkpoint_diagnostics,
                )

        drafts = apply_update_documents(parse_result.base_drafts, applied_updates)
        entries, validation_diagnostics = validate_entry_drafts(
            drafts,
            default_source=self.source,
        )
        graph = DependencyGraph(entries.values())
        cycle_diagnostics = self._cycle_diagnostics(graph)

        file_results, raw_events = self._parse_event_documents(event_documents)
        cross_file_duplicates = self._cross_file_duplicate_diagnostics(raw_events)
        duplicate_ids = {
            item.stable_key for item in cross_file_duplicates if item.stable_key
        }
        raw_events = [
            event for event in raw_events if event.event_id not in duplicate_ids
        ]
        raw_events.sort(key=lambda event: event.sort_key())

        for event in raw_events:
            if event.event_id and event.event_id in event_state.processed_keys:
                continue
            event_state.process(event, entries)
            if self.checkpoint_enabled and self.checkpoint_every_event:
                self._save_checkpoint(
                    store,
                    applied_updates=applied_updates,
                    event_state=event_state,
                    reason="event",
                    diagnostics=checkpoint_diagnostics,
                )

        effective_policy_version = (
            applied_updates[-1].version
            if applied_updates
            else parse_result.policy_version
        )
        update_history = self._update_history(parse_result, applied_updates)
        cumulative_changed = sorted(
            {
                changed
                for update in applied_updates
                for changed in update.changed_entry_ids
            },
            key=graph.entry_sort_key,
        )
        cumulative = self._affected_for(
            graph,
            event_state,
            cumulative_changed,
        )
        per_update = [
            self._update_report(
                graph,
                event_state,
                update,
                update_history.get(update.update_id, {}),
            )
            for update in applied_updates
        ]
        truth_comparison, truth_diagnostics = self._truth_comparison(
            graph,
            event_state,
            parse_result,
            update_history,
        )

        if self.checkpoint_enabled:
            self._save_checkpoint(
                store,
                applied_updates=applied_updates,
                event_state=event_state,
                reason="final",
                diagnostics=checkpoint_diagnostics,
            )

        diagnostics = sort_diagnostics(
            list(self.initial_diagnostics)
            + list(parse_result.diagnostics)
            + validation_diagnostics
            + cycle_diagnostics
            + [item for result in file_results for item in result.diagnostics]
            + cross_file_duplicates
            + list(event_state.diagnostics)
            + truth_diagnostics
            + checkpoint_diagnostics
        )
        exit_code = EXIT_PARTIAL if has_errors(diagnostics) else EXIT_SUCCESS

        graph_data = graph.to_dict(
            affected_entry_ids=cumulative["affected_entries"],
            changed_entry_ids=cumulative_changed,
        )
        graph_data["structure_version"] = GRAPH_STRUCTURE_VERSION
        graph_data["policy_identity"] = parse_result.policy_identity
        graph_data["cycle_errors"] = [
            {
                "code": item.code,
                "entry_ids": item.details.get("entry_ids", []),
                "entry_indices": item.details.get("entry_indices", []),
                "entry_ordinals": item.details.get("entry_ordinals", []),
            }
            for item in cycle_diagnostics
        ]
        dot_text = graph.to_dot(
            affected_entry_ids=cumulative["affected_entries"],
            changed_entry_ids=cumulative_changed,
        )
        mermaid_text = render_mermaid(
            graph,
            affected_entry_ids=cumulative["affected_entries"],
            changed_entry_ids=cumulative_changed,
        )

        file_reports = self._file_reports(file_results, event_state)
        watermark = (
            applied_updates[-1].stable_key if applied_updates else None
        )
        report: dict[str, Any] = {
            "report_version": REPORT_STRUCTURE_VERSION,
            "status": "success" if exit_code == EXIT_SUCCESS else "partial",
            "exit_code": exit_code,
            "policy": {
                "source": self.source,
                "declared_version": parse_result.policy_version,
                "effective_version": effective_policy_version,
                "identity": parse_result.policy_identity,
                "graph_nodes": len(graph.entries),
                "graph_edges": len(graph.edges),
            },
            "summary": {
                "applied_updates": len(applied_updates),
                "changed_entries": len(cumulative_changed),
                "affected_entries": len(cumulative["affected_entries"]),
                "affected_tokens": len(cumulative["affected_tokens"]),
                "affected_sessions": len(cumulative["affected_sessions"]),
                "active_tokens": len(event_state.active_tokens()),
                "active_sessions": len(event_state.active_sessions()),
                "errors": sum(1 for item in diagnostics if item.is_error),
                "warnings": sum(1 for item in diagnostics if not item.is_error),
                "truth_consistent": sum(
                    1 for item in truth_comparison if item["consistent"]
                ),
                "truth_inconsistent": sum(
                    1 for item in truth_comparison if not item["consistent"]
                ),
            },
            "checkpoint": {
                "status": checkpoint_status,
                "structure_version": self.structure_version,
                "policy_version": parse_result.policy_version,
                "effective_policy_version": effective_policy_version,
                "applied_update_count": len(applied_updates),
                "watermark": watermark,
                "processed_event_count": len(event_state.processed_keys),
                "event_state_reset": event_state_reset,
                "removed_temp_files": sorted(removed_temp_files),
                "diagnostics": [item.to_dict() for item in checkpoint_diagnostics],
            },
            "diagnostics": [item.to_dict() for item in diagnostics],
            "errors": [item.to_dict() for item in diagnostics if item.is_error],
            "rejections": [
                item.to_dict()
                for item in diagnostics
                if item.is_error
                and item.code
                not in {
                    "E_CYCLE",
                    "E_INTERNAL",
                    "E_IO",
                    "E_CHECKPOINT_WRITE",
                }
            ],
            "changed_entry_ids": cumulative_changed,
            "affected_entries": cumulative["affected_entries"],
            "affected_tokens": cumulative["affected_tokens"],
            "affected_sessions": cumulative["affected_sessions"],
            "active_tokens": event_state.active_tokens(),
            "active_sessions": event_state.active_sessions(),
            "propagation_chains": cumulative["chains"],
            "updates": per_update,
            "cycles": [
                {
                    "entry_ids": list(cycle),
                    "entry_indices": [
                        graph.entries[entry_id].entry_index for entry_id in cycle
                    ],
                    "entry_ordinals": [
                        graph.entries[entry_id].entry_index + 1
                        for entry_id in cycle
                    ],
                }
                for cycle in graph.cycles
            ],
            "truth_comparison": truth_comparison,
            "files": file_reports,
            "artifacts": {
                "report": "diagnostics.json",
                "dependency_graph_json": "dependency_graph.json",
                "dependency_graph_dot": "dependency_graph.dot",
                "dependency_graph_mermaid": "dependency_graph.mmd",
            },
        }
        output_paths = write_snapshots(
            self.output_dir,
            report,
            graph_data,
            dot_text,
            mermaid_text,
        )
        return RunResult(
            report=report,
            graph_data=graph_data,
            dot_text=dot_text,
            mermaid_text=mermaid_text,
            diagnostics=diagnostics,
            exit_code=exit_code,
            output_paths=output_paths,
        )

    def _save_checkpoint(
        self,
        store: CheckpointStore,
        *,
        applied_updates: list[PolicyUpdate],
        event_state: EventState,
        reason: str,
        diagnostics: list[Diagnostic],
    ) -> None:
        effective_version = (
            applied_updates[-1].version
            if applied_updates
            else self.parse_result.policy_version
        )
        payload = checkpoint_payload(
            policy_version=self.parse_result.policy_version,
            policy_identity=self.parse_result.policy_identity,
            effective_policy_version=effective_version,
            applied_updates=[
                update.checkpoint_record() for update in applied_updates
            ],
            event_state=event_state.to_checkpoint(),
        )
        try:
            store.save(payload, structure_version=self.structure_version)
        except OSError as exc:
            diagnostics.append(
                Diagnostic(
                    code="E_CHECKPOINT_WRITE",
                    message=f"checkpoint write failed: {exc}",
                    source=str(store.path),
                )
            )
            return
        if self.on_checkpoint is not None:
            self.on_checkpoint(payload, reason)

    def _parse_event_documents(
        self,
        documents: list[EventDocument],
    ) -> tuple[list[EventFileResult], list[RawEvent]]:
        results: list[EventFileResult] = []
        events: list[RawEvent] = []
        for document in documents:
            result = parse_event_file(
                document.source,
                document.raw if document.raw is not None else "",
                load_error=document.load_error,
            )
            results.append(result)
            events.extend(result.events)
        return results, events

    def _cross_file_duplicate_diagnostics(
        self,
        events: list[RawEvent],
    ) -> list[Diagnostic]:
        groups: dict[str, list[RawEvent]] = {}
        for event in events:
            if event.event_id:
                groups.setdefault(event.event_id, []).append(event)
        diagnostics: list[Diagnostic] = []
        for event_id in sorted(groups):
            group = groups[event_id]
            if len(group) < 2:
                continue
            for event in group:
                diagnostics.append(
                    Diagnostic(
                        code="E_EVENT_DUP",
                        message="duplicate event_id across event streams; all copies are rejected",
                        source=event.source,
                        index=event.index,
                        offset=event.offset,
                        stable_key=event_id,
                        details={"event_id": event_id},
                    )
                )
        return sort_diagnostics(diagnostics)

    def _cycle_diagnostics(self, graph: DependencyGraph) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for cycle in graph.cycles:
            indices = [
                graph.entries[entry_id].entry_index for entry_id in cycle
            ]
            diagnostics.append(
                Diagnostic(
                    code="E_CYCLE",
                    message="policy dependency cycle detected",
                    source=self.source,
                    index=min(indices) if indices else None,
                    details={
                        "entry_ids": list(cycle),
                        "entry_indices": indices,
                        "entry_ordinals": [index + 1 for index in indices],
                    },
                )
            )
        return diagnostics

    def _update_history(
        self,
        parse_result: PolicyParseResult,
        applied_updates: list[PolicyUpdate],
    ) -> dict[str, dict[str, str]]:
        effective = parse_result.policy_version
        result: dict[str, dict[str, str]] = {}
        for update in applied_updates:
            result[update.update_id] = {
                "before_version": effective,
                "after_version": update.version,
                "watermark": update.stable_key,
            }
            effective = update.version
        return result

    def _affected_for(
        self,
        graph: DependencyGraph,
        event_state: EventState,
        changed_entry_ids: Iterable[str],
    ) -> dict[str, Any]:
        propagation = graph.propagate(changed_entry_ids)
        affected_set = set(propagation.affected_entries)
        affected_tokens = [
            token
            for token in event_state.active_tokens()
            if affected_set.intersection(token.get("dependencies", []))
        ]
        affected_sessions = [
            session
            for session in event_state.active_sessions()
            if affected_set.intersection(session.get("dependencies", []))
        ]
        chains = []
        for token in affected_tokens:
            path = graph.best_path_for_dependencies(
                propagation,
                token.get("dependencies", []),
            )
            chains.append(
                build_terminal_chain(
                    graph,
                    propagation,
                    path,
                    terminal_type="token",
                    terminal_id=token["token_id"],
                    metadata={"session_id": token["session_id"]},
                )
            )
        for session in affected_sessions:
            path = graph.best_path_for_dependencies(
                propagation,
                session.get("dependencies", []),
            )
            chains.append(
                build_terminal_chain(
                    graph,
                    propagation,
                    path,
                    terminal_type="session",
                    terminal_id=session["session_id"],
                    metadata={"binding_id": session["binding_id"]},
                )
            )
        return {
            "propagation": propagation,
            "affected_entries": list(propagation.affected_entries),
            "affected_tokens": sorted(affected_tokens, key=lambda token: token["token_id"]),
            "affected_sessions": sorted(
                affected_sessions, key=lambda session: session["session_id"]
            ),
            "chains": sort_chains(chains),
        }

    def _update_report(
        self,
        graph: DependencyGraph,
        event_state: EventState,
        update: PolicyUpdate,
        history: dict[str, str],
    ) -> dict[str, Any]:
        affected = self._affected_for(
            graph,
            event_state,
            update.changed_entry_ids,
        )
        return {
            "update_id": update.update_id,
            "watermark": update.stable_key,
            "source_index": update.source_index,
            "ordinal": update.source_index + 1,
            "op": update.op,
            "before_version": history.get("before_version"),
            "after_version": history.get("after_version", update.version),
            "changed_entry_ids": list(update.changed_entry_ids),
            "affected_entries": affected["affected_entries"],
            "affected_tokens": [
                token["token_id"] for token in affected["affected_tokens"]
            ],
            "affected_sessions": [
                session["session_id"] for session in affected["affected_sessions"]
            ],
            "propagation_chains": affected["chains"],
        }

    def _truth_comparison(
        self,
        graph: DependencyGraph,
        event_state: EventState,
        parse_result: PolicyParseResult,
        update_history: dict[str, dict[str, str]],
    ) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
        comparisons: list[dict[str, Any]] = []
        diagnostics: list[Diagnostic] = []
        for marker in parse_result.markers:
            history = update_history.get(marker.update_id)
            if history is None:
                continue
            observed = self._affected_for(
                graph,
                event_state,
                marker.changed_entry_ids,
            )
            observed_tokens = sorted(
                token["token_id"] for token in observed["affected_tokens"]
            )
            observed_sessions = sorted(
                session["session_id"]
                for session in observed["affected_sessions"]
            )
            expected_tokens = sorted(marker.expected_tokens)
            expected_sessions = sorted(marker.expected_sessions)
            versions_match = (
                marker.before_version == history["before_version"]
                and marker.after_version == history["after_version"]
            )
            tokens_match = observed_tokens == expected_tokens
            sessions_match = observed_sessions == expected_sessions
            consistent = versions_match and tokens_match and sessions_match
            entry = {
                "marker_id": marker.marker_id,
                "update_id": marker.update_id,
                "source": marker.source,
                "index": marker.source_index,
                "ordinal": marker.source_index + 1,
                "basis": ["sample.sync_markers", "policy.dependency_graph"],
                "before_version": marker.before_version,
                "after_version": marker.after_version,
                "observed_before_version": history["before_version"],
                "observed_after_version": history["after_version"],
                "changed_entry_ids": list(marker.changed_entry_ids),
                "expected_tokens": expected_tokens,
                "observed_tokens": observed_tokens,
                "expected_sessions": expected_sessions,
                "observed_sessions": observed_sessions,
                "missing_tokens": sorted(set(expected_tokens) - set(observed_tokens)),
                "unexpected_tokens": sorted(
                    set(observed_tokens) - set(expected_tokens)
                ),
                "missing_sessions": sorted(
                    set(expected_sessions) - set(observed_sessions)
                ),
                "unexpected_sessions": sorted(
                    set(observed_sessions) - set(expected_sessions)
                ),
                "versions_match": versions_match,
                "tokens_match": tokens_match,
                "sessions_match": sessions_match,
                "consistent": consistent,
            }
            comparisons.append(entry)
            if not consistent:
                diagnostics.append(
                    Diagnostic(
                        code="E_SYNC",
                        message="sync marker observation is inconsistent with graph-derived invalidation",
                        source=marker.source,
                        index=marker.source_index,
                        stable_key=marker.marker_id,
                        details={
                            "marker_id": marker.marker_id,
                            "update_id": marker.update_id,
                            "versions_match": versions_match,
                            "tokens_match": tokens_match,
                            "sessions_match": sessions_match,
                        },
                    )
                )
        return comparisons, diagnostics

    def _file_reports(
        self,
        file_results: list[EventFileResult],
        event_state: EventState,
    ) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for result in file_results:
            accepted = sum(
                1
                for record in event_state.records
                if record.get("source") == result.source
                and record.get("status") == "accepted"
            )
            rejected = sum(
                1
                for record in event_state.records
                if record.get("source") == result.source
                and record.get("status") == "rejected"
            )
            status = result.status
            if result.diagnostics and status == "ok":
                status = "partial"
            reports.append(
                {
                    "source": result.source,
                    "status": status,
                    "events": len(result.events),
                    "accepted": accepted,
                    "rejected": rejected,
                    "diagnostics": [
                        item.to_dict() for item in result.diagnostics
                    ],
                }
            )
        return reports
