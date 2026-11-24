"""端到端离线多通道声场合成流水线。"""

from __future__ import annotations
import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import errors as E
from .audio import read_mono_wav, read_wav_summary, write_multichannel_wav
from .config import AMP_RESIDUAL_TOL, FP_SCALE
from .fixedpoint import (
    fixed_convolve,
    quantize,
    quantize_array,
    to_int16,
)
from .images import enumerate_images
from .persistence import (
    OUTPUT_META_SUFFIX,
    atomic_write_json,
    compute_version_id,
    load_block,
    output_is_current,
    prepare_state,
    read_interrupt_marker,
    save_block,
    write_interrupt_marker,
    write_output_meta,
)
from .reconstruction import (
    BlockId,
    BlockResult,
    broadband_ir,
    ir_length_for,
    merge_blocks,
    residual_trace,
)
from .scheduler import InterruptFlag, run_scheduler
from .scene import Scene, load_scene


@dataclass
class RunArtifacts:
    scene: Scene
    version_id: str
    output_wav: Path
    report_path: Path
    report: dict


def _load_dry_signals(scene: Scene) -> list[np.ndarray]:
    return [read_mono_wav(source.wav, scene.sample_rate) for source in scene.sources]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def synthesize_outputs(
    scene: Scene,
    ir_q: np.ndarray,
    ir_f: np.ndarray,
    dry_signals: list[np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """干信号与宽带冲激响应逐频段/逐源卷积，返回定点 PCM 与 float64 参考。"""

    broadband_q, broadband_f = broadband_ir(ir_q, ir_f)
    n_speakers = len(scene.speakers)
    n_out = dry_signals[0].shape[0] + broadband_q.shape[-1] - 1
    mix_q = np.zeros((n_speakers, n_out), dtype=object)
    mix_f = np.zeros((n_speakers, n_out), dtype=np.float64)
    for source_index, dry in enumerate(dry_signals):
        dry_q = quantize_array(dry)
        for speaker in range(n_speakers):
            conv = fixed_convolve(dry_q, broadband_q[speaker])
            mix_q[speaker] = np.asarray(
                [int(mix_q[speaker, idx]) + int(conv[idx]) for idx in range(n_out)],
                dtype=object,
            )
            mix_f[speaker] += np.convolve(dry, broadband_f[speaker], mode="full")
    pcm = np.stack([to_int16(mix_q[speaker]) for speaker in range(n_speakers)], axis=1)
    return pcm, mix_f, mix_q, n_out


def run_pipeline(
    scene_path: str,
    state_dir: Path,
    output_dir: Path,
    *,
    clean: bool,
    interrupt: InterruptFlag | None = None,
    block_observer=None,
) -> RunArtifacts:
    scene = load_scene(scene_path)
    dry_signals = _load_dry_signals(scene)
    version_id = compute_version_id(scene, dry_signals)

    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    completed = prepare_state(state_dir, version_id, clean=clean)

    images = enumerate_images(scene)
    ir_length = ir_length_for(scene, images)
    amp_tol_q = quantize(AMP_RESIDUAL_TOL)
    from .config import Spec

    energy_tol_q2 = Spec.energy_tol_q2()
    blocks = BlockId.all_blocks(scene)
    interrupt = interrupt or InterruptFlag()

    def state_writer(result: BlockResult) -> None:
        save_block(state_dir, version_id, result)

    try:
        new_results, finished_names = run_scheduler(
            scene,
            images,
            blocks,
            ir_length,
            amp_tol_q,
            energy_tol_q2,
            completed,
            state_writer,
            interrupt,
            block_observer,
        )
    except E.BusinessError as exc:
        if exc.code == E.E_INTERRUPTED:
            done = exc.details.get("completed_blocks", [])
            write_interrupt_marker(state_dir, version_id, done, exc.details.get("reason", "signal"))
            exc.details["version_id"] = version_id
        raise
    all_names = sorted({block.name() for block in blocks})
    if sorted(finished_names) != all_names:
        write_interrupt_marker(state_dir, version_id, finished_names, interrupt.reason)
        raise E.BusinessError(
            E.E_INTERRUPTED,
            "分块未全部完成",
            {"completed": finished_names, "expected": all_names},
        )
    if interrupt.is_set():
        write_interrupt_marker(state_dir, version_id, finished_names, interrupt.reason)
        raise E.BusinessError(
            E.E_INTERRUPTED,
            "收到可处理终止信号，已在分块边界停止",
            {
                "version_id": version_id,
                "reason": interrupt.reason,
                "completed_blocks": finished_names,
                "expected_blocks": all_names,
            },
        )

    loaded = [load_block(state_dir, block) for block in blocks]
    ir_q, ir_f, records, image_summaries, first_energy_violation = merge_blocks(
        scene, loaded, ir_length
    )
    trace = residual_trace(scene, records, AMP_RESIDUAL_TOL, ir_q, ir_f)

    pcm, mix_f, mix_q, n_out = synthesize_outputs(scene, ir_q, ir_f, dry_signals)
    output_wav = output_dir / "soundfield_multichannel.wav"
    meta_path = output_dir / f"soundfield_multichannel.wav{OUTPUT_META_SUFFIX}"
    content_sha = _sha256_bytes(pcm.tobytes(order="C"))
    if not output_is_current(meta_path, version_id, scene.sample_rate, content_sha):
        tmp_wav = output_dir / ".soundfield_multichannel.wav.tmp"
        try:
            write_multichannel_wav(str(tmp_wav), pcm, scene.sample_rate)
            tmp_wav.replace(output_wav)
            write_output_meta(
                meta_path,
                {
                    "version_id": version_id,
                    "sample_rate": scene.sample_rate,
                    "channels": pcm.shape[1],
                    "frames": pcm.shape[0],
                    "content_sha256": content_sha,
                },
            )
        except BaseException:
            # 失败时保留旧文件。
            tmp_wav.unlink(missing_ok=True)
            raise

    float_ref_path = output_dir / "soundfield_float64_reference.wav"
    float_pcm = np.clip(np.round(mix_f.T * 32768.0), -32768, 32767).astype("<i2")
    float_tmp = output_dir / ".soundfield_float64_reference.wav.tmp"
    write_multichannel_wav(str(float_tmp), float_pcm, scene.sample_rate)
    float_tmp.replace(float_ref_path)

    existing_summary = read_wav_summary(str(output_wav))
    decision_flips = [item for item in image_summaries if item["decision_flip"]]
    report = {
        "ok": True,
        "business_code": E.E_OK,
        "version_id": version_id,
        "seed": scene.seed,
        "scene_path": scene.path,
        "sample_rate": scene.sample_rate,
        "layout": scene.layout,
        "band_edges_hz": list(scene.band_edges_hz),
        "band_count": scene.band_count,
        "reconstruction_order": scene.reconstruction_order,
        "max_reflections": scene.max_reflections,
        "image_count": len(images),
        "block_count": len(blocks),
        "ir_length_samples": ir_length,
        "output_summary": existing_summary,
        "output_content_sha256": content_sha,
        "fixed_point": {
            "scale_bits": 24,
            "amplitude_residual_tolerance": AMP_RESIDUAL_TOL,
            "energy_residual_tolerance": 1e-12,
            "negligible_energy_threshold": 1e-10,
            "decision_policy": "能量是否可忽略仅依据定点结果，float64 仅用于诊断",
        },
        "first_image_energy_violation": first_energy_violation,
        "residual_trace": trace,
        "negligible_decision": {
            "total_images_bands": len(image_summaries),
            "negligible_fixed_count": sum(1 for s in image_summaries if s["negligible_fixed_only"]),
            "decision_flip_count": len(decision_flips),
            "decision_flips": decision_flips[:20],
        },
        "interrupt_marker": read_interrupt_marker(state_dir),
        "float64_diagnostic": {
            "max_abs_mix_diff": float(
                max(
                    abs(int(mix_q[sp, idx]) / FP_SCALE - float(mix_f[sp, idx]))
                    for sp in range(mix_q.shape[0])
                    for idx in range(mix_q.shape[1])
                )
            ),
            "reference_wav": float_ref_path.name,
        },
    }
    report_path = output_dir / "residual_report.json"
    atomic_write_json(report_path, report)
    return RunArtifacts(
        scene=scene,
        version_id=version_id,
        output_wav=output_wav,
        report_path=report_path,
        report=report,
    )
