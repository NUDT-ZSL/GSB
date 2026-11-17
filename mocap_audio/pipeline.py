"""端到端处理管线：校验 -> 并行解码/检测 -> 拼接 -> 测量 -> 判定 -> 提交。"""

import os

from .commit import commit_outputs
from .dynamics import evaluate_segments
from .loudness import measure
from .report import build_report, render_report
from .resample import rational_resample, theoretical_output_frames
from .scheduler import run_segment_jobs
from .state import StateStore, compute_snapshot_id
from .sync import reference_identity
from .timeline import map_segment, stitch, total_reference_frames


def run(session, signaler, provided_snapshot=None):
    ref_device = next(d for d in session.devices if d.is_reference)
    ref_rate = ref_device.sample_rate
    snapshot_id = provided_snapshot or compute_snapshot_id(session)

    store = StateStore(session.resolve(session.state_dir), snapshot_id)
    store.prepare()
    if store.interrupted():
        os.remove(store.interrupt_path)
    else:
        store.reset()

    reuse = {}
    if provided_snapshot is not None:
        for _, seg in session.iter_segments():
            cached = store.load_segment(seg)
            if cached is not None:
                reuse[(seg.device_id, seg.segment_id)] = cached

    planned, results = run_segment_jobs(
        session, ref_rate, store, signaler, reuse)

    clocks = []
    for index, ((dev, seg), result) in enumerate(zip(planned, results)):
        if dev.is_reference:
            result["clock"] = reference_identity(
                dev.sample_rate,
                {"device_id": dev.device_id, "segment_id": seg.segment_id})
        clocks.append(result["clock"])

    total_frames = total_reference_frames(session, ref_rate)
    contributions = []
    for (dev, seg), result in zip(planned, results):
        contrib = map_segment(
            result["samples"], dev.sample_rate, ref_rate,
            result["clock"], seg, session.output_channels)
        contributions.append(contrib)

    stitched = stitch(contributions, ref_rate, total_frames,
                      session.output_channels, session.timeslice_ms)
    for slice_idx in range(len(stitched["slices"])):
        signaler.check(store, "timeslice", slice_idx)

    loudness_result = measure(
        stitched["timeline"], stitched["owner"], stitched["gap_mask"],
        contributions, ref_rate, session.stat_window_ms,
        session.absolute_gate_db, session.relative_gate_db, session.tail_rule)
    dynamics_verdicts = evaluate_segments(
        contributions, stitched, loudness_result, session, clocks)

    output_samples = rational_resample(
        stitched["timeline"], ref_rate, session.target_sample_rate)
    expected_frames = theoretical_output_frames(
        total_frames, ref_rate, session.target_sample_rate)

    output_info = {
        "path": session.resolve(session.output_wav),
        "frames": output_samples.shape[0],
        "sample_rate": session.target_sample_rate,
        "bits": session.target_bits,
        "channels": output_samples.shape[1],
    }
    reused_indexes = {idx for idx, result in enumerate(results)
                      if result.get("restored")}
    report = build_report(
        session, snapshot_id, planned, results, stitched, loudness_result,
        dynamics_verdicts, output_info, reused_indexes, expected_frames)
    report_text = render_report(report)

    os.makedirs(os.path.dirname(output_info["path"]) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(session.resolve(session.output_report)) or ".",
                exist_ok=True)
    commit_info = commit_outputs(
        output_info["path"], session.resolve(session.output_report),
        output_samples, session.target_sample_rate, session.target_bits,
        report_text, snapshot_id)

    store.write_manifest([
        {"device_id": seg.device_id, "segment_id": seg.segment_id}
        for _, seg in planned])
    return {"report": report, "commit": commit_info, "snapshot_id": snapshot_id}

