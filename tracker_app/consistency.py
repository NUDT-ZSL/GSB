from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .errors import TrackingError


ALLOWED_TRANSITIONS = {
    "new": {"new", "tracked", "lost", "terminated"},
    "tracked": {"tracked", "lost", "terminated"},
    "lost": {"lost", "tracked", "terminated"},
    "terminated": {"terminated"},
}


def validate_checkpoint_state_chain(
    *,
    seq_id: str,
    checkpoints: list[dict[str, Any]],
) -> None:
    previous_states: dict[int, str] = {}
    previous_birth: dict[int, int] = {}
    previous_termination: dict[int, int | None] = {}
    previous_next_id = 1
    for checkpoint in sorted(checkpoints, key=lambda item: int(item["frame_index"])):
        frame_index = int(checkpoint["frame_index"])
        tracks = {
            int(item["track_id"]): item
            for item in checkpoint["tracker"]["tracks"]
        }
        state_distribution = Counter(item["state"] for item in tracks.values())
        expected_distribution = checkpoint["state_distribution"]
        if any(
            int(expected_distribution.get(state, 0)) != count
            for state, count in {
                "new": state_distribution.get("new", 0),
                "tracked": state_distribution.get("tracked", 0),
                "lost": state_distribution.get("lost", 0),
                "terminated": state_distribution.get("terminated", 0),
            }.items()
        ):
            raise TrackingError(
                "E_INCONSISTENT",
                "state distribution does not match checkpoint track snapshot",
                seq_id=seq_id,
                frame_index=frame_index,
                category="state_distribution_mismatch",
            )
        next_id = int(checkpoint["tracker"]["next_track_id"])
        if next_id < previous_next_id:
            raise TrackingError(
                "E_INCONSISTENT",
                "next track id moved backwards",
                seq_id=seq_id,
                frame_index=frame_index,
                category="track_id_allocation_regression",
            )
        previous_next_id = next_id
        for track_id, track in tracks.items():
            state = str(track["state"])
            if state not in ALLOWED_TRANSITIONS:
                raise TrackingError(
                    "E_INCONSISTENT",
                    "unknown track state",
                    seq_id=seq_id,
                    frame_index=frame_index,
                    category="unknown_track_state",
                    context={"track_id": track_id, "state": state},
                )
            if track_id in previous_states:
                old_state = previous_states[track_id]
                if state not in ALLOWED_TRANSITIONS[old_state]:
                    raise TrackingError(
                        "E_INCONSISTENT",
                        "illegal track state transition",
                        seq_id=seq_id,
                        frame_index=frame_index,
                        category="illegal_state_transition",
                        context={
                            "track_id": track_id,
                            "from": old_state,
                            "to": state,
                        },
                    )
                if int(track["birth_frame"]) != previous_birth[track_id]:
                    raise TrackingError(
                        "E_INCONSISTENT",
                        "track birth frame changed across checkpoints",
                        seq_id=seq_id,
                        frame_index=frame_index,
                        category="birth_frame_mutation",
                        context={"track_id": track_id},
                    )
                if old_state == "terminated":
                    old_term = previous_termination.get(track_id)
                    if track.get("termination_frame") != old_term:
                        raise TrackingError(
                            "E_INCONSISTENT",
                            "terminated track metadata was mutated",
                            seq_id=seq_id,
                            frame_index=frame_index,
                            category="termination_metadata_mutation",
                            context={"track_id": track_id},
                        )
            elif int(track["birth_frame"]) != frame_index:
                raise TrackingError(
                    "E_INCONSISTENT",
                    "track appears after its birth checkpoint",
                    seq_id=seq_id,
                    frame_index=frame_index,
                    category="late_track_insertion",
                    context={"track_id": track_id},
                )
            previous_states[track_id] = state
            previous_birth[track_id] = int(track["birth_frame"])
            previous_termination[track_id] = track.get("termination_frame")

        assignment_track_ids = {
            int(row["pred_track_id"])
            for row in checkpoint["assignments"]
            if row.get("pred_track_id") is not None
        }
        for track_id in assignment_track_ids:
            prior_state = previous_states.get(track_id)
            if prior_state == "terminated":
                raise TrackingError(
                    "E_INCONSISTENT",
                    "terminated prediction track appears in a later match",
                    seq_id=seq_id,
                    frame_index=frame_index,
                    category="terminated_track_rematched",
                    context={"track_id": track_id},
                )
            track = tracks.get(track_id)
            if track is None:
                raise TrackingError(
                    "E_INCONSISTENT",
                    "assignment references a missing track",
                    seq_id=seq_id,
                    frame_index=frame_index,
                    category="missing_assigned_track",
                    context={"track_id": track_id},
                )


