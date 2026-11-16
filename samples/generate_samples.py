"""生成确定性的本地样本：词典、HMM 与词性概率表、输入句子。"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def write_lexicon(path: Path) -> None:
    entries = [
        ("我", 1200), ("去", 800), ("北京", 900), ("上海", 700),
        ("大学", 650), ("学生", 500), ("学习", 420), ("自然", 300),
        ("语言", 320), ("处理", 310), ("喜欢", 260), ("非常", 240),
        ("研究", 280), ("生", 400), ("故宫", 180), ("参观", 210),
        ("明天", 230), ("天气", 190), ("晴朗", 120), ("很", 600),
        ("认真", 150), ("的", 3000), ("在", 1500), ("和", 1100),
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for word, frequency in sorted(entries, key=lambda item: item[0]):
            handle.write(f"{word} {frequency}\n")


def write_hmm(path: Path) -> None:
    states = ["B", "E", "M", "S"]
    transition_probs = {
        "B": {"E": 0.7, "M": 0.3},
        "M": {"E": 0.7, "M": 0.3},
        "E": {"B": 0.5, "S": 0.5},
        "S": {"B": 0.5, "S": 0.5},
    }
    initial_probs = {"B": 0.5, "S": 0.5, "E": 0.0, "M": 0.0}
    # 字 -> 各状态发射概率（和为 1）；未列出的字运行时走平滑。
    chars = {
        "猫": (0.45, 0.30, 0.10, 0.15),
        "咪": (0.10, 0.55, 0.20, 0.15),
        "星": (0.50, 0.20, 0.15, 0.15),
        "云": (0.15, 0.45, 0.25, 0.15),
        "跑": (0.30, 0.20, 0.10, 0.40),
        "跳": (0.25, 0.25, 0.10, 0.40),
        "高": (0.35, 0.30, 0.15, 0.20),
    }
    emission_probs = {
        char: dict(zip(states, values))
        for char, values in sorted(chars.items())
    }
    payload = {
        "states": states,
        "initial_probs": initial_probs,
        "transition_probs": transition_probs,
        "emission_probs": emission_probs,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_pos(path: Path) -> None:
    states = ["n", "r", "v", "a", "d", "u"]
    transition_probs = {
        "r": {"v": 0.6, "n": 0.3, "u": 0.1},
        "n": {"v": 0.35, "u": 0.25, "n": 0.25, "a": 0.15},
        "v": {"n": 0.55, "a": 0.2, "d": 0.15, "r": 0.1},
        "a": {"n": 0.6, "u": 0.2, "v": 0.2},
        "d": {"a": 0.45, "v": 0.4, "n": 0.15},
        "u": {"n": 0.5, "v": 0.3, "a": 0.2},
    }
    initial_probs = {
        "r": 0.35, "n": 0.3, "v": 0.15, "d": 0.1, "a": 0.08, "u": 0.02,
    }
    word_tags = {
        "我": {"r": 1.0},
        "去": {"v": 0.8, "n": 0.2},
        "北京": {"n": 0.9, "a": 0.1},
        "上海": {"n": 0.9, "a": 0.1},
        "大学": {"n": 1.0},
        "学生": {"n": 1.0},
        "学习": {"v": 0.8, "n": 0.2},
        "自然": {"n": 0.6, "a": 0.4},
        "语言": {"n": 1.0},
        "处理": {"v": 0.75, "n": 0.25},
        "喜欢": {"v": 1.0},
        "非常": {"d": 1.0},
        "研究": {"v": 0.6, "n": 0.4},
        "生": {"n": 0.7, "v": 0.3},
        "故宫": {"n": 1.0},
        "参观": {"v": 1.0},
        "明天": {"n": 0.7, "d": 0.3},
        "天气": {"n": 1.0},
        "晴朗": {"a": 1.0},
        "很": {"d": 1.0},
        "认真": {"a": 1.0},
        "的": {"u": 1.0},
        "在": {"v": 0.5, "d": 0.5},
        "和": {"u": 0.6, "r": 0.4},
    }
    emission_probs = {}
    for word, tag_dist in sorted(word_tags.items()):
        vector = {tag: 0.0 for tag in states}
        vector.update(tag_dist)
        emission_probs[word] = vector
    payload = {
        "states": states,
        "initial_probs": initial_probs,
        "transition_probs": transition_probs,
        "emission_probs": emission_probs,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_input(path: Path) -> None:
    lines = [
        ("s01", "我去北京"),
        ("s02", "我喜欢自然语言处理"),
        ("s03", "大学生在学习"),
        ("s04", "猫咪在跑跳"),
        ("s05", "明天天气非常晴朗"),
        ("s06", "我去参观故宫"),
        ("s07", "星云很高"),
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for sentence_id, text in lines:
            handle.write(f"{sentence_id}\t{text}\n")


def write_config(path: Path) -> None:
    config = {
        "lexicon_path": str(HERE / "lexicon.txt"),
        "hmm_path": str(HERE / "hmm_probs.json"),
        "pos_path": str(HERE / "pos_probs.json"),
        "max_word_length": 4,
        "hmm_emission_smoothing": 1e-6,
        "pos_emission_smoothing": 1e-6,
        "pos_transition_smoothing": 1e-8,
        "unknown_confidence": 0.01,
        "low_confidence_threshold": -10.0,
        "max_workers": 4,
    }
    path.write_text(
        json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> None:
    write_lexicon(HERE / "lexicon.txt")
    write_hmm(HERE / "hmm_probs.json")
    write_pos(HERE / "pos_probs.json")
    write_input(HERE / "input.txt")
    write_config(HERE / "config.json")
    print("samples generated under", HERE)


if __name__ == "__main__":
    main()
