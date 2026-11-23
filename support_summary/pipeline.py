"""DAG execution, stage snapshots, recovery, and run reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .cache import (
    CacheStore,
    chunk_key_components,
    merge_block_key_components,
    merge_final_key_components,
    rewrite_key_components,
    score_key_components,
)
from .canonical import canonical_bytes, sha256_value
from .errors import BusinessError, InjectedCrash
from .models import STAGES, Session, SummaryConfig, Vocabulary
from .nodes import (
    build_chunks,
    build_merge_block,
    finalize_merge_blocks,
    rewrite_unit_entry,
    score_chunks,
    verify_one_shot_equivalence,
)
from .snapshots import SnapshotManager
from .validation import (
    dataset_signature,
    load_config,
    load_sessions,
    load_vocabulary,
    resource_guard,
    validate_config,
    validate_sessions,
    validate_vocabulary_usage,
)


CrashHandler = Callable[[str], None]


@dataclass
class RunResult:
    summaries: Dict[str, Dict[str, Any]]
    events: List[Dict[str, Any]] = field(default_factory=list)
    stage_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    stage_status: Dict[str, str] = field(default_factory=dict)
    snapshot_versions: Dict[str, int] = field(default_factory=dict)
    resumed_stages: List[str] = field(default_factory=list)
    recomputed_stages: List[str] = field(default_factory=list)
    degradations: List[Dict[str, Any]] = field(default_factory=list)
    cache_stats: Dict[str, int] = field(default_factory=dict)
    run_signature: str = ""
    orphan_temp_files: List[str] = field(default_factory=list)

    @property
    def summary_bytes(self) -> bytes:
        return canonical_bytes(
            {
                session_id: self.summaries[session_id]
                for session_id in sorted(self.summaries)
            }
        )

    @property
    def summary_digest(self) -> str:
        return sha256_value(self.summaries)

    def node_sets(self) -> Tuple[Set[str], Set[str]]:
        recomputed = {
            str(event["node_id"])
            for event in self.events
            if event["status"] == "recomputed"
        }
        reused = {
            str(event["node_id"])
            for event in self.events
            if event["status"] == "reused"
        }
        return recomputed, reused

    def to_report(self) -> Dict[str, Any]:
        recomputed_nodes, reused_nodes = self.node_sets()
        recomputed_sessions = sorted(
            {
                str(event["session_id"])
                for event in self.events
                if event["status"] == "recomputed"
            }
        )
        all_sessions_with_events = {
            str(event["session_id"]) for event in self.events
        }
        reused_sessions = sorted(all_sessions_with_events - set(recomputed_sessions))
        return {
            "schema_version": 1,
            "ok": True,
            "run_signature": self.run_signature,
            "summary_digest": self.summary_digest,
            "summaries": {
                session_id: self.summaries[session_id]
                for session_id in sorted(self.summaries)
            },
            "degradations": self.degradations,
            "cache_stats": self.cache_stats,
            "events": self.events,
            "recomputed_nodes": sorted(recomputed_nodes),
            "reused_nodes": sorted(reused_nodes),
            "recomputed_sessions": recomputed_sessions,
            "reused_sessions": reused_sessions,
            "stages": {
                stage: {
                    "status": self.stage_status.get(stage, "not_run"),
                    "sessions": sorted(self.stage_results.get(stage, {}).keys()),
                    "snapshot_version": self.snapshot_versions.get(stage),
                }
                for stage in STAGES
            },
            "resumed_stages": self.resumed_stages,
            "recomputed_stages": self.recomputed_stages,
            "orphan_temp_files_removed": self.orphan_temp_files,
        }


class PipelineRunner:
    def __init__(
        self,
        sessions: Sequence[Session],
        vocabulary: Vocabulary,
        config: SummaryConfig,
        *,
        cache_dir: Path,
    ) -> None:
        self.sessions = sorted(sessions, key=lambda item: item.session_id)
        self.vocabulary = vocabulary
        self.config = config
        self.cache = CacheStore(cache_dir)
        self.session_by_id = {
            session.session_id: session for session in self.sessions
        }

    def validate(self) -> List[Dict[str, Any]]:
        validate_sessions(self.sessions)
        validate_config(self.config)
        validate_vocabulary_usage(self.sessions, self.vocabulary)
        return resource_guard(self.sessions, self.vocabulary, self.config)

    def run(
        self,
        *,
        checkpoint_dir: Optional[Path] = None,
        resume: bool = True,
        crash_at: Optional[str] = None,
        crash_handler: Optional[CrashHandler] = None,
        bypass_stages: Sequence[str] = (),
    ) -> RunResult:
        degradations = self.validate()
        signature = dataset_signature(
            self.sessions, self.vocabulary, self.config
        )
        session_ids = [session.session_id for session in self.sessions]
        manager: Optional[SnapshotManager] = None
        loaded: Dict[str, Dict[str, Any]] = {}
        orphan_temp_files: List[str] = []
        if checkpoint_dir is not None:
            manager = SnapshotManager(checkpoint_dir, session_ids, signature)
            orphan_temp_files = manager.remove_orphan_temp_files()
            if resume:
                loaded = manager.load()
            elif any(manager.root.glob("[0-9][0-9]_*.json")):
                manager.reset()

        bypass = set(bypass_stages)
        if loaded:
            last_loaded = max(STAGES.index(stage) for stage in loaded)
            bypass.update(STAGES[last_loaded + 1 :])

        result = RunResult(
            summaries={},
            degradations=degradations,
            run_signature=signature,
            orphan_temp_files=orphan_temp_files,
        )
        stage_data: Dict[str, Dict[str, Any]] = {}

        for stage in STAGES:
            if stage in loaded:
                envelope = loaded[stage]
                stage_data[stage] = dict(envelope["data"])
                result.stage_results[stage] = stage_data[stage]
                result.stage_status[stage] = "resumed_committed"
                result.snapshot_versions[stage] = int(envelope["version"])
                result.resumed_stages.append(stage)
                continue

            if stage == "chunk_tokenize":
                data = self._run_chunk_stage(bypass=stage in bypass)
            elif stage == "unit_score":
                data = self._run_score_stage(
                    stage_data["chunk_tokenize"], bypass=stage in bypass
                )
            elif stage == "rewrite":
                data = self._run_rewrite_stage(
                    stage_data["unit_score"],
                    bypass=stage in bypass,
                    crash_at=crash_at,
                    crash_handler=crash_handler,
                )
            elif stage == "summary_merge":
                data = self._run_merge_stage(
                    stage_data["chunk_tokenize"],
                    stage_data["rewrite"],
                    bypass=stage in bypass,
                )
            else:
                raise AssertionError(f"unknown stage {stage}")

            stage_data[stage] = data
            result.stage_results[stage] = data
            result.stage_status[stage] = "recomputed"
            result.recomputed_stages.append(stage)
            if crash_at == f"{stage}:before_commit":
                self._inject_crash(f"{stage}:before_commit", crash_handler)
            if manager is not None:
                envelope = manager.commit(
                    stage,
                    data,
                    session_ids,
                )
                result.snapshot_versions[stage] = int(envelope["version"])

        result.summaries = {
            session_id: data["final"]["payload"]
            for session_id, data in stage_data["summary_merge"].items()
        }
        result.events = list(self.cache.events)
        result.cache_stats = dict(self.cache.stats)
        return result

    @staticmethod
    def _inject_crash(
        point: str, crash_handler: Optional[CrashHandler]
    ) -> None:
        if crash_handler is not None:
            crash_handler(point)
            raise InjectedCrash(f"injected crash at {point}")
        raise InjectedCrash(f"injected crash at {point}")

    def _run_chunk_stage(self, *, bypass: bool) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for session in self.sessions:
            chunks = build_chunks(session, self.vocabulary, self.config)
            artifacts = []
            for chunk in chunks:
                components = chunk_key_components(
                    session, self.vocabulary, self.config, chunk
                )
                artifact, _ = self.cache.get_or_compute(
                    "chunk_tokenize",
                    session.session_id,
                    components,
                    lambda chunk=chunk: chunk,
                    node_id=(
                        f"{session.session_id}/chunk_tokenize/"
                        f"block:{int(chunk['block_index'])}"
                    ),
                    bypass=bypass,
                )
                artifacts.append(artifact)
            data[session.session_id] = artifacts
        return data

    def _run_score_stage(
        self, chunk_stage: Mapping[str, Any], *, bypass: bool
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for session in self.sessions:
            scored = []
            for chunk_artifact in chunk_stage[session.session_id]:
                payload = score_chunks(
                    session,
                    [chunk_artifact["payload"]],
                    self.vocabulary,
                    self.config,
                )[0]
                components = score_key_components(
                    session,
                    self.vocabulary,
                    self.config,
                    chunk_artifact,
                    payload,
                )
                artifact, _ = self.cache.get_or_compute(
                    "unit_score",
                    session.session_id,
                    components,
                    lambda payload=payload: payload,
                    node_id=(
                        f"{session.session_id}/unit_score/"
                        f"block:{int(payload['block_index'])}"
                    ),
                    bypass=bypass,
                )
                scored.append(artifact)
            data[session.session_id] = scored
        return data

    def _run_rewrite_stage(
        self,
        score_stage: Mapping[str, Any],
        *,
        bypass: bool,
        crash_at: Optional[str],
        crash_handler: Optional[CrashHandler],
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for session_index, session in enumerate(self.sessions):
            blocks = []
            for score_artifact in score_stage[session.session_id]:
                score_payload = score_artifact["payload"]
                rewrite_artifacts = []
                for entry in score_payload["unit_scores"]:
                    if not entry.get("selected", True):
                        continue
                    placeholder = {
                        "unit_id": entry["unit_id"],
                        "score_content_hash": sha256_value(dict(entry)),
                        "constraints_hash": sha256_value(
                            session.constraints.to_dict()
                        ),
                    }
                    components = rewrite_key_components(
                        session,
                        self.vocabulary,
                        self.config,
                        score_artifact,
                        placeholder,
                    )
                    artifact, _ = self.cache.get_or_compute(
                        "rewrite",
                        session.session_id,
                        components,
                        lambda entry=entry, score_payload=score_payload: (
                            rewrite_unit_entry(
                                session,
                                score_payload,
                                entry,
                                self.vocabulary,
                                self.config,
                            )
                        ),
                        node_id=(
                            f"{session.session_id}/rewrite/block:"
                            f"{int(score_payload['block_index'])}/unit:{entry['unit_id']}"
                        ),
                        bypass=bypass,
                    )
                    rewrite_artifacts.append(artifact)
                rewrite_artifacts.sort(
                    key=lambda artifact: (
                        int(artifact["payload"]["turn_index"]),
                        int(artifact["payload"]["unit_index"]),
                        str(artifact["payload"]["unit_id"]),
                    )
                )
                blocks.append(
                    {
                        "block_index": int(score_payload["block_index"]),
                        "start_turn_index": int(score_payload["start_turn_index"]),
                        "end_turn_index": int(score_payload["end_turn_index"]),
                        "rewrites": rewrite_artifacts,
                    }
                )
            data[session.session_id] = blocks
            if crash_at == "rewrite:partial" and session_index == 0:
                self._inject_crash("rewrite:partial", crash_handler)
        return data

    def _run_merge_stage(
        self,
        chunk_stage: Mapping[str, Any],
        rewrite_stage: Mapping[str, Any],
        *,
        bypass: bool,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for session in self.sessions:
            chunk_artifacts = sorted(
                chunk_stage[session.session_id],
                key=lambda artifact: int(artifact["payload"]["block_index"]),
            )
            rewrite_blocks = sorted(
                rewrite_stage[session.session_id],
                key=lambda block: int(block["block_index"]),
            )
            rewrites_by_block = {
                int(block["block_index"]): block for block in rewrite_blocks
            }
            block_artifacts = []
            block_payloads = []
            for chunk_artifact in chunk_artifacts:
                chunk = chunk_artifact["payload"]
                block_index = int(chunk["block_index"])
                rewrite_block = rewrites_by_block.get(
                    block_index, {"rewrites": []}
                )
                rewrite_artifacts = rewrite_block["rewrites"]
                payload = build_merge_block(
                    session.session_id,
                    block_index,
                    [artifact["payload"] for artifact in rewrite_artifacts],
                    int(chunk["token_count"]),
                    [str(row["word"]) for row in chunk["frequencies"]],
                    self.config.precision,
                    int(chunk["start_turn_index"]),
                    int(chunk["end_turn_index"]),
                )
                rewrite_hashes = [
                    str(artifact["content_hash"]) for artifact in rewrite_artifacts
                ]
                components = merge_block_key_components(
                    session,
                    self.config,
                    payload,
                    rewrite_hashes,
                    self.vocabulary,
                )
                artifact, _ = self.cache.get_or_compute(
                    "summary_merge",
                    session.session_id,
                    components,
                    lambda payload=payload: payload,
                    node_id=(
                        f"{session.session_id}/summary_merge/"
                        f"block:{block_index}"
                    ),
                    bypass=bypass,
                )
                block_artifacts.append(artifact)
                block_payloads.append(artifact["payload"])

            block_hashes = [
                str(artifact["content_hash"]) for artifact in block_artifacts
            ]
            final_components = merge_final_key_components(
                session, self.config, block_hashes, self.vocabulary
            )
            final_artifact, _ = self.cache.get_or_compute(
                "summary_merge",
                session.session_id,
                final_components,
                lambda: finalize_merge_blocks(
                    session.session_id,
                    block_payloads,
                    self.config.precision,
                ),
                node_id=f"{session.session_id}/summary_merge/final",
                bypass=bypass,
            )
            verify_one_shot_equivalence(
                session,
                block_payloads,
                [
                    {
                        "block_index": block["block_index"],
                        "rewrites": [
                            artifact["payload"]
                            for artifact in block["rewrites"]
                        ],
                    }
                    for block in rewrite_blocks
                ],
                self.config.precision,
            )
            data[session.session_id] = {
                "blocks": block_artifacts,
                "final": final_artifact,
            }
        return data


def load_runner(
    sessions_path: Path,
    vocabulary_path: Path,
    config_path: Path,
    cache_dir: Path,
) -> PipelineRunner:
    sessions = load_sessions(sessions_path)
    vocabulary = load_vocabulary(vocabulary_path)
    config = load_config(config_path)
    return PipelineRunner(
        sessions,
        vocabulary,
        config,
        cache_dir=cache_dir,
    )


def run_pipeline(
    sessions_path: Path,
    vocabulary_path: Path,
    config_path: Path,
    *,
    cache_dir: Path,
    checkpoint_dir: Optional[Path] = None,
    resume: bool = True,
    crash_at: Optional[str] = None,
    crash_handler: Optional[CrashHandler] = None,
) -> RunResult:
    runner = load_runner(
        sessions_path, vocabulary_path, config_path, cache_dir
    )
    return runner.run(
        checkpoint_dir=checkpoint_dir,
        resume=resume,
        crash_at=crash_at,
        crash_handler=crash_handler,
    )
