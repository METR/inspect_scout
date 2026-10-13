"""Pattern-based transcript scanner using grep-style matching."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from dataclasses import replace
from typing import TypeVar

from .._scanner.result import Reference, Result
from .._scanner.scanner import (
    SCANNER_CONTENT_ATTR,
    SCANNER_NAME_ATTR,
    Scanner,
    mark_streaming_support,
    scanner,
)
from .._transcript.handle import TranscriptHandle
from .._transcript.types import Transcript, TranscriptContent
from ._match import (
    Match,
    compile_pattern,
    find_matches_in_events,
    find_matches_in_messages,
)


@scanner(messages="all", supports_streaming=True)
def grep_scanner(
    pattern: str | list[str] | dict[str, str | list[str]],
    *,
    regex: bool = False,
    ignore_case: bool = True,
    word_boundary: bool = False,
    name: str | None = None,
    metadata: bool | None = None,
) -> Scanner[Transcript]:
    r"""Pattern-based transcript scanner.

    Scans transcript messages and events for text patterns using grep-style
    matching. By default, patterns are treated as literal strings (like grep
    without -E). Set `regex=True` to treat patterns as regular expressions.

    What gets searched depends on what's populated in the transcript:
    - Messages are searched if `transcript.messages` is populated
    - Events are searched if `transcript.events` is populated
    - Control population via `@scanner(messages=..., events=...)`

    Also accepts a `TranscriptHandle`, searching messages and events
    incrementally and retaining matches rather than the full transcript.

    Args:
        pattern: Pattern(s) to search for. Can be:
            - str: Single pattern
            - list[str]: Multiple patterns (OR logic, single aggregated result)
            - dict[str, str | list[str]]: Labeled patterns (returns multiple results,
              one per label)
        regex: If True, treat patterns as regular expressions.
            Default False (literal string matching).
        ignore_case: Case-insensitive matching. Default True (like grep -i).
        word_boundary: Match whole words only (adds \b anchors). Default False.
        name: Scanner name.
            Use this to assign a name when passing `grep_scanner()` directly to `scan()` rather than delegating to it from another scanner.
        metadata: Whether to read the sample's full metadata, target and scores
            from the log body. Defaults to the wrapping scanner's setting
            (True if unset). False keeps the summary values instead. All
            scanners sharing a transcript read must decline metadata to skip
            ingestion. Does not affect matching or results.

    Returns:
        Scanner that returns:
        - Single Result (for str/list input): value=count of matches, explanation=context snippets, references=[M1]/[E1] citations
        - list[Result] (for dict input): one Result per label with its count

    Raises:
        ValueError: If pattern list is empty or dict has no labels.
        PatternError: If regex=True and pattern is an invalid regular expression.

    Examples:
        Simple pattern (messages only):
            grep_scanner("error")

        Multiple patterns (OR logic):
            grep_scanner(["error", "failed", "exception"])

        Labeled patterns (separate results):
            grep_scanner({
                "errors": ["error", "failed"],
                "warnings": ["warning", "caution"],
            })

        With regex:
            grep_scanner(r"https?://\S+", regex=True)

        Search both messages and events:
            @scanner(messages="all", events=["tool", "error"])
            def find_errors() -> Scanner[Transcript]:
                return grep_scanner("error")
    """
    # Validate patterns
    if isinstance(pattern, list) and len(pattern) == 0:
        raise ValueError("Pattern list cannot be empty")
    if isinstance(pattern, dict) and len(pattern) == 0:
        raise ValueError("Pattern dict cannot be empty")

    async def scan(transcript: Transcript | TranscriptHandle) -> Result | list[Result]:
        groups: dict[str | None, str | list[str]] = (
            {label: patterns for label, patterns in pattern.items()}
            if isinstance(pattern, dict)
            else {None: pattern}
        )
        compiled = {
            label: [
                compile_pattern(p, regex, ignore_case, word_boundary)
                for p in ([patterns] if isinstance(patterns, str) else patterns)
            ]
            for label, patterns in groups.items()
        }
        matches: dict[str | None, list[Match]] = {label: [] for label in groups}

        messages = (
            transcript.messages
            if isinstance(transcript, Transcript)
            else transcript.messages()
        )
        async with aclosing(_indexed(messages)) as source:
            async for index, message in source:
                for label, patterns in compiled.items():
                    matches[label].extend(
                        replace(match, index=index)
                        for match in find_matches_in_messages([message], patterns)
                    )

        events = (
            transcript.events
            if isinstance(transcript, Transcript)
            else transcript.events()
        )
        async with aclosing(_indexed(events)) as event_source:
            async for index, event in event_source:
                for label, patterns in compiled.items():
                    matches[label].extend(
                        replace(match, index=index)
                        for match in find_matches_in_events([event], patterns)
                    )

        results: list[Result] = []
        for label, found in matches.items():
            result = _build_result(found)
            result.label = label
            results.append(result)
        return results if isinstance(pattern, dict) else results[0]

    # set name for collection by @scanner if specified
    if name is not None:
        setattr(scan, SCANNER_NAME_ATTR, name)

    if metadata is not None:
        setattr(scan, SCANNER_CONTENT_ATTR, TranscriptContent(metadata=metadata))
    # Preserve streaming when another @scanner factory returns this callable.
    mark_streaming_support(scan, True)
    return scan


def _build_result(matches: list[Match]) -> Result:
    """Build a Result from a list of matches."""
    if not matches:
        return Result(value=0, explanation=None, references=[])

    # Build explanation with context snippets
    explanation_parts: list[str] = []
    references: list[Reference] = []
    seen_ids: set[str] = set()

    for m in matches:
        # Use M for messages, E for events
        prefix = "M" if m.source == "message" else "E"
        cite = f"[{prefix}{m.index}]"
        explanation_parts.append(f"{cite}: {m.context}")

        # Add reference for each unique message/event
        if m.id not in seen_ids:
            references.append(Reference(type=m.source, cite=cite, id=m.id))
            seen_ids.add(m.id)

    return Result(
        value=len(matches),
        explanation="\n".join(explanation_parts),
        references=references,
    )


T = TypeVar("T")


async def _indexed(
    items: list[T] | AsyncIterator[T],
) -> AsyncGenerator[tuple[int, T], None]:
    """Number content and close iteration without closing the shared handle."""
    if isinstance(items, list):
        for index, item in enumerate(items, start=1):
            yield index, item
    else:
        try:
            index = 0
            async for item in items:
                index += 1
                yield index, item
        finally:
            if isinstance(items, AsyncGenerator):
                await items.aclose()
