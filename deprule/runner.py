"""Checkpointed DAG runner and invalidation ledger construction."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .common import atomic_write_json, canonical_hash
from .errors import Codes, DomainError
from .graph import (
    NODE_ORDER,
    STAGE_NODES,
    ArtifactStore,
    node_cache_key,
    plan_invalidation,
)
from .parser import (
    build_export,
    crosscheck_and_reroot,
    normalize_sentence,
    parse_arcs,
    preanalyze,
    select_root,
)
from .rules import PARSER_CONFIG_VERSION


def linguistic_fingerprint(export_obj: dict) -> str:
    """Hash parse semantics without rule-version or cache-key metadata."""

    return canonical_hash({
        "syntax_tree_set": export_obj.get("syntax_tree_set", []),
        "dependency_arcs": sorted(
            (
                arc["arc_id"],
                arc["head"],
                arc["dependent"],
                arc["label"],
                arc["direction"],
            )
            for arc in export_obj.get("dependency_arcs", [])
        ),
        "parse_labels": sorted(
            (row["arc_id"], row["token_index"], row["label"])
            for row in export_obj.get("parse_labels", [])
        ),
        "roots": sorted(
            (root["index"], root["label"]) for root in export_obj.get("roots", [])
        ),
    })


class PipelineRunner:
    def __init__(
        self,
        tasks: list[dict],
        snapshot: dict,
        state_dir: str | Path,
        *,
        previous_ledger: dict | None = None,
        old_snapshot: dict | None = None,
        resume: bool = True,
        fresh: bool = False,
        crash_mid_stage: int | None = None,
        crash_after_stage: int | None = None,
    ):
        self.tasks = tasks
        self.snapshot = snapshot
        self.ir = snapshot["ir"]
        self.state_dir = Path(state_dir)
        self.tasks_hash = canonical_hash(tasks)
        self.pipeline_id = canonical_hash({
            "tasks_hash": self.tasks_hash,
            "rule_snapshot_hash": snapshot["source_hash"],
            "parser_config": PARSER_CONFIG_VERSION,
        })[:16]
        self.checkpoint_dir = self.state_dir / "checkpoints" / self.pipeline_id
        self.work_dir = self.state_dir / "work" / self.pipeline_id
        self.store = ArtifactStore(self.state_dir / "cache")
        self.previous_ledger = previous_ledger
        self.previous_rows = {
            row["sentence_id"]: row for row in (previous_ledger or {}).get("sentences", [])
        }
        self.impact = plan_invalidation(tasks, old_snapshot, snapshot)
        self.resume = resume
        self.crash_mid_stage = crash_mid_stage
        self.crash_after_stage = crash_after_stage
        self.states: dict[str, dict[str, Any]] = {}

        if fresh:
            shutil.rmtree(self.checkpoint_dir, ignore_errors=True)
            shutil.rmtree(self.work_dir, ignore_errors=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def _token_hash(self, task: dict) -> str:
        return canonical_hash({"token_list": task["token_list"]})

    def _previous_ref(self, sid: str, node: str) -> dict | None:
        row = self.previous_rows.get(sid)
        if not row:
            return None
        return (row.get("nodes") or {}).get(node)

    def _current_entry(self, cache_key: str) -> dict | None:
        entry = self.store.get_entry(cache_key)
        if not entry:
            return None
        digest = entry.get("content_hash")
        try:
            obj = self.store.get_blob(entry.get("node", "unknown"), digest)
        except DomainError:
            return None
        entry["_object"] = obj
        return entry

    def _try_reuse(
        self,
        *,
        sid: str,
        node: str,
        cache_key: str,
        affected: bool,
        pending_blobs: list[tuple[str, dict]],
        pending_entries: list[dict],
    ) -> tuple[dict, str, str, str, str | None] | None:
        entry = self._current_entry(cache_key)
        if entry is not None:
            return entry["_object"], entry["content_hash"], cache_key, "reused", "cache_hit"

        previous = self._previous_ref(sid, node)
        if not affected and previous and previous.get("content_hash"):
            try:
                obj = self.store.get_blob(node, previous["content_hash"])
            except DomainError:
                obj = None
            if obj is not None:
                digest = previous["content_hash"]
                pending_entries.append({
                    "cache_key": cache_key,
                    "node": node,
                    "content_hash": digest,
                    "rekeyed_from": previous.get("cache_key"),
                    "rule_snapshot_hash": self.snapshot["source_hash"],
                })
                return obj, digest, cache_key, "reused", "semantic_rekey"
        return None

    def _remember(
        self,
        sid: str,
        node: str,
        obj: dict,
        digest: str,
        cache_key: str,
        status: str,
        detail: str | None,
    ) -> None:
        state = self.states.setdefault(sid, {"objects": {}, "hashes": {}, "keys": {}, "nodes": {}})
        state["objects"][node] = obj
        state["hashes"][node] = digest
        state["keys"][node] = cache_key
        state["nodes"][node] = {
            "node": node,
            "cache_key": cache_key,
            "content_hash": digest,
            "status": status,
            "detail": detail,
        }

    def _stage_path(self, stage: int) -> Path:
        return self.checkpoint_dir / f"stage-{stage:02d}.json"

    def _load_checkpoint(self, stage: int) -> dict | None:
        path = self._stage_path(stage)
        if not path.exists():
            return None
        import json

        doc = json.loads(path.read_text(encoding="utf-8"))
        checksum = doc.pop("content_checksum", None)
        actual = canonical_hash(doc)
        doc["content_checksum"] = checksum
        if checksum != actual:
            raise DomainError(
                "stage checkpoint checksum mismatch",
                code=Codes.E_STALE_RESULT,
                stage=stage,
                expected=checksum,
                actual=actual,
            )
        return doc

    def _prepare_recovery(self) -> int:
        for tmp in self.checkpoint_dir.glob("*.tmp"):
            tmp.unlink(missing_ok=True)
        committed = 0
        for stage in range(1, 5):
            doc = self._load_checkpoint(stage)
            if doc and doc.get("stage") == stage:
                committed = stage
            else:
                break
        for stage in range(committed + 1, 5):
            shutil.rmtree(self.work_dir / f"stage_{stage}", ignore_errors=True)
        return committed

    def _recover_stage(self, stage: int) -> None:
        doc = self._load_checkpoint(stage)
        if not doc:
            raise DomainError(f"missing committed checkpoint for stage {stage}", code=Codes.E_INTERNAL)
        task_by_id = {task["sentence_id"]: task for task in self.tasks}
        for sid in doc["completed_sentences"]:
            refs = doc["refs"][sid]
            for node in STAGE_NODES[stage]:
                ref = refs[node]
                obj = self.store.get_blob(node, ref["content_hash"])
                self._remember(
                    sid,
                    node,
                    obj,
                    ref["content_hash"],
                    ref["cache_key"],
                    "recovered",
                    "checkpoint_committed",
                )
                if node == "normalize":
                    self.states[sid]["task"] = task_by_id[sid]

    def _write_checkpoint(self, stage: int, pending_entries: list[dict]) -> None:
        for kind, obj in self._pending_blobs:
            self.store.put_blob(kind, obj)

        refs = {}
        completed = []
        for task in self.tasks:
            sid = task["sentence_id"]
            completed.append(sid)
            refs[sid] = {
                node: {
                    "cache_key": self.states[sid]["keys"][node],
                    "content_hash": self.states[sid]["hashes"][node],
                }
                for node in STAGE_NODES[stage]
            }
        body = {
            "schema": "deprule-stage-checkpoint-v1",
            "pipeline_id": self.pipeline_id,
            "stage": stage,
            "stage_name": {
                1: "normalize_validate",
                2: "preanalysis",
                3: "parse_arcs_labels",
                4: "root_regression_crosscheck",
            }[stage],
            "version": stage,
            "monotonic_version": stage,
            "tasks_hash": self.tasks_hash,
            "rule_snapshot_hash": self.snapshot["source_hash"],
            "completed_sentences": completed,
            "sentence_count": len(completed),
            "refs": refs,
        }
        body["content_checksum"] = canonical_hash(body)
        atomic_write_json(self._stage_path(stage), body)
        for entry in pending_entries:
            self.store.put_entry(entry["cache_key"], entry)

    def _affected(self, sid: str, node: str) -> bool:
        return node in self.impact["sentence_nodes"].get(sid, [])

    def execute_stage(self, stage: int) -> None:
        self._pending_blobs: list[tuple[str, dict]] = []
        pending_entries: list[dict] = []
        stage_work = self.work_dir / f"stage_{stage}"
        stage_work.mkdir(parents=True, exist_ok=True)

        for index, task in enumerate(self.tasks):
            sid = task["sentence_id"]
            self.states.setdefault(sid, {"objects": {}, "hashes": {}, "keys": {}, "nodes": {}})
            partial = stage_work / f"{sid}.partial.json"
            atomic_write_json(partial, {
                "stage": stage,
                "sentence_id": sid,
                "status": "intermediate",
            })

            if stage == 1:
                self._run_normalize(task, pending_entries)
            elif stage == 2:
                self._run_preanalysis(task, pending_entries)
            elif stage == 3:
                self._run_arcs_labels(task, pending_entries)
            elif stage == 4:
                self._run_root_checksums(task, pending_entries)

            atomic_write_json(partial, {
                "stage": stage,
                "sentence_id": sid,
                "status": "computed_uncommitted",
            })
            if self.crash_mid_stage == stage and index == 0:
                os._exit(43)

        self._write_checkpoint(stage, pending_entries)

    def _add_computed(
        self,
        task: dict,
        node: str,
        obj: dict,
        upstream: dict[str, str],
        pending_entries: list[dict],
    ) -> tuple[str, str]:
        sid = task["sentence_id"]
        token_hash = self._token_hash(task)
        cache_key = node_cache_key(node, token_hash, self.ir, upstream)
        digest = canonical_hash(obj)
        self._pending_blobs.append((node, obj))
        pending_entries.append({
            "cache_key": cache_key,
            "node": node,
            "content_hash": digest,
            "token_sequence_hash": token_hash,
            "rule_snapshot_hash": self.snapshot["source_hash"],
            "tag_set_version": self.ir["tag_set_version"],
            "root_policy_version": self.ir["root_policy_version"],
            "upstream": upstream,
        })
        self._remember(sid, node, obj, digest, cache_key, "recomputed", "computed")
        return digest, cache_key

    def _run_normalize(self, task: dict, pending_entries: list[dict]) -> None:
        sid = task["sentence_id"]
        token_hash = self._token_hash(task)
        cache_key = node_cache_key("normalize", token_hash, self.ir, {})
        reused = self._try_reuse(
            sid=sid, node="normalize", cache_key=cache_key,
            affected=self._affected(sid, "normalize"),
            pending_blobs=self._pending_blobs, pending_entries=pending_entries,
        )
        if reused:
            obj, digest, key, status, detail = reused
            self._remember(sid, "normalize", obj, digest, key, status, detail)
        else:
            obj = normalize_sentence(task)
            self._add_computed(task, "normalize", obj, {}, pending_entries)

    def _run_preanalysis(self, task: dict, pending_entries: list[dict]) -> None:
        sid = task["sentence_id"]
        state = self.states[sid]
        norm = state["objects"]["normalize"]
        upstream = {"normalize": state["hashes"]["normalize"]}
        token_hash = norm["token_sequence_hash"]
        cache_key = node_cache_key("preanalysis", token_hash, self.ir, upstream)
        reused = self._try_reuse(
            sid=sid, node="preanalysis", cache_key=cache_key,
            affected=self._affected(sid, "preanalysis"),
            pending_blobs=self._pending_blobs, pending_entries=pending_entries,
        )
        if reused:
            obj, digest, key, status, detail = reused
            self._remember(sid, "preanalysis", obj, digest, key, status, detail)
            return
        obj = preanalyze(norm, self.ir)
        self._add_computed(task, "preanalysis", obj, upstream, pending_entries)

    def _run_arcs_labels(self, task: dict, pending_entries: list[dict]) -> None:
        sid = task["sentence_id"]
        state = self.states[sid]
        norm = state["objects"]["normalize"]
        pre = state["objects"]["preanalysis"]
        token_hash = norm["token_sequence_hash"]

        arcs_upstream = {
            "normalize": state["hashes"]["normalize"],
            "preanalysis": state["hashes"]["preanalysis"],
        }
        arcs_key = node_cache_key("arcs", token_hash, self.ir, arcs_upstream)
        arcs_reuse = self._try_reuse(
            sid=sid, node="arcs", cache_key=arcs_key,
            affected=self._affected(sid, "arcs"),
            pending_blobs=self._pending_blobs, pending_entries=pending_entries,
        )
        pair = None
        if arcs_reuse:
            arcs_obj, arcs_hash, key, status, detail = arcs_reuse
            self._remember(sid, "arcs", arcs_obj, arcs_hash, key, status, detail)
        else:
            pair = parse_arcs(norm, pre, self.ir)
            arcs_obj = pair[0]
            arcs_hash, key = self._add_computed(task, "arcs", arcs_obj, arcs_upstream, pending_entries)

        labels_upstream = dict(arcs_upstream)
        labels_upstream["arcs"] = arcs_hash
        labels_key = node_cache_key("labels", token_hash, self.ir, labels_upstream)
        labels_reuse = self._try_reuse(
            sid=sid, node="labels", cache_key=labels_key,
            affected=self._affected(sid, "labels"),
            pending_blobs=self._pending_blobs, pending_entries=pending_entries,
        )
        if labels_reuse:
            labels_obj, labels_hash, key, status, detail = labels_reuse
            self._remember(sid, "labels", labels_obj, labels_hash, key, status, detail)
            return
        if pair is None:
            pair = parse_arcs(norm, pre, self.ir)
        self._add_computed(task, "labels", pair[1], labels_upstream, pending_entries)

    def _run_root_checksums(self, task: dict, pending_entries: list[dict]) -> None:
        sid = task["sentence_id"]
        state = self.states[sid]
        norm = state["objects"]["normalize"]
        pre = state["objects"]["preanalysis"]
        arcs_doc = state["objects"]["arcs"]
        labels_doc = state["objects"]["labels"]
        token_hash = norm["token_sequence_hash"]

        root_upstream = {
            "normalize": state["hashes"]["normalize"],
            "preanalysis": state["hashes"]["preanalysis"],
            "arcs": state["hashes"]["arcs"],
            "labels": state["hashes"]["labels"],
        }
        root_key = node_cache_key("root_crosscheck", token_hash, self.ir, root_upstream)
        reused = self._try_reuse(
            sid=sid, node="root_crosscheck", cache_key=root_key,
            affected=self._affected(sid, "root_crosscheck"),
            pending_blobs=self._pending_blobs, pending_entries=pending_entries,
        )
        if reused:
            root_obj, root_hash, key, status, detail = reused
            self._remember(sid, "root_crosscheck", root_obj, root_hash, key, status, detail)
        else:
            root_info = select_root(norm, pre, self.ir)
            root_obj = crosscheck_and_reroot(arcs_doc, root_info, self.ir)
            self._add_computed(task, "root_crosscheck", root_obj, root_upstream, pending_entries)

        checksum_upstream = dict(root_upstream)
        checksum_upstream["root_crosscheck"] = state["hashes"]["root_crosscheck"]
        checksum_obj = {
            "sentence_id": sid,
            "stage_checksums": {
                "stage_1_normalize_validate": state["hashes"]["normalize"],
                "stage_2_preanalysis": state["hashes"]["preanalysis"],
                "stage_3_parse_arcs_labels": canonical_hash({
                    "arcs": state["hashes"]["arcs"],
                    "labels": state["hashes"]["labels"],
                }),
                "stage_4_root_regression_crosscheck": state["hashes"]["root_crosscheck"],
            },
            "artifact_hashes": {
                node: state["hashes"][node]
                for node in ("normalize", "preanalysis", "arcs", "labels", "root_crosscheck")
            },
        }
        checksum_key = node_cache_key("checksums", token_hash, self.ir, checksum_upstream)
        checksum_reuse = self._try_reuse(
            sid=sid, node="checksums", cache_key=checksum_key,
            affected=self._affected(sid, "checksums"),
            pending_blobs=self._pending_blobs, pending_entries=pending_entries,
        )
        if checksum_reuse:
            obj, digest, key, status, detail = checksum_reuse
            self._remember(sid, "checksums", obj, digest, key, status, detail)
        else:
            self._add_computed(task, "checksums", checksum_obj, checksum_upstream, pending_entries)

    def _run_export(self, task: dict) -> dict:
        sid = task["sentence_id"]
        state = self.states[sid]
        norm = state["objects"]["normalize"]
        upstream = {node: state["hashes"][node] for node in NODE_ORDER if node in state["hashes"] and node != "export"}
        token_hash = norm["token_sequence_hash"]
        export_key = node_cache_key("export", token_hash, self.ir, upstream)
        reused = self._try_reuse(
            sid=sid,
            node="export",
            cache_key=export_key,
            affected=self._affected(sid, "export"),
            pending_blobs=[],
            pending_entries=[],
        )
        if reused:
            export_obj, digest, key, status, detail = reused
            self._remember(sid, "export", export_obj, digest, key, status, detail)
            return export_obj

        keys = dict(state["keys"])
        keys["export"] = export_key
        export_obj = build_export(
            task,
            state["objects"]["preanalysis"],
            state["objects"]["labels"],
            state["objects"]["root_crosscheck"],
            state["hashes"],
            keys,
            self.snapshot,
            self.ir,
        )
        digest = self.store.put_blob("export", export_obj)
        self.store.put_entry(export_key, {
            "cache_key": export_key,
            "node": "export",
            "content_hash": digest,
            "upstream": upstream,
        })
        self._remember(sid, "export", export_obj, digest, export_key, "recomputed", "computed")
        return export_obj

    def run(self) -> tuple[dict, dict]:
        committed_stage = self._prepare_recovery() if self.resume else 0
        for stage in range(1, 5):
            if stage <= committed_stage:
                self._recover_stage(stage)
            else:
                self.execute_stage(stage)
            if self.crash_after_stage == stage:
                os._exit(42)

        exports = []
        ledger_sentences = []
        recomputed_instances: list[str] = []
        reused_instances: list[str] = []
        recovered_instances: list[str] = []
        linguistic_changed_ids: list[str] = []

        for task in self.tasks:
            sid = task["sentence_id"]
            export_obj = self._run_export(task)
            exports.append(export_obj)

            linguistic_changed: bool | None = None
            previous_export = self._previous_ref(sid, "export")
            if previous_export and previous_export.get("content_hash"):
                try:
                    old_export = self.store.get_blob("export", previous_export["content_hash"])
                    linguistic_changed = (
                        linguistic_fingerprint(old_export) != linguistic_fingerprint(export_obj)
                    )
                    if linguistic_changed:
                        linguistic_changed_ids.append(sid)
                except DomainError:
                    linguistic_changed = None

            node_refs = {}
            row_recomputed = []
            row_reused = []
            row_recovered = []
            for node in NODE_ORDER:
                if node not in self.states[sid]["nodes"]:
                    continue
                ref = dict(self.states[sid]["nodes"][node])
                node_refs[node] = ref
                qualified = f"{sid}:{node}"
                if ref["status"] == "recomputed":
                    row_recomputed.append(node)
                    recomputed_instances.append(qualified)
                elif ref["status"] == "recovered":
                    row_recovered.append(node)
                    recovered_instances.append(qualified)
                else:
                    row_reused.append(node)
                    reused_instances.append(qualified)
            ledger_sentences.append({
                "sentence_id": sid,
                "affected_nodes": self.impact["sentence_nodes"].get(sid, []),
                "linguistic_result_changed": linguistic_changed,
                "recomputed_nodes": row_recomputed,
                "reused_nodes": row_reused,
                "recovered_nodes": row_recovered,
                "nodes": node_refs,
            })

        result = {
            "schema": "deprule-parse-result-v1",
            "business_code": Codes.OK,
            "pipeline_id": self.pipeline_id,
            "rule": {
                "format": self.snapshot["format"],
                "source_hash": self.snapshot["source_hash"],
                "semantic_hash": self.ir["semantic_hash"],
                "rule_revision": self.ir["rule_revision"],
                "tag_set_version": self.ir["tag_set_version"],
                "root_policy_version": self.ir["root_policy_version"],
            },
            "results": exports,
        }
        ledger = {
            "schema": "deprule-invalidation-ledger-v1",
            "business_code": Codes.OK,
            "pipeline_id": self.pipeline_id,
            "tasks_hash": self.tasks_hash,
            "rule": result["rule"],
            "impact": self.impact,
            "committed_checkpoint_stage": committed_stage,
            "sentences": ledger_sentences,
            "affected_sentence_ids": self.impact["affected_sentence_ids"],
            "linguistic_result_changed_sentence_ids": linguistic_changed_ids,
            "recomputed_node_instances": recomputed_instances,
            "reused_node_instances": reused_instances,
            "recovered_node_instances": recovered_instances,
            "recomputed_kinds": sorted({item.split(":", 1)[1] for item in recomputed_instances}),
            "reused_kinds": sorted({item.split(":", 1)[1] for item in reused_instances}),
            "recovered_kinds": sorted({item.split(":", 1)[1] for item in recovered_instances}),
        }
        return result, ledger


def run_pipeline(tasks: list[dict], snapshot: dict, state_dir: str | Path, **kwargs) -> tuple[dict, dict]:
    return PipelineRunner(tasks, snapshot, state_dir, **kwargs).run()
