"""Character-level hashed-feature BIESO Viterbi tagger."""

from __future__ import annotations

import math
from typing import Any, Iterable

from .errors import BusinessError, NonFiniteModelWeightError
from .injection import Injection
from .resources import EntityEntry, EngineConfig, ResourceBundle
from .utils import hash_bucket, quantize, stable_hash

LABEL_ORDER = ("B", "I", "E", "S", "O")
START_LABELS = ("B", "S", "O")
END_LABELS = ("E", "S", "O")
VALID_TRANSITIONS = {
    "B": ("I", "E"),
    "I": ("I", "E"),
    "E": ("B", "S", "O"),
    "S": ("B", "S", "O"),
    "O": ("B", "S", "O"),
}


def _entry_covering(entries: Iterable[EntityEntry], chars: list[str], position: int) -> list[EntityEntry]:
    text = "".join(chars)
    result = []
    for entry in entries:
        first_start = max(0, position - len(entry.term) + 1)
        for start in range(first_start, position + 1):
            if text.startswith(entry.term, start):
                result.append(entry)
                break
    return result


def _max_hit(entries: Iterable[EntityEntry], chars: list[str], position: int) -> tuple[int, bool, bool, str]:
    covering = _entry_covering(entries, chars, position)
    if not covering:
        return 0, False, False, "-"
    covering.sort(key=lambda item: (-len(item.term), item.priority, item.term, item.entity_type))
    entry = covering[0]
    text = "".join(chars)
    possible_starts = [index for index in range(max(0, position - len(entry.term) + 1), position + 1) if text.startswith(entry.term, index)]
    start = min(possible_starts) if possible_starts else position
    return len(entry.term), start == position, start + len(entry.term) - 1 == position, entry.entity_type


def position_feature_buckets(
    chars: list[str],
    position: int,
    model: dict[str, Any],
    entries: Iterable[EntityEntry],
) -> list[tuple[str, int]]:
    buckets = int(model.get("hash_buckets", 257))
    feature_dim = int(model.get("feature_dim", 8))
    if buckets <= 0 or feature_dim <= 0:
        raise ValueError("feature_dim and hash_buckets must be positive")
    current = chars[position]
    previous = chars[position - 1] if position > 0 else "<BOS>"
    following = chars[position + 1] if position + 1 < len(chars) else "<EOS>"
    hit_length, hit_start, hit_end, hit_type = _max_hit(entries, chars, position)
    char_hashes = model.get("char_embedding_hashes", {})
    if not isinstance(char_hashes, dict):
        char_hashes = {}
    current_bucket = char_hashes.get(current)
    if not isinstance(current_bucket, int):
        current_bucket = hash_bucket(f"char:{current}", buckets) % buckets
    base_features = [
        ("bias", hash_bucket("bias", buckets)),
        ("cur", int(current_bucket) % buckets),
        ("prev", hash_bucket(f"prev:{previous}", buckets)),
        ("next", hash_bucket(f"next:{following}", buckets)),
        ("hitlen", hash_bucket(f"hitlen:{hit_length}", buckets)),
        ("hitstart", hash_bucket(f"hitstart:{int(hit_start)}", buckets)),
        ("hitend", hash_bucket(f"hitend:{int(hit_end)}", buckets)),
        ("hittype", hash_bucket(f"hittype:{hit_type}", buckets)),
    ]
    if feature_dim <= len(base_features):
        return base_features[:feature_dim]
    features = list(base_features)
    extra_index = 0
    while len(features) < feature_dim:
        offset = extra_index % 3
        if offset == 0:
            feature = f"cur2:{current}"
        elif offset == 1:
            feature = f"prev2:{previous}:{current}"
        else:
            feature = f"next2:{current}:{following}"
        features.append((f"extra{extra_index}", hash_bucket(feature, buckets)))
        extra_index += 1
    return features


def _sparse_weights(model: dict[str, Any], label: str) -> dict[int, float]:
    raw = model.get("emission_weights", {})
    if not isinstance(raw, dict):
        raise ValueError("emission_weights must be an object")
    values = raw.get(label, {})
    if not isinstance(values, dict):
        raise ValueError(f"emission_weights.{label} must be an object")
    result: dict[int, float] = {}
    for key, value in values.items():
        result[int(key)] = float(value)
    return result


def _transition(model: dict[str, Any], previous: str, current: str) -> float:
    raw = model.get("transition_weights", {})
    if not isinstance(raw, dict):
        raise ValueError("transition_weights must be an object")
    row = raw.get(previous, {})
    if isinstance(row, dict):
        value = row.get(current, 0.0)
    else:
        value = 0.0
    return float(value)


