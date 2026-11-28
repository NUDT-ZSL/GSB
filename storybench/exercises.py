"""Paired cloze and same-POS wrong-word exercise generation."""

from __future__ import annotations

import hashlib
import random
from typing import Any

from .model import MaskedDenoisingScorer
from .text import detokenize, replace_word_at_ordinal, split_sentences, tokenize, words


CONTENT_POS = {
    "NOUN",
    "VERB_PAST",
    "VERB_BASE",
    "VERB_ING",
    "VERB_ED",
    "ADJ",
    "ADV",
}


def _stable_seed(*parts: object) -> int:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


class ExerciseGenerator:
    def __init__(
        self,
        lexicon: dict[str, dict[str, str]],
        scorer: MaskedDenoisingScorer,
        entity_aliases: dict[str, list[str]] | None = None,
    ):
        self.lexicon = lexicon
        self.scorer = scorer
        self.entity_aliases = {
            key.lower(): [item.lower() for item in values]
            for key, values in (entity_aliases or {}).items()
        }

    def _entity_surfaces(self, key_entities: list[str]) -> set[str]:
        surfaces = set()
        for entity in key_entities:
            normalized = entity.lower()
            surfaces.add(normalized)
            surfaces.update(self.entity_aliases.get(normalized, []))
        return surfaces

    def _same_pos_candidates(
        self,
        target_word: str,
        target_info: dict[str, Any],
        key_entities: list[str],
        allowed_words: list[str],
    ) -> list[str]:
        normalized = target_word.lower()
        pos = target_info.get("pos")
        target_class = target_info.get("class", "unknown")
        target_frame = target_info.get("frame")
        if not pos or pos not in CONTENT_POS:
            return []
        entity_surfaces = self._entity_surfaces(key_entities)
        allowed = {word.lower() for word in allowed_words}
        candidates = []
        same_class_fallback = []
        for word in sorted(self.lexicon):
            info = self.lexicon[word]
            if word == normalized or word in entity_surfaces or word not in allowed:
                continue
            if info.get("pos") != pos:
                continue
            if info.get("number") != target_info.get("number"):
                continue
            if target_frame and info.get("frame") != target_frame:
                continue
            if info.get("class", "unknown") != target_class:
                candidates.append(word)
            else:
                same_class_fallback.append(word)
        candidates.extend(same_class_fallback)
        if candidates:
            return candidates
        fallback = []
        fallback_same_class = []
        for word in sorted(self.lexicon):
            info = self.lexicon[word]
            if word == normalized or word in entity_surfaces:
                continue
            if info.get("pos") != pos:
                continue
            if info.get("number") != target_info.get("number"):
                continue
            if target_frame and info.get("frame") != target_frame:
                continue
            if info.get("class", "unknown") != target_class:
                fallback.append(word)
            else:
                fallback_same_class.append(word)
        return fallback + fallback_same_class

    def _contextual_entry(
        self,
        word: str,
        sentence_words: list[str],
        ordinal: int,
    ) -> dict[str, Any]:
        base = dict(self.lexicon.get(word, {}))
        alternates = base.get("alternates", [])
        if not alternates:
            return base
        previous = sentence_words[ordinal - 2].lower() if ordinal > 1 else ""
        following = (
            sentence_words[ordinal].lower()
            if ordinal < len(sentence_words)
            else ""
        )
        previous_info = self.lexicon.get(previous, {})
        following_info = self.lexicon.get(following, {})
        for alternate in alternates:
            alternate_pos = alternate.get("pos")
            if alternate_pos == "ADJ" and following_info.get("pos") in {
                "NOUN",
                "PROPN",
            }:
                if previous_info.get("pos") in {"DET", "ADJ", "NUM"} or ordinal == 1:
                    merged = dict(base)
                    merged.update(alternate)
                    return merged
            if (
                alternate_pos == "ADV"
                and previous_info.get("pos", "").startswith("VERB_")
            ):
                merged = dict(base)
                merged.update(alternate)
                return merged
        return base

    def _best_distractor(
        self,
        fragment: str,
        target_ordinal: int,
        original_probability: float,
        target_word: str,
        target_info: dict[str, Any],
        candidates: list[str],
        rng: random.Random,
    ) -> dict[str, Any] | None:
        tokens = tokenize(fragment)
        scored = []
        for candidate in candidates:
            perturbed_tokens = replace_word_at_ordinal(tokens, target_ordinal - 1, candidate)
            perturbed = detokenize(perturbed_tokens)
            position_scores = self.scorer.score_sentence(perturbed)
            if len(position_scores) < target_ordinal:
                continue
            score = position_scores[target_ordinal - 1]
            candidate_info = self.lexicon.get(candidate, {})
            scored.append(
                {
                    "word": candidate,
                    "perturbed_sentence": perturbed,
                    "log_probability": score.log_probability,
                    "probability": score.probability,
                    "rank": score.rank,
                    "pos": candidate_info.get("pos"),
                    "class": candidate_info.get("class"),
                    "pos_match": candidate_info.get("pos") == target_info.get("pos"),
                    "number_match": candidate_info.get("number")
                    == target_info.get("number"),
                    "frame_match": candidate_info.get("frame")
                    == target_info.get("frame"),
                    "semantic_class_changed": candidate_info.get("class")
                    != target_info.get("class"),
                }
            )
        if not scored:
            return None
        scored.sort(key=lambda item: (item["log_probability"], item["word"]))
        different_class = [
            item for item in scored if item["semantic_class_changed"]
        ]
        eligible = different_class or scored
        shortlist = eligible[: min(3, len(eligible))]
        choice = rng.choice(shortlist)
        choice = dict(choice)
        choice["original_log_probability"] = original_probability
        choice["context_margin"] = original_probability - choice["log_probability"]
        choice["context_inappropriate"] = (
            choice["log_probability"] < original_probability
        )
        return choice

    def generate_for_unit(
        self,
        *,
        story_id: str,
        sentence_id: str,
        level_id: str,
        unit_id: str,
        rewritten: str,
        key_entities: list[str],
        allowed_words: list[str],
        seed: int,
    ) -> list[dict[str, Any]]:
        exercises: list[dict[str, Any]] = []
        entity_surfaces = self._entity_surfaces(key_entities)
        fragment_offset = 0
        for fragment_index, fragment in enumerate(split_sentences(rewritten), start=1):
            fragment_words = words(fragment)
            position_scores = self.scorer.score_sentence(fragment)
            score_by_ordinal = {
                score.word_ordinal: score for score in position_scores
            }
            candidates = []
            for ordinal, token in enumerate(fragment_words, start=1):
                normalized = token.lower()
                info = self._contextual_entry(normalized, fragment_words, ordinal)
                if normalized in entity_surfaces:
                    continue
                if info.get("pos") not in CONTENT_POS:
                    continue
                distractors = self._same_pos_candidates(
                    normalized, info, key_entities, allowed_words
                )
                if not distractors:
                    continue
                score = score_by_ordinal.get(ordinal)
                if score is None:
                    continue
                candidates.append(
                    {
                        "ordinal": ordinal,
                        "token": token,
                        "normalized": normalized,
                        "pos": info.get("pos"),
                        "frame": info.get("frame"),
                        "class": info.get("class"),
                        "entry": info,
                        "predictability": score.probability,
                        "rank": score.rank,
                        "restored_word": score.restored_word,
                        "distractors": distractors,
                    }
                )
            candidates.sort(
                key=lambda item: (
                    item["predictability"],
                    -item["rank"],
                    item["ordinal"],
                    item["normalized"],
                )
            )
            chosen = None
            distractor = None
            for candidate in candidates:
                rng = random.Random(
                    _stable_seed(
                        seed,
                        story_id,
                        level_id,
                        sentence_id,
                        fragment_index,
                        candidate["ordinal"],
                        candidate["normalized"],
                    )
                )
                possible = self._best_distractor(
                    fragment,
                    candidate["ordinal"],
                    candidate["predictability"],
                    candidate["normalized"],
                    candidate["entry"],
                    candidate["distractors"],
                    rng,
                )
                if possible and possible["context_inappropriate"]:
                    chosen = candidate
                    distractor = possible
                    break
            if chosen is None or distractor is None:
                exercises.append(
                    {
                        "status": "skipped",
                        "story_id": story_id,
                        "level_id": level_id,
                        "sentence_id": sentence_id,
                        "unit_id": unit_id,
                        "fragment_index": fragment_index,
                        "original_sentence": fragment,
                        "reason": "no non-entity content position had a legal same-POS distractor",
                    }
                )
                fragment_offset += len(fragment_words)
                continue
            ordinal = chosen["ordinal"]
            tokens = tokenize(fragment)
            blank_tokens = list(tokens)
            index = [
                token_index
                for token_index, token in enumerate(tokens)
                if token[:1].isalpha()
            ][ordinal - 1]
            blank_tokens[index] = "___"
            cloze_sentence = detokenize(blank_tokens)
            exercise_number = len(exercises) + 1
            exercise_id = (
                f"{story_id}-{level_id}-{sentence_id}-F{fragment_index:02d}-EX{exercise_number:02d}"
            )
            unit_word_ordinal = fragment_offset + ordinal
            position = {
                "fragment_index": fragment_index,
                "fragment_word_ordinal": ordinal,
                "unit_word_ordinal": unit_word_ordinal,
                "token_index": index,
                "original_word": chosen["token"],
            }
            exercises.append(
                {
                    "status": "generated",
                    "exercise_id": exercise_id,
                    "story_id": story_id,
                    "level_id": level_id,
                    "sentence_id": sentence_id,
                    "unit_id": unit_id,
                    "fragment_index": fragment_index,
                    "target_key_entities": list(key_entities),
                    "cloze": {
                        "cloze_id": exercise_id + "-C",
                        "sentence": cloze_sentence,
                        "answer": chosen["token"],
                        "blank": "___",
                        "position": position,
                        "target_pos": chosen["pos"],
                        "selection_reason": (
                            "lowest masked-denoising predictability among non-entity "
                            "content words with a legal same-POS distractor"
                        ),
                        "not_key_entity": chosen["normalized"] not in entity_surfaces,
                        "model_evidence": {
                            "model_version": self.scorer.MODEL_VERSION,
                            "original_log_probability": round(
                                score_by_ordinal[ordinal].log_probability, 9
                            ),
                            "probability": round(chosen["predictability"], 9),
                            "rank": chosen["rank"],
                            "restored_word": chosen["restored_word"],
                        },
                    },
                    "wrong_word": {
                        "wrong_word_id": exercise_id + "-W",
                        "original_sentence": fragment,
                        "perturbed_sentence": distractor["perturbed_sentence"],
                        "position": position,
                        "original_word": chosen["token"],
                        "replacement_word": distractor["word"],
                        "target_pos": chosen["pos"],
                        "replacement_pos": distractor["pos"],
                        "pos_match": distractor["pos_match"],
                        "number_match": distractor["number_match"],
                        "target_frame": chosen["frame"],
                        "replacement_frame": distractor.get("frame"),
                        "frame_match": distractor["frame_match"],
                        "syntax_position_legal": distractor["pos_match"]
                        and distractor["number_match"]
                        and distractor["frame_match"],
                        "semantic_class_changed": distractor[
                            "semantic_class_changed"
                        ],
                        "context_inappropriate": distractor[
                            "context_inappropriate"
                        ],
                        "seed": _stable_seed(
                            seed,
                            story_id,
                            level_id,
                            sentence_id,
                            fragment_index,
                            ordinal,
                            chosen["normalized"],
                        ),
                        "model_evidence": {
                            "model_version": self.scorer.MODEL_VERSION,
                            "original_log_probability": round(
                                distractor["original_log_probability"], 9
                            ),
                            "replacement_log_probability": round(
                                distractor["log_probability"], 9
                            ),
                            "context_margin": round(distractor["context_margin"], 9),
                            "replacement_rank": distractor["rank"],
                        },
                    },
                    "pairing": {
                        "one_to_one": True,
                        "cloze_id": exercise_id + "-C",
                        "wrong_word_id": exercise_id + "-W",
                        "shared_target_word": chosen["token"],
                        "shared_position": position,
                    },
                }
            )
            fragment_offset += len(fragment_words)
        return exercises
