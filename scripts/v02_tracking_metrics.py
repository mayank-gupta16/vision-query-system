#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Minimal TrackEval adaptation and geometry tracker for issue #24.

Metric semantics are adapted from TrackEval commit
12c8791b303e0a0b50f753af204249e622d0281a. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

Box = tuple[int, int, int, int]
TrackedBox = tuple[str, Box]
FrameDetections = tuple[tuple[TrackedBox, ...], tuple[TrackedBox, ...]]
Association = Literal["greedy", "global"]
Motion = Literal["last", "velocity"]
MAX_FRAMES = 10_000
MAX_FRAME_OBJECTS = 64
MAX_IDENTITIES = 512
MAX_OBSERVATIONS = 100_000
MAX_SEQUENCES = 100


def _validate_frames(frames: Sequence[FrameDetections]) -> None:
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError("invalid_frames")
    identities: set[str] = set()
    observations = 0
    for ground_truth, predictions in frames:
        if len(ground_truth) > MAX_FRAME_OBJECTS or len(predictions) > MAX_FRAME_OBJECTS:
            raise ValueError("invalid_frames")
        for group in (ground_truth, predictions):
            identifiers = [identifier for identifier, _ in group]
            if any(
                type(identifier) is not str or not identifier or len(identifier) > 128
                for identifier in identifiers
            ) or len(identifiers) != len(set(identifiers)):
                raise ValueError("invalid_frames")
            identities.update(identifiers)
            observations += len(group)
            for _, box in group:
                if (
                    len(box) != 4
                    or any(type(coordinate) is not int for coordinate in box)
                    or not (0 <= box[0] < box[2] <= 1_000_000_000)
                    or not (0 <= box[1] < box[3] <= 1_000_000_000)
                ):
                    raise ValueError("invalid_frames")
    if len(identities) > MAX_IDENTITIES or observations > MAX_OBSERVATIONS:
        raise ValueError("invalid_frames")


def iou(first: Box, second: Box) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    union = first_area + second_area - intersection
    return 0.0 if union <= 0 else intersection / union


def iou_basis_points(first: Box, second: Box) -> int:
    return int(iou(first, second) * 10_000)


