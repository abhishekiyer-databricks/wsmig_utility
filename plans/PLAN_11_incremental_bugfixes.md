# PLAN 11 — Incremental-run bugfixes & correctness fixes

**Status:** PLAN ONLY — not yet implemented. This is the review gate before writing the fixes.

## Context & sources
Findings come from three real campaigns against live workspaces (no laptop-run code — everything ran
as in-workspace Jobs, run-as an SPN):
1. **PLAN 10 airgap incremental campaign** (2026-08-28) — 3 runs on a shared state store; 77
   one-per-resource source changes audited. Full audit:
   `~/Downloads/wsmig_runs/airgap_incremental_test_report.xlsx`.
2. **PLAN 10.5 direct-mode campaign** (2026-08-29) — mirror of PLAN 10 in direct mode; 79/79 verdicts
   matched (BUG-1 reproduced → mode-independent).
3. **mobility-prod airgap production run** (2026-09-02) — first real customer estate (source ws
   `100000000000001` → `adb-1000000000000001.19`); surfaced Findings 8/9/10/11 from the import report
   (`~/Downloads/import_status (1).xlsx`, `manual_actions_import.md`).

Most of the user's *prior* hypotheses were **refuted** by the campaigns (ACL-only changes ARE detected,
deletes ARE surfaced, group-membership messaging IS correct) — see Appendix B. The items below are the
confirmed defects and correctness gaps.

> **Scope split (2026-09-03):** the former **Finding-6** (UC-Volume FUSE-write durability + fail-loud +
> ACL-enrichment parallelization) has **moved to `PLAN_12_optional_scale_and_durability.md`**. Its
> blocking symptom was already resolved environmentally (cluster `no_proxy` incl. the external Volume's
> ADLS/blob endpoints), so it is now optional large-scale hardening, not an import-correctness fix, and
> lives on its own track.

---

## Findings summary (severity-sorted — review order)

