"""Cohort grouping: turn a corpus of transcripts into groups for cohort scanners.

A *cohort scanner* (declared via `@scanner(group_by=...)` with a
`Sequence[Transcript]` input) analyzes a group of related transcripts together
(e.g. the same task across models, or repeated epochs of one configuration). This
module computes those groups from the transcript index and assigns each a stable
identity used for recording and resumption.
"""

from logging import getLogger
from typing import Sequence, cast

from inspect_ai._util.hash import mm3_hash
from inspect_ai._util.json import to_json_str_safe
from pydantic import JsonValue

from ._scanspec import (
    COHORT_PRESETS,
    CohortMembership,
    CohortSpec,
    GroupDim,
)
from ._transcript.types import TranscriptInfo

logger = getLogger(__name__)


def resolve_group_by(cohort: CohortSpec, scanner_key: str) -> list[GroupDim]:
    """Resolve the effective grouping dimensions for a cohort spec.

    A `preset` takes precedence over an explicit `group_by`.

    Args:
        cohort: Cohort specification.
        scanner_key: Scanner key (for error messages).

    Returns:
        The list of grouping dimensions.

    Raises:
        ValueError: If neither a preset nor `group_by` is specified.
    """
    if cohort.preset is not None:
        return list(COHORT_PRESETS[cohort.preset])
    if cohort.group_by:
        return list(cohort.group_by)
    raise ValueError(
        f"Cohort scanner '{scanner_key}' has no grouping defined. Specify "
        "`group_by` or `preset` on the scanner decorator or in the scan "
        "configuration."
    )


def _dim_value(info: TranscriptInfo, dim: GroupDim) -> JsonValue:
    return cast(JsonValue, getattr(info, dim))


def _cohort_label(key: dict[str, JsonValue]) -> str:
    return " | ".join(f"{dim}={value!r}" for dim, value in key.items())


def _cohort_id(key: dict[str, JsonValue]) -> str:
    """Compute a stable, filesystem-safe cohort id from grouping-dim values.

    The id is derived only from the grouping dimension *values* (not the member
    transcript ids), so it is stable across runs even as membership changes. A
    short hash of the canonical key is appended to guarantee uniqueness even when
    sanitization would otherwise collapse distinct values.
    """
    import re

    slug = "__".join(
        f"{dim}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', str(value))}"
        for dim, value in key.items()
    )
    slug = slug[:96].strip("_") or "cohort"
    digest = mm3_hash(to_json_str_safe(key))[:8]
    return f"{slug}__{digest}"


def members_digest(members: Sequence[str], max_members: int | None) -> str:
    """Hash of the (sorted) member ids and cap, for drift detection."""
    return mm3_hash(to_json_str_safe([sorted(members), max_members]))


def compute_cohorts(
    infos: Sequence[TranscriptInfo],
    cohort: CohortSpec,
    scanner_key: str,
) -> list[CohortMembership]:
    """Group transcripts into cohorts for a cohort scanner.

    Transcripts are grouped by the resolved grouping dimensions; groups smaller
    than `cohort.min_size` are dropped. Within a cohort, members are sorted by
    transcript id (deterministic) and truncated to `cohort.max_members` if set.

    Args:
        infos: The transcript index to group.
        cohort: The cohort specification.
        scanner_key: Scanner key (for ids/labels/errors).

    Returns:
        The resolved cohorts, ordered by `cohort_id`.
    """
    group_by = resolve_group_by(cohort, scanner_key)

    # group transcripts by their dimension-value tuple (preserve key ordering)
    groups: dict[tuple[JsonValue, ...], list[TranscriptInfo]] = {}
    for info in infos:
        key_tuple: tuple[JsonValue, ...] = tuple(
            _dim_value(info, dim) for dim in group_by
        )
        groups.setdefault(key_tuple, []).append(info)

    memberships: list[CohortMembership] = []
    for key_tuple, group_infos in groups.items():
        if len(group_infos) < cohort.min_size:
            continue

        key: dict[str, JsonValue] = {
            str(dim): value for dim, value in zip(group_by, key_tuple, strict=True)
        }
        all_members = sorted(info.transcript_id for info in group_infos)
        total = len(all_members)
        truncated = cohort.max_members is not None and total > cohort.max_members
        members = all_members[: cohort.max_members] if truncated else all_members

        memberships.append(
            CohortMembership(
                cohort_id=_cohort_id(key),
                key=key,
                label=_cohort_label(key),
                members=members,
                members_digest=members_digest(members, cohort.max_members),
                total_members=total,
                truncated=truncated,
                max_members=cohort.max_members,
            )
        )

    memberships.sort(key=lambda m: m.cohort_id)
    return memberships
