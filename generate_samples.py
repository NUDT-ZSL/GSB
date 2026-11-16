"""Generate a deterministic local sample directory (fixed seed, no network)."""

import json
import os
import random
import shutil
import sys

SEED = 47

PERSONS = ["张三", "王芳", "李四", "赵伟", "刘静", "张建国"]
PLACES = ["北京", "上海", "杭州市", "广州", "南京路"]
ORGS = ["华为公司", "清华大学", "人民医院"]
FILLERS = list("我你他在来着了是和与说走看今天很好去年月日也都不要想会从回")

BASE_DICT = [
    {"text": "张三", "type": "PER", "freq": 50},
    {"text": "王芳", "type": "PER", "freq": 40},
    {"text": "李四", "type": "PER", "freq": 30},
    {"text": "赵伟", "type": "PER", "freq": 20},
    {"text": "刘静", "type": "PER", "freq": 10},
    {"text": "北京", "type": "LOC", "freq": 60},
    {"text": "上海", "type": "LOC", "freq": 55},
    {"text": "杭州市", "type": "LOC", "freq": 45},
    {"text": "广州", "type": "LOC", "freq": 35},
    {"text": "南京路", "type": "LOC", "freq": 15},
    {"text": "华为公司", "type": "ORG", "freq": 70},
    {"text": "清华大学", "type": "ORG", "freq": 65},
    {"text": "人民医院", "type": "ORG", "freq": 25},
    {"text": "测试", "type": "ANIMAL", "freq": 1},
]

DICT_V2 = [
    {"text": "王芳", "type": "PER", "freq": 40},
    {"text": "李四", "type": "PER", "freq": 30},
    {"text": "赵伟", "type": "PER", "freq": 20},
    {"text": "刘静", "type": "PER", "freq": 10},
    {"text": "张建国", "type": "PER", "freq": 33},
    {"text": "北京", "type": "LOC", "freq": 60},
    {"text": "上海", "type": "LOC", "freq": 77},
    {"text": "杭州市", "type": "LOC", "freq": 45},
    {"text": "广州", "type": "LOC", "freq": 35},
    {"text": "华为公司", "type": "ORG", "freq": 70},
    {"text": "清华大学", "type": "ORG", "freq": 65},
    {"text": "阿里巴巴", "type": "ORG", "freq": 80},
]

EDITS = [
    {"op": "model", "model_path": "model_v2.json"},
    {"op": "add", "entry": {"text": "王小明", "type": "PER", "freq": 88}},
    {"op": "delete", "text": "张三"},
    {"op": "replace", "entries_path": "dict_v2.json"},
    {"op": "add", "entry": {"text": "北京", "type": "ORG", "freq": 999}},
]

SIGNAL_EDITS = [
    {"op": "add", "entry": {"text": "陈强", "type": "PER", "freq": 11}},
    {"op": "add", "entry": {"text": "林敏", "type": "PER", "freq": 12}},
    {"op": "add", "entry": {"text": "周杰", "type": "PER", "freq": 13}},
    {"op": "add", "entry": {"text": "吴燕", "type": "PER", "freq": 14}},
    {"op": "add", "entry": {"text": "郑浩", "type": "PER", "freq": 15}},
]

LABELS = (["O"] + ["%s-%s" % (p, t)
                   for t in ("PER", "LOC", "ORG") for p in "BIES"])


def build_sentences(rng):
    pool = PERSONS + PLACES + ORGS + ["王小明", "阿里巴巴"]
    sents = []
    for i in range(48):
        sid = "s%04d" % (i + 1)
        tokens = []
        for _ in range(rng.randint(2, 5)):
            tokens.append(rng.choice(FILLERS))
        for _ in range(rng.randint(1, 3)):
            tokens.append(rng.choice(pool))
            tokens.append(rng.choice(FILLERS))
        rng.shuffle(tokens)
        sents.append((sid, list("".join(tokens))))
    # guarantee coverage for invalidation tests
    def force(sid_idx, text):
        chars = sents[sid_idx][1]
        chars.extend(list(text))
    force(0, "张三")
    force(1, "张三")
    force(2, "张三")
    force(3, "王小明")
    force(4, "王小明")
    force(5, "北京")
    force(6, "北京")
    force(7, "阿里巴巴")
    return sents


