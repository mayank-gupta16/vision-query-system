# SPDX-License-Identifier: Apache-2.0
"""Contracts for the locked detector/runtime research harness."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import cast

import evaluate_v02_gates as evaluator
import prepare_v02_detection_dataset as preparation
import pytest
import run_v02_detection_benchmark as benchmark

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "v02-detection-research"
RESULT_ROOT = FIXTURE_ROOT / "results"


def _json(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def test_dataset_manifest_binds_source_annotations_splits_and_gate_policy() -> None:
    source_path = FIXTURE_ROOT / "source-manifest.json"
    annotations_path = FIXTURE_ROOT / "annotations.json"
    dataset_path = FIXTURE_ROOT / "dataset-manifest.json"
    source = _json(source_path)
    annotations = _json(annotations_path)
    dataset = _json(dataset_path)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()

    assert annotations["source_manifest_sha256"] == source_sha256
    assert cast(dict[str, object], dataset["acquisition"])["sha256"] == source_sha256
    policy, _ = evaluator.load_policy(ROOT / "fixtures" / "v02-evaluation" / "policy.json")
    validated, digest = evaluator.load_manifest(dataset_path, policy)
    assert validated == dataset
    assert digest == hashlib.sha256(dataset_path.read_bytes()).hexdigest()

    source_records = cast(list[dict[str, object]], source["sources"])
    assert len(source_records) == 10
    assert len({record["source_id"] for record in source_records}) == 10
    assert {record["split"] for record in source_records} == {"calibration", "test"}
    assert all(
        cast(str, record["download_url"]).startswith("https://thumb.wikimedia.org/")
        for record in source_records
    )
    assert all("?" not in cast(str, record["download_url"]) for record in source_records)

    items = cast(list[dict[str, object]], annotations["items"])
    assert len(items) == 20
    assert len({item["item_id"] for item in items}) == 20
    split_sources: dict[str, set[object]] = {}
    for split in ("calibration", "test"):
        split_items = [item for item in items if item["split"] == split]
        split_sources[split] = {item["source_id"] for item in split_items}
        split_contract = cast(dict[str, object], cast(dict[str, object], dataset["splits"])[split])
        assert split_contract["item_count"] == len(split_items)
        assert (
            split_contract["annotation_sha256"]
            == hashlib.sha256(_canonical(split_items)).hexdigest()
        )
        item_ids = ("\n".join(cast(str, item["item_id"]) for item in split_items) + "\n").encode()
        assert split_contract["item_ids_sha256"] == hashlib.sha256(item_ids).hexdigest()
        counts = cast(dict[str, int], split_contract["stratum_item_counts"])
        assert counts == {
            "easy": len([item for item in split_items if item["stratum"] == "easy"]),
            "overall": len(split_items),
            "small_distant": len(
                [item for item in split_items if item["stratum"] == "small_distant"]
            ),
        }
    assert split_sources["calibration"].isdisjoint(split_sources["test"])
    benchmark._validate_dataset_locks(annotations, dataset)


def test_source_manifest_is_cc0_privacy_sanitized_and_bounded() -> None:
    manifest = _json(FIXTURE_ROOT / "source-manifest.json")
    assert manifest["license_expression"] == "CC0-1.0"
    assert manifest["terms_url"] == "https://creativecommons.org/publicdomain/zero/1.0/legalcode"
    for source in cast(list[dict[str, object]], manifest["sources"]):
        width = cast(int, source["download_width"])
        height = cast(int, source["download_height"])
        preparation._box(source["object_box"], width, height)
        assert len(cast(str, source["download_sha256"])) == 64
        assert len(cast(str, source["file_sha1"])) == 40
        assert cast(str, source["privacy_review"])
        for redaction in cast(list[dict[str, object]], source["redactions"]):
            assert redaction["kind"] in {"background-plate-risk", "plate"}
            preparation._box(redaction["box"], width, height)


def test_candidate_list_is_a_pinned_same_family_tradeoff() -> None:
    manifest = _json(FIXTURE_ROOT / "candidates.json")
    candidates = cast(list[dict[str, object]], manifest["candidates"])
    assert [candidate["input_width"] for candidate in candidates] == [256, 384, 512]
    assert [candidate["omz_reported_ap_50_95_millionths"] for candidate in candidates] == [
        254000,
        322000,
        363000,
    ]
    assert {candidate["omz_revision"] for candidate in candidates} == {
        "86ba23e80b27eb9149da911e5c023b108cb06e80"
    }
    for candidate in candidates:
        for key in ("model_xml", "model_bin"):
            artifact = cast(dict[str, object], candidate[key])
            assert cast(int, artifact["size"]) > 0
            assert benchmark._DIGEST.fullmatch(cast(str, artifact["sha256"]))
            assert len(cast(str, artifact["download_sha384"])) == 96
            assert cast(str, artifact["url"]).startswith("https://storage.openvinotoolkit.org/")
    runtime = cast(dict[str, dict[str, object]], manifest["runtime"])
    assert {
        component["license_expression"]
        for component in runtime.values()
        if isinstance(component, dict)
    } <= {
        "Apache-2.0",
        "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        "LicenseRef-python-build-standalone-composite-20260825",
    }
    validated, validated_runtime, confidence_floor, grid = benchmark._validate_candidate_manifest(
        manifest
    )
    assert validated == candidates
    assert validated_runtime == runtime
    assert confidence_floor == 10_000
    assert grid == list(range(50_000, 1_000_000, 50_000))


def test_research_json_loaders_reject_duplicate_and_nonfinite_values(tmp_path: Path) -> None:
    for name, raw in {
        "duplicate.json": b'{"schema":1,"schema":2}',
        "nonfinite.json": b'{"value":NaN}',
    }.items():
        path = tmp_path / name
        path.write_bytes(raw)
        with pytest.raises(preparation.PreparationError, match="invalid_manifest"):
            preparation._load_json(path)
        with pytest.raises(benchmark.BenchmarkError, match="invalid_input"):
            benchmark._load_json(path)


def test_candidate_manifest_and_worker_result_fail_closed() -> None:
    candidate_manifest = _json(FIXTURE_ROOT / "candidates.json")
    runtime = cast(dict[str, object], candidate_manifest["runtime"])
    runtime["openvino"] = None
    with pytest.raises(benchmark.BenchmarkError, match="invalid_candidate_manifest"):
        benchmark._validate_candidate_manifest(candidate_manifest)

    annotations_path = FIXTURE_ROOT / "annotations.json"
    annotations = _json(annotations_path)
    items = [
        item
        for item in cast(list[dict[str, object]], annotations["items"])
        if item["split"] == "test"
    ]
    timings = {cast(str, item["item_id"]): 1 for item in items}
    predictions = {
        cast(str, item["item_id"]): [
            {"box_milli_pixels": [0, 0, 1000, 1000], "confidence_millionths": 500_000}
        ]
        for item in items
    }
    result: dict[str, object] = {
        "annotation_lock_sha256": hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
        "cold_start_ns": 1,
        "cpu_ns": 0,
        "item_wall_ns": timings,
        "measurement_wall_ns": 1,
        "numpy_version": "2.5.3",
        "openvino_version": "2026.3.1-22476-759c5a6ab8c-releases/2026/3",
        "peak_rss_bytes": 1,
        "predictions": predictions,
        "process_wall_ns": 1,
        "processed_item_count": len(items),
        "python_version": "3.13.15",
        "seed": 1729,
        "split": "test",
        "telemetry_version": "2025.2.0",
        "warm_start_ns": 1,
    }
    for field, value in (("seed", True), ("seed", 1729.0), ("processed_item_count", True)):
        changed = cast(dict[str, object], json.loads(json.dumps(result)))
        changed[field] = value
        with pytest.raises(benchmark.BenchmarkError, match="invalid_worker_result"):
            benchmark._validate_worker_result(
                changed,
                items,
                split="test",
                seed=1729,
                annotation_sha256=hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
            )
    first_id = cast(str, items[0]["item_id"])
    first_prediction = predictions[first_id][0]
    first_prediction["box_milli_pixels"] = [0, 0, 640_001, 1000]
    with pytest.raises(benchmark.BenchmarkError, match="invalid_worker_result"):
        benchmark._validate_worker_result(
            result,
            items,
            split="test",
            seed=1729,
            annotation_sha256=hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize("bad_version", [True, 1.0])
def test_research_schema_versions_require_exact_integers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bad_version: object,
) -> None:
    candidate = _json(FIXTURE_ROOT / "candidates.json")
    candidate["schema_version"] = bad_version
    with pytest.raises(benchmark.BenchmarkError, match="invalid_candidate_manifest"):
        benchmark._validate_candidate_manifest(candidate)

    annotations = _json(FIXTURE_ROOT / "annotations.json")
    dataset = _json(FIXTURE_ROOT / "dataset-manifest.json")
    annotations["schema_version"] = bad_version
    with pytest.raises(benchmark.BenchmarkError, match="dataset_lock_mismatch"):
        benchmark._validate_dataset_locks(annotations, dataset)
    with pytest.raises(benchmark.BenchmarkError, match="invalid_annotations"):
        benchmark._items(annotations, "test")

    source = _json(FIXTURE_ROOT / "source-manifest.json")
    source["schema_version"] = bad_version
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source), encoding="ascii")
    monkeypatch.setattr(preparation, "SOURCE_MANIFEST", source_path)
    with pytest.raises(preparation.PreparationError, match="invalid_manifest"):
        preparation.prepare(tmp_path, tmp_path / "output")


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("inference_num_threads", 4.0),
        ("num_streams", True),
        ("num_streams", 1.0),
    ],
)
def test_candidate_inference_numbers_require_exact_integers(
    field: str,
    bad_value: object,
) -> None:
    manifest = _json(FIXTURE_ROOT / "candidates.json")
    cast(dict[str, object], manifest["inference"])[field] = bad_value
    with pytest.raises(benchmark.BenchmarkError, match="invalid_candidate_manifest"):
        benchmark._validate_candidate_manifest(manifest)


@pytest.mark.parametrize(
    "bad_name",
    [
        "../../escaped-fp32-openvino-2026.3.1",
        "/tmp/escaped-fp32-openvino-2026.3.1",
        "safe/name-fp32-openvino-2026.3.1",
        "safe\\name-fp32-openvino-2026.3.1",
    ],
)
def test_candidate_names_cannot_escape_roots(bad_name: str) -> None:
    manifest = _json(FIXTURE_ROOT / "candidates.json")
    cast(list[dict[str, object]], manifest["candidates"])[0]["name"] = bad_name
    with pytest.raises(benchmark.BenchmarkError, match="invalid_candidate_manifest"):
        benchmark._validate_candidate_manifest(manifest)


@pytest.mark.parametrize(
    "url",
    [
        "https://files.pythonhosted.org/packages/%2Ftmp%2Foutside.whl",
        "https://files.pythonhosted.org/packages/safe%5Coutside.whl",
        "https://files.pythonhosted.org/packages/..%2Foutside.whl",
    ],
)
def test_runtime_wheel_names_cannot_escape_root(url: str) -> None:
    with pytest.raises(benchmark.BenchmarkError, match="invalid_runtime_manifest"):
        benchmark._wheel_filename(url, "invalid_runtime_manifest")


def test_nonblocking_regular_file_readers_reject_fifos_and_links(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_bytes(b"{}")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    link = tmp_path / "link"
    link.symlink_to(regular)
    started = time.monotonic()
    for path in (fifo, link):
        with pytest.raises(preparation.PreparationError):
            preparation._regular_file(path, maximum=16)
        with pytest.raises(benchmark.BenchmarkError):
            benchmark._read_regular(path, 16, "invalid_input")
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("module", [preparation, benchmark])
def test_research_subprocess_drains_are_hard_bounded(
    module: object,
    stream: str,
) -> None:
    descriptor = 1 if stream == "stdout" else 2
    command = [
        sys.executable,
        "-c",
        f"import os; data=b'x'*65536\nwhile True: os.write({descriptor}, data)",
    ]
    error = preparation.PreparationError if module is preparation else benchmark.BenchmarkError
    started = time.monotonic()
    with pytest.raises(error):
        module._run_bounded(  # type: ignore[attr-defined]
            command,
            timeout_seconds=5,
            maximum_output_bytes=1024,
            code="bounded_failure",
        )
    assert time.monotonic() - started < 2


def test_decoder_sandbox_clears_caller_secrets(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"x")
    output = tmp_path / "output"
    output.mkdir()
    command = preparation._decoder_command(source, output)
    executable = command.index("/usr/bin/ffmpeg")
    environment = dict(os.environ)
    environment["VISUALWORLD_CALLER_SENTINEL"] = "must-not-cross"
    returncode, stdout, stderr = preparation._run_bounded(
        [*command[:executable], "/usr/bin/env"],
        timeout_seconds=5,
        maximum_output_bytes=64 * 1024,
        code="decoder_failed",
        environment=environment,
    )
    assert returncode == 0
    assert stderr == b""
    assert b"VISUALWORLD_CALLER_SENTINEL" not in stdout
    assert b"must-not-cross" not in stdout
    assert b"PATH=/usr/bin:/bin" in stdout
    assert b"HOME=/home/worker" in stdout


@pytest.mark.parametrize("module", [preparation, benchmark])
def test_output_directory_creation_rejects_final_and_ancestor_links(
    module: object,
    tmp_path: Path,
) -> None:
    redirected = tmp_path / "redirected"
    final_link = tmp_path / "final-link"
    final_link.symlink_to(redirected)
    error = preparation.PreparationError if module is preparation else benchmark.BenchmarkError
    with pytest.raises(error):
        module._create_private_directory(final_link)  # type: ignore[attr-defined]
    assert not redirected.exists()

    ancestor_target = tmp_path / "ancestor-target"
    ancestor_target.mkdir()
    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(ancestor_target, target_is_directory=True)
    with pytest.raises(error):
        module._create_private_directory(ancestor_link / "child")  # type: ignore[attr-defined]
    assert not (ancestor_target / "child").exists()


@pytest.mark.parametrize("module", [preparation, benchmark])
def test_output_writes_remain_anchored_after_root_path_replacement(
    module: object,
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    moved = tmp_path / "moved"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    descriptor = module._create_private_directory(output)  # type: ignore[attr-defined]
    try:
        output.rename(moved)
        output.symlink_to(redirected, target_is_directory=True)
        if module is preparation:
            preparation._write_new(descriptor, "nested/value.bin", b"anchored")
            assert (moved / "nested" / "value.bin").read_bytes() == b"anchored"
            assert not (redirected / "nested").exists()
        else:
            benchmark._write_new(descriptor, "result.json", {"anchored": True})
            assert _json(moved / "result.json") == {"anchored": True}
            assert not (redirected / "result.json").exists()
    finally:
        module._close_once(descriptor)  # type: ignore[attr-defined]


def test_benchmark_cli_redacts_paths_and_survives_broken_stdout(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "run_v02_detection_benchmark.py"
    marker = "private-token-never-print"
    blocking_file = tmp_path / marker
    blocking_file.write_bytes(b"x")
    command = [
        sys.executable,
        os.fspath(script),
        "--dataset-root",
        os.fspath(tmp_path),
        "--models-root",
        os.fspath(tmp_path),
        "--base-python",
        os.fspath(tmp_path / "python"),
        "--wheels-root",
        os.fspath(tmp_path),
        "--output",
        os.fspath(blocking_file / "output"),
        "--source-revision",
        "0" * 40,
        "--evaluated-on",
        "2026-09-07",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, timeout=10)
    assert completed.returncode == 1
    assert completed.stderr == b""
    assert json.loads(completed.stdout) == {"error": "output_failed", "status": "error"}
    assert marker.encode() not in completed.stdout + completed.stderr
    assert b"Traceback" not in completed.stdout + completed.stderr

    preparation_script = ROOT / "scripts" / "prepare_v02_detection_dataset.py"
    preparation_command = [
        sys.executable,
        os.fspath(preparation_script),
        "--source-root",
        os.fspath(tmp_path / marker),
        "--output",
        os.fspath(tmp_path / "unused"),
    ]
    preparation_failure = subprocess.run(
        preparation_command,
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert preparation_failure.returncode == 1
    assert preparation_failure.stderr == b""
    assert marker.encode() not in preparation_failure.stdout + preparation_failure.stderr
    assert b"Traceback" not in preparation_failure.stdout + preparation_failure.stderr

    failing_commands = ([sys.executable, os.fspath(script), "--worker"], preparation_command)
    for failing_command in failing_commands:
        for redirection in ("exec 1>/dev/full", "exec 1>&-; exec 2>&-"):
            broken = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    f'{redirection}; exec "$@"',
                    "bash",
                    *failing_command,
                ],
                check=False,
                capture_output=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                timeout=10,
            )
            assert broken.returncode == 1
            assert b"Traceback" not in broken.stderr


def test_verified_runtime_rejects_extra_tampered_and_swapped_inputs(tmp_path: Path) -> None:
    manifest = _json(FIXTURE_ROOT / "candidates.json")
    runtime = cast(dict[str, object], manifest["runtime"])
    wheels = benchmark._verify_runtime_wheels(runtime, ROOT / "artifacts" / "issue21" / "wheels")
    destination = tmp_path / "site-packages"
    benchmark._extract_runtime(wheels, destination)
    extra = destination / "sitecustomize.py"
    extra.write_text("raise SystemExit\n", encoding="ascii")
    with pytest.raises(benchmark.BenchmarkError, match="invalid_runtime_tree"):
        benchmark._verify_extracted_runtime(wheels, destination)
    extra.unlink()
    target = destination / "openvino_telemetry" / "__init__.py"
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(benchmark.BenchmarkError, match="invalid_runtime_tree"):
        benchmark._verify_extracted_runtime(wheels, destination)

    swapped = tmp_path / "python"
    swapped.write_bytes(b"not the pinned interpreter")
    with pytest.raises(benchmark.BenchmarkError, match="runtime_python_mismatch"):
        benchmark._verify_base_python(swapped, runtime)


def test_v2_policy_enforces_bound_runtime_license_evidence() -> None:
    policy, _ = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
    )
    original, _ = evaluator.load_policy()
    inherited = cast(dict[str, object], json.loads(json.dumps(policy)))
    inherited["policy_version"] = original["policy_version"]
    inherited.pop("allowed_bundled_component_license_expressions")
    inherited.pop("allowed_runtime_license_expressions")
    inherited.pop("required_runtime_license_evidence_sha256")
    inherited.pop("runtime_license_evidence_exempt_sha256")
    assert inherited == original
    manifest = _json(FIXTURE_ROOT / "candidates.json")
    candidates = cast(list[dict[str, object]], manifest["candidates"])
    runtime = cast(dict[str, object], manifest["runtime"])
    candidate = {
        "artifacts": benchmark._candidate_artifacts(candidates[0], runtime, "2026-09-07"),
        "configuration_sha256": "1" * 64,
        "name": candidates[0]["name"],
        "runtime_closure_sha256": "2" * 64,
    }
    evaluator._validate_candidate(candidate, policy, date(2026, 9, 7), "invalid_receipt")
    for copyleft_expression in (
        "GPL-3.0-or-later WITH GCC-exception-3.1",
        "LGPL-2.1-or-later",
    ):
        top_level_copyleft = cast(dict[str, object], json.loads(json.dumps(candidate)))
        top_level_artifacts = cast(list[dict[str, object]], top_level_copyleft["artifacts"])
        model_weight = next(
            artifact for artifact in top_level_artifacts if artifact["kind"] == "weights"
        )
        model_weight["license_expression"] = copyleft_expression
        with pytest.raises(evaluator.EvaluationError, match="invalid_receipt"):
            evaluator._validate_candidate(
                top_level_copyleft, policy, date(2026, 9, 7), "invalid_receipt"
            )

    changed = cast(dict[str, object], json.loads(json.dumps(candidate)))
    artifacts = cast(list[dict[str, object]], changed["artifacts"])
    numpy_artifact = next(artifact for artifact in artifacts if artifact["name"] == "numpy")
    evidence = cast(dict[str, object], numpy_artifact["license_evidence"])
    notices = cast(list[dict[str, object]], evidence["notice_files"])
    notices[0]["sha256"] = "0" * 64
    with pytest.raises(evaluator.EvaluationError, match="invalid_receipt"):
        evaluator._validate_candidate(changed, policy, date(2026, 9, 7), "invalid_receipt")


def test_published_machine_results_reproduce_frozen_gate_outputs() -> None:
    raw_path = RESULT_ROOT / "raw-results.json"
    raw_bytes = raw_path.read_bytes()
    raw = cast(dict[str, object], json.loads(raw_bytes))
    assert hashlib.sha256(raw_bytes).hexdigest() == (
        "1346459c77f47a150b59c257370f4fa20ce96e68ae61d669ed0f877866a689dc"
    )
    assert b"/root" not in raw_bytes
    provenance = cast(dict[str, object], raw["provenance"])
    assert provenance["source_revision"] == "7c77e6ec0b84549f9b79c34078fe8f6a3c641338"
    assert (
        provenance["evaluation_harness_sha256"]
        == hashlib.sha256(
            (ROOT / "scripts" / "run_v02_detection_benchmark.py").read_bytes()
        ).hexdigest()
    )

    policy, policy_sha256 = evaluator.load_policy(
        ROOT / "fixtures" / "v02-evaluation" / "policy-v0.2-gates-2.json"
    )
    dataset, dataset_sha256 = evaluator.load_manifest(
        FIXTURE_ROOT / "dataset-manifest.json", policy
    )
    outputs = cast(dict[str, dict[str, object]], raw["outputs"])
    expected_status = {
        "vehicle-detection-0200": "fail",
        "vehicle-detection-0201": "pass",
        "vehicle-detection-0202": "pass",
    }
    for short_name, status in expected_status.items():
        candidate = f"{short_name}-fp32-openvino-2026.3.1"
        output = outputs[candidate]
        receipt_path = RESULT_ROOT / cast(str, output["receipt"])
        receipt_bytes = receipt_path.read_bytes()
        receipt = cast(dict[str, object], json.loads(receipt_bytes))
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        assert receipt_sha256 == output["receipt_sha256"]
        validated = evaluator.validate_receipt(
            receipt,
            policy,
            policy_sha256,
            dataset,
            dataset_sha256,
        )
        generated_gate = evaluator.evaluate(
            policy,
            validated,
            baseline=None,
            baseline_receipt_sha256=None,
            receipt_sha256=receipt_sha256,
            as_of=date(2026, 9, 7),
        )
        gate_path = RESULT_ROOT / cast(str, output["gate"])
        gate_bytes = gate_path.read_bytes()
        assert hashlib.sha256(gate_bytes).hexdigest() == output["gate_sha256"]
        assert cast(dict[str, object], json.loads(gate_bytes)) == generated_gate
        assert generated_gate["status"] == status


def test_rgb24_primitives_are_deterministic_and_exact() -> None:
    pixels = bytes((1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12))
    assert preparation.resize_rgb24(pixels, 2, 2, 2, 2) == pixels
    mutable = bytearray(pixels)
    preparation.fill_box(mutable, 2, 2, (1, 0, 2, 1), (20, 21, 22))
    assert mutable == bytes((1, 2, 3, 20, 21, 22, 7, 8, 9, 10, 11, 12))
    assert preparation.crop_rgb24(mutable, 2, 2, (1, 0, 2, 2)) == (
        1,
        2,
        bytes((20, 21, 22, 10, 11, 12)),
    )
    first = preparation.compose_item(
        "fixture",
        "small_distant",
        2,
        2,
        pixels,
        64,
        36,
        {
            "easy_max_height": 24,
            "easy_max_width": 46,
            "small_distant_max_height": 6,
            "small_distant_max_width": 9,
        },
    )
    assert first == preparation.compose_item(
        "fixture",
        "small_distant",
        2,
        2,
        pixels,
        64,
        36,
        {
            "easy_max_height": 24,
            "easy_max_width": 46,
            "small_distant_max_height": 6,
            "small_distant_max_width": 9,
        },
    )
    parsed = preparation.parse_ppm(first[0])
    assert parsed[:2] == (64, 36)


def _metric_fixture() -> tuple[list[dict[str, object]], dict[str, object]]:
    items: list[dict[str, object]] = [
        {"item_id": "easy", "object_box": [0, 0, 10, 10], "stratum": "easy"},
        {
            "item_id": "small",
            "object_box": [0, 0, 10, 10],
            "stratum": "small_distant",
        },
    ]
    repetition: dict[str, object] = {
        "predictions": {
            "easy": [
                {
                    "box_milli_pixels": [20_000, 20_000, 30_000, 30_000],
                    "confidence_millionths": 900_000,
                },
                {
                    "box_milli_pixels": [0, 0, 10_000, 10_000],
                    "confidence_millionths": 800_000,
                },
            ],
            "small": [
                {
                    "box_milli_pixels": [0, 0, 10_000, 10_000],
                    "confidence_millionths": 700_000,
                },
                {
                    "box_milli_pixels": [20_000, 20_000, 30_000, 30_000],
                    "confidence_millionths": 200_000,
                },
            ],
        }
    }
    return items, repetition


def test_detection_metrics_count_false_positives_and_ranked_ap() -> None:
    items, repetition = _metric_fixture()
    metrics = benchmark.accuracy_metrics(repetition, items, 500_000)
    assert metrics == {
        "map50_basis_points": {"easy": 5000, "overall": 6666, "small_distant": 10000},
        "precision_basis_points": {"easy": 5000, "overall": 6666, "small_distant": 10000},
        "recall_basis_points": {"easy": 10000, "overall": 10000, "small_distant": 10000},
    }
    assert benchmark.iou_basis_points((0, 0, 10, 10), (5, 0, 15, 10)) == 3333


def test_threshold_selection_and_receipt_aggregation_are_adverse() -> None:
    items, repetition = _metric_fixture()
    calibration = cast(dict[str, object], json.loads(json.dumps(repetition)))
    calibration_predictions = cast(dict[str, list[dict[str, object]]], calibration["predictions"])
    calibration_predictions["easy"][0]["confidence_millionths"] = 100_000
    selected, summaries = benchmark.choose_threshold(
        [calibration] * 5,
        items,
        [200_000, 700_000],
    )
    assert selected == 700_000
    assert [summary["passes_calibration_floors"] for summary in summaries] == [False, True]

    repetitions: list[dict[str, object]] = [
        {
            "metrics": {
                "peak_rss_bytes": {"overall": value},
                "warm_start_wall_ms": {"overall": index},
            }
        }
        for index, value in enumerate((10, 12, 11, 15, 13), start=1)
    ]
    assert benchmark._aggregate(repetitions) == {
        "dispersion": {
            "peak_rss_bytes": {"overall": 1},
            "warm_start_wall_ms": {"overall": 1},
        },
        "metrics": {
            "peak_rss_bytes": {"overall": 15},
            "warm_start_wall_ms": {"overall": 3},
        },
    }