def validate_set_consistency(
    *,
    seq_id: str,
    revision: int,
    assignments: list[dict[str, Any]],
    completion_rows: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
) -> None:
    edge_counts: Counter[tuple[int, int]] = Counter()
    tracks_with_truth: set[int] = set()
    for row in assignments:
        if row.get("pred_track_id") is None:
            continue
        pred_id = int(row["pred_track_id"])
        truth_id = int(row["track_id_true"])
        edge_counts[(pred_id, truth_id)] += 1
        tracks_with_truth.add(pred_id)
    for row in completion_rows:
        tracks_with_truth.add(int(row["pred_track_id"]))

    all_track_ids: set[int] = set()
    for checkpoint in checkpoints:
        all_track_ids.update(
            int(track["track_id"]) for track in checkpoint["tracker"]["tracks"]
        )
    missing_truth = sorted(all_track_ids - tracks_with_truth)
    if missing_truth:
        raise TrackingError(
            "E_INCONSISTENT",
            "prediction track is not associated with any truth track",
            seq_id=seq_id,
            category="prediction_without_truth",
            context={"track_ids": missing_truth},
        )

    truths_by_pred: dict[int, set[int]] = defaultdict(set)
    preds_by_truth: dict[int, set[int]] = defaultdict(set)
    for pred_id, truth_id in edge_counts:
        truths_by_pred[pred_id].add(truth_id)
        preds_by_truth[truth_id].add(pred_id)
    many_to_many: list[dict[str, Any]] = []
    for pred_id, truth_ids in truths_by_pred.items():
        shared_truths = [
            truth_id
            for truth_id in truth_ids
            if len(preds_by_truth[truth_id]) > 1
        ]
        if len(truth_ids) > 1 and shared_truths:
            many_to_many.append(
                {
                    "pred_track_id": pred_id,
                    "truth_track_ids": sorted(truth_ids),
                    "shared_truths": sorted(shared_truths),
                    "preds_by_shared_truth": {
                        str(truth_id): sorted(preds_by_truth[truth_id])
                        for truth_id in shared_truths
                    },
                }
            )
    if many_to_many:
        raise TrackingError(
            "E_INCONSISTENT",
            "prediction/truth identity mapping contains a many-to-many conflict",
            seq_id=seq_id,
            category="many_to_many_identity_mapping",
            context={"conflicts": many_to_many},
        )

    seen_segment_ids: set[int] = set()
    for segment in segments:
        track_id = int(segment["track_id"])
        if track_id in seen_segment_ids:
            raise TrackingError(
                "E_INCONSISTENT",
                "duplicate segment for one prediction track",
                seq_id=seq_id,
                category="duplicate_segment",
                context={"track_id": track_id},
            )
        seen_segment_ids.add(track_id)
        if not segment.get("truth_track_ids"):
            raise TrackingError(
                "E_INCONSISTENT",
                "track segment has no associated truth track",
                seq_id=seq_id,
                category="segment_without_truth",
                context={"track_id": track_id},
            )
        if int(segment["revision"]) != revision:
            raise TrackingError(
                "E_INCONSISTENT",
                "track segment revision differs from run revision",
                seq_id=seq_id,
                category="segment_revision_mismatch",
                context={"track_id": track_id},
            )
