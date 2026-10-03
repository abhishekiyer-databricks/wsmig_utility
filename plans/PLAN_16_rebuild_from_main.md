# PLAN 16 — Rebuild the PLAN 13 backlog on top of `main`, in small live-verified waves

**Status:** PROPOSED 2026-10-04. Supersedes the implementation on branch `plan13-backlog-b1-b13`
(kept for reference only; never merged). Requirements = PLAN_13 B1–B13 + the incremental-export ask,
as clarified by the user 2026-10-03/04. PLAN_14 and PLAN_15 are folded in here (§4 Wave 1 / Wave 5).

---

## 0. Ground rules (non-negotiable)

1. **`main` works. Nothing that works on `main` may break.** Every change is either a bug fix with a
   regression test, or new behaviour behind a default that is **byte-identical to `main`**
   (`parallel_threads=1`, blank `catalog_mapping_json`, `incremental_export=false` until its wave is
   signed off, etc.).
2. **Small waves, live QA after each, stop for sign-off.** No wave starts until the previous one is
   live-verified and the user has committed it. Never all at once.
3. **Golden-baseline diff.** Before Wave 1, run `main` end-to-end on a fresh target and keep its reports
   + state table as the *golden baseline*. After every wave, re-run on a fresh target and diff
   per-asset-type status counts against it. Any difference must be an *intended* change listed in that
   wave's "expected diff"; anything else = stop.
4. **Both computes.** We test on serverless; **the customer runs CLASSIC** (their cross-workspace
   connectivity only works there). Every wave's live QA includes at least one full run on a **classic
   job cluster**. Thread/stdout and Spark-collect behaviour differ between the two.
5. **No premise without a live test of the exact production call.** (B8 went wrong because `mkdir` was
   tested and `workspace/import` was not.) Every "the API does X" claim names the call that was tested.
6. **Fail loud, never swallow.** Any new `except` either re-raises, records a FAILED row, or logs at
   WARNING/ERROR with the reason. No `return []` / `or {}` on a read the run depends on.
7. **Never commit/push** — the user does all git operations.

---

## 1. Contracts on `main` that must survive (regression checklist)

| Contract | Where on main |
|---|---|
| Upsert decision: no row+absent→CREATE, no row+present→ADOPT, fp same→SKIP, fp moved→UPDATE, **row+gone from target→CREATE (self-heal)** | `state_store.decide` |
| **Live existence probe** per family (incl. workspace `get-status`) — the state table is NOT the only source of truth; a lost/empty state self-heals | `*_importer.existing_keys` |
| Notebook/file edits are detected and migrated (GAP1). **Mechanism changes in Wave 6 by user decision 2026-10-04:** content sha256 → source `modified_at`; the guarantee itself must survive | `export_runner._apply_content_fingerprint` (today) |
| run_id: blank → resume newest incomplete bundle → else fresh; export/import: widget → taskValues → resume → `LATEST_*` pointer. **Standalone export/import jobs + airgap import work with a blank run_id** | `bundle_state.resolve_*_run_id`, notebooks |
| Bundle is self-contained (re-importable into a fresh target) | export |
| Dry run writes to the `_dryrun` twin only | `Config.state_table_fqn` |
| Failed outcome never advances the fingerprint | `base_importer._record` |
| Phase order + identity two-pass membership | `phases.py`, `identity_importer` |
| Resume from checkpoint; `retry_mode` narrows the work list only | `base_importer._process_one` |

Any wave that touches a row above must carry an explicit regression test for it.

---

## 2. What went wrong on the PLAN 13 branch (so we don't repeat it)

- **Too much at once:** 13 items + structural rewrites in one pass, about 5K lines. The offline tests
  passed, but the failures were in job wiring, scale and multi-run state, which offline tests don't cover.
- **Changes nobody asked for:** QA-2 replaced the live existence probe with "state is the source of
  truth". That removed `main`'s self-heal, and one bad state load then corrupted the table
  permanently (blank `target_object_id`, new fingerprints stamped on stale objects).
- **B8 was built on an untested premise** ("homes can't be created", based on testing `mkdir` only).
  That gave 0% home content; it was then reverted, which left the re-sweep you actually asked for
  as dead code.
