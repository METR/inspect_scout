# S3 Log Caching Implementation Plan

## Overview
Cache DataFrame rows per S3 log file using KVStore with etag-based invalidation to avoid slow S3 reads during EvalLogTranscriptsDB construction.

## Implementation Phases

### Phase 1: Create Enhanced resolve_logs Function
**File:** `src/inspect_scout/_transcript/database.py`

- [x] Create `resolve_logs_with_etag()` function that:
  - Copies logic from inspect_ai's `resolve_logs()`
  - Returns `list[tuple[str, str | None]]` where tuple is `(path, etag)`
  - Etag extracted from `FileInfo.etag` returned by `fs.info()`
  - Add TODO comment suggesting inspect_ai could support this natively

### Phase 2: Create S3 Cache Helper Functions
**File:** `src/inspect_scout/_transcript/database.py`

- [x] Add `_get_cached_df_for_s3_log(kvstore, log_path, current_etag) -> pd.DataFrame | None`
  - Check cache, return DataFrame if etag matches, None otherwise

- [x] Add `_put_cached_df_for_s3_log(kvstore, log_path, etag, df)`
  - Serialize df subset as JSON: `{"etag": ..., "records": [...]}`
  - Store in KVStore with log path as key

- [x] Use `is_s3_filename()` for S3 detection

### Phase 3: Modify EvalLogTranscriptsDB Constructor
**File:** `src/inspect_scout/_transcript/database.py` (lines 192-283)

Replace `samples_df()` call with:

- [x] Call `resolve_logs_with_etag()` to get `list[(path, etag)]`
- [x] Separate S3 paths from local paths
- [x] Open KVStore: `inspect_kvstore("scout_s3_log_cache")`
- [x] For each S3 path:
  - Try `_get_cached_df_for_s3_log()`
  - If hit: use cached DataFrame
  - If miss: call `_read_samples_df_serial([log_path], ...)` to get rows, then `_put_cached_df_for_s3_log()`
- [x] For local paths: call `_read_samples_df_serial()` directly
- [x] Concatenate all DataFrames (cached + freshly read)
- [x] Fall back to current behavior on any errors

### Phase 4: Import Additions

- [x] Import `_read_samples_df_serial` from `inspect_ai.analysis._dataframe.samples.table`
- [x] Import `inspect_kvstore` from `inspect_ai._util.kvstore`
- [x] Import `is_s3_filename` from `inspect_ai._util.asyncfiles`
- [x] Import `filesystem` from `inspect_ai._util.file`

## Testing Strategy

- [x] Verified all existing tests pass (642 passed, 6 skipped)
- [ ] Manual test with S3 paths (cache miss, then cache hit) - requires S3 setup
- [ ] Manual test with local paths (no caching)
- [ ] Manual test with mixed S3 + local paths
- [ ] Manual test etag mismatch (cache invalidation)
- [ ] Manual test cache corruption (fallback to direct read)

## Completed Steps

- [x] All imports added successfully
- [x] `resolve_logs_with_etag()` function created
- [x] Cache helper functions `_get_cached_df_for_s3_log()` and `_put_cached_df_for_s3_log()` created
- [x] `EvalLogTranscriptsDB.__init__()` modified to use `_build_transcripts_df()` method
- [x] Linting passed (ruff check)
- [x] Type checking passed (mypy)
- [x] Formatting verified (ruff format)
- [x] All existing tests pass (642 passed, 6 skipped)

## Cache Structure

**Cache Key:** S3 URI as-is (e.g., `s3://bucket/path/to/log.json`)

**Cache Value:** JSON object:
```json
{
  "etag": "abc123...",
  "records": [...DataFrame records as JSON array...]
}
```

**Serialization:** Use `df.to_json(orient='records')`
**Deserialization:** Use `pd.DataFrame.from_records()`

## Notes

- DataFrame has multiple rows per log file (one row per sample/transcript)
- S3 detection via `is_s3_filename(path)` utility
- Cache invalidation based on etag comparison
- Graceful fallback to direct S3 read on cache errors
