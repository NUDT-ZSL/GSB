"""确定性观测工程生成器：固定种子产出全部样本，不依赖外部影像。"""
from __future__ import annotations

import hashlib

import numpy as np

SEED = 42
BANDS = ["blue", "green", "red", "nir", "swir"]
REF_BAND = "green"
TRUE_COLOR_MAP = {"R": "red", "G": "green", "B": "blue"}
HEIGHT, WIDTH = 96, 128
BIT_DEPTH = 12
MAX_DN = 2 ** BIT_DEPTH - 1
OFFSET_LIMIT = 8.0  # 配准偏移超限阈值（像素）
NOISE_SIGMA = 1.0

# 已知地物区域（公共网格上的矩形）及其逐波段解析真值反射率
REGION_TRUTH = {
    "basalt": {"rect": (10, 30, 10, 40),
               "reflectance": {"blue": 0.12, "green": 0.13, "red": 0.15, "nir": 0.18, "swir": 0.20}},
    "vegetation": {"rect": (50, 75, 60, 100),
                   "reflectance": {"blue": 0.08, "green": 0.18, "red": 0.06, "nir": 0.55, "swir": 0.30}},
    "ice": {"rect": (60, 85, 15, 45),
            "reflectance": {"blue": 0.85, "green": 0.87, "red": 0.85, "nir": 0.70, "swir": 0.40}},
    "soil": {"rect": (20, 40, 80, 115),
             "reflectance": {"blue": 0.25, "green": 0.30, "red": 0.38, "nir": 0.42, "swir": 0.45}},
}

# 定标板采样点（公共网格坐标 + 基准反射率），逐波段再乘波段系数
PANEL_COMMON = [
    (20, 55, 0.05), (40, 20, 0.15), (70, 45, 0.30),
    (100, 25, 0.50), (110, 70, 0.70), (60, 78, 0.90),
]
PANEL_FACTOR = {"blue": 0.90, "green": 1.00, "red": 1.05, "nir": 1.10, "swir": 0.95}

# 逐波段传感器 / 几何 / 暗角 / 暗电流 / 偏移参数
BAND_PARAMS = {
    "blue":  {"gain": 3000.0, "path": 0.020, "E0": 1.00, "inc_deg": 25.0, "obs_deg": 5.0,
              "vignette": {"c2": 0.15, "c4": 0.05}, "dark": "zero",     "shift": (0, 0)},
    "green": {"gain": 3200.0, "path": 0.018, "E0": 0.98, "inc_deg": 26.0, "obs_deg": 6.0,
              "vignette": {"c2": 0.0,  "c4": 0.0},  "dark": "gradient", "shift": (0, 0)},
    "red":   {"gain": 3400.0, "path": 0.015, "E0": 0.95, "inc_deg": 27.0, "obs_deg": 4.0,
              "vignette": {"c2": 0.25, "c4": 0.08}, "dark": "pattern",  "shift": (2, -1)},
    "nir":   {"gain": 3600.0, "path": 0.010, "E0": 0.80, "inc_deg": 28.0, "obs_deg": 7.0,
              "vignette": {"c2": 0.20, "c4": 0.10}, "dark": "flat",     "shift": (-3, 2)},
    "swir":  {"gain": 3800.0, "path": 0.008, "E0": 0.60, "inc_deg": 30.0, "obs_deg": 8.0,
              "vignette": {"c2": 0.30, "c4": 0.12}, "dark": "ramp",     "shift": (15, 10)},
}

# 定标板污染：云影（DN 减半）与局部过曝（饱和）
CONTAMINATION = {
    "red": {"cloud": [2, 3], "saturate": [5]},
    "nir": {"saturate": [1, 2, 3, 5]},
}


