"""Deterministic feature and rule based intent scoring."""

from __future__ import annotations

import math
from typing import Any

from .injection import Injection
from .resources import ConflictRule, EngineConfig, ResourceBundle
from .utils import quantize, stable_hash


def _occurrences(text: str, term: str) -> list[int]:
    if not term:
        return []
    starts: list[int] = []
    position = text.find(term)
    while position >= 0:
        starts.append(position)
        position = text.find(term, position + max(1, len(term)))
    return starts


class IntentScorer:
    def __init__(self, resources: ResourceBundle) -> None:
        self.resources = resources
        self.config = resources.config

    def _injected_conflicts(
        self, injections: tuple[Injection, ...]
    ) -> tuple[list[ConflictRule], list[str]]:
        rules = list(self.resources.conflicts)
        ids: list[str] = []
        for injection in injections:
            if injection.fault != "rule_conflict_pair":
                continue
            ids.append(injection.injection_id)
            trigger = str(injection.options.get("trigger", injection.options.get("when", "退款")))
            left = str(injection.options.get("left", injection.options.get("winner", "查询订单")))
            right = str(injection.options.get("right", injection.options.get("loser", "申请退款")))
            penalty = float(injection.options.get("penalty", 1.25))
            reason = f"injected_conflict_pair:{injection.injection_id}"
            rules.extend(
                [
                    ConflictRule(trigger=trigger, winner=left, loser=right, penalty=penalty, reason=reason, raw={}),
                    ConflictRule(trigger=trigger, winner=right, loser=left, penalty=penalty, reason=reason, raw={}),
                ]
            )
        rules.sort(key=lambda item: (item.trigger, item.winner, item.loser, item.reason))
        return rules, ids

    def score(
        self,
        normalized: dict[str, Any],
        stage_injections: tuple[Injection, ...] = (),
    ) -> dict[str, Any]:
        text = normalized["text"]
        digits = self.config.digits
        purpose = self.config.scoring
        length_bonus = float(purpose.get("length_bonus", 0.15))
        position_decay = float(purpose.get("position_decay", 0.02))
        intents = self.resources.intents
        names = sorted(intents)
        raw_scores = {name: [] for name in names}
        feature_hits: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
        for name in names:
            definition = intents[name]
            for feature in sorted(definition.features, key=lambda item: (item.term, item.weight)):
                starts = _occurrences(text, feature.term)
                for start in starts:
                    decay = 1.0 / (1.0 + position_decay * start)
                    contribution = feature.weight * (1.0 + length_bonus * len(feature.term)) * decay
                    raw_scores[name].append(contribution)
                    feature_hits[name].append(
                        {
                            "term": feature.term,
                            "start": start,
                            "length": len(feature.term),
                            "weight": quantize(feature.weight, digits),
                            "decay": quantize(decay, digits),
                            "contribution": quantize(contribution, digits),
                        }
                    )

        conflicts, conflict_injection_ids = self._injected_conflicts(stage_injections)
        conflict_details: list[dict[str, Any]] = []
        conflict_penalties = {name: [] for name in names}
        for rule in conflicts:
            if rule.trigger not in text:
                continue
            if rule.winner not in raw_scores or rule.loser not in raw_scores:
                continue
            penalty = rule.penalty
            conflict_penalties[rule.loser].append(penalty)
            conflict_details.append(
                {
                    "trigger": rule.trigger,
                    "winner": rule.winner,
                    "loser": rule.loser,
                    "penalty": quantize(penalty, digits),
                    "reason": rule.reason,
                }
            )

        raw_values: dict[str, float] = {}
        score_values: dict[str, float] = {}
        for name in names:
            positive = math.fsum(sorted(raw_scores[name]))
            penalty_total = math.fsum(sorted(conflict_penalties[name]))
            raw_values[name] = quantize(positive - penalty_total, digits)
            score_values[name] = raw_values[name]
        chosen, normalized_scores = self._normalize(score_values, digits)
        confidence = normalized_scores[chosen]
        threshold = quantize(self.config.confidence_threshold, digits)
        injection_ids = list(conflict_injection_ids)
        override = False
        for injection in stage_injections:
            if injection.fault != "confidence_threshold":
                continue
            injection_ids.append(injection.injection_id)
            target = injection.options.get("value", injection.options.get("threshold", threshold))
            confidence = quantize(float(target), digits)
            normalized_scores = dict(normalized_scores)
            normalized_scores[chosen] = confidence
            override = True
        details = {
            "raw_scores": raw_values,
            "normalized_scores": normalized_scores,
            "feature_hits": feature_hits,
            "conflict_penalties": conflict_details,
            "chosen_by": "rounded_score_then_lexicographic_name",
            "precision": digits,
            "threshold": threshold,
        }
        return {
            "intent": chosen,
            "confidence": confidence,
            "scores": score_values,
            "normalized_scores": normalized_scores,
            "threshold_met": bool(confidence >= threshold),
            "confidence_override": override,
            "details": details,
            "injection_ids": sorted(set(injection_ids)),
            "fingerprint": stable_hash(
                {
                    "intent": chosen,
                    "confidence": confidence,
                    "scores": score_values,
                    "normalized": normalized_scores,
                    "details": details,
                }
            ),
        }

    @staticmethod
    def _normalize(scores: dict[str, float], digits: int) -> tuple[str, dict[str, float]]:
        names = sorted(scores)
        if not names:
            raise ValueError("intent vocabulary is empty")
        chosen = min(names, key=lambda name: (-scores[name], name))
        maximum = max(scores.values())
        exponentials = {name: math.exp(scores[name] - maximum) for name in names}
        total = math.fsum(exponentials[name] for name in names)
        normalized = {name: quantize(exponentials[name] / total, digits) for name in names}
        return chosen, normalized

