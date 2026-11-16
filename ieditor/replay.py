"""Replay engine: identical operation sequences drive interactive and demo runs.

Every operation is applied through the same deterministic functions used by the
CLI. Stage exceptions are isolated into a failure list and processing of the
remaining operations/projects continues. A graceful stop request finishes the
current project's atomic snapshot and progress record before returning.
"""
from __future__ import annotations

import json
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .config import QuantRules, RenderConfig, VersionMap
from .converter import (convert_project, roundtrip_mask_equal, save_project)
from .editor import edit_mask_rect, set_channel_weight, set_intensity
from .errors import (E_OK, E_RENDER_FAILED, FailureList, IEditorError)
from .fingerprint import structure_fingerprint
from .images import load_rgb, save_png
from .inpaint import inpaint
from .style import apply_style
from .diagnostics import compare_renders
from .pipeline import render_project, resolve_path
from .snapshot import (load_snapshot, write_progress, write_snapshot)
from . import parsers


STAGES = ("parse", "convert", "inpaint", "style", "recovery")


@dataclass
class Session:
    work_dir: Path
    config: RenderConfig
    version_map: VersionMap
    quant_rules: QuantRules
    root_dir: Path = field(default_factory=lambda: Path.cwd())
    project: Optional[Any] = None
    project_file: Optional[Path] = None
    global_version: int = 0
    failure_list: FailureList = field(default_factory=FailureList)
    last_report: Optional[dict] = None
    render_stats: Optional[dict] = None
    stop_requested: bool = False
    rendered_fingerprints: Dict[str, str] = field(default_factory=dict)
    operation_log: List[dict] = field(default_factory=list)
    project_cache: Dict[str, Any] = field(default_factory=dict)

    def request_stop(self) -> None:
        self.stop_requested = True