def _start_score(model: dict[str, Any], label: str) -> float:
    raw = model.get("start_weights", {})
    return float(raw.get(label, 0.0)) if isinstance(raw, dict) else 0.0


def _end_score(model: dict[str, Any], label: str) -> float:
    raw = model.get("end_weights", {})
    return float(raw.get(label, 0.0)) if isinstance(raw, dict) else 0.0


def _iter_numeric_weights(model: dict[str, Any]) -> Iterable[float]:
    for label in LABEL_ORDER:
        for value in _sparse_weights(model, label).values():
            yield value
    transitions = model.get("transition_weights", {})
    if isinstance(transitions, dict):
        for row in transitions.values():
            if isinstance(row, dict):
                for value in row.values():
                    yield float(value)
    for key in ("start_weights", "end_weights"):
        values = model.get(key, {})
        if isinstance(values, dict):
            for value in values.values():
                yield float(value)


class BIESOTagger:
    def __init__(self, resources: ResourceBundle) -> None:
        self.resources = resources
        self.config = resources.config
        self.model = resources.sequence_model
        self.entries = resources.all_entity_entries()

    def _apply_nonfinite_injection(
        self,
        model: dict[str, Any],
        injections: tuple[Injection, ...],
    ) -> list[str]:
        injection_ids: list[str] = []
        for injection in injections:
            if injection.fault != "nonfinite_weight":
                continue
            injection_ids.append(injection.injection_id)
            label = str(injection.options.get("label", "B"))
            if label not in LABEL_ORDER:
                label = "B"
            emission = model.setdefault("emission_weights", {}).setdefault(label, {})
            bucket_raw = injection.options.get("bucket")
            if bucket_raw is None:
                bucket = next((int(key) for key in sorted(emission, key=lambda item: int(item))), 0)
            else:
                bucket = int(bucket_raw)
            replacement = str(injection.options.get("value", "nan")).lower()
            emission[str(bucket)] = math.inf if replacement in {"inf", "infinity", "+inf"} else math.nan
            injection_ids.append(injection.injection_id)
        return sorted(set(injection_ids))

    def tag(
        self,
        normalized: dict[str, Any],
        stage_injections: tuple[Injection, ...] = (),
    ) -> dict[str, Any]:
        model = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in self.model.items()
        }
        model["emission_weights"] = {
            label: dict(values)
            for label, values in self.model.get("emission_weights", {}).items()
            if isinstance(values, dict)
        }
        injection_ids = self._apply_nonfinite_injection(model, stage_injections)
        try:
            for value in _iter_numeric_weights(model):
                if not math.isfinite(value):
                    raise NonFiniteModelWeightError("sequence model contains NaN or infinite weight")
            decoded = self._viterbi(normalized["chars"], model)
        except NonFiniteModelWeightError as exc:
            raise BusinessError(
                "E_NONFINITE_WEIGHT",
                str(exc),
                stage="sequence",
                details={"injection_ids": injection_ids},
            ) from exc
        labels = decoded["labels"]
        fragments = self._fragments(labels, normalized["text"])
        return {
            "labels": labels,
            "path_score": quantize(decoded["path_score"], self.config.digits),
            "position_scores": decoded["position_scores"],
            "fragments": fragments,
            "emission_details": decoded["emission_details"],
            "injection_ids": injection_ids,
            "model_summary": {
                "feature_dim": int(model.get("feature_dim", 8)),
                "hash_buckets": int(model.get("hash_buckets", 257)),
                "labels": list(model.get("labels", LABEL_ORDER)),
            },
            "fingerprint": stable_hash(
                {
                    "labels": labels,
                    "path_score": decoded["path_score"],
                    "fragments": fragments,
                    "position_scores": decoded["position_scores"],
                }
            ),
        }

    def _viterbi(self, chars: list[str], model: dict[str, Any]) -> dict[str, Any]:
        if not chars:
            raise BusinessError("E_EMPTY_SENTENCE", "cannot tag an empty character sequence", stage="sequence")
        model_labels = tuple(model.get("labels", LABEL_ORDER))
        labels = tuple(label for label in LABEL_ORDER if label in model_labels)
        if set(labels) != set(LABEL_ORDER):
            labels = LABEL_ORDER
        emission_by_label = {label: _sparse_weights(model, label) for label in labels}
        feature_rows: list[list[tuple[str, int]]] = []
        emission_rows: list[dict[str, float]] = []
        digits = self.config.digits
        for position in range(len(chars)):
            features = position_feature_buckets(chars, position, model, self.entries)
            feature_rows.append(features)
            row: dict[str, float] = {}
            for label in labels:
                weights = emission_by_label[label]
                parts = [weights[bucket] for _, bucket in features if bucket in weights]
                row[label] = math.fsum(sorted(parts))
            emission_rows.append(row)

        scores: dict[str, float] = {}
        backpointers: list[dict[str, str | None]] = []
        first_back: dict[str, str | None] = {}
        first = emission_rows[0]
        for label in labels:
            if label not in START_LABELS:
                scores[label] = -math.inf
                first_back[label] = None
            else:
                scores[label] = _start_score(model, label) + first[label]
                first_back[label] = None
        backpointers.append(first_back)
        cumulative_rows: list[dict[str, float | None]] = []
        cumulative_rows.append({label: scores[label] for label in labels})
        for position in range(1, len(chars)):
            next_scores: dict[str, float] = {}
            back: dict[str, str | None] = {}
            for current in labels:
                best_score = -math.inf
                best_previous: str | None = None
                for previous in labels:
                    if current not in VALID_TRANSITIONS.get(previous, ()):
                        continue
                    candidate = scores[previous] + _transition(model, previous, current)
                    if candidate > best_score:
                        best_score = candidate
                        best_previous = previous
                if best_previous is None:
                    next_scores[current] = -math.inf
                    back[current] = None
                else:
                    next_scores[current] = best_score + emission_rows[position][current]
                    back[current] = best_previous
            scores = next_scores
            backpointers.append(back)
            cumulative_rows.append({label: scores[label] for label in labels})

        final_candidates = [
            (scores[label] + _end_score(model, label), label)
            for label in labels
            if label in END_LABELS and math.isfinite(scores[label])
        ]
        if not final_candidates:
            raise BusinessError("E_INTERNAL", "BIESO decoder has no valid final path", stage="sequence")
        best_final_score, final_label = max(
            final_candidates,
            key=lambda item: (item[0], -LABEL_ORDER.index(item[1])),
        )
        path = [final_label]
        for position in range(len(chars) - 1, 0, -1):
            previous = backpointers[position][path[-1]]
            if previous is None:
                raise BusinessError("E_INTERNAL", "BIESO backpointer is missing", stage="sequence")
            path.append(previous)
        path.reverse()
        position_scores = []
        for index, label in enumerate(path):
            position_scores.append(
                {
                    "position": index,
                    "char": chars[index],
                    "best_label": label,
                    "emission_scores": {
                        candidate: quantize(emission_rows[index][candidate], digits)
                        for candidate in labels
                    },
                    "best_cumulative_scores": {
                        candidate: (
                            quantize(cumulative_rows[index][candidate], digits)
                            if math.isfinite(cumulative_rows[index][candidate])
                            else None
                        )
                        for candidate in labels
                    },
                }
            )
        emission_details = [
            {
                "position": index,
                "char": chars[index],
                "features": [
                    {"name": name, "bucket": bucket}
                    for name, bucket in feature_rows[index]
                ],
            }
            for index in range(len(chars))
        ]
        return {
            "labels": path,
            "path_score": best_final_score,
            "position_scores": position_scores,
            "emission_details": emission_details,
        }

    def _fragments(self, labels: list[str], text: str) -> list[dict[str, Any]]:
        fragments: list[dict[str, Any]] = []
        start: int | None = None
        for index, label in enumerate(labels):
            if label == "B":
                if start is not None:
                    fragments.append(self._fragment(start, index, text))
                start = index
            elif label == "S":
                if start is not None:
                    fragments.append(self._fragment(start, index, text))
                    start = None
                fragments.append(self._fragment(index, index + 1, text))
            elif label == "E" and start is not None:
                fragments.append(self._fragment(start, index + 1, text))
                start = None
            elif label == "O" and start is not None:
                fragments.append(self._fragment(start, index, text))
                start = None
        if start is not None:
            fragments.append(self._fragment(start, len(labels), text))
        return sorted(fragments, key=lambda item: (item["start"], item["end"], item["type"], item["text"]))

    def _fragment(self, start: int, end: int, text: str) -> dict[str, Any]:
        fragment_text = text[start:end]
        exact = [
            entry
            for entry in self.entries
            if entry.term == fragment_text
        ]
        exact.sort(key=lambda item: (-len(item.term), item.priority, item.entity_type, item.term))
        entity_type = exact[0].entity_type if exact else "实体"
        weight = exact[0].weight if exact else 0.5
        return {
            "type": entity_type,
            "text": fragment_text,
            "start": start,
            "end": end,
            "length": end - start,
            "weight": weight,
            "source": "sequence",
            "label_span": "BIESO",
        }
