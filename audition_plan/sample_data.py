"""确定性生成输入样本：事件表 JSON、片段清单 JSON 与占位音频文件。

样本按场景分目录生成，每个目录都包含可直接喂给 CLI 的 ``events.json`` /
``clips.json`` 以及 ``audio/`` 下的占位音频文件（内容固定，不依赖网络）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

# 每个场景：events / clips；clip 带 "missing": true 表示清单登记了但文件故意缺失。
SCENARIOS: dict[str, dict[str, Any]] = {
    # 业务主样例：多数事件可匹配，包含一个标签无匹配事件与一个文件缺失片段。
    "base": {
        "title": "技能冷却事件主样例",
        "events": [
            {"event_id": "evt_fire_nova", "time_sec": 0.0, "required_tags": ["爆炸", "火焰"]},
            {"event_id": "evt_ice_shield", "time_sec": 0.5, "required_tags": ["护盾", "冰"]},
            {"event_id": "evt_storm_chain", "time_sec": 1.0, "required_tags": ["闪电"]},
            {"event_id": "evt_shadow_step", "time_sec": 1.6, "required_tags": ["位移", "暗影"]},
            {"event_id": "evt_holy_light", "time_sec": 2.4, "required_tags": ["治疗"]},
        ],
        "clips": [
            {"clip_id": "fire_burst", "tags": ["爆炸", "火焰"], "duration_sec": 1.0},
            {"clip_id": "frost_shield", "tags": ["护盾", "冰"], "duration_sec": 1.0},
            {"clip_id": "thunder_chain", "tags": ["闪电", "连锁"], "duration_sec": 1.2},
            {"clip_id": "heal_tick", "tags": ["治疗"], "duration_sec": 0.8},
            {"clip_id": "fire_burst_alt", "tags": ["爆炸", "火焰"], "duration_sec": 0.9},
            {"clip_id": "ghost_boom", "tags": ["爆炸"], "duration_sec": 1.0, "missing": True},
        ],
    },
    # (1) 3 个事件只有 2 个可用片段：第 3 个事件缺失。
    "01_insufficient_clips": {
        "title": "片段不足",
        "events": [
            {"event_id": "evt_1", "time_sec": 0.0, "required_tags": ["命中"]},
            {"event_id": "evt_2", "time_sec": 1.0, "required_tags": ["命中"]},
            {"event_id": "evt_3", "time_sec": 2.0, "required_tags": ["命中"]},
        ],
        "clips": [
            {"clip_id": "hit_a", "tags": ["命中"], "duration_sec": 1.0},
            {"clip_id": "hit_b", "tags": ["命中"], "duration_sec": 1.0},
        ],
    },
    # (2) 事件要求"爆炸"，片段池里没有任何爆炸片段：该事件缺失，其他事件不受影响。
    "02_tag_no_match": {
        "title": "标签无匹配",
        "events": [
            {"event_id": "evt_boom", "time_sec": 0.0, "required_tags": ["爆炸"]},
            {"event_id": "evt_heal", "time_sec": 1.0, "required_tags": ["治疗"]},
        ],
        "clips": [
            {"clip_id": "heal_soft", "tags": ["治疗"], "duration_sec": 1.0},
            {"clip_id": "ui_ping", "tags": ["提示"], "duration_sec": 0.4},
        ],
    },
    # (4) 两事件相隔 0.5 秒，片段均 1 秒：两行都应有重叠警告。
    "04_overlap": {
        "title": "时间轴重叠",
        "events": [
            {"event_id": "evt_cast_a", "time_sec": 0.0, "required_tags": ["施法"]},
            {"event_id": "evt_cast_b", "time_sec": 0.5, "required_tags": ["施法"]},
        ],
        "clips": [
            {"clip_id": "cast_a", "tags": ["施法"], "duration_sec": 1.0},
            {"clip_id": "cast_b", "tags": ["施法"], "duration_sec": 1.0},
        ],
    },
    # (5) 片段清单含不存在的文件：该片段记为不可用并被跳过，其他片段正常匹配。
    "05_missing_file": {
        "title": "片段文件缺失",
        "events": [
            {"event_id": "evt_boom", "time_sec": 0.0, "required_tags": ["爆炸"]},
            {"event_id": "evt_ice", "time_sec": 1.0, "required_tags": ["冰"]},
        ],
        "clips": [
            {"clip_id": "boom_ok", "tags": ["爆炸"], "duration_sec": 1.0},
            {"clip_id": "ice_gone", "tags": ["冰"], "duration_sec": 1.0, "missing": True},
            {"clip_id": "ice_ok", "tags": ["冰"], "duration_sec": 1.0},
        ],
    },
    # (6) 事件时间 2,1,2：排序后为 1,2,2，两个 2 秒事件按出现顺序匹配不同片段。
    "06_unordered_duplicate": {
        "title": "乱序与重复时间",
        "events": [
            {"event_id": "evt_late_first", "time_sec": 2.0, "required_tags": ["待机"]},
            {"event_id": "evt_early", "time_sec": 1.0, "required_tags": ["待机"]},
            {"event_id": "evt_late_second", "time_sec": 2.0, "required_tags": ["待机"]},
        ],
        "clips": [
            {"clip_id": "idle_a", "tags": ["待机"], "duration_sec": 0.5},
            {"clip_id": "idle_b", "tags": ["待机"], "duration_sec": 0.5},
            {"clip_id": "idle_c", "tags": ["待机"], "duration_sec": 0.5},
        ],
    },
    # (9) 格式错误：第二个事件缺少必需字段 time_sec。
    "09_invalid_missing_field": {
        "title": "缺少必需字段",
        "events": [
            {"event_id": "evt_ok", "time_sec": 0.0, "required_tags": ["命中"]},
            {"event_id": "evt_broken", "required_tags": ["命中"]},
        ],
        "clips": [
            {"clip_id": "hit_a", "tags": ["命中"], "duration_sec": 1.0},
        ],
    },
}


def _audio_bytes(keyword: str) -> bytes:
    """生成固定的占位音频字节，仅用于"文件可读"判定，不参与音频解码。"""

    return b"RIFF\x00\x00\x00\x00WAVEfmt " + b"placeholder:" + keyword.encode("ascii")


def _clip_path(clip_id: str) -> str:
    return f"audio/{clip_id}.wav"


def write_scenario(base: str | Path, name: str) -> Path:
    if name not in SCENARIOS:
        raise KeyError(f"未知场景: {name}")
    spec = SCENARIOS[name]
    root = Path(base) / name
    (root / "audio").mkdir(parents=True, exist_ok=True)

    clip_payload: list[dict[str, Any]] = []
    for clip in spec["clips"]:
        relative = _clip_path(clip["clip_id"])
        if not clip.get("missing"):
            (root / relative).write_bytes(_audio_bytes(clip["clip_id"]))
        clip_payload.append(
            {
                "clip_id": clip["clip_id"],
                "path": relative,
                "tags": list(clip["tags"]),
                "duration_sec": clip["duration_sec"],
            }
        )

    # 原样保留场景里的字段（包括"故意缺字段"的格式错误样本），不补齐默认值。
    event_payload = [
        {key: list(value) if isinstance(value, list) else value
         for key, value in event.items()}
        for event in spec["events"]
    ]
    _dump(root / "events.json", {"events": event_payload})
    _dump(root / "clips.json", {"clips": clip_payload})
    return root


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate_all(base: str | Path = "samples", names: Iterable[str] | None = None) -> list[Path]:
    return [write_scenario(base, name) for name in (names or SCENARIOS)]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    base = Path(args[0]) if args else Path("samples")
    roots = generate_all(base)
    print(f"已生成 {len(roots)} 个确定性样例场景到 {base.resolve()}")
    for root in roots:
        print(f"  {root.name}: {SCENARIOS[root.name]['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
