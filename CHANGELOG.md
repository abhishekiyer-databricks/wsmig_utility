# Changelog

All notable changes to the Workspace Migration Utility are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (`MAJOR.MINOR.PATCH`). The
version here is the single source of truth in [`VERSION`](VERSION); the tool stamps it into every
bundle's `manifest.json`, `export_index.json`, and the migration state table.


## [Unreleased]

## [1.2.1] - 2026-11-23

Hotfix (PLAN 16.3.1). Import side only: no state-schema, fingerprint, natural-key, bundle or job-template change, so
existing 1.2.0 bundles import as they are (no re-inventory, no re-export, no source-side upgrade in `airgap` mode).

### Upgrade notes (from 1.2.0)
- **Pull into the existing Git folder — no reinstall.**
- **Homes:** run the import job once with `retry_mode=failed_only`, `dry_run=false` and the **same `run_id`**. The
  user / SP homes 1.2.0 left `prerequisite_missing` are created, and their notebooks, files, folders, queries,
  dashboards and Genie spaces land with their permissions, in that one run. Home rows then read `created`.
- **Identities:** nothing is removed. Identity rows 1.2.0 recorded as `created` / `adopted` are skipped while the
  source is unchanged (no surprise writes). Identities 1.2.0 assigned directly although the source only had them via
  a group stay assigned; when such a row is next updated or adopted it carries the note *directly assigned on
  target, nested only on source … left unchanged*. To clean them up, see RUNBOOK → Nested identities; 1.2.1
  re-runs keep the cleanup (1.2.0 re-assigned them).

### Fixed
- **User homes are created by the tool** (ES-2364565). Since 2026-11-12 a home appears only on the owner's
  sign-in or a workspace admin's `mkdirs`, so content under owners who never sign in on the target stayed
  `prerequisite_missing` forever. The import now creates each missing home (`mkdirs` as the workspace-admin run-as),
  at most once per owner per pass, and reads it back. A refusal is a `prerequisite_missing` row quoting the server:
  `DIRECTORY_PROTECTED` → "the owner is not assigned to the target workspace"; anything else → "confirm the run-as
  identity is a workspace admin". Owners deleted in source still go to `/Users_Backup` (decided before any
  `mkdirs`). Dry runs and the existence check never create anything.
- **False "under-populated" group warning.** Group members were matched on the target by display name, which is
  not stable across workspaces for the same account user, so `users` reported e.g. *160/174 members added; could
  not resolve 14 … stays under-populated* although the target group already matched the source. Members are now
  matched by their source id first (account identities keep their id in every workspace), with the name as the
  fallback (recreated workspace-local identities). A member that is a nested-only identity still waiting for the
  nested-identity pass is added after that pass instead of being reported missing; only a member that still
  cannot be found is a warning.
- **Service principal homes** (latent bug): every account SP is in `sp_mapping` as `appId → appId`, which the tool
  read as "recreated SP, home auto-provisioned", so it never checked or created an account SP's home and reported
  it as provisioned. Only an SP mapped to a *different* applicationId is treated as recreated now; its new home
  `/Users/<newAppId>` is checked and created the same way.
- **Nested identities stay nested.** An account group, SP or user that the source had only as a member of an
  assigned group (`workspace_permissions == []`) was assigned directly on the target (`[]` became `USER`, and a
  workspace-SCIM POST of a user/SP is itself a direct assignment) — and re-assigned on every re-run. 1.2.1 never
  assigns or POSTs it: it waits (up to 5 min, one SCIM list per kind every 10 s) until the identity is visible
  through its parent, then applies its entitlements. No parent on the target → `prerequisite_missing` naming the
  parents and their outcome, never a direct assignment. Unknown (`None`, pre-Plan-6 bundles) keeps the `USER`
  fallback; workspace-local and system groups are unchanged.
- **Identities already on the target get their entitlements** (adopt path). 1.2.0 only recorded `adopted` — the
  likely cause of "group A's Workspace access was not copied". An identity direct on the source but visible on the
  target only through a group is now also assigned directly.

### Added
- **Verified entitlements in the report.** Every entitlement / role write is GET → PATCH only the missing values →
  GET. The Users, Service Principals and Groups sheets gain **Entitlements copied**, **Already present**,
  **Entitlements failed** and **Assignment** (`direct` / `via group (…)` / `direct on target (pre-existing); nested
  on source`). An entitlement that is refused or does not read back fails the row (`not_applied`, server message
  kept), so `retry_mode=failed_only` retries it — 1.2.0 logged a warning that never reached the report. No extra
  call for identities with no direct entitlements.

### Docs
- RUNBOOK: homes created by the tool + the two home failure rows; new **Nested identities** section (direct vs via
  group, the DB3 note, safe manual cleanup); job `run_as` SPs need the Service Principal User role.

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

### Docs
- **PyPI proxy guidance corrected:** both `pypi.org` (the index) and `files.pythonhosted.org` (the downloads) must be
  allowed — `pypi.org` alone fails at download. On DBR 15.4 LTS only `openpyxl` + `et-xmlfile` are downloaded; the
  guide now shows how to check a cluster's own list (Permissions guide, Runbook step 3).
- **Workspace settings are documented as best effort:** set via the API and read back for up to 3 minutes; a
  workspace admin should review them after the migration.

### Fixed
- **Pipeline permissions were lost** with `403 Only metastore admins can change pipeline owner` (CUST-1): the PUT
  carried the source `IS_OWNER`, which on a UC pipeline is an owner change, and the PUT is all-or-nothing. The
  tool now never changes a pipeline's owner — it keeps the current owner, gives the source owner `CAN_MANAGE`
  and applies every other grant exactly. ACL Parity reads `match` with a `known_gap` note.
- **ACL Parity reported queries as `extra_on_target`** after their owner was set (QA16.3-1): changing a query's
  owner makes Databricks add an explicit `CAN_MANAGE` for the new owner, which a creator-owner on the source
  doesn't show. That one line is no longer counted as a difference; any other extra grant still is.
- **Alerts V2 already on the target were never found** (QA16.3-2): the existence check read the alerts list under
  the wrong key, so every run re-sent a create, got `ALREADY_EXISTS` and reported the alert `Adopted`. An
  unchanged alert is now `Skipped` with no write, and a changed one is updated in place.
- **Workspace settings reported `Created` that the target never took** (B4). Every write is read back; a key the
  target ignores is `failed` / `not_applied` ("set it in the admin console"), never `Created`.
  Workspace-conf reads lag writes (seen live: up to a few minutes), so a key that does not read back at once is
  re-read with the others every 15 s for up to 3 min — one shared wait, so the worst case is 3 min in total
  however many keys are held. A key that reads back late says "verified by read-back after N s". Workspace
  settings stay **best effort**: the docs now advise a workspace admin to review them after the migration.
- **The ACL of a warehouse with no owner failed** with `400 PUT requests for warehouse … with no existing owner
  must provide a new owner` (seen live on the platform-created Serverless Starter Warehouse of a new workspace).
  When neither the source ACL nor the target warehouse has an owner, the grants are now added (PATCH) instead of
  replacing the ACL; every other warehouse and object keeps the full-replace PUT.
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
