"""End-to-end tests for cohort scanners (scanners over a group of transcripts)."""

import asyncio
from pathlib import Path

import pytest
from inspect_ai.model._chat_message import ChatMessageUser
from inspect_scout import (
    Cohort,
    Result,
    Scanner,
    scan,
    scanner,
    transcripts_db,
    transcripts_from,
)
from inspect_scout._scanjob import ScanJob
from inspect_scout._scanner.scorer import as_scorer
from inspect_scout._scanresults import scan_results_df
from inspect_scout._scanspec import CohortSpec
from inspect_scout._transcript.types import Transcript


def _make_transcript(task_id: str, model: str, epoch: int, success: bool) -> Transcript:
    return Transcript(
        transcript_id=f"{task_id}-{model}-{epoch}",
        source_type="test",
        source_id=f"src-{model}",
        task_set="bench",
        task_id=task_id,
        task_repeat=epoch,
        model=model,
        agent="react",
        success=success,
        metadata={},
        messages=[ChatMessageUser(content=f"task {task_id} on {model}")],
        events=[],
    )


def _seed_db(db_path: Path) -> None:
    # 2 tasks x 3 models x 1 epoch = 6 transcripts
    transcripts = [
        _make_transcript(task_id, model, 0, success=(model == "good"))
        for task_id in ("t1", "t2")
        for model in ("good", "bad", "ok")
    ]

    async def insert() -> None:
        async with transcripts_db(str(db_path)) as db:
            await db.insert(transcripts)

    asyncio.run(insert())


@scanner(name="cohort_summary", group_by=["task_set", "task_id"], messages="all")
def cohort_summary_factory() -> Scanner[Cohort]:
    """Cohort scanner that summarizes a group of transcripts for one task."""

    async def scan_cohort(cohort: Cohort) -> Result:
        transcripts = cohort.transcripts
        # every member must carry its messages (content="all")
        assert all(len(t.messages) > 0 for t in transcripts)
        assert cohort.upstream.empty  # no dependencies in this scan
        return Result(
            value=len(cohort.members),
            explanation=f"{len(cohort.members)} attempts",
            metadata={"models": sorted(t.model or "?" for t in transcripts)},
        )

    return scan_cohort


@scanner(name="per_transcript", messages="all")
def per_transcript_factory() -> Scanner[Transcript]:
    async def scan_one(transcript: Transcript) -> Result:
        return Result(value=transcript.transcript_id is not None)

    return scan_one


def test_cohort_scan_groups_by_task(tmp_path: Path) -> None:
    """A cohort scanner produces one result per group (here: per task_id)."""
    db_path = tmp_path / "db"
    scans_path = tmp_path / "scans"
    db_path.mkdir()
    scans_path.mkdir()
    _seed_db(db_path)

    status = scan(
        scanners=[cohort_summary_factory()],
        transcripts=transcripts_from(str(db_path)),
        scans=str(scans_path),
        max_processes=1,
        display="none",
    )

    assert status.complete, status
    df = scan_results_df(status.location, scanner="cohort_summary").scanners[
        "cohort_summary"
    ]

    # two task_ids -> two cohorts
    assert len(df) == 2
    # each cohort has the three models
    assert sorted(int(v) for v in df["value"].tolist()) == [3, 3]
    assert set(df["cohort_size"].tolist()) == {3}
    # cohort identity columns are present
    for col in ("cohort_id", "cohort_label", "cohort_members", "cohort_members_digest"):
        assert col in df.columns
    # input_type marks a cohort result
    assert set(df["input_type"].tolist()) == {"transcripts"}


def test_cohort_and_per_transcript_in_one_scan(tmp_path: Path) -> None:
    """Per-transcript and cohort scanners coexist in a single scan."""
    db_path = tmp_path / "db"
    scans_path = tmp_path / "scans"
    db_path.mkdir()
    scans_path.mkdir()
    _seed_db(db_path)

    status = scan(
        scanners=[per_transcript_factory(), cohort_summary_factory()],
        transcripts=transcripts_from(str(db_path)),
        scans=str(scans_path),
        max_processes=1,
        display="none",
    )

    assert status.complete, status
    results = scan_results_df(status.location)
    # per-transcript scanner: one row per transcript (6)
    assert len(results.scanners["per_transcript"]) == 6
    # cohort scanner: one row per task (2)
    assert len(results.scanners["cohort_summary"]) == 2


def test_cohort_scope_override_runs_one_scanner_at_two_scopes(tmp_path: Path) -> None:
    """A per-scanner cohort override lets one scanner run at differing scopes."""
    db_path = tmp_path / "db"
    scans_path = tmp_path / "scans"
    db_path.mkdir()
    scans_path.mkdir()
    _seed_db(db_path)

    # same scanner under two keys; one keeps the decorator default
    # (group_by=[task_set, task_id] -> 2 cohorts of 3), the other overrides to
    # group_by=[task_set, task_id, model] -> 6 cohorts of 1 (min_size=1).
    job = ScanJob(
        transcripts=transcripts_from(str(db_path)),
        scanners={
            "by_task": cohort_summary_factory(),
            "by_task_model": cohort_summary_factory(),
        },
        cohort={
            "by_task_model": CohortSpec(
                group_by=["task_set", "task_id", "model"], min_size=1
            )
        },
        scans=str(scans_path),
    )
    status = scan(job, max_processes=1, display="none")

    assert status.complete, status
    results = scan_results_df(status.location)
    assert len(results.scanners["by_task"]) == 2
    assert len(results.scanners["by_task_model"]) == 6


