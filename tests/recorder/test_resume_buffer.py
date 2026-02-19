"""Tests for FileRecorder resume with existing results."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from inspect_scout._recorder.buffer import RecorderBuffer
from inspect_scout._recorder.file import FileRecorder
from inspect_scout._scanspec import ScanSpec


def _minimal_scan_spec(scan_id: str, scanners: list[str]) -> ScanSpec:
    return ScanSpec(
        scan_id=scan_id,
        scan_name="test",
        scanners={
            name: {  # type: ignore[dict-item]
                "name": name,
            }
            for name in scanners
        },
    )


def _make_parquet_table(
    transcript_ids: list[str],
    scan_errors: list[str | None],
    scan_id: str = "test-scan",
) -> pa.Table:
    """Create a minimal parquet table matching the recorder schema."""
    n = len(transcript_ids)
    return pa.table(
        {
            "transcript_id": transcript_ids,
            "scan_id": [scan_id] * n,
            "scanner_key": ["test_scanner"] * n,
            "scanner_name": ["test_scanner"] * n,
            "value": ["1"] * n,
            "value_type": ["number"] * n,
            "scan_error": scan_errors,
            "timestamp": ["2025-01-01T00:00:00Z"] * n,
        }
    )


@pytest.fixture
def scan_dir(tmp_path: Path) -> Path:
    """Create a scan directory with a _scan.json spec."""
    scan_id = "test-scan-id"
    d = tmp_path / f"scan_id={scan_id}"
    d.mkdir()

    spec = _minimal_scan_spec(scan_id, ["test_scanner"])
    (d / "_scan.json").write_text(spec.model_dump_json())

    return d


@pytest.fixture
def monkeypatch_buffer_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make RecorderBuffer use a temp directory for buffers."""
    buffer_base = tmp_path / "buffers"
    buffer_base.mkdir()

    from inspect_ai._util.hash import mm3_hash
    from inspect_scout._util.path import normalize_for_hashing

    @staticmethod  # type: ignore[misc]
    def patched_buffer_dir(scan_location: str) -> Path:
        normalized = normalize_for_hashing(scan_location)
        return buffer_base / f"{mm3_hash(normalized)}"

    monkeypatch.setattr(RecorderBuffer, "buffer_dir", patched_buffer_dir)
    return buffer_base


@pytest.mark.asyncio
async def test_resume_skips_completed_transcripts(
    scan_dir: Path,
    monkeypatch_buffer_dir: Path,
) -> None:
    """When resuming, transcripts that completed successfully are marked as recorded."""
    table = _make_parquet_table(
        transcript_ids=["tid-1", "tid-2", "tid-3"],
        scan_errors=[None, None, None],
    )
    pq.write_table(table, str(scan_dir / "test_scanner.parquet"))

    recorder = FileRecorder()
    await recorder.resume(scan_dir.as_posix())

    assert await recorder.is_recorded("tid-1", "test_scanner")
    assert await recorder.is_recorded("tid-2", "test_scanner")
    assert await recorder.is_recorded("tid-3", "test_scanner")


@pytest.mark.asyncio
async def test_resume_retries_errored_transcripts(
    scan_dir: Path,
    monkeypatch_buffer_dir: Path,
) -> None:
    """Transcripts with scan_error are NOT marked as recorded (will be retried)."""
    table = _make_parquet_table(
        transcript_ids=["tid-ok", "tid-err"],
        scan_errors=[None, "RuntimeError: random failure"],
    )
    pq.write_table(table, str(scan_dir / "test_scanner.parquet"))

    recorder = FileRecorder()
    await recorder.resume(scan_dir.as_posix())

    assert await recorder.is_recorded("tid-ok", "test_scanner")
    assert not await recorder.is_recorded("tid-err", "test_scanner")


@pytest.mark.asyncio
async def test_resume_unknown_transcript_not_recorded(
    scan_dir: Path,
    monkeypatch_buffer_dir: Path,
) -> None:
    """Transcripts not in results are not marked as recorded."""
    table = _make_parquet_table(
        transcript_ids=["tid-1"],
        scan_errors=[None],
    )
    pq.write_table(table, str(scan_dir / "test_scanner.parquet"))

    recorder = FileRecorder()
    await recorder.resume(scan_dir.as_posix())

    assert not await recorder.is_recorded("tid-unknown", "test_scanner")


@pytest.mark.asyncio
async def test_resume_multiple_scanners(
    tmp_path: Path,
    monkeypatch_buffer_dir: Path,
) -> None:
    """Resume index handles multiple scanners correctly."""
    scan_id = "multi-scanner"
    d = tmp_path / f"scan_id={scan_id}"
    d.mkdir()

    spec = _minimal_scan_spec(scan_id, ["scanner_a", "scanner_b"])
    (d / "_scan.json").write_text(spec.model_dump_json())

    table_a = _make_parquet_table(
        transcript_ids=["t1", "t2"],
        scan_errors=[None, None],
    )
    table_b = _make_parquet_table(
        transcript_ids=["t1", "t2"],
        scan_errors=[None, "error"],
    )
    pq.write_table(table_a, str(d / "scanner_a.parquet"))
    pq.write_table(table_b, str(d / "scanner_b.parquet"))

    recorder = FileRecorder()
    await recorder.resume(d.as_posix())

    assert await recorder.is_recorded("t1", "scanner_a")
    assert await recorder.is_recorded("t2", "scanner_a")
    assert await recorder.is_recorded("t1", "scanner_b")
    assert not await recorder.is_recorded("t2", "scanner_b")


@pytest.mark.asyncio
async def test_resume_no_parquets(
    scan_dir: Path,
    monkeypatch_buffer_dir: Path,
) -> None:
    """Resume works when there are no existing parquet files."""
    recorder = FileRecorder()
    await recorder.resume(scan_dir.as_posix())

    assert not await recorder.is_recorded("anything", "test_scanner")