- **B9 was applied to every job**, which broke the standalone export/import jobs and the airgap
  import.
- **The B5 logger kept the Volume file mirror** (full-file copy every 25 lines under a global lock)
  and added a line per object. That is O(N²) I/O and serialises every worker thread.
- **Corrections to my own earlier review (2026-10-03):**
  - The B7 "a publish that fails is never retried" finding was **wrong**. The FAILED row exists,
    so `decide()` returns UPDATE (not ADOPT) and `update_one` re-runs the reconcile step. It does
    heal, as long as the dashboard name is unique in its folder.
  - **QA-3's statement that "main wrote optimistically" is wrong.** `main` also gated on
    `_home_present` and failed with `prerequisite_missing`. That is exactly the 185 dirs + 79
    notebooks + 13 files in the customer `import_status.xlsx`.

---

## 3. What from the branch is good and gets REUSED

Port by copying the specific function or test (no cherry-picking whole commits), onto a fresh
branch from `main`.

| Branch piece | Verdict | Change needed when porting |
|---|---|---|
| B3 `ComputeImporter._put_policy_shape` + create/edit wiring + `test_b3_*` | **Reuse as-is** | — |
| B4 `verify_applied`, `VerificationFailed`, `CAT_NOT_APPLIED`, `classify_error` branch, `MiscImporter._set_conf` read-back + `_read_conf_key`, `test_b4_*` | **Reuse, small fix** | normalise compare (case-insensitive bools, treat `None` as "unknown → FAILED with observed=None") |
| B10 ACL-importer skip of platform-internal paths + collector `_object_acl` skip, `test_b10_*` | **Reuse, small fix** | move the matcher into `helpers.is_platform_internal()` (collectors must not import importers) |
| QA-3 "no-op create → `skipped`, not phantom `created`" (`out.get("skipped")` branch) for roots/Trash/`.db_internal` | **Reuse** | reporting-only change; list it in the Wave 2 expected diff |
| B11 `_verify_report_written`, `completed_no_report`, ERROR-with-traceback, `test_b11_*` | **Reuse, small fix** | the notebook's LAST cell must **raise** on `completed_no_report` (job goes red), not just print |
| B12 widget label prefixes (all four notebooks + installer) + `test_b12_*` | **Reuse as-is** | only the labels; drop the branch-only widgets that don't come back |
| B2 `SqlImporter._set_query_owner`, `sql_collector` owner enrichment, `source_owner` unit field, `test_b2_*` | **Reuse, small fix** | fold `source_owner` into the query fingerprint explicitly (so an owner change is detected) |
| B7 collector `_published_state` + `_schedules`; importer `_reconcile_publish_and_schedules`, `_get_published`, `_reconcile_schedules`, subscriptions; `asset_export` publish facets in payload + fingerprint; `test_b7_*` | **Reuse, fixes** | `_get_published`: only 404 = draft; any other error → FAILED (don't re-publish on a transient error). A schedule edit under the same name → update it, not skip |
| B13 `parse_catalog_mapping`, DLT `catalog` remap, `test_b13_*` (parser + DLT) | **Reuse** | DLT: put the remap in the note, not a warning (or every row turns yellow) |
| B13 `remap_catalog_refs` (regex over the whole JSON) | **REDO** | rewrite only inside dashboard dataset `queryLines` and Genie table-identifier fields (parse JSON first); no context-free bare-word replace |
| B9 manifest-missing actionable error (`ImportRunner.verify_bundle`), `test_b9_*` for it | **Reuse** | — |
| B9 `run_id={{job.run_id}}` in every job template | **DROP** | redesigned in Wave 3 |
| B1 `job_templates.run_as_for_job` + `source_run_as_spn` widget | **Reuse, fix** | `run_as_sp` required only when a non-airgap_source job is selected |
| B6 `BaseCollector.map_parallel` + `WorkspaceCollector.enrich` (parallel ACL fetch) | **Reuse, fix** | keep progress logging on the consumer (main) thread; count fetch failures into the collector's errors (shown in the report) instead of silently leaving `acl=None` |
| B6 import `_run_parallel` / `sublevels` / lock-guarded `_record` | **REDO** | design in Wave 5 (workers do API calls only; the main thread records + logs; id maps snapshotted per sub-level) |
| B5 `_LiveStdoutHandler`, `_ContextFilter`, `_RedactFilter`, `register_secret`, `set_context`, `configure_logging`, `get_log` | **Reuse** | strip the file mirror + capture buffer; fix the `StructuredLogger` INFO threshold |
| B5 file mirror / `set_log_file` / `flush_log_file` / `execution_*.log` | **DROP** (user 2026-10-04) | logs go to run output only |
| B8 `DeferredHome`, `_resweep_deferred_homes`, `_resweep_home_content`, `defer` resolution | **DROP** | Wave 3 builds the simple re-sweep |
| QA-2 state-derived `existing_keys` | **DROP** | keep `main`'s live probe; parallelise it (Wave 5) |
| Incremental export: `modified_at` in the content fingerprint (`asset_export._workspace_units` `fingerprint_extra`), collector `modified_at` capture, export `_unchanged_in_state` skip | **Reuse concept, fixes** | Wave 6: read the LIVE state table (not dry-run-derived), keep the live existence probe, add the upgrade transition rule + missing-`modified_at` fallback + incremental-bundle guard |
| `StateStore.migrated_keys` | **DROP** | not needed |
| `tests/fixtures_fvm1.py` additions (uncommitted on the branch: dashboards matrix, phase_incremental, scale users, identities) | **Reuse** | the QA bed |
| `plans/qa-testing-agent.md` | **Reuse** | add rules 3–5 from §0 |

---

## 4. The waves (live QA + stop after each)

Each wave: offline tests → golden diff → live E2E (serverless **and** classic) → incremental run → stop
for review → user commits. The "expected diff" vs the golden baseline is declared up front.

### Wave 0 — Baseline (no code)
- Fresh target. Run `main`'s `direct_end_to_end_live` on the full fixture bed: Run 1 (fresh), seed
  the `phase_incremental` edits, Run 2 (incremental), a `retry_mode=failed_only` run. Repeat Run 1 on a
  **classic** job cluster.
- Save `import_status.xlsx`, `export_status.xlsx`, `inventory.xlsx`, and a CSV dump of the state table
  → `~/Downloads/wsmig_runs/plan16_golden/`. Record wall-clock time per stage.
- Write a small `tests/golden_diff.py`: per sheet × status counts, plus per-object status for a fixed
  sample list.

### Wave 1 — Foundations: logging + state-store hardening (no functional change)
**B5 logging (user directive 2026-10-04: every step in the run output, NO log files).**
- Port the stdlib-logging layer (§3). `StructuredLogger._emit` → app logger only; delete the file
  mirror, `set_log_file`, `flush_log_file`, capture buffer, `EXECUTION_*_LOG` writes in
  01/02/04 and the related tests/manifest exclusions (keep the exclusion helper tolerant of old bundles).
- Format: `ts LEVEL [module] run=<id> stage=<STAGE> | msg  k=v…`. Secrets redacted.
- **What is logged (every stage, every step):** stage start/end with config summary; each collector /
  importer phase start → progress → end with counts; **one line per object that does work or fails**
  (`created/updated/adopted/failed/manual`, with object + target id + error category + raw error);
  intermediate steps that can block (state load/flush with row counts + duration, existence probe,
  content fetch, report write, preflight checks) at INFO; API calls at DEBUG.
- **Unchanged/skipped objects are aggregated** in progress lines (`workspace: 4,000/10,555 done —
  3,950 skipped unchanged, 48 created, 2 failed`) every N items **and** at least every 30 s, so a stuck
  run's last line always names the phase + the object in flight. `log_level=DEBUG` adds per-skip lines.
- **Output size (verify first, Wave 1 task):** confirm the job notebook output limit live (I believe
  ~20 MB total / ~8 MB per cell → run cancelled). Per-object lines are about 150 B, so a 1M-object
  customer run needs the skip aggregation above. Also confirm whether the classic driver log
  (downloadable from the job run) captures the same lines; if not, add a `StreamHandler(sys.__stderr__)`
  so the driver stderr log has them too (no Volume writes).
- **Threads:** log per-object outcomes from the main (consumer) thread wherever possible (the
  `parallel_map` generator already yields on the main thread). Verify worker-thread lines reach the
  cell on **classic and serverless**.
- `dbutils.notebook.exit` (if ever added) goes alone in a trailing cell.

**State-store hardening (PLAN_14 QA-7 + the R2 corruption path).**
- Split `SqlBackend.sql()` (DDL/DML, result ignored) from `query()` (SELECT, collect, **raise on
  error**, log row count + duration). `state_store.load()` uses `query()`. Nothing returns `[]` on an
  exception.
- **Step 0 before coding:** on a classic cluster AND serverless, run the exact `SELECT * … WHERE
  source_workspace_id=…` + `.collect()` against a ≥25K-row copy of the QA state table and capture the
  real error, if any. Fix that root cause specifically.
- `StatementApiBackend`: follow `next_chunk_internal_link` (rows 16,355/21,276 bug).
- **Non-destructive MERGE:** `target_object_id`, `source_object_id`, `last_source_fingerprint`,
  `first_seen` update as `COALESCE(NULLIF(s.c,''), t.c)` — an empty value can never blank a stored one.
- **Live-run guard:** if a live import loads 0 rows but `misc/` shows a prior completed import for this
  pair (or `LATEST_EXPORT` points to an earlier imported run), abort with an explicit message.
- `StateStore._cache` reads go through snapshot helpers (`list(...)`), ready for Wave 5.
- Remediation note for the QA target corrupted on the branch: `RESTORE TABLE … TO VERSION AS OF <pre-Run-2>`.
- **Expected diff vs golden:** none in statuses; more output lines; no `execution_*.log` in `misc/`.

### Wave 2 — Small, isolated fixes (each an independent toggle-free bug fix)
B3 (policy family), B4 (workspace-conf read-back), B10 (`.db_internal` ACL skip), QA-3 no-op-create
labelling, B11 (report-write loud + task fails), B12 (labels), B2 (query owner + verify; jobs/pipelines/
warehouses already keep IS_OWNER via ACLs; dashboards/genie/clusters documented as not settable), B1
(source run-as SP for `airgap_source`, with the `run_as_sp`-optional fix).
- **Expected diff:** family policy `FAILED→Created`; workspace-conf keys the platform dropped show
  `FAILED (not_applied)` (true state); `.db_internal` ACL rows `failed→skipped`; root/Trash/`.db_internal`
  dirs `Created→Skipped`; queries show owner set; query fingerprints move once → one UPDATE per query.
- Live QA includes one **airgap** install in the source workspace for B1.

### Wave 3 — Run identity + user homes
**B9 (redesigned, never breaks standalone/airgap).**
- Templates keep `run_id: ""`. Add a new param `job_run_id: "{{job.run_id}}"` to every notebook task.
- `01_Inventory`: precedence unchanged (widget → resume incomplete → fresh); "fresh" now uses
  `job_run_id` when present (else the timestamp, as today). Export/import precedence unchanged
  (widget → taskValues → resume → pointer). So the dir = Jobs-UI run id for every fresh job run;
  a standalone export/import job still finds its bundle via the pointer; a repair reuses the same dir.
- Every stage logs `run_id=<x> resolved via <how> (job run <job_run_id>)`.
- Keep the actionable manifest-missing error.

**B8 (exactly the user's ask: best-effort re-sweep before ACLs).**
- During the workspace phase, collect units that FAILED with `prerequisite_missing` under
  `/Users/<user>/…` (owner in the source roster). Immediately before the ACL phase, clear the home
  cache and re-attempt each once via the normal `_process_one`; the outcome replaces the earlier row.
  Log `home re-sweep: N retried, M healed, K still missing`.
- **Optional, separate decision gate:** the QA-3 discovery (a content `workspace/import` into a
  not-yet-existing `/Users/<user>` creates the home). Before adopting it, verify live as the run-as SP:
  the created home's permissions show the user as owner/Manage, and the user can open it. If yes →
  write optimistically for in-roster users (big improvement on `main`); if no → stay with `main`'s gate +
  the re-sweep. QA-5A (don't report `/Users/<user>` root rows) comes with this.
- **Expected diff:** run dir names = job run ids; fewer home `prerequisite_missing` (re-sweep heals).
- Live QA: e2e job, **repair run**, standalone `inventory`→`export`→`import` jobs, standalone
  `failed_only` with an explicit run_id, airgap source job → handoff → target import job.

### Wave 4 — Dashboards + catalog rename
B7 (publish state / embed mode / schedules / flag-only unpublish / atomic row) and B13 (catalog mapping).
- B13 rewrites only SQL inside dashboard `queryLines` and Genie table identifiers (parsed JSON), plus
  the DLT `catalog` field. The active mapping is folded into the import-side decision for those three
  types, so setting a mapping later updates already-migrated assets.
- **Expected diff:** source-published dashboards land published; with a mapping, the three types point
  at the renamed catalog. Blank mapping → identical to golden.
- Live QA: the B7 dashboard matrix (draft / published Shared / published Individual / scheduled),
  incremental publish/unpublish/republish, a renamed target catalog, and a catalog named `main`
  appearing in titles/columns (must stay untouched).

### Wave 5 — Performance: parallelism (B6) + id-map build-once (PLAN 15)
All behind `parallel_threads` (default `1` = `main`, byte-identical); the installer exposes it.
- Scope 1: inventory workspace ACL enrichment (reuse `map_parallel`). Fetch failures counted and
  reported, not silent.
- Workspace `existing_keys`: same live probe as `main`, run on the pool, home presence cached per pass
  (both outcomes). Progress lines while it runs (fixes the "20 min of silence").
- Scope 2 import: per dependency sub-level, workers do **API calls only** and return an outcome; the
  **main thread** records state/checkpoint/results and logs, so no lock is needed for recording.
  The target-id maps a sub-level reads are built **once at sub-level start** (PLAN 15) → O(1) lookups
  and no `_cache` iteration race. Identity + misc stay serial. Jobs: topological sub-levels by
  `run_job_task` (cycles → serial tail), so job→job refs resolve on pass 1.
- Live QA: threads=1 run = golden exactly; threads=8 run = same statuses; scale test (≥25K state rows,
  ≥10K workspace objects) with timings vs Wave 0.

### Wave 6 — Incremental by metadata: `modified_at` replaces the content hash (user decision 2026-10-04)
**Requirement (user):** do NOT hash notebook/workspace-file bytes to detect change. Use the object's own
last-modified timestamp: store it in the state table, and compare it **at export** against the state
value so unchanged content is never downloaded (or re-uploaded).

**Step 0 — verify the signal live before coding (rule §0.5).** On source, for NOTEBOOK and FILE, record
`modified_at` from `workspace/list` (the bulk call inventory already makes — no extra API call), then
check that it moves for: UI edit, `workspace/import overwrite=true`, notebook revision restore, file
re-upload, and that it does NOT need to move for rename/move (path = natural key → a new unit anyway).
Also confirm whether a permission-only change bumps it (harmless: one extra re-upload). If any real
content edit does NOT bump it → stop and decide with the user before building.

**Design.**
- **Collector:** carry `modified_at` (epoch ms) on every NOTEBOOK/FILE record (reuse the branch line in
  `workspace_collector`).
- **Fingerprint:** for `notebook`/`workspace_file` the fingerprint = `payload (path, object_type,
  language) + modified_at`. The content sha256 is **removed** from the fingerprint (still computed as
  manifest/checksum metadata when bytes are fetched, never used for change detection).
- **State table:** new column `source_modified_at` (added via `ADD COLUMNS` + swallow "already exists",
  same pattern as `last_source_detail`). Import writes it on every successful create / update / adopt /
  skip. It makes the value human-queryable and lets export decide without re-deriving the fingerprint.
- **Export (direct mode, reads the LIVE state table — never the `_dryrun` twin; export does not depend
  on `dry_run`, fixing QA-4):** per content unit,
  `state.source_modified_at == unit.modified_at` AND state has a target id AND `last_action` is a
  success → **unchanged: don't download**, emit the unit with `export_status=unchanged` and the
  fingerprint; else download as today. Per-phase log: `content: 10,555 total — 10,540 unchanged (not
  fetched), 15 fetched`.
- **Import:** unchanged fingerprint → SKIP (no bytes needed); moved → UPDATE (re-upload with
  `overwrite=true`); new → CREATE. Decision code is `main`'s `decide()`, unchanged — only the
  fingerprint input changed.
- **Airgap:** export runs in the source and has no state table → downloads everything (as today); the
  import still SKIPs unchanged content by `modified_at`, so the re-upload is saved even in airgap.

**Safety rails (so nothing on `main` breaks).**
1. **Missing `modified_at`** (API omits it) → treat as changed: download + UPDATE. Never "unchanged" on
   an unknown — worst case is an extra upload, never a dropped edit.
2. **Upgrade from `main` (no mass re-upload):** state rows written by `main` have no
   `source_modified_at` and a sha-based fingerprint. For those rows ONLY, the first upgraded run
   downloads the bytes, and if the sha matches the stored fingerprint it records SKIP + the
   `modified_at` (no re-upload); if not, UPDATE. This is a one-time transition per object — every
   later run is purely `modified_at`, no hashing. (Alternative if you prefer simpler code: accept one
   full re-upload of all content on the first upgraded run — at customer scale that is hours, so the
   transition rule is recommended.)
3. **Live existence probe stays** (`main`'s self-heal): an unchanged object deleted on target is
   recreated — but its bytes weren't exported. That unit (and any unit that needs bytes the incremental
   bundle doesn't have: `force_full_import`, state lost, row failed) is recorded **FAILED** with
   "bytes not in this incremental bundle — re-run export with `force_full_export=true`" — loud, never
   a silent wrong write. `force_full_export=true` always downloads everything.
4. **Target-side edits are NOT detected** (same as `main` today — only source changes drive updates).
   Documented.
5. Ships behind `incremental_export` (default `false` = today's full download) until this wave's live
   campaign passes, then flipped per §6 decision 3.

**SQL queries:** legacy-query text comes back inline from the list call, so there are no bytes to skip;
their fingerprint is over the payload (excludes `updated_at`). Optional follow-up: use `updated_at` vs
state to skip the per-query GET-by-id enrichment in inventory. Not needed for correctness.

**Note:** this removes the content download/upload cost; it does not reduce inventory listing + ACL
enrichment (the real customer-scale hotspot) — that is Wave 5.

**Live acceptance:** (a) no-change re-run downloads ~0 notebooks/files and uploads 0; (b) edited
notebook + file + query migrate; (c) a target-deleted unchanged notebook fails loud with the
force_full_export hint, then heals with it; (d) a `main`-written state table upgrades without a mass
re-upload; (e) airgap re-run uploads only changed content; (f) runs on classic + serverless.

---

## 5. Live QA protocol per wave (for the QA agent)
1. Fresh target (or the restored baseline), Git folder at the exact commit, `00_Install_Jobs`.
2. Dry-run job → live job (serverless) → seed the incremental edits → live job → `failed_only` →
   one run on a **classic** cluster.
3. Golden diff + this wave's expected diff + each item's live acceptance.
4. Read the run output: every stage shows start/progress/end, and no 30 s+ silent gaps.
5. Stop and report: what was tested, run ids, the diff, and any bug filed in this plan's §7. Don't fix
   anything without sign-off.

## 6. Open decisions for the user
1. Wave 3 B8 optional gate: adopt the optimistic home write if the ownership check passes?
2. Wave 1: if the output-size limit is confirmed, is "aggregate unchanged skips, per-object lines
   for work/failures" acceptable as the default (DEBUG shows every skip)?
3. Wave 6: is `incremental_export` default `true` once signed off, or kept opt-in?
4. Wave 6 upgrade from `main`: one-time sha transition per object (recommended, no mass re-upload) vs
   accept one full re-upload on the first upgraded run?

## 7. Bugs found during PLAN 16 QA
*(appended per wave)*
