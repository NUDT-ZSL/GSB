"""Independent ground-truth parsing and fallback reference construction."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .catalog import LIP_CHANNELS, normalize_phoneme_label, phoneme_reference
from .errors import fail
from .io_utils import read_json
from .models import PhonemeEvent, Sentence, StressMark, TruthData, VersionData


def _truth_time_us(
    item: dict[str, Any],
    side: str,
    *,
    path: Path,
    sentence_id: str,
    phoneme_id: str | None,
    time_base: str,
    fps: float | None,
) -> int:
    for key in (f"{side}_ms", f"{side}_milliseconds"):
        if key in item:
            try:
                value = float(item[key])
            except (TypeError, ValueError):
                fail(
                    "E_INVALID_NUMBER",
                    f"ground-truth {side} must be numeric",
                    path=str(path),
                    sentence_id=sentence_id,
                    phoneme_id=phoneme_id,
                )
            return int(round(value * 1000.0))
    for key in (f"{side}_frame", f"{side}_frames"):
        if key in item:
            if fps is None or fps <= 0:
                fail(
                    "E_SAMPLE_RATE_MISMATCH",
                    "frame-based ground truth requires a positive fps",
                    path=str(path),
                    sentence_id=sentence_id,
                    phoneme_id=phoneme_id,
                )
            try:
                value = float(item[key])
            except (TypeError, ValueError):
                fail(
                    "E_INVALID_NUMBER",
                    f"ground-truth {side} frame must be numeric",
                    path=str(path),
                    sentence_id=sentence_id,
                    phoneme_id=phoneme_id,
                )
            return int(round(value * 1_000_000.0 / fps))
    if side in item:
        try:
            value = float(item[side])
        except (TypeError, ValueError):
            fail(
                "E_INVALID_NUMBER",
                f"ground-truth {side} must be numeric",
                path=str(path),
                sentence_id=sentence_id,
                phoneme_id=phoneme_id,
            )
        if time_base == "frames":
            if fps is None or fps <= 0:
                fail(
                    "E_SAMPLE_RATE_MISMATCH",
                    "frame-based ground truth requires a positive fps",
                    path=str(path),
                    sentence_id=sentence_id,
                    phoneme_id=phoneme_id,
                )
            return int(round(value * 1_000_000.0 / fps))
        return int(round(value * 1000.0))
    fail(
        "E_MISSING_TIME",
        f"ground truth is missing {side} time",
        path=str(path),
        sentence_id=sentence_id,
        phoneme_id=phoneme_id,
    )


def _parse_truth_sentence(
    raw: dict[str, Any],
    *,
    path: Path,
    index: int,
    time_base: str,
    fps: float | None,
) -> Sentence:
    sentence_id = str(raw.get("id", raw.get("sentence_id", f"S{index:02d}")))
    raw_phonemes = raw.get("phonemes", raw.get("segments"))
    if not isinstance(raw_phonemes, list) or not raw_phonemes:
        fail(
            "E_MISSING_PHONEME",
            "ground-truth sentence has no phonemes",
            path=str(path),
            sentence_id=sentence_id,
        )
    phonemes: list[PhonemeEvent] = []
    seen: set[str] = set()
    for phoneme_index, item in enumerate(raw_phonemes, start=1):
        if not isinstance(item, dict):
            fail(
                "E_MISSING_PHONEME",
                "ground-truth phoneme must be an object",
                path=str(path),
                sentence_id=sentence_id,
                details={"phoneme_index": phoneme_index},
            )
        phoneme_id_raw = item.get("id", item.get("phoneme_id"))
        if phoneme_id_raw is None:
            fail(
                "E_MISSING_PHONEME",
                "ground-truth phoneme has no id",
                path=str(path),
                sentence_id=sentence_id,
                details={"phoneme_index": phoneme_index},
            )
        phoneme_id = str(phoneme_id_raw)
        if phoneme_id in seen:
            fail(
                "E_DUPLICATE_PHONEME",
                "ground-truth phoneme id occurs more than once",
                path=str(path),
                sentence_id=sentence_id,
                phoneme_id=phoneme_id,
            )
        seen.add(phoneme_id)
        label = item.get("canonical", item.get("label", item.get("phoneme")))
        if label is None:
            fail(
                "E_MISSING_PHONEME",
                "ground-truth phoneme has no canonical label",
                path=str(path),
                sentence_id=sentence_id,
                phoneme_id=phoneme_id,
            )
        canonical = normalize_phoneme_label(str(label), "canonical")
        if canonical is None:
            fail(
                "E_UNKNOWN_PHONEME",
                f"unknown ground-truth canonical label {label!r}",
                path=str(path),
                sentence_id=sentence_id,
                phoneme_id=phoneme_id,
            )
        start_us = _truth_time_us(
            item,
            "start",
            path=path,
            sentence_id=sentence_id,
            phoneme_id=phoneme_id,
            time_base=time_base,
            fps=fps,
        )
        end_us = _truth_time_us(
            item,
            "end",
            path=path,
            sentence_id=sentence_id,
            phoneme_id=phoneme_id,
            time_base=time_base,
            fps=fps,
        )
        if end_us <= start_us:
            fail(
                "E_INVALID_INTERVAL",
                "ground-truth phoneme end must follow start",
                path=str(path),
                sentence_id=sentence_id,
                phoneme_id=phoneme_id,
            )
        reference = phoneme_reference(canonical)
        supplied_targets = item.get("reference_targets", item.get("targets"))
        targets: dict[str, float] = {}
        if isinstance(supplied_targets, dict):
            for channel in LIP_CHANNELS:
                if channel not in supplied_targets:
                    fail(
                        "E_GROUND_TRUTH",
                        f"ground-truth reference targets lack {channel}",
                        path=str(path),
                        sentence_id=sentence_id,
                        phoneme_id=phoneme_id,
                    )
                try:
                    targets[channel] = float(supplied_targets[channel])
                except (TypeError, ValueError):
                    fail(
                        "E_GROUND_TRUTH",
                        f"ground-truth target {channel} is not numeric",
                        path=str(path),
                        sentence_id=sentence_id,
                        phoneme_id=phoneme_id,
                    )
        else:
            targets = dict(reference["targets"])
        phonemes.append(
            PhonemeEvent(
                id=phoneme_id,
                source_label=str(label),
                canonical=canonical,
                category=str(item.get("category", reference["category"])),
                viseme=str(item.get("viseme", reference["viseme"])),
                start_us=start_us,
                end_us=end_us,
                targets=targets,
            )
        )
    phonemes.sort(key=lambda item: (item.start_us, item.end_us, item.id))
    for previous, current in zip(phonemes, phonemes[1:]):
        if current.start_us < previous.end_us:
            fail(
                "E_TIME_OVERLAP",
                f"ground-truth phoneme {current.id} overlaps {previous.id}",
                path=str(path),
                sentence_id=sentence_id,
                phoneme_id=current.id,
                previous_phoneme_id=previous.id,
            )

    emotion = raw.get("emotion", "neutral")
    emotion_intensity = 0.0
    anchor_id = raw.get("emotion_anchor_phoneme_id", raw.get("emotion_anchor_id"))
    if isinstance(emotion, dict):
        emotion_label = str(emotion.get("label", "neutral"))
        emotion_intensity = float(emotion.get("intensity", 0.0))
        anchor_id = emotion.get("anchor_phoneme_id", anchor_id)
    else:
        emotion_label = str(emotion or "neutral")
        emotion_intensity = float(raw.get("emotion_intensity", 0.0))
    if anchor_id is not None:
        anchor_id = str(anchor_id)
        if not any(item.id == anchor_id for item in phonemes):
            fail(
                "E_MISSING_PHONEME",
                "ground-truth emotion anchor is absent",
                path=str(path),
                sentence_id=sentence_id,
                phoneme_id=anchor_id,
            )

    stress: list[StressMark] = []
    raw_stress = raw.get("stress", raw.get("stress_marks", []))
    if raw_stress is None:
        raw_stress = []
    if not isinstance(raw_stress, list):
        raw_stress = [raw_stress]
    for item in raw_stress:
        if isinstance(item, str):
            stress_id, level = item, 1.0
        elif isinstance(item, dict):
            stress_id = item.get("phoneme_id", item.get("id"))
            level = float(item.get("level", item.get("strength", 1.0)))
        else:
            stress_id, level = None, 1.0
        if stress_id is None:
            fail(
                "E_MISSING_PHONEME",
                "ground-truth stress entry has no phoneme id",
                path=str(path),
                sentence_id=sentence_id,
            )
        stress_id = str(stress_id)
        if not any(phoneme.id == stress_id for phoneme in phonemes):
            fail(
                "E_MISSING_PHONEME",
                "ground-truth stress phoneme is absent",
                path=str(path),
                sentence_id=sentence_id,
                phoneme_id=stress_id,
            )
        stress.append(StressMark(stress_id, max(0.0, min(1.0, level))))
    start_us = min(item.start_us for item in phonemes)
    end_us = max(item.end_us for item in phonemes)
    if raw.get("start_ms") is not None:
        start_us = min(start_us, int(round(float(raw["start_ms"]) * 1000.0)))
    if raw.get("end_ms") is not None:
        end_us = max(end_us, int(round(float(raw["end_ms"]) * 1000.0)))
    return Sentence(
        id=sentence_id,
        text=str(raw.get("text", "")),
        start_us=start_us,
        end_us=end_us,
        emotion_label=emotion_label,
        emotion_intensity=max(0.0, min(1.0, emotion_intensity)),
        emotion_anchor_id=anchor_id,
        stress=stress,
        phonemes=phonemes,
    )


def parse_truth(path: str | Path) -> TruthData:
    truth_path = Path(path).expanduser().resolve()
    raw = read_json(truth_path)
    if not isinstance(raw, dict):
        fail("E_GROUND_TRUTH", "ground-truth root must be an object", path=str(truth_path))
    raw_sentences = raw.get("sentences")
    if not isinstance(raw_sentences, list) or not raw_sentences:
        fail(
            "E_MISSING_PHONEME",
            "ground truth has no sentences",
            path=str(truth_path),
        )
    time_base = str(raw.get("time_base", "milliseconds")).strip().lower()
    if time_base in {"ms", "msec", "millisecond"}:
        time_base = "milliseconds"
    if time_base in {"frame", "fps"}:
        time_base = "frames"
    if time_base not in {"milliseconds", "frames"}:
        fail(
            "E_TIME_BASE",
            f"unsupported ground-truth time base {time_base!r}",
            path=str(truth_path),
        )
    fps_value = raw.get("fps", raw.get("frame_rate"))
    try:
        fps = float(fps_value) if fps_value is not None else None
    except (TypeError, ValueError):
        fail(
            "E_INVALID_NUMBER",
            "ground-truth fps must be numeric",
            path=str(truth_path),
            value=fps_value,
        )
    if fps is not None and (not math.isfinite(fps) or fps <= 0.0):
        fail(
            "E_SAMPLE_RATE_MISMATCH",
            "ground-truth fps must be finite and positive",
            path=str(truth_path),
            fps=fps,
        )
    sentences = [
        _parse_truth_sentence(
            item,
            path=truth_path,
            index=index,
            time_base=time_base,
            fps=fps,
        )
        for index, item in enumerate(raw_sentences, start=1)
        if isinstance(item, dict)
    ]
    if len(sentences) != len(raw_sentences):
        fail(
            "E_GROUND_TRUTH",
            "ground-truth sentence entries must be objects",
            path=str(truth_path),
        )
    sentence_ids = [sentence.id for sentence in sentences]
    if len(set(sentence_ids)) != len(sentence_ids):
        fail(
            "E_DUPLICATE_SENTENCE",
            "ground-truth sentence id occurs more than once",
            path=str(truth_path),
        )
    sentences.sort(key=lambda item: (item.start_us, item.id))
    for previous, current in zip(sentences, sentences[1:]):
        if current.start_us < previous.end_us:
            fail(
                "E_TIME_OVERLAP",
                f"ground-truth sentence {current.id} overlaps {previous.id}",
                path=str(truth_path),
                sentence_id=current.id,
                previous_sentence_id=previous.id,
            )
    all_phonemes = [item for sentence in sentences for item in sentence.phonemes]
    all_ids = [item.id for item in all_phonemes]
    if len(set(all_ids)) != len(all_ids):
        fail(
            "E_DUPLICATE_PHONEME",
            "ground-truth phoneme id occurs more than once",
            path=str(truth_path),
        )
    all_phonemes.sort(key=lambda item: (item.start_us, item.end_us, item.id))
    for previous, current in zip(all_phonemes, all_phonemes[1:]):
        if current.start_us < previous.end_us:
            fail(
                "E_TIME_OVERLAP",
                "ground-truth phonemes overlap across sentences",
                path=str(truth_path),
                phoneme_id=current.id,
                previous_phoneme_id=previous.id,
            )
    duration_us = max(item.end_us for item in all_phonemes)
    if raw.get("duration_ms") is not None:
        duration_us = max(duration_us, int(round(float(raw["duration_ms"]) * 1000.0)))
    return TruthData(path=truth_path, duration_us=duration_us, sentences=sentences, source="file")


def fallback_truth(baseline: VersionData) -> TruthData:
    sentences = [
        Sentence(
            id=sentence.id,
            text=sentence.text,
            start_us=sentence.start_us,
            end_us=sentence.end_us,
            emotion_label=sentence.emotion_label,
            emotion_intensity=sentence.emotion_intensity,
            emotion_anchor_id=sentence.emotion_anchor_id,
            stress=[StressMark(item.phoneme_id, item.level) for item in sentence.stress],
            phonemes=[
                PhonemeEvent(
                    id=phoneme.id,
                    source_label=phoneme.source_label,
                    canonical=phoneme.canonical,
                    category=phoneme.category,
                    viseme=phoneme.viseme,
                    start_us=phoneme.start_us,
                    end_us=phoneme.end_us,
                    targets=dict(phoneme.targets),
                )
                for phoneme in sentence.phonemes
            ],
        )
        for sentence in baseline.sentences
    ]
    return TruthData(
        path=None,
        duration_us=baseline.duration_us,
        sentences=sentences,
        source="synthesized-from-normalized-baseline",
    )
