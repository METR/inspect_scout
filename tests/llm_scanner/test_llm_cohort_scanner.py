"""Tests for the cohort comparison renderer and the llm_cohort_scanner."""

import asyncio
import json
from pathlib import Path

from inspect_ai.model import ModelOutput
from inspect_ai.model._chat_message import ChatMessageAssistant, ChatMessageUser
from inspect_scout import (
    llm_cohort_scanner,
    scan,
    transcripts_as_str,
    transcripts_db,
    transcripts_from,
)
from inspect_scout._scanresults import scan_results_df
from inspect_scout._transcript.types import Transcript


def test_transcripts_as_str_namespaces_and_extracts_references() -> None:
    t1 = Transcript(
        transcript_id="t1",
        model="good",
        messages=[
            ChatMessageUser(content="solve it", id="u1"),
            ChatMessageAssistant(content="done", id="a1"),
        ],
    )
    t2 = Transcript(
        transcript_id="t2",
        model="bad",
        messages=[ChatMessageUser(content="solve it", id="u2")],
    )

    body, extract = asyncio.run(transcripts_as_str([t1, t2]))

    # each member rendered with a namespaced header and message labels
    assert "Transcript T1" in body
    assert "Transcript T2" in body
    assert "[T1:M1]" in body and "[T1:M2]" in body and "[T2:M1]" in body

    # citations resolve to the right (transcript_id, message id)
    refs = extract("compare [T1:M2] with [T2:M1]")
    by_tid = {r.transcript_id: r for r in refs}
    assert by_tid["t1"].id == "a1"
    assert by_tid["t1"].cite == "[T1:M2]"
    assert by_tid["t2"].id == "u2"


def _make_transcript(task_id: str, model: str) -> Transcript:
    return Transcript(
        transcript_id=f"{task_id}-{model}",
        source_type="test",
        task_set="bench",
        task_id=task_id,
        model=model,
        success=(model == "good"),
        messages=[
            ChatMessageUser(content=f"task {task_id}", id=f"{task_id}-{model}-u"),
            ChatMessageAssistant(content="attempt", id=f"{task_id}-{model}-a"),
        ],
    )


def test_llm_cohort_scanner_e2e(tmp_path: Path) -> None:
    db_path = tmp_path / "db"
    scans_path = tmp_path / "scans"
    db_path.mkdir()
    scans_path.mkdir()

    transcripts = [
        _make_transcript(task_id, model)
        for task_id in ("t1", "t2")
        for model in ("good", "bad")
    ]

    async def insert() -> None:
        async with transcripts_db(str(db_path)) as db:
            await db.insert(transcripts)

    asyncio.run(insert())

    # two cohorts (t1, t2) -> two model calls
    mock_responses = [
        ModelOutput.from_content(
            model="mockllm",
            content="The good model in [T1:M2] succeeded where [T2:M1] stalled.\n\nANSWER: the good model handled it",
        ),
        ModelOutput.from_content(
            model="mockllm",
            content="Divergence at [T1:M2] vs [T2:M2].\n\nANSWER: differing tool use",
        ),
    ]

    status = scan(
        scanners=[
            llm_cohort_scanner(question="Why did some attempts fail?", name="rc")
        ],
        transcripts=transcripts_from(str(db_path)),
        scans=str(scans_path),
        max_processes=1,
        model="mockllm/model",
        model_args={"custom_outputs": mock_responses},
        display="none",
    )

    assert status.complete, status
    df = scan_results_df(status.location, scanner="rc").scanners["rc"]

    # one row per cohort (task)
    assert len(df) == 2
    assert set(df["input_type"].tolist()) == {"transcripts"}
    assert set(df["cohort_size"].tolist()) == {2}

    # at least one row carries cross-transcript references with a transcript_id
    found_ref_with_tid = False
    for refs_json in df["message_references"].tolist():
        for ref in json.loads(refs_json):
            if ref.get("transcript_id"):
                found_ref_with_tid = True
    assert found_ref_with_tid, "expected cohort references to carry transcript_id"
