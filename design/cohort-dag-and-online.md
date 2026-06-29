# Cohort scanning, the analysis DAG, and online execution

Status: design (foundation shipped as offline cohort scanners; the rest is a planned PR stack).

## Goal

Let users define, in human-readable form, an arbitrary graph of analyses over eval
transcripts — per-transcript analyses (e.g. summaries) and **cohort** analyses that
compare *groups* of transcripts (same task across models, across epochs, across
agents, any combination) — where cohort analyses may **depend on** other analyses,
and run the whole graph either **offline** (batch over saved logs) or **online**
(inline during an eval), with identical results.

Design qualities we are optimizing for: clean abstractions, testable, modular,
fully flexible grouping, human-readable & human-definable, symmetric with the
existing online *single-transcript* scanner, and durable.

## The core idea: separate three concerns

Everything below falls out of splitting the problem into three independent pieces:

1. **WHAT** an analysis is — a `Scanner` (per-transcript or cohort). *(exists today)*
2. **WHAT it reads** — its inputs: raw transcript content and/or the outputs of
   upstream analyses, read from a shared **store**. *(the dependency model)*
3. **WHEN/WHERE it runs** — a **driver**: offline batch, online (eval hook), or a
   watcher. *(the trigger)*

The **store** is the seam (collection point), the **DAG** is the dependency model,
and the **driver** is the trigger. Each can be built and tested in isolation. The
single most important invariant: *a node is a pure function of its inputs read from
the store; who fills the store and when is just a driver.* This is already true for
single-transcript scanners (offline `scan()` vs online `eval(scanner=...)` run the
same `Scanner[Transcript]`); we extend the same symmetry to cohorts.

## Abstractions

### Subject — the thing a result is keyed by
A result is keyed by a *subject*: a **transcript** (`id = transcript_id`) or a
**cohort** (`id = cohort_id`). `cohort_id` derives only from grouping-dimension
*values* (so it is identical no matter which driver assembled the cohort).

### Scanner node
A scanner is a DAG node, declared on the decorator:

```python
@scanner(messages="all")                                  # per-transcript leaf
def summarize() -> Scanner[Transcript]: ...

@scanner(group_by=["task_set","task_id"], depends_on=["summarize"])
def cross_model() -> Scanner[SummarizedCohort]: ...       # cohort node, reads summaries
```

`ScannerConfig` gains `depends_on: tuple[str, ...]`. Node *kind* is implied:
per-transcript (no `group_by`) or cohort (`group_by` present). Edges reference
upstream scanner **keys**.

### AnalysisStore — the durable collection seam
Physically the **existing recorder buffer** (`scanner=<key>/<subject_id>.parquet`),
which already persists full `ResultReport`s (value/answer/explanation/metadata/
references). It is the durable, cross-process collection point. Interface:

```python
class AnalysisStore(Protocol):
    async def get(self, scanner_key: str, subject_id: str) -> ResultReport | None: ...
    async def members_present(self, scanner_key: str, ids: Iterable[str]) -> set[str]: ...
    async def is_recorded(self, scanner_key: str, subject_id: str) -> bool: ...
    async def record(self, scanner_key: str, subject: Subject,
                     reports: list[ResultReport]) -> None: ...
```

The only new piece vs. today is a **mid-run reader** over buffer fragments (usable
before `sync()`), local-FS first. This is what makes summaries readable downstream
*without* round-tripping through Inspect's `Score` (which strips
`answer/explanation/metadata` — see `sample_metadata.py`).

### Node executor — pure and testable
One executor per node kind, a pure function of (node, inputs from store, recorder):

```python
async def run_transcript_node(node, transcript, store) -> list[ResultReport]
async def run_cohort_node(node, membership, store) -> list[ResultReport]
```

`run_cohort_node` assembles its input from the store: raw member transcripts and/or
upstream member results (when `depends_on` is set), renders with `transcripts_as_str`
(namespaced `[T#:M#]` citations), runs the scanner, records. *Testable with a fixture
store — no eval, no orchestrator, no hooks.*

