# SPDX-License-Identifier: Apache-2.0
"""Shared v1 port and deterministic-fake contract tests."""

from __future__ import annotations

import builtins
import hashlib
import socket
import sqlite3
import subprocess
from collections.abc import Callable
from typing import NoReturn, cast

import pytest

from visualworld.ingestion import (
    Artifact,
    EvidenceRef,
    Fingerprint,
    FrameRef,
    Geometry,
    MediaTime,
    Rational,
    Record,
    RunManifest,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.ports import (
    MAX_PORT_BATCH_ITEMS,
    CapabilityDescriptor,
    Effect,
    EvidenceStore,
    FakeEvidenceStore,
    FakeFrameSampler,
    FakeVideoSource,
    FakeWorldStore,
    FrameSampler,
    PortCall,
    PortError,
    PortErrorCode,
    PortKind,
    VideoSource,
    WorldStore,
)


def records() -> tuple[
    Source,
    tuple[FrameRef, ...],
    Artifact,
    EvidenceRef,
    RunManifest,
    bytes,
]:
    time_base = TimeBase("1", "1000")
    source = Source.create(
        Fingerprint("a0" * 32, "10"),
        (SourceStream(0, 16, 12, 0, time_base),),
    )
    frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index * 100), time_base),
        )
        for index in range(3)
    )
    content = b"deterministic-pixels"
    artifact = Artifact(hashlib.sha256(content).hexdigest(), str(len(content)))
    evidence = EvidenceRef.create(
        frames[1].frame_id,
        artifact,
        Geometry(16, 12, (1, 2, 8, 10), "measured"),
    )
    run = RunManifest.create(
        source.source_id,
        (),
        Sampling(Rational("5", "1")),
        "preparing",
    )
    return source, frames, artifact, evidence, run, content


def test_fakes_satisfy_only_the_four_v1_port_protocols() -> None:
    source, frames, _, _, _, _ = records()
    implementations: tuple[object, ...] = (
        FakeVideoSource(source, frames),
        FakeFrameSampler((frames[0].frame_id,)),
        FakeEvidenceStore(),
        FakeWorldStore(),
    )
    protocols: tuple[type[object], ...] = (VideoSource, FrameSampler, EvidenceStore, WorldStore)

    for implementation, protocol in zip(implementations, protocols, strict=True):
        assert isinstance(implementation, protocol)


def test_capability_descriptors_are_bounded_offline_and_deny_ambient_effects() -> None:
    source, frames, _, _, _, _ = records()
    descriptors = (
        FakeVideoSource(source, frames).descriptor,
        FakeFrameSampler(()).descriptor,
        FakeEvidenceStore().descriptor,
        FakeWorldStore().descriptor,
    )

    assert {descriptor.port for descriptor in descriptors} == set(PortKind)
    for descriptor in descriptors:
        assert descriptor.contract_version == 1
        assert descriptor.deterministic is True
        assert descriptor.offline is True
        assert descriptor.allowed_effects == ()
        for effect in Effect:
            with pytest.raises(PortError) as raised:
                descriptor.require(effect)
            assert raised.value.code is PortErrorCode.CAPABILITY_DENIED
            assert effect.value not in str(raised.value)


def test_video_source_is_deterministic_bounded_and_instrumented() -> None:
    source, frames, _, _, _, _ = records()
    fake = FakeVideoSource(source, tuple(reversed(frames)))

    assert fake.probe() == source
    assert fake.read_frames(limit=2) == frames[:2]
    assert fake.read_frames(after_decode_index="0", limit=2) == frames[1:]
    assert fake.calls == (
        PortCall(PortKind.VIDEO_SOURCE, "probe", 1),
        PortCall(PortKind.VIDEO_SOURCE, "read_frames", 2),
        PortCall(PortKind.VIDEO_SOURCE, "read_frames", 2),
    )

    with pytest.raises(PortError, match="limit_exceeded"):
        fake.read_frames(limit=0)
    with pytest.raises(PortError, match="invalid_request"):
        fake.read_frames(after_decode_index="01")
    with pytest.raises(PortError, match="limit_exceeded"):
        fake.read_frames(after_decode_index=str(2**64))
    with pytest.raises(PortError, match="conflict"):
        FakeVideoSource(source, (frames[0], frames[0]))


def test_frame_sampler_returns_only_configured_candidates_in_configured_order() -> None:
    source, frames, _, _, _, _ = records()
    fake = FakeFrameSampler((frames[2].frame_id, frames[0].frame_id))
    sampling = Sampling(Rational("5", "1"))

    assert fake.sample(source, frames, sampling) == (frames[2], frames[0])
    assert fake.sample(source, frames, sampling) == (frames[2], frames[0])
    assert fake.calls == (
        PortCall(PortKind.FRAME_SAMPLER, "sample", 2),
        PortCall(PortKind.FRAME_SAMPLER, "sample", 2),
    )

    missing = FakeFrameSampler(("frm_" + "0" * 64,))
    with pytest.raises(PortError, match="conflict"):
        missing.sample(source, frames, sampling)
    with pytest.raises(PortError, match="limit_exceeded"):
        fake.sample(source, cast(tuple[FrameRef, ...], list(frames)), sampling)


