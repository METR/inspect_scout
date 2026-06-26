"""Type definitions for scanner and loader modules."""

from typing import Sequence, Union

from inspect_ai.event._event import Event
from inspect_ai.model._chat_message import ChatMessage
from typing_extensions import Literal

from .._transcript.types import Transcript
from .cohort_input import Cohort

ScannerInput = Union[
    Transcript,
    Sequence[Transcript],
    ChatMessage,
    Sequence[ChatMessage],
    Event,
    Sequence[Event],
]
"""Per-transcript inputs a scanner can receive (and that are persisted on a
`ResultReport`): a single transcript, its messages/events, or a list thereof.

Cohort scanners receive a `Cohort` instead — see `AnyScannerInput`.
"""

AnyScannerInput = Union[ScannerInput, Cohort]
"""Everything a scanner can accept: the per-transcript `ScannerInput` shapes plus a
`Cohort` (the input to a cohort scanner declared with `@scanner(group_by=...)`).
"""

ScannerInputNames = Literal[
    "transcript", "transcripts", "event", "events", "message", "messages"
]
