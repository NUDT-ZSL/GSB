"""测试共享夹具与断言辅助。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from PIL import Image

from vbench.config import Config, load_config
from vbench.pipeline import LatentPatch, Pipeline, RenderRequest
from vbench.report import build_report
from vbench.samplegen import generate_fault_suite, generate_valid_sample
from vbench.samples import load_clips


class Workspace:
    def __init__(self, root: str, **kwargs: Any) -> None:
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.sample_dir = os.path.join(root, "samples")
        self.config_path = generate_valid_sample(self.sample_dir, **kwargs)
        self.fault_dir = os.path.join(root, "faults")
        self.faults = generate_fault_suite(self.fault_dir, self.sample_dir)

    def config(self, name: str | None = None) -> Config:
        if name is None:
            return load_config(self.config_path)
        return load_config(os.path.join(self.fault_dir, name, "config.json"))

    def clips(self, config: Config, sample_dir: str | None = None):
        return load_clips(sample_dir or self.sample_dir, config)

    def output(self, *parts: str) -> str:
        path = os.path.join(self.root, *parts)
        os.makedirs(path, exist_ok=True)
        return path


def fresh_workspace(parent: str, name: str, **kwargs: Any) -> Workspace:
    root = os.path.join(parent, name)
    shutil.rmtree(root, ignore_errors=True)
    return Workspace(root, **kwargs)


def full_render(ws: Workspace, output_name: str,
                config: Config | None = None,
                sample_dir: str | None = None,
                prompt_overrides: Dict[str, str] | None = None,
                latent_patches: List[LatentPatch] | None = None,
                stop_event=None) -> Dict[str, Any]:
    config = config or ws.config()
    clips, failures = ws.clips(config, sample_dir)
    request = RenderRequest(
        sample_dir=sample_dir or ws.sample_dir,
        output_dir=ws.output(output_name), clips=clips,
        load_failures=failures,
        prompt_overrides=prompt_overrides or {},
        latent_patches=latent_patches or [],
        stop_event=stop_event)
    pipeline = Pipeline(config)
    outcome = pipeline.run(request)
    report = build_report(config, outcome["results"], outcome["failures"],
                          outcome["decision"], outcome["manifest_version"])
    outcome["report"] = report
    return outcome


def load_png(path: str) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def collect_frames(output_dir: str) -> Dict[str, np.ndarray]:
    frames: Dict[str, np.ndarray] = {}
    frame_root = os.path.join(output_dir, "frames")
    for clip_id in sorted(os.listdir(frame_root)):
        for tier in sorted(os.listdir(os.path.join(frame_root, clip_id))):
            tier_dir = os.path.join(frame_root, clip_id, tier)
            for file_name in sorted(os.listdir(tier_dir)):
                if file_name.endswith(".png"):
                    key = f"{clip_id}/{tier}/{file_name}"
                    frames[key] = load_png(os.path.join(tier_dir, file_name))
    return frames


def collect_raw_latents(output_dir: str) -> Dict[str, np.ndarray]:
    latents: Dict[str, np.ndarray] = {}
    stage_root = os.path.join(output_dir, "stages")
    for dirpath, _dirs, files in os.walk(stage_root):
        for file_name in sorted(files):
            if file_name.endswith(".raw"):
                rel = os.path.relpath(os.path.join(dirpath, file_name),
                                      stage_root)
                latents[rel] = np.fromfile(
                    os.path.join(dirpath, file_name), dtype=np.float32).copy()
    return latents


def assert_frames_equal(test_case, left: Dict[str, np.ndarray],
                        right: Dict[str, np.ndarray]) -> None:
    test_case.assertEqual(sorted(left), sorted(right))
    for key in left:
        test_case.assertTrue(
            np.array_equal(left[key], right[key]),
            msg=f"像素帧不一致: {key}")


def assert_latents_equal(test_case, left: Dict[str, np.ndarray],
                         right: Dict[str, np.ndarray]) -> None:
    test_case.assertEqual(sorted(left), sorted(right))
    for key in left:
        test_case.assertTrue(
            np.array_equal(left[key], right[key]),
            msg=f"中间潜变量不一致: {key}")
