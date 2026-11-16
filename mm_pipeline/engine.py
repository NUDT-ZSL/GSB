"""Multimodal collaborative reasoning engine with recoverable rounds."""
from __future__ import annotations

import json
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .alignment import align_relations
from .config import PipelineConfig, canonical_hash, load_relations
from .features import extract_text_features, extract_visual_features
from .fusion import fuse_round
from .persistence import (SnapshotError, atomic_write_json,
                          cleanup_stale_temp, load_snapshot, verify_snapshot,
                          write_snapshot)
from .sample import discover_samples

STAGE_CODES = {
    "feature_extraction": "E_FEATURE_FAILED",
    "relation_alignment": "E_ALIGNMENT_FAILED",
    "fusion": "E_FUSION_FAILED",
}
STAGE_ORDER = {"file_existence": 0, "png_validation": 1,
               "json_validation": 2, "sample_loading": 3}


def _stage_sort_key(item: dict) -> tuple:
    return (item.get("sample_name") or "",
            item.get("round") if item.get("round") is not None else -1,
            STAGE_ORDER.get(item.get("stage"), 4),
            item.get("code") or "")


def _load_sample_relations(input_dir: Path, sample_name: str) -> list[dict]:
    path = input_dir / sample_name / "relations.json"
    return load_relations(path)


def _stable_sort_key(sample_name: str, round_index: int) -> str:
    return f"{sample_name}#round{round_index:06d}"


def _process_one_round(input_dir: Path, sample_name: str, image,
                       description: dict, relations: list[dict],
                       cfg: PipelineConfig, round_index: int) -> dict:
    from .sample import Sample

    sample = Sample(name=sample_name, image=image, description=description)
    started = time.perf_counter()
    try:
        visual = extract_visual_features(sample, cfg)
        text = extract_text_features(sample, cfg)
    except Exception as exc:
        try:
            exc.stage = "feature_extraction"
        except (AttributeError, TypeError):
            pass
        raise
    feature_seconds = time.perf_counter() - started

    started = time.perf_counter()
    try:
        aligned, dangling = align_relations(
            sample, relations, visual, text, cfg)
    except Exception as exc:
        try:
            exc.stage = "relation_alignment"
        except (AttributeError, TypeError):
            pass
        raise
    alignment_seconds = time.perf_counter() - started

    started = time.perf_counter()
    try:
        round_result = fuse_round(sample_name, round_index, aligned, cfg)
    except Exception as exc:
        try:
            exc.stage = "fusion"
        except (AttributeError, TypeError):
            pass
        raise
    fusion_seconds = time.perf_counter() - started

    round_result["sort_key"] = _stable_sort_key(sample_name, round_index)
    round_result["region_bboxes"] = [
        {"region_id": region["region_id"], "bbox": region["bbox"]}
        for region in visual["regions"]
    ]
    return {"round_result": round_result, "dangling": dangling,
            "timings": {"feature_extraction_seconds": feature_seconds,
                        "relation_alignment_seconds": alignment_seconds,
                        "fusion_seconds": fusion_seconds}}


