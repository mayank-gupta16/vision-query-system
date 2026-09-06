# SPDX-License-Identifier: Apache-2.0
"""Contract and exact-rational goldens for the production frame sampler."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from visualworld.ingestion import (
    Fingerprint,
    FrameRef,
    MediaTime,
    Rational,
    Sampling,
    Source,
    SourceStream,
    TimeBase,
)
from visualworld.ports import FrameSampler, PortCall, PortError, PortErrorCode, PortKind
from visualworld.sampling import (
    PtsFrameSampler,
    ResumableFrameSampler,
    SamplingCursor,
    SamplingLimits,
)


def _source(time_base: TimeBase | None = None) -> Source:
    selected_time_base = TimeBase("1", "1000") if time_base is None else time_base
    return Source.create(
        Fingerprint("ab" * 32, "1000"),
        (SourceStream(0, 16, 12, 0, selected_time_base),),
    )


def _frame(
    source: Source,
    decode_index: int,
    pts: int,
    *,
    duration: int | None = None,
) -> FrameRef:
    time_base = source.streams[0].time_base
    return FrameRef.create(
        source.source_id,
        0,
        str(decode_index),
        MediaTime(str(pts), time_base),
        None if duration is None else MediaTime(str(duration), time_base),
    )


def _collect_pages(
    sampler: PtsFrameSampler,
    source: Source,
    candidates: tuple[FrameRef, ...],
    ends: tuple[int, ...],
) -> tuple[FrameRef, ...]:
    sampling = Sampling(Rational("5", "1"))
    selected: list[FrameRef] = []
    cursor: SamplingCursor | None = None
    start = 0
    for end in ends:
        page_candidates = candidates[start:end] if cursor is None else candidates[start - 1 : end]
        page = sampler.sample_page(
            source,
            page_candidates,
            sampling,
            cursor=cursor,
            end_of_stream=end == len(candidates),
        )
        selected.extend(page.frames)
        cursor = page.cursor
        start = end
    assert cursor is not None and cursor.finished is True
    return tuple(selected)


def test_cfr_one_shot_uses_exact_pts_and_frame_sampler_contract() -> None:
    source = _source(TimeBase("1", "30"))
    candidates = tuple(_frame(source, index, index) for index in range(30))
    sampler = PtsFrameSampler()

    result = sampler.sample(source, candidates, Sampling(Rational("5", "1")))

    assert isinstance(sampler, FrameSampler)
    assert isinstance(sampler, ResumableFrameSampler)
    assert [frame.decode_index for frame in result] == ["0", "6", "12", "18", "24"]
    assert all(frame is candidates[int(frame.decode_index)] for frame in result)
    assert [frame.pts.value for frame in result] == ["0", "6", "12", "18", "24"]
    assert sampler.calls == (PortCall(PortKind.FRAME_SAMPLER, "sample", 5),)
    assert sampler.descriptor.deterministic is True
    assert sampler.descriptor.offline is True
    assert sampler.descriptor.allowed_effects == ()


def test_vfr_gap_policy_uses_nearest_boundaries_and_last_duration() -> None:
    source = _source()
    candidates = (
        _frame(source, 0, 0),
        _frame(source, 1, 100),
        _frame(source, 2, 350),
        _frame(source, 3, 500, duration=300),
    )

    result = PtsFrameSampler().sample(source, candidates, Sampling(Rational("5", "1")))

    assert [frame.pts.value for frame in result] == ["0", "100", "350", "500"]
    assert tuple(frame.pts for frame in result) == tuple(frame.pts for frame in candidates)
    assert tuple(frame.frame_id for frame in result) == tuple(
        frame.frame_id for frame in candidates
    )


def test_midpoint_ties_use_lower_decode_index_and_duplicate_pts_emit_once() -> None:
    source = _source()
    candidates = (
        _frame(source, 0, -100),
        _frame(source, 1, 0),
        _frame(source, 2, 200),
        _frame(source, 3, 200),
        _frame(source, 4, 210),
    )

    result = PtsFrameSampler().sample(source, candidates, Sampling(Rational("5", "1")))

    assert [frame.decode_index for frame in result] == ["0", "1"]
    assert [frame.pts.value for frame in result] == ["-100", "0"]

    duplicate_source = _source()
    duplicate_candidates = (
        _frame(duplicate_source, 0, 0),
        _frame(duplicate_source, 1, 190),
        _frame(duplicate_source, 2, 190),
        _frame(duplicate_source, 3, 400),
    )
    resumed = _collect_pages(PtsFrameSampler(), duplicate_source, duplicate_candidates, (3, 4))
    assert [frame.decode_index for frame in resumed] == ["0", "1", "3"]


def test_resumed_pages_match_one_shot_across_boundaries() -> None:
    source = _source()
    values = (0, 50, 190, 210, 400, 610, 790)
    candidates = tuple(
        _frame(source, index, pts, duration=210 if index == len(values) - 1 else None)
        for index, pts in enumerate(values)
    )
    expected = PtsFrameSampler().sample(source, candidates, Sampling(Rational("5", "1")))

    for ends in ((2, 5, 7), (1, 2, 3, 4, 5, 6, 7), (6, 7)):
        resumed = _collect_pages(PtsFrameSampler(), source, candidates, ends)
        assert [frame.frame_id for frame in resumed] == [frame.frame_id for frame in expected]
        assert len({frame.frame_id for frame in resumed}) == len(resumed)


def test_resume_requires_the_exact_full_overlap_record() -> None:
    source = _source()
    candidates = (_frame(source, 0, 0), _frame(source, 1, 200))
    sampling = Sampling(Rational("5", "1"))
    sampler = PtsFrameSampler()
    first = sampler.sample_page(source, candidates, sampling)
    assert first.cursor is not None
    changed_overlap = replace(candidates[-1], duration=MediaTime("100", TimeBase("1", "1000")))
    assert changed_overlap.frame_id == candidates[-1].frame_id

    with pytest.raises(PortError, match="conflict"):
        sampler.sample_page(
            source,
            (changed_overlap, _frame(source, 2, 400)),
            sampling,
            cursor=first.cursor,
            end_of_stream=True,
        )


class _CancelDuringPage(threading.Event):
    def __init__(self, after_checks: int) -> None:
        super().__init__()
        self._after_checks = after_checks
        self._checks = 0

    def is_set(self) -> bool:
        self._checks += 1
        return self._checks >= self._after_checks


def test_cancellation_does_not_publish_cursor_and_retry_is_identical() -> None:
    source = _source()
    candidates = tuple(_frame(source, index, index * 100) for index in range(8))
    sampling = Sampling(Rational("5", "1"))
    sampler = PtsFrameSampler()
    first = sampler.sample_page(source, candidates[:4], sampling)
    assert first.cursor is not None
    call_count = len(sampler.calls)
    second_candidates = candidates[3:]

    with pytest.raises(PortError) as raised:
        sampler.sample_page(
            source,
            second_candidates,
            sampling,
            cursor=first.cursor,
            end_of_stream=True,
            cancelled=_CancelDuringPage(3),
        )
    assert raised.value.code is PortErrorCode.CANCELLED
    assert raised.value.__cause__ is None
    assert len(sampler.calls) == call_count

    resumed = sampler.sample_page(
        source,
        second_candidates,
        sampling,
        cursor=first.cursor,
        end_of_stream=True,
    )
    expected = _collect_pages(PtsFrameSampler(), source, candidates, (4, 8))
    assert [frame.frame_id for frame in (*first.frames, *resumed.frames)] == [
        frame.frame_id for frame in expected
    ]


def test_high_target_rate_and_large_gap_do_not_duplicate_or_iterate_targets() -> None:
    source = _source(TimeBase("1", "1"))
    candidates = (_frame(source, 0, 0), _frame(source, 1, 3600))

    result = PtsFrameSampler().sample(source, candidates, Sampling(Rational("1000", "1")))

    assert result == candidates


def test_page_total_frame_and_duration_caps_fail_without_partial_progress() -> None:
    source = _source()
    sampling = Sampling(Rational("5", "1"))
    page_limited = PtsFrameSampler(
        limits=SamplingLimits(max_page_candidates=2, max_total_candidates=3)
    )
    with pytest.raises(PortError, match="limit_exceeded"):
        page_limited.sample(
            source,
            tuple(_frame(source, index, index * 100) for index in range(3)),
            sampling,
        )

    total_limited = PtsFrameSampler(
        limits=SamplingLimits(max_page_candidates=3, max_total_candidates=3)
    )
    first = total_limited.sample_page(
        source,
        (_frame(source, 0, 0), _frame(source, 1, 100)),
        sampling,
    )
    assert first.cursor is not None
    with pytest.raises(PortError, match="limit_exceeded"):
        total_limited.sample_page(
            source,
            (
                _frame(source, 1, 100),
                _frame(source, 2, 200),
                _frame(source, 3, 300),
            ),
            sampling,
            cursor=first.cursor,
            end_of_stream=True,
        )

    duration_limited = PtsFrameSampler(limits=SamplingLimits(max_duration_seconds=1))
    with pytest.raises(PortError, match="limit_exceeded"):
        duration_limited.sample(
            source,
            (_frame(source, 0, 0), _frame(source, 1, 1001)),
            sampling,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"max_page_candidates": 0},
        {"max_page_candidates": 65},
        {"max_total_candidates": 0},
        {"max_duration_seconds": True},
    ],
)
def test_sampling_limits_are_bounded(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "max_page_candidates": 64,
        "max_total_candidates": 100_000,
        "max_duration_seconds": 3600,
    }
    values.update(changes)
    with pytest.raises(ValueError):
        SamplingLimits(**cast(Any, values))


def test_invalid_candidates_and_cursor_fail_with_structured_errors() -> None:
    source = _source()
    other = Source.create(Fingerprint("cd" * 32, "1"), source.streams)
    valid = _frame(source, 0, 0)
    later = _frame(source, 1, 200)
    sampling = Sampling(Rational("5", "1"))
    sampler = PtsFrameSampler()
    first = sampler.sample_page(source, (valid, later), sampling)
    assert first.cursor is not None
    wrong_time_base = FrameRef.create(
        source.source_id,
        0,
        "1",
        MediaTime("1", TimeBase("1", "90000")),
    )
    cases = (
        cast(tuple[FrameRef, ...], [valid]),
        (_frame(other, 0, 0),),
        (valid, valid),
        (valid, wrong_time_base),
        (later, valid),
        (_frame(source, 0, 1), _frame(source, 1, 0)),
    )
    for candidates in cases:
        with pytest.raises(PortError) as raised:
            sampler.sample(source, candidates, sampling)
        assert raised.value.code in {PortErrorCode.INVALID_REQUEST, PortErrorCode.CONFLICT}
        assert source.source_id not in str(raised.value)

    with pytest.raises(PortError, match="conflict"):
        sampler.sample_page(
            source,
            (later, _frame(source, 2, 400)),
            Sampling(Rational("4", "1")),
            cursor=first.cursor,
            end_of_stream=True,
        )
    finished = replace(first.cursor, finished=True)
    with pytest.raises(PortError, match="conflict"):
        sampler.sample_page(
            source,
            (later, _frame(source, 2, 400)),
            sampling,
            cursor=finished,
            end_of_stream=True,
        )


def test_empty_end_of_stream_is_valid_but_open_empty_page_is_not() -> None:
    source = _source()
    sampler = PtsFrameSampler()
    sampling = Sampling(Rational("5", "1"))
    page = sampler.sample_page(source, (), sampling, end_of_stream=True)
    assert page.frames == () and page.cursor is None and page.finished is True
    with pytest.raises(PortError, match="invalid_request"):
        sampler.sample_page(source, (), sampling)


def test_sampler_uses_no_ambient_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    candidate = _frame(source, 0, 0)

    def denied(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("filesystem access attempted")

    monkeypatch.setattr(Path, "open", denied)
    assert PtsFrameSampler().sample(source, (candidate,), Sampling(Rational("5", "1"))) == (
        candidate,
    )
