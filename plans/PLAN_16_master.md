# PLAN 16 — Master: rebuild the PLAN 13 backlog on top of `main`, one wave at a time

**Status:** APPROVED direction 2026-10-04. `main` is the base. Branch `plan13-backlog-b1-b13` is
**reference only** (never merged); we copy specific functions/tests from it where marked below.
Requirements = PLAN_13 B1–B13 + the incremental-export ask, as clarified by the user 2026-10-03/04.
PLAN_14 and PLAN_15 are folded in (16.1 and 16.5).

## Process (every sub-plan)
1. One sub-plan per wave (`PLAN_16_<n>_*.md`) = the detailed dev + test spec, reviewed before coding.
2. One feature branch per sub-plan, cut from `main` (e.g. `plan16-1-logging-state`).
3. **Claude** codes + writes/runs the offline unit tests → **user** reviews, commits and pushes →
   **Claude** runs live QA on the real workspaces (Git folder at that commit) and reports.
4. Fix → re-test until the wave's acceptance passes → user merges the feature branch to `main`.
5. Only then does the next sub-plan start. Never two waves in flight.

## Ground rules (non-negotiable)
1. **`main` works and must not break.** Every change is a bug fix with a regression test, or new
   behaviour whose default is identical to `main`.
2. **Golden baseline diff.** Wave 0 runs `main` end to end and saves its reports + state table. After
   every wave: re-run on a fresh target, diff per-asset-type status counts. Only the wave's declared
   *expected diff* may differ.
3. **Classic compute only (user decision 2026-10-04).** The customer runs **classic** (their
   cross-workspace connectivity only works there), so every live QA run uses a **classic job
   cluster**. No serverless test runs.
4. **No premise without testing the exact production API call** (B8 went wrong testing `mkdir`
   instead of `workspace/import`).
5. **Fail loud.** No new `except` that silently returns `[]`/`{}`/`None` on a read the run depends on.
6. **Do exactly what was asked.** No unrequested structural redesigns (e.g. replacing the live
   existence probe with "state is the source of truth").
7. **Backward compatible with `main`-written state and bundles (user 2026-10-04).** The customer has
   ALREADY run its initial migration with `main`. Every wave must work on top of that: state rows,
   natural keys, `last_action` values, fingerprints and bundles written by `main` stay readable and
   meaningful; no change may make an existing row look new (→ duplicate create) or look done (→ a
   gap that never heals). Each sub-plan states its upgrade path (what a `main`-written row/bundle does
   on the first run after upgrade) and has a test that seeds `main`-shaped state + bundle and runs the
   new code over it; live QA runs the wave on top of the Wave 0 state when the change touches state.

## Contracts on `main` that every wave must preserve
- Upsert decision (`state_store.decide`): CREATE / ADOPT / SKIP / UPDATE, and **row present but object
  gone from target → CREATE** (self-heal). Requires the **live existence probe** in each importer.
