"""Recomputable listening reports and renderer boundary evaluation."""

from __future__ import annotations

import math

import numpy as np

from .analysis import _power_to_level_db, _strength_statistics
from .geometry import circular_resultant


SOURCE_TYPES = ["arc", "pool_boil", "droplet", "spatter"]


def _state_statistics(
    strength_frames: list[dict], state_label: str
) -> tuple[dict, list[int]]:
    frames = [
        frame
        for frame in strength_frames
        if frame["state"] == state_label
    ]
    frame_indices = [int(frame["frame_index"]) for frame in frames]
    result = {}
    for source_type in SOURCE_TYPES:
        values = np.asarray(
            [frame["source_types_power"][source_type] for frame in frames],
            dtype=float,
        )
        result[source_type] = _strength_statistics(values)
    total_power = np.asarray(
        [
            sum(frame["source_types_power"].values())
            for frame in frames
        ],
        dtype=float,
    )
    result["total_soundscape"] = _strength_statistics(total_power)
    return result, frame_indices


def _azimuth_distribution(
    render_frames: list[dict],
    state_label: str | None = None,
    bin_size_deg: float = 30.0,
) -> dict:
    selected_frames = [
        frame
        for frame in render_frames
        if state_label is None or frame["state"] == state_label
    ]
    azimuths = []
    weights = []
    type_energy = {source_type: 0.0 for source_type in SOURCE_TYPES}
    for frame in selected_frames:
        for source in frame["sources"]:
            energy = float(
                source["strength_power"]
                * source["distance_attenuation_amplitude"] ** 2
            )
            azimuths.append(float(source["azimuth_deg"]))
            weights.append(energy)
            type_energy[source["source_type"]] += energy

    bin_count = int(round(360.0 / bin_size_deg))
    bin_energy = np.zeros(bin_count, dtype=float)
    if azimuths:
        bin_indices = (
            np.floor(np.asarray(azimuths) / bin_size_deg).astype(int) % bin_count
        )
        for index, energy in zip(bin_indices, weights, strict=True):
            bin_energy[index] += float(energy)
    total_energy = float(np.sum(bin_energy))
    bins = []
    for index, energy in enumerate(bin_energy):
        bins.append(
            {
                "index": index,
                "from_deg": index * bin_size_deg,
                "to_deg": (index + 1) * bin_size_deg,
                "center_deg": index * bin_size_deg + bin_size_deg / 2.0,
                "energy": float(energy),
                "fraction": (
                    float(energy / total_energy) if total_energy > 0 else 0.0
                ),
            }
        )
    dominant_bin = (
        max(bins, key=lambda item: item["energy"]) if bins else None
    )
    circular = (
        circular_resultant(azimuths, weights)
        if azimuths
        else {
            "resultant_length": 0.0,
            "resultant_azimuth_deg": None,
            "mean_azimuth_deg": None,
        }
    )
    return {
        "state": state_label or "whole_segment",
        "frame_count": len(selected_frames),
        "source_frame_count": len(azimuths),
        "total_attenuated_energy": total_energy,
        "bins": bins,
        "dominant_bin": dominant_bin,
        "azimuth_energy_concentration": circular,
        "energy_by_source_type": type_energy,
        "speaker_energy": {
            "channel_power": [
                float(
                    np.sum(
                        [
                            frame["channel_power"][channel]
                            for frame in selected_frames
                        ]
                    )
                )
                for channel in range(
                    len(selected_frames[0]["channel_power"])
                    if selected_frames
                    else 0
                )
            ]
        },
    }


