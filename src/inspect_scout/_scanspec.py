from datetime import datetime
from typing import Any, Type

from inspect_ai.model._model_config import ModelConfig
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    model_validator,
)
from shortuuid import uuid
from typing_extensions import Literal, NotRequired, Required, TypedDict

from inspect_scout._query.condition import Condition
from inspect_scout._query.condition_sql import condition_as_sql
from inspect_scout._validation.types import ValidationSet

from ._util.constants import DEFAULT_MAX_TRANSCRIPTS

GroupDim = Literal["task_set", "task_id", "task_repeat", "model", "agent", "source_id"]
"""Transcript dimension usable to group transcripts into a cohort.

These map directly to fields on `TranscriptInfo`.
"""

CohortPreset = Literal[
    "same_task_across_models",
    "same_task_in_sample",
    "same_task_across_agents",
]
"""Named cohort grouping presets.

- `same_task_across_models`: group by `(task_set, task_id)` — the same task
  prompt attempted by different models.
- `same_task_in_sample`: group by `(task_set, task_id, model, agent)` — repeated
  epochs (`task_repeat`) of one model+agent configuration.
- `same_task_across_agents`: group by `(task_set, task_id, model)` — the same
  model with different agent scaffolds.
"""

COHORT_PRESETS: dict[CohortPreset, list[GroupDim]] = {
    "same_task_across_models": ["task_set", "task_id"],
    "same_task_in_sample": ["task_set", "task_id", "model", "agent"],
    "same_task_across_agents": ["task_set", "task_id", "model"],
}
"""Mapping of cohort preset names to their grouping dimensions."""


class CohortSpec(BaseModel):
    """Specification of how to group transcripts into cohorts for a cohort scanner.

    A cohort scanner (declared via `@scanner(group_by=...)` with a
    `Sequence[Transcript]` input) analyzes a group of related transcripts
    together. The cohort is defined by holding one or more grouping dimensions
    constant; transcripts that share the same values for those dimensions form a
    cohort. Provide either `group_by` (explicit dimensions) or `preset` (a named
    set of dimensions).
    """

    group_by: list[GroupDim] | None = Field(default=None)
    """Dimensions held constant to form a cohort (e.g. `["task_set", "task_id"]`)."""

    preset: CohortPreset | None = Field(default=None)
    """Named grouping preset (takes precedence over `group_by` when set)."""

    min_size: int = Field(default=2)
    """Minimum number of transcripts for a group to be scanned as a cohort."""

    max_members: int | None = Field(default=None)
    """Maximum transcripts to include per cohort (bounds worker memory and prompt
    size). When a cohort exceeds this, members are truncated (deterministically by
    transcript id) and the result is flagged."""

    model_config = ConfigDict(extra="forbid")


class CohortMembership(BaseModel):
    """A resolved cohort: the concrete set of transcripts in one group."""

    cohort_id: str
    """Stable, filesystem-safe identifier derived from the grouping dimension values."""

    key: dict[str, JsonValue]
    """The grouping dimension values that define this cohort (e.g. `{"task_set": "cybench", "task_id": "crypto-1"}`)."""

    label: str
    """Human-readable cohort label (e.g. `task_set=cybench | task_id=crypto-1`)."""

    members: list[str]
    """Sorted transcript ids belonging to this cohort (after any `max_members` truncation)."""

    members_digest: str
    """Hash of the member ids (and cap), used to detect membership drift on resume."""

    total_members: int = Field(default=0)
    """Number of transcripts matched before any `max_members` truncation."""

    truncated: bool = Field(default=False)
    """Whether `members` was truncated due to `max_members`."""

    max_members: int | None = Field(default=None)
    """The `max_members` cap in effect (folded into `members_digest`)."""

    missing_members: list[str] = Field(default_factory=list)
    """Planned members that could not be read when the cohort was scanned.

    Non-empty only for a degraded (subset) scan; `members`/`members_digest` then
    reflect the members actually scanned, so the cohort re-runs once the missing
    members become available.
    """


class ScannerSpec(BaseModel):
    """Scanner used by scan."""

    name: str
    """Scanner name."""

    version: int = Field(default=0)
    """Scanner version."""

    package_version: str | None = Field(default=None)
    """Scanner package version (if in a package)."""

    file: str | None = Field(default=None)
    """Scanner source file (if not in a package)."""

    params: dict[str, Any] = Field(default_factory=dict)
    """Scanner arguments."""

    cohort: CohortSpec | None = Field(default=None)
    """Cohort grouping for this scanner (set for cohort scanners).

    When present, this scanner is a *cohort scanner*: it is applied once per
    cohort of transcripts (grouped per this spec) rather than once per transcript.
    """


GIT_VERSION_UNKNOWN = "0.0.0-dev.0+unknown"


