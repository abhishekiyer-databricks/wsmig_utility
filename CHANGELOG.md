# Changelog

All notable changes to the Workspace Migration Utility are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (`MAJOR.MINOR.PATCH`). The
version here is the single source of truth in [`VERSION`](VERSION); the tool stamps it into every
bundle's `manifest.json`, `export_index.json`, and the migration state table.


## [Unreleased]

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
