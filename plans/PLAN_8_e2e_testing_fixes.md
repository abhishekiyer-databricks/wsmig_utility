# PLAN 8 — End-to-end testing fixes (2026-08-12 session)

## Purpose
This plan collects **bugs found while live-testing the full pipeline** (source_ws → target_ws,
direct mode) during the 2026-08-12 session, plus the agreed fix for each. Nothing here is
implemented yet — the session is for **discussion + triage**. We implement **and test all fixes
together at the end** of the session.

Related handoff context: memory `incremental-rework-findings-2026-08-12` (the earlier incremental
re-work change-set — some of those items may fold in here).

Legend: **Status** = `Triaged` (agreed, not started) / `In progress` / `Done (tested)`.

---

## Bug 1 — Cluster libraries: only the FIRST library on a shared cluster installs on force-start

**Status:** Done (offline tested; live blocked only by target cluster networking) — 2026-08-18.
Implemented in `misc_importer` by tracking `self._force_started_clusters` and DEFERRING the stop to
`run()` (`_stop_force_started_clusters`) — cluster started once, all its libraries install, stopped
once at the end. Per-unit results unchanged. Tests:
`test_multiple_libraries_on_one_cluster_start_it_once_and_stop_it_once`,
`test_a_failed_install_still_stops_the_force_started_cluster`. (Live force-start not runnable — the
target_ws clusters can't reach RUNNING per the ignored 5c networking issue — but the batching logic
is fully offline-covered and uses the same start/install/delete endpoints already in use.)
**Area:** `src/importers/misc_importer.py` (`_install_library`, `_start_cluster_and_wait`,
`_stop_cluster`), phase 11 (cluster libraries)
**Config involved:** `library_force_start_clusters=true`

### Symptom (live)
Two libraries target the same target cluster `wsmig_test_cluster`
(`0812-102056-kkx9g8hs`). With `library_force_start_clusters=true` on the retry run:
- `maven:com.google.code.gson:gson:2.10.1` → **Created** ("cluster was force-started … then stopped").
- `pypi:tabulate==0.9.0` → **FAILED**: `400 INVALID_STATE Cluster … is in unexpected state Terminating.`

The flag worked; the *first* library on the cluster installed. Every *subsequent* library on the
**same cluster** failed.

### Root cause
`_install_library` runs a full **start → install → stop** cycle **per library unit**. Each library
is its own unit and they are processed one at a time:
1. Library #1 (gson): cluster TERMINATED → force-start → wait RUNNING → install → `finally` calls
   `clusters/delete` (stop). Cluster enters **Terminating**.
2. Library #2 (tabulate): `_cluster_state` != RUNNING → calls `clusters/start`, but the cluster is
   mid-**Terminating** → Databricks returns `400 INVALID_STATE … Terminating`.
   `_start_cluster_and_wait` only swallows errors containing `"already"`, so this raises
   `PrerequisiteMissing` → FAILED.

So the stop that library #1 triggers races the start that library #2 needs. Whichever library runs
first wins; the rest on that cluster are doomed.

### Fix — clean approach (agreed): batch libraries by target cluster
Instead of start/install/stop per library, group all cluster-library units by their **target
cluster** and, per cluster:
1. Start the cluster once (only if not already RUNNING; remember whether WE started it).
2. Install **all** libraries for that cluster (each install still recorded as its own unit
   result — created / failed / skipped — so per-library reporting is unchanged).
3. Stop the cluster **once** at the end, only if WE started it (never stop a cluster the customer
   already had running).

Design notes / things to preserve while implementing:
- Keep the "only stop what we started" guarantee (`started_by_us`).
- Per-library results must still be individually reported (one row per library in the Cluster
  Libraries sheet) — batching is an execution optimization, not a reporting merge.
- A single library's install failure must not abort the others on the same cluster, and must not
  skip the final stop (no idle DBUs left burning).
