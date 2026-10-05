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
| **16.1** Logging + state-store hardening (READY FOR DEV) | B5: notebook cell = INFO (phases, progress every 500, failures), driver log `stderr` = always DEBUG (per-object start/end, every API call), downloadable 30 days; **no log files**. PLAN_14 QA-7: state reads fail loud, `count(*)` check on every load, non-destructive MERGE, failed save turns the job red. **W0-8 (user 2026-10-04):** state + checkpoint flush on 200 rows OR 300 s (no flush per failure), PLAN_16_1 §4.4. | `src/utils/logger.py`: `_LiveStdoutHandler`, `_ContextFilter`, `_RedactFilter`, `register_secret`, `set_context`, `configure_logging`, `get_log`; `token_manager.build_clients` `register_secret` call. **Drop** the file mirror/capture buffer. |
| **16.2** Customer release fixes (RESCOPED 2026-10-05, user: ship to the customer today; spec `PLAN_16_2_customer_release.md`) | (1) **Logging truth** (16.1 QA findings): a checkpoint-resumed unit logs `(resumed from checkpoint, no API call)`; `verify bundle manifest` gets its `Phase complete:` line. (2) **B10 `.db_internal` — skip completely**: directory, contents and ACLs; no ACL fetch at inventory (today 1×403 per user home), reported `Skipped — platform-internal` (never `Created`), still listed so `main`-written rows don't turn into deleted-in-source. (3) **W0-1 dashboard + Genie ACLs** (HIGH, user: super important): resolve by **source object id** (`acls.json source_id` → state `source_object_id` → `target_object_id`), path/name fallback; keep the ACL natural key; `main`-written `skipped_no_object` rows heal on the first upgraded run; ACL Parity covers every object present on the target. (4) **B7 dashboard publish + schedule parity** (user 2026-10-05): draft→draft, published→published with the same credentials mode (publisher/viewer), schedules + subscriptions migrated, ACLs as-is (one ACL for draft+published, verified). Two new unit types `lakeview_dashboard_publish` / `lakeview_dashboard_schedule`, so the dashboard fingerprint stays = `main` and `main`-created drafts get published + scheduled on the first upgraded run. Never unpublish / never delete a schedule (flag). APIs verified live 2026-10-05 (PLAN_16_2 §4.0). (5) **ACLs follow a successful retry** (16.1 QA): any ACL unit whose object was created/updated/adopted in THIS run joins the ACL work list in every `retry_mode`; checkpoint resume no longer treats `skipped_no_object` as done. (6) **Export hard-fails without inventory** (user 2026-10-04): absent / unreadable / no `objects_by_type` → clear error naming path + run_id + "run 01_Inventory first"; never re-runs the inventory. QA: fresh target → `main` e2e (new golden on the current bed + customer-shaped state) → 16.2 e2e ON TOP of that state (upgrade) → `failed_only` → a 16.2 e2e on a fresh state schema (golden diff). | B10 acl_importer skip + `workspace_collector._object_acl` skip (matcher → `helpers`). B7 `DashboardsCollector._published_state`/`_schedules`, `DashboardsImporter._reconcile_publish_and_schedules`/`_get_published` (fix: only 404 = draft; subscriptions added; redone as two unit types per PLAN_16_2 §4.1). Tests `test_b7_*`, `test_b10_*` in `tests/test_plan13.py` (publish/ACL parts only). |
| **16.2b** Remaining small isolated fixes (moved out of 16.2 on 2026-10-05) | B3 policy family; B4 workspace-conf read-back; no-op creates labelled `skipped` (W0-7: home roots / `Created` dirs that don't exist); B11 report-write loud + task fails; B12 widget labels; B2 query owner; B1 source run-as SP for airgap; **W0-6 workspace conf coverage — REPORT ONLY** (extended key list, non-default/different keys → inventory sheet + `Manual step`, never written). | B3 `ComputeImporter._put_policy_shape`. B4 `verify_applied`, `VerificationFailed`, `CAT_NOT_APPLIED`, `MiscImporter._set_conf`/`_read_conf_key`. The `out.get("skipped")` branch in `base_importer._process_one`. B11 `ImportRunner._verify_report_written` + `completed_no_report`. B12 labels. B2 `SqlImporter._set_query_owner`, `sql_collector` owner enrichment, `source_owner` (+ fp). B1 `job_templates.run_as_for_job` + `source_run_as_spn`. Tests `test_b1/2/3/4/11/12_*`. |
| **16.3** Run identity + user homes | **B9 (DEFERRED here from 16.2 by the user 2026-10-05): the bundle directory is named by the Jobs-UI job run id ONLY (no date/time), whatever the number of runs/repairs/failures; old `YYYYMMDD_HHMMSS` bundles stay readable (explicit run_id / LATEST pointer).** Via a new `job_run_id={{job.run_id}}` param (keep `run_id` blank; standalone/airgap unchanged). B8: one best-effort re-sweep of home-content `prerequisite_missing` units just before the ACL phase. Optional gate: optimistic home write (only if a live check shows the user owns a home created this way). **QA16.2-1 (deferred here by the user 2026-10-05, MEDIUM, same on `main`):** `BaseImporter.missing_parent_prerequisite` turns ANY create error containing "does not exist" into "workspace folder `<parent>` does not exist on target" (`prerequisite_missing`) and drops the server message — live: a Genie create failing `403 … Table 'catalog_src_g9c6nd.wsmig_test.retry_tbl' does not exist` was reported as a missing user home while the folder existed (`get-status` 200). Fix: only classify as a folder prerequisite when the error is about the parent path (or a `get-status` of the parent confirms it is absent), and always carry the raw server error (IMP-2 rule). **Also investigate every other importer / error-classification path for the same pattern** (substring matches that relabel a real server error: SQL queries/alerts, Lakeview dashboards, Genie share this helper; check `_ERROR_MAP` / `classify_error` callers, workspace content, jobs, DLT, serving) and add a regression test per path. Evidence: `~/Desktop/wsmig_runs/plan16_2/FINDINGS.md` QA16.2-1. | B9 actionable manifest-missing error in `ImportRunner.verify_bundle` + its test. **Drop** `{{job.run_id}}` in templates and all B8 `DeferredHome`/defer code. |
| **16.4** Dashboards + catalog rename | B7 remainder (publish + schedules + subscriptions shipped in 16.2): notification-destination migration, atomic row. B13 `catalog_mapping_json` for dashboards, Genie, DLT. | B7 `DashboardsCollector._published_state`/`_schedules`, `DashboardsImporter._reconcile_publish_and_schedules`/`_get_published`/`_reconcile_schedules`/subscriptions, `asset_export._lakeview_units` facets (fix: only 404 = draft; update an edited schedule). B13 `helpers.parse_catalog_mapping`, DLT `catalog` remap (note, not warning). **Redo** `remap_catalog_refs` — rewrite only inside dataset `queryLines` / Genie table identifiers. |
| **16.5** Performance (B6 + PLAN_15) | `parallel_threads` (default 1 = `main`): parallel inventory ACL enrichment; parallel workspace existence probe (same live probe); import parallel within dependency sub-levels with the main thread recording; id maps built once per sub-level; identity/misc serial; jobs topo-ordered by `run_job_task`. | `BaseCollector.map_parallel`, `WorkspaceCollector.enrich` (report fetch failures). Installer/job `parallel_threads` param plumbing. **Redo** the import-side `_run_parallel`. |
| **16.6** Incremental by metadata | Notebooks/files: change detection by source `modified_at` (no content hash), stored in state `source_modified_at`, compared at export to skip downloads (direct mode, LIVE state table); missing `modified_at` = changed; one-time transition for `main`-written rows; live existence probe kept; default off until signed off. | `workspace_collector` `modified_at` capture; `asset_export._workspace_units` `fingerprint_extra`; `ExportRunner._unchanged_in_state` concept (fix: live table, success + target-id check). |
| **16.7** Cutover: resume schedules (user 2026-10-05, take up later) | Import runs pause every migrated schedule (`pause_job_schedules=true`, default — jobs and, from 16.2, dashboard schedules), and nothing un-pauses them: re-running with `pause_job_schedules=false` does NOT help (unchanged fingerprint → SKIP). Add a cutover option on the import job (e.g. `resume_schedules=true`, or a packaged `wsmig - cutover` job) that touches ONLY schedules: every job schedule/continuous/trigger and every dashboard schedule the tool paused is set back to the SOURCE's pause status from the bundle (UNPAUSED only where the source was UNPAUSED); nothing else is imported; result rows + report sheet; dry-run supported. Works on `main`-written state (jobs). | — (new) |

QA contract: `plans/qa-testing-agent.md` (copied from the branch 2026-10-04 and updated for PLAN 16:
classic compute, golden diff, per-wave stop). Fixtures: reuse `tests/fixtures_fvm1.py` from the branch
(copied in 16.1).

## Open decisions (asked in the relevant sub-plan)
- 16.3: adopt the optimistic home write if the ownership check passes?
- 16.6: upgrade transition (recommended) vs one full re-upload; `incremental_export` default after sign-off.
