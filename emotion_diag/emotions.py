"""Deterministic acoustic-to-emotion state machine."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .config import EngineConfig


STATE_CODES = {"neutral": 0, "calm": 1, "happy": 2, "angry": 3, "sad": 4}
CODE_STATES = {value: key for key, value in STATE_CODES.items()}


def tie_break_key(item: Dict[str, Any]) -> tuple[float, int, int, int]:
    """Equal scores resolve by speaker asc, frame asc, then state code."""
    state = str(item.get("state", "neutral"))
    return (
        -float(item["score"]),
        int(item.get("speaker_id", 0)),
        int(item.get("frame_index", 0)),
        int(item.get("state_code", STATE_CODES[state])),
    )


def resolve_tie(candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return sorted(candidates, key=tie_break_key)[0]


def rank_transition_records(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank equal-strength transition evidence by speaker then frame."""
    return sorted(
        records,
        key=lambda record: (
            -float(record.get("transition_score", record.get("confidence", 0.0))),
            int(record.get("speaker_id", 0)),
            int(record.get("frame_index", 0)),
        ),
    )


def _speaker_bias(speaker_id: int) -> Dict[str, float]:
    if speaker_id == 2:
        return {"happy": 0.16, "angry": 0.05, "sad": -0.05}
    if speaker_id == 3:
        return {"sad": 0.16, "calm": 0.10, "happy": -0.06}
    if speaker_id == 1:
        return {"neutral": 0.12, "angry": -0.03}
    return {"calm": 0.18, "neutral": 0.08}


def score_frame_states(
    feature: Dict[str, Any],
    truth_row: Dict[str, Any],
    previous_state: str,
    config: EngineConfig,
) -> List[Dict[str, Any]]:
    states = [str(x) for x in config["states"]]
    scores = {state: 0.08 for state in states}
    scores["neutral"] += 0.10

    rms = float(feature.get("rms", 0.0))
    rate = max(-6.0, min(6.0, float(feature.get("energy_change_rate", 0.0))))
    zcr_gradient = float(feature.get("zcr_gradient", 0.0))
    zcr = float(feature.get("zcr", 0.0))
    speaker_id = int(truth_row.get("speaker_id", 0))

    if rms < 0.035:
        scores["calm"] += 0.42
        scores["sad"] += 0.24
        scores["neutral"] += 0.08
    elif rms < 0.095:
        scores["neutral"] += 0.34
        scores["calm"] += 0.22
        scores["happy"] += 0.10
    elif rms < 0.17:
        scores["happy"] += 0.28
        scores["neutral"] += 0.16
        scores["angry"] += 0.16
    else:
        scores["angry"] += 0.38
        scores["happy"] += 0.20

    if rate > 0.18:
        scores["happy"] += min(0.30, 0.12 + rate * 0.03)
        scores["angry"] += min(0.26, 0.08 + rate * 0.025)
    elif rate < -0.18:
        scores["sad"] += min(0.32, 0.12 + abs(rate) * 0.03)
        scores["calm"] += 0.12

    if zcr_gradient > 0.018:
        scores["angry"] += min(0.24, zcr_gradient * 5.0)
        scores["happy"] += 0.06
    elif zcr_gradient < -0.018:
        scores["calm"] += 0.16
        scores["sad"] += 0.10

    if zcr > 0.22:
        scores["angry"] += 0.12
    elif zcr < 0.08:
        scores["sad"] += 0.08
        scores["calm"] += 0.08

    if not bool(truth_row.get("is_speech", speaker_id > 0)):
        scores["calm"] += 0.22
        scores["sad"] += 0.06
        scores["angry"] -= 0.08

    for state, bias in _speaker_bias(speaker_id).items():
        if state in scores:
            scores[state] += bias

    if previous_state in scores:
        scores[previous_state] += 0.10

    candidates: List[Dict[str, Any]] = []
    for state in states:
        score = round(max(0.0, float(scores[state])), 9)
        candidates.append(
            {
                "state": state,
                "state_code": STATE_CODES[state],
                "score": score,
                "speaker_id": speaker_id,
                "frame_index": int(feature["frame_index"]),
            }
        )
    return candidates


def advance_states(
    features: Sequence[Dict[str, Any]],
    truth: Sequence[Dict[str, Any]],
    config: EngineConfig,
    *,
    initial_state: str = "neutral",
    start_index: int = 0,
    end_index: int | None = None,
) -> Dict[str, Any]:
    end_index = len(features) if end_index is None else int(end_index)
    current_state = initial_state
    records: List[Dict[str, Any]] = []

    for position in range(int(start_index), end_index):
        feature = features[position]
        truth_row = truth[position]
        frame_index = int(feature["frame_index"])
        candidates = score_frame_states(feature, truth_row, current_state, config)
        best = resolve_tie(candidates)
        top_score = float(best["score"])
        tied = [c for c in candidates if float(c["score"]) == top_score]
        total_score = sum(float(c["score"]) for c in candidates) or 1.0
        next_state = str(best["state"])
        previous_state = current_state
        record = {
            "frame_index": frame_index,
            "speaker_id": int(truth_row.get("speaker_id", 0)),
            "state": next_state,
            "state_code": STATE_CODES[next_state],
            "previous_state": previous_state,
            "previous_state_code": STATE_CODES[previous_state],
            "transition": f"{previous_state}->{next_state}",
            "changed": previous_state != next_state,
            "confidence": round(top_score / total_score, 9),
            "transition_score": top_score,
            "tie_count": len(tied),
            "tie_break": [
                "score_desc",
                "speaker_id_asc",
                "frame_index_asc",
                "state_code_asc",
            ],
            "basis": {
                "speaker_id": int(truth_row.get("speaker_id", 0)),
                "is_speech": bool(truth_row.get("is_speech", False)),
                "turn_label": truth_row.get("turn_label"),
                "rms": float(feature.get("rms", 0.0)),
                "energy_change_rate": float(feature.get("energy_change_rate", 0.0)),
                "zcr": float(feature.get("zcr", 0.0)),
                "zcr_gradient": float(feature.get("zcr_gradient", 0.0)),
                "f0_hz": float(feature.get("f0_hz", 0.0)),
                "candidate_scores": {
                    c["state"]: c["score"] for c in candidates
                },
            },
        }
        records.append(record)
        current_state = next_state

    return {
        "start_index": int(start_index),
        "end_index": end_index,
        "initial_state": initial_state,
        "final_state": current_state,
        "records": records,
    }


def batch_transition_variants(
    features: Sequence[Dict[str, Any]],
    truth: Sequence[Dict[str, Any]],
    config_payload: Dict[str, Any],
    start_index: int,
    end_index: int,
) -> Dict[str, Dict[str, Any]]:
    """Compute this batch's result from every possible incoming state."""
    config = EngineConfig(config_payload)
    variants: Dict[str, Dict[str, Any]] = {}
    ordered_states = sorted(STATE_CODES.items(), key=lambda item: item[1])
    for state, code in ordered_states:
        result = advance_states(
            features,
            truth,
            config,
            initial_state=state,
            start_index=start_index,
            end_index=end_index,
        )
        variants[str(code)] = {
            "final_state": result["final_state"],
            "final_state_code": STATE_CODES[result["final_state"]],
            "records": result["records"],
        }
    return variants
