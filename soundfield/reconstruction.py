"""逐频段镜像声源冲激响应重建（定点主路径 + float64 参考路径）。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import errors as E
from .config import (
    ENERGY_RESIDUAL_TOL,
    FP_SCALE,
    NEGLIGIBLE_ENERGY,
    SPEED_OF_SOUND,
    Spec,
)
from .fixedpoint import chain_mul_q, energy_f64, energy_q, quantize, quantize_array
from .frd import apply_fractional_delay, design_filter, split_delay
from .images import ImageSource, enumerate_images
from .scene import Scene


@dataclass(frozen=True)
class BlockId:
    """重建分块标识：频段 × 镜像阶数 × 扬声器。"""

    band: int
    order: int
    speaker: int

    def name(self) -> str:
        return f"b{self.band:02d}_o{self.order:02d}_s{self.speaker:02d}"

    @classmethod
    def all_blocks(cls, scene: Scene) -> list["BlockId"]:
        # 确定性分块顺序：频段 -> 阶数 -> 扬声器。
        return [
            cls(band=band, order=order, speaker=speaker)
            for band in range(scene.band_count)
            for order in range(scene.reconstruction_order + 1)
            for speaker in range(len(scene.speakers))
        ]


@dataclass
class ContributionRecord:
    """真实执行过的逐镜像累加记录（仅保留越限采样点，非事后重演）。"""

    image_index: int
    order: int
    band: int
    speaker: int
    sample: int
    fixed_value: int
    float_value: float
    fixed_contribution: int
    float_contribution: float
    residual_contribution: float


@dataclass
class BlockResult:
    block: BlockId
    ir_q: np.ndarray
    ir_f: np.ndarray
    records: list[ContributionRecord]
    image_summaries: list[dict]
    first_energy_violation: dict | None
    block_energy_q: int
    block_energy_f: float


def ir_length_for(scene: Scene, images: list[ImageSource]) -> int:
    """确定冲激响应长度：覆盖最长镜像路径，并给分数延迟留出抽头余量。"""

    if scene.ir_length_samples > 0:
        return scene.ir_length_samples
    max_delay = 0.0
    for image in images:
        image_pos = np.asarray(image.position, dtype=np.float64)
        for speaker in scene.speakers:
            distance = float(np.linalg.norm(image_pos - np.asarray(speaker.position, dtype=np.float64)))
            max_delay = max(max_delay, distance / SPEED_OF_SOUND * scene.sample_rate)
    from .config import FRAC_TAPS

    return int(math.ceil(max_delay) + FRAC_TAPS + 2)


def _weights(scene: Scene, image: ImageSource, distance: float, band: int) -> tuple[int, float]:
    """频段幅度权重 = 路径距离衰减 × 反射链吸声乘积 × 频段增益。"""

    if distance <= 0.0:
        raise E.BusinessError(
            E.E_INTERNAL,
            "镜像声源到扬声器路径长度非正",
            {"image_index": image.image_index, "distance": distance},
        )
    atten_f = 1.0 / distance
    gain_f = scene.band_gains_f[band]
    weight_f = atten_f * image.absorption_f[band] * gain_f

    atten_q = quantize(atten_f)
    gain_q = quantize(gain_f)
    weight_q = chain_mul_q([atten_q, image.absorption_q[band], gain_q])
    return weight_q, weight_f


def compute_block(
    scene: Scene,
    images: list[ImageSource],
    block: BlockId,
    ir_length: int,
    amp_tol_q: int,
    energy_tol_q2: int,
) -> BlockResult:
    """计算单个分块。纯函数式：相同输入逐采样点相同，与并发度无关。"""

    band = block.band
    order = block.order
    speaker = block.speaker
    speaker_pos = np.asarray(scene.speakers[speaker].position, dtype=np.float64)

    ir_q = np.zeros(ir_length, dtype=object)
    ir_f = np.zeros(ir_length, dtype=np.float64)
    records: list[ContributionRecord] = []
    summaries: list[dict] = []
    first_violation: dict | None = None

    order_images = [img for img in images if img.order == order]
    for image in order_images:
        image_pos = np.asarray(image.position, dtype=np.float64)
        distance = float(np.linalg.norm(image_pos - speaker_pos))
        delay_samples = distance / SPEED_OF_SOUND * scene.sample_rate
        offset, frac = split_delay(delay_samples)
        coeffs_f, coeffs_q = design_filter(frac)
        weight_q, weight_f = _weights(scene, image, distance, band)

        contrib_q = apply_fractional_delay(ir_length, weight_q, coeffs_q, offset)
        contrib_f = np.zeros(ir_length, dtype=np.float64)
        weighted_f = weight_f * coeffs_f
        stop = min(offset + len(weighted_f), ir_length)
        if offset < ir_length:
            contrib_f[offset:stop] = weighted_f[: stop - offset]

        ir_q += contrib_q
        ir_f += contrib_f

        reference_q = np.round(ir_f * FP_SCALE).astype(np.int64)
        violating = [
            sample
            for sample in range(ir_length)
            if abs(int(ir_q[sample]) - int(reference_q[sample])) > amp_tol_q
        ]
        for sample in violating:
            sample = int(sample)
            records.append(
                ContributionRecord(
                    image_index=image.image_index,
                    order=order,
                    band=band,
                    speaker=speaker,
                    sample=sample,
                    fixed_value=int(ir_q[sample]),
                    float_value=float(ir_f[sample]),
                    fixed_contribution=int(contrib_q[sample]),
                    float_contribution=float(contrib_f[sample]),
                    residual_contribution=float(
                        abs(int(contrib_q[sample]) / FP_SCALE - contrib_f[sample])
                    ),
                )
            )

        img_energy_q = energy_q(contrib_q)
        img_energy_f = energy_f64(contrib_f)
        img_energy_diff = abs(img_energy_q / (FP_SCALE * FP_SCALE) - img_energy_f)
        negligible_q = img_energy_q <= Spec.negligible_energy_q()
        negligible_f = img_energy_f <= NEGLIGIBLE_ENERGY
        summary = {
            "image_index": image.image_index,
            "source_id": image.source_id,
            "order": order,
            "band": band,
            "speaker": speaker,
            "distance_m": distance,
            "delay_samples": delay_samples,
            "integer_offset": offset,
            "fractional_delay": frac,
            "weight_fixed_q": weight_q,
            "weight_float64": weight_f,
            "weight_abs_diff": abs(weight_q / FP_SCALE - weight_f),
            "energy_fixed": img_energy_q / (FP_SCALE * FP_SCALE),
            "energy_float64": img_energy_f,
            "energy_abs_diff": img_energy_diff,
            "negligible_fixed_only": negligible_q,
            "negligible_float64_diagnostic": negligible_f,
            "decision_flip": negligible_q != negligible_f,
        }
        summaries.append(summary)
        if first_violation is None and img_energy_diff > ENERGY_RESIDUAL_TOL:
            first_violation = {
                "image_index": image.image_index,
                "order": order,
                "band": band,
                "speaker": speaker,
                "energy_abs_diff": img_energy_diff,
                "tolerance": ENERGY_RESIDUAL_TOL,
            }

    return BlockResult(
        block=block,
        ir_q=ir_q,
        ir_f=ir_f,
        records=records,
        image_summaries=summaries,
        first_energy_violation=first_violation,
        block_energy_q=energy_q(ir_q),
        block_energy_f=energy_f64(ir_f),
    )


def merge_blocks(
    scene: Scene,
    results: list[BlockResult],
    ir_length: int,
) -> tuple[np.ndarray, np.ndarray, list[ContributionRecord], list[dict], dict | None]:
    """按规范分块顺序合并为 (扬声器, 频段, 采样) 的双路径冲激响应。

    定点整数加法满足结合律，任意分块/并发划分合并后逐采样点一致；
    float64 参考路径按 BlockId 字典序求和，保证可重复。
    """

    n_speakers = len(scene.speakers)
    ir_q = np.zeros((n_speakers, scene.band_count, ir_length), dtype=object)
    ir_f = np.zeros((n_speakers, scene.band_count, ir_length), dtype=np.float64)
    all_records: list[ContributionRecord] = []
    all_summaries: list[dict] = []
    first_violation: dict | None = None
    for result in sorted(results, key=lambda r: (r.block.band, r.block.order, r.block.speaker)):
        block = result.block
        ir_q[block.speaker, block.band] += result.ir_q
        ir_f[block.speaker, block.band] += result.ir_f
        all_records.extend(result.records)
        all_summaries.extend(result.image_summaries)
        if first_violation is None and result.first_energy_violation is not None:
            first_violation = result.first_energy_violation
    return ir_q, ir_f, all_records, all_summaries, first_violation


def broadband_ir(ir_q: np.ndarray, ir_f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """频段合并为宽带冲激响应（半开频段区间，每镜像每频段恰好计入一次）。"""

    n_speakers = ir_q.shape[0]
    ir_length = ir_q.shape[2]
    merged_q = np.zeros((n_speakers, ir_length), dtype=object)
    for speaker in range(n_speakers):
        for band in range(ir_q.shape[1]):
            for sample in range(ir_length):
                merged_q[speaker, sample] = int(merged_q[speaker, sample]) + int(
                    ir_q[speaker, band, sample]
                )
    return merged_q, ir_f.sum(axis=1, dtype=np.float64)


def residual_trace(
    scene: Scene,
    records: list[ContributionRecord],
    amp_tol: float,
    ir_q: np.ndarray,
    ir_f: np.ndarray,
) -> dict:
    """基于真实累加记录构建残差溯源报告（不重新执行任何镜像计算）。"""

    if not records:
        return {"status": "within_tolerance", "first_violation": None, "contributors": []}
    records_sorted = sorted(records, key=lambda r: (r.band, r.sample, r.order, r.image_index))
    first = records_sorted[0]
    contributors = [
        {
            "image_index": rec.image_index,
            "order": rec.order,
            "residual_contribution": rec.residual_contribution,
            "fixed_contribution": rec.fixed_contribution / FP_SCALE,
            "float_contribution": rec.float_contribution,
        }
        for rec in records_sorted
        if rec.band == first.band and rec.sample == first.sample
    ]
    contributors = [item for item in contributors if item["residual_contribution"] > 0.0]
    contributors.sort(key=lambda item: item["residual_contribution"], reverse=True)

    if not contributors:
        return {
            "status": "violation",
            "first_violation": {
                "band": first.band,
                "sample": first.sample,
                "speaker": first.speaker,
                "order": first.order,
                "image_index": first.image_index,
                "aggregate_residual": abs(
                    int(ir_q[first.speaker, first.band, first.sample]) / FP_SCALE
                    - ir_f[first.speaker, first.band, first.sample]
                ),
                "tolerance": amp_tol,
            },
            "amplification_analysis": None,
            "contributors": [],
            "record_count": len(records_sorted),
        }
    chain_component = float(
        np.mean([abs(c["fixed_contribution"] - c["float_contribution"]) for c in contributors])
    )
    aggregate_residual = abs(
        int(ir_q[first.speaker, first.band, first.sample]) / FP_SCALE
        - ir_f[first.speaker, first.band, first.sample]
    )
    summation_component = max(aggregate_residual - chain_component, 0.0)
    dominant = "absorption_chain" if chain_component >= summation_component else "band_summation"
    return {
        "status": "violation",
        "first_violation": {
            "band": first.band,
            "sample": first.sample,
            "speaker": first.speaker,
            "order": first.order,
            "image_index": first.image_index,
            "aggregate_residual": aggregate_residual,
            "tolerance": amp_tol,
        },
        "amplification_analysis": {
            "chain_component_mean": chain_component,
            "summation_component": summation_component,
            "dominant_path": dominant,
            "criterion": "chain_component_mean >= summation_component 判定为吸声累乘放大，否则为频段求和放大",
        },
        "contributors": contributors,
        "record_count": len(records_sorted),
    }


__all__ = [
    "BlockId",
    "BlockResult",
    "ContributionRecord",
    "broadband_ir",
    "compute_block",
    "enumerate_images",
    "ir_length_for",
    "merge_blocks",
    "residual_trace",
]


# 静默导入，避免循环引用风格问题时保留显式依赖。
_ = quantize_array
