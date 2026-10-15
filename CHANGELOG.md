# Changelog

All notable changes to the Workspace Migration Utility are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (`MAJOR.MINOR.PATCH`). The
version here is the single source of truth in [`VERSION`](VERSION); the tool stamps it into every
bundle's `manifest.json`, `export_index.json`, and the migration state table.


## [Unreleased]

## [1.2.0] - 2026-10-13

Customer fixes, wave 2 (PLAN 16.3). No state-schema change; the only fingerprint change is Alert V2 (below).

### Upgrade notes (from 1.1.x)
- **Pull into the existing Git folder — no reinstall.** Jobs installed by 1.1.x work unchanged. Each notebook now
  ends its output with the run's folder (`Run id : …` / `Folder : …`).
- The first upgraded run **sets each migrated SQL query's owner** to its source owner once (rows read `updated`,
  "owner set to … (verified)") and **re-checks every workspace setting against the target**: a setting the target
  does not hold is re-applied and read back.
- **Every Alert V2 reads `updated` once** on the first 1.2.0 run (a same-content update), then only on real edits.
  In `airgap` mode this needs the source-side Git folder upgraded too.
- **Synced-table pipelines become `manual`** (they used to fail on every run) and leave the `failed_only` bucket.
- **A missing import report now turns the task red** (`completed_no_report`).
- Failures that said "targets workspace folder … which does not exist" now show the **server's own error** when
  the folder actually exists — re-run with `retry_mode=failed_only` to see the real cause (often a missing UC table).
- **Pipelines keep the migration identity as owner.** User-owned pipelines also run as it; SP-owned pipelines keep
  running as their SP (a one-time `updated` on 1.x targets). The source owner gets `CAN_MANAGE`. A metastore admin
  can transfer ownership afterwards (Runbook). Pipeline ACL rows that failed on 1.x (`Only metastore admins can
  change pipeline owner`) heal on the next run or `failed_only`.

### Added
- **SQL queries keep their source owner** (B2). The owner travels outside the payload and the fingerprint, is set
  after create/update with a PATCH, and verified with a GET. A failed or ignored owner change is
  `created_with_warning` ("query left owned by the migration SP"), never a failure. An owner deleted in source is
  left alone. Unchanged queries are rechecked against the owner the target list already returns (no extra calls).
- **SP-owned pipelines keep running as their SP** (CUST-1): the spec gets `run_as` = the source SP (remapped through
  `sp_mapping`). If the target refuses it (e.g. the runner lacks the Service Principal User role), the pipeline is
  created without it as `created_with_warning`. An explicit source `run_as` naming another user is dropped (the
  API refuses it). Only the target's exact refusal (`PERMISSION_DENIED` … `'run_as'` …
  `servicePrincipal.user`, verified live) triggers the fallback; any other error fails the row as before.
- Each of `01_Inventory`, `02_Export` and `04_Import` prints the run's folder at the end of its output (B9).

### Fixed
- **Pipeline permissions were lost** with `403 Only metastore admins can change pipeline owner` (CUST-1): the PUT
  carried the source `IS_OWNER`, which on a UC pipeline is an owner change, and the PUT is all-or-nothing. The
  tool now never changes a pipeline's owner — it keeps the current owner, gives the source owner `CAN_MANAGE`
  and applies every other grant exactly. ACL Parity reads `match` with a `known_gap` note.
- **Workspace settings reported `Created` that the target never took** (B4). Every write is read back; a key the
  target ignores is `failed` / `not_applied` ("set it in the admin console"), never `Created`.
- **Synced-table pipelines failed on every run** (`libraries must contain at least one element`, CUST-2). A
  pipeline with no source code is `manual` with a note naming its synced table — at export and, for 1.x bundles,
  at import (no API call). A **Lakeflow Connect ingestion gateway** (no `libraries` by design) is `manual` with a
  note naming its UC connection; Lakeflow Connect is documented as a manual step (Runbook).