| ID | Severity | Area | One-line | Status |
|---|---|---|---|---|
| **BUG-1** | HIGH | import correctness | Alert V2 updates detected but dropped via the create-race adopt path → state goes permanently stale | confirmed + reproduced (both modes) |
| **Finding-9** | HIGH | import correctness / data-loss | Name-only natural keys collapse distinct same-named objects onto ONE target (queries proven; dashboards, Genie affected) + legacy_query update uses wrong verb | confirmed (mobility-prod) |
| **Finding-10** | HIGH | import correctness / lift-and-shift | Reference remap must be exact-or-fail-loud; today every site silently substitutes/drops/keeps-dangling, and jobs miss several reference types entirely | confirmed (code audit + mobility-prod) |
| **Finding-4** | MEDIUM → REQUIRED | reporting | Report must show ALL outstanding items from the state table (cumulative), not a per-run snapshot | design (to build) |
| **Finding-7** | MEDIUM | reporting correctness | The SP OAuth-secret manual unit must be KIND-scoped — no false "manual step" for account-level SPNs | confirmed (code) |
| **Finding-8** | MEDIUM | correctness / parity | Orphaned-owner handling (PLAN 9) never extended to folder-placed assets: SQL queries, Lakeview dashboards, Genie spaces | confirmed (mobility-prod) |
| **Finding-12** | MEDIUM (FEATURE) | DAB detection / config | Configurable DAB bundle-root path/pattern — path-based DAB detection must not assume the `.bundle` folder; teams that root a bundle at a plain directory (dummy-user home) go undetected today | new feature |
| **Finding-2** | LOW (messaging) | reporting | Identity notes must name WHICH sub-attribute changed and whether it was applied (per-component matrix) | confirmed (code) |
| **Finding-3** | LOW (UX) | reporting | Show `deleted_in_source` inline on each asset-type tab (today it's only on the Summary sheet) | DECIDED (customer) |
| **Finding-5** | LOW | state-schema | `_utc` suffix rename + record-metadata review of `wsmig_migration_state` | review |
| **Finding-11** | LOW | reporting/consistency | Cluster-library collection is not ephemeral-aware → noisy `skipped_no_object` rows for libs on excluded job/DLT clusters | confirmed (mobility-prod) |
| ~~Finding-6~~ | — | — | **MOVED → PLAN 12** (UC-Volume durability + fail-loud + ACL parallelization; resolved environmentally) | relocated |

## Recommended implementation order
1. **Finding-9 then Finding-10** — do 9 first (correct *which* target a reference resolves to), then
   10 (what to do when there is *no* target). They both touch `parent_path`/reference resolution.
2. **Finding-8 with Finding-9** — both touch `parent_path` for the same four folder-placed families;
   share the home-resolution seam.
3. **BUG-1** — the create-race adopt heal-in-place fix; benefits from Finding-9's existence-map fix as
   its secondary cause.
4. **Finding-7 + Finding-2** — identity/SP reporting correctness (both needed together for account SPNs).
5. **Finding-4** — cumulative Outstanding sheet (would have surfaced BUG-1's stale row).
6. **Finding-11, Finding-3, Finding-5** — reporting/consistency polish + state-schema review.
7. **Finding-12 (FEATURE)** — independent, source-side (inventory/export) only; can ship on its own
   track. Touches the same `.bundle`-literal sites the collectors use, so it is cleanest to land
   before/with Finding-8/9 (which also edit `parent_path` handling) to avoid re-touching those files.

Every finding below carries a regression test; the campaign in the last section validates them end-to-end.

---

## BUG-1 (HIGH, confirmed + reproduced) — Alert V2 updates are detected but never applied; state then goes permanently stale

### Symptom (100% proven, no ambiguity)
- Source alert `wsmig_test_alert_v2` threshold changed `0 → 1` (A1). Export fingerprint moved
  Run B→C; payload shows `double_value: 1.0`. **The change was detected.**
- Run C import recorded the alert as **`adopted`** with an **empty `target_id`**, note
  *"already existed on target (create raced the existence check) — adopted, not duplicated"*.
- **Target alert still has `threshold.double_value = 0`** (read live post-run). The edit was dropped.
- The state row for the alert was then **overwritten with the NEW fingerprint** (`sha256:035881bca…`,
  identical to the Run-C source fp) while the target stayed stale → **every future run now
  `SKIP`s on fingerprint-match → the change is permanently lost with no self-heal.**

### State-table evidence (`catalog_6_3aez8m.target_operations.wsmig_migration_state`)
```
alert_v2 | wsmig_test_alert_v2 | target_object_id=4308521176234470 | fp=sha256:035881bca | last_action=adopted
```
The row DOES carry the correct `target_object_id` — but the import path that ran neither used it
nor applied the update.

### Root cause
On the incremental run the alert took the **CREATE → `RESOURCE_ALREADY_EXISTS` → adopt** branch in
`src/importers/base_importer.py` (≈ lines 605-611):
```python
except Exception as exc:
    if is_already_exists(exc):
        self._record(unit, ACTION_ADOPTED, target_id=safe_str(existing.get(key)),  # existing.get(key) == "" here
                     note="already existed on target (create raced the existence check) — adopted, not duplicated")
        return
```
Two defects combine:
1. **The decision resolved to CREATE, not UPDATE** — `existing_keys()` for the SQL phase
   (`sql_importer.existing_keys`, the paginated `GET /api/2.0/alerts` → `{display_name: id}` map)
   did **not** contain `wsmig_test_alert_v2` at decision time (hence `existing.get(key)==""`), so
   `exists=False` and `state.decide(...)` returned CREATE even though a valid state row exists.
2. **The ALREADY_EXISTS adopt handler is a dead-end for stale objects** — unlike the true ADOPT
   branch (lines 583-586, which re-checks the stored fingerprint and calls `_do_update` when it
   moved), the create-race adopt handler (a) takes `target_id` only from the empty `existing` map
   and (b) **never calls `update_one`**, so the fingerprint-moved edit is silently discarded — and
   the state row is stamped with the new fingerprint, defeating any later retry.

`sql_importer.update_one()` for `alert_v2` (the `PATCH /api/2.0/alerts/{id}` path) is correct and
would work — it is simply never reached.

### Fix (two parts)
1. **Make the create-race adopt path heal-and-update** (primary): in the `is_already_exists`
   handler, resolve the real `target_id` from the state row / a re-query
   (`self.state.get_target_id(asset_type, key)` or a fresh `existing_keys()` for that key), and if
   the unit fingerprint differs from the stored one, call `self._do_update(unit, target_id)` before
   recording — mirroring the ADOPT-branch staleness check (lines 583-586). This makes the code
   self-healing regardless of why the existence map missed.
2. **Fix the alert existence match** (secondary, prevents the wrong CREATE decision): ensure
   `sql_importer.existing_keys()` reliably surfaces alert_v2 by the same natural key the collector
   emits (verify pagination + the DAB-stamping interaction — Run B also showed a parallel
   `wsmig_dab_alert`/`skipped (handled by DAB redeploy)` unit; confirm DAB-stamping is not
   perturbing the real alert's key/asset_type between runs). **Finding-9's full-path key + id-anchor
   removes this secondary cause directly.**

### Regression test
- Offline: a unit whose action decision is CREATE but the create raises `RESOURCE_ALREADY_EXISTS`
  AND whose stored fingerprint differs → assert `update_one` is invoked and status is `updated`
  (not `adopted`), and the state fingerprint is only advanced after a successful update.
- Live: baseline-create an alert, change its threshold, re-run → assert target threshold changes
  and status is `updated`.

### Scope check
The same create-race adopt handler is generic (all asset types). Alerts hit it because their
existence map missed; audit shows warehouses/jobs/clusters/pools/policies took the proper UPDATE
path (they update in place correctly). Fixing part 1 hardens every asset type against this class.

---

## Finding-9 (HIGH, correctness / silent data-loss) — name-only natural keys collapse distinct same-named objects onto ONE target

**Symptom (proven, mobility-prod).** 5 **distinct** source `legacy_query` objects (source ids
`460d7e83`, `307a20cf`, `87a01361`, `ff4cdc91`, `f8909c3e`) all named `"New query"` (the default
draft name) ALL resolved to the SAME target id `2ece748b`, **0 were created**, and all 5 recorded
FAILED. The visible error is a 404 (Bug A below), but the root problem is that 5 different queries
were treated as one.

### Two bugs stacked
- **Bug A (secondary — the visible 404).** `sql_importer.update_one` for `legacy_query`
  (`sql_importer.py:105-108`) does **`POST /api/2.0/sql/queries/{id}`**, which does not exist on the
  modern Queries API → `404 ENDPOINT_NOT_FOUND`. Update must be **`PATCH /api/2.0/sql/queries/{id}`**
  with an `update_mask` (`POST`-with-id was the old Redash `/preview/sql/queries/{id}` convention).
  CREATE (`POST /api/2.0/sql/queries`, no id, `sql_importer.py:138`) is correct. (`alert_v2` update
  already uses `PATCH`, legacy_alert uses `PUT` — both correct; only `legacy_query` update is wrong.)
- **Bug B (primary — silent data-loss).** Distinct objects that share a name collapse to one target.
  `sql_importer.existing_keys()` builds `{display_name: id}` (`:70-71`, alerts `:76`), and the
  collector natural key is name-based (`sql_collector` query/alert `name or oid`
  `:81,142`; `dashboards_collector` `display_name` `:17`; `genie_collector` `title` `:26`). N
  same-named source objects → ONE key → all decided adopt/update of the single existing target → **N-1
  silently lost**. Even with Bug A fixed, all 5 would `PATCH` the same object (last-write-wins).

### Which asset types are affected (audited across all collectors)
| Family | natural_key today | Unique path available? | Verdict |
|---|---|---|---|
| notebook / workspace_file / directory | **`path`** (`workspace_collector.py:24`) | — the key already IS the full path | **SAFE — no change** |
| legacy_query / alert_v2 / legacy_alert | `name or oid` (`sql_collector.py:81,142`) | YES — `parent_path` enriched (`:97-108,143`) | **COLLIDES → full-path key** |
| lakeview_dashboard | `display_name` (`dashboards_collector.py:17`) | YES — `parent_path` (`:39`) | **COLLIDES → full-path key** |
| genie_space | `title` (`genie_collector.py:26`) | YES — `parent_path` (`:45`) | **COLLIDES → full-path key** |
| job | `name` (`jobs_collector.py:19`) | **NO** — not a workspace-tree object | COLLIDES → **id-anchored (separate)** |
| dlt_pipeline | `name` (`dlt_collector.py:16`) | **NO** | COLLIDES → **id-anchored (separate)** |
| cluster / instance_pool / cluster_policy | name (`compute_collector.py:40,54,77`) | **NO** | COLLIDES → **id-anchored (separate)** |
| user / service_principal / group / secret_scope / sql_warehouse / serving | email / appId / displayName / scope / name | naturally unique per workspace | LOW risk |

### Direct answer to "can this happen for notebooks, files, dashboards, genie, …?"
- **Notebooks & workspace files: NO.** They are already keyed by the full `path`, which the workspace
  forces to be unique — this is exactly the "track by complete path, not the name" fix, already in
  place for that family.
- **Dashboard files & Genie: YES** — same bug as queries (keyed by `display_name` / `title`). They DO
  carry `parent_path`, so the full-path fix applies directly to them.
- **Jobs, DLT pipelines, clusters/pools/policies: YES in principle, but the path fix does NOT apply** —
  they are not workspace-tree objects and have no path. Duplicate names there need an **id-anchored**
  key instead (see Fix §4). Separate, lower-urgency item.

### Fix
1. **Bug A:** `legacy_query` update → `self.client.patch(f"api/2.0/sql/queries/{target_id}", {"query":
   self._query_body(payload), "update_mask": "<changed fields>"})`. Keep CREATE as POST (no id).
2. **Bug B — full-path natural key for the four workspace-tree API families** (mirror notebooks/files):
   - **Collector:** natural_key = normalized **`parent_path + "/" + name`** for `legacy_query`,
     `alert_v2`/`legacy_alert`, `lakeview_dashboard`, `genie_space` (fall back to `name` ONLY when
     `parent_path` is genuinely absent — then use §3's id-anchor to stay unique).
   - **Importer `existing_keys`:** build **`{full_path: target_id}`** (list target objects WITH their
     `parent_path`, join on the full path), not `{display_name: id}`, so adopt-vs-create no longer
     collapses. This also removes BUG-1's "existence map missed" secondary cause for alerts.
   - **Consistency:** the key is the SOURCE full path — apply the SAME remaps used elsewhere (recreated-
     SP home remap; the Finding-8 orphan-owner divert) when matching against the target. **Implement
     together with Finding-8** — both touch `parent_path` for these exact four families.
3. **Robust complement (recommended) — id-anchor on re-runs.** The state store already stores
   `source_object_id ↔ target_object_id`. Resolve the target object for UPDATE from the STATE row
   (source id → stored target id), not the name map, so (a) two objects sharing folder+name (if ever)
   and (b) empty-`parent_path` cases are still distinguished, and updates always hit the right object.
4. **Non-tree types (jobs / DLT / compute):** separate note — assess whether the customer's estate has
   duplicate job/cluster/pipeline names; if so, anchor their state key to `source_object_id` (name is
   not a safe key and there is no path). Do **not** apply the path fix to these.

### Regression tests
- Two distinct source queries with the same `display_name` in DIFFERENT folders → both migrate as
  separate target objects (2 created), NOT one overwrite.
- A changed query on re-run → status `updated` via **PATCH** (not POST), hitting the correct target id
  resolved from state.
- Same test for `lakeview_dashboard` and `genie_space` (duplicate names in different folders → both
  created).

### State-table migration caveat (mandatory)
Changing the natural_key changes the **state-store key**. Existing customer state rows keyed by the old
name won't match the new full-path key → they would look "new" and risk a re-create. Plan a one-time
**backfill/migration** of `natural_key` for these asset types (mirror the additive/migration discipline
in Finding-5 / PLAN 8 Bug 5), not just a CREATE-DDL/code change.

---

## Finding-10 (HIGH, correctness — lift-and-shift principle) — reference remapping must be exact-or-fail-loud

**Principle (customer-stated 2026-09-03).** This utility is **lift-and-shift, not cleanup.** The tool
already does the right two things: (1) it recreates each referable object on target with a new id, and
(2) it maintains `source id → natural key → target id` maps per asset type (`base_importer.remap_id`
over the per-type `*_target_ids` context maps + the state store). **The ONLY remap allowed is
`source object → the target object the tool created for it`.** If the referenced object is **not
available on source** (deleted before export, never exported, or out of scope) there is nothing
legitimate to remap to → **FAIL LOUD**. Never substitute an arbitrary object, never silently drop the
reference, never leave a dangling source id.

**The tool already has the exact discriminator** — `remap_id` returns THREE outcomes, and callers
currently collapse the last two into one "degrade + warn" path:
- `(target_id, key)` → resolved → **remap** (correct, keep).
- `("", key)` → the referenced object IS in the bundle but not yet created on target (dependency
  ordering, a deselected `import_assets` family, or a prior create failed) → **RETRYABLE** (a
  prerequisite that `retry_mode=failed_only` heals once the family lands) — NOT a silent degrade.
- `("", "")` → the referenced object is **NOT in the bundle at all** (deleted on source / never
  exported / out of scope) → nothing to remap to, ever → **HARD FAIL LOUD**.

### Audit — every reference-remap site and its current (wrong) behavior
| Site | Reference | Current behavior when unresolved | Should be |
|---|---|---|---|
| `sql_importer._remap_warehouse` (queries, alerts) `:238-251` | `warehouse_id` / `data_source_id` | **substitute the first available warehouse**, else drop; warn | in-bundle→retryable; not-in-bundle/empty→**hard fail** |
| `dashboards_importer` `:74-80` | `warehouse_id` | **substitute the first available warehouse**; warn | same |
| `genie_importer` `:74-84` | `warehouse_id` | **substitute the first available warehouse**; warn | same |
| `compute_importer` cluster `:248-251` | `instance_pool_id` / `driver_instance_pool_id` / `policy_id` | **drop the reference**; warn (degraded) | in-bundle→retryable; not-in-bundle→**hard fail** |
| `compute_importer` policy pins `:186-199` | pool id in policy `value`/`defaultValue`/`values` | **keep the source id** (values list uses `... or v`); warn | same |
| `jobs_importer._remap_new_cluster` `:148-153` | `policy_id` / `instance_pool_id` | **drop the reference**; warn | same |
| `jobs_importer` task `:123-131` | `existing_cluster_id` | **keep the source id**; warn ("FAIL AT RUN") | in-bundle→retryable; not-in-bundle→**hard fail** |
| `dlt_importer._remap_clusters` `:105-111` | `policy_id` / `instance_pool_id` | **drop the reference**; warn | same |
| `sql_importer` legacy_alert `:187-195` | `query_id` | **keep the source query_id**; warn | in-bundle→retryable; not-in-bundle→**hard fail** |

The three **warehouse substitution** rows are the clearest violation (the object silently points at a
*different* warehouse than intended — exactly the row-6 alert case, and it makes an object look
migrated when it isn't). "Drop" and "keep-dangling" are less bad but still change/break config silently.

**Identity remap is the ONE correct exception — do NOT fail-loud it.** `jobs/dlt _remap_run_as` keeps
the source appId when the SP is not in `sp_mapping` — that is **correct**, because an account-level SP's
`applicationId` is stable and identical on target (only workspace-local SPs are remapped, and those ARE
in the map). Leave run_as as-is; the mobility-prod run_as 403s were a `servicePrincipal.user`
permission issue, not a remap issue.

### GAP — jobs miss several reference types ENTIRELY (never remapped → silently point at source/absent objects)
`jobs_importer` remaps `existing_cluster_id`, `new_cluster.policy_id/instance_pool_id`, and `run_as`,
but does **NOT** remap (verified — no code path):
- **`sql_task.warehouse_id`** (SQL / dashboard / alert / file tasks carry a warehouse) → keeps the
  **source** warehouse id → the task points at a non-existent/other warehouse and fails at run.
- **`pipeline_task.pipeline_id`** (a task that triggers a DLT pipeline) → keeps the **source**
  pipeline id → triggers the wrong/absent pipeline.
- **`run_job_task.job_id`** (a task that triggers another job) → keeps the **source** job id →
  triggers the wrong/absent job.
These are arguably higher severity than the fail-loud change — they silently mis-point today. Each
must be remapped through the corresponding target-id map (`sql_warehouse` / `dlt_pipeline` / `job`)
under the same exact-or-fail-loud rule.

### Fix
1. **Add a shared `require_remap(asset_type, source_id) -> target_id`** on `base_importer`: resolve via
   `remap_id`; on `("", key)` raise a **retryable** `PrerequisiteMissing` (*"references <type> '<key>'
   which is in the bundle but not yet on target — import that family, then re-run
   retry_mode=failed_only"*); on `("", "")` raise a **hard** failure (*"references <type> id '<src>'
   which is not available on source / not in this migration — lift-and-shift does not substitute a
   different object; fix on source and re-export"*). No substitute, no drop, no keep-dangling.
2. **Replace the three warehouse substitution fallbacks** (sql `_remap_warehouse`, dashboards, genie)
   with `require_remap`. Delete the `next(iter(... .values()))` "any warehouse beats none" logic.
3. **Replace the compute / jobs / dlt drop-and-keep-source branches** with `require_remap` (the
   retryable-vs-hard split falls out of `remap_id`'s three-way return).
4. **Add the missing jobs remaps**: `sql_task.warehouse_id`, `pipeline_task.pipeline_id`,
   `run_job_task.job_id` — through their target-id maps, same rule.
5. **Empty `warehouse_id`** (the row-6 alert case): `remap_id("sql_warehouse", "")` → `("", "")` →
   **hard fail** with the "no warehouse configured on source" message. This SUPERSEDES the earlier
   "keep FAILED, don't skip" note from the row-6 analysis — it now fails loud by this general rule,
   which is exactly the customer's stated preference (a conscious red failure, not a silent substitute
   or skip).

### Dependency-order note (why retryable ≠ noise)
The tool imports in dependency order (pools → policies → clusters; warehouses before
queries/alerts/dashboards/genie; DLT/jobs later), so most in-bundle references resolve within one run.
The `("", key)` retryable case mainly arises when a family is **deselected** via `import_assets` or a
prior create failed — there, a retryable prerequisite (not a silent degrade) is correct and self-heals
on `retry_mode=failed_only`. The `("", "")` case is the genuine lift-and-shift violation → hard fail.

### Regression tests
- Alert / query / dashboard / genie referencing a warehouse **not in the bundle** → HARD FAIL (no
  substitution), actionable message. Referencing one **in the bundle but not yet created** → retryable
  prerequisite that resolves on retry.
- Cluster / job-cluster / DLT-cluster referencing a pool/policy **not in the bundle** → hard fail (not
  dropped); **in-bundle-not-yet** → retryable.
- Job with `sql_task.warehouse_id` / `pipeline_task.pipeline_id` / `run_job_task.job_id` → remapped to
  the target ids; unresolved-not-in-bundle → hard fail.
- Empty `warehouse_id` alert → hard fail with the "no warehouse on source" message.
- run_as with an **account** SP not in `sp_mapping` → left as-is (NOT failed) — appId is stable.

### Interaction with other findings
- **Supersedes** the row-6 "keep FAILED, improve message" proposal — subsumed by rule §5 (fail loud).
- **Complements Finding-9:** Finding-9 fixes *which* target object a reference resolves to (full-path /
  id-anchored keys, so the right target is found); Finding-10 governs *what to do when there is no
  target to resolve to* (fail loud, never substitute). Implement 9 before/with 10 so the resolution is
  correct before the fail-loud rule bites.

---

## Finding-4 (MEDIUM → REQUIRED) — the report MUST show ALL outstanding failures from the state table (cumulative), not a per-run snapshot
Today each run's report lists only THAT run's failures. Persistent CREATE failures happen to recur
(object still absent → re-attempted every run — why the same 4 recurred across A/B/C), but a failure
that isn't re-attempted (fingerprint stable, or a row wrongly stamped current — exactly BUG-1)
silently drops off future reports. The state table is the cumulative source of truth (`last_action`
per pair), so the report must be driven from it.

**Requirement (to build):** add a dedicated **"Outstanding — not yet successfully migrated"** sheet
to `import_status.xlsx`, sourced from the STATE TABLE for this `source_workspace_id` pair (NOT from
this run's units), so it always shows **every currently-unresolved item — old carry-overs AND new —
regardless of whether this run touched it.**
- **Includes** every row whose `last_action ∈ { failed, created_with_warning, manual,
  skipped_no_object }`. (Plain `skipped` = successfully up-to-date → excluded. `deleted_in_source`
  shown in its own section, not here. `created`/`updated`/`adopted` = done → excluded.)
- **Columns:** `asset_type, natural_key, last_action, failure_category, last_error, last_run_id,
  first_seen, last_seen`, plus a derived **Origin** column = `new this run` (if `last_run_id ==`
  current run_id) vs `carried over` (older run) so old vs new outstanding items are visually distinct.
- **Self-describing header/legend (mandatory):** the sheet must state IN THE SHEET exactly what it
  includes and what each status means, e.g.: *"Cumulative outstanding items from the migration state
  table across ALL runs for this workspace pair — everything not yet successfully migrated. failed =
  create/update errored; created_with_warning = created but degraded (fix prerequisite + re-run);
  manual = must be done by hand (AKV scope, legacy alert/dashboard, SP/secret values, repos);
  skipped_no_object = declarative unit whose target object isn't present yet. Excludes items that are
  up-to-date (skipped/created/updated/adopted). Deletes are listed separately."*
- A one-line **totals banner** ("N outstanding: X failed, Y warning, Z manual, W skipped_no_object")
  at the top of the sheet and echoed in the run log.
- Keep the existing per-run status sheets as-is; this is an ADDITIONAL always-cumulative view. It
  would also have surfaced BUG-1's stale alert had the fix (BUG-1) not first prevented the bad state.

---

## Finding-7 (MEDIUM, reporting correctness) — the SP OAuth-secret manual unit must be KIND-scoped, not blanket

**Symptom.** `identity_importer._oauth_secret_manual_units` (≈ lines 142-174) emits a
`service_principal_secret` unit with `import_action="manual"` for **every** SP whose export note is
set — i.e. whenever `has_secrets` is True **or unknown** — with **NO account-vs-workspace-local
check**. So an **account-level SPN** (Databricks-managed *account* SP **or** external UMI/Entra SP)
gets a spurious *"create a new OAuth secret on target"* manual step, even though nothing needs doing.
(This is the code that produced the blanket `service_principal_secret → manual` rows in the PLAN
10/10.5 runs.)

**Why it's wrong for account-level SPNs (same account).** The `applicationId` is stable, and the OAuth
secret (or, for a UMI, the Azure managed-identity credential) lives at the **account** level tied to
that appId. Assigning the SP to the new workspace carries the identity — anything authenticating with
that SP keeps working, so there is **nothing to re-issue**. (A UMI doesn't use client-secret auth at
all → definitely nothing.) A `manual` step here is false work, and — with Finding-4 — it would sit in
the cumulative "Outstanding" sheet **forever** (a `manual` row never clears), permanently
misrepresenting the migration as incomplete.

**Where it IS genuinely manual.** Only when the SP does NOT keep its account-level identity:
- **Workspace-local Databricks-managed SP** — recreated on target with a **NEW applicationId**; its
  source secret cannot come across (write-only + new identity) → **genuine manual re-issue**.
- **Cross-account** — the SP must be re-provisioned in the new account (new appId) → manual (part of
  the account-admin provisioning task; preflight already flags account-identity gaps).

**Deterministic discriminator (no guesswork).** Key off the SP's **classification + appId stability**:
| SP classification | Keeps its appId on target? | `service_principal_secret` unit |
|---|---|---|
| Account-level (account db-managed / UMI / Entra), **adopted same-account** | yes | **NONE** — suppress, or emit as `skipped`/informational: *"account-owned SP — applicationId stable; OAuth secret/UMI credential lives at the account level and is intact; nothing to migrate"* |
| Workspace-local db-managed (created, **new appId**) | no | `manual` — *"recreated with new applicationId <id>; create a new OAuth secret on target and update whatever authenticates with it (secret VALUE never readable via API)"* |
| Cross-account (re-provisioned in new account) | no | `manual` (account-admin provisioning task) |

Classification is already known (`_kind_of(unit)` from `meta.resourceType`, plus the import action
adopted-vs-created / appId stability), so the branch is exact.

**Also fix the `has_secrets == unknown` conservatism.** Today an *inconclusive* secrets check (the
proxy call couldn't run) still emits a hard `manual` unit. For account-level SPNs it should not emit a
manual step at all; for workspace-local SPNs where the check was inconclusive, emit it as an
**informational/`skipped` "verify"** note rather than a hard `manual` task, so an unknowable check
never manufactures false outstanding work.

**Relationship to Finding-2.** Finding-2 fixes the NOTE on the SP *identity* unit; Finding-7 fixes the
**separate `service_principal_secret` manual unit**. BOTH are required for an account-level SPN to be
free of a spurious secret manual step — Finding-2 alone leaves this unit blanket-emitting.

**Regression test.**
- An **adopted account SP** (stable appId) with `has_secrets=True` → assert **no** `manual`
  `service_principal_secret` unit is emitted (or it is `skipped`/informational, never `manual`).
- A **workspace-local db-managed SP** created with a new appId + `has_secrets=True` → assert a `manual`
  `service_principal_secret` unit IS emitted with the re-issue instruction.
- `has_secrets=unknown` on an account SP → **no** `manual` unit.

**Doc updates.** Adjust the "Not defects → SP secret cases" note (Appendix A) and the not-migrated
catalog (`[[not-migrated-catalog-for-prod-docs]]` §C) so SP-secret "manual" is scoped to workspace-local
/ cross-account only, not account-level same-account.

---

## Finding-10 addendum (HIGH, correctness — found live by the PLAN 11 Run 2 test, 2026-09-07) — a `sql_task` remaps its warehouse but NOT its nested `query.query_id`; `dbt_task.warehouse_id` also missed

**Symptom (live).** Run 2 seeded a job (`wsmig_test_f10_refjob`) with a `sql_task` carrying both a
`warehouse_id` and a `query.query_id`. On target the warehouse WAS remapped to the target warehouse,
but `sql_task.query.query_id` was left as the **source** query id — which does not exist on target, so
the task would fail at run. API-verified on the target job. (`run_job_task.job_id`,
`pipeline_task.pipeline_id` and `sql_task.warehouse_id` all remapped correctly — the three the
original Finding-10 fix covered.)

**Root cause.** `jobs_importer._remap_task_references` only iterated the three *direct* id fields
(`sql_task.warehouse_id`, `pipeline_task.pipeline_id`, `run_job_task.job_id`). It never descended into
the `sql_task` sub-objects (`query.query_id`, `alert.alert_id`, `dashboard.dashboard_id`), and never
handled `dbt_task.warehouse_id`.

**Review — does any OTHER resource type have this gap? (audited every importer.)** No. Every singleton
importer already remaps its object references exact-or-fail-loud: `dashboards_importer` (`warehouse_id`
+ `parent_path`), `genie_importer` (`warehouse_id`), `sql_importer` (query/alert `warehouse_id` +
`data_source_id`, and the **legacy alert already remaps `query_id`** via `require_remap("legacy_query")`),
`compute_importer` (pool/policy), `dlt_importer` (pipeline-cluster pool/policy), `misc_importer`
(cluster-library `cluster_id`). The gap was **jobs `sql_task` only**.

**Fix (implemented 2026-09-07, `jobs_importer._remap_task_references`, 371 offline tests green).**
- `sql_task.query.query_id` → `require_remap("legacy_query", …)` (queries ARE migrated → exact-or-fail-loud;
  a query created later / deselected resolves on `retry_mode=failed_only`, same as the other refs).
- `dbt_task.warehouse_id` → `require_remap("sql_warehouse", …)`.
- `sql_task.alert` / `sql_task.dashboard` reference a **legacy SQL alert/dashboard**, which is a MANUAL
  migration item (never recreated by this tool) — so there is nothing to remap to: the ref is left
  as-is and NAMED in a `created_with_warning` note ("re-point by hand on target"), NOT hard-failed on an
  intentionally out-of-scope object.

**Regression tests (`tests/test_plan11.py`).** `test_job_sql_task_warehouse_AND_query_are_remapped_to_the_target`,
`test_job_sql_task_query_not_in_bundle_fails_loud`, `test_job_dbt_task_warehouse_is_remapped`,
`test_job_sql_task_legacy_alert_ref_is_warned_not_failed`. **Live re-test:** PLAN 11 Run 3 (re-run the
same `wsmig_test_f10_refjob`; the referenced query exists from Run 1, so the sql_task query_id must now
resolve to the target query id).

---

## Finding-8 (MEDIUM, correctness/parity) — orphaned-owner handling (PLAN 9) was never extended to folder-placed assets: SQL queries, Lakeview dashboards, Genie spaces

**Symptom (mobility-prod airgap run `20260825_160055`).** 9 `legacy_query` units failed
`prerequisite_missing` with *"`New Query …` targets workspace folder `/Users/<owner>/Drafts`, which
does not exist on target yet — provision/assign its owner … then re-run with retry_mode=failed_only."*
The owners (`alok.gogate@ril.com`, `shreha.tiwari@ril.com`, `roshan.patidar@ril.com`) are **absent
from the migrated Users roster** (verified: none appear in the run's `Users` sheet / 356 rows) — i.e.
genuine **orphaned / deleted-in-source owners** whose objects still carry the old `/Users/<owner>/…`
owner path. The same 404-on-missing-parent would hit **Lakeview dashboards** and **Genie spaces**
placed under an orphaned home (the mobility-prod `lakeview_dashboard` failure was a `/Shared/…` repo
folder, a sibling of the same class).

This is exactly the case PLAN 9 solved **for notebooks / workspace files / directories** (divert the
deleted-owner's content to `/Users_Backup/<owner>/…` as `created_with_warning`, preserving the bytes
instead of failing). It was **never extended to the folder-placed asset families**, so those still
hard-fail.

### Root cause (verified in code)
The two families take **different code paths for the same `/Users/<owner>` decision**:
- **workspace content** (`workspace_importer`) routes every path through **`_resolve_home_target`**
  (`workspace_importer.py:255-324`), the single resolver that folds SP-home remap (IMP-6),
  present-home passthrough, **orphaned-home divert to the backup root** (PLAN 9, fires when
  `_roster_status(owner) == "absent"` and `config.imports.workspace_home_backup` is on → returns a
  `backup` `HomeResolution` → recorded `created_with_warning`), and the in-roster/unknown →
  `prerequisite` degrade.
- **folder-placed assets** — `legacy_query` + `alert_v2` CREATE (`sql_importer.py:140,206`), Lakeview
  dashboard CREATE (`dashboards_importer.py:47`), Genie space CREATE (`genie_importer.py:49`) — share
  **`remap_parent_path`** (`base_importer.py:383-404`) + **`missing_parent_prerequisite`**
  (`base_importer.py:406-414`). `remap_parent_path` does ONLY (1) `/Workspace` prefix normalization
  and (2) the recreated-SP appId home remap (`/Users/<oldAppId>` → `/Users/<newAppId>` via
  `sp_mapping`). It **never calls `_resolve_home_target`** and has **no orphaned-home divert**; when
  the create then 404s "parent does not exist", `missing_parent_prerequisite` converts it to a hard
  `PrerequisiteMissing`. So an orphaned-owner query/dashboard/genie is **never preserved** — it just
  fails, and (with Finding-4) sits in the cumulative Outstanding view until the owner is provisioned,
  which for a deleted-in-source owner **never happens**.

### Fix
1. **Lift the home-resolution decision into a shared seam.** `_resolve_home_target` and its helpers
   (`_home_present`, `_roster_status`, `_backup_path`, `_remap_home_path`, `home_owner`) live only in
   `workspace_importer`. Move them to `base_importer` (or a shared mixin/util) so **all four
   folder-placed importers and the workspace importer share ONE decision** — no second, divergent copy.
2. **Make `remap_parent_path` apply the same PLAN 9 decision to `parent_path`.** After the existing
   SP-appId remap, if `parent_path` is under `/Users/<owner>` and that home is **absent on target**:
   - owner **absent from the source roster** + `workspace_home_backup` on → remap `parent_path` to
     `<workspace_home_backup_root>/<owner>/<rest>` (default `/Users_Backup`) and have the create
     record **`created_with_warning`** with a PLAN 9-style note (*"owner `<owner>` was deleted in
     source — object preserved at `<backup_path>`; reassign to the intended owner if needed"*), instead
     of `missing_parent_prerequisite` failing.
   - owner **in-roster / unknown** → keep `prerequisite` (recovers into the real home on
     `retry_mode=failed_only` once the owner logs in / is provisioned) — unchanged, mirrors PLAN 9 §3.
3. **Ensure the backup parent folder exists before the create.** A user with ONLY a query/dashboard/
   genie (no notebooks/files) will not have had `<backup_root>/<owner>` created by the earlier
   workspace-content phase → `mkdirs` it first (the create APIs require the parent to exist; unlike
   `workspace/import`+`mkdirs`, they do not auto-provision it — which is the whole reason this family
   fails where notebooks succeed).
4. **ACLs.** These objects' ACLs key off the object id, not the path, so no ACL path-remap is needed;
   still record the divert for reporting so the customer knows where the object landed.
5. Result: the orphaned-owner case reaches parity with notebooks (preserved, `created_with_warning`),
   and no longer lingers as a permanent `prerequisite_missing` in the Finding-4 Outstanding sheet.

### Re-run consistency (MANDATORY — diverting the path without this re-creates the object every run)
Diverting `parent_path` to `/Users_Backup` changes WHERE the object lands on target, but the tool
matches a unit on its **SOURCE natural_key**, which must stay the SOURCE path (`/Users/<owner>/…`) —
never the diverted backup path. If the divert is applied without keeping matching consistent, a
diverted dashboard/genie/query's source key (`/Users/<owner>/…`) will not match the target object now
sitting at `/Users_Backup/<owner>/…` → the existence check misses → the decision goes CREATE → it
re-creates (or hits the BUG-1 create-race adopt) on **every** run. This is a real churn/duplication
trap, not hypothetical.

**The workspace importer already solves this and is the reference pattern.** `workspace_importer.
existing_keys` (`:120-141`, comment `:133-136`) keys the existence map by the **SOURCE** natural_key
but probes the **divert-RESOLVED** target path (`_resolve_home_target(path).target_path`), so a re-run
ADOPTS the already-migrated content instead of recreating it: `found[source_path] = resolved_target_path`.

**Dashboards / Genie / queries do NOT have this yet** and resolve by object id via a type-API list
(not `get-status` on a path), so the divert MUST be paired with divert-aware matching, BOTH of:
1. **Mirror `workspace_importer:137`** — key the existence map by the SOURCE full path, but resolve the
   target lookup through the SAME divert (so the object at `/Users_Backup/…` is found and adopted).
2. **Id-anchor via state (primary for these types — Finding-9 §3)** — the state row is keyed by the
   SOURCE path and stores the created object's `target_object_id`; on re-run, resolve the object
   directly by that stored id, so the path divert is irrelevant to matching. This is the robust path
   and also closes the BUG-1 create-race window.

Net: **key on the source path; resolve existence through the divert (and/or the stored target id).**
Never let the natural_key become the backup path.

### Scope
Applies to the four families that route through `remap_parent_path`: **`legacy_query` CREATE**,
**`alert_v2` CREATE**, **Lakeview dashboard CREATE**, **Genie space CREATE**. Legacy SQL **alerts (v1)**
and legacy **dashboards** are already `manual` (never created) → out of scope. Verify live that the
create APIs honor a `parent_path` pointing at a non-home folder (`/Users_Backup/…`) — `parent_path` is
already honored for these three families (PLAN 8 Bug 7 + Lakeview/Genie siblings), and the backup root
is an ordinary folder, so it should; confirm on target.

### Regression test
- Offline: a `legacy_query` / dashboard / genie unit whose `parent_path` owner is **absent-from-roster**
  + `workspace_home_backup` on → assert `parent_path` is remapped to `<backup_root>/<owner>/…` and the
  recorded status is `created_with_warning` (NOT `prerequisite_missing`).
- Offline: an **in-roster** owner whose home is absent → assert status stays `prerequisite` (recovers on
  retry), not a silent divert.
- Live: seed a query owned by a user absent from the roster; import → assert it lands under
  `/Users_Backup/<owner>/` as `created_with_warning`.

### Doc update
Add to the not-migrated catalog (`[[not-migrated-catalog-for-prod-docs]]`): once fixed, orphaned-owner
SQL queries / Lakeview dashboards / Genie spaces are **preserved in the backup root**, matching the
notebook/workspace-file behavior — not left as a `prerequisite_missing` failure.

---

## Finding-2 (LOW → messaging) — identity notes must name WHICH sub-attribute changed and whether it was applied

**Symptom (as first found):** G2/G5/G6 (add/remove a member on an **account** or **entra** group):
the group's fingerprint correctly moves → status `updated`, but the note reads *"re-applied; no
source-side change detected; workspace permissions…"* — which contradicts `updated` and doesn't
describe the member delta (workspace-local groups G9/G10 correctly say *"members added/removed: […]"*).

**Root cause (verified in code):** the account-group branch of `identity_importer.update_one`
(≈ lines 292-301) calls `_diff_note(..., include_members=False)` — it deliberately DROPS the member
component, so a membership-only change falls through to the fixed *"no source-side change detected"*
string, which is false (a change WAS detected — that's why it is on the update path).

**Key enabler — the granularity ALREADY exists (no fingerprint/schema change needed):** the
fingerprint is a single opaque hash (says "something changed", not what), BUT the tool also stores
`last_source_detail` — a JSON snapshot of `{members, entitlements, roles}` — and `_diff_note`
(identity_importer.py ≈ 868-896) diffs those THREE components **separately** vs the prior snapshot,
naming what was added / removed-in-source. `_kind_of(unit)` (from `meta.resourceType`) gives
account/entra vs workspace-local. So "what changed" AND "which identity kind" are **fully
deterministic** (set-diff + kind lookup, zero guesswork). It already works for workspace-local groups
and for users/SPs; only the account/entra branch drops the member component.

**Fix (small, localized to `identity_importer` — `update_one` account branches + `_diff_note`):**
compute the member diff for account/entra groups too (for DETECTION/reporting — members are still not
APPLIED), and compose the note per-component from the matrix below. No new data captured, no
fingerprint/schema change. First run has no prior snapshot → keep the existing neutral "baseline
captured this run" degrade (already handled).

### Note matrix — GROUPS
| Change on source | Group kind | Report status | Note to emit |
|---|---|---|---|
| entitlement add/remove | account / entra | `updated` | `entitlements added/removed: [<list>] — applied on target (workspace-scoped)` |
| membership add/remove ONLY | account / entra | `updated` (membership drives the fp; keep `updated`, do NOT claim "no change") | `membership changed in source (added/removed: [<list>]); account/Entra-group membership is account-managed — NOT migrated by the tool (present via the account group in the same account; cross-account, provision via SCIM/Entra). Workspace entitlements/ACLs re-applied.` |
| both entitlement + membership | account / entra | `updated` | both clauses joined |
| `group.manager` grant | account / entra | `skipped` | `group-manager is an ACCOUNT-level role grant (not a workspace ACL object type) — account-owned and stable; no workspace action needed in the same account; cross-account, re-establish at the account level (account-admin task)` |
| membership / entitlement change | workspace-local | `updated` | `members added/removed: [<list>] / entitlements added: [<list>] — applied on target` (already correct today) |

### Note matrix — SERVICE PRINCIPALS
| Change on source | SP kind | Report status | Note to emit |
|---|---|---|---|
| entitlement add/remove | account SPN | `updated` | `entitlements added/removed: [<list>] — applied (workspace-scoped)` |
| client_id / OAuth secret "change" | account SPN | `skipped` / n-a | `applicationId is stable and account-owned; OAuth secret is write-only (never returned by any API) — nothing to migrate; the SP is assigned to the workspace with its identity intact` |
| `servicePrincipal.user` / `servicePrincipal.manager` grant | account SPN | `skipped` | `SP user/manager is an ACCOUNT-level role grant (not a workspace ACL object type) — account-owned and stable; no workspace action needed in the same account; cross-account, re-establish at the account level (account-admin task)` |
| new db-managed SP (+ its secret) | workspace-local | `created` | `recreated with new applicationId <id>; OAuth secret cannot be exported — re-issue manually on target` |

**Reframing note (customer-agreed 2026-09-02):** the role-grant cases (G4/G8/SP1/SP7/SP14) must NOT
read as "out of scope / not supported" — that implies a gap. They are **account-level grants that are
account-owned and stable** (the same framing as account-group membership and account-SP credentials):
in the same account they persist with the identity and need no workspace-level action; cross-account
they are an account-admin re-establish task. Use the matrix wording above, not "out of scope".

---

## Finding-3 (LOW → DECIDED, reporting/UX) — surface `deleted_in_source` inline on each asset-type tab

**Current design (verified in code).** The per-asset-type tabs (`import_report.py:420-449`) are built
from **this run's units** (`rows`), whose `Import Status` is one of the 9 live statuses:
`created / updated / adopted / skipped / created_with_warning / manual / not_selected /
skipped_no_object / failed`. A `deleted_in_source` item is NOT in the current bundle, so it is held
separately in `context["deleted_in_source"]` and rendered **only** as a standalone "Deleted in source
— review" table on the **Summary** sheet + the runbook (`:360-383`). So today the **Jobs tab does NOT
show `deleted_in_source`** — that status lives only on the Summary sheet.

**Decision (customer 2026-09-03) — make the user's expectation the design.** Each asset-type tab
should show `deleted_in_source` as a first-class status alongside the others, so e.g. the Jobs tab
reads `created / updated / skipped (DAB or unchanged) / deleted_in_source / …` in one place. Implement
by injecting synthetic per-type rows for the tab from `context["deleted_in_source"][asset_type]`
(status `deleted_in_source`, note "on target but no longer in the source bundle — NOT deleted; set
`allow_deletes=true` to remove"), sourced from the STATE table (they carry `last_action=
deleted_in_source`), so the tab reflects them even on a run that didn't otherwise touch that type.
Keep the Summary "Deleted in source — review" table as the roll-up (belt-and-suspenders), and count
the injected rows only once in the per-type totals.

**Rename sub-case — DECIDED = Option A (customer 2026-09-03): treat a rename as delete-old + create-new.**
The tool matches on natural_key (name for compute/jobs/DLT/warehouses; path for workspace content) and
does NOT correlate on the stored `source_object_id`, so a rename `foo → foo_renamed` surfaces as TWO
rows — old `foo` → `deleted_in_source` (target's `foo` left in place; `allow_deletes=false`), new
`foo_renamed` → `created` — AND leaves a **duplicate on target** (old stays, new created). This is
**accepted for v1**: from the operator's view "we see the new name and no longer see the old one → treat
the old as deleted-in-source and create the new one." Fair and non-destructive (nothing is auto-deleted).
- **Display:** once deletes are inline (above), both rows co-locate on the same tab. Do NOT attempt a
  "renamed from X" cross-link — a rename is genuinely indistinguishable from delete+create without an
  id match, and the customer does not want the extra machinery for v1.
- **Explicitly NOT doing (Option B, deferred):** id-anchored true-rename handling (correlate by
  `source_object_id` → single `updated` row, rename in place, no duplicate). That is a natural extension
  of Finding-9 §3 if ever wanted, but it is OUT of scope for this plan by customer decision. Document in
  `[[not-migrated-catalog-for-prod-docs]]` that a source rename yields a target duplicate (old name
  retained) the operator resolves manually if desired.

**Interaction with Finding-4.** The cumulative Outstanding sheet is a DIFFERENT view (everything not
yet migrated, across all runs); `deleted_in_source` is explicitly excluded there (it's its own action).
This finding is about the per-run per-type tab, not the Outstanding sheet — the two are complementary.

---

## Finding-5 (LOW, state-table schema review — from the customer's read of `wsmig_migration_state`)
Actual columns (verified live): `source_workspace_id, asset_type, natural_key, source_object_id,
target_object_id, last_source_fingerprint, last_action, last_error, last_error_raw, failure_category,
last_run_id, connectivity_mode, tool_version, last_source_detail, first_seen_utc, last_seen_utc`.
- **Naming: drop the `_utc` suffix** on `first_seen_utc` / `last_seen_utc` (and the identity table's
  same two) → `first_seen` / `last_seen`. They are stored as **STRING** ISO-8601 timestamps; consider
  typing them as `TIMESTAMP` while renaming. CAVEAT: renaming a column on a live Delta table needs
  Delta column-mapping enabled (`ALTER TABLE … RENAME COLUMN`, needs `delta.columnMapping.mode=name`)
  or a create-new-column + backfill + drop migration — plan the upgrade path for tables already at
  customer sites (mirror the PLAN 8 Bug 5 additive `ALTER TABLE ADD COLUMNS` approach + a one-time
  copy), do not just change the CREATE DDL.
- **`deleted_in_source` is ALREADY a `last_action` value** — no change needed. Confirmed live: 9 rows
  this run (`ACTION_DELETED_IN_SOURCE` is in `LAST_ACTIONS`; written by the deleted-in-source pass).
- **created_at / updated_at: already covered** — `first_seen` = created_at (set once, preserved on
  merge), `last_seen` = updated_at, PLUS `last_run_id` names the run. NUANCE to document: `last_seen`
  is "last OBSERVED in a run" (it advances every run the pair is seen, even a no-op `skipped`), not
  "last MODIFIED". If a strict last-mutation timestamp is wanted, that is not separately stored today
  (could add a `last_changed` set only when `last_action`/fingerprint actually changes) — otherwise
  the two columns are sufficient record metadata.
- **Retries key off the `last_action` column.** `retry_mode` selects which `last_action` values are
  re-scoped (`src/state/state_store.py` `RETRY_BUCKETS`): `off`→∅; `failed_only`→{`failed`};
  `skipped_only`→{`skipped`,`manual`,`not_selected`,`skipped_no_object`};
  `failed_and_skipped`→{`failed`,`created_with_warning`,`skipped`,`manual`,`not_selected`,
  `skipped_no_object`}. (Consequence tied to BUG-1: a stale alert stamped `adopted` is picked up by
  NO retry_mode → another reason the create-race adopt path must heal in-place.)

---

## Finding-12 (FEATURE, MEDIUM — HIGH-impact for directory-root teams) — configurable DAB bundle-root path/pattern (don't hard-code `.bundle`)

**Motivation (customer, 2026-09-03).** DAB deployment location is a per-team convention, and this
customer has (at least) two:
- **Team A** deploys everything under **`/Shared/.bundle/…`** — the standard CLI default (a `.bundle`
  folder at the deploy root).
- **Team B** created a **dummy user** and hands that user's **home directory as the bundle root path**
  to `databricks bundle deploy` (`root_path: /Users/<dummy>@…/…`). With an explicit `root_path` the
  CLI does **NOT** create a `.bundle` folder — the deployed tree lives directly under the configured
  directory. So **there is no `.bundle` segment anywhere in Team B's paths.**

Today all **path-based** DAB detection hard-codes the literal `/.bundle/` segment, so **Team B's
bundle-deployed assets are classified as manual.** That is not cosmetic: a bundle asset misread as
manual gets **migrated file-by-file** by this tool AND its native object (dashboard/genie/alert)
**re-created on target**, duplicating what Team B's own DevOps bundle pipeline will redeploy — the
exact DAB-duplication class we already guard against for `.bundle` deployments (see
`[[importer-dab-content-and-acl-contract]]`, `[[dab-detection-and-cli-version]]`).

**In scope = ONLY the path-detected families.** Jobs (`jobs_collector._is_dab` →
`settings.deployment.kind=="BUNDLE"`) and DLT pipelines (`dlt_collector` →
`spec.deployment.kind=="BUNDLE"`) use the **reliable field signal** and are correct regardless of
deploy location — **leave them untouched** (customer-stated, and consistent with
`[[dab-detection-underreports-jobs]]`: `deployment.kind` is THE mature indicator). This feature only
changes the families that have **no field signal** and must fall back to the path:
notebooks / directories / workspace_files, Lakeview dashboards, Genie spaces, Alerts V2, legacy
dashboards — **plus** the bundle **state-file discovery** that feeds the authoritative pathless-asset
registry (below).

### The parameter (refined from the customer's proposal)
Add one source-side widget/config value — a **list of "bundle-root indicators"**, each entry being
EITHER:
- a **folder-name glob** (no leading `/`, e.g. `*.bundle`, `.bundle`) — matches when ANY path segment
  matches the glob; the bundle root is the path up to and including that segment (today's behavior); OR
- an **absolute directory prefix** (leading `/`, e.g. `/Users/dab-deployer@corp.com/prod`) — matches
  when the path is at/under that prefix; the bundle root IS that prefix.

**Default = `.bundle`** (exact segment match → byte-for-byte the current behavior). Entries containing
glob metacharacters (`*?[`) are treated as globs, so an operator can pass `*.bundle` to catch any
`*.bundle`-named folder, or a full directory for the Team-B pattern. Multiple entries are allowed
(a workspace hosting BOTH teams sets `[".bundle", "/Users/dab-deployer@corp.com"]`). This satisfies the
customer's ask — "keep a directory path OR a pattern, defaulting to `.bundle`" — with one mechanism.

> **Naming:** `dab_bundle_roots` (config field) / widget `dab_bundle_roots` (CSV). Alias-accept a
> singular value. Keep it a top-level `Config` field (it is set on the SOURCE side; import consumes the
> STAMPED flag — see §"where it's read").

### Design — centralize the literal, then make it configurable
The `/.bundle/` literal is **scattered across 7 sites** (audited live): `helpers.dab_path_info` (:43),
`workspace_collector` classification (:97) AND state-file discovery (:119-121),
`dashboards_collector` (:34), `genie_collector` (:39), `sql_collector` (:89 legacy dash, :137
alert_v2), `inventory_runner` scope label (:162), `asset_export._DAB_ROOT_SEGMENT` (:171, :202), and
`acl_importer` (:352). Do NOT patch each — **route them all through ONE helper.**

1. **Generalize `dab_path_info(path, roots=None)`** in `helpers.py` to accept the matcher list
   (default `[".bundle"]` → identical output to today). It returns
   `{"deployed_by_dab", "dab_scope", "bundle_root"}`:
   - glob entry → find the matching segment; `bundle_root` = prefix through that segment; `dab_scope`
     from the segment BEFORE it (Shared→`shared`, else `user`) — exactly as now.
   - directory-prefix entry → `deployed_by_dab=True` if under it; `bundle_root` = the prefix;
     `dab_scope` from the prefix's top segment (`/Shared…`→shared, `/Users…`→user, else `user`).
   All six collector call-sites pass `config.dab_bundle_roots` (thread it through the collectors, which
   already receive `config`). Verify the default `.bundle` path still yields identical stamping on the
   existing 280+ offline fixtures (no behavior change for Team A).
2. **State-file discovery must honor the roots too** — this is the important half, because the
   authoritative pathless-asset registry (`dab_registry`) depends on FINDING the
   `<root>/**/state/resources.json` files, and `databricks bundle deploy` writes `state/` under the
   configured `root_path` **regardless** of whether that root is `.bundle` or a plain directory. Change
   `workspace_collector` (:119-121) from `"/.bundle/" in p and "/state/" in p` to
   **`is_bundle_root_path(p, roots) and "/state/" in p`** + the same filename check. Then Team B's
   `/Users/<dummy>/…/state/resources.json` is discovered → the registry claims Team B's pathless
   clusters/warehouses/pools/scopes/serving too. (This is a strict superset; `.bundle` still matches.)
3. **`asset_export._DAB_ROOT_SEGMENT`** (:171) identifies the bundle's own SOURCE tree (`files/` +
   `state/` under the root) so it is exported as the bundle's copy, not migrated. Make it use the same
   matcher against `config.dab_bundle_roots` so Team B's tree is handled identically.
4. **Import consumes the STAMPED flag, not a re-derivation** — `deployed_by_dab`/`dab_scope` are
   written into the bundle records at inventory/export, so import already knows without re-checking the
   path. The one import site that re-derives from the literal (`acl_importer.py:352`,
   `"/.bundle/" in object_key`) must instead **read the stamped flag** off the object's record (or, if
   a record isn't in hand there, thread `dab_bundle_roots` via `config_resolved.json`). Preferring the
   stamped flag keeps the parameter a **source-side-only** setting — cleaner and avoids the target
   needing to know Team B's convention.

### Where it's read
- **Set on the SOURCE side** (widgets on `01_Inventory`/`02_Export`; a job `base_parameter` via
  `00_Install_Jobs`/`job_templates.py`). In `direct` mode 01/02 run in the target but still read the
  source — same widget. It is recorded in `config_resolved.json` + `manifest.json` for provenance.
- **Import needs no new widget** (consumes the stamped flag). Belt-and-suspenders: carry
  `dab_bundle_roots` in `config_resolved.json` so any residual path re-derivation can honor it.

### Why this is the right shape (vs alternatives considered)
- A single **list of matchers** (glob OR directory) is strictly more general than either "a pattern"
  or "a directory" alone, handles a mixed-team workspace, and defaults to today's behavior. ✅
- Rejected: **relying on the state-file registry alone** and dropping path detection — the registry is
  authoritative for pathless assets, but notebooks/dirs/files that live under Team B's root have **no**
  state-file entry of their own (they are the bundle's `files/`), so path classification is still
  needed to skip them; and dashboards/genie/alerts want the `dab_scope` label. Keep both, both
  configurable.
- Rejected: **auto-discovering roots by scanning for any `state/resources.json`** — plausible but
  risks false positives and needs a full-tree scan; an explicit operator-provided root is safer and is what
  the customer asked for. (Could be added later as an opt-in "auto-detect" that seeds the list.)

### Regression tests
- `dab_path_info("/Shared/.bundle/b/x")` with default roots → `deployed_by_dab=True, scope=shared`
  (unchanged); `"/Users/u@x/.bundle/b/x"` → user (unchanged); a non-bundle path → False (unchanged).
- With `roots=["/Users/dab@corp.com/prod"]`: `"/Users/dab@corp.com/prod/dash"` → `True` (scope user,
  bundle_root the prefix); a path outside it → False; and `.bundle` NO LONGER required to match.
- With `roots=["*.bundle"]`: a `/Shared/myteam.bundle/…` folder matches (glob), `.bundle` still matches.
- State-file discovery: a `resources.json` under a configured directory-root's `state/` is discovered
  and the registry claims that bundle's pathless cluster/warehouse (Team-B parity with `.bundle`).
- Import: a Team-B dashboard/genie/alert stamped `deployed_by_dab=True` at export is **skipped**
  (not re-created) and its ACLs ignored — same contract as a `.bundle` asset.
- Offline suite unchanged for Team A (default roots) — no fixture stamping drift.

### Doc updates
- Add `dab_bundle_roots` to the widget/config reference and `[[not-migrated-catalog-for-prod-docs]]`
  (DAB-managed content is not migrated; the marker is now configurable per deploy convention).
- Note the interaction: this only affects **path-detected** families; jobs/pipelines stay on
  `deployment.kind`.

### Finding-12 addendum (found live by the PLAN 11 Run 3 test, 2026-09-07) — CONTENT under a configured root leaked to `create`
**Symptom (live).** With `dab_bundle_roots=.bundle,/Shared/DAB_Deployments` set and a Team-B bundle
deployed at `/Shared/DAB_Deployments/wsmig_teamb` (no `.bundle`), the Team-B **native resources** were
correctly skipped (dashboard `handled by DAB redeploy`; job via `deployment.kind`), BUT the bundle's
**workspace CONTENT** — the notebook, `databricks.yml`, `state/*.json`, and the directories — was
**created on target** (`import_action=create`/`create_and_upload`), duplicating what the team's bundle
redeploys. (The `.bundle` twins were correctly `dab_redeploy`.)

**Root cause.** `asset_export.derive_import_action(unit)` decided content-skip via
`is_dab_content_path(asset_type, natural_key)` **without** the configured `roots` — so it matched only
the hard-coded `/.bundle/` fast path. `export_runner._refresh_import_actions` (the authoritative stamp)
called it without roots too. The collector DID stamp `deployed_by_dab` roots-aware, but
`derive_import_action` ignored that flag.

**Fix (2026-09-07, 373 offline tests green).** `derive_import_action(unit, roots=None)` now (a) threads
`roots` into `is_dab_content_path`, and (b) also honours the collector's already-stamped
`deployed_by_dab` flag for content types — belt-and-suspenders. `export_runner._refresh_import_actions`
passes `config.dab_bundle_roots`. Tests: `test_derive_import_action_content_honors_configured_bundle_root`,
`test_derive_import_action_content_honors_recorded_dab_flag`. **Live re-test:** PLAN 11 Run 4 (Team-B
content must now be `dab_redeploy`/skipped, not created).

---

## ~~Finding-6~~ — MOVED to `PLAN_12_optional_scale_and_durability.md`
UC-Volume FUSE-write durability + fail-loud stage boundaries + ACL-enrichment parallelization. The
blocking symptom (the customer empty bundle) was **resolved environmentally** by setting cluster `no_proxy` to
include the UC-Volume backing-storage endpoints (`*.azuredatabricks.net`, `*.databricks.azure.com`,
`169.254.169.254`, `127.0.0.1`, plus `.dfs.core.windows.net`/`.blob.core.windows.net` for the EXTERNAL
ADLS-backed staging Volume). The code hardening is now optional and tracked in PLAN 12.

---

## Finding-11 (LOW, reporting/consistency) — cluster-library collection is not ephemeral-aware, so libraries on excluded job/DLT clusters surface as noisy `skipped_no_object` rows

**Symptom (mobility-prod airgap run `20260825_160055`).** 147 `cluster_library` units reported
`Skipped (no target object)`, note *"source cluster '<id>' is not in the migrated cluster set
(ephemeral/deleted) — the library was not installed."* They are **~49 clusters × the same 3 libraries**
(`aiohttp`, `paramiko>=5.0`, `pycryptodome`) — the classic signature of an **ephemeral job cluster**
(datetime-format cluster ids like `0825-040918-07keo04b`, `cluster_source=JOB`). Only **1** all-purpose
cluster existed/migrated; the ~49 are ephemeral job clusters that happened to be running at inventory.

**These are NOT failures and nothing is lost** — the status is `skipped_no_object` (not `failed`), and
the libraries live on ephemeral job clusters that are **recreated with their own library config every
time the job / its DAB redeploy runs on target**. They were never meant to migrate as standalone
libraries. This is **reporting noise** that also inflates the `skipped_no_object` count and looks
alarming.

### Root cause (verified in code) — the ephemeral exclusion is applied to CLUSTERS but not to their LIBRARIES
- **`compute_collector._clusters()` (`:62-73`) excludes ephemeral clusters** at inventory:
  `is_ephemeral = _EPHEMERAL_CLUSTER.match(name) or cluster_source in (JOB, PIPELINE, MODELS)` →
  *"omit entirely from inventory (only all-purpose clusters are relevant)."* Correct.
- **`misc_importer` (Bug 13, LOCKED 2026-08-14) downgrades** a library whose target cluster is absent
  from FAILED → `skipped_no_object` with the "ephemeral/deleted" note (`misc_importer.py:196-203`).
  Correct — no red failure.
- **But `misc_collector._cluster_libraries()` (`:66-85`) is NOT ephemeral-aware.** It calls
  `GET /api/2.0/libraries/all-cluster-statuses`, which returns library statuses for **every** cluster
  (including the ephemeral job clusters `compute_collector` just excluded), and applies **no** filter.
  So it inventories libraries for exactly the clusters that were deliberately excluded → 147 orphaned
  `skipped_no_object` rows. The exclusion decision (Plan 1a §8) was made for clusters but never carried
  over to their libraries.

### Fix
Make `misc_collector._cluster_libraries()` apply the SAME ephemeral exclusion `compute_collector`
uses. `all-cluster-statuses` returns `cluster_id` but not `cluster_source`, so:
1. Fetch `clusters/list` (or reuse the compute collector's already-filtered set) to build the set of
   **non-ephemeral (all-purpose) cluster ids** — `cluster_source not in (JOB, PIPELINE, MODELS)` and
   name not matching `_EPHEMERAL_CLUSTER`.
2. In `_cluster_libraries`, **skip any library whose `cluster_id` is not in that set** — so libraries
   on job/DLT/model clusters are never inventoried, matching how the clusters themselves are excluded.
3. Result: those 147 don't appear at all (no confusing skipped rows, cleaner `skipped_no_object`
   count). No correctness change — they were never installable and are job/DAB-managed anyway.

Keep the `misc_importer` Bug-13 downgrade as a safety net (a genuinely deleted all-purpose cluster's
library still skips cleanly rather than failing).

### Fact vs. theory
- **Fact (code):** clusters filter ephemeral at collection (`compute_collector:66`); the library
  collector (`misc_collector:66-85`) does not.
- **Fact (report):** all 147 reference clusters not in the migrated set; ~49 clusters × the same 3 libs.
- **Strong inference:** those ~49 are ephemeral JOB clusters. Confirm on source (Ops) that those
  `cluster_id`s have `cluster_source = JOB` if a definitive check is wanted.

### Regression test
- A `cluster_library` whose `cluster_id` belongs to a `cluster_source=JOB` (or `PIPELINE`/`MODELS`,
  or `_EPHEMERAL_CLUSTER`-named) cluster is **not emitted** by `misc_collector` (0 units), while a
  library on an all-purpose cluster is emitted normally.

---

# PLAN 11 validation test — blank-workspace campaign, STOP-for-review after every run

**Status:** to run AFTER the PLAN 11 fixes are implemented. Mirrors the PLAN 10 / PLAN 10.5
methodology — real in-workspace Jobs, run-as an SPN, no laptop-run migration code — but with ONE
deliberate change: **after every run I STOP and hand the run's report to the user for review before
proceeding to the next run.** Nothing advances without an explicit "go".

**Role:** act as a human Databricks platform tester. The CLI is control-plane only (create the Git
folder, run `00_Install_Jobs`, fill widgets, trigger/monitor, transfer the bundle in airgap, read
Volumes). 0 assumptions; the goal is to prove each PLAN 11 fix behaves as specified on a real estate.

## What this campaign must prove (traceability to the findings)
| Fix | What the run must show |
|---|---|
| **BUG-1** | An alert whose threshold changed re-imports as **`updated`** (target value actually changes), never `adopted`-with-empty-target; state fingerprint advances only after the update lands. |
| **Finding-9** | Two distinct same-named queries (and a dashboard, a Genie space) in DIFFERENT folders → **both created** as separate target objects; a changed query re-imports via **PATCH** hitting the right id. |
| **Finding-10** | An alert/query/dashboard/genie referencing a warehouse **not in the bundle** → **HARD FAIL** (no silent substitution); one **in-bundle-not-yet** → retryable prerequisite that heals on `retry_mode=failed_only`. Empty `warehouse_id` → hard fail. Jobs `sql_task.warehouse_id`/`pipeline_task.pipeline_id`/`run_job_task.job_id` → remapped to target ids. |
| **Finding-8** | A query / Lakeview dashboard / Genie space owned by a user **absent from the roster** → **`created_with_warning`** under `/Users_Backup/<owner>/…`, NOT `prerequisite_missing`; and a re-run **adopts** it (no duplicate). |
| **Finding-7** | An **account** SPN produces **no** `service_principal_secret → manual` unit; a **workspace-local** db-managed SPN does. |
| **Finding-2** | Account/Entra group with a membership-only change → `updated` with the per-component note (names the member delta + the account-managed clause), not "no source-side change detected". |
| **Finding-4** | The report has an **Outstanding** sheet driven from the state table, with the Origin (`new this run` vs `carried over`) column and the self-describing legend + totals banner. |
| **Finding-11** | Libraries on ephemeral job/DLT clusters do **not** appear as `skipped_no_object` rows at all. |
| **Finding-3 / Finding-5** | Rename cross-linking (if implemented) + `_utc`-renamed columns present with a clean upgrade path. |

## Environment (verify live at pre-flight — STOP if any value is unexplained)
- **Git:** repo `https://github.com/abhishekiyer-databricks/wsmig_utility`, branch
  **`feature/ws_import`** — at the commit that contains the PLAN 11 fixes (NOT the pre-PLAN-11 code the
  PLAN 10/10.5 mirrors used). Create a Repos/Git folder on the workspace(s) needed for the mode chosen.
- **Blank target workspace — FRESH, in a NEW REGION** (user is creating it; confirmed 2026-09-03). Empty,
  so no pre-existing state/objects confound the baseline (same "blank workspace" intent as PLAN 10/10.5).
  Confirm host, the run-as target SPN (create + make workspace-admin if missing; grant the CLI user
  `servicePrincipal.user` on it so jobs can bind run_as), and a state catalog+schema + staging Volume.
- **UC prep on the new metastore — DO FIRST (new region ⇒ NEW regional metastore, mirror PLAN 10.5 step 0).**
  The estate's **DLT pipelines, Genie spaces, and AI/BI dashboards + trips-referencing alerts** reference
  UC tables **by FQN** (Genie `serialized_space` + alert `query_text` are literal FQNs; UC is out of scope
  so the tool does NOT remap them). On a fresh metastore those tables don't exist → without them DLT ×N +
  Genie + dashboards + those alerts fail `permission_denied`/table-not-found. So BEFORE any run:
  a. Read the SOURCE schemas of the referenced tables (via the source profile).
  b. CREATE the referenced catalog(s) + schema(s) + **EMPTY tables mirroring those columns** on the new
     metastore. Managed catalog first (`CREATE CATALOG …` — works if the metastore has default managed
     storage); if none, build an ADLS-backed UC stack in THIS region (access connector + storage
     credential + external location + catalog) per `[[target-ws-uc-stack-bozvdk-2026-08-18]]`.
  c. Grant the target SPN `USE_CATALOG/USE_SCHEMA/SELECT` on those (+ `CREATE/MODIFY` if DLT writes there),
     and the state schema `USE_CATALOG/USE_SCHEMA/CREATE_TABLE/MODIFY/SELECT` + Volume `READ/WRITE_VOLUME`.
  (This is env prep, not a code path — a fresh-target delta vs the mode, exactly as PLAN 10.5 handled it.)
- **Mode: `direct`** (decided with the user 2026-09-03). Rationale: `04_Import` is mode-agnostic by
  construction, PLAN 10.5 already proved direct == airgap (79/79), and every PLAN 11 fix is import-side
  or collector-side — all mode-independent. Direct is operationally simpler (ONE end-to-end job, no
  manual bundle hop) and additionally exercises the collectors reading the source over OAuth M2M (a
  superset). **If direct passes, airgap follows** — PLAN 11 changes nothing in the airgap-only file-hop
  path. All three runs are triggers of the packaged `direct_end_to_end_live` job with a fresh `run_id`
  (as in PLAN 10.5). (An airgap smoke pass is optional and only re-confirms the unchanged file hop.)
- **UC objects referenced by assets** (DLT / Genie / trips-referencing alerts) must pre-exist on the
  target metastore by FQN (UC is out of scope, not remapped) — create empty mirror tables as in PLAN
  10.5 step 0 if the estate has them.
- **State store:** ONE fresh state schema reused across ALL runs of this campaign, so UPSERT
  fingerprints drive incremental detection. Fresh `run_id` per run.

## Deliverables (all under `~/Downloads/wsmig_runs/`, `plan11_` prefix)
1. `plan11_1_import.xlsx` — Run 1 baseline import report.
2. `plan11_2_import.xlsx` — Run 2 incremental import report.
3. `plan11_3_import.xlsx` — Run 3 retry/heal import report (if a retry pass is needed to prove
   Finding-10 retryable + Finding-8 adopt-on-rerun).
4. `plan11_source_changes.xlsx` — the exact changes applied before Run 2 (cols: Case · Resource Type ·
   Resource Name · Update Done · New-or-Existing · How Applied · Capability · Timestamp · Expected ·
   Which finding it exercises).
5. `plan11_validation_report.xlsx` — per fix: expected vs actual, PASS / FAIL, with the traceability
   table above filled in from live evidence.
6. Final response per run: **job-run URLs** + **output Volume path(s)**.

## Run-by-run flow — **STOP AND WAIT FOR USER REVIEW AT EACH ▛ CHECKPOINT ▟**

### Run 1 — baseline full migration (blank target)
1. Pre-flight (environment table above): verify target host + run-as SPN; create the staging Volume +
   state schema on the target; create the Git folder; mint an M2M token against the source and read
   source SCIM (1-line control-plane curl) to confirm direct connectivity; `00_Install_Jobs`
   (`deploy_jobs=direct_end_to_end_live`, `connectivity_mode=direct`, source url/client_id, staging +
   state config). Create any UC mirror tables the estate's DLT/Genie/alerts reference (PLAN 10.5 step 0).
2. `run-now` the `direct_end_to_end_live` job (01 inventory → 02 export → 04 import all in the target,
   01/02 reading the source over OAuth M2M, bundle written straight to the target Volume) with a fresh
   `run_id` and `dry_run=false`; monitor 01→02→04.
3. Download `reports/import_status.xlsx` → `plan11_1_import.xlsx`.
4. Validate vs the live target (identities, compute, workspace content incl. any `/Users_Backup/…`,
   sql, dlt, dashboards, genie, secrets, ACL parity). Record baseline counts.
5. **▛ CHECKPOINT 1 — STOP.** Present `plan11_1_import.xlsx` + the validation summary + run URLs +
   Volume path. **Wait for the user's review and explicit go-ahead before applying any source changes.**

### Between Run 1 and Run 2 — apply the targeted source changes
Apply one change per fix from the traceability table (log each into `plan11_source_changes.xlsx`),
specifically seeding the PLAN 11 scenarios:
- **BUG-1:** change an existing alert's threshold.
- **Finding-9:** create two same-named queries in different folders (+ a same-named dashboard and Genie
  space in different folders); edit one query.
- **Finding-10:** an alert/query pointing at a warehouse that will NOT be in the bundle (out-of-scope /
  deleted) → expect hard fail; an alert pointing at an in-bundle warehouse whose family is deselected
  in `import_assets` for Run 2 → expect retryable; an alert with empty `warehouse_id`; a job with
  `sql_task.warehouse_id` / `pipeline_task.pipeline_id` / `run_job_task.job_id`.
- **Finding-8:** a query / dashboard / genie owned by a user absent from the roster.
- **Finding-7:** ensure both an account SPN and a workspace-local db-managed SPN are present.
- **Finding-2:** a membership-only change on an account/Entra group.
- **Finding-11:** confirm ephemeral job clusters with libraries exist (usually already true).

### Run 2 — incremental (same state store, fresh run_id)
6. `run-now` `direct_end_to_end_live` again with a NEW `run_id` (same target state schema, so UPSERT
   fingerprints decide create/update/skip); download → `plan11_2_import.xlsx`.
7. Validate each seeded change against the traceability table; fill `plan11_validation_report.xlsx`.
8. **▛ CHECKPOINT 2 — STOP.** Present `plan11_2_import.xlsx` + the per-fix PASS/FAIL table + URLs +
   Volume path. **Wait for the user's review before any retry/heal run.**

### Run 3 — retry/heal pass (only if needed to close Finding-10 retryable + Finding-8 adopt)
9. Re-select the deselected family (Finding-10 retryable) and/or re-run with `retry_mode=failed_only`;
   confirm the retryable prerequisites now resolve and the Finding-8 diverted objects **adopt** (no
   duplicate). Download → `plan11_3_import.xlsx`.
10. **▛ CHECKPOINT 3 — STOP.** Present the final validation report; confirm every fix is PASS or record
    the residual with RCA. **Wait for the user's sign-off to close the campaign.**

## Notes
- Reuse the SAME state schema across all runs; fresh `run_id` per run — that is what makes the UPSERT
  fingerprints exercise incremental detection.
- The tool never auto-deletes (`allow_deletes=false` default); deletes are REPORTED as
  `deleted_in_source`.
- Any deviation from the expected behavior → file it (append to this plan or open a follow-up) with RCA;
  do not silently "fix in place" during the campaign.
- Related: [[plan10-incremental-airgap-test]] [[plan10-5-direct-mode-test]]
  [[plan9-orphaned-home-backup]] [[not-migrated-catalog-for-prod-docs]].

---

# Appendix A — Not defects (documented for completeness)
- **G4/G8/SP1/SP7/SP14** — granting *group-manager* / *service-principal user/manager* roles:
  `group` and `service_principal` are not ACL object types in the bundle (`acls.json` covers
  directory/notebook/file/job/cluster/policy/pool/warehouse/dlt/dashboard/genie/alert/serving/
  secret_scope/repo), so the tool does not migrate them and correctly reports `skipped (unchanged)`.
  **Do NOT message these as "out of scope"** — they are ACCOUNT-level rule-set grants that are
  **account-owned and stable** (like account-group membership / account-SP credentials): in the same
  account they persist with the identity, cross-account they are an account-admin re-establish task.
  Use the reframed wording in the Finding-2 note matrix, not a "not supported" hint.
- **DB3/GE2** — landed on **DAB-managed** dashboard/genie whose ACLs are deliberately ignored
  (`skipped_no_object`). ACL-only detection is proven on 7 other object types (J8/J9/C4/C8/C12/
  C15/D1 all `updated`). Test-setup limitation, not a defect.
- **DB2** — the Lakeview `PATCH serialized_dashboard` marker did not persist on source (API
  normalized it away), so the dashboard was genuinely unchanged → correct `skip`. (In direct mode this
  was later re-tested with a valid dashboard edit and detection worked — see PLAN 10.5.)
- **N3** — `.dbquery.ipynb` (Unified SQL Editor) files cannot be created via the public Workspace
  import API (`BAD_REQUEST`), so the "created but not visible in target folder" hypothesis was not
  testable this run.
- **SP secret cases (SP3/4/9/10) & X4** — dropped: same Databricks account, so an account/UMI SP is
  assigned into the new workspace with its credential intact; secret values are never exported by
  design. Nothing to migrate or detect. (Finding-7 formalizes the reporting side of this.)
- **Cluster-libraries ×2 / external-model serving ×1 / control-job run_as ×1** — the same 4
  by-design/environmental failures as the baseline (need a running cluster / write-only provider
  key / an account SP not bindable on target). Not incremental defects.

# Appendix B — Refuted prior hypotheses (now behaving correctly — likely already fixed by PLAN 8)
- ACL-only changes → **detected** (`updated`), not wrongly skipped.
- Deletes (SP5/SP11/J10/N6/S2 + X2 rename) → **surfaced as `deleted_in_source`**.
- Workspace-local group member removal (G10) → *"members removed in source (retained on target —
  review)"* — correct, not the old "3/3 members added" wording.
- Notebook CONTENT edit (X1/N9) → **detected** ("64 bytes uploaded") — content migration is IN SCOPE
  and change-detected. Mechanism (not a per-run diff): the metadata payload is only
  `{path, object_type, language}`, but `content_fetcher` hashes the fetched bytes
  (`content_fetcher.py:153`) and `export_runner._apply_content_fingerprint` (`:50-69`) folds that
  `sha256` into the unit's fingerprint (input-only, not in the payload). So a code edit MOVES the
  fingerprint → import decides UPDATE → re-uploads with `overwrite=true`; unchanged content → skip. The
  hash is computed once at export over bytes we fetch anyway, so there is no extra cost. (This
  supersedes the old `[[fingerprint-blind-spot-sp-secrets]]` "notebook content silently skips" note.)
