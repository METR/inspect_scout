"""Execution of cohort scanners (phase B of a scan).

Cohort scanners analyze a *group* of transcripts together. Unlike the
per-transcript pipeline (which fans one transcript out to many scanners), a
cohort job is many-transcripts to one-scanner, so it runs as a separate phase:

1. `compute_cohort_plan` groups the transcript index into cohorts (freezing the
   membership into the scan spec so a resumed scan rebuilds the same groups).
2. `scan_cohorts` runs each cohort scanner once per cohort, reading the member
   transcripts lazily, and records one result row per cohort.

This phase deliberately reuses the existing recorder (cohort results live under
the cohort scanner's own key) and runs single-process with bounded concurrency.
"""

import traceback
from logging import getLogger
from typing import Any, Callable

import anyio
from inspect_ai._util.error import PrerequisiteError
from inspect_ai._util.json import jsonable_python
from inspect_ai.model._model import init_model_usage, model_usage
from inspect_ai.util import span

from ._cohort import compute_cohorts, members_digest
from ._recorder.recorder import ScanRecorder
from ._scancontext import ScanContext
from ._scanner.cohort_input import Cohort, CohortMember, UpstreamResults
from ._scanner.result import Error, Result, ResultReport, as_resultset
from ._scanner.scanner import Scanner, config_for_scanner
from ._scanspec import CohortMembership, ScanSpec, ScanTranscripts
from ._transcript.transcripts import Transcripts, TranscriptsReader
from ._transcript.types import TranscriptContent, TranscriptInfo
from ._util.attachments import resolve_event_attachments
from ._util.refusal import RefusalError

logger = getLogger(__name__)


async def compute_cohort_plan(
    tr: TranscriptsReader,
    cohort_scanners: dict[str, Scanner[Any]],
    spec: ScanSpec,
) -> dict[str, TranscriptInfo]:
    """Group the transcript index into cohorts for each cohort scanner.

    Mutates `spec.cohorts` with the resolved plan (so a resumed scan rebuilds the
    same groups) and returns an index mapping transcript id -> `TranscriptInfo`
    for reading member content during the cohort scan.
    """
    infos = [info async for info in tr.index()]
    cohort_index = {info.transcript_id: info for info in infos}

    plan: dict[str, list[CohortMembership]] = {}
    for scanner_key in cohort_scanners:
        cohort_spec = spec.scanners[scanner_key].cohort
        if cohort_spec is None:
            raise PrerequisiteError(
                f"Cohort scanner '{scanner_key}' is missing its cohort grouping."
            )
        plan[scanner_key] = compute_cohorts(infos, cohort_spec, scanner_key)

    spec.cohorts = plan
    return cohort_index


async def scan_cohorts(
    *,
    scan: ScanContext,
    recorder: ScanRecorder,
    transcripts: Transcripts,
    snapshot: ScanTranscripts,
    cohort_index: dict[str, TranscriptInfo],
    cohort_scanners: dict[str, Scanner[Any]],
    max_concurrency: int,
    fail_on_error: bool,
    on_complete: Callable[[str, CohortMembership, bool], None],
) -> None:
    """Run each cohort scanner once per cohort, recording one result per cohort.

    Opens its own transcript reader (independent of the per-transcript phase's
    reader lifecycle) for reading member content.

    Args:
        scan: Scan context (its `spec.cohorts` holds the frozen plan).
        recorder: Recorder for cohort results.
        transcripts: Transcript collection to read members from.
        snapshot: Snapshot of the transcript collection (reader hint).
        cohort_index: Map of transcript id -> info for reading members.
        cohort_scanners: The cohort scanners to run (key -> scanner).
        max_concurrency: Maximum cohorts scanned concurrently.
        fail_on_error: Re-raise scanner exceptions instead of recording them.
        on_complete: Callback invoked per cohort with (scanner_key, membership,
            recorded) where `recorded` is True if the cohort was already recorded
            (skipped).
    """
    plan = scan.spec.cohorts or {}
    limiter = anyio.CapacityLimiter(max(1, max_concurrency))

    async def process(
        tr: TranscriptsReader,
        scanner_key: str,
        scanner: Scanner[Any],
        content: TranscriptContent,
        membership: CohortMembership,
    ) -> None:
        async with limiter:
            if await recorder.is_cohort_recorded(membership, scanner_key):
                on_complete(scanner_key, membership, True)
                return
            report, effective = await _scan_one_cohort(
                tr=tr,
                cohort_index=cohort_index,
                content=content,
                scanner_key=scanner_key,
                scanner=scanner,
                membership=membership,
                fail_on_error=fail_on_error,
            )
            await recorder.record_cohort(effective, scanner_key, [report], None)
            on_complete(scanner_key, membership, False)

    async with transcripts.reader(snapshot) as tr:
        async with anyio.create_task_group() as tg:
            for scanner_key, scanner in cohort_scanners.items():
                content = config_for_scanner(scanner).content
                for membership in plan.get(scanner_key, []):
                    tg.start_soon(
                        process, tr, scanner_key, scanner, content, membership
                    )


