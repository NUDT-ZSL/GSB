"""生成确定性演示样本：PCM、三版本切片文件、识别配置与词表。

重复执行产出逐字节相同的文件。用法：python3 scripts/gen_samples.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from asr_gov.migrate import migrate
from asr_gov.formats import dump_slices
from asr_gov.pcm import synthesize_clip, DEFAULT_CLIP_ID, DEFAULT_SEED
from asr_gov.sample_data import build_ground_truth, build_vocabs, default_config

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")


def main():
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    clip = synthesize_clip()
    pcm_path = os.path.join(SAMPLES_DIR, f"{DEFAULT_CLIP_ID}.pcm")
    with open(pcm_path, "wb") as fh:
        fh.write(clip.to_bytes())

    gt = build_ground_truth()
    v2_set = migrate(gt, 2).slice_set
    v1_set = migrate(gt, 1).slice_set
    outputs = {
        "slices.v1.txt": dump_slices(v1_set, 1),
        "slices.v2.txt": dump_slices(v2_set, 2),
        "slices.v3.json": dump_slices(gt, 3),
    }
    for name, content in outputs.items():
        with open(os.path.join(SAMPLES_DIR, name), "w", encoding="utf-8") as fh:
            fh.write(content)

    config = default_config()
    with open(os.path.join(SAMPLES_DIR, "config.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n")

    vocab1, vocab2 = build_vocabs()
    for vocab in (vocab1, vocab2):
        path = os.path.join(SAMPLES_DIR, f"{vocab.version}.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(vocab.to_dict(), indent=2, sort_keys=True) + "\n")

    meta = {
        "clip_id": DEFAULT_CLIP_ID,
        "seed": DEFAULT_SEED,
        "sample_rate": clip.sample_rate,
        "n_samples": clip.n_samples,
        "duration_ms": clip.duration_ms,
        "pcm_sha256": clip.digest(),
    }
    with open(os.path.join(SAMPLES_DIR, f"{DEFAULT_CLIP_ID}.meta.json"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps(meta, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
