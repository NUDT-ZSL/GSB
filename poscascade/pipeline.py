from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_text,
    canonical_json,
    canonical_jsonl,
    object_checksum,
    sha256_hex,
)
from .config import PipelineConfig, load_config
from .errors import BusinessError, ErrorCode, ErrorCollector
from .models import LabelSet, Lexicon, load_labels, load_lexicon
from .segment import run_segmentation
from .selfcheck import run_selfcheck
from .store import GenerationRecord, SnapshotRecord, SnapshotStore, STAGE_ORDER
from .summarize import run_summarization
from .tag import run_tagging
from .validate import read_input_lines, run_validation


class SimulatedCrash(BaseException):
    pass


@dataclass
class RunOutcome:
    ok: bool
    code: str
    mode: str
    generation: int | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    recovery_report: dict[str, Any] = field(default_factory=dict)
    selfcheck: dict[str, Any] = field(default_factory=dict)
    affected: dict[str, list[str]] = field(default_factory=dict)
    recomputed: dict[str, list[str]] = field(default_factory=dict)
    reused: dict[str, list[str]] = field(default_factory=dict)
    degradations: list[dict[str, Any]] = field(default_factory=list)
    output_paths: dict[str, str] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "mode": self.mode,
            "generation": self.generation,
            "errors": list(self.errors),
            "snapshots": dict(self.snapshots),
            "recovery_report": dict(self.recovery_report),
            "selfcheck": dict(self.selfcheck),
            "affected": dict(self.affected),
            "recomputed": dict(self.recomputed),
            "reused": dict(self.reused),
            "degradations": list(self.degradations),
            "output_paths": dict(self.output_paths),
            "message": self.message,
        }


@dataclass
class CrashController:
    after_stage: str | None = None
    mid_stage: str | None = None
    after_docs: int = 1
    hard: bool = True

    def progress_hook(self, stage: str, processed: int, doc_id: str) -> None:
        if self.mid_stage == stage and processed >= self.after_docs:
            self._terminate(f"mid_stage:{stage}:{doc_id}")

    def after_commit(self, stage: str) -> None:
        if self.after_stage == stage:
            self._terminate(f"after_stage:{stage}")

    def _terminate(self, reason: str) -> None:
        if self.hard:
            sys.stdout.write(f"SIMULATED_HARD_TERMINATION {reason}\n")
            sys.stdout.flush()
            sys.stderr.write(f"SIMULATED_HARD_TERMINATION {reason}\n")
            sys.stderr.flush()
            os._exit(137)
        raise SimulatedCrash(reason)


