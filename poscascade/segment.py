from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Callable, Iterable

from .canonical import quantize
from .config import PipelineConfig
from .errors import BusinessError, ErrorCode
from .models import Lexicon, LexiconEntry


def _build_index(lexicon: Lexicon) -> dict[str, list[LexiconEntry]]:
    index: dict[str, list[LexiconEntry]] = defaultdict(list)
    for entry in lexicon.entries:
        index[entry.word[0]].append(entry)
    for first in index:
        index[first].sort(key=lambda entry: (entry.word, entry.index))
    return dict(index)


def _matching_entries(
    text: str,
    start: int,
    index: dict[str, list[LexiconEntry]],
) -> list[LexiconEntry]:
    matches: list[LexiconEntry] = []
    for entry in index.get(text[start], ()):
        if text.startswith(entry.word, start):
            matches.append(entry)
    return matches


def _count_line_candidates(
    line: dict[str, Any],
    index: dict[str, list[LexiconEntry]],
) -> int:
    text = line["text"]
    return sum(
        len(_matching_entries(text, start, index))
        for start in range(len(text))
    )


def _candidate_sort_key(entry: LexiconEntry) -> tuple[int, int, str, int]:
    return (-entry.freq, -len(entry.word), entry.word, entry.index)


def _path_is_better(
    new_score: float,
    new_tokens: tuple[dict[str, Any], ...],
    current_score: float,
    current_tokens: tuple[dict[str, Any], ...],
) -> bool:
    new_quantized = quantize(new_score)
    current_quantized = quantize(current_score)
    if new_quantized != current_quantized:
        return new_quantized > current_quantized
    new_starts = tuple(token["start"] for token in new_tokens)
    current_starts = tuple(token["start"] for token in current_tokens)
    if new_starts != current_starts:
        return new_starts < current_starts
    new_ends = tuple(token["end"] for token in new_tokens)
    current_ends = tuple(token["end"] for token in current_tokens)
    if new_ends != current_ends:
        return new_ends < current_ends
    new_words = tuple(token["word"] for token in new_tokens)
    current_words = tuple(token["word"] for token in current_tokens)
    return new_words < current_words


def _segment_line(
    line: dict[str, Any],
    lexicon: Lexicon,
    config: PipelineConfig,
    index: dict[str, list[LexiconEntry]],
    *,
    cap: int | None,
) -> tuple[dict[str, Any], int]:
    text = line["text"]
    length = len(text)
    paths: list[dict[str, Any] | None] = [None] * (length + 1)
    paths[0] = {"score": 0.0, "tokens": ()}
    comparisons = 0
    dropped = 0

    for start in range(length):
        path = paths[start]
        if path is None:
            continue
        matches = _matching_entries(text, start, index)
        candidates = sorted(matches, key=_candidate_sort_key)
        if cap is not None and len(candidates) > cap:
            dropped += len(candidates) - cap
            candidates = candidates[:cap]
        comparisons += len(matches) if cap is None else len(candidates)
        if not candidates:
            candidates = []

        for entry in candidates:
            end = start + len(entry.word)
            edge_score = math.log(float(entry.freq))
            token = {
                "word": entry.word,
                "start": start,
                "end": end,
                "codepoint_len": end - start,
                "freq": entry.freq,
                "edge_score": quantize(edge_score),
                "source": "dict",
                "pos_candidates": list(entry.pos),
            }
            new_score = float(path["score"]) + edge_score
            new_tokens = tuple(path["tokens"]) + (token,)
            current = paths[end]
            if current is None or _path_is_better(
                new_score,
                new_tokens,
                float(current["score"]),
                tuple(current["tokens"]),
            ):
                paths[end] = {"score": new_score, "tokens": new_tokens}

        if not matches:
            word = text[start]
            fallback_score = math.log(1.0)
            token = {
                "word": word,
                "start": start,
                "end": start + 1,
                "codepoint_len": 1,
                "freq": 1,
                "edge_score": quantize(fallback_score),
                "source": "fallback",
                "pos_candidates": [config.fallback_pos],
            }
            new_score = float(path["score"]) + fallback_score
            new_tokens = tuple(path["tokens"]) + (token,)
            current = paths[start + 1]
            if current is None or _path_is_better(
                new_score,
                new_tokens,
                float(current["score"]),
                tuple(current["tokens"]),
            ):
                paths[start + 1] = {"score": new_score, "tokens": new_tokens}

    if paths[length] is None:
        raise BusinessError(
            ErrorCode.E_OFFSET_LEN,
            "无法构造覆盖全行的分词路径",
            doc_id=line["doc_id"],
            line_no=line["line_no"],
            stage="segment",
            details={"codepoint_len": length},
        )

    degradations: list[dict[str, Any]] = []
    if dropped:
        degradations.append(
            {
                "category": "SEGMENT_CANDIDATE_CAP",
                "doc_id": line["doc_id"],
                "line_no": line["line_no"],
                "stage": "segment",
                "detail": {
                    "max_candidates_per_start": cap,
                    "dropped_candidate_edges": dropped,
                    "offset_unit": "unicode_codepoint",
                },
            }
        )

    tokens = list(paths[length]["tokens"])
    return (
        {
            "doc_id": line["doc_id"],
            "line_no": line["line_no"],
            "punct_class": line["punct_class"],
            "text_sha256": line["text_sha256"],
            "text_codepoint_len": length,
            "token_count": len(tokens),
            "path_score": quantize(float(paths[length]["score"])),
            "tokens": tokens,
            "degradations": degradations,
        },
        comparisons,
    )