class ReplayEngine:
    def __init__(self, work_dir: Path, config: RenderConfig,
                 version_map: VersionMap, quant_rules: QuantRules,
                 root_dir: Path | None = None):
        self.root_dir = Path(root_dir).resolve() if root_dir else Path.cwd()
        self.session = Session(work_dir=Path(work_dir), config=config,
                               version_map=version_map,
                               quant_rules=quant_rules,
                               root_dir=self.root_dir)

    def _resolve(self, raw: str) -> Path:
        expanded = raw.replace("${WORK_DIR}", str(self.session.work_dir))
        path = Path(expanded)
        if path.is_absolute():
            return path
        return (self.root_dir / path).resolve()

    # ------------------------------------------------------------------ #
    def _reference(self) -> np.ndarray:
        assert self.session.project is not None
        ref_path = resolve_path(self.session.project_file,
                                self.session.project.style_ref_path)
        return load_rgb(ref_path)

    def _snapshot(self) -> None:
        assert self.session.project is not None and self.session.project_file
        self.session.global_version += 1
        write_snapshot(self.session.project, self.session.project_file,
                       self.session.work_dir, self.session.global_version)
        last_seq = (self.session.project.edit_log[-1]["seq"]
                    if self.session.project.edit_log else -1)
        write_progress(self.session.work_dir,
                       self.session.project.project_id, last_seq,
                       self.session.global_version)

    # ------------------------------------------------------------------ #
    def apply(self, op: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"op": op.get("op"), "business_code": E_OK}
        kind = op.get("op")
        try:
            handler = self._handlers()[kind]
        except KeyError:
            raise IEditorError(f"未知回放操作: {kind}")
        handler(op, result)
        self.session.operation_log.append(
            {"op": kind, "business_code": result["business_code"]})
        return result

    def _handlers(self) -> Dict[str, Callable]:
        return {
            "load": self._op_load,
            "switch_version": self._op_switch,
            "edit_mask_rect": self._op_edit_mask,
            "set_channel_weight": self._op_set_weight,
            "set_intensity": self._op_set_intensity,
            "render": self._op_render,
            "diagnose": self._op_diagnose,
            "verify_snapshot": self._op_verify_snapshot,
        }

    # ------------------------------------------------------------------ #
    def _op_load(self, op: dict, result: dict) -> None:
        path = self._resolve(op["path"])
        project = parsers.parse_project(path)
        self.session.project = project
        self.session.project_file = path
        result["source_version"] = project.source_version
        result["fingerprint"] = structure_fingerprint(project)

    def _op_switch(self, op: dict, result: dict) -> None:
        project = self.session.project
        assert project is not None
        target = int(op["target_version"])
        reference = self._reference()
        converted = convert_project(project, target,
                                    self.session.version_map,
                                    self.session.quant_rules, reference)
        out_path = self._resolve(op["out_path"])
        save_project(converted, out_path)
        reparsed = parsers.parse_project(out_path)
        if not roundtrip_mask_equal(project, reparsed):
            raise IEditorError("转换后重解析掩码位图不一致")
        # File formats do not carry the loss log; keep it on the live unified
        # structure so subsequent diagnostics/snapshots retain downgrade losses.
        reparsed.loss_log = list(converted.loss_log)
        self.session.project = reparsed
        self.session.project_file = out_path
        self.session.project_cache[str(out_path.resolve())] = reparsed
        self._snapshot()
        result["target_version"] = target
        result["fingerprint"] = structure_fingerprint(reparsed)
        result["loss_log_size"] = len(reparsed.loss_log)

    def _op_edit_mask(self, op: dict, result: dict) -> None:
        project = self.session.project
        assert project is not None
        self._ensure_working_copy()
        updated = edit_mask_rect(project, int(op["x"]), int(op["y"]),
                                 int(op["width"]), int(op["height"]),
                                 int(op["value"]))
        self._persist_editable(updated, result)

    def _op_set_weight(self, op: dict, result: dict) -> None:
        project = self.session.project
        assert project is not None
        self._ensure_working_copy()
        updated = set_channel_weight(project, str(op["channel"]),
                                     float(op["weight"]),
                                     self.session.version_map)
        self._persist_editable(updated, result)

    def _op_set_intensity(self, op: dict, result: dict) -> None:
        project = self.session.project
        assert project is not None
        self._ensure_working_copy()
        updated = set_intensity(project, int(op["intensity"]),
                                self.session.version_map)
        self._persist_editable(updated, result)

    def _ensure_working_copy(self) -> None:
        """Copy canonical sample projects into the work dir before editing.

        Sample projects under samples/ are treated as read-only; the first
        mutation persists an identical working copy under the session work
        directory and redirects subsequent operations to that copy.
        """
        assert self.session.project is not None
        assert self.session.project_file is not None
        current = self.session.project_file.resolve()
        if current.parent.resolve() == self.session.work_dir.resolve():
            return
        suffix = current.suffix
        copy_path = self.session.work_dir / (
            f"{self.session.project.project_id}_working{suffix}")
        save_project(self.session.project, copy_path)
        self.session.project_file = copy_path
        self.session.project_cache[str(copy_path.resolve())] = \
            self.session.project

    def _persist_editable(self, updated, result: dict) -> None:
        assert self.session.project_file is not None
        save_project(updated, self.session.project_file)
        reparsed = parsers.parse_project(self.session.project_file)
        reparsed.loss_log = list(updated.loss_log)
        self.session.project = reparsed
        self.session.project_cache[
            str(self.session.project_file.resolve())] = reparsed
        self._snapshot()
        result["fingerprint"] = structure_fingerprint(reparsed)
        result["global_version"] = self.session.global_version

    def _op_render(self, op: dict, result: dict) -> None:
        project = self.session.project
        assert project is not None and self.session.project_file is not None
        if not project.style.channel_weights:
            # v1 projects carry only an intensity档位; expand deterministically
            # through the version map for rendering without mutating the file.
            from dataclasses import replace as _replace
            project = _replace(
                project,
                style=_replace(
                    project.style,
                    channel_weights=self.session.version_map.weights_for(
                        project.style.intensity),
                    palette=project.style.palette or []))
        output_dir = self._resolve(op["output_dir"])
        image_path = resolve_path(self.session.project_file,
                                  project.image_path)
        ref_path = resolve_path(self.session.project_file,
                                project.style_ref_path)
        source_image = load_rgb(image_path)
        reference = load_rgb(ref_path)
        try:
            inpaint_result = inpaint(project, source_image,
                                     self.session.config)
        except IEditorError:
            raise
        except Exception as exc:
            raise IEditorError(str(exc), code=E_RENDER_FAILED,
                               context={"stage": "inpaint"})
        try:
            style_result = apply_style(project, inpaint_result.image,
                                       reference, self.session.config)
        except IEditorError:
            raise
        except Exception as exc:
            raise IEditorError(str(exc), code=E_RENDER_FAILED,
                               context={"stage": "style"})
        output_dir.mkdir(parents=True, exist_ok=True)
        save_png(output_dir / f"{project.project_id}_inpainted.png",
                 inpaint_result.image)
        save_png(output_dir / f"{project.project_id}_styled.png",
                 style_result.image)
        self.session.render_stats = {
            "inpaint": inpaint_result.stats(),
            "style": style_result.stats(),
        }
        digest = _array_hash(style_result.image)
        self.session.rendered_fingerprints[project.project_id] = digest
        result["styled_sha256"] = digest
        result["render_stats"] = self.session.render_stats

    def _op_diagnose(self, op: dict, result: dict) -> None:
        path_a = self._resolve(op["project_a"])
        path_b = self._resolve(op["project_b"])
        # Prefer the live session project so in-memory downgrade loss logs are
        # retained; fall back to disk parsing for independent projects.
        current = self.session.project
        current_file = self.session.project_file
        def _pick(path: Path):
            key = str(path.resolve())
            if key in self.session.project_cache:
                return self.session.project_cache[key]
            if current is not None and current_file is not None and \
                    current_file.resolve() == path.resolve():
                return current
            return parsers.parse_project(path)
        project_a = _pick(path_a)
        project_b = _pick(path_b)
        artifacts_a = render_project(project_a, path_a, self.session.config,
                                     version_map=self.session.version_map)
        artifacts_b = render_project(project_b, path_b, self.session.config,
                                     version_map=self.session.version_map)
        report = compare_renders(project_a, project_b, artifacts_a,
                                 artifacts_b, self.session.config,
                                 self.session.version_map)
        report_path = self._resolve(op["report_path"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(report_path, report)
        self.session.last_report = report
        result["report_path"] = str(report_path)
        result["version_compat_warning"] = report["version_compat_warning"]
        result["max_abs_diff"] = report["max_abs_diff"]

    def _op_verify_snapshot(self, op: dict, result: dict) -> None:
        assert self.session.project is not None
        snapshot = load_snapshot(self.session.project, self.session.work_dir)
        result["global_version"] = snapshot.global_version
        result["fingerprint"] = snapshot.project_fingerprint

    # ------------------------------------------------------------------ #
    def run_sequence(self, operations: List[dict],
                     resume_after: int = -1) -> Dict[str, Any]:
        applied: List[dict] = []
        for index, op in enumerate(operations):
            if index <= resume_after:
                continue
            kind = op.get("op")
            source_hint = _source_hint(kind, op, self.session)
            try:
                op_result = self.apply(op)
            except IEditorError as exc:
                stage = _stage_for(kind, exc)
                self.session.failure_list.add(
                    stage, exc,
                    source_path=source_hint,
                    location=_location_for(kind, op, exc))
                applied.append({"index": index, "op": kind,
                                "business_code": exc.code,
                                "error": str(exc)})
                if self.session.stop_requested:
                    break
                continue
            except Exception as exc:  # defensive isolation
                stage = _stage_for(kind, exc)
                self.session.failure_list.add(
                    stage, exc,
                    source_path=source_hint,
                    location=_location_for(kind, op, exc))
                applied.append({"index": index, "op": kind,
                                "business_code": "E_FORMAT_INVALID",
                                "error": str(exc)})
                if self.session.stop_requested:
                    break
                continue
            applied.append({"index": index, **op_result})
            if self.session.stop_requested:
                break
        return {
            "applied": applied,
            "failures": self.session.failure_list.to_dict_list(),
            "stop_requested": self.session.stop_requested,
            "rendered_fingerprints": dict(
                sorted(self.session.rendered_fingerprints.items())),
        }

    def install_signal_handlers(self) -> None:
        def handler(signum, frame):  # pragma: no cover - signal path
            self.session.request_stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def _source_hint(kind: str, op: dict, session: "Session") -> str:
    if kind == "load":
        return str(session.root_dir / op.get("path", ""))
    if kind == "diagnose":
        return str(session.root_dir / op.get("project_a", ""))
    return str(session.project_file or "")


def _stage_for(kind: str, exc: BaseException | None = None) -> str:
    if kind == "load":
        return "parse"
    if kind == "switch_version":
        return "convert"
    if kind == "render":
        stage = getattr(exc, "context", {}) or {}
        return stage.get("stage", "inpaint") if isinstance(stage, dict) else "inpaint"
    if kind == "verify_snapshot":
        return "recovery"
    if kind == "diagnose":
        return "style"
    return "parse"


def _location_for(kind: str, op: dict, exc: IEditorError) -> Any:
    if hasattr(exc, "context") and exc.context:
        return exc.context
    if kind == "edit_mask_rect":
        return {"rect": [op.get("x"), op.get("y"),
                         op.get("width"), op.get("height")]}
    if kind in ("set_channel_weight", "set_intensity"):
        return {"param_key": kind}
    return {"op_index": op.get("index")}


def _array_hash(image: np.ndarray) -> str:
    import hashlib
    return hashlib.sha256(image.astype(np.uint8).tobytes()).hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    import os
    os.replace(tmp, path)
