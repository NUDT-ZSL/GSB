"""确定性一致性报告生成。

报告只包含可复现的测量与判定数据，不写入时间戳、主机名等环境信息；
所有集合型字段排序后输出，保证任意并发度下字节一致。
"""

import json

import numpy as np


def _json_default(obj):
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError("不可序列化的类型 %r" % type(obj))


def build_report(session, snapshot_id, planned, results, stitched,
                 loudness_result, dynamics_verdicts, output_info,
                 reused_indexes, theoretical_frames):
    segment_reports = []
    sync_degraded = False
    for index, ((dev, seg), result) in enumerate(zip(planned, results)):
        clock = result["clock"]
        sync_degraded = sync_degraded or clock.get("degraded", False)
        segment_reports.append({
            "segment_index": index,
            "device_id": dev.device_id,
            "segment_id": seg.segment_id,
            "restored_from_state": index in reused_indexes,
            "wav_info": dict(result["wav_info"], path=seg.file),
            "declared_channels": seg.channels,
            "declared_gain": seg.gain,
            "sync": {
                "requested_model_order": clock["requested_model_order"],
                "used_model_order": clock["used_model_order"],
                "degraded": clock["degraded"],
                "degradation_reasons": clock["degradation_reasons"],
                "drift_coeff": clock["drift_coeff"],
                "drift_rate": clock["drift_rate"],
                "drift_ppm": clock["drift_ppm"],
                "offset_samples_device": clock["offset_samples_device"],
                "offset_samples_reference": clock["offset_samples_reference"],
                "offset_integer_reference": clock["offset_integer_reference"],
                "rms_residual_device_samples":
                    clock["rms_residual_device_samples"],
                "max_abs_residual_device_samples":
                    clock["max_abs_residual_device_samples"],
                "observation_count": clock["observation_count"],
                "observations": clock["observations"],
                "missing_markers": clock["missing_markers"],
            },
        })

    report = {
        "schema_version": 1,
        "session_id": session.session_id,
        "snapshot_id": snapshot_id,
        "reference_device_id": session.reference_device_id,
        "reference_sample_rate": next(
            d.sample_rate for d in session.devices if d.is_reference),
        "target_sample_rate": session.target_sample_rate,
        "target_bits": session.target_bits,
        "concurrency": session.concurrency,
        "memory_budget_bytes": session.memory_budget_bytes,
        "timeslice_ms": session.timeslice_ms,
        "timeline": {
            "reference_frames": stitched["timeline"].shape[0],
            "output_channels": session.output_channels,
            "gap_ranges": stitched["gap_ranges"],
            "time_slices": stitched["slices"],
        },
        "sync_segments": segment_reports,
        "loudness": loudness_result,
        "dynamics_verdicts": dynamics_verdicts,
        "output": {
            "wav": session.output_wav,
            "frames": output_info["frames"],
            "sample_rate": output_info["sample_rate"],
            "bits": output_info["bits"],
            "channels": output_info["channels"],
            "theoretical_frames": theoretical_frames,
            "frame_error": abs(output_info["frames"] - theoretical_frames),
        },
        "degraded_overall": bool(sync_degraded or stitched["gap_ranges"]),
    }
    return report


def render_report(report):
    return json.dumps(report, ensure_ascii=False, sort_keys=True,
                      indent=2, default=_json_default)

