"""Tests for grep_scanner streaming."""

import weakref
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from inspect_ai.event import Event, InfoEvent
from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalLog,
    EvalSample,
    EvalSpec,
    write_eval_log,
)
from inspect_ai.model import ChatMessage, ChatMessageUser
from inspect_scout import (
    Loader,
    Scanner,
    Transcript,
    TranscriptInfo,
    grep_scanner,
    loader,
    scan,
    scan_results_df,
    scanner,
    transcripts_from,
)
from inspect_scout._scanner.result import Reference
from inspect_scout._transcript.handle import SpooledTranscriptHandle


class StreamingTranscript:
    """One-pass input that rejects materialization and shared-handle closure."""

    def __init__(
        self,
        messages: Iterable[ChatMessage] = (),
        events: Iterable[Event] = (),
    ) -> None:
        self.info = TranscriptInfo(transcript_id="test")
        self._messages = messages
        self._events = events
        self.message_reads = 0
        self.event_reads = 0
        self.closed_iterators = 0

    async def messages(self) -> AsyncIterator[ChatMessage]:
        self.message_reads += 1
        assert self.message_reads == 1
        try:
            for message in self._messages:
                yield message
        finally:
            self.closed_iterators += 1

    async def events(self) -> AsyncIterator[Event]:
        self.event_reads += 1
        assert self.event_reads == 1
        try:
            for event in self._events:
                yield event
        finally:
            self.closed_iterators += 1

    async def load(self) -> Transcript:
        raise AssertionError("Grep must not materialize the transcript")

    async def aclose(self) -> None:
        raise AssertionError("The scan owns the shared handle")

    async def __aenter__(self) -> "StreamingTranscript":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


@pytest.mark.asyncio
async def test_streaming_releases_content_and_preserves_citations() -> None:
    message_refs: list[weakref.ReferenceType[ChatMessageUser]] = []
    event_refs: list[weakref.ReferenceType[InfoEvent]] = []

    def messages() -> Iterable[ChatMessageUser]:
        for index in range(100):
            # At most the previous item may be held when requesting the next.
            assert sum(ref() is not None for ref in message_refs) <= 1
            message = ChatMessageUser(
                id=f"m{index}", content="error error " + "x" * 1000
            )
            message_refs.append(weakref.ref(message))
            yield message

    def events() -> Iterable[InfoEvent]:
        for index in range(100):
            assert sum(ref() is not None for ref in event_refs) <= 1
            event = InfoEvent(uuid=f"e{index}", data="error " + "x" * 1000)
            event_refs.append(weakref.ref(event))
            yield event

    handle = StreamingTranscript(messages(), events())
    scan_fn = grep_scanner({"errors": "error", "missing": "absent", "empty": []})
    # Scanner's public input type omits handles, as it does for llm_scanner.
    results = await scan_fn(cast(Transcript, handle))

    assert isinstance(results, list)
    assert [result.label for result in results] == ["errors", "missing", "empty"]
    assert [result.value for result in results] == [300, 0, 0]
    assert results[0].references == [
        Reference(type="message", cite=f"[M{index + 1}]", id=f"m{index}")
        for index in range(100)
    ] + [
        Reference(type="event", cite=f"[E{index + 1}]", id=f"e{index}")
        for index in range(100)
    ]
    assert results[0].explanation is not None
    assert "[M100]: USER: error **error**" in results[0].explanation
    assert "[E100]: INFO: **error**" in results[0].explanation
    assert all(result.explanation is None for result in results[1:])
    assert all(result.references == [] for result in results[1:])
    assert all(ref() is None for ref in message_refs)
    assert all(ref() is None for ref in event_refs)
    assert handle.closed_iterators == 2


@pytest.mark.asyncio
async def test_closes_active_iterator_when_matching_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object) -> None:
        raise RuntimeError("matching failed")

    handle = StreamingTranscript([ChatMessageUser(content="error")])
    monkeypatch.setattr(
        "inspect_scout._grep_scanner._grep_scanner.find_matches_in_messages", fail
    )
    with pytest.raises(RuntimeError, match="matching failed"):
        await grep_scanner("error")(cast(Transcript, handle))
    assert handle.closed_iterators == 1
    assert handle.event_reads == 0