- **"Folder does not exist" was shown for errors that had nothing to do with folders** (QA16.2-1) — e.g. a Genie
  space whose UC table is missing. The folder message is now used only when a live check of that folder returns a
  real 404; likewise "dashboard is not published" only when `…/published` returns 404. Otherwise the report shows
  the server's error.
- **Alert V2 read `updated` on every run without an edit** (QA16.2.1-1): `evaluation.state` and
  `evaluation.last_evaluated_at` changed with each scheduled evaluation and were part of the fingerprint. They are
  now stripped (export) and dropped from the body (1.x bundles).

### Changed
- **A standalone import with a blank `run_id` always uses `LATEST_EXPORT.json`** (B9). It no longer continues an
  unfinished import found in some older folder (that could update the target back to an older copy of the
  source). An import that crashed in the latest export's folder is still continued from its checkpoint.
- **Inventory / export resume only the newest folder** (B9): an older abandoned folder is never picked up.
- **A report that does not land fails the import task** (B11): each report-write failure is logged at ERROR, the
  written files are checked for existence and size, and the run ends `completed_no_report` (the notebook raises:
  "the target WAS changed and the state WAS saved; re-run the import with run_id=<id> to rewrite the reports").

## [1.1.1] - 2026-10-12

Hotfix (PLAN 16.2.1). Import-side only — no change to inventory, export, bundles, fingerprints or the state schema.

### Upgrade notes (from 1.1.0 / 1.0.0)
- **Pull into the existing Git folder — no reinstall, no re-export.** Then run the existing **import** job with
  `retry_mode=failed_only`, `dry_run=false` and your last `run_id` (blank = the latest export). In that one run the
  family-based policies are created, then the jobs and clusters that use them, then their permissions.

### Fixed
- **Cluster policies built on a policy family** (e.g. *Job Compute*, *Personal Compute*) failed to create with
  `INVALID_PARAMETER_VALUE policy_family_id and definition cannot be used together`, and every job and cluster using
  them then failed `prerequisite_missing`. Create and edit now send the family id + its overrides and never the
  resolved definition; custom (non-family) policies are sent exactly as before.

## [1.1.0] - 2026-10-05

Live-tested end to end on classic compute in `direct` mode, including the upgrade from a
1.0.0-migrated workspace (PLAN 16.1 + 16.2 QA).

### Upgrade notes (from 1.0.0)
- **Pull the latest code into the existing Git folder and run the existing jobs — no reinstall.**
  Jobs installed by 1.0.0 work unchanged; the new `log_level` job parameter is optional (defaults to
  `INFO`). State tables, bundles and checkpoints written by 1.0.0 are read as-is; no schema change.
- The first upgraded run publishes the already-migrated dashboards that are published on the
  source, recreates their schedules (**PAUSED** while `pause_job_schedules=true`, the default) and
  applies the dashboard / Genie permissions that 1.0.0 skipped. Nothing else is rewritten.
- Dashboards published with **publisher credentials** are published on the target by the migration
  service principal — have the owner re-publish to restore their own credentials (Runbook).
- The standalone import job still defaults to `dry_run=true`; pass `dry_run=false` for a live run.

### Added
- **Logging you can debug from** (PLAN 16.1 §3). The notebook cell shows the stage, every phase
  start/end with counts and elapsed time, a progress line every 500 objects, and every failure /
  warning with the object, category and the server's own error. The driver log (**Compute → Driver
  logs → Standard error**, kept 30 days) always carries the full DEBUG trace: one start and one
  outcome line per object, every API call and decision. New `log_level` widget / job parameter
  (cell only; default `INFO`).
- **AI/BI dashboard publish state, schedules and subscriptions are migrated** (PLAN 16.2 §4). A
  source-published dashboard is published on the target with the same credentials mode
  (publisher / viewer); each schedule is recreated (PAUSED when `pause_job_schedules=true`) with its
  subscribers (users by user name, notification destinations by name). Two new row types —
  `lakeview_dashboard_publish`, `lakeview_dashboard_schedule` — with their own report sheets. The
  dashboard row and its fingerprint are unchanged, so an upgraded run only adds the new rows. Never
  unpublishes or deletes a schedule. See the Runbook for the publisher-credential caveat.