### Scheduler — the DAG
Build a digraph from `depends_on` over the job's scanner keys; Kahn topo-sort into
ordered **stages** (cycle → `PrerequisiteError` at plan time). Readiness predicate:
- transcript node ready for `T` when every upstream node has recorded `T`.
- cohort node ready for cohort `C` when its **full frozen membership** is present and
  every upstream node has recorded each member.

The current hardcoded per-transcript→cohort split is the degenerate 2-stage case.

### Driver — when/where (the online/offline symmetry)
All drivers run the *same* node executors over the *same* store:
- **OfflineDriver** (`scan()`): read the corpus, run topo stages to completion, one
  final `sync()`.
- **OnlineDriver** (Inspect `@hooks` coordinator): per-transcript nodes run per-sample
  via the existing embedded `eval(scanner=...)` path (`_eval/task/scan.py`); cohort
  nodes run at lifecycle **barriers** over the now-complete store.
- **WatcherDriver** (future): poll a shared store; fire ready nodes — for members
  produced by *separate* processes.

## Execution model

A single eval run is one process with an ordered sequence of barriers; each analysis
attaches to the earliest barrier at which its inputs are complete:

```
sample completes ──► on_task_end ───────► on_run_end ─────────► on_eval_set_end
(transcript nodes:   (one task+model:      (all tasks × models   (whole set)
 summaries)           epochs/samples)       in this run)
```

- **Collection** is the shared store on disk — process-agnostic; members accumulate
  there as samples finish, in whatever process produced them.
- **Trigger** is the driver. In-process barriers (hooks) can only fire for evals
  running in *that* process.

Consequence (the one real constraint, and it does **not** restrict grouping):
- Members produced by **one** run/process (the typical `eval(tasks=[...],
  model=[...], epochs=N)` or one `eval_set`) → any grouping runs **online** at the
  appropriate barrier (epoch cohorts at `on_task_end`, cross-model at `on_run_end`).
- Members produced by **separate** `inspect eval` processes → no shared in-process
  hook; that cohort runs **offline** (`scan()` over the union of logs) or via a
  WatcherDriver. Results are identical (`cohort_id` is driver-independent).

Grouping flexibility is total and lives entirely in `group_by`/`depends_on`; online
vs offline only changes *when/where* a node fires, never *what* you can group.

## Human-definable surface

The whole analysis graph is declared in `scout.yaml` (readable; the graph is
inferred from `group_by` + `depends_on`):

```yaml
analyses:
  summarize:
    name: summarize
    file: scanners.py
  epoch_consistency:
    name: cohort_compare
    file: scanners.py
    group_by: [task_set, task_id, model]    # epochs of one model
    depends_on: [summarize]
  cross_model:
    name: cohort_compare
    file: scanners.py
    group_by: [task_set, task_id]            # across models
    depends_on: [summarize]
transcripts: ./logs        # offline corpus; online attaches to the eval instead
```

`online: auto|require|never` is a **job-level** knob (not on the scanner): `auto`
runs nodes in-process when a hosting barrier exists, else offline; `require` errors if
no online barrier is available; `never` forces offline. The *same* scanners run either
way.

## Identity, durability, resume

- `cohort_id` from grouping-dim values → stable across drivers and runs.
- `members_digest` (hash of frozen membership) detects drift → re-scan + warn.
- The store is durable parquet; `is_recorded`/`is_cohort_recorded` gate work →
  resume and cross-process collection for free.
- `inputs_digest` (over upstream versions + content) added to cohort rows so a changed
  upstream summarizer cascade-invalidates downstream cohorts (deferred refinement).

## Online ≡ offline guarantee

Cohort nodes fire **only at full-membership barriers**, never incrementally. This (a)
makes online results byte-identical to offline, and (b) sidesteps `max_members`
truncation divergence. It is a property worth asserting in tests.

## Testability

- **Node executors**: pure functions over a fixture store — no eval/hooks needed.
- **Store**: small unit interface.
- **Scheduler**: graph/topo/cycle/readiness unit tests.
- **Drivers**: tiny `eval(--epochs N, model=[A,B])` with `mockllm`, asserting
  `online_result == offline_result` over the same logs.

## PR stack