def hungarian_max(weights: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    """Return a deterministic maximum-weight rectangular assignment."""

    if not weights:
        return []
    column_count = len(weights[0])
    if column_count == 0:
        return []
    if any(len(row) != column_count for row in weights):
        raise ValueError("ragged_weights")
    row_count = len(weights)
    if row_count > MAX_IDENTITIES or column_count > MAX_IDENTITIES:
        raise ValueError("assignment_too_large")
    if any(
        not math.isfinite(float(value)) or float(value) < 0.0 for row in weights for value in row
    ):
        raise ValueError("invalid_weights")
    transposed = row_count > column_count
    matrix = (
        [
            [float(weights[row][column]) for row in range(row_count)]
            for column in range(column_count)
        ]
        if transposed
        else [[float(value) for value in row] for row in weights]
    )
    rows = len(matrix)
    columns = len(matrix[0])
    maximum = max(max(row) for row in matrix)
    costs = [[maximum - value for value in row] for row in matrix]
    u = [0.0] * (rows + 1)
    v = [0.0] * (columns + 1)
    p = [0] * (columns + 1)
    way = [0] * (columns + 1)
    for row_index in range(1, rows + 1):
        p[0] = row_index
        column_zero = 0
        minimums = [math.inf] * (columns + 1)
        used = [False] * (columns + 1)
        while True:
            used[column_zero] = True
            current_row = p[column_zero]
            delta = math.inf
            next_column = 0
            for column_index in range(1, columns + 1):
                if used[column_index]:
                    continue
                current = (
                    costs[current_row - 1][column_index - 1] - u[current_row] - v[column_index]
                )
                if current < minimums[column_index]:
                    minimums[column_index] = current
                    way[column_index] = column_zero
                if minimums[column_index] < delta:
                    delta = minimums[column_index]
                    next_column = column_index
            for column_index in range(columns + 1):
                if used[column_index]:
                    u[p[column_index]] += delta
                    v[column_index] -= delta
                else:
                    minimums[column_index] -= delta
            column_zero = next_column
            if p[column_zero] == 0:
                break
        while True:
            previous = way[column_zero]
            p[column_zero] = p[previous]
            column_zero = previous
            if column_zero == 0:
                break
    pairs = [(p[column] - 1, column - 1) for column in range(1, columns + 1) if p[column]]
    if transposed:
        pairs = [(column, row) for row, column in pairs]
    return sorted(pairs)


def _similarities(frame: FrameDetections) -> list[list[float]]:
    ground_truth, predictions = frame
    return [[iou(gt_box, pred_box) for _, pred_box in predictions] for _, gt_box in ground_truth]


def frame_matches(frame: FrameDetections, threshold: float = 0.5) -> list[tuple[str, str]]:
    _validate_frames((frame,))
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("invalid_threshold")
    ground_truth, predictions = frame
    similarity = _similarities(frame)
    return [
        (ground_truth[row][0], predictions[column][0])
        for row, column in hungarian_max(similarity)
        if similarity[row][column] >= threshold
    ]


def _hota_details_with_association_sums(
    frames: Sequence[FrameDetections],
) -> tuple[dict[str, object], list[float]]:
    _validate_frames(frames)
    gt_counts: dict[str, int] = {}
    prediction_counts: dict[str, int] = {}
    potential: dict[tuple[str, str], float] = {}
    similarities: list[list[list[float]]] = []
    for ground_truth, predictions in frames:
        similarity = _similarities((ground_truth, predictions))
        similarities.append(similarity)
        for gt_id, _ in ground_truth:
            gt_counts[gt_id] = gt_counts.get(gt_id, 0) + 1
        for prediction_id, _ in predictions:
            prediction_counts[prediction_id] = prediction_counts.get(prediction_id, 0) + 1
        row_sums = [sum(row) for row in similarity]
        column_sums = [
            sum(similarity[row][column] for row in range(len(ground_truth)))
            for column in range(len(predictions))
        ]
        for row, (gt_id, _) in enumerate(ground_truth):
            for column, (prediction_id, _) in enumerate(predictions):
                denominator = row_sums[row] + column_sums[column] - similarity[row][column]
                if denominator > 0.0:
                    key = (gt_id, prediction_id)
                    potential[key] = potential.get(key, 0.0) + similarity[row][column] / denominator
    if not gt_counts or not prediction_counts:
        return (
            {
                "association_accuracy_basis_points": [0] * 19,
                "detection_accuracy_basis_points": [0] * 19,
                "false_negatives": [sum(gt_counts.values())] * 19,
                "false_positives": [sum(prediction_counts.values())] * 19,
                "hota_alpha_basis_points": [0] * 19,
                "hota_basis_points": 0,
                "true_positives": [0] * 19,
            },
            [0.0] * 19,
        )
    alignment = {
        key: value / (gt_counts[key[0]] + prediction_counts[key[1]] - value)
        for key, value in potential.items()
    }
    hota_values: list[float] = []
    detection_values: list[float] = []
    association_values: list[float] = []
    true_positive_values: list[int] = []
    false_negative_values: list[int] = []
    false_positive_values: list[int] = []
    association_sums: list[float] = []
    for alpha_index in range(1, 20):
        alpha = alpha_index * 0.05
        true_positives = 0
        false_negatives = 0
        false_positives = 0
        match_counts: dict[tuple[str, str], int] = {}
        for frame_index, (ground_truth, predictions) in enumerate(frames):
            similarity = similarities[frame_index]
            if not ground_truth:
                false_positives += len(predictions)
                continue
            if not predictions:
                false_negatives += len(ground_truth)
                continue
            scores = [
                [
                    alignment.get((gt_id, prediction_id), 0.0) * similarity[row][column]
                    for column, (prediction_id, _) in enumerate(predictions)
                ]
                for row, (gt_id, _) in enumerate(ground_truth)
            ]
            pairs = [
                (row, column)
                for row, column in hungarian_max(scores)
                if similarity[row][column] >= alpha - math.ulp(1.0)
            ]
            true_positives += len(pairs)
            false_negatives += len(ground_truth) - len(pairs)
            false_positives += len(predictions) - len(pairs)
            for row, column in pairs:
                key = (ground_truth[row][0], predictions[column][0])
                match_counts[key] = match_counts.get(key, 0) + 1
        detection_denominator = true_positives + false_negatives + false_positives
        detection_accuracy = (
            true_positives / detection_denominator if detection_denominator else 0.0
        )
        association_sum = sum(
            matches * (matches / (gt_counts[gt_id] + prediction_counts[prediction_id] - matches))
            for (gt_id, prediction_id), matches in match_counts.items()
        )
        association_accuracy = association_sum / max(1, true_positives)
        association_sums.append(association_sum)
        detection_values.append(detection_accuracy)
        association_values.append(association_accuracy)
        true_positive_values.append(true_positives)
        false_negative_values.append(false_negatives)
        false_positive_values.append(false_positives)
        hota_values.append(math.sqrt(detection_accuracy * association_accuracy))
    return (
        {
            "association_accuracy_basis_points": [
                int(value * 10_000) for value in association_values
            ],
            "detection_accuracy_basis_points": [int(value * 10_000) for value in detection_values],
            "false_negatives": false_negative_values,
            "false_positives": false_positive_values,
            "hota_alpha_basis_points": [int(value * 10_000) for value in hota_values],
            "hota_basis_points": int(sum(hota_values) * 10_000 / len(hota_values)),
            "true_positives": true_positive_values,
        },
        association_sums,
    )


def hota_details(frames: Sequence[FrameDetections]) -> dict[str, object]:
    """Compute HOTA details over the official 0.05..0.95 alpha grid."""

    return _hota_details_with_association_sums(frames)[0]


def _validate_sequences(sequences: Sequence[Sequence[FrameDetections]]) -> None:
    if not 1 <= len(sequences) <= MAX_SEQUENCES:
        raise ValueError("invalid_sequences")
    if sum(len(frames) for frames in sequences) > MAX_FRAMES:
        raise ValueError("invalid_sequences")
    if (
        sum(
            len(ground_truth) + len(predictions)
            for frames in sequences
            for ground_truth, predictions in frames
        )
        > MAX_OBSERVATIONS
    ):
        raise ValueError("invalid_sequences")


def combined_hota_details(
    sequences: Sequence[Sequence[FrameDetections]],
) -> dict[str, object]:
    """Combine complete-sequence HOTA components using TrackEval semantics."""

    _validate_sequences(sequences)
    sequence_results = [_hota_details_with_association_sums(frames) for frames in sequences]
    true_positives = [
        sum(cast(list[int], details["true_positives"])[index] for details, _ in sequence_results)
        for index in range(19)
    ]
    false_negatives = [
        sum(cast(list[int], details["false_negatives"])[index] for details, _ in sequence_results)
        for index in range(19)
    ]
    false_positives = [
        sum(cast(list[int], details["false_positives"])[index] for details, _ in sequence_results)
        for index in range(19)
    ]
    association_sums = [sum(values[index] for _, values in sequence_results) for index in range(19)]
    detection_values = [
        true_positives[index]
        / max(1, true_positives[index] + false_negatives[index] + false_positives[index])
        for index in range(19)
    ]
    association_values = [
        association_sums[index] / max(1, true_positives[index]) for index in range(19)
    ]
    hota_values = [
        math.sqrt(detection_values[index] * association_values[index]) for index in range(19)
    ]
    return {
        "association_accuracy_basis_points": [int(value * 10_000) for value in association_values],
        "detection_accuracy_basis_points": [int(value * 10_000) for value in detection_values],
        "false_negatives": false_negatives,
        "false_positives": false_positives,
        "hota_alpha_basis_points": [int(value * 10_000) for value in hota_values],
        "hota_basis_points": int(sum(hota_values) * 10_000 / len(hota_values)),
        "true_positives": true_positives,
    }


def hota_basis_points(frames: Sequence[FrameDetections]) -> int:
    return cast(int, hota_details(frames)["hota_basis_points"])


def identity_details(frames: Sequence[FrameDetections]) -> dict[str, int]:
    """Compute TrackEval identity counts and F1 at IoU 0.50."""

    _validate_frames(frames)
    gt_counts: dict[str, int] = {}
    prediction_counts: dict[str, int] = {}
    potential: dict[tuple[str, str], int] = {}
    for ground_truth, predictions in frames:
        similarity = _similarities((ground_truth, predictions))
        for gt_id, _ in ground_truth:
            gt_counts[gt_id] = gt_counts.get(gt_id, 0) + 1
        for prediction_id, _ in predictions:
            prediction_counts[prediction_id] = prediction_counts.get(prediction_id, 0) + 1
        for row, (gt_id, _) in enumerate(ground_truth):
            for column, (prediction_id, _) in enumerate(predictions):
                if similarity[row][column] >= 0.5:
                    key = (gt_id, prediction_id)
                    potential[key] = potential.get(key, 0) + 1
    total_gt = sum(gt_counts.values())
    total_predictions = sum(prediction_counts.values())
    if not total_gt and not total_predictions:
        return {
            "identity_false_negatives": 0,
            "identity_false_positives": 0,
            "identity_true_positives": 0,
            "idf1_basis_points": 0,
        }
    gt_ids = sorted(gt_counts)
    prediction_ids = sorted(prediction_counts)
    matrix = [
        [float(potential.get((gt_id, prediction_id), 0)) for prediction_id in prediction_ids]
        for gt_id in gt_ids
    ]
    identity_true_positives = sum(int(matrix[row][column]) for row, column in hungarian_max(matrix))
    identity_false_negatives = total_gt - identity_true_positives
    identity_false_positives = total_predictions - identity_true_positives
    denominator = 2 * identity_true_positives + identity_false_negatives + identity_false_positives
    return {
        "identity_false_negatives": identity_false_negatives,
        "identity_false_positives": identity_false_positives,
        "identity_true_positives": identity_true_positives,
        "idf1_basis_points": (
            0 if denominator == 0 else 2 * identity_true_positives * 10_000 // denominator
        ),
    }


def combined_identity_details(
    sequences: Sequence[Sequence[FrameDetections]],
) -> dict[str, int]:
    """Combine complete-sequence identity counts using TrackEval semantics."""

    _validate_sequences(sequences)
    details = [identity_details(frames) for frames in sequences]
    identity_true_positives = sum(item["identity_true_positives"] for item in details)
    identity_false_negatives = sum(item["identity_false_negatives"] for item in details)
    identity_false_positives = sum(item["identity_false_positives"] for item in details)
    denominator = 2 * identity_true_positives + identity_false_negatives + identity_false_positives
    return {
        "identity_false_negatives": identity_false_negatives,
        "identity_false_positives": identity_false_positives,
        "identity_true_positives": identity_true_positives,
        "idf1_basis_points": (
            0 if denominator == 0 else 2 * identity_true_positives * 10_000 // denominator
        ),
    }


def idf1_basis_points(frames: Sequence[FrameDetections]) -> int:
    return identity_details(frames)["idf1_basis_points"]


def identity_diagnostics(frames: Sequence[FrameDetections]) -> tuple[int, int, int]:
    """Return ID switches, matched-gap fragmentations, and GT track frames."""

    _validate_frames(frames)
    last_prediction: dict[str, str] = {}
    gap_after_match: dict[str, bool] = {}
    switches = 0
    fragmentations = 0
    track_frames = 0
    for frame in frames:
        ground_truth, _ = frame
        track_frames += len(ground_truth)
        matches = dict(frame_matches(frame))
        for gt_id, _ in ground_truth:
            prediction_id = matches.get(gt_id)
            if prediction_id is None:
                if gt_id in last_prediction:
                    gap_after_match[gt_id] = True
                continue
            previous = last_prediction.get(gt_id)
            if previous is not None and previous != prediction_id:
                switches += 1
            if gap_after_match.get(gt_id, False):
                fragmentations += 1
            last_prediction[gt_id] = prediction_id
            gap_after_match[gt_id] = False
    return switches, fragmentations, track_frames


def false_cut_continuations(
    frames: Sequence[FrameDetections], cut_frame_index: int, window_frames: int = 4
) -> int:
    """Count prediction IDs emitted on both sides of a cut."""

    _validate_frames(frames)
    before: set[str] = set()
    after: set[str] = set()
    start = max(0, cut_frame_index - window_frames)
    end = min(len(frames), cut_frame_index + window_frames)
    for frame_index in range(start, end):
        target = before if frame_index < cut_frame_index else after
        target.update(prediction_id for prediction_id, _ in frames[frame_index][1])
    return len(before & after)


@dataclass
class _TrackState:
    track_id: str
    last_box: Box
    last_pts_ms: int
    previous_box: Box | None = None
    previous_pts_ms: int | None = None
    missed_samples: int = 0


class GeometryTracker:
    """A clip-local deterministic tracker with explicit termination semantics."""

    def __init__(
        self,
        *,
        association: Association,
        motion: Motion,
        iou_threshold_basis_points: int,
        max_missed_samples: int,
        width_milli: int,
        height_milli: int,
    ) -> None:
        if association not in {"greedy", "global"} or motion not in {"last", "velocity"}:
            raise ValueError("invalid_tracker")
        if not 0 <= iou_threshold_basis_points <= 10_000 or not 0 <= max_missed_samples <= 64:
            raise ValueError("invalid_tracker")
        self._association = association
        self._motion = motion
        self._threshold = iou_threshold_basis_points
        self._max_missed_samples = max_missed_samples
        self._width = width_milli
        self._height = height_milli
        self._states: dict[str, _TrackState] = {}
        self._next_id = 1
        self._maximum_active_tracks = 0
        self._termination_counts = {"cut": 0, "miss_timeout": 0, "source_end": 0}

    @property
    def maximum_active_tracks(self) -> int:
        return self._maximum_active_tracks

    @property
    def termination_counts(self) -> dict[str, int]:
        return dict(self._termination_counts)

    def reset(self) -> None:
        self._termination_counts["cut"] += len(self._states)
        self._states.clear()

    def finish(self) -> None:
        self._termination_counts["source_end"] += len(self._states)
        self._states.clear()

    def _prediction(self, state: _TrackState, pts_ms: int) -> Box:
        if (
            self._motion == "last"
            or state.previous_box is None
            or state.previous_pts_ms is None
            or state.last_pts_ms <= state.previous_pts_ms
        ):
            return state.last_box
        elapsed = pts_ms - state.last_pts_ms
        history = state.last_pts_ms - state.previous_pts_ms
        last_center_x = state.last_box[0] + state.last_box[2]
        last_center_y = state.last_box[1] + state.last_box[3]
        previous_center_x = state.previous_box[0] + state.previous_box[2]
        previous_center_y = state.previous_box[1] + state.previous_box[3]
        delta_x = (last_center_x - previous_center_x) * elapsed // history
        delta_y = (last_center_y - previous_center_y) * elapsed // history
        shift_x = delta_x // 2
        shift_y = delta_y // 2
        left = max(
            0,
            min(self._width - (state.last_box[2] - state.last_box[0]), state.last_box[0] + shift_x),
        )
        top = max(
            0,
            min(
                self._height - (state.last_box[3] - state.last_box[1]), state.last_box[1] + shift_y
            ),
        )
        return (
            left,
            top,
            left + state.last_box[2] - state.last_box[0],
            top + state.last_box[3] - state.last_box[1],
        )

    def _pairs(
        self, predictions: list[tuple[str, Box]], detections: Sequence[Box]
    ) -> list[tuple[int, int]]:
        scores = [
            [iou_basis_points(box, detection) for detection in detections] for _, box in predictions
        ]
        if self._association == "global":
            return [
                (row, column)
                for row, column in hungarian_max(scores)
                if scores[row][column] >= self._threshold
            ]
        ranked = sorted(
            (
                (-score, predictions[row][0], detections[column], row, column)
                for row, values in enumerate(scores)
                for column, score in enumerate(values)
                if score >= self._threshold
            )
        )
        used_rows: set[int] = set()
        used_columns: set[int] = set()
        pairs: list[tuple[int, int]] = []
        for _, _, _, row, column in ranked:
            if row not in used_rows and column not in used_columns:
                used_rows.add(row)
                used_columns.add(column)
                pairs.append((row, column))
        return pairs

    def update(self, detections: Sequence[Box], pts_ms: int) -> tuple[TrackedBox, ...]:
        predicted = [
            (track_id, self._prediction(state, pts_ms))
            for track_id, state in sorted(self._states.items())
        ]
        pairs = self._pairs(predicted, detections) if predicted and detections else []
        outputs: list[TrackedBox] = []
        used_detections: set[int] = set()
        matched_tracks: set[str] = set()
        for row, column in pairs:
            track_id = predicted[row][0]
            state = self._states[track_id]
            box = detections[column]
            state.previous_box = state.last_box
            state.previous_pts_ms = state.last_pts_ms
            state.last_box = box
            state.last_pts_ms = pts_ms
            state.missed_samples = 0
            used_detections.add(column)
            matched_tracks.add(track_id)
            outputs.append((track_id, box))
        expired: list[str] = []
        for track_id, state in self._states.items():
            if track_id not in matched_tracks:
                state.missed_samples += 1
                if state.missed_samples > self._max_missed_samples:
                    expired.append(track_id)
        for track_id in expired:
            del self._states[track_id]
            self._termination_counts["miss_timeout"] += 1
        for column, box in enumerate(detections):
            if column in used_detections:
                continue
            track_id = f"t{self._next_id:04d}"
            self._next_id += 1
            self._states[track_id] = _TrackState(track_id, box, pts_ms)
            outputs.append((track_id, box))
        self._maximum_active_tracks = max(self._maximum_active_tracks, len(self._states))
        return tuple(sorted(outputs))