def inspect_segment_budget(
    validation_payload: dict[str, Any],
    lexicon: Lexicon,
    config: PipelineConfig,
) -> tuple[list[int], int, bool]:
    index = _build_index(lexicon)
    counts: list[int] = []
    total = 0
    for line in validation_payload["lines"]:
        count = _count_line_candidates(line, index)
        counts.append(count)
        total += count
    return counts, total, total > config.max_candidate_edges_per_stage


def run_segmentation(
    validation_payload: dict[str, Any],
    lexicon: Lexicon,
    config: PipelineConfig,
    *,
    reusable_lines: dict[str, dict[str, Any]] | None = None,
    recompute_doc_ids: Iterable[str] | None = None,
    progress_hook: Callable[[str, int, str], None] | None = None,
) -> dict[str, Any]:
    index = _build_index(lexicon)
    lines_in = validation_payload["lines"]
    counts = [_count_line_candidates(line, index) for line in lines_in]
    total_candidates = sum(counts)
    budget_exceeded = total_candidates > config.max_candidate_edges_per_stage
    cap_mode = False

    if budget_exceeded:
        if config.candidate_limit_policy == "error":
            cursor = 0
            for line, count in zip(lines_in, counts):
                cursor += count
                if cursor > config.max_candidate_edges_per_stage:
                    raise BusinessError(
                        ErrorCode.E_RESOURCE_LIMIT,
                        "词典候选比较次数超过配置上限",
                        doc_id=line["doc_id"],
                        line_no=line["line_no"],
                        stage="segment",
                        details={
                            "candidate_comparisons": cursor,
                            "max_candidate_edges_per_stage": config.max_candidate_edges_per_stage,
                            "policy": config.candidate_limit_policy,
                        },
                    )
        cap_mode = True

    cap = config.degrade_max_candidates_per_start if cap_mode else None
    recompute = set(recompute_doc_ids) if recompute_doc_ids is not None else None
    reusable_lines = reusable_lines or {}
    output_lines: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []
    actual_comparisons = 0

    for processed, line in enumerate(lines_in, start=1):
        doc_id = line["doc_id"]
        if recompute is not None and doc_id not in recompute and doc_id in reusable_lines:
            output_line = reusable_lines[doc_id]
            actual_comparisons += 0
        else:
            output_line, line_comparisons = _segment_line(
                line,
                lexicon,
                config,
                index,
                cap=cap,
            )
            actual_comparisons += line_comparisons
        output_lines.append(output_line)
        degradations.extend(output_line.get("degradations", []))
        if progress_hook is not None:
            progress_hook("segment", processed, doc_id)

    return {
        "stage": "segment",
        "dictionary": lexicon.to_ref(),
        "config_checksum": config.checksum,
        "candidate_budget": config.max_candidate_edges_per_stage,
        "candidate_edges_discovered": total_candidates,
        "candidate_comparisons": actual_comparisons,
        "candidate_budget_exceeded": budget_exceeded,
        "candidate_cap_mode": cap_mode,
        "candidate_cap_per_start": cap,
        "line_count": len(output_lines),
        "lines": output_lines,
        "degradations": degradations,
    }