def _line_map(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    return {line["doc_id"]: line for line in payload.get("lines", [])}


def _affected_by_line_diff(
    old_payload: dict[str, Any] | None,
    new_payload: dict[str, Any],
) -> list[str]:
    old_lines = _line_map(old_payload)
    affected: list[str] = []
    for line in new_payload.get("lines", []):
        doc_id = line["doc_id"]
        old_line = old_lines.get(doc_id)
        if old_line is None or canonical_json(old_line) != canonical_json(line):
            affected.append(doc_id)
    return sorted(affected)


class CascadePipeline:
    def __init__(
        self,
        *,
        input_path: str | Path,
        dict_path: str | Path,
        labels_path: str | Path,
        store_path: str | Path,
        output_dir: str | Path,
        config: PipelineConfig | str | Path | None = None,
        crash: CrashController | None = None,
        fault_injection: str | None = None,
    ) -> None:
        self.input_path = Path(input_path)
        self.dict_path = Path(dict_path)
        self.labels_path = Path(labels_path)
        self.store_path = Path(store_path)
        self.output_dir = Path(output_dir)
        if isinstance(config, PipelineConfig):
            self.config = config
        else:
            self.config = load_config(config)
        self.crash = crash or CrashController(hard=False)
        self.fault_injection = fault_injection
        self.current_stage = "prepare"
        self.store = SnapshotStore(self.store_path)

    def run(self) -> RunOutcome:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stage = "prepare"
        self.current_stage = stage
        errors = ErrorCollector()
        try:
            labels = load_labels(self.labels_path)
            lexicon = load_lexicon(self.dict_path, labels)
            if self.config.fallback_pos not in labels.labels:
                errors.add(
                    ErrorCode.E_LABEL_INVALID,
                    "fallback_pos 不在受控标签集合中",
                    stage="prepare",
                    details={
                        "fallback_pos": self.config.fallback_pos,
                        "labels": list(labels.labels),
                    },
                )

            stage = "validate"
            self.current_stage = stage
            raw_rows, input_checksum = read_input_lines(self.input_path, self.config, errors)
            if errors.has_errors():
                return self._error_outcome(
                    errors,
                    mode="rejected",
                    message="输入或资源校验失败，未提交任何阶段快照",
                )

            active = self.store.active_generation()
            if active is not None:
                self._assert_no_rollback(active, labels, lexicon)

            new_identity = (
                input_checksum,
                self.config.checksum,
                lexicon.version,
                lexicon.checksum,
                labels.version,
                labels.checksum,
            )
            active_snapshots = self.store.load_active_snapshots()
            active_last_stage = self._last_stage_from_snapshots(active_snapshots)

            if active is not None and active.identity == new_identity:
                mode = "resume"
                generation = active
                plan = dict(active.plan)
                base_generation = active.base_generation
            elif active is None:
                mode = "initial"
                doc_ids = [record["doc_id"] for record in raw_rows]
                plan = self._initial_plan(doc_ids)
                base_generation = None
                generation = self.store.start_generation(
                    input_checksum=input_checksum,
                    config_checksum=self.config.checksum,
                    dict_version=lexicon.version,
                    dict_checksum=lexicon.checksum,
                    labels_version=labels.version,
                    labels_checksum=labels.checksum,
                    doc_ids=doc_ids,
                    plan=plan,
                    base_generation=None,
                )
            else:
                mode = "upgrade"
                plan = self._upgrade_plan(
                    active,
                    active_snapshots,
                    raw_rows,
                    input_checksum,
                    labels,
                    lexicon,
                )
                base_generation = active.generation
                doc_ids = [record["doc_id"] for record in raw_rows]
                generation = self.store.start_generation(
                    input_checksum=input_checksum,
                    config_checksum=self.config.checksum,
                    dict_version=lexicon.version,
                    dict_checksum=lexicon.checksum,
                    labels_version=labels.version,
                    labels_checksum=labels.checksum,
                    doc_ids=doc_ids,
                    plan=plan,
                    base_generation=base_generation,
                )

            target_snapshots = self.store.list_snapshots(generation.generation)
            target_by_stage = {item.stage: item for item in target_snapshots}
            base_snapshots: dict[str, SnapshotRecord] = {}
            if base_generation is not None:
                base_snapshots = {
                    item.stage: item
                    for item in self.store.list_snapshots(base_generation)
                }

            recovery_report = self._recovery_report(
                mode=mode,
                active_before=active,
                active_last_stage=active_last_stage,
                target=generation,
                target_by_stage=target_by_stage,
                plan=plan,
            )
            return self._execute(
                generation=generation,
                plan=plan,
                raw_rows=raw_rows,
                input_checksum=input_checksum,
                labels=labels,
                lexicon=lexicon,
                base_generation=base_generation,
                base_snapshots=base_snapshots,
                target_by_stage=target_by_stage,
                recovery_report=recovery_report,
                mode=mode,
            )
        except BusinessError as exc:
            errors.add(
                exc.code,
                exc.message,
                doc_id=exc.doc_id,
                line_no=exc.line_no,
                stage=exc.stage or self.current_stage,
                details=exc.details,
            )
            return self._error_outcome(
                errors,
                mode="error",
                message="级联执行失败，未提交失败阶段",
            )
        except Exception as exc:
            errors.add(
                ErrorCode.E_UNATTRIBUTED,
                f"未被业务阶段归因的异常: {type(exc).__name__}: {exc}",
                stage=self.current_stage,
                details={
                    "exception_type": type(exc).__name__,
                    "traceback": traceback.format_exc(),
                },
            )
            return self._error_outcome(
                errors,
                mode="error",
                message="存在未归因异常，已汇入 E_UNATTRIBUTED",
            )

    def _assert_no_rollback(
        self,
        active: GenerationRecord,
        labels: LabelSet,
        lexicon: Lexicon,
    ) -> None:
        if lexicon.version < active.dict_version:
            raise BusinessError(
                ErrorCode.E_SNAPSHOT_ROLLBACK,
                "词典版本低于活动代际，拒绝回退",
                stage="prepare",
                details={
                    "requested": lexicon.version,
                    "active": active.dict_version,
                },
            )
        if labels.version < active.labels_version:
            raise BusinessError(
                ErrorCode.E_SNAPSHOT_ROLLBACK,
                "标签集合版本低于活动代际，拒绝回退",
                stage="prepare",
                details={
                    "requested": labels.version,
                    "active": active.labels_version,
                },
            )

    @staticmethod
    def _last_stage_from_snapshots(snapshots: dict[str, SnapshotRecord]) -> str | None:
        for stage in reversed(STAGE_ORDER):
            if stage in snapshots:
                return stage
        return None

    @staticmethod
    def _initial_plan(doc_ids: list[str]) -> dict[str, Any]:
        ordered = sorted(doc_ids)
        return {
            "mode": "initial",
            "base_generation": None,
            "affected": {stage: list(ordered) for stage in STAGE_ORDER},
            "reasons": ["initial_generation"],
            "label_contraction": [],
            "no_dict_hit_docs": [],
        }

    def _upgrade_plan(
        self,
        active: GenerationRecord,
        active_snapshots: dict[str, SnapshotRecord],
        raw_rows: list[dict[str, Any]],
        input_checksum: str,
        labels: LabelSet,
        lexicon: Lexicon,
    ) -> dict[str, Any]:
        doc_ids = sorted(record["doc_id"] for record in raw_rows)
        all_stages_all = {stage: list(doc_ids) for stage in STAGE_ORDER}
        reasons: list[str] = []
        old_tag = active_snapshots.get("tag")
        old_labels = set(old_tag.payload.get("label_set", [])) if old_tag else set()
        removed_labels = sorted(old_labels - set(labels.labels))

        if input_checksum != active.input_checksum:
            reasons.append("input_changed")
        if self.config.checksum != active.config_checksum:
            reasons.append("config_changed")
        if lexicon.version != active.dict_version:
            reasons.append("dict_version_changed")
        if lexicon.checksum != active.dict_checksum:
            reasons.append("dict_checksum_changed")
        if labels.version != active.labels_version:
            reasons.append("labels_version_changed")
        if labels.checksum != active.labels_checksum:
            reasons.append("labels_checksum_changed")
        if removed_labels:
            reasons.append("label_set_contracted")

        if input_checksum != active.input_checksum or self.config.checksum != active.config_checksum:
            return {
                "mode": "upgrade",
                "base_generation": active.generation,
                "affected": all_stages_all,
                "reasons": reasons or ["resource_changed"],
                "label_contraction": removed_labels,
                "no_dict_hit_docs": [],
            }

        if "validate" not in active_snapshots:
            return {
                "mode": "upgrade",
                "base_generation": active.generation,
                "affected": all_stages_all,
                "reasons": reasons + ["validate_snapshot_missing"],
                "label_contraction": removed_labels,
                "no_dict_hit_docs": [],
            }

        new_validation = run_validation(raw_rows, input_checksum, self.config)
        old_segment = active_snapshots.get("segment")
        old_tag = active_snapshots.get("tag")
        old_summary = active_snapshots.get("summarize")

        no_dict_hit_docs: list[str] = []
        if old_segment is None:
            segment_affected = list(doc_ids)
            reasons.append("segment_snapshot_missing")
            probe_segment = run_segmentation(new_validation, lexicon, self.config)
        else:
            probe_segment = run_segmentation(new_validation, lexicon, self.config)
            segment_affected = _affected_by_line_diff(old_segment.payload, probe_segment)
            old_cap_mode = bool(old_segment.payload.get("candidate_cap_mode"))
            new_cap_mode = bool(probe_segment.get("candidate_cap_mode"))
            if old_cap_mode or new_cap_mode:
                segment_affected = list(doc_ids)
                reasons.append("candidate_cap_mode_changed_or_active")

            old_lines = _line_map(old_segment.payload)
            new_lines = _line_map(probe_segment)
            for doc_id in doc_ids:
                old_line = old_lines.get(doc_id)
                new_line = new_lines.get(doc_id)
                if old_line is None or new_line is None:
                    continue
                old_hit = any(token.get("source") == "dict" for token in old_line["tokens"])
                new_hit = any(token.get("source") == "dict" for token in new_line["tokens"])
                if not old_hit and not new_hit:
                    no_dict_hit_docs.append(doc_id)

        if old_tag is None:
            tag_affected = list(doc_ids)
            reasons.append("tag_snapshot_missing")
            probe_tag = run_tagging(probe_segment, labels, self.config)
        else:
            probe_tag = run_tagging(probe_segment, labels, self.config)
            tag_affected = _affected_by_line_diff(old_tag.payload, probe_tag)
            tag_affected = sorted(set(tag_affected) | set(segment_affected))
            if old_tag.payload.get("degradations") or probe_tag.get("degradations"):
                tag_affected = list(doc_ids)
                reasons.append("tag_degradation_mode_changed_or_active")

        if old_summary is None:
            summary_affected = list(doc_ids)
            reasons.append("summary_snapshot_missing")
        else:
            summary_affected = sorted(set(segment_affected) | set(tag_affected))

        no_hit_affected = sorted(set(no_dict_hit_docs) & set(segment_affected))
        if no_hit_affected:
            raise BusinessError(
                ErrorCode.E_INCONSISTENT,
                "未命中旧词条且未命中新词条的文档被判为分词受影响",
                stage="invalidate",
                details={"doc_ids": no_hit_affected},
            )

        return {
            "mode": "upgrade",
            "base_generation": active.generation,
            "affected": {
                "validate": [],
                "segment": segment_affected,
                "tag": tag_affected,
                "summarize": summary_affected,
            },
            "reasons": reasons or ["resource_identity_replaced"],
            "label_contraction": removed_labels,
            "no_dict_hit_docs": sorted(no_dict_hit_docs),
        }

    @staticmethod
    def _recovery_report(
        *,
        mode: str,
        active_before: GenerationRecord | None,
        active_last_stage: str | None,
        target: GenerationRecord,
        target_by_stage: dict[str, SnapshotRecord],
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        completed_target = [
            stage for stage in STAGE_ORDER if stage in target_by_stage
        ]
        next_stage = next(
            (stage for stage in STAGE_ORDER if stage not in target_by_stage),
            None,
        )
        return {
            "mode": mode,
            "base_generation": target.base_generation,
            "target_generation": target.generation,
            "last_committed_stage_at_start": active_last_stage,
            "completed_stages_at_start": completed_target,
            "resume_from_stage": next_stage,
            "rebuild_stages": [
                stage
                for stage in STAGE_ORDER
                if stage not in target_by_stage
            ],
            "committed_stage_count_at_start": len(completed_target),
            "uncommitted_intermediate_policy": "discard_memory_and_rebuild_stage",
            "committed_rows_are_reused": True,
            "active_before_generation": (
                active_before.generation if active_before is not None else None
            ),
            "affected": plan.get("affected", {}),
            "reasons": plan.get("reasons", []),
        }

    def _execute(
        self,
        *,
        generation: GenerationRecord,
        plan: dict[str, Any],
        raw_rows: list[dict[str, Any]],
        input_checksum: str,
        labels: LabelSet,
        lexicon: Lexicon,
        base_generation: int | None,
        base_snapshots: dict[str, SnapshotRecord],
        target_by_stage: dict[str, SnapshotRecord],
        recovery_report: dict[str, Any],
        mode: str,
    ) -> RunOutcome:
        all_doc_ids = list(generation.doc_ids)
        affected: dict[str, list[str]] = {
            stage: sorted(plan.get("affected", {}).get(stage, []))
            for stage in STAGE_ORDER
        }
        records: dict[str, SnapshotRecord] = dict(target_by_stage)
        skipped_committed_stages = [
            stage for stage in STAGE_ORDER if stage in records
        ]
        executed_stages: list[str] = []
        invocation_recomputed: dict[str, list[str]] = {}
        errors = ErrorCollector()

        for stage in STAGE_ORDER:
            if stage in records:
                continue
            self.current_stage = stage
            if self.fault_injection == stage:
                raise RuntimeError(f"demo injected unattributed fault at stage {stage}")
            executed_stages.append(stage)
            stage_recompute = affected[stage]
            invocation_recomputed[stage] = list(stage_recompute)
            stage_reused = sorted(set(all_doc_ids) - set(stage_recompute))
            if stage == "validate":
                if (
                    not stage_recompute
                    and "validate" in base_snapshots
                ):
                    payload = base_snapshots["validate"].payload
                else:
                    payload = run_validation(raw_rows, input_checksum, self.config)
            elif stage == "segment":
                base_lines = _line_map(
                    base_snapshots["segment"].payload
                    if "segment" in base_snapshots
                    else None
                )
                payload = run_segmentation(
                    records["validate"].payload,
                    lexicon,
                    self.config,
                    reusable_lines=base_lines,
                    recompute_doc_ids=stage_recompute,
                    progress_hook=self.crash.progress_hook,
                )
            elif stage == "tag":
                base_lines = _line_map(
                    base_snapshots["tag"].payload
                    if "tag" in base_snapshots
                    else None
                )
                payload = run_tagging(
                    records["segment"].payload,
                    labels,
                    self.config,
                    reusable_lines=base_lines,
                    recompute_doc_ids=stage_recompute,
                    progress_hook=self.crash.progress_hook,
                )
            elif stage == "summarize":
                base_lines = _line_map(
                    base_snapshots["summarize"].payload
                    if "summarize" in base_snapshots
                    else None
                )
                payload = run_summarization(
                    records["tag"].payload,
                    labels,
                    reusable_lines=base_lines,
                    recompute_doc_ids=stage_recompute,
                    progress_hook=self.crash.progress_hook,
                )
            else:
                raise BusinessError(ErrorCode.E_STAGE_ORDER, f"未知阶段: {stage}")

            if (
                stage == "validate"
                and self.crash.mid_stage == "validate"
                and len(payload.get("lines", [])) >= self.crash.after_docs
            ):
                self.crash.progress_hook("validate", len(payload["lines"]), payload["lines"][-1]["doc_id"])

            record, _committed = self.store.commit_stage(
                generation=generation.generation,
                stage=stage,
                payload=payload,
                affected_doc_ids=stage_recompute,
                recompute_doc_ids=stage_recompute,
                reuse_doc_ids=stage_reused,
                extra_metadata={
                    "mode": mode,
                    "reasons": plan.get("reasons", []),
                    "base_generation": base_generation,
                },
            )
            records[stage] = record
            self.crash.after_commit(stage)

        recovery_report["skipped_committed_stages"] = skipped_committed_stages
        recovery_report["executed_stages_this_run"] = executed_stages
        recovery_report["invocation_recomputed"] = invocation_recomputed

        recomputed = {
            stage: list(records[stage].metadata["recomputed_doc_ids"])
            for stage in STAGE_ORDER
        }
        reused = {
            stage: list(records[stage].metadata["reused_doc_ids"])
            for stage in STAGE_ORDER
        }
        degradations: list[dict[str, Any]] = []
        for stage in ("segment", "tag", "summarize"):
            degradations.extend(records[stage].payload.get("degradations", []))

        selfcheck = run_selfcheck(
            records["validate"].payload,
            records["segment"].payload,
            records["tag"].payload,
            records["summarize"].payload,
            labels,
            affected=affected,
            recomputed=recomputed,
            reused=reused,
        )

        snapshot_summaries = {
            stage: records[stage].summary() for stage in STAGE_ORDER
        }
        output_paths, manifest = self._write_outputs(
            generation=generation,
            plan=plan,
            recovery_report=recovery_report,
            records=records,
            snapshot_summaries=snapshot_summaries,
            selfcheck=selfcheck,
            affected=affected,
            recomputed=recomputed,
            reused=reused,
            degradations=degradations,
        )

        code = ErrorCode.OK
        if selfcheck["code"] != ErrorCode.OK:
            code = ErrorCode.E_INCONSISTENT
            for issue in selfcheck["issues"]:
                self.store.record_event(
                    code=issue["code"],
                    message=issue["message"],
                    stage=issue["stage"],
                    doc_id=issue.get("doc_id"),
                    line_no=issue.get("line_no"),
                    details={"category": issue["category"], **issue.get("detail", {})},
                )

        return RunOutcome(
            ok=code == ErrorCode.OK,
            code=code,
            mode=mode,
            generation=generation.generation,
            snapshots=snapshot_summaries,
            recovery_report=recovery_report,
            selfcheck=selfcheck,
            affected=affected,
            recomputed=recomputed,
            reused=reused,
            degradations=degradations,
            output_paths=output_paths,
            message="级联完成" if code == ErrorCode.OK else "跨模块一致性自检失败",
        )

    def _write_outputs(
        self,
        *,
        generation: GenerationRecord,
        plan: dict[str, Any],
        recovery_report: dict[str, Any],
        records: dict[str, SnapshotRecord],
        snapshot_summaries: dict[str, dict[str, Any]],
        selfcheck: dict[str, Any],
        affected: dict[str, list[str]],
        recomputed: dict[str, list[str]],
        reused: dict[str, list[str]],
        degradations: list[dict[str, Any]],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        paths: dict[str, str] = {}
        validation_text = canonical_jsonl(records["validate"].payload["lines"])
        segment_text = canonical_jsonl(records["segment"].payload["lines"])
        annotation_text = canonical_jsonl(records["tag"].payload["lines"])
        summary_text = canonical_json(records["summarize"].payload) + "\n"

        outputs = {
            "validation": ("validation.jsonl", validation_text),
            "segmentation": ("segmentation.jsonl", segment_text),
            "annotations": ("annotations.jsonl", annotation_text),
            "summary": ("summary.json", summary_text),
        }
        for key, (filename, text) in outputs.items():
            path = self.output_dir / filename
            atomic_write_text(path, text)
            paths[key] = str(path)

        run_meta = {
            "mode": recovery_report["mode"],
            "code": selfcheck["code"],
            "generation": generation.to_dict(),
            "plan": plan,
            "recovery_report": recovery_report,
            "snapshot_commits": snapshot_summaries,
            "affected": affected,
            "recomputed": recomputed,
            "reused": reused,
            "degradations": degradations,
            "selfcheck": selfcheck,
            "business_outputs": {
                "annotations_sha256": sha256_hex(annotation_text),
                "summary_sha256": sha256_hex(summary_text),
                "segmentation_sha256": sha256_hex(segment_text),
            },
        }
        meta_text = canonical_json(run_meta) + "\n"
        meta_path = self.output_dir / "run_meta.json"
        atomic_write_text(meta_path, meta_text)
        paths["run_meta"] = str(meta_path)

        selfcheck_path = self.output_dir / "selfcheck.json"
        atomic_write_text(selfcheck_path, canonical_json(selfcheck) + "\n")
        paths["selfcheck"] = str(selfcheck_path)

        errors_path = self.output_dir / "errors.json"
        atomic_write_text(errors_path, "[]\n")
        paths["errors"] = str(errors_path)

        manifest = {
            "generation": generation.generation,
            "annotations_sha256": sha256_hex(annotation_text),
            "summary_sha256": sha256_hex(summary_text),
            "segmentation_sha256": sha256_hex(segment_text),
            "selfcheck_sha256": object_checksum(selfcheck),
            "files": {
                key: Path(value).name for key, value in paths.items()
            },
        }
        manifest_path = self.output_dir / "manifest.json"
        atomic_write_text(manifest_path, canonical_json(manifest) + "\n")
        paths["manifest"] = str(manifest_path)
        return paths, manifest

    def _error_outcome(
        self,
        errors: ErrorCollector,
        *,
        mode: str,
        message: str,
    ) -> RunOutcome:
        for error in errors.errors:
            self.store.record_event(
                code=error.code,
                message=error.message,
                stage=error.stage,
                doc_id=error.doc_id,
                line_no=error.line_no,
                details=error.details,
            )
        errors_text = canonical_json(errors.to_list()) + "\n"
        errors_path = self.output_dir / "errors.json"
        atomic_write_text(errors_path, errors_text)
        meta_path = self.output_dir / "run_meta.json"
        atomic_write_text(
            meta_path,
            canonical_json(
                {
                    "mode": mode,
                    "code": errors.primary_code(),
                    "errors": errors.to_list(),
                    "unattributed_policy": "never_silently_drop",
                }
            )
            + "\n",
        )
        return RunOutcome(
            ok=False,
            code=errors.primary_code(),
            mode=mode,
            errors=errors.to_list(),
            output_paths={
                "errors": str(errors_path),
                "run_meta": str(meta_path),
            },
            message=message,
        )
