#!/usr/bin/env python3
"""Platform smoke test: WAV round-trip, atomic replace, and thread-pool creation."""

from __future__ import annotations

import json
import os
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from voice_dialog.util import atomic_write_json


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="voice-dialog-smoke-") as temp:
        root = Path(temp)
        wav_path = root / "roundtrip.wav"
        samples = np.asarray([0, 1200, -1200, 32767, -32768, 0], dtype="<i2")
        with wave.open(str(wav_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(samples.tobytes())
        with wave.open(str(wav_path), "rb") as reader:
            decoded = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
        wav_ok = bool(np.array_equal(decoded, samples))

        target = root / "state.json"
        replacement = root / "state.replacement.json"
        replacement.write_text('{"generation":2}\n', encoding="utf-8")
        os.replace(replacement, target)
        replace_ok = json.loads(target.read_text(encoding="utf-8")) == {"generation": 2}

        atomic_write_json(root / "atomic.json", {"nested": [1, 2, 3]})
        atomic_ok = json.loads((root / "atomic.json").read_text(encoding="utf-8")) == {"nested": [1, 2, 3]}

        with ThreadPoolExecutor(max_workers=4) as executor:
            values = list(executor.map(lambda value: value * value, range(8)))
        thread_ok = values == [value * value for value in range(8)]

    result = {
        "wav_roundtrip": wav_ok,
        "os_replace": replace_ok,
        "atomic_json": atomic_ok,
        "thread_pool": thread_ok,
        "all_passed": wav_ok and replace_ok and atomic_ok and thread_ok,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