def test_cohort_scanner_rejected_as_scorer() -> None:
    """Cohort scanners cannot be converted to per-sample Inspect scorers."""
    with pytest.raises(ValueError, match="Cohort scanners"):
        as_scorer(cohort_summary_factory())  # type: ignore[arg-type]


def test_cohort_input_shape() -> None:
    """The `Cohort` input exposes members, transcripts, and an upstream accessor."""
    from inspect_scout import Cohort, CohortMember, UpstreamResults

    t = Transcript(transcript_id="t1", model="m", messages=[])
    cohort = Cohort(
        key={"task_id": "q1"},
        cohort_id="c1",
        label="q1",
        members=[CohortMember(subject_id="t1", transcript=t)],
        members_digest="d",
        upstream=UpstreamResults(),
    )
    assert [m.subject_id for m in cohort.members] == ["t1"]
    assert cohort.transcripts == [t]
    assert cohort.upstream.empty
    assert cohort.upstream.results("summarize") == []
    with pytest.raises(KeyError):
        cohort.members[0].upstream("summarize")


def test_cohort_degraded_membership_rescans_when_member_arrives(
    tmp_path: Path,
) -> None:
    """Degraded cohorts re-run when the missing member arrives (B4 honest digest).

    A degraded (subset) cohort records its actual members digest, so the full
    cohort is not considered recorded and re-runs once the missing member exists.
    """
    from inspect_scout._cohort import members_digest
    from inspect_scout._recorder.factory import scan_recorder_for_location
    from inspect_scout._scanner.result import ResultReport
    from inspect_scout._scanspec import CohortMembership, ScannerSpec, ScanSpec

    scans_dir = tmp_path / "scans"
    scans_dir.mkdir()
    spec = ScanSpec(
        scan_name="degraded-test",
        scanners={"rc": ScannerSpec(name="rc", cohort=CohortSpec(group_by=["task_id"]))},
    )
    full = CohortMembership(
        cohort_id="task_id-q1__x",
        key={"task_id": "q1"},
        label="task_id=q1",
        members=["a", "b", "c"],
        members_digest=members_digest(["a", "b", "c"], None),
        total_members=3,
    )
    # what a degraded run (member "c" unreadable) would record
    degraded = full.model_copy(
        update={
            "members": ["a", "b"],
            "members_digest": members_digest(["a", "b"], None),
            "missing_members": ["c"],
        }
    )
    report = ResultReport(
        input_type="transcripts",
        input_ids=["a", "b"],
        input=[],
        result=Result(value=2),
        validation=None,
        error=None,
        events=[],
        model_usage={},
    )

    async def run() -> None:
        recorder = scan_recorder_for_location(str(scans_dir))
        await recorder.init(spec, str(scans_dir))
        await recorder.record_cohort(degraded, "rc", [report], None)
        # the degraded cohort is recorded as itself
        assert await recorder.is_cohort_recorded(degraded, "rc")
        # but the FULL cohort (member "c" now present) is not -> re-runs
        assert not await recorder.is_cohort_recorded(full, "rc")

    asyncio.run(run())


def test_cohort_resume_skips_recorded_and_rescans_on_drift(tmp_path: Path) -> None:
    """is_cohort_recorded skips recorded cohorts and re-scans drifted membership."""
    from inspect_scout._recorder.factory import scan_recorder_for_location
    from inspect_scout._scanner.result import ResultReport
    from inspect_scout._scanspec import CohortMembership, ScannerSpec, ScanSpec

    scans_dir = tmp_path / "scans"
    scans_dir.mkdir()

    spec = ScanSpec(
        scan_name="resume-test",
        scanners={
            "rc": ScannerSpec(
                name="rc", cohort=CohortSpec(group_by=["task_set", "task_id"])
            )
        },
    )
    membership = CohortMembership(
        cohort_id="task_set-bench__task_id-t1__abcd",
        key={"task_set": "bench", "task_id": "t1"},
        label="task_set=bench | task_id=t1",
        members=["m1", "m2"],
        members_digest="digest-1",
        total_members=2,
    )
    report = ResultReport(
        input_type="transcripts",
        input_ids=["m1", "m2"],
        input=[],
        result=Result(value=2),
        validation=None,
        error=None,
        events=[],
        model_usage={},
    )

    async def run() -> None:
        recorder = scan_recorder_for_location(str(scans_dir))
        await recorder.init(spec, str(scans_dir))

        # not recorded yet
        assert not await recorder.is_cohort_recorded(membership, "rc")
        # record, then it is recognized as recorded
        await recorder.record_cohort(membership, "rc", [report], None)
        assert await recorder.is_cohort_recorded(membership, "rc")
        # a membership whose digest differs (drift) forces a re-scan
        drifted = membership.model_copy(update={"members_digest": "digest-2"})
        assert not await recorder.is_cohort_recorded(drifted, "rc")

    asyncio.run(run())