@pytest.fixture
def eval_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def data_path(package_name: str) -> Path:
        return tmp_path / package_name

    monkeypatch.setattr("inspect_ai._util.appdirs.user_data_path", data_path)
    path = tmp_path / "input.eval"
    write_eval_log(
        EvalLog(
            status="success",
            eval=EvalSpec(
                dataset=EvalDataset(name="grep-test"),
                model="mockllm/model",
                config=EvalConfig(),
                created=datetime(2026, 1, 1).isoformat(),
                task="grep-test",
            ),
            samples=[
                EvalSample(
                    id="test",
                    uuid="grep-test",
                    epoch=1,
                    input="error",
                    target="error",
                    metadata={"large": "error " * 5000},
                    messages=[ChatMessageUser(id="task", content="error")],
                    events=[InfoEvent(uuid="event", data="error")],
                )
            ],
        ),
        path,
    )
    return path


@pytest.mark.parametrize("metadata", [False, True, None])
@pytest.mark.parametrize("include_events", [False, True])
def test_scan_streams_eval_logs_and_respects_metadata(
    tmp_path: Path,
    eval_log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: bool | None,
    include_events: bool,
) -> None:
    observed: list[bool] = []
    original = SpooledTranscriptHandle.messages

    async def observe_messages(
        self: SpooledTranscriptHandle,
    ) -> AsyncIterator[ChatMessage]:
        parsed = await self.parsed_result()
        assert parsed is not None
        observed.append(parsed.has_metadata)
        async for message in original(self):
            yield message

    async def forbid_load(self: SpooledTranscriptHandle) -> Transcript:
        raise AssertionError("The scan must stay on the streaming path")

    @scanner(messages="all", events=["info"], metadata=False)
    def with_events() -> Scanner[Transcript]:
        if metadata is None:
            return grep_scanner("error")
        return grep_scanner("error", metadata=metadata)

    if include_events:
        scan_fn = with_events()
    elif metadata is None:
        scan_fn = grep_scanner("error", name="find_errors")
    else:
        scan_fn = grep_scanner("error", name="find_errors", metadata=metadata)

    monkeypatch.setattr("inspect_scout._util.constants.SPOOL_THRESHOLD_BYTES", 0)
    monkeypatch.setattr(SpooledTranscriptHandle, "messages", observe_messages)
    monkeypatch.setattr(SpooledTranscriptHandle, "load", forbid_load)
    status = scan(
        [scan_fn],
        transcripts=transcripts_from(eval_log_path),
        scans=str(tmp_path / "scans"),
        display="none",
        max_processes=1,
        fail_on_error=True,
    )
    assert status.complete
    assert not status.errors
    expected_metadata = metadata if metadata is not None else not include_events
    assert observed and all(value is expected_metadata for value in observed)
    results = scan_results_df(status.location)
    scanner_name = "with_events" if include_events else "find_errors"
    assert list(results.scanners) == [scanner_name]
    assert results.scanners[scanner_name]["value"].tolist() == [
        2 if include_events else 1
    ]


def test_scan_preserves_custom_loader(tmp_path: Path, eval_log_path: Path) -> None:
    @loader(messages="all")
    def skip_first_message() -> Loader[Transcript]:
        async def load(transcript: Transcript) -> AsyncIterator[Transcript]:
            yield transcript.model_copy(update={"messages": transcript.messages[1:]})

        return load

    @scanner(loader=skip_first_message())
    def after_first_message() -> Scanner[Transcript]:
        return grep_scanner("error")

    status = scan(
        [after_first_message()],
        transcripts=transcripts_from(eval_log_path),
        scans=str(tmp_path / "scans"),
        display="none",
        max_processes=1,
        fail_on_error=True,
    )
    assert status.complete
    assert not status.errors
    results = scan_results_df(status.location)
    assert results.scanners["after_first_message"]["value"].tolist() == [0]
