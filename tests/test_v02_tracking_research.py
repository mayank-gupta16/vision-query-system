# SPDX-License-Identifier: Apache-2.0
"""Contracts for the locked short-term tracking research harness."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate_v02_gates as evaluator  # noqa: E402
import prepare_v02_tracking_dataset as preparation  # noqa: E402
import run_v02_tracking_benchmark as benchmark  # noqa: E402
import v02_tracking_metrics as metrics  # noqa: E402

FIXTURE_ROOT = ROOT / "fixtures" / "v02-tracking-research"
B = (0, 0, 100, 100)


def _frame(
    ground_truth: tuple[metrics.TrackedBox, ...],
    predictions: tuple[metrics.TrackedBox, ...],
) -> metrics.FrameDetections:
    return ground_truth, predictions


def _json(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _passing_metrics() -> dict[str, dict[str, int]]:
    return {
        name: {
            stratum: (
                0
                if name
                in {
                    "failure_rate_basis_points",
                    "false_continuations_across_cuts",
                    "fragmentations_per_1000_track_frames",
                    "id_switches_per_1000_track_frames",
                }
                else 1
                if name == "peak_rss_bytes"
                else 10_000
                if name in {"hota_basis_points", "idf1_basis_points"}
                else 1_000
            )
            for stratum in strata
        }
        for name, strata in benchmark.METRIC_STRATA.items()
    }


def _valid_calibration_results() -> tuple[
    dict[str, object], dict[str, str], list[dict[str, object]]
]:
    trusted = {
        "dataset_validator_sha256": "1" * 64,
        "detection_dataset_helper_sha256": "2" * 64,
        "detection_worker_sha256": "3" * 64,
        "gate_evaluator_sha256": "4" * 64,
        "sampling_dataset_helper_sha256": "5" * 64,
        "sampling_harness_sha256": "6" * 64,
    }
    detection_hashes = {
        seed: hashlib.sha256(f"detection-{seed}".encode()).hexdigest() for seed in benchmark.SEEDS
    }
    detection_repetitions = [
        {
            "cpu_ns": 1,
            "detection_count": 1,
            "peak_rss_bytes": 1,
            "prediction_sha256": detection_hashes[seed],
            "process_wall_ns": 1,
            "seed": seed,
            "wall_ns": 2,
            "worker_environment": {
                "numpy_version": "1",
                "openvino_version": "1",
                "python_version": "1",
                "telemetry_version": "1",
            },
        }
        for seed in benchmark.SEEDS
    ]
    policy, _ = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
    )
    results: list[dict[str, object]] = []
    for name in ("global-last-iou", "global-velocity-iou"):
        for iou in (1000, 3000, 5000):
            for missed in (0, 2, 5):
                for cut in (1500, 2500, 3500):
                    configuration = benchmark._configuration({"name": name}, iou, missed, cut)
                    repetitions = [
                        {
                            "cpu_ns": 1,
                            "detection_prediction_sha256": detection_hashes[seed],
                            "integrated_wall_ns": 2,
                            "maximum_active_tracks": 1,
                            "metrics": _passing_metrics(),
                            "seed": seed,
                            "termination_counts": {
                                "cut": 0,
                                "miss_timeout": 0,
                                "source_end": 1,
                            },
                            "tracker_output_sha256": hashlib.sha256(
                                f"tracker-{name}-{iou}-{missed}-{cut}-{seed}".encode()
                            ).hexdigest(),
                            "tracker_wall_ns": 1,
                        }
                        for seed in benchmark.SEEDS
                    ]
                    aggregate = benchmark._aggregate(repetitions)
                    results.append(
                        {
                            "aggregate": aggregate,
                            "configuration": configuration,
                            "configuration_id": benchmark._configuration_id(configuration),
                            "passes_gates": benchmark._passes_gates(aggregate, policy),
                            "repetitions": repetitions,
                        }
                    )
    selected = [
        benchmark._configuration({"name": name}, 5000, 0, 1500)
        for name in ("global-last-iou", "global-velocity-iou")
    ]
    return (
        {
            "configurations": results,
            "detection_repetitions": detection_repetitions,
            "evaluated_on": "2026-09-08",
            "phase": "calibration",
            "provenance": {
                "annotation_lock_sha256": "a" * 64,
                "candidate_manifest_sha256": "b" * 64,
                "dataset_manifest_sha256": "c" * 64,
                "evaluation_harness_sha256": "d" * 64,
                "metric_implementation_sha256": "e" * 64,
                "policy_sha256": "f" * 64,
                "source_manifest_sha256": "0" * 64,
                "source_revision": "9" * 40,
                **trusted,
            },
            "schema": "visualworld.v02-tracking-calibration-results",
            "schema_version": 1,
            "selected_configurations": selected,
        },
        trusted,
        selected,
    )


def test_tracking_dataset_lock_binds_complete_source_separated_sequences() -> None:
    source_path = FIXTURE_ROOT / "source-manifest.json"
    annotations_path = FIXTURE_ROOT / "annotations.json"
    dataset_path = FIXTURE_ROOT / "dataset-manifest.json"
    annotations = _json(annotations_path)
    dataset = _json(dataset_path)
    assert (
        annotations["source_manifest_sha256"]
        == hashlib.sha256(source_path.read_bytes()).hexdigest()
    )
    assert (
        cast(dict[str, object], dataset["acquisition"])["sha256"]
        == annotations["source_manifest_sha256"]
    )
    policy, _ = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
    )
    assert evaluator.load_manifest(dataset_path, policy)[0] == dataset

    clips = cast(list[dict[str, object]], annotations["clips"])
    tracks = cast(list[dict[str, object]], annotations["tracks"])
    assert len(clips) == 15
    assert len(tracks) == 72
    assert sum(clip["split"] == "calibration" for clip in clips) == 5
    assert all(
        clip["frame_count"] == 60 and len(cast(list[object], clip["frames"])) == 60
        for clip in clips
    )
    assert all(
        cast(list[str], frame["strata"])
        == (
            ["overall"]
            if clip["primary_stratum"] == "stationary_camera"
            else ["overall", clip["primary_stratum"]]
        )
        for clip in clips
        for frame in cast(list[dict[str, object]], clip["frames"])
    )
    assert all(
        len(cast(list[object], frame["objects"])) == 6
        and all(
            cast(int, item["visibility_millionths"]) >= preparation.DENSE_MIN_VISIBILITY_MILLIONTHS
            for item in cast(list[dict[str, object]], frame["objects"])
        )
        for clip in clips
        if clip["primary_stratum"] == "dense_crossing"
        for frame in cast(list[dict[str, object]], clip["frames"])
    )
    source_splits = {
        split: {
            source_id
            for clip in clips
            if clip["split"] == split
            for source_id in cast(list[str], clip["source_ids"])
        }
        for split in ("calibration", "test")
    }
    assert source_splits["calibration"].isdisjoint(source_splits["test"])


def test_tracking_source_rights_privacy_and_upstream_hashes_are_locked() -> None:
    source = _json(FIXTURE_ROOT / "source-manifest.json")
    assert source["license_expression"] == "CC0-1.0"
    assert (
        source["source_detection_annotation_sha256"]
        == hashlib.sha256(
            (ROOT / "fixtures" / "v02-detection-research" / "annotations.json").read_bytes()
        ).hexdigest()
    )
    assert (
        source["source_detection_manifest_sha256"]
        == hashlib.sha256(
            (ROOT / "fixtures" / "v02-detection-research" / "source-manifest.json").read_bytes()
        ).hexdigest()
    )
    assert cast(dict[str, object], source["privacy"]) == {
        "contains_faces": False,
        "contains_personal_data": False,
        "contains_plates": False,
        "contains_real_people": False,
        "source_boundary": "only plate-sanitized issue-21 RGB derivatives are accepted",
    }
    detection_annotations = _json(ROOT / "fixtures" / "v02-detection-research" / "annotations.json")
    clips, sources = preparation._validate_source_manifest(source, detection_annotations)
    assert len(clips) == 15
    assert len(sources) == 10


def test_tracking_candidates_and_upstream_decisions_are_exact() -> None:
    path = FIXTURE_ROOT / "candidates.json"
    manifest = _json(path)
    families, iou, missed, cut = benchmark._validate_candidates(
        manifest, hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert [family["name"] for family in families] == [
        "global-last-iou",
        "global-velocity-iou",
    ]
    assert iou == [1000, 3000, 5000]
    assert missed == [0, 2, 5]
    assert cut == [1500, 2500, 3500]
    detector = cast(dict[str, object], manifest["detector"])
    sampling = cast(dict[str, object], manifest["sampling"])
    tracking_dataset = cast(dict[str, object], manifest["dataset"])
    assert tracking_dataset == {
        "annotation_sha256": hashlib.sha256(
            (FIXTURE_ROOT / "annotations.json").read_bytes()
        ).hexdigest(),
        "dataset_manifest_sha256": hashlib.sha256(
            (FIXTURE_ROOT / "dataset-manifest.json").read_bytes()
        ).hexdigest(),
        "source_manifest_sha256": hashlib.sha256(
            (FIXTURE_ROOT / "source-manifest.json").read_bytes()
        ).hexdigest(),
    }
    assert (
        detector["receipt_sha256"]
        == hashlib.sha256(
            (
                ROOT / "fixtures/v02-detection-research/results/vehicle-detection-0201-receipt.json"
            ).read_bytes()
        ).hexdigest()
    )
    assert (
        sampling["receipt_sha256"]
        == hashlib.sha256(
            (ROOT / "fixtures/v02-sampling-research/results/fixed-5-fps-receipt.json").read_bytes()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("calibration", "iou_threshold_grid_basis_points", [1000.0, 3000, 5000]),
        ("calibration", "max_missed_samples_grid", [False, 2, 5]),
        ("dataset", "annotation_sha256", "0" * 64),
        ("detector", "confidence_millionths", 950000.0),
        ("metric_reference", "revision", "main"),
        ("termination", "scope", "persistent identity"),
    ],
)
def test_tracking_candidate_mutations_fail_closed(section: str, field: str, value: object) -> None:
    path = FIXTURE_ROOT / "candidates.json"
    manifest = _json(path)
    cast(dict[str, object], manifest[section])[field] = value
    with pytest.raises(benchmark.TrackingBenchmarkError, match="invalid_candidate_manifest"):
        benchmark._validate_candidates(manifest, hashlib.sha256(path.read_bytes()).hexdigest())


def test_tracking_annotation_payloads_match_dataset_contract() -> None:
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    source_sha256 = hashlib.sha256((FIXTURE_ROOT / "source-manifest.json").read_bytes()).hexdigest()
    benchmark._validate_annotations(annotations, dataset, source_sha256)
    for split, expected_count in (("calibration", 24), ("test", 48)):
        _, tracks, payload, ids = benchmark._split_payload(annotations, split)
        contract = cast(dict[str, object], cast(dict[str, object], dataset["splits"])[split])
        assert len(tracks) == expected_count == contract["item_count"]
        assert hashlib.sha256(payload).hexdigest() == contract["annotation_sha256"]
        assert hashlib.sha256(ids).hexdigest() == contract["item_ids_sha256"]


def test_occlusion_stratum_isolates_locked_mask_gaps_in_disjoint_lanes() -> None:
    source = _json(FIXTURE_ROOT / "source-manifest.json")
    derivation = cast(dict[str, object], source["derivation"])
    assert derivation["occlusion_mask_color_rgb"] == [74, 78, 82]
    assert derivation["occlusion_partial_mask_geometry"] == (
        "full box height and horizontal half-open interval [x0+floor(width/3), x0+2*floor(width/3))"
    )
    assert derivation["occlusion_visibility_millionths"] == (
        "floor(unmasked box pixels times 1000000 divided by box pixels)"
    )
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    missing = {"a": {20}, "b": {20, 21, 22}, "c": {20, 21, 22, 23, 24}}
    partial = {"a": {19, 21}, "b": {19, 23}, "c": {19, 25}}
    for clip in cast(list[dict[str, object]], annotations["clips"]):
        if clip["primary_stratum"] != "occlusion":
            continue
        for frame_index, frame in enumerate(cast(list[dict[str, object]], clip["frames"])):
            objects = cast(list[dict[str, object]], frame["objects"])
            by_role = {cast(str, item["track_id"])[-1]: item for item in objects}
            for role in ("a", "b", "c"):
                assert (role not in by_role) is (frame_index in missing[role])
                if role in by_role:
                    box = cast(list[int], by_role[role]["box"])
                    width = box[2] - box[0]
                    partial_mask_width = 2 * (width // 3) - width // 3
                    expected_partial_visibility = (width - partial_mask_width) * 1_000_000 // width
                    assert by_role[role]["visibility_millionths"] == (
                        expected_partial_visibility if frame_index in partial[role] else 1_000_000
                    )
            boxes = [
                cast(tuple[int, int, int, int], tuple(cast(list[int], item["box"])))
                for item in objects
            ]
            assert all(
                metrics.iou(first, second) == 0.0
                for index, first in enumerate(boxes)
                for second in boxes[index + 1 :]
            )


def test_partial_occlusion_visibility_matches_rendered_mask_pixels() -> None:
    background = bytes([1]) * (preparation.WIDTH * preparation.HEIGHT * 3)
    chip = (170, 60, bytes([2]) * (170 * 60 * 3))
    pixels, objects = preparation._render_frame(
        backgrounds=(background, background, background),
        chips=(chip, chip, chip, chip),
        clip_id="oracle-occlusion",
        frame_index=19,
        scenario="occlusion",
        variant=0,
    )
    mask = bytes(preparation.OCCLUSION_MASK_RGB)
    for item in objects:
        box = cast(list[int], item["box"])
        width = box[2] - box[0]
        height = box[3] - box[1]
        masked = 0
        for y in range(box[1], box[3]):
            for x in range(box[0], box[2]):
                offset = (y * preparation.WIDTH + x) * 3
                masked += pixels[offset : offset + 3] == mask
        third = width // 3
        assert masked == (2 * third - third) * height
        assert item["visibility_millionths"] == (
            (width * height - masked) * 1_000_000 // (width * height)
        )


def test_tracking_dataset_lock_rejects_coherent_source_or_oracle_substitution() -> None:
    candidates = _json(FIXTURE_ROOT / "candidates.json")
    source = _json(FIXTURE_ROOT / "source-manifest.json")
    source_clip = cast(list[dict[str, object]], source["clips"])[0]
    source_clip["background_seed"] = (cast(int, source_clip["background_seed"]) + 1) % 256
    detection_annotations = _json(ROOT / "fixtures/v02-detection-research/annotations.json")
    preparation._validate_source_manifest(source, detection_annotations)
    substituted_source_sha256 = hashlib.sha256(benchmark._canonical(source)).hexdigest()
    source_annotations = _json(FIXTURE_ROOT / "annotations.json")
    source_dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    source_annotations["source_manifest_sha256"] = substituted_source_sha256
    cast(dict[str, object], source_dataset["acquisition"])["sha256"] = substituted_source_sha256
    benchmark._validate_annotations(source_annotations, source_dataset, substituted_source_sha256)
    with pytest.raises(benchmark.TrackingBenchmarkError, match="tracking_dataset_mismatch"):
        benchmark._verify_tracking_dataset_lock(
            candidates,
            source_sha256=substituted_source_sha256,
            annotations_sha256=hashlib.sha256(benchmark._canonical(source_annotations)).hexdigest(),
            dataset_sha256=hashlib.sha256(benchmark._canonical(source_dataset)).hexdigest(),
        )

    annotations = _json(FIXTURE_ROOT / "annotations.json")
    dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    clips = cast(list[dict[str, object]], annotations["clips"])
    test_clip = next(clip for clip in clips if clip["split"] == "test")
    frame = cast(list[dict[str, object]], test_clip["frames"])[0]
    item = cast(list[dict[str, object]], frame["objects"])[0]
    cast(list[int], item["box"])[0] += 1
    _, _, payload, _ = benchmark._split_payload(annotations, "test")
    cast(dict[str, dict[str, object]], dataset["splits"])["test"]["annotation_sha256"] = (
        hashlib.sha256(payload).hexdigest()
    )
    source_sha256 = hashlib.sha256((FIXTURE_ROOT / "source-manifest.json").read_bytes()).hexdigest()
    benchmark._validate_annotations(annotations, dataset, source_sha256)
    with pytest.raises(benchmark.TrackingBenchmarkError, match="tracking_dataset_mismatch"):
        benchmark._verify_tracking_dataset_lock(
            candidates,
            source_sha256=source_sha256,
            annotations_sha256=hashlib.sha256(benchmark._canonical(annotations)).hexdigest(),
            dataset_sha256=hashlib.sha256(benchmark._canonical(dataset)).hexdigest(),
        )


@pytest.mark.parametrize("value", [True, 1.0, -1, 641])
def test_tracking_annotation_boxes_require_bounded_exact_integers(value: object) -> None:
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    source_sha256 = hashlib.sha256((FIXTURE_ROOT / "source-manifest.json").read_bytes()).hexdigest()
    first_clip = cast(list[dict[str, object]], annotations["clips"])[0]
    first_frame = cast(list[dict[str, object]], first_clip["frames"])[0]
    first_object = cast(list[dict[str, object]], first_frame["objects"])[0]
    cast(list[object], first_object["box"])[0] = value
    with pytest.raises(benchmark.TrackingBenchmarkError, match="invalid_annotations"):
        benchmark._validate_annotations(annotations, dataset, source_sha256)


def test_cut_score_separates_locked_calibration_cuts_without_oracle_labels() -> None:
    dataset_root = ROOT / "artifacts" / "issue24" / "dataset-v18"
    if not dataset_root.exists():
        pytest.skip("ignored research clips are not present")
    annotations = _json(FIXTURE_ROOT / "annotations.json")
    maxima: dict[str, int] = {}
    detected: dict[str, list[int]] = {}
    for clip in cast(list[dict[str, object]], annotations["clips"]):
        if clip["split"] != "calibration":
            continue
        previous: bytes | None = None
        scores: list[int] = []
        for _, pixels in benchmark._read_clip(dataset_root, clip):
            scores.append(benchmark._cut_score_basis_points(previous, pixels))
            previous = pixels
        primary = cast(str, clip["primary_stratum"])
        maxima[primary] = max(scores)
        detected[primary] = [index for index, score in enumerate(scores) if score >= 1500]
    assert detected["cuts"] == [20, 40]
    assert all(not frames for name, frames in detected.items() if name != "cuts")
    assert maxima["cuts"] > max(value for name, value in maxima.items() if name != "cuts")


def test_tracker_boundary_accepts_only_opaque_sequences_detections_pts_and_cut_score() -> None:
    detection_payload: dict[str, object] = {
        "sequences": [
            {
                "sequence_id": "sequence-00",
                "frames": [
                    {
                        "cut_score_basis_points": 0,
                        "detections": [[0, 0, 100000, 100000]],
                        "pts_ms": 0,
                    }
                ],
            }
        ]
    }
    configuration = benchmark._configuration({"name": "global-last-iou"}, 1000, 2, 1500)
    result = benchmark._run_tracker(detection_payload, configuration)
    output = cast(list[dict[str, object]], result["sequences"])[0]
    assert set(output) == {"detected_cut_frames", "frames", "sequence_id"}
    assert "split" not in json.dumps(result)
    assert "strata" not in json.dumps(result)
    assert "ground_truth" not in json.dumps(result)


def test_representative_failures_are_bounded_and_pixel_free() -> None:
    clips: list[dict[str, object]] = [
        {
            "cut_before_frame_indices": [],
            "frames": [{"objects": [{"box": [0, 0, 1, 1], "track_id": "ground-truth"}]}],
        }
    ]
    tracker_result: dict[str, object] = {
        "sequences": [
            {
                "frames": [{"observations": [], "pts_ms": 0}],
                "sequence_id": "sequence-00",
            }
        ]
    }
    failures = benchmark._representative_failures(clips, tracker_result)
    assert failures["missed_visible"] == [
        {
            "frame_index": 0,
            "ground_truth_track_id": "ground-truth",
            "pts_ms": 0,
            "sequence_id": "sequence-00",
        }
    ]
    assert all(len(items) <= benchmark.MAX_FAILURE_EXAMPLES_PER_KIND for items in failures.values())
    assert "pixels" not in json.dumps(failures)


def test_perfect_and_empty_tracking_goldens() -> None:
    perfect = [
        _frame((("g", B),), (("p", B),)),
        _frame((("g", B),), (("p", B),)),
    ]
    assert metrics.hota_basis_points(perfect) == 10_000
    assert metrics.idf1_basis_points(perfect) == 10_000
    assert metrics.identity_diagnostics(perfect) == (0, 0, 2)

    empty = [_frame((("g", B),), ()), _frame((("g", B),), ())]
    assert metrics.hota_basis_points(empty) == 0
    assert metrics.idf1_basis_points(empty) == 0
    assert metrics.identity_diagnostics(empty) == (0, 0, 2)


def test_split_identity_and_gap_diagnostics_goldens() -> None:
    split = [
        _frame((("g", B),), (("p1", B),)),
        _frame((("g", B),), (("p1", B),)),
        _frame((("g", B),), (("p2", B),)),
        _frame((("g", B),), (("p2", B),)),
    ]
    assert metrics.hota_basis_points(split) == 7071
    assert metrics.idf1_basis_points(split) == 5000
    assert metrics.identity_diagnostics(split) == (1, 0, 4)

    gap = [
        _frame((("g", B),), (("p1", B),)),
        _frame((("g", B),), ()),
        _frame((("g", B),), (("p2", B),)),
    ]
    assert metrics.identity_diagnostics(gap) == (1, 1, 3)


def test_complete_sequence_metrics_combine_counts_not_final_scores() -> None:
    perfect = [
        _frame((("g", B),), (("p", B),)),
        _frame((("g", B),), (("p", B),)),
    ]
    missed = [_frame((("g", B),), ())]
    hota = metrics.combined_hota_details([perfect, missed])
    identity = metrics.combined_identity_details([perfect, missed])
    assert hota["hota_basis_points"] == 8164
    assert identity == {
        "identity_false_negatives": 1,
        "identity_false_positives": 0,
        "identity_true_positives": 2,
        "idf1_basis_points": 8000,
    }


def test_iou_and_hota_alpha_boundaries_are_inclusive() -> None:
    half = (0, 0, 50, 100)
    below_half = (0, 0, 49, 100)
    ninety_five = (0, 0, 95, 100)
    assert metrics.frame_matches(_frame((("g", B),), (("p", half),))) == [("g", "p")]
    assert metrics.frame_matches(_frame((("g", B),), (("p", below_half),))) == []
    detail = metrics.hota_details([_frame((("g", B),), (("p", ninety_five),))])
    assert cast(list[int], detail["true_positives"])[0] == 1
    assert cast(list[int], detail["true_positives"])[-1] == 1

    just_below_half = (0, 0, 500_000_224, 999_999_552)
    huge = (0, 0, 1_000_000_000, 1_000_000_000)
    assert 0.5 - 1e-12 < metrics.iou(huge, just_below_half) < 0.5 - 2e-16
    epsilon_detail = metrics.hota_details([_frame((("g", huge),), (("p", just_below_half),))])
    assert cast(list[int], epsilon_detail["true_positives"])[8] == 1
    assert cast(list[int], epsilon_detail["true_positives"])[9] == 0


def test_fragmentation_ignores_unscored_full_occlusion() -> None:
    frames = [
        _frame((("g", B),), (("p", B),)),
        _frame((), ()),
        _frame((("g", B),), (("p", B),)),
    ]
    assert metrics.identity_diagnostics(frames) == (0, 0, 2)


def test_cut_continuation_and_reset_goldens() -> None:
    continued = [
        _frame((("before", B),), (("p1", B),)),
        _frame((("after", B),), (("p1", B),)),
    ]
    reset = [
        _frame((("before", B),), (("p1", B),)),
        _frame((("after", B),), (("p2", B),)),
    ]
    assert metrics.false_cut_continuations(continued, 1, 1) == 1
    assert metrics.false_cut_continuations(reset, 1, 1) == 0


def test_hungarian_ties_are_deterministic() -> None:
    assert metrics.hungarian_max([[1.0, 1.0], [1.0, 1.0]]) == [(0, 0), (1, 1)]
    assert metrics.hungarian_max([[1.0], [2.0]]) == [(1, 0)]


@pytest.mark.parametrize(
    "frames",
    [
        [((("g", (0, 0, 100, True)),), ())],
        [((("g", B), ("g", B)), ())],
        [],
    ],
)
def test_metric_input_validation_fails_closed(frames: object) -> None:
    with pytest.raises(ValueError, match="invalid_frames"):
        metrics.hota_basis_points(cast(list[metrics.FrameDetections], frames))


def test_assignment_rejects_nonfinite_ragged_and_oversized_inputs() -> None:
    with pytest.raises(ValueError, match="invalid_weights"):
        metrics.hungarian_max([[float("nan")]])
    with pytest.raises(ValueError, match="ragged_weights"):
        metrics.hungarian_max([[1.0], [1.0, 2.0]])
    with pytest.raises(ValueError, match="assignment_too_large"):
        metrics.hungarian_max([[0.0]] * (metrics.MAX_IDENTITIES + 1))


def test_tracker_retains_only_within_missed_sample_budget_and_never_reuses_ids() -> None:
    tracker = metrics.GeometryTracker(
        association="global",
        motion="last",
        iou_threshold_basis_points=1000,
        max_missed_samples=2,
        width_milli=640_000,
        height_milli=360_000,
    )
    first = tracker.update((B,), 0)[0][0]
    assert tracker.update((), 200) == ()
    assert tracker.update((), 400) == ()
    assert tracker.update((B,), 600)[0][0] == first
    tracker.reset()
    assert tracker.update((B,), 800)[0][0] != first
    tracker.finish()
    assert tracker.termination_counts == {"cut": 1, "miss_timeout": 0, "source_end": 1}


def test_tracker_zero_miss_budget_terminates_before_reacquisition() -> None:
    tracker = metrics.GeometryTracker(
        association="global",
        motion="velocity",
        iou_threshold_basis_points=1000,
        max_missed_samples=0,
        width_milli=640_000,
        height_milli=360_000,
    )
    first = tracker.update((B,), 0)[0][0]
    tracker.update((), 200)
    assert tracker.update((B,), 400)[0][0] != first
    assert tracker.termination_counts["miss_timeout"] == 1


def test_tracking_artifact_reports_in_process_review_boundary() -> None:
    artifact = benchmark._tracking_artifact(
        benchmark._configuration({"name": "global-last-iou"}, 3000, 2, 1500),
        "a" * 40,
        "2026-09-08",
        "b" * 64,
    )
    assert artifact["isolation"] == "in-process-reviewed"
    assert artifact["review_status"] == "approved-for-evaluation"
    assert artifact["distribution_review_status"] == "not-approved"


def test_calibration_results_are_deeply_validated_and_winners_recomputed() -> None:
    calibration, trusted, expected_selected = _valid_calibration_results()
    policy, _ = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
    )
    selected = benchmark._validate_calibration_results(
        calibration,
        candidates_sha256="b" * 64,
        annotations_sha256="a" * 64,
        dataset_sha256="c" * 64,
        harness_sha256="d" * 64,
        metric_sha256="e" * 64,
        policy=policy,
        policy_sha256="f" * 64,
        source_manifest_sha256="0" * 64,
        source_revision="9" * 40,
        trusted_code_digests=trusted,
    )
    assert selected == expected_selected

    retained = cast(dict[str, object], json.loads(json.dumps(calibration)))
    detection_payload: dict[str, object] = {
        "sequences": [
            {
                "frames": [
                    {
                        "cut_score_basis_points": 0,
                        "detections": [
                            [
                                column * 40_000,
                                row * 40_000,
                                column * 40_000 + 20_000,
                                row * 40_000 + 20_000,
                            ]
                            for row in range(8)
                            for column in range(8)
                        ],
                        "pts_ms": 0,
                    },
                    {
                        "cut_score_basis_points": 0,
                        "detections": [
                            [
                                320_000 + column * 40_000,
                                row * 40_000,
                                340_000 + column * 40_000,
                                row * 40_000 + 20_000,
                            ]
                            for row in range(8)
                            for column in range(8)
                        ],
                        "pts_ms": 200,
                    },
                ],
                "sequence_id": "sequence-00",
            }
        ]
    }
    retained_configuration = benchmark._configuration({"name": "global-last-iou"}, 1000, 5, 1500)
    observed_maximum = benchmark._run_tracker(detection_payload, retained_configuration)[
        "maximum_active_tracks"
    ]
    assert observed_maximum == 128
    retained_result = cast(list[dict[str, object]], retained["configurations"])[6]
    assert retained_result["configuration"] == retained_configuration
    for repetition in cast(list[dict[str, object]], retained_result["repetitions"]):
        repetition["maximum_active_tracks"] = observed_maximum
    assert (
        benchmark._validate_calibration_results(
            retained,
            candidates_sha256="b" * 64,
            annotations_sha256="a" * 64,
            dataset_sha256="c" * 64,
            harness_sha256="d" * 64,
            metric_sha256="e" * 64,
            policy=policy,
            policy_sha256="f" * 64,
            source_manifest_sha256="0" * 64,
            source_revision="9" * 40,
            trusted_code_digests=trusted,
        )
        == expected_selected
    )

    mutated = cast(dict[str, object], json.loads(json.dumps(calibration)))
    result = cast(list[dict[str, object]], mutated["configurations"])[0]
    repetition = cast(list[dict[str, object]], result["repetitions"])[0]
    repetition["seed"] = float(benchmark.SEEDS[0])
    with pytest.raises(benchmark.TrackingBenchmarkError, match="invalid_calibration_results"):
        benchmark._validate_calibration_results(
            mutated,
            candidates_sha256="b" * 64,
            annotations_sha256="a" * 64,
            dataset_sha256="c" * 64,
            harness_sha256="d" * 64,
            metric_sha256="e" * 64,
            policy=policy,
            policy_sha256="f" * 64,
            source_manifest_sha256="0" * 64,
            source_revision="9" * 40,
            trusted_code_digests=trusted,
        )


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("motion", "velocity"),
        ("iou_threshold_basis_points", 5000.0),
        ("source_revision", "8" * 40),
        ("gate_evaluator_sha256", "7" * 64),
    ],
)
def test_calibration_selection_requires_exact_winners_and_provenance(
    mutation: str, value: object
) -> None:
    _, trusted, selected = _valid_calibration_results()
    selection: dict[str, object] = {
        "calibration_results_sha256": "a" * 64,
        "candidate_manifest_sha256": "b" * 64,
        "dataset_validator_sha256": trusted["dataset_validator_sha256"],
        "detection_dataset_helper_sha256": trusted["detection_dataset_helper_sha256"],
        "detection_worker_sha256": trusted["detection_worker_sha256"],
        "evaluation_harness_sha256": "d" * 64,
        "gate_evaluator_sha256": trusted["gate_evaluator_sha256"],
        "metric_implementation_sha256": "e" * 64,
        "sampling_dataset_helper_sha256": trusted["sampling_dataset_helper_sha256"],
        "sampling_harness_sha256": trusted["sampling_harness_sha256"],
        "schema": "visualworld.v02-tracking-calibration-selection",
        "schema_version": 1,
        "selected_configurations": cast(object, selected),
        "source_revision": "9" * 40,
    }
    if mutation in {"motion", "iou_threshold_basis_points"}:
        first = cast(list[dict[str, object]], selection["selected_configurations"])[0]
        first[mutation] = value
    else:
        selection[mutation] = value
    with pytest.raises(benchmark.TrackingBenchmarkError, match="invalid_calibration_selection"):
        benchmark._validate_selection(
            selection,
            selection_sha256="c" * 64,
            candidates_sha256="b" * 64,
            calibration_results_sha256="a" * 64,
            calibration_selected_configurations=[
                benchmark._configuration({"name": name}, 5000, 0, 1500)
                for name in ("global-last-iou", "global-velocity-iou")
            ],
            harness_sha256="d" * 64,
            metric_sha256="e" * 64,
            source_revision="9" * 40,
            trusted_code_digests=trusted,
        )
