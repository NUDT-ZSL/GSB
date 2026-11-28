"""人工构造含故意提速、停顿与跳音的练习片段，用于验证异常判定逻辑。"""
from __future__ import annotations

import copy

import numpy as np

from .models import NormalizedRecord, NoteEvent


def _stable_seed(*parts: object) -> int:
    digest = 0
    for part in parts:
        for byte in str(part).encode("utf-8"):
            digest = (digest * 31 + byte) % (2**32)
    return digest


def inject_pattern(
    record: NormalizedRecord, injection: dict
) -> NormalizedRecord:
    """在归一化记录副本上注入人工异常，返回新记录对象。

    kind 支持：
      rush  : 区间内相邻音间隔按 factor 压缩（故意提速）；
      pause : 区间内插入 extra_silence_beats 的停顿；
      skip  : 区间内按 skip_fraction 随机删除音符（固定种子，确定性）。
    """
    result = copy.deepcopy(record)
    start = float(injection["start_beat"])
    end = float(injection["end_beat"])
    kind = injection["kind"]
    rng = np.random.default_rng(
        _stable_seed(result.student_id, result.piece_id, injection["name"])
    )

    new_events: list[NoteEvent] = []
    for voice in ("rh", "lh"):
        voice_events = sorted(
            (event for event in result.events if event.voice == voice),
            key=lambda event: event.onset_beat,
        )
        if kind == "rush":
            factor = float(injection["factor"])
            for idx, event in enumerate(voice_events):
                updated = copy.copy(event)
                if event.onset_beat >= start and event.onset_beat < end:
                    if idx > 0:
                        previous = new_events[-1].onset_beat
                        updated.onset_beat = (
                            previous
                            + (event.onset_beat - voice_events[idx - 1].onset_beat)
                            / factor
                        )
                new_events.append(updated)
        elif kind == "pause":
            silence = float(injection["extra_silence_beats"])
            for event in voice_events:
                updated = copy.copy(event)
                if event.onset_beat >= start:
                    updated.onset_beat = event.onset_beat + silence
                new_events.append(updated)
        elif kind == "skip":
            fraction = float(injection["skip_fraction"])
            for event in voice_events:
                in_window = start <= event.onset_beat < end
                if in_window and rng.random() < fraction:
                    continue
                new_events.append(copy.copy(event))
        else:
            raise ValueError(f"未知注入类型: {kind}")

    result.events = sorted(
        new_events, key=lambda event: (event.voice, event.onset_beat)
    )
    result.format_version = f"{result.format_version}+injected_{kind}"
    if result.truth_grid.size:
        for voice in ("rh", "lh"):
            voice_events = [
                event for event in result.events if event.voice == voice
            ]
            result.true_note_tempo[voice] = np.interp(
                [event.onset_beat for event in voice_events],
                result.truth_grid,
                result.truth_tempo,
            )
    result.true_sections = [
        *result.true_sections,
        {
            "kind": kind,
            "start_beat": start,
            "end_beat": end,
            "direction": kind,
            "magnitude": float(
                injection.get(
                    "factor",
                    injection.get("extra_silence_beats", injection.get("skip_fraction", 0.0)),
                )
            ),
            "injected": True,
            "name": injection["name"],
        },
    ]
    return result
