"""本地确定性样本生成：说话人参数、合成任务清单与逐帧真值（JSONL）。"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tts_app import synth  # noqa: E402

SEED = 20261123
SPEAKER_IDS = ("spk_alpha", "spk_beta", "spk_gamma")

TASKS = [
    ("utt_001", "hello world from local tts", "spk_alpha", "neutral", 1.0, 1200),
    ("utt_002", "deterministic synthesis demo", "spk_beta", "excited", 1.1, 1400),
    ("utt_003", "cache invalidation test case", "spk_alpha", "calm", 0.95, 1300),
    ("utt_004", "incremental update propagation", "spk_gamma", "neutral", 1.05, 1500),
    ("utt_005", "lineage replay verification", "spk_beta", "calm", 1.0, 1100),
    ("utt_006", "snapshot version control flow", "spk_gamma", "excited", 1.2, 1600),
]


def generate(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    speakers = {sid: synth.speaker_params(sid, SEED) for sid in SPEAKER_IDS}
    with open(os.path.join(data_dir, "speakers.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"seed": SEED, "speakers": speakers}, fh,
                  ensure_ascii=False, indent=1, sort_keys=True)

    tasks = []
    for utt_id, text, spk, style, prosody, dur in TASKS:
        tasks.append({
            "utterance_id": utt_id, "text": text, "speaker_id": spk,
            "style_tag": style, "prosody_scale": prosody,
            "sample_rate": synth.SAMPLE_RATE, "duration_ms": dur,
            "seed": SEED,
        })
    with open(os.path.join(data_dir, "tasks.jsonl"), "w",
              encoding="utf-8") as fh:
        for task in tasks:
            fh.write(json.dumps(task, ensure_ascii=False) + "\n")

    # 逐帧真值：用与引擎一致的确定性声学特征推导
    with open(os.path.join(data_dir, "truth.jsonl"), "w",
              encoding="utf-8") as fh:
        for task in tasks:
            spk = speakers[task["speaker_id"]]
            phones = synth.segment_phonemes(synth.normalize_text(task["text"]))
            acoustic = synth.acoustic_features(
                phones, spk, task["style_tag"], task["prosody_scale"],
                1.0, None, "fp32", task["seed"], task["utterance_id"])
            pcm = synth.vocoder_synthesize(
                acoustic, spk, {"n_fft": 512, "hop": 128}, "fp32",
                task["seed"], task["utterance_id"], task["sample_rate"])
            for frame in synth.frame_features(pcm, task["speaker_id"],
                                              acoustic, task["sample_rate"]):
                frame["utterance_id"] = task["utterance_id"]
                fh.write(json.dumps(frame, ensure_ascii=False) + "\n")
    return speakers, tasks


def load_data(data_dir):
    with open(os.path.join(data_dir, "speakers.json"),
              encoding="utf-8") as fh:
        speakers = json.load(fh)["speakers"]
    tasks = []
    with open(os.path.join(data_dir, "tasks.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return speakers, tasks


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data"
    speakers, tasks = generate(out)
    print(f"已生成 {len(speakers)} 个说话人、{len(tasks)} 条任务到 {out}/")
