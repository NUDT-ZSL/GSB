"""End-to-end diagnostic pipeline.

Stages: load -> extract -> classify -> describe -> parse -> inject ->
convert -> render -> snapshot -> report. Region tasks run on a thread pool
sized by config["concurrency"]; every task carries a stable sort key derived
from (image file name, region index) so the final ordering is identical to a
single-threaded run regardless of completion order.
"""

from __future__ import annotations

import io
import json
import os
import platform
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from . import v1io, v2io
from .classify import classify_region
from .convert import (downgrade_v2_to_v1, ir_from_v1, ir_from_v2,
                      roundtrip_v1_v2_v1, roundtrip_v2_v1_v2,
                      upgrade_v1_to_v2)
from .errors import E_RELATION_DANGLING, WorkbenchError
from .features import extract_regions, load_png
from .inject import EXPECTED_CODES, apply_injection
from .render import boundary_diagnostics, diff_overlays, render_overlay
from .snapshot import (OVERLAY_VERSION, SNAPSHOT_GLOBAL_VERSION,
                       SnapshotManager, sha256_file)


class _Timer:
    def __init__(self):
        self.timings: dict[str, int] = {}
        self._marks: dict[str, float] = {}

    def start(self, stage: str) -> None:
        self._marks[stage] = time.perf_counter()

    def stop(self, stage: str) -> None:
        self.timings[stage] = self.timings.get(stage, 0) + int(
            round((time.perf_counter() - self._marks.pop(stage)) * 1000))


def _run_tasks(tasks, max_workers: int, stop_event=None, on_complete=None):
    """Run callables (sort_key, fn); return results sorted by sort_key.

    Stops early (cancelling pending futures) when stop_event is set; the
    second return value flags whether an interruption happened.
    """
    results, interrupted = [], False
    if max_workers <= 1 and stop_event is None:
        for key, fn in tasks:
            results.append((key, fn()))
            if on_complete:
                on_complete(key)
        results.sort(key=lambda kv: kv[0])
        return results, interrupted
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(fn): key for key, fn in tasks}
        pending = dict(futures)
        for fut, key in list(pending.items()):
            if stop_event is not None and stop_event.is_set():
                interrupted = True
                break
            results.append((key, fut.result()))
            del pending[fut]
            if on_complete:
                on_complete(key)
        for fut in pending:
            fut.cancel()
    results.sort(key=lambda kv: kv[0])
    return results, interrupted


def _check_writable(directory) -> bool:
    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".wb_probe_")
        os.close(fd)
        os.unlink(tmp)
        return True
    except OSError:
        return False


def _describe(ir, config) -> str:
    template = sorted(config["template_weights"].items(),
                      key=lambda kv: (-kv[1], kv[0]))[0][0]
    if template == "compact":
        return f"{ir.inst_id}:{ir.category}@{ir.bbox}~{ir.conf_permille}/1000"
    return (f"Region {ir.inst_id} is a {ir.category} at bbox {ir.bbox} "
            f"with confidence {ir.conf_permille} permille.")


def _validate_relations(irs, relation_ids, source_name, failures):
    for ir in irs:
        for ref in ir.relations:
            if ref not in relation_ids:
                failures.append({
                    "image": source_name, "region_index": ir.sort_key[1],
                    "stage": "validate", "code": E_RELATION_DANGLING,
                    "detail": f"{ir.inst_id}: relation ref '{ref}' not in "
                              "relation table",
                })


def _png_bytes(arr) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="PNG")
    return buf.getvalue()