def test_evidence_store_verifies_content_and_is_idempotent() -> None:
    _, _, artifact, _, _, content = records()
    fake = FakeEvidenceStore(max_payload_bytes=len(content))

    assert fake.put(artifact, content) == artifact
    assert fake.put(artifact, content) == artifact
    assert fake.get(artifact.sha256) == content
    assert fake.calls == (
        PortCall(PortKind.EVIDENCE_STORE, "put", 1),
        PortCall(PortKind.EVIDENCE_STORE, "put", 1),
        PortCall(PortKind.EVIDENCE_STORE, "get", 1),
    )

    with pytest.raises(PortError, match="conflict"):
        fake.put(artifact, b"changed")
    with pytest.raises(PortError, match="limit_exceeded"):
        FakeEvidenceStore(max_payload_bytes=1).put(artifact, content)
    with pytest.raises(PortError, match="not_found"):
        fake.get("0" * 64)


def test_world_store_commits_atomically_and_lists_in_stable_order() -> None:
    source, frames, _, evidence, run, _ = records()
    fake = FakeWorldStore()
    batch: tuple[Record, ...] = (source, frames[2], frames[0], frames[1], evidence, run)

    fake.commit(batch)
    assert fake.get(source.source_id) == source

    with pytest.raises(PortError, match="invalid_request"):
        fake.list_frames(frames[0].frame_id, limit=1)
    with pytest.raises(PortError, match="invalid_request"):
        fake.list_evidence(source.source_id, limit=1)
    assert fake.get(run.run_id) == run
    assert fake.list_frames(source.source_id, limit=2) == frames[:2]
    assert fake.list_frames(source.source_id, after_decode_index="1", limit=2) == frames[2:]
    assert fake.list_evidence(frames[1].frame_id, limit=2) == (evidence,)

    before = fake.calls
    with pytest.raises(PortError, match="invalid_request"):
        fake.commit((cast(Record, object()),))
    assert fake.calls == before
    assert fake.get(source.source_id) == source


@pytest.mark.parametrize(
    "invalid",
    [
        lambda: CapabilityDescriptor(PortKind.VIDEO_SOURCE, "bad space", "1", True, True),
        lambda: CapabilityDescriptor(PortKind.VIDEO_SOURCE, "fake", "1", True, True, 65),
        lambda: CapabilityDescriptor(
            PortKind.VIDEO_SOURCE,
            "fake",
            "1",
            True,
            True,
            max_payload_bytes=2**63,
        ),
        lambda: CapabilityDescriptor(
            PortKind.VIDEO_SOURCE,
            "fake",
            "1",
            True,
            True,
            allowed_effects=(Effect.SHELL,),
        ),
        lambda: PortCall(PortKind.VIDEO_SOURCE, "bad space", 0),
        lambda: PortCall(PortKind.VIDEO_SOURCE, "probe", 65),
        lambda: PortError(
            PortErrorCode.CONFLICT,
            PortKind.VIDEO_SOURCE,
            "bad space",
        ),
        lambda: FakeEvidenceStore(max_payload_bytes=-1),
    ],
)
def test_capability_and_instrumentation_records_reject_invalid_values(
    invalid: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        invalid()


def test_port_errors_do_not_echo_untrusted_values() -> None:
    fake = FakeWorldStore()
    untrusted = "secret-path\nattack"

    with pytest.raises(PortError) as raised:
        fake.get(untrusted)
    assert raised.value.code is PortErrorCode.INVALID_REQUEST
    assert raised.value.port is PortKind.WORLD_STORE
    assert raised.value.operation == "get"
    assert raised.value.retryable is False
    assert untrusted not in str(raised.value)


def test_world_store_rejects_orphan_references_without_partial_commit() -> None:
    source, frames, _, evidence, run, _ = records()
    fake = FakeWorldStore()

    for orphan in (frames[0], evidence, run):
        with pytest.raises(PortError, match="conflict"):
            fake.commit((orphan,))
    with pytest.raises(PortError, match="not_found"):
        fake.get(source.source_id)


def test_fakes_do_not_touch_shell_network_filesystem_or_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, frames, artifact, evidence, run, content = records()
    video = FakeVideoSource(source, frames)
    sampler = FakeFrameSampler((frames[0].frame_id,))
    evidence_store = FakeEvidenceStore()
    world_store = FakeWorldStore()
    attempts: list[str] = []

    def denied(*_: object, **__: object) -> NoReturn:
        attempts.append("ambient-effect")
        raise AssertionError("ambient effect attempted")

    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(subprocess, "run", denied)

    assert video.probe() == source
    assert video.read_frames(limit=1) == frames[:1]
    assert sampler.sample(source, frames, Sampling(Rational("5", "1"))) == frames[:1]
    assert evidence_store.put(artifact, content) == artifact
    assert evidence_store.get(artifact.sha256) == content
    world_store.commit((source, *frames, evidence, run))
    assert world_store.get(run.run_id) == run
    assert attempts == []


def test_fake_batches_are_bounded() -> None:
    source, _, _, _, _, _ = records()
    many_frames = tuple(
        FrameRef.create(
            source.source_id,
            0,
            str(index),
            MediaTime(str(index), TimeBase("1", "1000")),
        )
        for index in range(MAX_PORT_BATCH_ITEMS + 1)
    )

    with pytest.raises(PortError, match="limit_exceeded"):
        FakeVideoSource(source, many_frames)
    with pytest.raises(PortError, match="limit_exceeded"):
        FakeWorldStore().commit(cast(tuple[Record, ...], many_frames))