- Edits to notebooks/files are detected and migrated (mechanism changes in 16.6, guarantee doesn't).
- run_id resolution: inventory = widget → resume incomplete → fresh; export/import = widget →
  taskValues → resume → `LATEST_*` pointer. **Standalone export/import jobs and airgap import work with
  a blank run_id.**
- Bundle is self-contained; dry run writes only to the `_dryrun` twin; a FAILED outcome never advances
  the fingerprint; phase order + identity two-pass membership; checkpoint resume; `retry_mode` narrows
  the work list only.

## Wave 0 — golden baseline (no code; done before 16.1's live QA)
Fresh target; `main`'s `direct_end_to_end_live` on the full fixture bed, on a **classic job
cluster**: Run 1 → seed incremental edits → Run 2 → `retry_mode=failed_only`. Save all xlsx + a CSV of
the state table + per-stage timings + downloaded driver logs + `FINDINGS.md` (how `main` behaves) to
`~/Desktop/wsmig_runs/plan16_golden/`. Add `tests/golden_diff.py` (per sheet × status counts + a fixed
per-object sample).

**QA run model (user 2026-10-04):** LIVE + DIRECT mode only — **no dry-run jobs**. One identity,
`ai27_wsmig_runner` (account SP, workspace ADMIN on both sides) = the direct-mode source read SP
(client id + OAuth secret, target scope `wsmig_runner/client_id|client_secret`) AND every job's run-as —
exactly how the customer runs it. `tests/runner_sp.py ensure source_ws target_ws` + `scope target_ws`
on every fresh target (secret saved once at `~/.wsmig/ai27_wsmig_runner.json`, 600, never printed).
UC lives in the region metastore and SURVIVES a target rebuild → one **state schema per run**
(`catalog_ws_xaik9y.wsmig_state_main` for Wave 0, `…wsmig_state_16_<n>` per wave), staging volume
`/Volumes/catalog_ws_xaik9y/wsmig_staging/staging`, with a run-specific subfolder. Reports per wave in
`~/Desktop/wsmig_runs/plan16_<n>/`. A fresh target per wave (the user recreates it).

## The waves

| Sub-plan | Scope (high level) | Reuse from `plan13-backlog-b1-b13` |
|---|---|---|
| **16.1** Logging + state-store hardening (**DONE** — live-QA'd 2026-10-05, released in 1.1.0) | B5: notebook cell = INFO (phases, progress every 500, failures), driver log `stderr` = always DEBUG (per-object start/end, every API call), downloadable 30 days; **no log files**. PLAN_14 QA-7: state reads fail loud, `count(*)` check on every load, non-destructive MERGE, failed save turns the job red. **W0-8 (user 2026-10-04):** state + checkpoint flush on 200 rows OR 300 s (no flush per failure), PLAN_16_1 §4.4. | `src/utils/logger.py`: `_LiveStdoutHandler`, `_ContextFilter`, `_RedactFilter`, `register_secret`, `set_context`, `configure_logging`, `get_log`; `token_manager.build_clients` `register_secret` call. **Drop** the file mirror/capture buffer. |
| **16.2** Customer release fixes (**DONE** — live-QA'd 2026-10-07, released as 1.1.0, `main` @ `c3421bb`; RESCOPED 2026-10-05, user: ship to the customer today; spec `PLAN_16_2_customer_release.md`) | (1) **Logging truth** (16.1 QA findings): a checkpoint-resumed unit logs `(resumed from checkpoint, no API call)`; `verify bundle manifest` gets its `Phase complete:` line. (2) **B10 `.db_internal` — skip completely**: directory, contents and ACLs; no ACL fetch at inventory (today 1×403 per user home), reported `Skipped — platform-internal` (never `Created`), still listed so `main`-written rows don't turn into deleted-in-source. (3) **W0-1 dashboard + Genie ACLs** (HIGH, user: super important): resolve by **source object id** (`acls.json source_id` → state `source_object_id` → `target_object_id`), path/name fallback; keep the ACL natural key; `main`-written `skipped_no_object` rows heal on the first upgraded run; ACL Parity covers every object present on the target. (4) **B7 dashboard publish + schedule parity** (user 2026-10-05): draft→draft, published→published with the same credentials mode (publisher/viewer), schedules + subscriptions migrated, ACLs as-is (one ACL for draft+published, verified). Two new unit types `lakeview_dashboard_publish` / `lakeview_dashboard_schedule`, so the dashboard fingerprint stays = `main` and `main`-created drafts get published + scheduled on the first upgraded run. Never unpublish / never delete a schedule (flag). APIs verified live 2026-10-05 (PLAN_16_2 §4.0). (5) **ACLs follow a successful retry** (16.1 QA): any ACL unit whose object was created/updated/adopted in THIS run joins the ACL work list in every `retry_mode`; checkpoint resume no longer treats `skipped_no_object` as done. (6) **Export hard-fails without inventory** (user 2026-10-04): absent / unreadable / no `objects_by_type` → clear error naming path + run_id + "run 01_Inventory first"; never re-runs the inventory. QA: fresh target → `main` e2e (new golden on the current bed + customer-shaped state) → 16.2 e2e ON TOP of that state (upgrade) → `failed_only` → a 16.2 e2e on a fresh state schema (golden diff). | B10 acl_importer skip + `workspace_collector._object_acl` skip (matcher → `helpers`). B7 `DashboardsCollector._published_state`/`_schedules`, `DashboardsImporter._reconcile_publish_and_schedules`/`_get_published` (fix: only 404 = draft; subscriptions added; redone as two unit types per PLAN_16_2 §4.1). Tests `test_b7_*`, `test_b10_*` in `tests/test_plan13.py` (publish/ACL parts only). |
| **16.3** Customer fixes wave 2 → 1.2.0 (RESCOPED by the user 2026-10-09, "Option A"; spec `PLAN_16_3_customer_fixes.md`, DRAFT FOR REVIEW) | **B3 moved to hotfix 16.2.1 / 1.1.1 (`PLAN_16_2_1_policy_family_hotfix.md`, user 2026-10-12 — customer blocked); 16.3 = the other six, on top of 1.1.1.** Seven items, in the user's priority order: **B9** bundle folder = Jobs-UI job run id (new `job_run_id={{job.run_id}}` param on inventory tasks; repair = same folder; run folders ordered by time because job run ids are not time-ordered); **B3** policy-family policies; **B4** workspace-conf read-back (+ re-check of 1.x false `Created` rows); **B11** report-write failure → task red (`completed_no_report`); **B2** legacy SQL query owner; **CUST-1** pipeline ACLs: never transfer the owner (keep the target's current owner, source owner → CAN_MANAGE; user 2026-10-12); **CUST-2** synced-table / library-less pipelines → `manual`. Everything else formerly in 16.3 → "Backlog after 16.3" below. | B9 `ImportRunner.verify_bundle` manifest-missing message + test. B3 `ComputeImporter._put_policy_shape`. B4 `MiscImporter._read_conf_key` (use 16.2 `NotApplied`, not `verify_applied`). B2 `SqlImporter._set_query_owner`, `source_owner` unit field (**no** fingerprint). |
| **16.4** Dashboards + catalog rename | B7 remainder (publish + schedules + subscriptions shipped in 16.2): notification-destination migration, atomic row. B13 `catalog_mapping_json` for dashboards, Genie, DLT. | B7 `DashboardsCollector._published_state`/`_schedules`, `DashboardsImporter._reconcile_publish_and_schedules`/`_get_published`/`_reconcile_schedules`/subscriptions, `asset_export._lakeview_units` facets (fix: only 404 = draft; update an edited schedule). B13 `helpers.parse_catalog_mapping`, DLT `catalog` remap (note, not warning). **Redo** `remap_catalog_refs` — rewrite only inside dataset `queryLines` / Genie table identifiers. |
| **16.5** Performance (B6 + PLAN_15) | `parallel_threads` (default 1 = `main`): parallel inventory ACL enrichment; parallel workspace existence probe (same live probe); import parallel within dependency sub-levels with the main thread recording; id maps built once per sub-level; identity/misc serial; jobs topo-ordered by `run_job_task`. | `BaseCollector.map_parallel`, `WorkspaceCollector.enrich` (report fetch failures). Installer/job `parallel_threads` param plumbing. **Redo** the import-side `_run_parallel`. |
| **16.6** Incremental by metadata | Notebooks/files: change detection by source `modified_at` (no content hash), stored in state `source_modified_at`, compared at export to skip downloads (direct mode, LIVE state table); missing `modified_at` = changed; one-time transition for `main`-written rows; live existence probe kept; default off until signed off. | `workspace_collector` `modified_at` capture; `asset_export._workspace_units` `fingerprint_extra`; `ExportRunner._unchanged_in_state` concept (fix: live table, success + target-id check). |
| **16.7** Cutover: resume schedules (user 2026-10-05, take up later) | Import runs pause every migrated schedule (`pause_job_schedules=true`, default — jobs and, from 16.2, dashboard schedules), and nothing un-pauses them: re-running with `pause_job_schedules=false` does NOT help (unchanged fingerprint → SKIP). Add a cutover option on the import job (e.g. `resume_schedules=true`, or a packaged `wsmig - cutover` job) that touches ONLY schedules: every job schedule/continuous/trigger and every dashboard schedule the tool paused is set back to the SOURCE's pause status from the bundle (UNPAUSED only where the source was UNPAUSED); nothing else is imported; result rows + report sheet; dry-run supported. Works on `main`-written state (jobs). | — (new) |

## Backlog after 16.3 (moved out of 16.3 by the user 2026-10-09; not scheduled)
- **16.4** dashboards + catalog rename (row above) — needs live API checks first (destination secrets) + a user decision on re-pushing already-migrated objects for a catalog rename.
- **B8** best-effort re-sweep of home-content `prerequisite_missing` units before the ACL phase (+ the optional optimistic home write gate).
- **B1** source run-as SP for the airgap source job · **B12** widget labels · **W0-6** workspace-conf coverage, REPORT ONLY · **W0-7** no-op creates labelled `skipped`.
- **QA16.2-2…4** (below). (QA16.2-1 moved INTO 16.3 as §7b by the user 2026-10-12, after the customer's 6 Genie failures.)
- The wider **B4** audit: read-back for the other mutating calls (global init scripts, permission assignments, SCIM entitlements/members, ACL PUT, update calls).

## 16.3 — findings from 16.2 QA (deferred by the user 2026-10-05; now in the backlog above)
None of these were caused by 16.2: the code involved is identical on `main`, so the customer already lives
with them today and 1.1.0 ships without them. Evidence for each: `~/Desktop/wsmig_runs/plan16_2/FINDINGS.md`.

### QA16.2-1 (MEDIUM): the report blames the wrong cause when a dashboard / Genie / query create fails
> **2026-10-12: moved into 16.3 as §7b (`PLAN_16_3_customer_fixes.md`)** after the customer hit it on 6 Genie spaces. The full spec and the audit of the other importers are there.
- **Scenario.** A Genie space lives in a user's folder (`/Users/alice@x.com/My space`) and is built on a UC
  table that does not exist on the target yet (UC is out of scope). Import tries to create it; the server
  refuses with `403 … Table 'cat.schema.retry_tbl' does not exist`.
- **What the tool reports.** `FAILED — prerequisite_missing: targets workspace folder /Users/alice@x.com,
  which does not exist on target yet — provision/assign its owner …`. The folder DOES exist (the tool's own
  check returned 200 a moment earlier). The real server message is not in the report at all (only in the
  driver DEBUG log).
- **Why it matters.** The operator goes off to fix a user home that is fine, while the actual fix is
  "create the UC table". It is still retryable, so nothing is lost — it just wastes the operator's time and
  breaks the rule that the report always carries the server's own error.
- **Cause.** `BaseImporter.missing_parent_prerequisite` treats ANY error containing the words "does not
  exist" as "the parent folder is missing". SQL queries/alerts, AI/BI dashboards and Genie spaces all use it.
- **Fix.** Call it a folder problem only when the error is about the parent folder (or a `get-status` of
  the parent confirms it is missing); otherwise keep the server's error. Always include the raw server error.
  **Also check every other importer / `classify_error` / `_ERROR_MAP` path for the same kind of keyword
  match** (workspace content, jobs, DLT, serving, …) and add a regression test per path.

### QA16.2-2 (LOW): a refused export leaves empty folders behind
- **Scenario.** Someone runs `02_Export` with a run_id that has no inventory. Since 16.2 the export
  correctly stops with a clear error and writes no files.
- **What is left.** An empty folder tree `<run_id>/export/{compute,dashboards,…}`, `misc/`, `reports/` on the
  staging volume (0 files). Harmless — without a manifest no import will ever accept it — but it looks
  like a real run when someone browses the volume.
- **Fix.** Create the run's folder layout only AFTER the inventory check passes.

### QA16.2-3 (MEDIUM): a re-migration with a NEW (or lost) state table can't apply some permissions
- **Scenario.** The normal case is: the same state table is used on every run. This finding is about the
  OTHER case: the target was already migrated, and then someone runs the migration again with an empty state
  table (a new `state_schema`, or the state table was dropped / lost). The tool then has to rediscover every
  object on the target ("adopt" it) instead of reading its id from the state table. Two kinds of objects
  are rediscovered without their target id, so their permissions are reported "target object absent" and
  never applied (also not on later runs):
  1. **Alerts V2.** The tool cannot see the existing alert in its up-front check (the list API does not
     return the alert's folder), tries to create it, gets "already exists", and records it as adopted — but
     with an EMPTY target id. Its permissions then have nothing to attach to.
  2. **Content of a deleted owner's home that was moved to `/Users_Backup`** (PLAN 9). On the first
     migration the tool creates `/Users_Backup/<id>/…` and remembers "source `/Users/<id>` = target
     `/Users_Backup/<id>`", so the permissions land on the backup copy. On a stateless re-run it adopts
     the backup copy but does not remember that mapping, so the permission step looks for `/Users/<id>`,
     which does not exist.
- **Live evidence.** Run E (new empty state schema on the already-migrated target): 2 Alerts V2 + 2 backup
  homes → `object_absent`. The same objects' permissions were applied fine in Run A (first migration) and
  in Runs B–C2 (state kept).
- **Who is affected.** Only a customer who re-runs with a new/lost state table over an already-migrated
  target. The customer's normal path (state kept) is not affected.
- **Fix.** (1) On "already exists" for an alert V2, look the alert up by name + folder and record its real
  id. (2) When adopting backup-diverted content, record the source → backup path mapping exactly as the
  create path does.

### QA16.2-4 (LOW): wrong reason on the permissions of a "manual step" object
- **Scenario.** Some objects are never created by the tool on purpose — a legacy SQL alert (obsolete API)
  or an Azure Key Vault-backed secret scope — they are reported "Manual step". Their permissions can't be
  applied until a person recreates the object.
- **What the tool says.** "the object's family WAS selected, but the object is not on target and this
  migration has no record of creating it" (`object_absent`). On `main` it said the equally wrong
  `family_not_selected`.
- **Fix.** Say "the object is a manual step (<reason>) — recreate it by hand, then re-run
  `import_assets=acls`" when the object's own row is `manual`.

## Customer-reported DLT findings (2026-10-07) — SCHEDULED in 16.3 (user 2026-10-09)
Reported from the customer's import run (`jioindiawest`). Both are on `main` too. The user will decide
later how (which option) and when (a hotfix branch from `main`, or folded into a wave) to tackle them.

### CUST-1 (HIGH): pipeline ACLs fail because the tool tries to transfer the pipeline's owner
- **Error.** `PUT …/api/2.0/permissions/pipelines/<id> -> 403: PERMISSION_DENIED Only metastore admins can
  change pipeline owner`.
- **Cause.** There is no explicit owner transfer, but the ACL phase does one by accident:
  `AclImporter._acl_body` (`src/importers/acl_importer.py`) only drops `inherited` grants and the built-in
  `admins` group, so the source's `IS_OWNER` grant is sent verbatim. On the target the pipeline was just
  created by the run-as SP (= its owner), so `IS_OWNER = <source owner>` is an owner change, which for a UC
  pipeline (`catalog` set) only a metastore admin may do. Our QA never caught it because the identity that
  ran the import was a metastore admin (fixture pipelines are UC with `IS_OWNER` on a user, e.g.
  `wsmig_test_pipeline_classic` → tanveer).
- **Why it matters more than "owner not migrated".** `PUT permissions` is one declarative call, so the 403
  loses EVERY grant on that pipeline (CAN_MANAGE / CAN_RUN / CAN_VIEW), not just the owner. And for a UC
  pipeline the owner is also its run identity, so the target pipeline runs as the migration SP until a
  metastore admin transfers it.
- **Correction (verified live 2026-10-09, PLAN_16_3 §0 V7):** a pipeline PUT with NO `IS_OWNER` entry is itself rejected (`The pipeline must have exactly one owner.`), so "omit" is not a fix; the working forms are a PUT with `IS_OWNER` = the target's CURRENT owner, or an additive PATCH. Options as now specified in PLAN_16_3 §6 / D1: (b) send the source owner, on the 403 re-send with the current owner (recommended); (a) always send the current owner. Original text kept below for history.
- **Fix options (to decide).** (a) For `pipelines`, always omit `IS_OWNER` from the PUT body — simplest,
  what the user asked for ("avoid it"), same outcome every run. (b) Send it; on this specific 403 retry once
  without it — keeps `main` behaviour where the transfer is allowed (non-UC pipeline / metastore-admin
  runner), one extra call per UC pipeline. Either way: the row gets a warning naming the source owner and
  "a metastore admin must transfer ownership (UI → Permissions, or `PUT permissions` as metastore admin)",
  and the ACL Parity sheet shows the missing `IS_OWNER` as a known gap, not a mismatch. Regression test: a
  UC pipeline ACL with `IS_OWNER` on another user → the other grants are applied.
- **Customer workaround today.** A metastore admin re-runs the import with `import_assets=acls`, or sets the
  owner (and the missed grants) by hand.

### CUST-2 (MEDIUM): synced-table pipelines fail to create (`libraries must contain at least one element`)
- **Error.** `dlt_pipeline  Synced table: jio_smb_stg.gold.enterprise_selfcare_twamp_daily_sync 3QJmNr  api_error
  POST …/api/2.0/pipelines -> 400: INVALID_PARAMETER_VALUE libraries must contain at least one element`.
- **Cause.** A `Synced table: <catalog.schema.table> <suffix>` pipeline is not user-authored: the platform
  creates it when a UC / Lakebase **synced table** is created (`POST /api/2.0/database/synced_tables`). Its
  spec has no notebooks/files, and the pipelines create API refuses an empty `libraries`; the service only
  creates these as a side effect of creating the synced table (same error reported internally in SUP-33742
  when pre-creating a sync pipeline). The collector lists them like any pipeline and export marks them
  `auto`, so import POSTs them.
- **How the customer solves it.** Not migratable as a pipeline: recreate the synced table on the target
  (needs the UC source table, the Lakebase database instance and the target catalog — UC/Lakebase are out of
  scope for this tool); that recreates the backing pipeline automatically.
- **Fix.** (1) Export: a pipeline whose spec has no `libraries` and no `ingestion_definition` is not
  creatable through the pipelines API → `manual` unit with a note; `Synced table:` pipelines get a specific
  note naming the table ("recreate the synced table on target"). Should also catch other platform-managed
  pipelines of the same shape (e.g. DBSQL materialized view / streaming table backing pipelines) — verify.
  (2) Import: the same guard in `DltImporter`, because the customer's existing (`main`-written) bundles
  already say `create` for these (ground rule 7) — report `manual`, never POST. Regression test for each.
- **Open check.** We have no synced-table pipeline in our beds, so its `spec.pipeline_type` value is unknown
  (ours are all `WORKSPACE`). The "no libraries" rule does not depend on it; ask the customer for one
  `GET /api/2.0/pipelines/<id>` to sharpen the note (and add a synced table to the fixture bed for live QA).

QA contract: `plans/qa-testing-agent.md` (copied from the branch 2026-10-04 and updated for PLAN 16:
classic compute, golden diff, per-wave stop). Fixtures: reuse `tests/fixtures_fvm1.py` from the branch
(copied in 16.1).

## Open decisions (asked in the relevant sub-plan)
- 16.3: D1–D6 in `PLAN_16_3_customer_fixes.md` §12 (CUST-1 source owner CAN_MANAGE, new-run folder, conf re-check, reinstall for run-id folders, CUST-2 coverage, one-time alert update).
- Backlog (B8): adopt the optimistic home write if the ownership check passes?
- 16.6: upgrade transition (recommended) vs one full re-upload; `incremental_export` default after sign-off.
- CUST-1 / CUST-2: scheduled in 16.3 (user 2026-10-09); CUST-1 = keep target owner (PLAN_16_3 §5, rewritten 2026-10-12).