def make_model(rng):
    chars = set(FILLERS)
    for word in (PERSONS + PLACES + ORGS
                 + ["王小明", "阿里巴巴", "张建国", "陈强", "林敏", "周杰",
                    "吴燕", "郑浩", "测试"]):
        chars.update(word)
    char_embedding = {}
    for ch in sorted(chars):
        char_embedding[ch] = [round(rng.uniform(-1, 1), 6) for _ in range(8)]
    transitions = {"<START>": {}}
    for src in LABELS:
        transitions["<START>"][src] = round(rng.uniform(-0.5, 0.5), 6)
        transitions[src] = {dst: round(rng.uniform(-1, 1), 6) for dst in LABELS}
    emission = {lab: [round(rng.uniform(-1, 1), 6) for _ in range(64)]
                for lab in LABELS}
    return {
        "feature_dim": 8,
        "hash_buckets": 64,
        "labels": LABELS,
        "char_embedding": char_embedding,
        "transitions": transitions,
        "emission": emission,
    }


def generate(sample_dir):
    if os.path.isdir(sample_dir):
        shutil.rmtree(sample_dir)
    os.makedirs(sample_dir)
    rng = random.Random(SEED)

    sents = build_sentences(rng)
    lines = []
    for sid, chars in sents:
        lines.append(json.dumps({
            "sent_id": sid,
            "chars": " ".join(chars),
            "positions": list(range(len(chars))),
        }, ensure_ascii=False))
    # one sentence that deterministically fails feature construction
    bad_chars = ["他", "\u0001", "在"]
    lines.append(json.dumps({
        "sent_id": "s9000",
        "chars": " ".join(bad_chars),
        "positions": list(range(len(bad_chars))),
    }, ensure_ascii=False))
    # invalid corpus lines -> E_CORPUS_INVALID
    lines.append(json.dumps({
        "sent_id": "s0001",
        "chars": "重 复",
        "positions": [0, 1],
    }, ensure_ascii=False))
    lines.append(json.dumps({
        "sent_id": "s9001",
        "chars": "位 置 错",
        "positions": [0, 2, 3],
    }, ensure_ascii=False))
    with open(os.path.join(sample_dir, "corpus.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    with open(os.path.join(sample_dir, "model.json"), "w",
              encoding="utf-8") as fh:
        json.dump(make_model(random.Random(SEED)), fh, ensure_ascii=False)
    with open(os.path.join(sample_dir, "model_v2.json"), "w",
              encoding="utf-8") as fh:
        json.dump(make_model(random.Random(SEED + 1)), fh, ensure_ascii=False)

    with open(os.path.join(sample_dir, "dict.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"entries": BASE_DICT}, fh, ensure_ascii=False)
    with open(os.path.join(sample_dir, "dict_v2.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"entries": DICT_V2}, fh, ensure_ascii=False)

    config = {
        "concurrency": 4,
        "max_edit_batch_size": 2,
        "dict_change_mode": "incremental",
        "invalidation_metrics": ["entry_fp", "model_fp", "sent_fp"],
        "snapshot_dir": "snapshots",
        "version_policy": {"strategy": "resume"},
        "batch_sleep_ms": 0,
    }
    with open(os.path.join(sample_dir, "config.json"), "w",
              encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)

    with open(os.path.join(sample_dir, "edits.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"edits": EDITS}, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(sample_dir, "edits_signal.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"edits": SIGNAL_EDITS}, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(sample_dir, "edits_empty.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"edits": []}, fh, ensure_ascii=False)
    with open(os.path.join(sample_dir, "edits_delete.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"edits": [{"op": "delete", "text": "张三"}]}, fh,
                  ensure_ascii=False)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "samples"
    generate(out)
    print("samples generated at %s" % out)
