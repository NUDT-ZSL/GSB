"""Workspace orchestration for the local AED professional application."""

from __future__ import annotations

import json
import os

import numpy as np

from .cache import ContentCache, SnapshotStore, canonical_json, sha256_bytes
from .config import KNOWN_TEMPLATE_VERSIONS, PipelineConfig
from .errors import AEDError, Code
from .pipeline import Dialog, Pipeline
from .synth import EVENT_TYPES, write_dataset
from .trace import TraceLog, check_stale, replay

DEFAULT_SPECS = [
    ("dlg-001", 20241101, 12000, ["spk-A", "spk-B"], "t1"),
    ("dlg-002", 20241102, 12000, ["spk-A", "spk-C", "spk-D"], "t1"),
]


class Studio:
    def __init__(self, workspace: str, cfg: PipelineConfig | None = None):
        self.ws = workspace
        self.data_dir = os.path.join(workspace, "data")
        self.cfg = cfg or PipelineConfig()
        os.makedirs(self.data_dir, exist_ok=True)
        self.cache = ContentCache(os.path.join(workspace, "cache"),
                                  max_entries=self.cfg.max_cache_entries)
        self.snaps = SnapshotStore(os.path.join(workspace, "snapshots"))
        self.trace = TraceLog(os.path.join(workspace, "trace.jsonl"))
        self.last_report = None

    # ---- dataset ---------------------------------------------------------
    def generate_dataset(self, specs=DEFAULT_SPECS) -> str:
        ids = [s[0] for s in specs]
        if len(ids) != len(set(ids)):
            raise AEDError(Code.E_DUPLICATE_DIALOG,
                           "duplicate dialog_id in dataset specs",
                           dialog_ids=sorted(ids))
        path = write_dataset(self.data_dir, specs)
        self._commit_template_snapshot(self.cfg.template_version)
        return path

    def load_manifest(self) -> list:
        path = os.path.join(self.data_dir, "manifest.jsonl")
        entries, seen = [], set()
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry["dialog_id"] in seen:
                    raise AEDError(Code.E_DUPLICATE_DIALOG,
                                   "duplicate dialog_id in manifest",
                                   dialog_id=entry["dialog_id"])
                seen.add(entry["dialog_id"])
                entries.append(entry)
        return entries

    def load_dialog(self, dialog_id: str, truth_override=None) -> Dialog:
        pcm = np.fromfile(os.path.join(self.data_dir, f"{dialog_id}.pcm"),
                          dtype="<i2")
        if truth_override is not None:
            truth = truth_override
        else:
            truth = []
            tp = os.path.join(self.data_dir, f"{dialog_id}.truth.jsonl")
            with open(tp, "r", encoding="utf-8") as fh:
                truth = [json.loads(l) for l in fh if l.strip()]
        entry = next(e for e in self.load_manifest()
                     if e["dialog_id"] == dialog_id)
        return Dialog(dialog_id, pcm, truth, entry["duration_ms"])

    # ---- full run --------------------------------------------------------
    def run_all(self) -> dict:
        pipeline = Pipeline(self.cfg, self.cache)
        reports = {}
        for entry in self.load_manifest():
            dialog = self.load_dialog(entry["dialog_id"])
            report = pipeline.run(dialog)
            self.trace.record_run(entry["dialog_id"], self.cfg,
                                  report["values"]["segments"],
                                  report["keys"]["segments"])
            reports[entry["dialog_id"]] = report
        self.last_report = reports
        self._commit_result_summary(reports)
        return reports

    # ---- incremental updates ----------------------------------------------
    def apply_change(self, change: dict, dialog_id: str) -> dict:
        """Apply one incremental change and recompute only the suffix."""
        kind = change["kind"]
        if kind == "template_upgrade":
            new_version = change["template_version"]
            if new_version not in KNOWN_TEMPLATE_VERSIONS:
                raise AEDError(Code.E_TEMPLATE_VERSION_UNKNOWN,
                               "unknown template library version",
                               template_version=new_version)
            self._commit_template_snapshot(new_version)
            self.cfg = PipelineConfig.from_dict(
                {**self.cfg.to_dict(), "template_version": new_version})
            dialog = self.load_dialog(dialog_id)
        elif kind == "truth_fix":
            dialog = self.load_dialog(dialog_id)
            truth = [dict(r) for r in dialog.truth]
            events = [i for i, r in enumerate(truth) if r["is_event"] == 1]
            idx = change["event_index"]
            target = events[idx * 5] if idx * 5 < len(events) else events[-1]
            truth[target]["event_type"] = change.get(
                "new_type", EVENT_TYPES[0])
            dialog = dialog.with_truth(truth)
        elif kind == "hop_change":
            self.cfg = PipelineConfig.from_dict(
                {**self.cfg.to_dict(), "hop": change["hop"]}).validate()
            dialog = self.load_dialog(dialog_id)
        elif kind in ("min_duration_change", "min_gap_change"):
            field = ("min_event_ms" if kind == "min_duration_change"
                     else "min_gap_ms")
            self.cfg = PipelineConfig.from_dict(
                {**self.cfg.to_dict(), field: change["value"]}).validate()
            dialog = self.load_dialog(dialog_id)
        else:
            raise ValueError(kind)

        pipeline = Pipeline(self.cfg, self.cache)
        result = pipeline.run_incremental(change, dialog)
        result["dialog"] = dialog
        self.trace.record_run(dialog_id, self.cfg,
                              result["report"]["values"]["segments"],
                              result["report"]["keys"]["segments"])
        self._commit_result_summary({dialog_id: result["report"]})
        return result

    def full_recompute_bytes(self, dialog_id: str, dialog=None) -> bytes:
        """Reference full recompute in a pristine cache (for equality)."""
        fresh = ContentCache(os.path.join(self.ws, "cache_ref"),
                             max_entries=self.cfg.max_cache_entries)
        pipeline = Pipeline(self.cfg, fresh)
        report = pipeline.run(dialog or self.load_dialog(dialog_id))
        return self._report_bytes(report)

    @staticmethod
    def _report_bytes(report: dict) -> bytes:
        out = [report["values"]["features"].tobytes()]
        for node in ("candidates", "segments", "metrics"):
            out.append(canonical_json(report["values"][node]))
        return b"|".join(out)

    # ---- snapshots ---------------------------------------------------------
    def _commit_template_snapshot(self, template_version: str) -> dict:
        payload = {"template_version": template_version,
                   "event_types": list(EVENT_TYPES)}
        return self.snaps.commit("template_library", payload)

    def _commit_result_summary(self, reports: dict) -> dict:
        payload = {
            "dialogs": {
                did: {
                    "keys": rep["keys"],
                    "status": rep["status"],
                    "metrics": rep["values"]["metrics"],
                    "segments_hash": sha256_bytes(canonical_json(
                        rep["values"]["segments"])),
                } for did, rep in reports.items()
            },
            "template_version": self.cfg.template_version,
        }
        return self.snaps.commit("result_summary", payload)

    # ---- trace / replay ----------------------------------------------------
    def replay_event(self, record_index: int) -> dict:
        records = self.trace.load()
        record = records[record_index]
        check_stale(record, self.cfg)
        return replay(record, self.cache)

    # ---- introspection -----------------------------------------------------
    def describe_cache(self, node: str) -> list:
        return self.cache.describe(node)