- The inventory Dashboards sheet shows Published / Credentials / Schedules / Subscribers.

### Fixed
- **Dashboard and Genie permissions are applied** (PLAN 16.2 §3). They were always reported
  `skipped_no_object` (`acls.json` names them by title, the state by path); they are now matched by
  source id, and appear on the ACL Parity sheet. An ACL whose object is simply absent is no longer
  mislabelled `family_not_selected` (new category `object_absent`).
- **A retry that heals an object also applies its permissions** in the same run, and a healed
  dashboard comes back published and scheduled (PLAN 16.2 §5). A same-`run_id` re-run re-checks
  `skipped_no_object` units instead of replaying them from the checkpoint.
- **`.db_internal` / `.ide` / `.databricks` folders are skipped end to end** (PLAN 16.2 §2): no
  per-home 403 at inventory, no ACL entry at export, reported **Skipped — platform-internal** (not
  Created) at import, and their old state rows are never reported deleted-in-source.
- **Export fails fast without an inventory** (PLAN 16.2 §6) instead of re-running the whole inventory
  and possibly exporting an empty bundle.
- Logs: a unit restored from the checkpoint says `resumed from checkpoint — no API call this run`;
  the manifest check logs its `Phase complete:` line (PLAN 16.2 §1).
- The import checkpoint now records each outcome's `asset_type`, so the start-of-run recovery replay
  restores lost rows for multi-type families (identity, compute, workspace, secrets, SQL, misc), not
  only single-type ones. Older checkpoints still replay as before.

- **The migration state table can no longer be silently mis-read** (PLAN 16.1 §4). Every load
  compares `count(*)` with the rows read and stops the run if they differ (logged as `state loaded
  rows=N expected=N`); a failed read raises instead of looking like an empty table (which would have
  re-created objects and dropped source edits). A MERGE can no longer blank a stored target id or
  fingerprint, and a failed state save turns the job red (`completed_state_not_saved`).
- Silent `except` handlers now log what they skipped (DEBUG for expected cases, WARNING when a result
  is degraded), so gaps are visible in the log.

### Changed
- The `execution_*.log` files are no longer written to `misc/` — the job's driver log replaces
  them (older bundles that contain them still verify).
- **Import state writes no longer commit once per failed object** (PLAN 16.1 §4.4). The migration
  state table and the import checkpoint are now written every 200 objects **or 5 minutes**, whichever
  comes first, plus at every phase end and run end. Before, every failure forced its own Delta MERGE
  (~3–7 s each, plus auto-OPTIMIZE churn), so runs with many failures spent most of their time in
  bookkeeping. A hard crash loses at most < 200 objects / 5 minutes of bookkeeping; the re-run adopts
  them (dashboards / Genie / alerts / legacy queries may be duplicated — see the Runbook).

## [1.0.0] - 2026-09-07

First production release.

### Added
- Notebook-based migration of **non-UC workspace assets** from a source to a target Databricks
  workspace (Azure region → Azure region).
- **Two connectivity modes:** `airgap` (two-sided, manual bundle handoff, no cross-workspace
  connectivity) and `direct` (default; all stages run in the target, source read over OAuth M2M).
- **Four notebooks** — `01_Inventory`, `02_Export`, `04_Import`, `00_Install_Jobs` — plus
  pre-packaged Jobs API 2.2 definitions in `jobs/`.
- **Identity migration** including Databricks-managed groups (membership + entitlements), with a
  persisted `old → new` id map.
- **Idempotent, incremental runs** via a Delta state store; **dry-run** default; a graded
  **preflight** go/no-go gate; per-unit **fail-soft** execution.
- **ACL replay** in a final phase with a source-vs-target parity report.
- Per-asset-type **Excel reports** and a **manual-actions** runbook in each bundle.
- Configurable DAB detection (`dab_bundle_roots`).
- Full **documentation set** under `docs/` (architecture, configuration, runbook, permissions).

[Unreleased]: https://example.com/compare/v1.0.0...HEAD
[1.0.0]: https://example.com/releases/tag/v1.0.0
