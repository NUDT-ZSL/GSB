"""Generate deterministic mono WAV test fixtures.

Each track is a reproducible ramp/impulse pattern so alignment can be checked
sample by sample.  Run directly:  python tests/make_samples.py
"""

import json
import os

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAV_DIR = os.path.join(ROOT, "samples", "wav")


def ramp(n, start=0.01, step=0.001):
    """Bounded deterministic ramp, never touching digital full scale."""
    return start + step * np.arange(n, dtype=np.float64)


def write_wav(name, samples, rate, subtype):
    path = os.path.join(WAV_DIR, name)
    sf.write(path, samples.astype(np.float64), rate, subtype=subtype, format="WAV")
    return path


def main():
    os.makedirs(WAV_DIR, exist_ok=True)
    # 8000 Hz / 16 bit, 0.5 s ramp.
    write_wav("track_8k_16.wav", ramp(4000), 8000, "PCM_16")
    # 16000 Hz / 24 bit, 0.5 s ramp.
    write_wav("track_16k_24.wav", ramp(8000), 16000, "PCM_24")
    # 44100 Hz / 16 bit, 0.25 s ramp.
    write_wav("track_44k_16.wav", ramp(11025), 44100, "PCM_16")
    # 44100 Hz / 24 bit impulse track: full-scale samples at fixed frames.
    impulse = np.zeros(22050, dtype=np.float64)
    impulse[100] = -1.0  # -2^23 is exactly representable -> clips
    impulse[200] = 1.0
    write_wav("track_impulse_24.wav", impulse, 44100, "PCM_24")

    # Invalid fixtures for error-code tests.
    write_wav("bad_stereo.wav", np.zeros((100, 2)), 44100, "PCM_16")
    write_wav("bad_rate.wav", np.zeros(100), 22050, "PCM_16")
    write_wav("bad_32bit.wav", np.zeros(100), 44100, "PCM_32")

    manifest = {
        "target_sample_rate": 44100,
        "target_bit_depth": 16,
        "output": "samples/out/mix.wav",
        "report": "samples/out/report.json",
        "tracks": [
            {
                "file": "wav/track_8k_16.wav",
                "offset": 0,
                "gain": 1.0,
                "crop_start": 0,
            },
            {
                "file": "wav/track_16k_24.wav",
                "offset": 1000,
                "gain": 0.5,
                "crop_start": 500,
                "crop_end": 4000,
            },
            {
                "file": "wav/track_44k_16.wav",
                "offset": -200,
                "gain": 1.0,
                "crop_start": 0,
            },
            {
                "file": "wav/track_impulse_24.wav",
                "offset": 5000,
                "gain": 2.0,
                "crop_start": 0,
            },
        ],
    }
    manifest_path = os.path.join(ROOT, "samples", "timeline.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    print("wrote fixtures under", WAV_DIR)
    print("manifest:", manifest_path)


if __name__ == "__main__":
    main()
