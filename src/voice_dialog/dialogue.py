"""Dialogue-script normalization and causal context resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ConfigError
from .util import canonicalize, load_json, stable_hash


def normalize_script(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("dialogue script root must be an object", code="E_SCRIPT_INVALID")
    raw_turns = raw.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ConfigError("dialogue script must contain a non-empty turns list", code="E_SCRIPT_INVALID")
    normalized_turns: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for index, item in enumerate(raw_turns):
        if not isinstance(item, dict):
            raise ConfigError(f"turn at index {index} is not an object", code="E_SCRIPT_INVALID")
        turn_id = str(item.get("turn_id", "")).strip()
        if not turn_id or turn_id in seen_ids:
            raise ConfigError(f"turn_id is missing or duplicated at index {index}", code="E_SCRIPT_INVALID")
        order = item.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            raise ConfigError(f"turn {turn_id} has a non-integer order", code="E_SCRIPT_INVALID")
        if order in seen_orders:
            raise ConfigError(f"turn order {order} is duplicated", code="E_SCRIPT_INVALID")
        speaker = str(item.get("speaker", "")).strip()
        audio = str(item.get("audio", "")).strip().replace("\\", "/")
        if not speaker or not audio:
            raise ConfigError(f"turn {turn_id} requires speaker and audio", code="E_SCRIPT_INVALID")
        normalized_turn = {
            "turn_id": turn_id,
            "order": order,
            "speaker": speaker,
            "audio": audio,
            "prev_turn": item.get("prev_turn") if "prev_turn" in item else None,
            "prev_turn_declared": "prev_turn" in item,
            "intent": str(item.get("intent", "unspecified")),
            "text": str(item.get("text", "")),
            "emotion_label": str(item.get("emotion_label", "")),
        }
        seen_ids.add(turn_id)
        seen_orders.add(order)
        normalized_turns.append(normalized_turn)
    normalized_turns.sort(key=lambda turn: (turn["order"], turn["turn_id"]))
    for index, turn in enumerate(normalized_turns):
        if not turn["prev_turn_declared"]:
            turn["prev_turn"] = normalized_turns[index - 1]["turn_id"] if index > 0 else None
    document = {
        "schema_version": 1,
        "dialogue_id": str(raw.get("dialogue_id", "dialogue")),
        "turns": normalized_turns,
    }
    document["script_fingerprint"] = stable_hash(document)
    return canonicalize(document)


def load_script(path: Path) -> dict[str, Any]:
    try:
        raw = load_json(path)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read dialogue script: {exc}", code="E_SCRIPT_INVALID") from exc
    return normalize_script(raw)


def predecessor_chain(
    turns: list[dict[str, Any]],
    current_id: str,
    *,
    window: int,
    forced_missing_for_turn: str = "",
    rule: str = "nearest_available",
) -> tuple[list[dict[str, Any]], str, bool]:
    """Return current+predecessors, fallback label, and whether a miss occurred."""
    by_id = {turn["turn_id"]: turn for turn in turns}
    order_index = {turn["turn_id"]: index for index, turn in enumerate(turns)}
    if current_id not in by_id:
        return [], "turn_not_found", True
    current = by_id[current_id]
    chain = [current]
    visited = {current_id}
    missing = False
    fallback_parts: list[str] = []
    if window <= 0:
        return chain, "none", False
    intended = current.get("prev_turn")
    if forced_missing_for_turn == current_id and intended:
        intended = None
        missing = True
        fallback_parts.append("injected_missing_predecessor")
    while len(chain) - 1 < window:
        predecessor = by_id.get(str(intended)) if intended else None
        if predecessor is not None and predecessor["turn_id"] not in visited:
            chain.append(predecessor)
            visited.add(predecessor["turn_id"])
            intended = predecessor.get("prev_turn")
            continue
        if intended is not None:
            missing = True
            fallback_parts.append(f"missing:{intended}")
        if rule == "zero_context":
            if intended is not None:
                fallback_parts.append("zero_context")
            break
        # nearest_available: walk backward by script order to the latest unused turn.
        fallback = None
        start = order_index[current_id] - 1
        for index in range(start, -1, -1):
            candidate = turns[index]
            if candidate["turn_id"] not in visited:
                fallback = candidate
                break
        if fallback is None:
            if intended is not None:
                fallback_parts.append("no_earlier_turn")
            break
        chain.append(fallback)
        visited.add(fallback["turn_id"])
        fallback_parts.append(f"nearest_available:{fallback['turn_id']}")
        intended = fallback.get("prev_turn")
    source = ";".join(fallback_parts) if fallback_parts else "none"
    return chain, source, missing


def resolve_contexts(
    script: dict[str, Any],
    *,
    window: int,
    forced_missing_targets: set[str] | list[str] | tuple[str, ...] = (),
    rule: str = "nearest_available",
) -> list[dict[str, Any]]:
    turns = script["turns"]
    forced = set(forced_missing_targets)
    contexts = []
    for turn in turns:
        chain, fallback, missing = predecessor_chain(
            turns,
            turn["turn_id"],
            window=window,
            forced_missing_for_turn=turn["turn_id"] if turn["turn_id"] in forced else "",
            rule=rule,
        )
        contexts.append(
            {
                "turn": turn,
                "chain": chain,
                "predecessor_ids": [item["turn_id"] for item in chain[1:]],
                "fallback_source": fallback,
                "missing_predecessor": missing,
            }
        )
    return contexts