1. **PR1 — offline cohort scanners.** *(done)* The driver-agnostic core in embryo.
2. **PR2 — store + node-executor extraction.** Add the mid-run `AnalysisStore` reader;
   extract `run_cohort_node` from `scan_cohorts` so it reads members from a source.
   Pure refactor, no behavior change; unlocks everything else.
3. **PR3 — analysis DAG (offline) + cohort-over-summaries.** `depends_on`, topo
   scheduler (2-phase becomes the 2-stage default), the `SummarizedCohort` input
   contract. Delivers summarize→cohort offline. Human-definable via `scout.yaml`.
4. **PR4 — OnlineDriver.** A Scout `@hooks` coordinator: per-transcript nodes on the
   `eval(scanner=...)` path; cohort nodes at `on_task_end`/`on_run_end`/
   `on_eval_set_end`. Reuses the scheduler, store, and node executors wholesale.
5. **PR5 (optional) — watcher/cross-process, inputs_digest cascade, remote-FS online.**

PRs 1–3 are pure Scout (stable, fully self-testable, deliver the offline capability).
PR4 couples to `inspect_ai` internals (`eval(scanner=)`, hook emit points), so the
offline DAG is treated as canonical and the online tier as a layered optimization.

## Honest constraints (do not over-promise)

- Cohorts fire at completion **barriers**, not truly per-sample-incremental.
- Across **separate** eval processes, cohorts are offline (or watcher) — never inline.
- Truncated (`max_members`) cohorts only at full-membership barriers.
- The online tier depends on `inspect_ai` internals that are version-sensitive; pin it
  with regression tests against that contract.

---

## Hardening (adversarial review outcome)

A 4-lens adversarial review (ergonomics / soundness / execution / break-it), verified
against source, confirmed the 3-concern spine is sound but found **5 blockers** that
change the abstractions. The decisions below supersede the sketch above.

### B1 — One `Cohort` input type (the central fix)
The `Scanner` protocol is single-positional (`__call__(self, input, /)`); there is no
way to type `Sequence[Transcript]` *and* upstream summaries through it, and the
existing detector rejects anything but `Sequence[Transcript]`. So **every cohort
scanner is `Scanner[Cohort]`** — one type for all kinds (raw, with-deps, meta):

```python
@dataclass(frozen=True)
class CohortMember:
    subject_id: str                       # transcript_id (or cohort_id for meta)
    transcript: Transcript | None         # None when the node declared no content
    def upstream(self, node_id: str, *, as_type: type[M] | None = None) -> M | StoredResult: ...

@dataclass(frozen=True)
class Cohort:
    key: dict[str, JsonValue]
    cohort_id: str
    members: Sequence[CohortMember]       # member-aligned; assembled by the executor
    members_digest: str
    upstream: UpstreamResults             # .results(node_id, as_type=...) -> Sequence[M]
```

The executor assembles `Cohort` (members + aligned upstream results) from the store
**before** calling the scanner, so the scanner body is a pure function of its input and
never touches the store. `SummarizedCohort` is dropped. Producers may declare
`@scanner(..., produces=Model)` for type-safe `as_type=` consumption.

### B2 — Edges are job-level node ids
`depends_on` cannot bind to scanner *keys* (computed with order-dependent numeric
suffixes in `ScanJob.__init__`). Edges reference the **node id** (the `scout.yaml`
`analyses:` map key, or explicit `id=` on the decorator), resolved *after* key
assignment and validated against the final id set. Forbid suffix auto-disambiguation
when edges are present. Node **kind** is explicit: cohort iff `group_by`/`preset` set;
has-deps iff `depends_on` set — remove the `is_cohort_signature` annotation sniffing.

### B3 — Node bodies run in `copy_context()`
`_scan_one_cohort` sets `init_transcript`/`init_model_usage` with bare `.set()`. Offline
that's safe (anyio child tasks copy context); inside an online hook (`_emit_to_all` runs
hooks in the eval's own context) it would **corrupt the eval's** `transcript()`/
`model_usage()`. Every node body must run in `contextvars.copy_context()`.