def render_path_evaluation() -> dict:
    return {
        "selected_path": "vector_base_amplitude_panning_2d_8ch",
        "comparison_path": "per_channel_analytic_assignment",
        "decision": (
            "选用 8 声道水平环绕的二维矢量基幅值平移。声源方向连续变化、"
            "飞溅事件密集且同时叠加时，基对求解比逐声道解析分配更可控。"
        ),
        "speaker_ring": {
            "channels": 8,
            "spacing_deg": 45.0,
            "front_channel": 0,
            "rear_channel": 4,
            "right_channel": 2,
            "left_channel": 6,
        },
        "applicability": {
            "source_distance": "大于等于 0.15 m；小于该距离时幅值钳位，不再满足 1/d",
            "horizontal_coverage": "0-360 度闭环覆盖，包含正后方",
            "head_rotation": "先按头部 yaw/pitch 旋转到监听者坐标，再做 VBAP",
            "energy": "每声源 sum(channel_gain^2)=1，距离能量为 amplitude^2",
            "source_density": "可叠加多个非相干事件；需在 PCM 母线保留削波余量",
            "elevation": "只做水平 VBAP，不提供耳廓 HRTF 高程谱线索",
        },
        "front_source": {
            "condition": (
                "0 度附近落在 0/315 与 0/45 相邻基对之间；0 度精确落单声道 0，"
                "跨 0/360 边界时由 315+0 基对连续过渡。"
            ),
            "analytic_error": "方位反算误差为浮点精度量级；能量守恒误差接近 0",
            "auditory_error": (
                "存在幅度平移幻象中心；若听者只依赖单扬声器定位，最坏可偏向"
                "相邻扬声器，约 22.5 度。缺少 HRTF 时高频方向感优于低频。"
            ),
        },
        "rear_source": {
            "condition": (
                "180 度精确落在后声道 4；非正后方由 135/180 或 180/225 基对"
                "连续插值，不依赖前向镜像假设。"
            ),
            "analytic_error": (
                "闭环布局无前后符号歧义，方位与能量可解析反算；不存在逐声道"
                "分配在正后方无对应扬声器时的就近折叠误差。"
            ),
            "auditory_error": (
                "二维幅值平移不提供 HRTF/pinna 的前后与仰角谱线索，窄带低频"
                "仍可能发生前后混淆；后方近距离声源会因距离钳位而显得偏硬。"
            ),
        },
        "dense_spatter": {
            "condition": (
                "每个事件独立求基对并做距离衰减，再按非相干声功率叠加；"
                "必须提高量化余量，事件同时出现时不能逐帧重新归一化总增益。"
            ),
            "error": (
                "声源方位本身仍可反算；误差主要来自事件掩蔽、量化削波和缺少"
                "垂直方向渲染，而非方位基求解。"
            ),
        },
        "limits": [
            "二维水平 VBAP 不编码仰角谱，因此不等于双耳 HRTF 渲染。",
            "声源低于最小距离时距离衰减被钳位，近场能量守恒以钳位模型为准。",
            "检查能量守恒使用母线缩混前的解析增益，WAV 的 master_scale 不参与该项核对。",
            "扬声器未校准或头部朝向轨迹与 HRIR 失配时，听觉误差会高于解析误差。",
        ],
    }