- Preserve existing error/PrerequisiteMissing messages for the genuine cases (no target cluster;
  cluster won't reach RUNNING; DBFS jar out of scope).
- Reconcile with the runner/phase model: cluster-library units are currently dispatched
  individually by the phase runner. Batching may need the grouping to happen inside the importer
  (collect units → group → execute) rather than the runner calling `_install_library` per unit.
  **Confirm how phase 11 dispatches units before implementing** so per-unit checkpointing/state
  writes still line up.

### Test plan
- Live: two clusters, one with ≥2 libraries (mixed maven/pypi), `library_force_start_clusters=true`,
  from a cold (TERMINATED) start → all libraries install, cluster started once and stopped once.
- A cluster the customer left RUNNING → libraries install, cluster is **not** stopped.
- Regression (offline): assert grouping starts a cluster once and stops once for N libraries, and
  that one library's failure doesn't prevent the others or the final stop.

---

## Bug 2 — `failed_only` retries `created_with_warning` units; the label lies

**Status:** Done (offline tested) — 2026-08-18. `RETRY_BUCKETS["failed_only"]` is now `{ACTION_FAILED}`
only; `created_with_warning` rides `failed_and_skipped` (a+c). Test:
`test_retry_buckets_pick_up_exactly_the_documented_actions`.
**Area:** `src/state/state_store.py` (`RETRY_BUCKETS`), and the knock-on note-drop is Bug 3
**Config involved:** `retry_mode=failed_only`

### Symptom (live)
The `import_status_retry_*.xlsx` from a `retry_mode=failed_only` run showed **4 service principals
as `Skipped (unchanged)`** even though none of them had FAILED. Cross-referencing the original
`import_status.xlsx`, those 4 SPs are exactly the ones marked **`Created (warning)`** (each carried
the "OAuth client secret existed on source and CANNOT be migrated" warning). The 5 plain
`Created`/`Adopted` SPs did **not** appear. Every other tab in the retry file was genuine failures
only, so the SP tab looked anomalous.

### Root cause
`RETRY_BUCKETS["failed_only"] = {ACTION_FAILED, ACTION_CREATED_WITH_WARNING}`
(`state_store.py:87`). `created_with_warning` was folded into `failed_only` **on purpose** (see the
comment at `state_store.py:79`) so warning units wouldn't "fall through both buckets and be silently
forgotten." But the effect is that `failed_only` does **not** mean "only failed":
1. The 4 warning SPs are re-selected into the work list (they pass `in_work_list`; non-selected
   assets are dropped as `retry_out_of_scope` and excluded from the report — hence only these show).
2. On re-attempt they already exist + fingerprint matches → recorded `Skipped (unchanged)`.

So the operator sees "successful, unchanged" objects in a report they expected to contain only
outstanding failures — confusing, and (via Bug 3) it also silently drops the warning's manual note.

### Fix — make `failed_only` literally failed-only (agreed)
- Remove `ACTION_CREATED_WITH_WARNING` from the `failed_only` bucket so it means `{ACTION_FAILED}`
  only.
- **Decide where `created_with_warning` goes so it is not silently forgotten** (the original comment's
  valid concern). Options to pick from when implementing:
  - (a) Add it to `failed_and_skipped` only (keep `failed_only` pure; `failed_and_skipped` still
    re-attempts warnings). — leans on the operator choosing the broader mode.
  - (b) Introduce a dedicated `retry_mode` value (e.g. `warnings_only` or `failed_and_warnings`)
    so warnings are retryable **explicitly**, never implicitly under `failed_only`.
  - (c) Keep warnings out of every automatic bucket but ensure they stay **visible** as a standing
    item (report/summary), so they aren't "forgotten" even though `failed_only` won't touch them.
  - **Leaning: (a) + (c)** — `failed_only` = failed; warnings ride `failed_and_skipped` AND remain
    visible in the summary. Confirm at implementation time.
- Update the `state_store.py:79` comment block to match the new semantics.

### Interaction with Bug 3
Bug 3 (below) is the reason a re-selected warning silently loses its note. Even after this fix, a
permanent-warning unit (OAuth secret) reached via `failed_and_skipped` would still drop its note on
the skip row — so Bug 2 and Bug 3 should be implemented and tested together.

### Test plan
- Regression (offline): a state row with `created_with_warning` is **not** in
  `retry_keys("failed_only")`; still present in whichever bucket we choose (a/b).
- Live: a run with a `created_with_warning` SP, then `retry_mode=failed_only` → the SP does **not**
  appear in the retry report; the retry report contains only genuinely failed units.

---

## Bug 3 — OAuth-secret SP should be a MANUAL label, not a warning (and its note is dropped on re-run)

**Decision (2026-08-13):** An SP that had an OAuth client secret on source is a **`manual`**
outcome, NOT `created_with_warning`. The utility can never create these secrets (no API returns a
secret value — same class as AKV / secret-scope values), so it is a standing human task, not a
degraded-but-done object. Modeling it as `manual` also means it naturally rides the
`skipped_only` / `failed_and_skipped` buckets (the "take it up later" case) and never pollutes
`failed_only` (Bug 2). Apply the same treatment on BOTH the create path and every re-run.

**Status:** Done (offline tested) — 2026-08-18. Implemented as a SEPARATE `service_principal_secret`
`manual` unit emitted every run by `identity_importer.load()` (`_oauth_secret_manual_units`), driven
by the source note — so the SP itself stays created/adopted, the secret task is `manual` (rides
skipped_only/failed_and_skipped, never failed_only, never collapses to 'unchanged'). The SP's own
create no longer emits the secret as a `warning`. Tests:
`test_an_sp_with_oauth_secrets_yields_a_created_sp_plus_a_manual_secret_task`,
`test_oauth_secret_manual_task_is_emitted_every_run_independent_of_the_sp_outcome`.
**Area:** `src/importers/identity_importer.py` (`update_one` / the skip-unchanged path for
user/SP), report + state recording of a skip that lands on a prior `created_with_warning` row
**Config involved:** any re-run / retry that re-touches a warned unit

### Symptom (live)
The 4 OAuth-secret SPs were flagged `Created (warning)` on the first run with the actionable note
*"OAuth client secret existed on source and CANNOT be migrated — create a new secret on target."*
On the retry they became `Skipped (unchanged)` with the generic note *"unchanged since the last
import (fingerprint match)"* — **the manual instruction is gone**, and the state action flips
`created_with_warning` → `skipped`, so a later `failed_only` retry (even after Bug 2's fix, this
applies to whatever bucket re-touches it) will **no longer surface it at all**. The permanent manual
task silently disappears after one re-run.

### Root cause
The OAuth-secret warning is a **permanent** condition (no API ever returns a secret — same class as
AKV / secret values). But:
- `_create_sp` (identity_importer.py ~L366-371) is the only place that emits the has_secrets manual
  warning; the update/skip path does not re-derive it.
- When a warned SP is re-processed and found unchanged, the row is recorded as a plain
  `skipped`/`unchanged` with the generic fingerprint-match note — the has_secrets signal is neither
  re-computed nor carried forward, so both the **note** and the **`created_with_warning` status**
  are lost.

### Fix — model the OAuth-secret SP as `manual`, persisted across re-runs (agreed)
- On `_create_sp`: when the source SP had secret(s), record the outcome as **`manual`** (with the
  actionable "create a new secret on target" note), not `created_with_warning`. The SP object itself
  is still created/adopted — the `manual` outcome is specifically the secret task.
  (Confirm at implementation time how to represent "SP created AND has a standing manual task" so
  the object is still counted as created while the secret shows in the manual list — likely a
  created row plus a manual-action entry, rather than overloading a single status.)
- On the update / skip-unchanged path for user/SP: **re-derive the has_secrets condition** so the
  manual note + manual action is re-emitted on every run where the source SP still has a secret —
  never collapse it to a generic "unchanged" row that loses the instruction.
- Because it's `manual`, it stays discoverable via `skipped_only` / `failed_and_skipped` and out of
  `failed_only` — consistent with Bug 2.

### Test plan
- Offline: a source SP with secret(s) → create path yields a `manual` action for the secret (SP
  still created); a re-run with unchanged fingerprint still re-emits the manual secret action + note.
- Live: first run → SP created + secret shows as a manual step; re-run → the OAuth-secret manual
  instruction is still present (not a bare "unchanged"), and it does not appear under `failed_only`.

### Live confirmation (2026-08-13 incremental runs)
Reproduced end-to-end. In run `20260813_045309`, SPs where a secret was **created** on source
(ai27-umi #3, wsmig_test_new_spn #10) showed `Updated — "entitlements/roles re-applied"` — the
generic note, with NO mention of the secret / manual action. Same generic note for secret
**removal** (ai27-umi-3 #4, wsmig-spn-secret-to-be-deleted #11). Root cause is `update_one`'s
hard-coded string (`identity_importer.py:247`). Fold the messaging fix into Bug 5.

---

## Bug 4 — Deleted-in-source items are invisible in the Excel report

**Status:** Done (offline tested; real xlsx render verified) — 2026-08-18
**Area:** `src/reports/import_report.py` (xlsx summary sheet builder, ~L267–331)
**Config involved:** none (reporting only)

### Symptom (live, 2026-08-13)
Two SPs deleted on source (`ai27-umi-to-be-deleted` #5, `wsmig-spn-to-be-deleted` #12; appIds
`03a150c0…`, `978bd289…`) simply **vanished** from the Service Principals tab in run
`20260813_061725`. There is no "deleted" row and no summary line — reading the xlsx, a deletion is
indistinguishable from "never existed". (The operator misread the SPs' lingering orphan home-dir
rows `/Users/<appId>` — still present as directories on source, shown `Skipped (unchanged)` in the
Notebooks tab — as the deletion status.)

### Root cause
Deletion detection **works**: `_report_deleted_in_source` (`import_runner.py:385-405`) writes a
`deleted_in_source` state row via `mark_missing_in_source` (`state_store.py:479`) and populates
`context["deleted_in_source"]`, and ACL Parity even flags the orphaned target grants as
`extra_on_target`. The gap is purely presentation: the finding is rendered **only in the markdown
runbook** (`import_report.py:519-527`), while the **xlsx summary** has just four sections — Outcome
roll-up / Failures / Manual steps / Per-asset-type (`import_report.py:267-331`) — and none of them
carries deletions. A deleted unit is also never a processed unit, so it never appears in any
per-asset-type tab either.

### Fix — surface deletions in the xlsx (agreed)
- Add a **"Deleted in source — review"** section to the xlsx summary sheet, mirroring the markdown
  runbook (`import_report.py:519-527`): grouped by asset_type, listing each natural_key, with the
  standing note "not deleted on target; set `allow_deletes=true` to opt into deletion".
  The data is already in `summary["deleted_in_source"]` (folded in at `import_report.py:112/142`).
- Add a **"Deleted in source"** count to the Outcome roll-up header/row so the total is visible at a
  glance (and consider a matching column in the Per-asset-type table).
- Colour is already registered (`import_report.py:51`, `deleted_in_source → FFE4E6`); reuse it.

### Test plan
- Live: delete a user, an SP, and a group on source; re-run → each appears in the new
  "Deleted in source" xlsx section with its asset_type + key, and the roll-up count is non-zero.
- Offline: given a `context["deleted_in_source"]` with entries, the generated xlsx contains the
  section and count; given none, the section is omitted.

### Additional live confirmation (2026-08-18, jobs incremental) — same gap, JOB asset type
Reproduced for a **job**: `new_wsmig_job` (created in run `20260817_095316`) was **deleted on
source** before the 2nd-pass run `20260818_023042`. Verified live: the job is **still PRESENT on
target** (correct — no auto-delete) but the `import_status - Third run.xlsx` has **no mention of it
anywhere** — no Jobs-tab row, no "Deleted in source" section, no roll-up count. Confirms the fix
must cover every asset family, not just identity: same root cause (rendered only in the markdown
runbook), same fix. Add a job deletion to the Bug 4 test matrix alongside user/SP/group.

---

## Bug 5 — Identity update note is generic; name the real change + report source-side removals

**Status:** Done (offline + LIVE tested) — 2026-08-18. Added `last_source_detail STRING` to the
schema, `_STATE_COLS` (the MERGE uses a HARD-CODED tuple, NOT the schema — plan assumption
corrected), and `record(source_detail=)` with carry-forward; `update_one` now diffs prior-vs-current
source snapshot and NAMES added + removed-in-source (reported, not applied). **Live-verified on
target_ws**: the existing `wsmig_migration_state` table got the column ALTER-added, idempotent on
re-run, MERGE round-trips the snapshot. **CORRECTION**: `ADD COLUMNS IF NOT EXISTS` is a
PARSE_SYNTAX_ERROR on Databricks SQL — used plain `ADD COLUMNS (...)` + swallow FIELD_ALREADY_EXISTS
(see memory [[databricks-sql-add-columns-syntax]]). Tests:
`test_identity_update_note_names_added_and_removed_entitlements`,
`test_group_member_removed_in_source_is_reported_not_applied`,
`test_identity_diff_degrades_gracefully_when_there_is_no_prior_snapshot`,
`test_source_detail_column_round_trips_and_is_carried_forward`,
`test_ensure_table_{adds_the_source_detail_column_with_supported_syntax,is_idempotent_when_the_column_already_exists}`.
**Area:** `src/state/state_store.py` (schema + `record`), `src/importers/identity_importer.py`
(`update_one`, `_sync_members`, `_apply_entitlements`), `src/state/sql_backend.py` (MERGE cols)
**Config involved:** none functionally; needs an ALTER on the **live** control table

### Symptom (live, 2026-08-13)
`update_one` reports a fixed string regardless of what actually changed:
- SP secret add/remove → `"entitlements/roles re-applied"` (see Bug 3 confirmation).
- WS-local group **member removal** (`wsmig_test_parent_grp` #10) → `"entitlements re-applied;
  2/2 members added"` — the removed member is **never mentioned**, and (additive-only) it **remains
  on target**. Same pattern silently shrinks the built-in `users` group (24→23 across runs, still
  reported "N/N added").

### Decisions (locked)
1. **Add a state column** storing the previous **source** member/entitlement/role snapshot, so we
   can diff old-source vs new-source and name exactly what was **added** and **removed in source**
   (diffing against target instead would conflate "removed in source" with "added on target by
   hand"). Requires an idempotent ALTER on the live table.
2. **Secrets** are reported as a **current-presence manual action** (Bug 3), never "added/removed"
   — the API only exposes secret *presence* (`has_secrets`, tri-state), not values or counts, so
   add-vs-remove can't be determined and the action is identical either way.
3. **Behaviour is unchanged on target**: still additive (`op:"add"`) — removals are **reported, not
   applied** (per the agreed "report, don't act" stance for source-side removals).

### Fix — implementation detail
**State schema (`state_store.py`):**
- Add `last_source_detail STRING` (JSON) to the `CREATE TABLE` for the main state table
  (`state_store.py:167-185`). Shape: `{"members": [...], "entitlements": [...], "roles": [...]}`
  (only the fields relevant to the asset_type; empty/omitted for non-identity types).
- **Live-table migration:** `CREATE TABLE IF NOT EXISTS` will NOT add the column to the existing
  customer table, so run an idempotent `ALTER TABLE {table_fqn} ADD COLUMNS IF NOT EXISTS
  (last_source_detail STRING)` at `ensure_tables` time (same "ALTER on live table" pattern noted for
  the `skip_reason` item in memory `incremental-rework-findings-2026-08-12`). Do NOT drop/recreate.
- `record()` (`state_store.py:274`): add an optional `source_detail: str = ""` kwarg; write it into
  the row dict (carry-forward from `prior` when a later row omits it, like the other fields).
  `sql_backend` derives its MERGE column list from the table schema (`sql_backend.py:88`), so once
  the schema has the column the upsert plumbs it through automatically — verify no hard-coded column
  list elsewhere needs it.

**Importer (`identity_importer.py`):**
- On **create** and **update**, persist the current source snapshot (members/entitlements/roles)
  into `last_source_detail` via `record(..., source_detail=...)`.
- In `update_one` (`identity_importer.py:236-262`), read the prior snapshot from the state row,
  diff against the current source payload, and build a precise note, e.g.
  `"entitlements added: [X]; removed in source (not removed on target — review): [Y]; members added:
  [A]; removed in source (retained on target — review): [B]"`. Replace the three hard-coded notes
  (lines ~247, ~255, ~258).
- `_sync_members` (`identity_importer.py:660`): keep the additive PATCH; additionally compute
  `removed = prior_source_members − current_source_members` and include them in the returned note as
  "removed in source (retained on target)". (For account/Entra groups, membership stays
  account-owned/not-modified — but still surface the diff for visibility if cheap.)
- `_apply_entitlements` (`identity_importer.py:736`): keep additive; report entitlements/roles that
  were present in the prior snapshot but absent now as "removed in source (not removed on target)".
- **Secrets:** when `has_secrets` is true, emit the Bug-3 `manual` action + note instead of the
  generic string; do not attempt add/remove wording.

**First-run-after-upgrade guard:** existing state rows have no `last_source_detail` yet. Treat a
missing/empty prior snapshot as "no diff available" — fall back to the current behaviour (re-apply +
a neutral note like "re-applied; prior detail not recorded"), and populate the column going forward.
Never crash on a null.

### Test plan
- Offline: given a prior snapshot and a new source payload, the diff yields correct added/removed
  sets for members and entitlements/roles; a missing prior snapshot degrades gracefully.
- Offline: `record(..., source_detail=...)` round-trips through the MERGE (column present in schema).
- Live: (a) add + remove a member on a ws-local group → note names both, target keeps the removed
  member, and the removal is visibly reported; (b) turn an entitlement off on source → reported as
  "removed in source, not removed on target"; (c) secret created on an SP → manual action, not
  "entitlements/roles re-applied"; (d) idempotent re-run after → the row settles to unchanged.

---

## Scope clarification (NOT a bug) — account-level manager / can-use / can-manage grants

Recorded so future test passes don't re-flag these as failures. "Can manage" on a group and
"Can use / Can manage" on a service principal are **account-level access-control** (rule-sets:
`roles/group.manager`, `roles/servicePrincipal.user`/`.manager`), NOT workspace-scoped grants and
NOT the SCIM `entitlements`/`roles` the tool captures. Verified live (2026-08-13): granting "Can
manage" on an account group propagates account-globally to every workspace the group is used in.

Consequences (all correct, no fix needed):
- The workspace-scoped migration neither reads nor writes these; there is nothing to carry.
- Same account → already in effect on target the moment the identity is assigned.
- Different account → provisioned during account setup / Entra→SCIM (customer/account-IT), which is
  the existing verify-only account-preflight task.
- In reports these show `Skipped (unchanged)` (nothing the tool tracks changed) — accurate, if terse.
  A row that *did* move (e.g. ai27-umi #1/#7 showing `Updated`) moved because of a **co-located**
  captured change (a secret/entitlement on the same SP), not the manager grant.
- Only genuine residual: a "manager" grant on a **workspace-LOCAL (db-managed) group** that the tool
  recreates would not carry — untested, edge case, likely still account-level. Note only.

### Bug 6 — add a disclaimer so "unchanged" on account-level grants isn't mistaken for a defect
**Status:** Done (offline tested; real xlsx render verified) — 2026-08-18
**Area:** `src/reports/import_report.py` (xlsx summary footer + markdown runbook)
Since a pure account-level manager/can-use/can-manage change is invisible to the tool (no captured
signal → the row shows `Skipped (unchanged)` and NO targeted per-row message is possible), add a
**one-time static disclaimer** to the report + runbook, e.g.:
> "Account-level access-control — group **Manager**, service-principal **Can use** / **Can manage**
> (account rule-sets) — is managed at the account and is **not tracked** by this workspace-scoped
> tool. Changes to it appear here as *unchanged*. In the same account it is already in effect on the
> target; across accounts it is provisioned during account setup / Entra→SCIM."

This is the only mitigation available (a per-row note would require detecting the change, which is
deliberately out of scope). Test: the disclaimer appears once in the xlsx summary and the runbook.

---

## Bug 7 — SQL queries (and alerts) are created WITHOUT their workspace folder (`parent_path` stripped)

**Status:** Done (offline + LIVE-verified on target_ws) — 2026-08-18. The queries/alerts LIST omits
`parent_path` (verified live), so `sql_collector` now enriches it via GET-by-id
(`_enrich_query_parent_path`); `sql_importer` PRESERVES + remaps it (`_remap_parent_path`: strips the
read API's `/Workspace` prefix, remaps a recreated SP home) instead of popping it, and a missing
parent folder becomes a clean `prerequisite_missing` (`_raise_if_missing_parent`), not a raw error.
Live: a query created via the real `_query_body` landed in `/Workspace/Users/<me>`. Tests:
`test_query_is_created_in_its_source_folder_with_parent_path_normalised`,
`test_query_parent_path_remaps_a_recreated_sp_home`,
`test_a_query_under_a_missing_folder_is_a_clean_prerequisite_not_a_raw_error`,
`test_query_and_alert_v2_are_enriched_via_get_by_id`.

**EXTENDED 2026-08-19 to Lakeview dashboards + Genie spaces** (same bug — a user-created dashboard's
`.lvdash.json` was landing at the API default, not the user's folder). Both collectors already
captured `parent_path` but the EXPORT payloads dropped it and the importers never sent it. Fix: the
remap+guard helpers were promoted to `BaseImporter` (`remap_parent_path`, `missing_parent_prerequisite`,
now shared by SQL/dashboards/genie); `asset_export` carries `parent_path` in the lakeview + genie
payloads; `dashboards_importer`/`genie_importer` set+remap it on CREATE (not update — an existing
object isn't moved) with the missing-parent guard. **Live-verified on source_ws**: Lakeview create
lands the `.lvdash.json` in the user folder; Genie create HONORS `parent_path` (a space targeted at a
subfolder landed there). Ready-made test objects already exist in source_ws: `wsmig_test_dashboard`
and `wsmig_test_genie`, both at `/Users/abhishek.iyer@…`. Tests:
`test_lakeview_dashboard_is_created_in_its_source_folder`,
`test_lakeview_dashboard_under_a_missing_folder_is_a_clean_prerequisite`,
`test_genie_space_is_created_in_its_source_folder`, + export payload assertions in
`test_build_all_asset_types_and_modes`.
**Area:** `src/importers/sql_importer.py` (`_create_query` ~L135-145, alert create ~L194-203)
**Found:** incremental workspace test (idris `idris_new_query.dbquery.ipynb` — Excel `Created`, but
NOT visible in Idris's folder; and "SQL Query not created in the same directory" for the new-dir
scenarios).

### Root cause
Both `_create_query` and the alert create do `body.pop("parent_path", None)` before POST, so the
object is created at the API default location, NOT in its source workspace folder. The report says
`Created` (the query object exists), but it never appears in the user's/target directory tree.

### Fix
- Preserve `parent_path` on create and **remap** it the same way workspace content paths are remapped
  (user-home remap, `/Users/<oldAppId>`→`/Users/<newAppId>` for SP homes, etc.), so the query/alert
  lands in the correct folder.
- Guard on the parent folder existing (same user-home dependency as notebooks/files — see Bug 8): if
  the parent isn't creatable yet, report it as `prerequisite_missing`, don't silently drop the path.
- Audit every other place a query/alert id or path is referenced (dashboards, jobs) to confirm the
  folder move doesn't strand references.

### Test plan
- Live: create a query and an alert in a user folder on source → after import they appear in the
  SAME folder on target (not at the root). New-dir-with-a-query scenario places the query in the dir.

---

## Bug 8 — Content under an unprovisioned/unresolvable user or SP home fails as a raw API error, not a clean prerequisite

**Status:** Done (offline tested) — 2026-08-18. `workspace_importer._guard_home_present` short-circuits
a DESCENDANT of a home whose owner is absent on target into ONE clean `prerequisite_missing` (cached
one get-status per home root), instead of the raw DIRECTORY_PROTECTED / parent-missing errors. An SP
in the identity map is treated as present (home auto-provisioned at SP-create); a user home is probed
(provisioned only on login). Covers Bug 14 (the root-vs-descendant message split). Test:
`test_content_under_an_absent_user_home_is_a_clean_prerequisite_not_a_raw_error`.
**Area:** `src/importers/workspace_importer.py` (dir mkdirs + notebook/file import paths), report
categorisation
**Found:** RIL first-run — the dominant failure class (≈264 of 297): `DIRECTORY_PROTECTED Folder
Users is protected` (63, api_error), `RESOURCE_DOES_NOT_EXIST The parent folder /Users/<x> does not
exist` (49 notebooks + 122 files, dependency_unresolved). Verified the owners (sahil1.shinde,
abhijit1.kamble, …) are NOT in the migrated Users roster.

### Root cause & context
A user's `/Users/<email>` home is created by Databricks on first login / provisioning and **cannot
be pre-created via API** (`/Users` root is protected). When source content lives under a home whose
owner is **not in the migrated roster** (orphaned content, or user not yet provisioned/logged-in on
target), the content can't land. The tool ALREADY detects this cleanly for the home **ROOT**
(prerequisite_missing "USER HOME directory cannot be created — assign the user…"), but every
**descendant** (subdirectories via `mkdirs`, notebooks/files via import) falls through to the raw
`DIRECTORY_PROTECTED` / `parent folder does not exist` API errors — mis-categorised as
`api_error`/`dependency_unresolved` and swamping the failure list.

### Fix (report/handling; NOT auto-creating homes — that's impossible)
- When a unit's path is under a user/SP home that is **known-absent on target** (root already
  flagged prerequisite_missing, or owner not in roster/identity map), short-circuit its descendants
  with the SAME `prerequisite_missing` classification + one clear grouped message ("owner home not
  present on target; provision/assign the owner (or have them log in), then retry_mode=failed_only"),
  instead of attempting the API call and surfacing the raw error.
- Never attempt to `mkdirs` the protected `/Users` root itself (skip top-level protected roots).
- Consider a single roll-up line: "N items skipped under M unprovisioned/orphaned home dirs".

### Test plan
- Live: source content under a home whose owner is not migrated → descendants reported as
  prerequisite_missing (grouped), no raw DIRECTORY_PROTECTED/parent-missing api_errors; retry after
  the owner exists imports them.

---

## Bug 9 — Cluster policy definitions keep SOURCE ids (instance pool), so jobs/clusters fail policy validation

**Status:** Done (offline tested; policy-definition SHAPE verified live) — 2026-08-18.
`compute_importer._remap_policy_definition` parses the policy `definition` (JSON string or dict) and
remaps pinned `instance_pool_id`/`driver_instance_pool_id` (`value`/`defaultValue`/`values`) through
the pool map, on both create and update; an unresolvable pin is reported degraded. Live-verified the
real definition shape on both workspaces (per-attribute constraint dicts; the built-in
`{"type":"forbidden"}` no-value case is handled without a false warning). Tests:
`test_cluster_policy_definition_pool_id_is_remapped`,
`test_cluster_policy_with_an_unresolvable_pinned_pool_warns`. (A live fixed+value remap wasn't run —
no such policy exists to inspect and cluster-create is blocked by the ignored target networking.)
**Area:** `src/transform` / `src/importers/compute_importer.py` (cluster policy definition remap)
**Found:** RIL jobs — `INVALID_PARAMETER_VALUE Cluster validation error: Validation failed for
instance_pool_id, the value must be 0227-…-pool-2e8i6wm3 (is "0814-…-pool-l4ar18lb"); …
driver_instance_pool_id …` (2 jobs: GIS_DATA_INGESTION, jb_test_hcmp_post_job_notification).

### Root cause
The job's cluster spec pool id WAS remapped to the target pool (`0814-…`), but the **cluster
policy** that pins `instance_pool_id`/`driver_instance_pool_id` still contains the **source** pool
id (`0227-…`) — policy definitions are not being id-remapped. The policy then rejects the (correctly
remapped) job. Any policy that fixes an object id (pools, and likely node types / other ids) is
affected.

### Fix
- When importing a cluster policy, **remap object ids inside the policy definition** (instance pool
  ids via the pool map; audit for other id-bearing policy fields) using the same maps the compute/
  job importers use. Adopted/pre-existing policies: detect+warn if their pinned ids don't match the
  target (can't rewrite an adopted policy silently).
- Add a regression fixture: a policy pinning a pool id + a job using both → both remap consistently.

### Test plan
- Live: a policy pinning an instance pool + a job under it → job creates successfully; the policy on
  target references the target pool id.

---

## Bug 10 — Alert V2 create payload is missing required fields

**Status:** Done (offline + LIVE-verified on target_ws) — 2026-08-18. ROOT CAUSE confirmed live: the
Alert V2 LIST is shallow (no `evaluation`/`schedule`/`query_text`); only GET-by-id returns them. Fix:
`sql_collector._alert_v2_full` enriches each alert via GET-by-id so the exported payload carries the
required `evaluation.source.name` + `schedule` (strip is a denylist and keeps them; `_alert_v2_body`
remaps warehouse/parent and passes them through). Live: an alert created via the real `_alert_v2_body`
with evaluation+schedule succeeded. Tests: `test_alert_v2_create_body_carries_evaluation_and_schedule`,
`test_query_and_alert_v2_are_enriched_via_get_by_id`.
**Area:** `src/exporters` (alert v2 payload capture) + `src/importers/sql_importer.py` (alert v2 create)
**Found:** RIL `alert_v2` (1/1 failed): `INVALID_PARAMETER_VALUE Field 'alert.evaluation.source.name'
is required, expected non-default value (not "")`. Earlier ai27 retry: `Field 'alert.schedule' is
required…`. Same class — the create body omits required Alert V2 fields.

### Root cause (to confirm at implementation)
The exported/rebuilt Alert V2 payload does not carry all required subfields
(`evaluation.source.name`, `schedule`, …) — either the strip removed them or the collector never
captured the full modern-alert shape. Alert V2 is a newer API; the payload contract needs a fresh
pass against the live `GET`/`POST /api/2.0/alerts` schema.

### Fix
- Capture + preserve the full Alert V2 definition (`evaluation.source`, `schedule`, threshold,
  notification) on export; validate the create body against the live API schema before POST.
- Remap referenced ids (query/warehouse) as needed. Add a fixture that round-trips a real Alert V2.

### Test plan
- Live: an Alert V2 on source (with schedule + evaluation source) imports cleanly on target.

---

## Bug 11 — Serving endpoint create sends both `served_models` and `served_entities`

**Status:** Done (offline tested) — 2026-08-18. `serving_importer._config_body` now sends ONLY
`served_entities`: drops `served_models` when both are present; promotes `served_models`
(model_name/model_version → entity_*) when only it exists. Tests:
`test_serving_create_sends_only_served_entities_not_both`,
`test_serving_promotes_served_models_to_served_entities_when_only_models`. (No live create — an
external-model endpoint needs a real provider key; the API's both-rejected contract is the
RIL-documented error.)
**Area:** `src/importers/serving_importer.py` (+ export payload for serving endpoints)
**Found:** RIL `serving_endpoint` (1 failed): `BAD_REQUEST Both served_models and served_entities
cannot be provided in the config. Databricks recommends using only served_entities…`.

### Root cause
The exported config carries both the deprecated `served_models` and the current `served_entities`;
the create sends both and the API rejects it.

### Fix
- On create, send **only `served_entities`** (drop `served_models` when `served_entities` is
  present; if only `served_models` exists, map it into `served_entities`). Confirm against the live
  serving API. (This is separate from the already-known "UC-registered / external-model endpoints
  are conditional/manual" scope — this is a payload-shape defect for the ones we DO attempt.)

### Test plan
- Live: an auto-migratable (external-model) serving endpoint imports without the served_models/
  served_entities conflict.

---

## Confirmations from the RIL first-run (not new bugs)
- **Bug 1 reproduced at scale:** RIL cluster libraries 22/22 failed — 16 `INVALID_STATE … Pending`
  (the force-start race, "Pending" variant of the "Terminating" case), 3 "did not reach RUNNING
  within 15 min" (slow/again-racing starts), 2 "source cluster has no target equivalent" (library on
  a cluster deleted-in-source / not migrated). Batching by cluster (Bug 1) fixes the 17 race cases;
  the 15-min timeouts are a separate capacity/config concern.
- **Bug 4 reproduced:** the incremental notebook delete (`.../wsmig_test/sql_nb`) is correctly NOT
  deleted on target but does NOT appear anywhere in the xlsx — exactly the missing "Deleted in
  source" section. (Deletions confirmed invisible for workspace content too, not just SPs.)
- **Jobs cluster 404** (RIL, 4 jobs): `NOT_FOUND Cluster 0306-… does not exist` — jobs reference an
  `existing_cluster_id` for an all-purpose cluster that no longer exists in source (only 3 clusters
  inventoried, all created). Source-side stale reference, not a tool defect — but candidate for
  graceful handling: flag "job references a cluster absent from source" clearly rather than a raw
  404. (Discuss whether to add.)
- **Jobs SQL-endpoint 403** (RIL, 1 job; also seen on ai27): job `run_as` lacks CAN_USE on the
  referenced warehouse ON TARGET. Correct fail-soft; the tool must not auto-grant warehouse access.
  Matches the memory note; optional hint polish only.
- **5c is NOT Bug 1 / not slowness — it's target networking (verified live):** the operator started
  `mobility_sandbox_apc01` on the TARGET and it terminated with
  `NETWORK_CHECK_STORAGE_FAILURE / X_NHC_STORAGE_UNREACHABLE` — the compute node timed out reaching
  Azure blob storage (`arprodcindiaa6.blob.core.windows.net`, …). So the "did not reach RUNNING in 15
  min" library failures are the target cluster **unable to start** (private-connectivity / NCC /
  firewall to storage), NOT the force-start race and NOT generic slowness. Customer/infra action on
  the target. Bug 1 (start-once) is still valid but is SECONDARY here — libraries can't install until
  the target clusters can actually start. (The 5-min clean start the operator saw earlier was on
  SOURCE.)

## Bug 12 — Job `run_as` warehouse-access 403 is a first-run ORDERING issue (+ graceful handling agreed)
**Status:** Done (offline tested) — 2026-08-18. Implemented option (c): a warehouse-403
("not authorized to use or monitor this SQL Endpoint") now gets a precise `_ERROR_MAP` hint —
EXPECTED on first run, the CAN_USE grant is applied in the final ACL phase after jobs, re-run
retry_mode=failed_only — and is filed `prerequisite_missing` (so failed_only re-attempts it), keeping
the verbatim server message (IMP-2). The tool still never auto-grants warehouse access. Reorder
options (a)/(b) not taken (the ACL-phase-last ordering is by design; retry self-heals). Test folded
into `test_classify_error_always_surfaces_the_actual_server_message`.
**Area:** phase order (`src/importers/phases.py`), `src/importers/jobs_importer.py`, error hinting
**Found:** RIL job "New Job Apr 20" → `403 … piyush.rohida is not authorized to use or monitor this
SQL Endpoint`. **Verified:** piyush IS a migrated target user, AND the target "Serverless Starter
Warehouse" ACL shows `Created — 2 explicit grants applied` (incl. All-workspace-users CAN_USE). But
ACLs are applied in the FINAL phase, AFTER jobs are created — so at job-create time the run_as had no
warehouse grant yet → 403. So this is NOT "customer must grant access"; it should **self-heal on
`retry_mode=failed_only`** after the full run.
- **Fix options (discuss):** (a) apply referenced-object grants (warehouse CAN_USE) BEFORE creating
  jobs, or (b) auto-retry job creates after the ACL phase, or (c) at minimum, message it as
  "expected on first run — the warehouse grant is applied later; re-run retry_mode=failed_only",
  not as a hard permission error. Note the circular-order constraint (ACLs are last by design).
- **Graceful handling (agreed) for the sibling cases:** job referencing a cluster ABSENT from
  source (§4b, RIL "0306-102643-cws4vbmj") → clear "job references a cluster that no longer exists on
  source; repoint or recreate" instead of a raw 404; keep the warehouse-403 hint precise.

### Test plan
- Live: a job whose run_as relies on a warehouse all-users grant → after a full run + retry, the job
  creates; the message on the first pass explains the ordering rather than implying missing access.

## Bug 13 — Cluster-library inventory includes libraries on ephemeral / non-migrated clusters
**Status:** Done (offline tested) — 2026-08-18. `_install_library` now raises `SkippedNoObject`
(→ `skipped_no_object`, not FAILED) with the LOCKED comment when the source cluster has no target
equivalent. Test: `test_a_library_on_a_non_migrated_cluster_is_skipped_no_object_not_failed`.
**Area:** `src/collectors/misc_collector.py` (`_cluster_libraries`, ~L66) vs
`src/collectors/compute_collector.py` (`_clusters`, ephemeral filter ~L66-70)
**Found:** RIL library on source cluster `0812-101839-2ycinxqz` → "no target equivalent" (2 libs).
`_cluster_libraries` reads `libraries/all-cluster-statuses` and emits a unit for EVERY cluster with a
library, while `compute_collector._clusters` deliberately SKIPS ephemeral clusters (`cluster_source`
in JOB/PIPELINE/MODELS, or job-/dlt-/mlflow- names). So a library on an ephemeral/job cluster (or a
cluster otherwise not migrated) is inventoried but has no target cluster → guaranteed failure.

### Decision (LOCKED 2026-08-14) — KEEP but DOWNGRADE
Do NOT drop these library units. **Keep them visible in inventory/report** (inventory is the base —
don't silently drop), but **downgrade the import outcome from FAILED to `skipped_no_object`** with a
clear comment, e.g. "source cluster `<id>` is not in the migrated cluster set (ephemeral/deleted) —
library not installed." This stops the red failure without hiding the fact that the source had a
library there. Uniform treatment (no ephemeral-vs-missing branching needed for v1).

### Fix
- At import (`misc_importer._install_library`) / collection: when the library's source cluster has no
  target equivalent, record `skipped_no_object` (not FAILED) with the comment above, instead of
  raising `PrerequisiteMissing`.
- Optional (nice-to-have, not required by the locked decision): stamp the cluster's `source`/`state`
  onto the library unit AT COLLECTION (source side, while the cluster still exists) so the comment can
  say ephemeral vs deleted — because at airgap import time the cluster may be gone and unclassifiable.

### Test plan
- Offline: a library whose cluster is absent from the migrated cluster set is recorded
  `skipped_no_object` with the clear comment — NOT a FAILED row; it still appears in the report.

## Bug 14 — Content under an unresolvable home: also covers the message split (folds into Bug 8)
Confirmed with data: of the 94 directory failures, **31 are the clean `prerequisite_missing` at the
home ROOT** (`/Users/<x>`, 2 path segments) and **63 are raw `api_error DIRECTORY_PROTECTED` at
sub-paths** (depth 3-7). Same root cause, two messages — exactly the Bug 8 split (root handled,
descendants fall through). No separate fix; tracked under Bug 8.

## Bug 15 — ACL report is unreadable / unverifiable (Asset Type is always "acl", no principals shown)
**Status:** Done (offline tested; real xlsx render verified) — 2026-08-18. Implemented report-side by
having `AclImporter` build per-grant detail (`context["acl_grants"]`, using the exact PUT-body
predicates) + a `present` list on parity objects, and `import_report._render_xlsx` rendering a
dedicated "Object Permissions (ACLs)" sheet + a "Verified present" parity column.
**Area:** `src/reports/import_report.py` (the "Object Permissions (ACLs)" sheet + "ACL Parity" sheet)
**Found:** RIL review — an operator could not verify a cluster's ACLs from the report at all.

### Problems
1. Every ACL row's **Asset Type column shows `acl`**; the real resource type + name are jammed into
   the natural key as `clusters:mobility_sandbox_apc01` / `notebooks:/Users/…`. There is no way to
   filter/scan by resource type.
2. The rows only say **"N explicit grants applied"** — they never list WHICH principals got WHICH
   permission levels, so an ACL cannot be verified from the report (had to fall back to
   `GET /api/2.0/permissions/{type}/{id}` on both workspaces).
3. The **ACL Parity** `match` rows likewise show no principals — only diffs (`extra_on_target`)
   carry a principal string.

### Root design intent (the contract this violates)
The stated design from day one: **inventory is the BASE, export layers export status on the same
rows, import layers import status on the same rows.** The INVENTORY ACL sheet already does this
correctly — `inventory_view.py:309-315`, sheet "Object Permissions (ACLs)", one row per
object×principal×permission with columns **Object Type · Object · Principal · Permission ·
Inherited** (matches the operator's Image #9). The IMPORT ACL sheet is the ONE tab that broke the
contract: it collapses each object to a single `acl` row + a count, discarding the per-grant rows.

### Fix — make the import ACL sheet MIRROR the inventory ACL sheet, + an import-status column
- Emit **one row per object×principal×permission**, same columns as inventory (**Object Type ·
  Object · Principal · Permission · Inherited**), re-expanded from the bundle's `export/acls.json`
  (which already holds per-grant detail) joined to the applied result — NOT one collapsed row.
- Add an **Import Status** column per grant: `applied` / `skipped — no target object` /
  `dropped — principal not on target` / `skipped — inherited/built-in` / `failed`. This is what
  lets an operator confirm, per principal, that e.g. `user:mintu3.ghosh@ril.com=CAN_RESTART` on the
  source cluster actually landed on target — the exact question that could not be answered.
- Fix the Asset-Type-is-always-`acl` problem as a consequence (Object Type is now a real column).
- Apply the same per-principal expansion to the **ACL Parity** tab so `match` rows also name the
  principals, not just diffs.
- Keep the source→target object id on the row for a trivial API cross-check.

### Test plan
- A generated import report's ACL sheet has the SAME row granularity + columns as the inventory ACL
  sheet, plus an Import Status per grant; an operator can confirm a specific user's grant on a
  specific cluster reached target without touching the API. Parity `match` rows name the principals.

### Related verification note (not a bug)
RIL cluster-ACL question resolved as **added-after-run**: parity for that run was
`missing_on_target=0`, all 3 clusters `match`, and the principal (mintu3.ghosh) is a migrated user —
so a grant seen on source-but-not-target now was added post-run and re-applies on the next
incremental run (cluster ACLs are declarative; resolvable principal).

- **ACL directory-add test inconclusive:** the incremental "add tanveer CAN_MANAGE on
  `directories:/Users/abhishek.iyer@…`" showed `unchanged (fingerprint match)`, BUT the target has
  "Workspace access control is disabled", and the source may be the same — so the grant may not have
  persisted to be captured. **Re-test on a pair with access control ENABLED** before deciding if
  this is a fingerprint gap. (Object-ACL grants ARE in the acl unit payload, so an add should move
  the fingerprint; needs a clean-env confirmation.)

---

## Bug 16 — Cluster libraries have no existence check, so every run re-attempts them and FAILS on a stopped cluster (already-installed libs never SKIP)

**Status:** Done (offline + LIVE-verified on target_ws) — 2026-08-18. `existing_keys` now indexes
installed libraries via `GET libraries/cluster-status` per target cluster
(`_installed_cluster_library_keys` / `_installed_library_labels`), so `decide()` returns SKIP/ADOPT
for an already-installed library — no re-attempt, no force-start, no spurious FAILED. **Live-verified**:
the real bug cluster `0817-095622-6v7oxou8` reports gson `PENDING`, and the existence check detects it
present (label `maven:com.google.code.gson:gson:2.10.1`) via the real per-cluster endpoint. Test:
`test_an_already_installed_library_skips_without_starting_the_cluster`.
**Area:** `src/importers/misc_importer.py` (`existing_keys`, `_install_library`), interacts with
`src/state/state_store.py` (`decide`, L262-271)
**Config involved:** `library_force_start_clusters` (default false)
**Distinct from:** Bug 1 (per-library force-start race) and Bug 13 (ephemeral/non-migrated
clusters). This is a missing live-existence/idempotency check that makes cluster libraries
un-SKIPpable across runs.

### Symptom (live, source_ws 1234567890999999 → target 7405611870949703, direct mode)
`maven:com.google.code.gson:gson:2.10.1` on cluster `wsmig_test_cluster`:
- **First run** `20260817_095316` → FAILED (`prerequisite_missing`, cluster TERMINATED).
- **Retry** `20260817_103933` (force-start on) → **Created** — gson installed, real target_id
  `0817-095622-6v7oxou8:maven:com.google.code.gson:gson:2.10.1`, state row written.
- **Second run** `20260817_134827` (a full new run) → **FAILED AGAIN** (`prerequisite_missing`,
  cluster TERMINATED) for the *same* gson that is already installed.

Verified live: `GET api/2.0/libraries/cluster-status?cluster_id=0817-095622-6v7oxou8` returns gson
with status `PENDING` — i.e. **already registered on the cluster**. So the Second run's FAILED is
entirely spurious; the library is already there. (The operator read this as "retry isn't updating
the control table" — it IS; see root cause.)

### Root cause
`MiscImporter.existing_keys()` enumerates global-init-scripts + workspace-conf keys **but never
installed cluster libraries** (`misc_importer.py:53-70`). So for every `cluster_library` unit
`exists = key in existing` is **always `False`**, which drives `state.decide()` straight into the
"row says we made it but it's gone from target → recreate" branch on *every* run
(`state_store.py:265-268`):

```
if not exists_on_target:
    return UpsertAction.CREATE
```

`decide()` can therefore **never return SKIP** for a cluster library — the control table is written
correctly, but the SKIP path is unreachable. Consequence: a cluster library is **re-attempted on
every run**, and re-attempting calls `libraries/install`, which needs the cluster **RUNNING**. The
tool deliberately stops clusters after creating them, so it fails `prerequisite_missing` on every
run where the cluster is terminated (the normal state) — even for a library that is already
installed. A one-off forced retry "fixes" it, but the very next run un-fixes the *report* (never the
actual install).

### Fix — give cluster libraries a live existence check (agreed)
- In `existing_keys()` (or a per-unit check in `_install_library`), query the installed libraries on
  the target cluster via `GET api/2.0/libraries/cluster-status?cluster_id=<target_cluster>` and index
  by the same natural key the units use (`<source_cluster>:<library_label>` → remapped to the target
  cluster). Treat a library already present (`INSTALLED`/`PENDING`/`RESOLVING` — any registered
  state) as **ADOPT**, so `decide()` sees `exists_on_target=True` and returns SKIP when the
  fingerprint matches.
- Net effect: a library already registered on the target cluster is **not re-attempted**, does **not**
  need the cluster started, and reports `Skipped (unchanged)` / `Adopted` instead of a spurious
  FAILED. Only genuinely-missing libraries drive an install (and only those hit the force-start path).
- Keep the existing genuine-failure messages (no target cluster; cluster won't reach RUNNING; DBFS
  jar out of scope) for libraries that really aren't present.
- Coordinate with Bug 1's by-cluster batching (same importer) — the existence check should run before
  deciding a cluster needs starting at all, so a cluster whose libraries are all already present is
  never force-started.

### Test plan
- Live: install a library via a forced retry, then run a normal full import from a TERMINATED cluster
  → the library reports `Skipped (unchanged)`/`Adopted`, the cluster is **not** started, no spurious
  FAILED.
- Live: a genuinely-missing library on a terminated cluster still reports the clean
  `prerequisite_missing` (or installs under `library_force_start_clusters=true`).
- Offline: with `cluster-status` returning gson present, `existing_keys` includes the gson natural
  key and `decide()` returns SKIP on a fingerprint match; with it absent, returns CREATE.
