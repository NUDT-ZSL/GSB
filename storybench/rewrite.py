"""Constraint-aware candidate generation and deterministic decoding."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any

from .model import MaskedDenoisingScorer
from .text import (
    WORD_PATTERN,
    detokenize,
    split_sentences,
    tokenize,
    word_count,
    word_token_indexes,
)


@dataclass(frozen=True)
class Change:
    change_type: str
    source: str
    original_word_ordinal: int
    rule_ref: str
    replacement: str | None = None
    pos: str | None = None
    rubric_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": self.change_type,
            "source": self.source,
            "original_word_ordinal": self.original_word_ordinal,
            "rule_ref": self.rule_ref,
            "rubric_refs": list(self.rubric_refs),
        }
        if self.replacement is not None:
            result["replacement"] = self.replacement
        if self.pos is not None:
            result["pos"] = self.pos
        return result


@dataclass(frozen=True)
class Action:
    kind: str
    ordinal: int
    source: str
    value: str | None
    rule_ref: str
    rubric_refs: tuple[str, ...]
    pos: str | None = None


@dataclass(frozen=True)
class State:
    tokens: tuple[str, ...]
    provenance: tuple[int | None, ...]
    changes: tuple[Change, ...]

    def fingerprint(self) -> str:
        raw = "\x1f".join(self.tokens)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Candidate:
    state: State
    text: str
    score: dict[str, Any]
    constraints: dict[str, Any]

    @property
    def candidate_id(self) -> str:
        payload = self.state.fingerprint() + "|" + "|".join(
            f"{change.change_type}:{change.source}:{change.replacement or ''}"
            for change in self.state.changes
        )
        return "cand-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _entity_surface_set(
    entity_names: list[str], aliases: dict[str, list[str]] | None
) -> set[str]:
    normalized_aliases = {
        key.lower(): [item.lower() for item in values]
        for key, values in (aliases or {}).items()
    }
    surfaces: set[str] = set()
    for entity in entity_names:
        surfaces.add(entity.lower())
        for alias in normalized_aliases.get(entity.lower(), []):
            surfaces.add(alias.lower())
    return surfaces


def _sentence_vocabulary(tokens: tuple[str, ...] | list[str]) -> list[str]:
    return [token.lower() for token in tokens if WORD_PATTERN.match(token)]


def validate_constraints(
    text: str,
    tokens: list[str] | tuple[str, ...],
    level: dict[str, Any],
    entity_names: list[str],
    entity_aliases: dict[str, list[str]] | None,
) -> dict[str, Any]:
    allowed = {word.lower() for word in level["allowed_words"]}
    limit = int(level["max_words"])
    vocabulary = _sentence_vocabulary(tokens)
    disallowed = sorted({word for word in vocabulary if word not in allowed})
    sentence_records = []
    length_ok = True
    for sentence_index, sentence in enumerate(split_sentences(text), start=1):
        count = word_count(sentence)
        passed = count <= limit
        length_ok = length_ok and passed
        sentence_records.append(
            {
                "sentence_index": sentence_index,
                "fragment": sentence,
                "word_count": count,
                "limit": limit,
                "passed": passed,
            }
        )
    present = set(vocabulary)
    surfaces = _entity_surface_set(entity_names, entity_aliases)
    missing_entities = []
    for entity in entity_names:
        entity_surfaces = {entity.lower()}
        entity_surfaces.update(
            alias.lower() for alias in (entity_aliases or {}).get(entity.lower(), [])
        )
        if not (entity_surfaces & present):
            missing_entities.append(entity)
    return {
        "vocabulary": {
            "passed": not disallowed,
            "disallowed_words": disallowed,
            "rule_ref": level["constraint_refs"]["vocabulary"],
        },
        "max_words": {
            "passed": length_ok,
            "limit": limit,
            "sentences": sentence_records,
            "rule_ref": level["constraint_refs"]["max_words"],
        },
        "entities": {
            "passed": not missing_entities,
            "missing_entities": missing_entities,
            "entity_surfaces": sorted(surfaces),
        },
        "passed": not disallowed and length_ok and not missing_entities,
    }


class ConstrainedRewriter:
    MAX_DEPTH = 4
    MAX_FRONTIER = 360

    def __init__(
        self,
        level: dict[str, Any],
        lexicon: dict[str, dict[str, str]],
        scorer: MaskedDenoisingScorer,
        entity_aliases: dict[str, list[str]] | None = None,
    ):
        self.level = level
        self.level_id = level["level_id"]
        self.lexicon = lexicon
        self.scorer = scorer
        self.entity_aliases = {
            key.lower(): [item.lower() for item in values]
            for key, values in (entity_aliases or {}).items()
        }
        self.allowed = {word.lower() for word in level["allowed_words"]}
        self.synonyms_by_source: dict[str, list[dict[str, Any]]] = {}
        for entry in level["synonyms"]:
            self.synonyms_by_source.setdefault(entry["from"].lower(), []).append(entry)
        self.split_conjunctions = set(level["split_policy"]["conjunctions"])
        self.min_split_words = int(level["split_policy"]["min_words_per_part"])
        self.require_right_clause_subject = bool(
            level["split_policy"].get("require_right_clause_subject", True)
        )
        self.deletable_pos = set(level["modifier_policy"]["deletable_pos"])
        self.modifier_rule_ref = level["modifier_policy"]["rule_ref"]
        self.split_rule_ref = level["split_policy"]["rule_ref"]

    def _initial_state(self, text: str) -> State:
        tokens = tuple(tokenize(text))
        provenance_list: list[int | None] = []
        word_ordinal = 0
        for token in tokens:
            if WORD_PATTERN.match(token):
                word_ordinal += 1
                provenance_list.append(word_ordinal)
            else:
                provenance_list.append(None)
        return State(tokens=tokens, provenance=tuple(provenance_list), changes=())

    @staticmethod
    def _word_tokens(tokens: tuple[str, ...]) -> list[tuple[int, str]]:
        return [
            (index, token)
            for index, token in enumerate(tokens)
            if WORD_PATTERN.match(token)
        ]

    def _entity_surfaces_for(self, entity_names: list[str]) -> set[str]:
        surfaces: set[str] = set()
        for entity in entity_names:
            surfaces.add(entity.lower())
            surfaces.update(
                alias.lower() for alias in self.entity_aliases.get(entity.lower(), [])
            )
        return surfaces

    def _can_delete_modifier(
        self,
        state: State,
        word_index: int,
        word_ordinal: int,
        entity_surfaces: set[str],
    ) -> bool:
        token = state.tokens[word_index]
        normalized = token.lower()
        if normalized in entity_surfaces:
            return False
        info = self.lexicon.get(normalized)
        if not info or info.get("pos") not in self.deletable_pos:
            return False
        ordinal_indexes = word_token_indexes(list(state.tokens))
        if word_ordinal >= len(ordinal_indexes):
            return False
        index = ordinal_indexes[word_ordinal]
        previous = state.tokens[index - 1].lower() if index > 0 else ""
        following = state.tokens[index + 1].lower() if index + 1 < len(state.tokens) else ""
        following_info = self.lexicon.get(following, {})
        if info["pos"] == "ADJ":
            if not (
                following_info.get("pos") in {"ADJ", "NOUN", "PROPN"}
                or following in entity_surfaces
            ):
                return False
            if previous in {"was", "were", "is", "are", "be", "been", "am"}:
                return False
            return True
        if info["pos"] == "ADV":
            previous_info = self.lexicon.get(previous, {})
            return previous_info.get("pos", "").startswith("VERB_")
        return False

    def _actions(self, state: State, entity_names: list[str]) -> list[Action]:
        actions: list[Action] = []
        entity_surfaces = self._entity_surfaces_for(entity_names)
        seen: set[tuple[Any, ...]] = set()
        changed_original_ordinals = {
            change.original_word_ordinal for change in state.changes
        }
        for word_ordinal, (word_index, token) in enumerate(
            self._word_tokens(state.tokens)
        ):
            normalized = token.lower()
            info = self.lexicon.get(normalized, {})
            original_ordinal = state.provenance[word_index]
            word_already_changed = original_ordinal in changed_original_ordinals
            if not word_already_changed:
                for entry in self.synonyms_by_source.get(normalized, []):
                    for option in entry["options"]:
                        target = option["to"]
                        if target not in self.allowed:
                            continue
                        if normalized in entity_surfaces and target not in entity_surfaces:
                            continue
                        action = Action(
                            kind="replace",
                            ordinal=word_ordinal,
                            source=normalized,
                            value=target,
                            rule_ref=option["entry_ref"],
                            rubric_refs=(option["entry_ref"], option["target_ref"]),
                            pos=info.get("pos"),
                        )
                        key = (action.kind, action.ordinal, action.value, action.rule_ref)
                        if key not in seen:
                            seen.add(key)
                            actions.append(action)
                if self._can_delete_modifier(
                    state, word_index, word_ordinal, entity_surfaces
                ):
                    action = Action(
                        kind="delete",
                        ordinal=word_ordinal,
                        source=normalized,
                        value=None,
                        rule_ref=self.modifier_rule_ref,
                        rubric_refs=(self.modifier_rule_ref,),
                        pos=info.get("pos"),
                    )
                    key = (action.kind, action.ordinal, action.rule_ref)
                    if key not in seen:
                        seen.add(key)
                        actions.append(action)
            if normalized in self.split_conjunctions:
                action = Action(
                    kind="split",
                    ordinal=word_ordinal,
                    source=normalized,
                    value=None,
                    rule_ref=self.split_rule_ref,
                    rubric_refs=(self.split_rule_ref,),
                    pos=info.get("pos"),
                )
                key = (action.kind, action.ordinal, action.rule_ref)
                if key not in seen:
                    seen.add(key)
                    actions.append(action)
        actions.sort(key=lambda item: (item.kind, item.ordinal, item.value or "", item.rule_ref))
        return actions

    @staticmethod
    def _replace_at_ordinal(state: State, ordinal: int, replacement: str) -> State:
        indexes = word_token_indexes(list(state.tokens))
        index = indexes[ordinal]
        tokens = list(state.tokens)
        tokens[index] = replacement
        return State(tuple(tokens), state.provenance, state.changes)

    @staticmethod
    def _delete_at_ordinal(state: State, ordinal: int) -> State:
        indexes = word_token_indexes(list(state.tokens))
        index = indexes[ordinal]
        tokens = state.tokens[:index] + state.tokens[index + 1 :]
        provenance = state.provenance[:index] + state.provenance[index + 1 :]
        return State(tokens, provenance, state.changes)

    def _split_at_ordinal(self, state: State, ordinal: int) -> State | None:
        indexes = word_token_indexes(list(state.tokens))
        index = indexes[ordinal]
        left = list(state.tokens[:index])
        right = list(state.tokens[index + 1 :])
        left_provenance = list(state.provenance[:index])
        right_provenance = list(state.provenance[index + 1 :])
        if word_count(detokenize(left)) < self.min_split_words:
            return None
        if word_count(detokenize(right)) < self.min_split_words:
            return None
        if self.require_right_clause_subject and right:
            first_word = right[0].lower()
            first_info = self.lexicon.get(first_word, {})
            first_pos = first_info.get("pos", "")
            if first_pos.startswith("VERB_") or first_pos in {
                "AUX",
                "ADV",
                "PREP",
                "CONJ",
                "NEG",
            }:
                return None
        while left and left[-1] in {",", ";", ":"}:
            left.pop()
            left_provenance.pop()
        if not left or left[-1] not in {".", "!", "?"}:
            left.append(".")
            left_provenance.append(None)
        tokens = tuple(left + right)
        provenance = tuple(left_provenance + right_provenance)
        return State(tokens, provenance, state.changes)

    def _apply_action(
        self, state: State, action: Action, entity_names: list[str]
    ) -> State | None:
        indexes = word_token_indexes(list(state.tokens))
        if action.ordinal < 0 or action.ordinal >= len(indexes):
            return None
        token_index = indexes[action.ordinal]
        original_ordinal = state.provenance[token_index]
        if original_ordinal is None:
            return None
        if action.kind == "replace" and action.value is not None:
            change = Change(
                change_type="replace",
                source=action.source,
                replacement=action.value,
                original_word_ordinal=original_ordinal,
                rule_ref=action.rule_ref,
                pos=action.pos,
                rubric_refs=action.rubric_refs,
            )
            updated = self._replace_at_ordinal(state, action.ordinal, action.value)
            return replace(updated, changes=state.changes + (change,))
        if action.kind == "delete":
            change = Change(
                change_type="delete",
                source=action.source,
                original_word_ordinal=original_ordinal,
                rule_ref=action.rule_ref,
                pos=action.pos,
                rubric_refs=action.rubric_refs,
            )
            updated = self._delete_at_ordinal(state, action.ordinal)
            return replace(updated, changes=state.changes + (change,))
        if action.kind == "split":
            updated = self._split_at_ordinal(state, action.ordinal)
            if updated is None:
                return None
            change = Change(
                change_type="split",
                source=action.source,
                original_word_ordinal=original_ordinal,
                rule_ref=action.rule_ref,
                pos=action.pos,
                rubric_refs=action.rubric_refs,
            )
            return replace(updated, changes=state.changes + (change,))
        return None

    def _candidate(
        self,
        state: State,
        entity_names: list[str],
    ) -> Candidate | None:
        if not state.tokens or not any(WORD_PATTERN.match(token) for token in state.tokens):
            return None
        text = detokenize(list(state.tokens))
        constraints = validate_constraints(
            text,
            state.tokens,
            self.level,
            entity_names,
            self.entity_aliases,
        )
        if not constraints["passed"]:
            return None
        score = self.scorer.score_candidate(text)
        return Candidate(state=state, text=text, score=score, constraints=constraints)

    @staticmethod
    def _candidate_sort_key(candidate: Candidate) -> tuple[Any, ...]:
        return (
            -float(candidate.score["average_log_probability"]),
            len(candidate.state.changes),
            candidate.text,
            candidate.candidate_id,
        )

    def rewrite(
        self,
        text: str,
        sentence_id: str,
        entity_names: list[str],
    ) -> dict[str, Any]:
        initial = self._initial_state(text)
        frontier = [initial]
        seen = {initial.tokens}
        best: Candidate | None = None
        valid_candidates: list[Candidate] = []
        expanded_states = 0
        for _depth in range(1, self.MAX_DEPTH + 1):
            next_frontier: list[State] = []
            for state in sorted(frontier, key=lambda item: item.tokens):
                for action in self._actions(state, entity_names):
                    updated = self._apply_action(state, action, entity_names)
                    if updated is None or updated.tokens in seen:
                        continue
                    seen.add(updated.tokens)
                    next_frontier.append(updated)
                    expanded_states += 1
                    candidate = self._candidate(updated, entity_names)
                    if candidate is None:
                        continue
                    valid_candidates.append(candidate)
                    if best is None or self._candidate_sort_key(candidate) < self._candidate_sort_key(
                        best
                    ):
                        best = candidate
            if not next_frontier:
                break
            next_frontier.sort(
                key=lambda item: (
                    sum(
                        1
                        for word in _sentence_vocabulary(item.tokens)
                        if word not in self.allowed
                    ),
                    len(item.tokens),
                    item.tokens,
                )
            )
            frontier = next_frontier[: self.MAX_FRONTIER]

        if best is None:
            fallback_text = detokenize(list(initial.tokens))
            fallback_constraints = validate_constraints(
                fallback_text,
                initial.tokens,
                self.level,
                entity_names,
                self.entity_aliases,
            )
            failure_reasons = []
            if not fallback_constraints["vocabulary"]["passed"]:
                failure_reasons.append(
                    "no candidate satisfied vocabulary constraint; disallowed: "
                    + ", ".join(fallback_constraints["vocabulary"]["disallowed_words"])
                )
            if not fallback_constraints["max_words"]["passed"]:
                too_long = [
                    sentence
                    for sentence in fallback_constraints["max_words"]["sentences"]
                    if not sentence["passed"]
                ]
                failure_reasons.append(
                    "no candidate satisfied sentence-length constraint; original lengths: "
                    + ", ".join(
                        f"{item['word_count']}>{item['limit']}" for item in too_long
                    )
                )
            if not fallback_constraints["entities"]["passed"]:
                failure_reasons.append(
                    "no candidate preserved entities: "
                    + ", ".join(fallback_constraints["entities"]["missing_entities"])
                )
            if not failure_reasons:
                failure_reasons.append(
                    "no transformation candidate was available at this level"
                )
            original_constraints_satisfied = fallback_constraints["passed"]
            status = (
                "unchanged_original"
                if original_constraints_satisfied
                else "fallback_original"
            )
            if original_constraints_satisfied:
                failure_reasons = [
                    "original already satisfies constraints; no safe transformation "
                    "candidate was found"
                ]
            return {
                "sentence_id": sentence_id,
                "level_id": self.level_id,
                "status": status,
                "original": text,
                "rewritten": fallback_text,
                "changes": [],
                "selected_candidate_id": None,
                "decoding": {
                    "valid_candidate_count": 0,
                    "expanded_state_count": expanded_states,
                    "max_depth": self.MAX_DEPTH,
                    "fallback_reasons": failure_reasons,
                },
                "constraints": fallback_constraints,
                "masked_denoising": self.scorer.score_candidate(fallback_text),
                "candidate_pool": [],
            }

        valid_candidates.sort(key=self._candidate_sort_key)
        pool = []
        for candidate in valid_candidates[:8]:
            pool.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "text": candidate.text,
                    "changes": [change.as_dict() for change in candidate.state.changes],
                    "average_log_probability": candidate.score["average_log_probability"],
                    "average_probability": candidate.score["average_probability"],
                    "perplexity": candidate.score["perplexity"],
                }
            )
        changes = [change.as_dict() for change in best.state.changes]
        return {
            "sentence_id": sentence_id,
            "level_id": self.level_id,
            "status": "rewritten",
            "original": text,
            "rewritten": best.text,
            "changes": changes,
            "selected_candidate_id": best.candidate_id,
            "decoding": {
                "valid_candidate_count": len(valid_candidates),
                "expanded_state_count": expanded_states,
                "max_depth": self.MAX_DEPTH,
                "fallback_reasons": [],
            },
            "constraints": best.constraints,
            "masked_denoising": best.score,
            "candidate_pool": pool,
        }
