"""标定引擎：参数版本化缓存、增量重算、不可信标记与快照持久化。"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time
from pathlib import Path

import numpy as np

from . import generator as gen
from . import pipeline as pl


class SnapshotError(Exception):
    """快照损坏或字段缺失时抛出，消息中列出缺失项。"""


REQUIRED_MANIFEST_KEYS = ["schema", "saved_at", "params", "versions", "bands",
                          "registration", "untrusted", "regions", "composite"]
REQUIRED_BAND_KEYS = ["solve", "correction_summary"]
REQUIRED_SOLVE_KEYS = ["a", "b", "kept_ids", "rejected_ids", "rounds", "dof", "notes"]
SNAPSHOT_DIR = "snapshot"
SCHEMA = 1


def default_params(project):
    """从观测工程提取可替换的标定参数（定标点集合加暗角参数）。"""
    params = {}
    for b in project["bands"]:
        data = project["bands_data"][b]
        params[b] = {
            "panel": copy.deepcopy(data["panel"]),
            "vignette": dict(data["vignette_params"]),
        }
    return params


class Engine:
    """标定工作台内核：持有观测工程、标定参数、逐波段缓存与最新结果。"""

    def __init__(self, root):
        self.root = Path(root)
        self.cache_dir = self.root / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.project = gen.generate_project()
        self.params = default_params(self.project)
        self.results = None
        self.last_recompute = None

    def param_version(self, band):
        """版本号只依据标定参数快照内容（定标点加暗角参数加数据版本），不看文件时间。"""
        payload = {
            "panel": self.params[band]["panel"],
            "vignette": self.params[band]["vignette"],
            "data_version": self.project["data_version"][band],
        }
        blob = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha1(blob).hexdigest()[:16]

    def replace_panel_point(self, band, idx, x, y, reflectance):
        pt = self.params[band]["panel"][idx]
        pt.update({"x": int(x), "y": int(y), "reflectance": float(reflectance)})

    def replace_panel_points(self, band, points):
        self.params[band]["panel"] = copy.deepcopy(points)

    def replace_vignette(self, band, c2, c4):
        self.params[band]["vignette"] = {"c2": float(c2), "c4": float(c4)}

    def _calibrate_band(self, band, version):
        data = self.project["bands_data"][band]
        h, w = self.project["height"], self.project["width"]
        vign = pl.build_vignette(self.params[band]["vignette"], h, w)
        corrected, summary = pl.correct_band(data["dn"], data["dark"], vign)
        max_dn = self.project["max_dn"]
        saturated = [int(p["id"]) for p in self.params[band]["panel"]
                     if data["dn"][p["y"], p["x"]] >= max_dn]
        solve = pl.solve_band(corrected, self.params[band]["panel"],
                              saturated_ids=saturated)
        refl = pl.reflectance_map(corrected, solve["a"], solve["b"])
        return {"band": band, "version": version, "corrected": corrected,
                "reflectance": refl, "solve": solve, "correction_summary": summary}

    def _cache_path(self, band):
        return self.cache_dir / (band + ".npz")

    def _load_cache(self, band, version):
        path = self._cache_path(band)
        meta_path = self.cache_dir / (band + ".json")
        if not (path.exists() and meta_path.exists()):
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("version") != version:
            return None
        arrays = np.load(path)
        return {"band": band, "version": version,
                "corrected": arrays["corrected"], "reflectance": arrays["reflectance"],
                "solve": meta["solve"], "correction_summary": meta["correction_summary"]}

    def _save_cache(self, result):
        band = result["band"]
        np.savez(self.cache_dir / (band + ".npz"),
                 corrected=result["corrected"], reflectance=result["reflectance"])
        meta = {"version": result["version"], "solve": result["solve"],
                "correction_summary": result["correction_summary"]}
        (self.cache_dir / (band + ".json")).write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def recompute(self):
        """增量重算：仅参数版本变化的波段重新标定，其余波段复用缓存（逐字节一致）。"""
        bands = self.project["bands"]
        versions = {b: self.param_version(b) for b in bands}
        band_results, recomputed, cached = {}, [], []
        for b in bands:
            hit = self._load_cache(b, versions[b])
            if hit is not None:
                band_results[b] = hit
                cached.append(b)
            else:
                band_results[b] = self._calibrate_band(b, versions[b])
                self._save_cache(band_results[b])
                recomputed.append(b)

        ref = self.project["ref_band"]
        limit = self.project["offset_limit"]
        h, w = self.project["height"], self.project["width"]
        panel_mask = np.ones((h, w), dtype=bool)
        for p in self.params[ref]["panel"]:
            panel_mask[max(0, p["y"] - 2):p["y"] + 3,
                       max(0, p["x"] - 2):p["x"] + 3] = False
        registration, aligned = {}, {}
        for b in bands:
            if b == ref:
                dy, dx, strength = 0, 0, 1.0
            else:
                dy, dx, strength = pl.estimate_shift(
                    band_results[ref]["reflectance"], band_results[b]["reflectance"])
            exceeded = bool(max(abs(dy), abs(dx)) > limit)
            al = pl.shift_with_fill(band_results[b]["reflectance"], -dy, -dx)
            aligned[b] = al
            registration[b] = {
                "dy": dy, "dx": dx, "peak_strength": strength,
                "offset_exceeded": exceeded,
                "resampled_to": ref, "fill": "nan",
                "align_residual": pl.alignment_residual(
                    band_results[ref]["reflectance"], al, panel_mask),
            }

        untrusted = {}
        max_dn = self.project["max_dn"]
        for b in bands:
            data = self.project["bands_data"][b]
            reasons = []
            sat = [int(p["id"]) for p in self.params[b]["panel"]
                   if data["dn"][p["y"], p["x"]] >= max_dn]
            if sat:
                reasons.append({"reason": "panel_local_overexposure",
                                "detail": "定标板局部过曝，饱和定标点 %s" % sat})
            kept = band_results[b]["solve"]["n_points"]
            if kept < pl.MIN_POINTS:
                reasons.append({"reason": "insufficient_calibration_points",
                                "detail": "剔除后仅剩 %d 个定标点（要求 >= %d）"
                                          % (kept, pl.MIN_POINTS)})
            if registration[b]["offset_exceeded"]:
                reasons.append({"reason": "registration_offset_exceeded",
                                "detail": "偏移 (dy=%d, dx=%d) 超过阈值 %.1f 像素"
                                          % (registration[b]["dy"],
                                             registration[b]["dx"], limit)})
            if reasons:
                untrusted[b] = reasons

        regions = pl.region_report(aligned, self.project["regions"], bands)
        composite, pre_clip, comp_record = pl.composite_true_color(
            aligned, untrusted, self.project["true_color_map"], bands, ref)

        self.results = {
            "versions": versions,
            "bands": band_results,
            "registration": registration,
            "aligned": aligned,
            "untrusted": untrusted,
            "regions": regions,
            "composite": composite,
            "composite_pre": pre_clip,
            "composite_record": comp_record,
        }
        self.last_recompute = {"recomputed": recomputed, "cached": cached,
                               "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        return self.last_recompute

    def save_snapshot(self):
        """把标定参数、残差、配准参数、不可信标记与区域偏差报告持久化为快照。"""
        if self.results is None:
            raise RuntimeError("尚未重算，无可保存的结果")
        snap = self.root / SNAPSHOT_DIR
        tmp = self.root / (SNAPSHOT_DIR + ".tmp")
        old = self.root / (SNAPSHOT_DIR + ".old")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        manifest = {
            "schema": SCHEMA,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "params": self.params,
            "versions": self.results["versions"],
            "bands": {b: {"solve": self.results["bands"][b]["solve"],
                          "correction_summary": self.results["bands"][b]["correction_summary"]}
                      for b in self.project["bands"]},
            "registration": self.results["registration"],
            "untrusted": self.results["untrusted"],
            "regions": self.results["regions"],
            "composite": self.results["composite_record"],
        }
        (tmp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        for b in self.project["bands"]:
            np.save(tmp / ("reflectance_%s.npy" % b),
                    self.results["bands"][b]["reflectance"])
            np.save(tmp / ("corrected_%s.npy" % b),
                    self.results["bands"][b]["corrected"])
        np.save(tmp / "composite.npy", self.results["composite"])
        np.save(tmp / "composite_pre.npy", self.results["composite_pre"])
        if old.exists():
            shutil.rmtree(old)
        if snap.exists():
            snap.rename(old)
        tmp.rename(snap)
        if old.exists():
            shutil.rmtree(old)
        return snap

    def load_snapshot(self):
        """从快照恢复标定状态；字段缺失或损坏时拒绝加载并指出缺失项。"""
        snap = self.root / SNAPSHOT_DIR
        manifest_path = snap / "manifest.json"
        if not manifest_path.exists():
            raise SnapshotError("快照缺失项: manifest.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SnapshotError("快照损坏: manifest.json 不是合法 JSON (%s)" % exc) from exc

        missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in manifest]
        if missing:
            raise SnapshotError("快照缺失字段: " + ", ".join(missing))
        for b in self.project["bands"]:
            band_rec = manifest["bands"].get(b)
            if band_rec is None:
                raise SnapshotError("快照缺失字段: bands.%s" % b)
            lack = [k for k in REQUIRED_BAND_KEYS if k not in band_rec]
            if lack:
                raise SnapshotError("快照缺失字段: " +
                                    ", ".join("bands.%s.%s" % (b, k) for k in lack))
            lack = [k for k in REQUIRED_SOLVE_KEYS if k not in band_rec["solve"]]
            if lack:
                raise SnapshotError("快照缺失字段: " +
                                    ", ".join("bands.%s.solve.%s" % (b, k) for k in lack))
            for arr in ("reflectance_%s.npy" % b, "corrected_%s.npy" % b):
                if not (snap / arr).exists():
                    raise SnapshotError("快照缺失文件: %s" % arr)
        for arr in ("composite.npy", "composite_pre.npy"):
            if not (snap / arr).exists():
                raise SnapshotError("快照缺失文件: %s" % arr)

        self.params = manifest["params"]
        band_results = {}
        for b in self.project["bands"]:
            band_results[b] = {
                "band": b,
                "version": manifest["versions"][b],
                "corrected": np.load(snap / ("corrected_%s.npy" % b)),
                "reflectance": np.load(snap / ("reflectance_%s.npy" % b)),
                "solve": manifest["bands"][b]["solve"],
                "correction_summary": manifest["bands"][b]["correction_summary"],
            }
        aligned = {}
        for b in self.project["bands"]:
            reg = manifest["registration"][b]
            aligned[b] = pl.shift_with_fill(band_results[b]["reflectance"],
                                            -reg["dy"], -reg["dx"])
        self.results = {
            "versions": manifest["versions"],
            "bands": band_results,
            "registration": manifest["registration"],
            "aligned": aligned,
            "untrusted": manifest["untrusted"],
            "regions": manifest["regions"],
            "composite": np.load(snap / "composite.npy"),
            "composite_pre": np.load(snap / "composite_pre.npy"),
            "composite_record": manifest["composite"],
        }
        return manifest
