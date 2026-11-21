"""PCM loading and content-addressed chunk hashing."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .errors import fail
from .models import AudioInfo, Clip
from .util import sha256_bytes, sha256_value


def load_pcm(clip: Clip, manifest_dir: Path) -> np.ndarray:
    path = Path(clip.audio_pcm)
    if not path.is_absolute():
        path = manifest_dir / path
    try:
        samples = np.fromfile(path, dtype="<i2")
    except OSError as exc:
        fail(
            "E_AUDIO_MISSING",
            "PCM file cannot be read",
            clip_id=clip.clip_id,
            path=str(path),
            error=str(exc),
        )
    if clip.channels != 1 or clip.sample_width != 2:
        fail(
            "E_CONFIG_INVALID",
            "only mono 16-bit PCM is supported",
            clip_id=clip.clip_id,
            channels=clip.channels,
            sample_width=clip.sample_width,
        )
    return np.ascontiguousarray(samples, dtype="<i2")


def audio_info(clip: Clip, manifest_dir: Path, chunk_samples: int) -> AudioInfo:
    samples = load_pcm(clip, manifest_dir)
    pcm = np.ascontiguousarray(samples, dtype="<i2").tobytes()
    pcm_hash = sha256_bytes(pcm)
    if clip.pcm_sha256 and clip.pcm_sha256 != pcm_hash:
        fail(
            "E_AUDIO_CHECKSUM",
            "PCM checksum does not match the sample manifest",
            clip_id=clip.clip_id,
            expected=clip.pcm_sha256,
            actual=pcm_hash,
        )
    hashes: list[str] = []
    chunk_bytes = max(1, chunk_samples) * 2
    for offset in range(0, len(pcm), chunk_bytes):
        hashes.append(sha256_bytes(pcm[offset : offset + chunk_bytes]))
    if not hashes:
        hashes.append(sha256_bytes(b""))
    chunk_manifest_hash = sha256_value(
        {"chunk_samples": chunk_samples, "chunk_hashes": hashes}
    )
    return AudioInfo(
        clip=clip,
        samples=samples,
        total_samples=int(samples.size),
        pcm_sha256=pcm_hash,
        chunk_sample_count=chunk_samples,
        chunk_hashes=tuple(hashes),
        chunk_manifest_hash=chunk_manifest_hash,
    )