class ScanRevision(BaseModel):
    """Git revision for scan."""

    type: Literal["git"]
    """Type of revision (currently only "git")"""

    origin: str
    """Revision origin server"""

    version: str = Field(default=GIT_VERSION_UNKNOWN)
    """Revision version (based on tags)."""

    commit: str
    """Revision commit."""


class ScanOptions(BaseModel):
    """Options used for scan."""

    max_transcripts: int = Field(default=DEFAULT_MAX_TRANSCRIPTS)
    """Maximum number of concurrent transcripts (defaults to 25)."""

    max_processes: int | None = Field(default=None)
    """Number of worker processes. Defaults to 4."""

    limit: int | None = Field(default=None)
    """Transcript limit (maximum number of transcripts to read)."""

    shuffle: bool | int | None = Field(default=None)
    """Shuffle order of transcripts."""


class TranscriptField(TypedDict, total=False):
    """Field in transcript data frame."""

    name: Required[str]
    """Field name."""

    type: Required[str]
    """Field type ("integer", "number", "boolean", "string", or "datetime")"""

    tz: NotRequired[str]
    """Timezone (for "datetime" fields)."""


class ScanTranscripts(BaseModel):
    """Transcripts targeted by a scan."""

    type: Literal["eval_log", "database"]
    """Transcripts backing store type ('eval_log' or 'database')."""

    location: str | None = Field(default=None)
    """Location of transcript collection (e.g. database location)."""

    filter: list[str] | None = Field(default=None)
    """Filter (SQL WHERE clauses) applied to transcripts for scan.

    Note that `transcript_ids` already reflects the filter so it need not be re-applied.
    """

    transcript_ids: dict[str, str | None] = Field(default_factory=dict)
    """IDs of transcripts mapped to optional location hints.

    The location value depends on the backing store:
    - For parquet databases: the parquet filename containing the transcript
    - For eval logs: the log file path containing the transcript
    - For other stores (e.g., relational DB): may be None if ID alone suffices
    """

    # deprecated fields

    count: int = Field(default=0)
    """Trancript count (deprecated)."""

    fields: list[TranscriptField] | None = Field(default=None)
    """Data types of transcripts fields (deprecated)"""

    data: str | None = Field(default=None)
    """Transcript data as a csv (deprecated)"""

    # migrate 'conditions' to 'filter'
    @model_validator(mode="before")
    @classmethod
    def convert_results_to_scans(cls: Type["ScanTranscripts"], values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        if values.get("conditions", None) is not None:
            values["filter"] = [
                condition_as_sql(Condition.model_validate(c), "filter")
                for c in values["conditions"]
            ]

        return values


class Worklist(BaseModel):
    """List of transcript ids to process for a scanner."""

    scanner: str
    """Scanner name."""

    transcripts: list[str]
    """List of transcript ids."""


class ScanSpec(BaseModel):
    """Scan specification (scanners, transcripts, config)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    scan_id: str = Field(default_factory=uuid)
    """Globally unique id for scan job."""

    scan_name: str
    """Scan job name."""

    scan_file: str | None = Field(default=None)
    """Source file for scan job."""

    scan_args: dict[str, Any] | None = Field(default=None)
    """Arguments used for invoking the scan job."""

    timestamp: datetime = Field(default_factory=datetime.now)
    """Time created."""

    tags: list[str] | None = Field(default=None)
    """Tags associated with the scan."""

    metadata: dict[str, Any] | None = Field(default=None)
    """Additional scan metadata."""

    model: ModelConfig | None = Field(default=None)
    """Model used for eval."""

    model_roles: dict[str, ModelConfig] | None = Field(default=None)
    """Model roles."""

    revision: ScanRevision | None = Field(default=None)
    """Source revision of scan."""

    packages: dict[str, str] = Field(default_factory=dict)
    """Package versions for scan."""

    options: ScanOptions = Field(default_factory=ScanOptions)
    """Scan options."""

    transcripts: ScanTranscripts | None = Field(default=None)
    """Transcripts to scan."""

    scanners: dict[str, ScannerSpec]
    """Scanners to apply to transcripts."""

    worklist: list[Worklist] | None = Field(default=None)
    """Transcript ids to process for each scanner (defaults to processing all transcripts)."""

    cohorts: dict[str, list[CohortMembership]] | None = Field(default=None)
    """Resolved cohorts for each cohort scanner (scanner key -> cohorts).

    Frozen at scan creation from the transcript snapshot so a resumed scan
    rebuilds the same groups from the spec.
    """

    validation: dict[str, ValidationSet] | None = Field(default=None)
    """Validation cases to apply for scanners."""

    @field_serializer("timestamp")
    def serialize_created(self, timestamp: datetime) -> str:
        return timestamp.astimezone().isoformat()
