"""Stage 1: deterministic per-frame voiceprint feature extraction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .audio import AudioFile, read_wav
from .concurrency import BatchOutcome, Task, run_tasks
from .config import PipelineConfig
from .determinism import array_fingerprint, content_hash, short_hash
from .errors import FailureRecord

EPS = 1e-12


@dataclass(frozen=True)
class FrameRecord:
    frame_index: int
    start_sample: int
    start_ms: float
    end_ms: float
    energy_db: float
    zcr: float
    flatness: float
    voiceprint: list[int]
    frame_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "start_sample": self.start_sample,
            "start_ms": round(self.start_ms, 4),
            "end_ms": round(self.end_ms, 4),
            "energy_db": round(self.energy_db, 6),
            "zcr": round(self.zcr, 6),
            "flatness": round(self.flatness, 6),
            "voiceprint": list(self.voiceprint),
            "frame_fingerprint": self.frame_fingerprint,
        }


@dataclass
class FileFeatures:
    file_name: str
    sample_count: int
    frame_count: int
    frames: list[FrameRecord]
    file_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "sample_count": self.sample_count,
            "frame_count": self.frame_count,
            "frames": [frame.to_dict() for frame in self.frames],
            "file_fingerprint": self.file_fingerprint,
        }


def extract_frame(frame: np.ndarray, frame_index: int, start_sample: int,
                  cfg: PipelineConfig) -> FrameRecord:
    windowed = frame * np.hamming(frame.shape[0]).astype(np.float64)
    rms = float(np.sqrt(np.mean(frame ** 2)) + EPS)
    energy_db = 20.0 * np.log10(rms)
    signs = np.signbit(frame)
    zcr = float(np.count_nonzero(signs[1:] != signs[:-1]) / max(1, frame.shape[0] - 1))

    spectrum = np.abs(np.fft.rfft(windowed)) + EPS
    geo_mean = float(np.exp(np.mean(np.log(spectrum))))
    arith_mean = float(np.mean(spectrum))
    flatness = float(min(1.0, geo_mean / (arith_mean + EPS)))

    bins = spectrum[1:]  # drop DC so floor noise does not dominate
    voiceprint = _quantized_voiceprint(bins, cfg.voiceprint_dims, cfg.quant_levels)
    fingerprint = short_hash({
        "i": frame_index,
        "e": round(energy_db, 6),
        "z": round(zcr, 6),
        "f": round(flatness, 6),
        "v": voiceprint,
    })
    return FrameRecord(
        frame_index=frame_index,
        start_sample=start_sample,
        start_ms=start_sample * 1000.0 / cfg.sample_rate,
        end_ms=(start_sample + cfg.frame_size) * 1000.0 / cfg.sample_rate,
        energy_db=float(energy_db),
        zcr=zcr,
        flatness=flatness,
        voiceprint=voiceprint,
        frame_fingerprint=fingerprint,
    )


def _quantized_voiceprint(bins: np.ndarray, dims: int, quant_levels: int) -> list[int]:
    """Fold the spectrum into *dims* shape bands and quantize on a fixed grid.

    Per-frame z-scoring removes absolute loudness while retaining the spectral
    shape that distinguishes speakers.  Quantization uses deterministic
    symmetric edges (no learned/global state, no RNG), so identical frames
    always map to identical integer codes.
    """
    if bins.shape[0] < dims:
        padded = np.zeros(dims, dtype=np.float64)
        padded[: bins.shape[0]] = bins
        bins = padded
    bands = np.array([
        float(np.mean(part))
        for part in np.array_split(bins, dims)
    ], dtype=np.float64)
    log_bands = np.log10(bands + EPS)
    mean = float(np.mean(log_bands))
    std = float(np.std(log_bands))
    if std < EPS:
        z_scores = np.zeros_like(log_bands)
    else:
        z_scores = (log_bands - mean) / std
    edges = np.linspace(-2.0, 2.0, quant_levels + 1)[1:-1]
    codes = np.digitize(z_scores, edges)
    codes = np.clip(codes, 0, quant_levels - 1).astype(int)
    return [int(code) for code in codes]


def _frame_matrix(samples: np.ndarray, frame_size: int, hop_size: int) -> np.ndarray:
    """Build the zero-padded frame matrix (frames x frame_size)."""
    count = int(np.ceil(max(0, samples.shape[0] - 1) / hop_size)) + 1
    if samples.shape[0] == 0:
        return np.zeros((0, frame_size), dtype=np.float64)
    matrix = np.zeros((count, frame_size), dtype=np.float64)
    for index, start in enumerate(range(0, samples.shape[0], hop_size)):
        end = min(samples.shape[0], start + frame_size)
        matrix[index, : end - start] = samples[start:end]
    return matrix


def compute_frame_metrics(frames: np.ndarray, cfg: PipelineConfig
                          ) -> dict[str, np.ndarray]:
    """Vectorized per-frame energy/ZCR/flatness/voiceprint for a frame block."""
    if frames.shape[0] == 0:
        return {
            "energy_db": np.zeros(0),
            "zcr": np.zeros(0),
            "flatness": np.zeros(0),
            "voiceprint": np.zeros((0, cfg.voiceprint_dims), dtype=np.int64),
        }
    window = np.hamming(frames.shape[1]).astype(np.float64)
    windowed = frames * window
    rms = np.sqrt(np.mean(frames ** 2, axis=1)) + EPS
    energy_db = 20.0 * np.log10(rms)

    signs = np.signbit(frames)
    zcr = np.count_nonzero(signs[:, 1:] != signs[:, :-1], axis=1) / max(
        1, frames.shape[1] - 1
    )

    spectrum = np.abs(np.fft.rfft(windowed, axis=1)) + EPS
    log_spectrum = np.log(spectrum)
    geo_mean = np.exp(np.mean(log_spectrum, axis=1))
    arith_mean = np.mean(spectrum, axis=1)
    flatness = np.minimum(1.0, geo_mean / (arith_mean + EPS))

    bins = spectrum[:, 1:]
    dims = cfg.voiceprint_dims
    if bins.shape[1] < dims:
        pad = np.zeros((bins.shape[0], dims - bins.shape[1]))
        bins = np.concatenate([bins, pad], axis=1)
    # mirror numpy.array_split exactly: remainder bins go to the first bands
    base, remainder = divmod(bins.shape[1], dims)
    boundaries = [0]
    for k in range(dims):
        boundaries.append(boundaries[-1] + base + (1 if k < remainder else 0))
    band_means = np.stack([
        np.mean(bins[:, boundaries[k]:boundaries[k + 1]], axis=1)
        for k in range(dims)
    ], axis=1)
    log_bands = np.log10(band_means + EPS)
    means = np.mean(log_bands, axis=1, keepdims=True)
    stds = np.std(log_bands, axis=1, keepdims=True)
    z_scores = np.where(stds < EPS, 0.0, (log_bands - means) / np.maximum(stds, EPS))
    edges = np.linspace(-2.0, 2.0, cfg.quant_levels + 1)[1:-1]
    codes = np.digitize(z_scores, edges)
    voiceprint = np.clip(codes, 0, cfg.quant_levels - 1).astype(np.int64)
    return {
        "energy_db": energy_db,
        "zcr": zcr,
        "flatness": flatness,
        "voiceprint": voiceprint,
    }


def extract_samples(file_name: str, samples: np.ndarray, cfg: PipelineConfig,
                    fault_injection: dict[str, Any] | None = None
                    ) -> tuple[FileFeatures, list[FailureRecord]]:
    """Extract frame features directly from an in-memory mono sample array."""
    samples = np.asarray(samples, dtype=np.float64)
    frame_size, hop_size = cfg.frame_size, cfg.hop_size
    starts = list(range(0, samples.shape[0], hop_size))
    fault = fault_injection or {}
    # Fixed chunk size keeps the math identical regardless of worker count:
    # each chunk is computed by the same vectorized routine, so floating-point
    # reduction order never depends on how work is scheduled.
    chunk_size = 64
    full_matrix = _frame_matrix(samples, frame_size, hop_size)

    def worker(task: Task) -> list[FrameRecord]:
        indices = task.payload["frame_indices"]
        if "frame_fail" in fault and fault["frame_fail"] in indices:
            raise RuntimeError("injected frame extraction failure")
        block = full_matrix[indices]
        metrics = compute_frame_metrics(block, cfg)
        records: list[FrameRecord] = []
        for position, index in enumerate(indices):
            start = index * hop_size
            voiceprint = [int(code) for code in metrics["voiceprint"][position]]
            energy_db = float(metrics["energy_db"][position])
            zcr = float(metrics["zcr"][position])
            flatness = float(metrics["flatness"][position])
            fingerprint = short_hash({
                "i": index,
                "e": round(energy_db, 6),
                "z": round(zcr, 6),
                "f": round(flatness, 6),
                "v": voiceprint,
            })
            records.append(FrameRecord(
                frame_index=index,
                start_sample=start,
                start_ms=start * 1000.0 / cfg.sample_rate,
                end_ms=(start + frame_size) * 1000.0 / cfg.sample_rate,
                energy_db=energy_db,
                zcr=zcr,
                flatness=flatness,
                voiceprint=voiceprint,
                frame_fingerprint=fingerprint,
            ))
        return records

    groups = [list(range(start, min(start + chunk_size, len(starts))))
              for start in range(0, len(starts), chunk_size)]
    tasks = [
        Task(sort_key=[file_name, group[0]],
             identifier=f"{file_name}#frames{group[0]}-{group[-1]}",
             stage="features", payload={"frame_indices": group})
        for group in groups
    ]
    outcome: BatchOutcome = run_tasks(tasks, worker, cfg.max_workers)
    flat_frames = [frame for group in outcome.results for frame in group]
    flat_frames.sort(key=lambda frame: frame.frame_index)
    file_hash = content_hash({
        "file": file_name,
        "samples_fingerprint": array_fingerprint(samples),
        "sample_count": int(samples.shape[0]),
        # only parameters that actually change frame math belong in this hash;
        # scheduling settings such as max_workers must not affect content
        "feature_params": {
            "frame_ms": cfg.frame_ms,
            "hop_ms": cfg.hop_ms,
            "voiceprint_dims": cfg.voiceprint_dims,
            "quant_levels": cfg.quant_levels,
            "sample_rate": cfg.sample_rate,
        },
    })
    return (
        FileFeatures(
            file_name=file_name,
            sample_count=int(samples.shape[0]),
            frame_count=len(starts),
            frames=flat_frames,
            file_fingerprint=file_hash,
        ),
        outcome.failures,
    )


def extract_file(path: str, cfg: PipelineConfig,
                 fault_injection: dict[str, Any] | None = None
                 ) -> tuple[FileFeatures, list[FailureRecord]]:
    audio: AudioFile = read_wav(path)
    return extract_samples(audio.name, audio.samples, cfg, fault_injection)


def extract_all(paths: list[str], cfg: PipelineConfig,
                fault_injection: dict[str, Any] | None = None
                ) -> tuple[list[FileFeatures], list[FailureRecord]]:
    features: list[FileFeatures] = []
    failures: list[FailureRecord] = []
    for path in sorted(paths):
        result, file_failures = extract_file(path, cfg, fault_injection)
        features.append(result)
        failures.extend(file_failures)
    failures.sort(key=lambda item: item.sort_key)
    return features, failures


def feature_artifact(features: list[FileFeatures],
                     failures: list[FailureRecord]) -> dict[str, Any]:
    return {
        "files": [item.to_dict() for item in features],
        "failures": [item.to_dict() for item in failures],
    }