def build_listening_report(
    project: dict,
    segment: dict,
    analysis: dict,
    snapshot_version: str,
    snapshot_content_sha256: str,
) -> dict:
    segment_id = segment["segment_id"]
    strength_frames = analysis["strength_frames"]
    render_frames = analysis["render_output"]["frames"]
    stable_stats, stable_frames = _state_statistics(strength_frames, "stable")
    unstable_stats, unstable_frames = _state_statistics(
        strength_frames, "unstable"
    )

    ratio = {}
    for source_type in SOURCE_TYPES:
        stable_mean = stable_stats[source_type]["mean_power"]
        unstable_mean = unstable_stats[source_type]["mean_power"]
        ratio[source_type] = (
            float(unstable_mean / stable_mean) if stable_mean > 0 else None
        )
    total_ratio = (
        unstable_stats["total_soundscape"]["mean_power"]
        / stable_stats["total_soundscape"]["mean_power"]
        if stable_stats["total_soundscape"]["mean_power"] > 0
        else None
    )
    total_delta_db = (
        10.0 * math.log10(total_ratio) if total_ratio else None
    )
    per_source_delta_db = {
        source_type: (
            10.0 * math.log10(ratio[source_type])
            if ratio[source_type]
            else None
        )
        for source_type in SOURCE_TYPES
    }

    report = {
        "schema_version": 1,
        "report_id": f"{segment_id}:{snapshot_version}:listening-compare",
        "project_id": project["project_id"],
        "segment_id": segment_id,
        "boundary_type": segment["boundary_type"],
        "snapshot_version": snapshot_version,
        "snapshot_content_sha256": snapshot_content_sha256,
        "source_digest": segment.get("source_digest"),
        "periods": analysis["state_intervals"],
        "period_frame_counts": {
            "stable": len(stable_frames),
            "unstable": len(unstable_frames),
        },
        "period_frame_indices": {
            "stable": stable_frames,
            "unstable": unstable_frames,
        },
        "strength_statistics": {
            "unit": "relative acoustic power before distance attenuation",
            "stable": stable_stats,
            "unstable": unstable_stats,
            "unstable_to_stable_mean_power_ratio": ratio,
            "audibility_delta_db": {
                "per_source_type": per_source_delta_db,
                "total_soundscape": total_delta_db,
            },
            "definitions": {
                "mean_power": "mean(source_type_power over frames in the interval)",
                "std_power": "population standard deviation over the interval",
                "rms_power": "sqrt(mean(power^2))",
                "relative_fluctuation_std_over_mean": "std_power / mean_power",
                "mean_level_db": "mean(10*log10(max(power,1e-12)))",
                "level_span_db": "max(level_db)-min(level_db)",
            },
        },
        "audibility_assessment": {
            "criterion": (
                "全声场稳定/失稳平均功率差达到 3 dB，且报告保留逐类声源"
                "起伏与方位聚集度供教师复核。"
            ),
            "total_soundscape_ratio_unstable_to_stable": total_ratio,
            "total_soundscape_delta_db": total_delta_db,
            "threshold_db": 3.0,
            "distinguishable": (
                total_delta_db is not None
                and abs(total_delta_db) >= 3.0
            ),
            "basis": (
                "由 strength_curves.json 的逐帧四类声源功率求和后，"
                "分组取均值并换算 10*log10(ratio)。"
            ),
        },
        "azimuth_energy_summary": {
            "definition": (
                "按监听者坐标方位角 30 度分箱；权重为 "
                "source_power*distance_attenuation_amplitude^2。"
            ),
            "stable": _azimuth_distribution(render_frames, "stable"),
            "unstable": _azimuth_distribution(render_frames, "unstable"),
            "whole_segment": _azimuth_distribution(render_frames, None),
        },
        "listening_signature": {
            "stable": {
                "relative_fluctuation": {
                    source_type: stable_stats[source_type][
                        "relative_fluctuation_std_over_mean"
                    ]
                    for source_type in SOURCE_TYPES
                },
                "azimuth_concentration_resultant_length": (
                    _azimuth_distribution(render_frames, "stable")[
                        "azimuth_energy_concentration"
                    ]["resultant_length"]
                ),
            },
            "unstable": {
                "relative_fluctuation": {
                    source_type: unstable_stats[source_type][
                        "relative_fluctuation_std_over_mean"
                    ]
                    for source_type in SOURCE_TYPES
                },
                "azimuth_concentration_resultant_length": (
                    _azimuth_distribution(render_frames, "unstable")[
                        "azimuth_energy_concentration"
                    ]["resultant_length"]
                ),
            },
        },
        "spatter_dense_intervals": analysis["spatter_dense_intervals"],
        "distance_jump_count": len(analysis["distance_jumps"]),
        "render_path_evaluation": render_path_evaluation(),
        "recompute": {
            "artifacts": {
                "source_objects": f"outputs/{segment_id}/source_objects.json",
                "strength_curves": f"outputs/{segment_id}/strength_curves.json",
                "spatial_trajectories": (
                    f"outputs/{segment_id}/spatial_trajectories.json"
                ),
                "multichannel_gains": (
                    f"outputs/{segment_id}/multichannel_gains.json"
                ),
            },
            "steps_zh": [
                "从 strength_curves.json 取每个 frame 的 source_types_power。",
                "按 period_frame_indices 分组，依次计算均值、标准差、RMS 与 dB 跨度。",
                "从 spatial_trajectories.json 取 listener 坐标方位角。",
                "从 multichannel_gains.json 取每声源 strength_power 与 distance_attenuation_amplitude。",
                "按 30 度方位箱累加 power*attenuation^2，得到 binned energy 与占比。",
                "以相同权重做圆周矢量合成，resultant_length 即方位聚集度。",
            ],
            "formulas": {
                "relative_fluctuation": "std(power)/mean(power)",
                "bin_energy": (
                    "sum(power*attenuation^2 for azimuth in [bin_start,bin_end))"
                ),
                "resultant_length": (
                    "hypot(sum(w*cos(az)),sum(w*sin(az)))/sum(w)"
                ),
                "resultant_azimuth_deg": (
                    "wrap360(degrees(atan2(sum(w*sin(az)),sum(w*cos(az))))"
                ),
            },
            "tolerance": {
                "absolute_energy": 1e-9,
                "relative_fluctuation": 1e-9,
                "azimuth_deg": 1e-7,
            },
        },
        "raw_counts": {
            "frames": len(render_frames),
            "source_frames": sum(len(frame["sources"]) for frame in render_frames),
            "spatter_events": len(segment["spatter_events"]),
        },
    }
    return report