class ReasoningEngine:
    def __init__(self, input_dir: str | Path, snapshot_dir: str | Path,
                 report_path: str | Path,
                 exit_after_round: int | None = None):
        self.input_dir = Path(input_dir)
        self.snapshot_dir = Path(snapshot_dir)
        self.report_path = Path(report_path)
        self.exit_after_round = exit_after_round
        self.cfg = PipelineConfig.load(self.input_dir / "config.json")
        self._stop_requested = threading.Event()
        self._previous_handlers = {}
        self.timings = {
            "sample_loading_seconds": 0.0,
            "feature_extraction_seconds": 0.0,
            "relation_alignment_seconds": 0.0,
            "fusion_seconds": 0.0,
            "snapshot_seconds": 0.0,
            "total_seconds": 0.0,
        }

    # ------------------------------------------------------------------ run
    def run(self) -> dict:
        total_start = time.perf_counter()
        cleanup_stale_temp(self.snapshot_dir)
        self._install_signal_handlers()
        try:
            result = self._run_locked(total_start)
        finally:
            self._restore_signal_handlers()
        return result

    def _run_locked(self, total_start: float) -> dict:
        loading_start = time.perf_counter()
        samples, load_failures = discover_samples(self.input_dir)
        self.timings["sample_loading_seconds"] = round(
            time.perf_counter() - loading_start, 6)

        samples.sort(key=lambda item: item.name)
        relations_by_sample = {
            sample.name: _load_sample_relations(self.input_dir, sample.name)
            for sample in samples
        }
        relations_hash = canonical_hash([
            relations_by_sample[sample.name] for sample in samples])

        snapshot = load_snapshot(self.snapshot_dir)
        resumed = False
        if snapshot is not None:
            verify_snapshot(snapshot, self.cfg.config_hash, relations_hash)
            resumed = True

        if snapshot is None:
            state = self._initial_state(samples, load_failures,
                                        relations_hash)
        else:
            state = snapshot
            state["resumed"] = True

        max_rounds = self.cfg.max_evidence_rounds
        active_names = [entry["sample_name"] for entry in state["sample_states"]
                        if entry["status"] == "active"]
        sample_lookup = {sample.name: sample for sample in samples}
        relations_lookup = relations_by_sample

        graceful_exit = False
        while active_names and state["current_round"] < max_rounds:
            round_index = state["current_round"] + 1
            round_outcomes = self._run_round_wave(
                round_index, active_names, sample_lookup,
                relations_lookup, state)
            active_names = round_outcomes["active_names"]
            state["current_round"] = round_index
            state["cursor"] = {
                "round": round_index,
                "active_sample_count": len(active_names),
                "active_sample_names": list(active_names),
            }
            state["version"] = int(state.get("version", 0)) + 1

            snapshot_start = time.perf_counter()
            write_snapshot(self.snapshot_dir, state)
            self.timings["snapshot_seconds"] += round(
                time.perf_counter() - snapshot_start, 6)

            if (self.exit_after_round is not None
                    and round_index >= self.exit_after_round):
                graceful_exit = True
                state["graceful_exit"] = True
                write_snapshot(self.snapshot_dir, state)
                break
            if self._stop_requested.is_set():
                graceful_exit = True
                state["graceful_exit"] = True
                write_snapshot(self.snapshot_dir, state)
                break

        for entry in state["sample_states"]:
            if entry["status"] == "active":
                entry["status"] = "concluded"
                entry["conclusion"] = self._conclusion_for(entry)
        state["completed"] = not graceful_exit
        state["resumed"] = resumed

        self.timings["total_seconds"] = round(
            time.perf_counter() - total_start, 6)
        self.timings["feature_extraction_seconds"] = round(
            self.timings["feature_extraction_seconds"], 6)
        self.timings["relation_alignment_seconds"] = round(
            self.timings["relation_alignment_seconds"], 6)
        self.timings["fusion_seconds"] = round(
            self.timings["fusion_seconds"], 6)
        self.timings["snapshot_seconds"] = round(
            self.timings["snapshot_seconds"], 6)

        report = self._build_report(state, graceful_exit)
        atomic_write_json(self.report_path, report)
        return {"report": report, "graceful_exit": graceful_exit,
                "resumed": resumed}

    # --------------------------------------------------------------- rounds
    def _run_round_wave(self, round_index: int, active_names: list[str],
                        sample_lookup: dict, relations_lookup: dict,
                        state: dict) -> dict:
        ordered_names = sorted(active_names)
        max_workers = min(self.cfg.concurrency, len(ordered_names))
        outcomes = []

        def work(sample_name: str):
            sample = sample_lookup[sample_name]
            return _process_one_round(
                self.input_dir, sample_name, sample.image,
                sample.description, relations_lookup[sample_name],
                self.cfg, round_index)

        if max_workers == 1:
            for sample_name in ordered_names:
                outcomes.append((sample_name, work(sample_name)))
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(work, name): name
                           for name in ordered_names}
                for future in futures:
                    sample_name = futures[future]
                    try:
                        outcomes.append((sample_name, future.result()))
                    except Exception as exc:  # noqa: BLE001 - business chain
                        outcomes.append((sample_name, exc))

        outcomes.sort(key=lambda pair: pair[0])
        next_active = []
        state_by_name = {entry["sample_name"]: entry
                         for entry in state["sample_states"]}

        for sample_name, outcome in outcomes:
            entry = state_by_name[sample_name]
            if isinstance(outcome, Exception):
                stage = getattr(outcome, "stage", None) or self._stage_from_exc(
                    type(outcome).__name__)
                failure = {
                    "sample_name": sample_name,
                    "round": round_index,
                    "stage": stage,
                    "code": STAGE_CODES.get(stage, "E_PIPELINE_EXCEPTION"),
                    "exception_type": type(outcome).__name__,
                    "message": str(outcome),
                }
                state["failures"].append(failure)
                entry["status"] = "failed"
                entry["failed_round"] = round_index
                entry["failure_code"] = failure["code"]
                continue

            for stage_name, seconds in outcome["timings"].items():
                self.timings[stage_name] += seconds
            round_result = outcome["round_result"]
            existing_warnings = {
                (item["sample_name"], item["relation_id"])
                for item in state["relation_warnings"]
            }
            for dangling in outcome["dangling"]:
                key = (dangling["sample_name"], dangling["relation_id"])
                if key in existing_warnings:
                    continue
                existing_warnings.add(key)
                dangling = dict(dangling)
                dangling["round"] = round_index
                state["relation_warnings"].append(dangling)
            entry["rounds"].append(round_result)
            entry["score_trajectory"].append(round_result["round_score"])
            entry["confidence_trajectory"].append(round_result["confidence"])
            entry["current_score"] = round_result["round_score"]
            entry["current_confidence"] = round_result["confidence"]
            entry["evidence_summary"] = self._evidence_summary(round_result)
            entry["region_bboxes"] = round_result["region_bboxes"]
            entry["sort_key"] = _stable_sort_key(sample_name, round_index)

            if round_result["converged"]:
                entry["status"] = "concluded"
                entry["conclusion"] = self._conclusion_for(entry)
            elif round_index >= self.cfg.max_evidence_rounds:
                entry["status"] = "concluded"
                entry["conclusion"] = self._conclusion_for(entry)
            else:
                next_active.append(sample_name)

        state["failures"].sort(key=_stage_sort_key)
        state["relation_warnings"].sort(
            key=lambda item: (item["sample_name"], item["round"],
                              item["relation_id"]))
        return {"active_names": next_active}

    @staticmethod
    def _stage_from_exc(exc_name: str) -> str:
        if "Feature" in exc_name:
            return "feature_extraction"
        if "Alignment" in exc_name:
            return "relation_alignment"
        if "Fusion" in exc_name:
            return "fusion"
        return "pipeline"

    # ---------------------------------------------------------------- state
    def _initial_state(self, samples, load_failures: list[dict],
                       relations_hash: str) -> dict:
        sample_states = []
        for sample in samples:
            sample_states.append({
                "sample_name": sample.name,
                "status": "active",
                "sort_key": _stable_sort_key(sample.name, 0),
                "current_round": 0,
                "current_score": 0.0,
                "current_confidence": 0.0,
                "score_trajectory": [],
                "confidence_trajectory": [],
                "rounds": [],
                "evidence_summary": [],
                "region_bboxes": [],
                "conclusion": None,
                "failed_round": None,
                "failure_code": None,
            })
        return {
            "format_version": 1,
            "version": 0,
            "current_round": 0,
            "cursor": {"round": 0, "active_sample_count": len(sample_states),
                       "active_sample_names": [s["sample_name"]
                                               for s in sample_states]},
            "config_hash": self.cfg.config_hash,
            "relations_hash": relations_hash,
            "sample_states": sample_states,
            "failures": sorted(load_failures, key=_stage_sort_key),
            "relation_warnings": [],
            "completed": False,
            "resumed": False,
            "graceful_exit": False,
        }

    @staticmethod
    def _evidence_summary(round_result: dict) -> list[dict]:
        return [{
            "relation_id": item["relation_id"],
            "type": item["type"],
            "visual_component": item["visual_component"],
            "text_component": item["text_component"],
            "supplement_component": item["supplement_component"],
            "active": item["active"],
        } for item in round_result["evidence_contributions"]]

    @staticmethod
    def _conclusion_for(entry: dict) -> str:
        confidence = float(entry.get("current_confidence", 0.0))
        if confidence >= 0.5:
            return "match"
        return "mismatch"

    def _build_report(self, state: dict, graceful_exit: bool) -> dict:
        sample_results = []
        for entry in state["sample_states"]:
            sample_results.append({
                "sample_name": entry["sample_name"],
                "joint_conclusion": entry["conclusion"],
                "final_confidence": entry["current_confidence"],
                "round_score_trajectory": entry["score_trajectory"],
                "confidence_trajectory": entry["confidence_trajectory"],
                "evidence_contribution_breakdown": entry["rounds"],
                "region_bboxes": entry["region_bboxes"],
                "status": entry["status"],
                "failure_code": entry["failure_code"],
                "sort_key": entry.get("sort_key"),
            })
        return {
            "report_version": state["version"],
            "snapshot_version": state["version"],
            "format_version": state["format_version"],
            "completed": state["completed"],
            "resumed": state["resumed"],
            "graceful_exit": graceful_exit,
            "current_round": state["current_round"],
            "cursor": state["cursor"],
            "config_hash": state["config_hash"],
            "relations_hash": state["relations_hash"],
            "sample_results": sample_results,
            "failures": state["failures"],
            "relation_warnings": state["relation_warnings"],
            "stage_timings_seconds": self.timings,
        }

    # --------------------------------------------------------------- signals
    def _install_signal_handlers(self) -> None:
        if not threading.current_thread() is threading.main_thread():
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            except (ValueError, OSError):
                continue

    def _restore_signal_handlers(self) -> None:
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                continue

    def _handle_signal(self, signum, frame) -> None:  # noqa: ARG002
        self._stop_requested.set()