### B4 — Fail-closed readiness + honest digest
`_scan_one_cohort` silently `continue`s over missing members but records
`members_digest` over the *full* frozen set → `is_cohort_recorded` then caches a
*partial* scan as authoritative forever, defeating online==offline. A cohort runs only
when its full membership is present; otherwise it stays unready and is surfaced as an
explicit error subject (never silently absent from a "complete" scan). If a policy
opts into subset-scanning, the digest is recorded over the **actual** members plus
`missing_members`/`effective_size`.

### B5 — Separate cohort row namespace
Cohort parquets must live under `scanner=<key>/cohorts/<cohort_id>.parquet`, **not**
beside per-transcript rows. Inspect's embedded-scan finalize reads `column("transcript_id")`
unconditionally (KeyError on cohort rows) and **deletes** any buffer file whose stem
isn't a live `transcript_id` (silent loss of cohort parquets on every online sync).
Namespacing must land in PR2, before the online driver (PR4).

### Majors
- **Store returns `StoredResult`, not `ResultReport`.** `to_df_columns` drops cohort
  `input` and stringifies `value`; the durable, recoverable projection is
  uuid/value(+value_type)/answer/explanation/metadata/references/label/type. No node may
  depend on an upstream node's `input`.
- **`inputs_digest` belongs in PR3, not PR5.** The moment any edge exists, resume keyed
  only on `members_digest` serves stale downstream results when an upstream scanner
  changes. Record `inputs_digest` (upstream versions/params + digest of upstream rows
  read) and check it in `is_cohort_recorded`.
- **Subject identity:** both record paths write `subject_id` + `subject_kind`; cohort
  identity is always the pair `(scanner_key, cohort_id)`. Canonicalize dim values
  (sorted-keys JSON) before grouping and hashing. `CohortSpec.include_missing=False`
  drops None-dim transcripts (with a plan-time warning) by default.

### Revised PR stack
1. **PR1 — offline cohort scanners** (open). *Amend before merge:* adopt the `Cohort`
   input type, fail-closed digest, and `copy_context` isolation, so we ship the right
   abstraction rather than `Sequence[Transcript]`-then-break.
2. **PR2 — store + row-shape + executor extraction** (the version-sensitive seam):
   `subject_id`/`subject_kind` columns, `cohorts/` namespace, `AnalysisStore` mid-run
   reader returning `StoredResult`, pure `run_*_node` executors in `copy_context`.
   Must precede PR4.
3. **PR3 — offline DAG + cohort-over-summaries**: `Cohort` type wired to `depends_on`
   job-level edges, Kahn topo scheduler (2-phase = 2-stage default), fail-closed
   readiness, `inputs_digest`, typed `produces=`/`as_type=`, `include_missing`.
4. **PR4 — OnlineDriver**: `@hooks` coordinator at `on_task_end`/`on_run_end`/
   `on_eval_set_end`; regression tests asserting online==offline and no eval-context
   leak / no cohort-file deletion.
5. **PR5 (optional)** — watcher / cross-process / remote-FS / cohort-over-cohort.

### Locked decisions (from review)
- **Input type:** adopt the single `Cohort` object **in PR1, before merge** (replacing
  `Sequence[Transcript]`), plus the B3 (`copy_context`) and B4 (fail-closed/honest
  digest) fixes — ship the right abstraction from day one.
- **Cohort-over-cohort (meta-cohort):** deferred to PR5. The `Cohort`/`upstream` types
  are designed to add it later without a type change.
- **Upstream error → downstream:** configurable; **default run-degraded** — run over the
  members that have results, record the digest over the **actual** members plus
  `missing_members`/`effective_size` (never cached as complete; re-runs when the missing
  member arrives). `block` (stay unready + error subject) is the opt-in strict mode.
- **Cohort metrics:** excluded from per-scanner summary metrics for now (additive later
  with "aggregate over cohorts" semantics if a scalar per-cohort metric is wanted).

### Still open (lock before PR3/PR4)
- `max_members` online: accept non-byte-identical online/offline, or fold
  `members_digest` into `cohort_id`?
- Membership drift: destructive overwrite vs point-in-time history.