def _smooth_field(rng, h=HEIGHT, w=WIDTH, coarse=8):
    g = rng.normal(0.0, 1.0, (coarse, coarse))
    rep = (h // coarse + 1, w // coarse + 1)
    up = np.kron(g, np.ones(rep))[:h, :w]
    for _ in range(3):  # 简单盒式平滑
        up = (up + np.roll(up, 1, 0) + np.roll(up, -1, 0)
              + np.roll(up, 1, 1) + np.roll(up, -1, 1)) / 5.0
    up -= up.mean()
    return up / (up.std() + 1e-12)


def dark_frame(kind, rng, h=HEIGHT, w=WIDTH):
    yy, xx = np.mgrid[0:h, 0:w]
    if kind == "zero":
        return np.zeros((h, w))
    if kind == "gradient":
        return np.broadcast_to(np.linspace(0.0, 40.0, w), (h, w)).copy()
    if kind == "pattern":
        return 30.0 + 10.0 * np.sin(2 * np.pi * xx / 32.0) * np.cos(2 * np.pi * yy / 24.0)
    if kind == "flat":
        return np.full((h, w), 50.0)
    if kind == "ramp":
        return 80.0 + 20.0 * yy / h
    raise ValueError(f"未知暗电流类型: {kind}")


def generate_project(seed=SEED):
    """生成完整观测工程（前向成像模型），返回 dict。"""
    rng = np.random.default_rng(seed)
    h, w = HEIGHT, WIDTH

    common = _smooth_field(rng)
    base = {}
    for i, b in enumerate(BANDS):
        base[b] = 0.30 + 0.02 * i + 0.12 * common \
            + 0.01 * _smooth_field(rng)
    for reg in REGION_TRUTH.values():
        y0, y1, x0, x1 = reg["rect"]
        for b in BANDS:
            base[b][y0:y1, x0:x1] = reg["reflectance"][b]

    bands_data = {}
    truth = {}
    for b in BANDS:
        p = BAND_PARAMS[b]
        dy, dx = p["shift"]
        scene = np.roll(base[b], (dy, dx), axis=(0, 1))
        panel = []
        for i, (px, py, rr) in enumerate(PANEL_COMMON):
            refl = min(0.95, rr * PANEL_FACTOR[b])
            bx, by = px + dx, py + dy  # 定标点随波段偏移落在各自像面
            scene[by - 1:by + 2, bx - 1:bx + 2] = refl
            panel.append({"id": i, "x": bx, "y": by, "reflectance": refl})

        from .pipeline import build_vignette  # 延迟导入避免循环
        vign = build_vignette(p["vignette"], h, w)
        dark = dark_frame(p["dark"], rng)
        cos_inc = float(np.cos(np.deg2rad(p["inc_deg"])))
        radiance = p["gain"] * (scene * cos_inc * p["E0"] + p["path"])
        dn = np.round(radiance * vign + dark + rng.normal(0.0, NOISE_SIGMA, (h, w)))

        cont = CONTAMINATION.get(b, {})
        for i in cont.get("cloud", []):
            pt = panel[i]
            dn[pt["y"] - 1:pt["y"] + 2, pt["x"] - 1:pt["x"] + 2] *= 0.5
        for i in cont.get("saturate", []):
            pt = panel[i]
            dn[pt["y"] - 1:pt["y"] + 2, pt["x"] - 1:pt["x"] + 2] = MAX_DN
        dn = np.clip(dn, 0, MAX_DN).astype(np.int32)

        bands_data[b] = {
            "dn": dn,
            "dark": dark,
            "vignette_params": dict(p["vignette"]),
            "panel": panel,
            "geometry": {"solar_incidence_deg": p["inc_deg"],
                         "observation_deg": p["obs_deg"],
                         "E0": p["E0"]},
            "sensor_gain": p["gain"],
        }
        truth[b] = {"a": p["gain"] * cos_inc * p["E0"], "b": p["gain"] * p["path"]}

    data_version = {b: hashlib.sha1(bands_data[b]["dn"].tobytes()).hexdigest()[:16]
                    for b in BANDS}
    return {
        "seed": seed,
        "bands": list(BANDS),
        "ref_band": REF_BAND,
        "true_color_map": dict(TRUE_COLOR_MAP),
        "height": h, "width": w,
        "bit_depth": BIT_DEPTH, "max_dn": MAX_DN,
        "offset_limit": OFFSET_LIMIT,
        "bands_data": bands_data,
        "regions": REGION_TRUTH,
        "truth": {"linear": truth, "regions": REGION_TRUTH},
        "data_version": data_version,
    }