def run_pipeline(data_dir, out_dir, serial=False, stop_event=None,
                 progress_hook=None, global_version=SNAPSHOT_GLOBAL_VERSION):
    """Run the full diagnostic pipeline. Returns (exit_code, result_dict)."""
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timer, failures = _Timer(), []
    mode = "serial" if serial else "parallel"

    timer.start("load")
    config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    relations = json.loads((data_dir / "relations.json").read_text(encoding="utf-8"))
    relation_ids = {r["id"] for r in relations}
    v1_text = (data_dir / "exchange_v1.txt").read_text(encoding="utf-8")
    v2_text = (data_dir / "exchange_v2.json").read_text(encoding="utf-8")
    injections = json.loads((data_dir / "injection.json").read_text(encoding="utf-8"))
    config_hash = sha256_file(data_dir / "config.json")
    relations_hash = sha256_file(data_dir / "relations.json")
    width, height = config["image"]["width"], config["image"]["height"]
    max_workers = 1 if serial else max(1, int(config["concurrency"]))
    timer.stop("load")

    environment = {
        "python": platform.python_version(),
        "platform": platform.system(),
        "workdir_writable": _check_writable(out_dir),
        "tempdir_writable": _check_writable(tempfile.gettempdir()),
    }

    snap = SnapshotManager(out_dir / "snapshot")
    if snap.is_hit(global_version, config_hash, relations_hash):
        # Cache hit: reuse the fully replaced snapshot; preserve the previous
        # full diagnostic report and only refresh the snapshot marker.
        existing = {}
        report_path = out_dir / "report.json"
        if report_path.exists():
            try:
                existing = json.loads(report_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
        report = {
            **existing,
            "run_id": f"seed-{config['seed']}", "mode": mode,
            "environment": environment,
            "snapshot": {"global_version": global_version, "hit": True,
                         "status": "complete"},
        }
        _atomic_write_json(report_path, report)
        return 0, {"report": report, "canonical": {"snapshot_hit": True}}

    # ---- extract + classify (concurrent, stable ordering) -----------------
    timer.start("extract")
    image_paths = sorted((data_dir / "images").glob("*.png"))
    extract_tasks = []
    for path in image_paths:
        def make(p):
            def job():
                arr = load_png(p, config)
                return extract_regions(p.name, arr, config)
            return job
        extract_tasks.append(((path.name, -1), make(path)))
    extract_results, interrupted = _run_tasks(
        extract_tasks, max_workers, stop_event, progress_hook)
    all_regions = []
    for _key, (regions, fails) in extract_results:
        all_regions.extend(regions)
        failures.extend(fails)
    all_regions.sort(key=lambda f: f.sort_key)
    timer.stop("extract")

    timer.start("classify")
    classify_tasks = []
    for feat in all_regions:
        def make(f):
            def job():
                cat, conf, breakdown = classify_region(f, config)
                return f, cat, conf, breakdown
            return job
        classify_tasks.append((feat.sort_key, make(feat)))
    classify_results, cls_interrupted = _run_tasks(
        classify_tasks, max_workers, stop_event, progress_hook)
    interrupted = interrupted or cls_interrupted
    classified = [r for _k, r in classify_results]
    timer.stop("classify")

    # ---- describe + serialize (deterministic order) -----------------------
    timer.start("describe")
    from .model import RegionDesc
    gen_irs, descriptions = [], []
    for feat, cat, conf, breakdown in classified:
        stem = Path(feat.image).stem
        ir = RegionDesc(
            inst_id=f"{stem}#r{feat.region_index:03d}", category=cat,
            bbox=feat.bbox, conf_permille=conf,
            source_version=config["target_format_version"],
            orig_bbox=tuple(float(v) for v in feat.bbox),
            orig_confidence=conf / 1000.0, sort_key=feat.sort_key,
        )
        gen_irs.append(ir)
        descriptions.append({
            "inst_id": ir.inst_id, "image": feat.image,
            "region_index": feat.region_index, "category": cat,
            "conf_permille": conf, "bbox": list(feat.bbox),
            "text": _describe(ir, config), "score_breakdown": breakdown,
        })
    gen_v1_records = [{"inst_id": ir.inst_id, "category": ir.category,
                       "bbox": list(ir.bbox), "conf_permille": ir.conf_permille}
                      for ir in gen_irs]
    gen_v1_text = v1io.serialize_v1(gen_v1_records)
    up_insts, up_colors, up_sidecar = upgrade_v1_to_v2(gen_irs)
    gen_v2_text = v2io.serialize_v2(up_insts, up_colors)
    timer.stop("describe")

    # ---- parse exchange files into unified IR ------------------------------
    timer.start("parse")
    v1_records, v1_errors = v1io.parse_v1(v1_text)
    for e in v1_errors:
        failures.append({"image": "exchange_v1.txt", "region_index": e["line"],
                         "stage": "parse", "code": e["code"],
                         "detail": e["detail"]})
    irs_v1 = [ir_from_v1(r, sort_key=("exchange_v1.txt", i))
              for i, r in enumerate(v1_records)]
    v2_insts, v2_colors, v2_errors = parse_exchange_v2(v2_text, config)
    for e in v2_errors:
        failures.append({"image": "exchange_v2.json",
                         "region_index": e.get("index"),
                         "stage": "parse", "code": e["code"],
                         "detail": e["detail"]})
    irs_v2 = [ir_from_v2(inst, sort_key=("exchange_v2.json", i))
              for i, inst in enumerate(v2_insts)]
    _validate_relations(irs_v2, relation_ids, "exchange_v2.json", failures)
    timer.stop("parse")

    # ---- fault injection (isolated per spec) -------------------------------
    timer.start("inject")
    inj_dir = out_dir / "injected"
    inj_dir.mkdir(exist_ok=True)
    injection_report = []
    for spec in injections:
        codes = []
        try:
            m_v1, m_v2 = apply_injection(spec, v1_text, v2_text, width)
            (inj_dir / f"{spec['id']}_v1.txt").write_text(m_v1, encoding="utf-8")
            (inj_dir / f"{spec['id']}_v2.json").write_text(m_v2, encoding="utf-8")
            _r, errs1 = v1io.parse_v1(m_v1)
            insts, _c, errs2 = parse_exchange_v2(m_v2, config)
            codes.extend(e["code"] for e in errs1)
            codes.extend(e["code"] for e in errs2)
            inj_irs = [ir_from_v2(i2, sort_key=(spec["id"], n))
                       for n, i2 in enumerate(insts)]
            dangling = []
            _validate_relations(inj_irs, relation_ids, spec["id"], dangling)
            codes.extend(f["code"] for f in dangling)
        except Exception as exc:  # injection must never abort the run
            codes.append(f"E_INTERNAL:{exc}")
        expected = EXPECTED_CODES[spec["type"]]
        injection_report.append({
            "id": spec["id"], "type": spec["type"],
            "expected_code": expected, "codes": codes,
            "hit": expected in codes,
        })
    timer.stop("inject")

    # ---- bidirectional conversion + round-trip checks ----------------------
    timer.start("convert")
    conv_instances, conv_colors, conv_sidecar = upgrade_v1_to_v2(irs_v1)
    down_records, loss_ledger = downgrade_v2_to_v1(irs_v2)
    rt = {
        "v1_v2_v1": roundtrip_v1_v2_v1(irs_v1),
        "v2_v1_v2": roundtrip_v2_v1_v2(irs_v2),
    }
    timer.stop("convert")

    # ---- cross-version render consistency + boundary diagnostics -----------
    timer.start("render")
    overlay_v1 = render_overlay(irs_v1, width, height)
    overlay_v2 = render_overlay(irs_v2, width, height)
    render_diff = diff_overlays(overlay_v1, overlay_v2)
    boundary = boundary_diagnostics(irs_v2, config)
    timer.stop("render")

    # ---- versioned snapshot (whole-directory atomic replacement) -----------
    timer.start("snapshot")
    completed = [ir.inst_id for ir in gen_irs]
    manifest = {
        "global_version": global_version,
        "config_hash": config_hash,
        "relations_hash": relations_hash,
        "overlay_version": OVERLAY_VERSION,
        "status": "interrupted" if interrupted else "complete",
        "regions": {ir.inst_id: {"desc_version": ir.source_version}
                    for ir in gen_irs},
        "progress": {"completed_regions": completed,
                     "total_regions": len(all_regions)},
    }
    artifacts = {
        "descriptions.json": json.dumps(descriptions, indent=2,
                                        ensure_ascii=False).encode("utf-8"),
        "descriptions_v1.txt": gen_v1_text.encode("utf-8"),
        "descriptions_v2.json": gen_v2_text.encode("utf-8"),
        "upgrade_sidecar.json": json.dumps(conv_sidecar, indent=2).encode("utf-8"),
        "overlay_v1.png": _png_bytes(overlay_v1),
        "overlay_v2.png": _png_bytes(overlay_v2),
    }
    snap.write(manifest, artifacts)
    timer.stop("snapshot")

    # ---- diagnostic report (atomic replace) --------------------------------
    timer.start("report")
    failures.sort(key=lambda f: (str(f["image"]),
                                 -1 if f["region_index"] is None
                                 else f["region_index"],
                                 f["stage"], f["code"]))
    report = {
        "run_id": f"seed-{config['seed']}",
        "mode": mode,
        "environment": environment,
        "snapshot": {"global_version": global_version, "hit": False,
                     "status": manifest["status"],
                     "config_hash": config_hash,
                     "relations_hash": relations_hash},
        "regions": {"extracted": len(all_regions),
                    "classified": len(classified)},
        "render_diff": render_diff,
        "round_trip": rt,
        "downgrade_loss": loss_ledger,
        "boundary_sensitive": boundary,
        "failures": failures,
        "injections": injection_report,
        "stage_timings_ms": timer.timings,
    }
    _atomic_write_json(out_dir / "report.json", report)
    timer.stop("report")

    canonical = {
        "regions": [(f.image, f.region_index, f.area, f.bbox,
                     tuple(round(c, 6) for c in f.mean_color),
                     round(f.compactness, 9)) for f in all_regions],
        "classified": [(f.image, f.region_index, c, cf)
                       for f, c, cf, _b in classified],
        "gen_v1_text": gen_v1_text,
        "gen_v2_text": gen_v2_text,
        "downgrade_loss": loss_ledger,
        "failures": failures,
        "boundary_sensitive": boundary,
        "render_diff": render_diff,
        "round_trip": rt,
        "interrupted": interrupted,
    }
    return 0, {"report": report, "canonical": canonical}


def parse_exchange_v2(text: str, config):
    return v2io.parse_v2(text, (config["image"]["width"],
                                config["image"]["height"]))


def _atomic_write_json(path, obj) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