async def _scan_one_cohort(
    *,
    tr: TranscriptsReader,
    cohort_index: dict[str, TranscriptInfo],
    content: TranscriptContent,
    scanner_key: str,
    scanner: Scanner[Any],
    membership: CohortMembership,
    fail_on_error: bool,
) -> tuple[ResultReport, CohortMembership]:
    """Run a cohort scanner over one cohort.

    Returns the result report plus the *effective* membership actually scanned:
    when some planned members can't be read (degraded run), the effective
    membership records the members actually used and an honest `members_digest`
    (+ `missing_members`), so the cohort re-runs once the missing members arrive
    rather than being cached as complete.
    """
    from inspect_ai.log._transcript import Transcript as InspectTranscript
    from inspect_ai.log._transcript import init_transcript

    # reset model usage tracking for this cohort scan
    init_model_usage(initial_usage={})

    inspect_transcript = InspectTranscript()
    init_transcript(inspect_transcript)

    error: Error | None = None
    final_result: Result | None = None
    cohort_members: list[CohortMember] = []
    present_ids: list[str] = []
    missing_ids: list[str] = []

    try:
        # read member content lazily (one cohort at a time)
        for transcript_id in membership.members:
            info = cohort_index.get(transcript_id)
            if info is None:
                missing_ids.append(transcript_id)
                continue
            transcript = await tr.read(info, content)
            cohort_members.append(
                CohortMember(subject_id=transcript_id, transcript=transcript)
            )
            present_ids.append(transcript_id)

        if not cohort_members:
            raise RuntimeError(f"cohort '{membership.label}' has no readable members")

        cohort = Cohort(
            key=membership.key,
            cohort_id=membership.cohort_id,
            label=membership.label,
            members=cohort_members,
            members_digest=members_digest(present_ids, membership.max_members),
            upstream=UpstreamResults(),
        )

        async with span("scan"):
            result = await scanner(cohort)

        final_result = as_resultset(result) if isinstance(result, list) else result

    except PrerequisiteError:
        raise
    except Exception as ex:  # pylint: disable=W0718
        if fail_on_error:
            raise
        error = Error(
            transcript_id=None,
            cohort_id=membership.cohort_id,
            scanner=scanner_key,
            error=str(ex),
            traceback=traceback.format_exc(),
            refusal=isinstance(ex, RefusalError),
        )

    # effective membership: honest digest over the members actually scanned
    if missing_ids and present_ids:
        effective = membership.model_copy(
            update={
                "members": present_ids,
                "members_digest": members_digest(present_ids, membership.max_members),
                "missing_members": missing_ids,
            }
        )
    else:
        effective = membership

    report = ResultReport(
        input_type="transcripts",
        input_ids=present_ids or list(membership.members),
        input=[m.transcript for m in cohort_members if m.transcript is not None],
        result=final_result,
        validation=None,
        error=error,
        events=jsonable_python(resolve_event_attachments(inspect_transcript)),
        model_usage=model_usage(),
    )
    return report, effective
