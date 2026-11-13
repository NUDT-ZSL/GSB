"""Deterministic 16-bit PCM WAV sample generation for demos and tests.

Every sample is produced from pure arithmetic (sine waves, fixed spikes) so
the generated files are bit-for-bit reproducible on any machine.  No external
audio libraries or network access are involved.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Iterable, Sequence

FULL_SCALE = 32768.0
DEFAULT_RATE = 44100


def db_to_amplitude(dbfs_peak: float) -> int:
    """Peak amplitude for a dBFS value (0 dBFS == 32767 full-scale peak)."""
    return int(round(32767 * 10.0 ** (dbfs_peak / 20.0)))


def sine_samples(
    duration: float,
    rate: int = DEFAULT_RATE,
    rms_dbfs: float = -20.0,
    freq: float = 440.0,
    channels: int = 1,
) -> tuple[list[int], int, int]:
    """Interleaved sine samples whose RMS equals ``rms_dbfs``.

    The amplitude is chosen as ``rms * sqrt(2)``; with an integer number of
    cycles across the clip this yields the requested RMS almost exactly.
    """
    nframes = int(round(duration * rate))
    amplitude = rms_dbfs_to_amplitude(rms_dbfs)
    per_channel: list[int] = []
    for index in range(nframes):
        phase = 2.0 * math.pi * freq * index / rate
        value = int(round(amplitude * math.sin(phase)))
        per_channel.append(max(-32768, min(32767, value)))

    if channels == 1:
        return per_channel, nframes, 1

    interleaved: list[int] = []
    for index, value in enumerate(per_channel):
        # Give each channel a slightly different level so stereo handling is
        # visible in the data, while both stay musical and in range.
        interleaved.append(value)
        for channel in range(1, channels):
            shifted = int(round(value * (0.8 ** channel)))
            interleaved.append(shifted)
    return interleaved, nframes, channels


def rms_dbfs_to_amplitude(rms_dbfs: float) -> float:
    return FULL_SCALE * 10.0 ** (rms_dbfs / 20.0) * math.sqrt(2.0)


def constant_samples(
    duration: float, rate: int = DEFAULT_RATE, value: int = 0, channels: int = 1
) -> tuple[list[int], int, int]:
    nframes = int(round(duration * rate))
    value = max(-32768, min(32767, value))
    return [value] * (nframes * channels), nframes, channels


def spike_samples(
    duration: float,
    rate: int = DEFAULT_RATE,
    base_rms_dbfs: float = -40.0,
    spike_value: int = 32000,
    spike_count: int = 4,
    freq: float = 500.0,
) -> tuple[list[int], int, int]:
    """Low-level sine with a few near-full-scale samples: a high crest factor.

    Used to exercise peak limiting after positive gain is requested.
    """
    samples, nframes, channels = sine_samples(duration, rate, base_rms_dbfs, freq)
    if channels != 1:
        raise ValueError("spike_samples only supports mono")
    middle = len(samples) // 2
    for offset in range(spike_count):
        index = middle + offset
        if 0 <= index < len(samples):
            samples[index] = spike_value
    return samples, nframes, channels


def write_wav(
    path: str | Path,
    samples: Sequence[int],
    rate: int = DEFAULT_RATE,
    channels: int = 1,
    sample_width: int = 2,
) -> Path:
    """Write interleaved integer samples as an uncompressed PCM WAV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if sample_width == 2:
        frame_bytes = b"".join(
            int(sample).to_bytes(2, "little", signed=True) for sample in samples
        )
    elif sample_width == 1:
        # 8-bit PCM is unsigned with a 128 offset.
        frame_bytes = bytes(
            max(0, min(255, int(sample) // 256 + 128)) for sample in samples
        )
    else:
        raise ValueError("this helper writes 8- or 16-bit PCM only")

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(rate)
        wav.writeframes(frame_bytes)
    return path


def write_sine_wav(
    path: str | Path,
    duration: float = 1.0,
    rate: int = DEFAULT_RATE,
    rms_dbfs: float = -20.0,
    freq: float = 440.0,
    channels: int = 1,
) -> Path:
    samples, _, channels_used = sine_samples(duration, rate, rms_dbfs, freq, channels)
    return write_wav(path, samples, rate, channels_used)


def truncate_file(source: str | Path, destination: str | Path, keep_bytes: int) -> Path:
    """Copy the first ``keep_bytes`` bytes of a file (simulates truncation)."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes()[:keep_bytes])
    return destination


def write_corrupt_no_riff(path: str | Path, size: int = 2048) -> Path:
    """Binary garbage that does not start with a RIFF header."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pattern = bytes((index * 37 + 11) % 256 for index in range(size))
    path.write_bytes(pattern)
    return path


def write_broken_fmt(path: str | Path) -> Path:
    """RIFF/WAVE file whose fmt chunk declares an invalid audio format."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt_payload = (
        (0).to_bytes(2, "little")     # audio format 0 == invalid
        + (1).to_bytes(2, "little")   # channels
        + (44100).to_bytes(4, "little")
        + (88200).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
    )
    fmt_chunk = b"fmt " + len(fmt_payload).to_bytes(4, "little") + fmt_payload
    data_chunk = b"data" + (0).to_bytes(4, "little")
    body = b"WAVE" + fmt_chunk + data_chunk
    header = b"RIFF" + (len(body)).to_bytes(4, "little") + body
    path.write_bytes(header)
    return path


# --------------------------------------------------------------------------- #
# Standard sample set used by the demo
# --------------------------------------------------------------------------- #
def create_sample_set(directory: str | Path) -> dict[str, Path]:
    """Create the deterministic sample files and return name -> path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}

    files["tone_a"] = write_sine_wav(
        directory / "tone_a.wav", 1.0, 44100, -10.0, 440.0
    )
    files["tone_b"] = write_sine_wav(
        directory / "tone_b.wav", 1.0, 44100, -18.0, 660.0
    )
    files["tone_c"] = write_sine_wav(
        directory / "tone_c.wav", 1.0, 44100, -30.0, 880.0
    )
    files["tone_long"] = write_sine_wav(
        directory / "tone_long.wav", 3.0, 44100, -22.0, 330.0
    )
    files["spikey"] = write_wav(
        directory / "spikey.wav",
        *spike_samples(1.0, 44100, -40.0, 32000, 4)[:1],
        rate=44100,
    )
    files["stereo"] = write_sine_wav(
        directory / "stereo.wav", 1.0, 44100, -20.0, 440.0, channels=2
    )
    files["sr_22050"] = write_sine_wav(
        directory / "sr_22050.wav", 1.0, 22050, -20.0, 440.0
    )
    files["sr_48000"] = write_sine_wav(
        directory / "sr_48000.wav", 1.0, 48000, -20.0, 440.0
    )
    files["eight_bit"] = write_wav(
        directory / "eight_bit.wav",
        sine_samples(1.0, 44100, -20.0, 440.0)[0],
        rate=44100,
        channels=1,
        sample_width=1,
    )
    files["truncated_data"] = truncate_file(
        write_sine_wav(directory / "_valid_for_truncation.wav", 1.0, 44100, -20.0),
        directory / "truncated_data.wav",
        keep_bytes=(directory / "_valid_for_truncation.wav").stat().st_size - 8000,
    )
    (directory / "_valid_for_truncation.wav").unlink(missing_ok=True)
    files["truncated_header"] = truncate_file(
        write_sine_wav(directory / "_valid_for_truncation2.wav", 1.0, 44100, -20.0),
        directory / "truncated_header.wav",
        keep_bytes=20,
    )
    (directory / "_valid_for_truncation2.wav").unlink(missing_ok=True)
    files["corrupt"] = write_corrupt_no_riff(directory / "corrupt.wav")
    files["broken_fmt"] = write_broken_fmt(directory / "broken_fmt.wav")
    return files


SAMPLE_DESCRIPTIONS: dict[str, str] = {
    "tone_a": "1.00 s, 44.1 kHz mono sine, RMS -10 dBFS",
    "tone_b": "1.00 s, 44.1 kHz mono sine, RMS -18 dBFS",
    "tone_c": "1.00 s, 44.1 kHz mono sine, RMS -30 dBFS",
    "tone_long": "3.00 s, 44.1 kHz mono sine, RMS -22 dBFS",
    "spikey": "1.00 s, low-level sine with 4 full-scale spikes (peak limiting demo)",
    "stereo": "1.00 s, 44.1 kHz stereo sine, RMS -20 dBFS",
    "sr_22050": "1.00 s, 22.05 kHz mono sine (sample-rate mismatch)",
    "sr_48000": "1.00 s, 48 kHz mono sine (sample-rate mismatch)",
    "eight_bit": "1.00 s, 8-bit PCM (bit-depth rejection)",
    "truncated_data": "valid header, audio data cut short (truncation)",
    "truncated_header": "only 20 bytes of the header (unreadable WAV)",
    "corrupt": "binary garbage without a RIFF header",
    "broken_fmt": "RIFF header with an invalid fmt chunk",
}


def describe_sample_set(files: dict[str, Path]) -> Iterable[str]:
    for name in sorted(files):
        yield f"  {files[name].name:<22} {SAMPLE_DESCRIPTIONS.get(name, '')}"
