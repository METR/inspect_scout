"""The `Cohort` input passed to a cohort scanner.

A cohort scanner (`@scanner(group_by=...)`) receives a single `Cohort` — a group of
related transcripts plus (later) the outputs of upstream analyses for each member. One
input type serves every cohort kind (raw, depends-on-summaries, meta-cohort), so the
single-positional `Scanner` protocol holds and the type composes.

The scanner body is a pure function of the `Cohort` it is handed: the executor reads
member content and upstream results from the store and assembles the `Cohort` before
calling the scanner, so the scanner never touches the store directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, TypeVar

from pydantic import BaseModel, JsonValue

from .._transcript.types import Transcript

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class CohortMember:
    """One member of a cohort: its transcript and any upstream analyses for it."""

    subject_id: str
    """Member identity (transcript id, or cohort id for a meta-cohort member)."""

    transcript: Transcript | None
    """The member's transcript (None when the scanner declared no content to read)."""

    _upstream: Mapping[str, Any] = field(default_factory=dict)
    """Upstream node id -> this member's result for that node (populated when the
    cohort scanner declares `depends_on`; empty until dependencies ship)."""

    def upstream(self, node_id: str, *, as_type: type[M] | None = None) -> Any:
        """Return this member's result from an upstream node.

        Args:
            node_id: The upstream node (scanner) id this cohort depends on.
            as_type: Optional model to parse the result value into (used once
                cohort dependencies ship).

        Raises:
            KeyError: If there is no upstream result for `node_id` (e.g. this scan
                declared no dependencies).
        """
        if node_id not in self._upstream:
            raise KeyError(
                f"no upstream result '{node_id}' for member '{self.subject_id}' "
                "(cohort dependencies are not available in this scan)"
            )
        value = self._upstream[node_id]
        return as_type.model_validate(value) if as_type is not None else value


class UpstreamResults:
    """Cohort-wide accessor for upstream analyses, aligned to `Cohort.members`."""

    def __init__(self, by_node: Mapping[str, Sequence[Any]] | None = None) -> None:
        self._by_node: dict[str, Sequence[Any]] = dict(by_node or {})

    def results(self, node_id: str, *, as_type: type[M] | None = None) -> Sequence[Any]:
        """Member-aligned results from an upstream node (empty when no dependency)."""
        values = self._by_node.get(node_id, [])
        if as_type is not None:
            return [as_type.model_validate(v) for v in values]
        return values

    @property
    def empty(self) -> bool:
        """Whether this cohort has any upstream results at all."""
        return not self._by_node


@dataclass(frozen=True)
class Cohort:
    """A group of related transcripts handed to a cohort scanner."""

    key: dict[str, JsonValue]
    """The grouping-dimension values that define this cohort (e.g. `{"task_set": ..., "task_id": ...}`)."""

    cohort_id: str
    """Stable, filesystem-safe identifier derived from `key`."""

    label: str
    """Human-readable cohort label."""

    members: Sequence[CohortMember]
    """The cohort's members (sorted by subject id)."""

    members_digest: str
    """Hash of the member ids actually present, for drift detection."""

    upstream: UpstreamResults = field(default_factory=UpstreamResults)
    """Cohort-wide upstream analyses (empty until cohort dependencies ship)."""

    @property
    def transcripts(self) -> list[Transcript]:
        """The members' transcripts (convenience for raw-cohort scanners)."""
        return [m.transcript for m in self.members if m.transcript is not None]
