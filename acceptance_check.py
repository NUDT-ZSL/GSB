#!/usr/bin/env python3
"""端到端验收：双路径多档位、预算取舍、增量/全量一致、异常恢复。

运行：python3 acceptance_check.py
产物：samples/（确定性样本）、artifacts/acceptance/（渲染与报告）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import numpy as np

from vbench.config import ConfigError, load_config
from vbench.errors import ErrorCode
from vbench.pipeline import LatentPatch, Pipeline, RenderRequest
from vbench.report import build_report
from vbench.samplegen import generate_fault_suite, generate_valid_sample
from vbench.samples import load_clips
from tests._harness import (collect_frames, collect_raw_latents,
                            assert_frames_equal, assert_latents_equal)

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(ROOT, "samples", "valid")
FAULT_ROOT = os.path.join(ROOT, "samples", "faults")
OUT_ROOT = os.path.join(ROOT, "artifacts", "acceptance")


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def render(config, sample_dir: str, output_dir: str,
           prompt_overrides=None, latent_patches=None):
    clips, failures = load_clips(sample_dir, config)
    outcome = Pipeline(config).run(RenderRequest(
        sample_dir=sample_dir, output_dir=output_dir, clips=clips,
        load_failures=failures,
        prompt_overrides=prompt_overrides or {},
        latent_patches=latent_patches or []))
    report = build_report(config, outcome["results"], outcome["failures"],
                          outcome["decision"], outcome["manifest_version"])
    return outcome, report


def main() -> int:
    shutil.rmtree(OUT_ROOT, ignore_errors=True)
    os.makedirs(OUT_ROOT, exist_ok=True)
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -- {detail}")

    section("1. 生成确定性样本与异常注入用例")
    config_path = generate_valid_sample(SAMPLE_DIR, clip_count=4, frames=6,
                                        denoise_steps=8, replace_steps=3)
    faults = generate_fault_suite(FAULT_ROOT, SAMPLE_DIR)
    record("sample-generation", os.path.isfile(config_path),
           f"合法样本 + {len(faults)} 个异常用例")

    section("2. 双路径多档位全量渲染")
    config = load_config(config_path)
    outcome, report = render(
        config, SAMPLE_DIR, os.path.join(OUT_ROOT, "full"))
    report_path = os.path.join(OUT_ROOT, "full", "diagnostic_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
    record("dual-path-multi-tier",
           report["failure_summary"] == {},
           f"档位={report['tiers']} 片段={report['clip_count']} "
           f"版本={report['snapshot_version']}")
    record("shared-segment",
           all(item["shared_ok"]
               for item in report["shared_segment_judgement"]),
           "共享段逐元素相同、分歧段不同")
    record("tier-reuse-upsampling",
           all(any(r["upsampled"] for r in report["tier_reuse_records"]
                   if r["clip_id"] == c)
               for c in {r["clip_id"] for r in report["tier_reuse_records"]}),
           "高档位由真实上采样+后续去噪得到")

    section("3. 重复运行逐元素一致（确定性）")
    outcome_b, report_b = render(
        config, SAMPLE_DIR, os.path.join(OUT_ROOT, "full-repeat"))
    frames_equal = collect_frames(outcome["store"].root)
    frames_b = collect_frames(outcome_b["store"].root)
    try:
        assert_frames_equal(_AssertProxy(), frames_equal, frames_b)
        lat_equal = collect_raw_latents(outcome["store"].root)
        lat_b = collect_raw_latents(outcome_b["store"].root)
        assert_latents_equal(_AssertProxy(), lat_equal, lat_b)
        record("repeatability", True, "像素帧与中间潜变量逐元素一致")
    except AssertionError as exc:
        record("repeatability", False, str(exc))

    section("4. 增量编辑序列 vs 空白工作区全量")
    clips, _ = load_clips(SAMPLE_DIR, config)
    override = {clips[0].clip_id: "red triangle rotates fast"}
    patch = LatentPatch(clip_id=clips[1].clip_id, tier="128x128",
                        path="text", step=5, frame=0, channel=1, delta=0.375)
    inc_dir = os.path.join(OUT_ROOT, "incremental")
    render(config, SAMPLE_DIR, inc_dir)
    render(config, SAMPLE_DIR, inc_dir, prompt_overrides=override)
    inc_outcome, _ = render(config, SAMPLE_DIR, inc_dir,
                            prompt_overrides=override,
                            latent_patches=[patch])
    clean_dir = os.path.join(ROOT, "artifacts", "acceptance-clean")
    shutil.rmtree(clean_dir, ignore_errors=True)
    clean_outcome, _ = render(config, SAMPLE_DIR,
                              os.path.join(clean_dir, "clean"),
                              prompt_overrides=override,
                              latent_patches=[patch])
    try:
        assert_frames_equal(_AssertProxy(),
                            collect_frames(inc_outcome["store"].root),
                            collect_frames(clean_outcome["store"].root))
        assert_latents_equal(_AssertProxy(),
                             collect_raw_latents(inc_outcome["store"].root),
                             collect_raw_latents(clean_outcome["store"].root))
        record("incremental-equals-full", True,
               "提示词+潜变量补丁编辑后逐像素/逐潜变量一致")
    except AssertionError as exc:
        record("incremental-equals-full", False, str(exc))

    section("5. 预算与档位取舍")
    zero_config = load_config(os.path.join(
        faults["zero_budget"], "config.json"))
    zero_outcome, zero_report = render(
        zero_config, faults["zero_budget"],
        os.path.join(OUT_ROOT, "zero-budget"))
    codes = {item.code for item in zero_outcome["failures"]}
    record("zero-denoise-budget",
           ErrorCode.E_DENOISE_BUDGET_PENDING in codes,
           "预算为零 -> 全部档位挂起并保留清单")
    preview = Pipeline(config).preview_first_frame(RenderRequest(
        sample_dir=SAMPLE_DIR,
        output_dir=os.path.join(OUT_ROOT, "preview"),
        clips=clips, load_failures=[]))
    record("first-frame-preview", preview.get("code") is None,
           f"最低档首帧预览 {preview['preview']['estimated_cost_ms']}ms")
    pending_config = load_config(os.path.join(
        faults["zero_preview"], "config.json"))
    pending_clips, pending_failures = load_clips(
        faults["zero_preview"], pending_config)
    pending_preview = Pipeline(pending_config).preview_first_frame(
        RenderRequest(sample_dir=faults["zero_preview"],
                      output_dir=os.path.join(OUT_ROOT, "preview-pending"),
                      clips=pending_clips,
                      load_failures=pending_failures))
    record("preview-budget-pending",
           pending_preview.get("code") == ErrorCode.E_PREVIEW_BUDGET_PENDING,
           "首帧预算为零 -> 中间状态而非阻塞")

    section("6. 异常注入业务码")
    expected = {
        "missing_field": ErrorCode.E_CONFIG_INVALID,
        "bad_table_len": ErrorCode.E_CONFIG_INVALID,
        "bad_guidance": ErrorCode.E_CONFIG_INVALID,
        "non_monotonic": ErrorCode.E_CONFIG_INVALID,
        "frame_mismatch": ErrorCode.E_TIMESERIES_INVALID,
        "duplicate_id": ErrorCode.E_TIMESERIES_INVALID,
    }
    for name, expected_code in expected.items():
        sample = faults[name]
        try:
            fault_config = load_config(os.path.join(sample, "config.json"))
            _clips, fault_failures = load_clips(sample, fault_config)
            ok = any(item.code == expected_code for item in fault_failures)
            detail = expected_code
        except ConfigError as exc:
            ok = expected_code == ErrorCode.E_CONFIG_INVALID
            detail = f"{expected_code}: {exc}"
        record(f"fault-{name}", ok, detail)

    div_config = load_config(os.path.join(
        faults["path_divergence"], "config.json"))
    div_outcome, _ = render(
        div_config, faults["path_divergence"],
        os.path.join(OUT_ROOT, "divergence"))
    record("path-divergence-detection",
           any(item.code == ErrorCode.E_PATH_DIVERGENCE
               for item in div_outcome["failures"]),
           "共享段被污染 -> E_PATH_DIVERGENCE")

    section("7. 异常隔离与报告完整性")
    mismatch_config = load_config(config_path)
    mismatch_outcome, mismatch_report = render(
        mismatch_config, faults["frame_mismatch"],
        os.path.join(OUT_ROOT, "isolation"))
    rendered = len(mismatch_outcome["results"])
    record("failure-isolation", rendered == 3,
           f"1 个坏片段被隔离，其余 {rendered} 个合法片段完成渲染")
    required_keys = {"tier_frame_stats", "shared_segment_judgement",
                     "budget_consumption", "decision_record",
                     "snapshot_version", "failure_list"}
    record("report-schema", required_keys.issubset(report),
           "诊断报告含全部要求字段")

    summary = {
        "total": len(checks),
        "passed": sum(1 for item in checks if item["passed"]),
        "failed": sum(1 for item in checks if not item["passed"]),
        "checks": checks,
        "full_report": report_path,
    }
    with open(os.path.join(OUT_ROOT, "acceptance_summary.json"), "w",
              encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2,
                  sort_keys=True)
    section(f"验收结果 {summary['passed']}/{summary['total']} 通过")
    return 0 if summary["failed"] == 0 else 1


class _AssertProxy:
    def assertTrue(self, condition, msg: str = "") -> None:
        if not condition:
            raise AssertionError(msg)

    def assertEqual(self, left, right, msg: str = "") -> None:
        if left != right:
            raise AssertionError(msg or f"{left!r} != {right!r}")


if __name__ == "__main__":
    raise SystemExit(main())
