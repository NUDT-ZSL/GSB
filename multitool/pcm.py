"""PCM WAV decoding.

Only uncompressed mono PCM WAV at 16 or 24 bits is accepted.  Samples are
normalized to 32 bit float intermediate representation divided by full scale.
The 24 bit path parses the little-endian three-byte words by hand instead of
relying on a third-party integer conversion.
"""

import wave

import numpy as np

from .errors import E_FORMAT

ALLOWED_RATES = (8000, 16000, 44100)
ALLOWED_BITS = (16, 24)


class TrackFormatError(ValueError):
    """Raised when a track does not satisfy rate/bit-depth/channel rules."""

    def __init__(self, reason):
        super().__init__(reason)
        self.code = E_FORMAT
        self.reason = reason


def _decode_s24_le(raw):
    """Hand-written little-endian signed 24 bit PCM parser.

    Each frame is exactly three bytes: low, middle, high.  The high byte is
    sign extended into a signed 32 bit integer.
    """
    data = np.frombuffer(raw, dtype=np.uint8)
    if len(data) % 3 != 0:
        raise TrackFormatError("24bit payload length is not a multiple of 3")
    triples = data.reshape(-1, 3)
    low = triples[:, 0].astype(np.int32)
    mid = triples[:, 1].astype(np.int32)
    high = triples[:, 2].astype(np.int32)
    # Sign-extend the high byte: values >= 0x80 are negative.
    high = np.where(high >= 0x80, high - 256, high)
    return (high << 16) | (mid << 8) | low


def read_wav_mono(path):
    """Read a mono PCM WAV into a float64 array.

    Returns ``(samples, sample_rate, bit_depth, clipped)`` where ``clipped``
    is True when the raw integer stream reaches digital full scale.
    Raises :class:`TrackFormatError` for unsupported formats.
    """
    try:
        wav = wave.open(path, "rb")
    except (wave.Error, OSError) as exc:
        raise TrackFormatError(f"cannot open WAV: {exc}") from exc

    with wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        n_frames = wav.getnframes()
        compression = wav.getcomptype()
        raw = wav.readframes(n_frames)

    if compression != "NONE":
        raise TrackFormatError(f"compressed WAV not supported: {compression}")
    if channels != 1:
        raise TrackFormatError(f"expected mono track, got {channels} channels")
    if sample_rate not in ALLOWED_RATES:
        raise TrackFormatError(
            f"sample rate {sample_rate} not in {ALLOWED_RATES}"
        )
    if sample_width == 2:
        bit_depth = 16
        integer = np.frombuffer(raw, dtype="<i2").astype(np.int32)
        samples = integer.astype(np.float64) / float(1 << 15)
        clipped = bool(np.any(integer >= (1 << 15) - 1) or np.any(integer <= -(1 << 15)))
    elif sample_width == 3:
        bit_depth = 24
        integer = _decode_s24_le(raw)
        samples = integer.astype(np.float64) / float(1 << 23)
        clipped = bool(
            np.any(integer >= (1 << 23) - 1) or np.any(integer <= -(1 << 23))
        )
    else:
        raise TrackFormatError(
            f"bit depth {sample_width * 8} not in {ALLOWED_BITS}"
        )

    if samples.shape[0] != n_frames:
        raise TrackFormatError("decoded frame count disagrees with header")
    return samples, sample_rate, bit_depth, clipped
