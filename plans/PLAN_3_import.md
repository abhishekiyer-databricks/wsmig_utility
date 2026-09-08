# Plan 3 — IMPORT  (TARGET side)

> Sub-plan of `plans/PLAN_0_master.md` (master). Review gate for the **write half** of the utility.
> Scope: `00_Account_Preflight`, `04_Import`, the `src/importers/` tree, `src/state/state_store.py`,
> the dual-mode connectivity work (master §1a), and the end-to-end orchestrator (master §4a).
> Consumes the bundle Plan 2 produces; writes the **target** workspace.
>
> **Why one plan, not five.** Every phase (identity → compute → … → misc) shares the same runner,
> migration state table, checkpoint, selector, ACL pass and report. Splitting them into separate review
> gates would mean reviewing the same machinery five times and designing the id-map hand-offs
> between phases in the dark. The plan is one document; the **build** is still strictly phased and
> phase-by-phase testable (§10).

---

## 1. Objective

Take a verified bundle and make the target workspace match the source, **repeatably**:

1. **Verify the bundle** (`manifest.json` checksums) and **gate** on `00_Account_Preflight` before
   writing anything.
2. Create/update assets **in dependency order** (§6) — identity first, ACLs last — remapping every
   source reference to the corresponding **target** id as it goes.
3. Be **selectable**: the operator picks which asset families run this session (`import_assets`,
   §5), so "skip genie for now, run it next week" is a supported, first-class flow — not a code edit.
4. Be **resumable**: a crash mid-run picks up where it left off, driven by a
   `LATEST_EXPORT.json` pointer + an import checkpoint on the Volume (§3, §4).
5. Be **idempotent across runs via UPSERT**: a central **Delta migration state table** holds the
   `(source_ws_id, asset_type, natural_key) → (source_id, target_id, fingerprint)` mapping so a
   re-run **creates** what's new, **updates** what changed, and **skips** what didn't (§7 —
   including the decision on *when* to write it, which was the open question).
6. Work in **both connectivity modes** (master §1a) and, in `direct` mode, run as a **single
   end-to-end Job** with export (§8).
7. Never silently skip: every unit ends the run with an `import_status` + reason in
   `import_results.json` and the report, joinable 1:1 against `export_index.json`.

**Non-goals here:** the pre/post transform diff and the **cross-stage** three-column
inventoried→exported→imported reconciliation (Plan 4); Hive metastore, UC, MLflow, DBFS, Agent Bricks
(out of scope, master §6a).

### 1a. What import ACTUALLY outputs (your question — it is NOT "test now, report later")

To be clear, because "validation report is Plan 4" was misleading: **import produces a complete,
customer-readable output set of its own, including an Excel workbook.** What Plan 4 adds is only the
*join across all three stages*, which needs nothing new from import.

| Artifact | Written by | Contents |
|---|---|---|
| **`import_status.xlsx`** | this plan | the operator's artifact: same shape/style as `export_status.xlsx` (reuses `inventory_view` + `excel_generator`), one row per unit with **Import Status**, **Action Taken**, **Target Id**, **Note/reason**, plus a Summary sheet (per-asset-type counts, an action roll-up, a **failures table first**, and a manual-actions table) |
| `import_results.json` | this plan | machine-readable per-unit outcome — the file Plan 4 joins on `(asset_type, natural_key)` |
| `import_results.html` | this plan | the same, as a browsable page |
| **`acl_parity_report.{json,html}`** | this plan | post-apply source-vs-target ACL diff (§6b) |
| `manual_actions.md` | this plan | everything a human must do, with reasons (repos, legacy dashboards, secret values, UC prereqs) |
| `execution_import.log` | this plan | the run log (local-then-copy, Volume-safe) |
| `wsmig_migration_state` rows | this plan | durable per-object state incl. failures (§7) |
| *(Plan 4)* `validation_report` | **Plan 4** | the three-column **inventoried → exported → imported** join + the change report (created/updated/unchanged/deleted-in-source) |

So the split is: **import owns the "what happened this run" reporting** (including Excel); Plan 4 owns
the **"source vs target, end to end"** reconciliation that spans stages. Import deliberately writes
`import_results.json` in the shape Plan 4 joins on, so Plan 4 is a reader, not a retrofit.

---

## 2. Inputs, and the dual-mode preamble

**Inputs (all from the bundle dir):** `manifest.json`, `export_index.json`, `export/**` (payloads +
`content/` bytes), `export/acls.json`, `identity_classification.json`, `config_resolved.json`.
Everything the importer needs is in the bundle — **it never reads the source workspace** in
`airgap` mode, and in `direct` mode it reads the source only during the *export* stage, never
during import. Import's only live counterpart is the **target**.

**Connectivity work landed in this plan** (master §1a), because import is the first stage where the
`direct` mode changes anything structural:

| Deliverable | Detail |
|---|---|
| `config_manager` | `connectivity_mode` (`airgap`\|`direct`); `source_workspace_url`, `source_sp_client_id`, `source_sp_secret_scope`+`source_sp_secret_key` or `spn_secret_value` (§2a); `state_catalog`/`state_schema` (§7b); `staging_location` → `target_staging_location` when mode=`direct`; `validate()` per-mode; `redacted()` strips `spn_secret_value` |
| `token_manager.oauth_m2m_token_provider(host, client_id, client_secret)` | `POST {host}/oidc/v1/token`, `grant_type=client_credentials`, `scope=all-apis`; caches the token and refreshes at `expires_in − 60s`. Same `with_retry` wrapper as everything else so 429/5xx on the token endpoint is handled |
| `token_manager.build_clients(config, dbutils)` | returns `(source_client, target_client)`. `airgap`: both are the local context-token client (source side) / target client (target side). `direct`: `source_client` = M2M-bound `ApiClient` on `source_workspace_url`, `target_client` = local context-token client |
| Secret read | **two supported ways** (§2a) — direct widget, or a secret-scope pointer. Whichever is used, the value is redacted everywhere it could be persisted |
| Notebook guards | `01`/`02` assert `role=="source"` **or** (`role=="target"` and mode=="direct"); `04_Import` always asserts `role=="target"` |

**Deliberately unchanged:** `collectors/` and `importers/` both already take a `client` argument, so
the mode is invisible to them — the runner decides which client to hand over. That is the whole
point of putting the mode in one place.

**Preflight for `direct` mode** adds two checks (§9): the M2M token actually mints, and the source
SP can call an admin-only endpoint (`GET /api/2.0/preview/scim/v2/Groups?count=1`) — so a
mis-scoped SP fails in 2 seconds instead of halfway through a 40-minute inventory.

### 2a. Source SP credentials — BOTH input options supported (customer decision 2026-08-05)

**Correction — there is no naming convention to agree.** My earlier open question was wrong: the scope
and key names are just **widget inputs**, so whatever the customer already calls them works. Nothing
to standardise, nothing to wait on. Three widgets, exactly as you described:

| Widget | Required? | Purpose |
|---|---|---|
| `source_sp_client_id` | **MANDATORY** in `direct` mode | the SP's `applicationId` — not a secret |
| `source_sp_secret_scope` + `source_sp_secret_key` | optional | if **both** set → read the secret via `dbutils.secrets.get(scope, key)` in the target workspace |
| `spn_secret_value` | optional | used **only when the scope/key pair is empty** — the raw secret |

**Resolution (simple precedence, no ambiguity):**
```
if source_sp_secret_scope and source_sp_secret_key:  secret = dbutils.secrets.get(scope, key)
elif spn_secret_value:                               secret = spn_secret_value
else:                                                fail fast, naming both options
```
Scope+key wins when present, so a customer who has set up a scope can leave `spn_secret_value` blank
(or stale) with no surprise. Only one of the two paths is ever consulted, so there's never doubt
about which credential a run used. `source_sp_client_id` missing in `direct` mode is a fail-fast from
`Config.validate()`.

**The scope path is the one to prefer** (and the notebook markdown says so) because a widget value is
visible on the Job/run page and retained in run history, whereas `dbutils.secrets.get` values are
auto-redacted by Databricks in notebook output. But `spn_secret_value` is fully supported — it's
genuinely the quicker path for a first smoke test.

**Redaction is mandatory on the `spn_secret_value` path**, since it's a plain string in `Config`:
`Config.redacted()` must strip it exactly as it already strips `ctx.token`; it must never reach a
logger kwarg; and the notebook prints only the `client_id` plus which path was used. **A test asserts
the literal appears in no written artifact.**

---

## 3. `LATEST_EXPORT.json` — which bundle does import act on?

Export already writes `LATEST_INVENTORY.json` for the source side (Plan 2 §2b). Import needs the
mirror-image pointer, and Plan 2's is not usable for it: it names the run whose **inventory** is
newest, which is not necessarily one whose **export completed**.

**`<staging>/wsmig/<src_ws_id>/LATEST_EXPORT.json`**, written by `02_Export` as its **very last
action — after `manifest.json`**, so its existence proves the bundle it names is complete:

```jsonc
{
  "run_id": "20260804_140233",
  "generated_utc": "2026-08-04T14:07:11Z",
  "source_workspace_id": "1234567890123456",
  "connectivity_mode": "direct",
  "tool_version": "0.1.0",
  "manifest_checksum": "sha256:…",     // ties the pointer to THIS bundle's manifest
  "counts": { "job": 12, "notebook": 40, … }
}
```

In `airgap` mode the ops team copies the whole run dir **and** this pointer file; the
`manifest_checksum` is how import detects a pointer left over from a *different* upload (pointer
says run X, the run X dir on the target has a different manifest → refuse and name both).

**Run resolution in `04_Import`** (precedence, mirroring Plan 2 §7a so the two behave alike):
1. **`run_id` widget set** → use it verbatim (deliberate control; re-import a specific bundle).
2. Else, **latest incomplete IMPORT** for this `source_ws_id` (an `import_checkpoint.json` present
   with no `import_results.json`) → **resume it** (§4). This is what makes a plain re-run continue.
3. Else, **`LATEST_EXPORT.json`** → its `run_id`.
4. Else → **fail loudly** ("run 02_Export first, or pass run_id"). Never invent a run_id — that
   would import an empty bundle and report a spuriously clean run.

Import **prints the resolved run_id and how it was resolved**, plus the bundle's
`connectivity_mode` and export summary counts, before touching anything. In `direct` mode the
end-to-end Job also passes `run_id` through `dbutils.jobs.taskValues` (§8), so path 1 is normally
what fires and the pointer is the durable fallback.

**Bundle verification is a hard gate, not a warning.** `ArtifactWriter.verify_manifest()` runs
first; any checksum mismatch or missing file **aborts before a single write**, listing the offending
files. A partial upload must never present as a partial migration. Override widget
`skip_manifest_verify` (default `false`) exists only for a customer who deliberately hand-pruned a
bundle, and it stamps a loud warning into the report.

---

## 4. Checkpointing & idempotency (within a run)

Two distinct mechanisms, deliberately not conflated:

| | **Checkpoint** (`import_checkpoint.json`) | **Migration state table** (Delta, §7) |
|---|---|---|
| Scope | **within/across attempts of ONE run_id** | **across runs, forever** |
| Question | "did I already do this item in this attempt?" | "does this object exist on target, and has it changed since?" |
| Lives on | the staging Volume, in the run dir | Delta table in the target's UC catalog |
| Lost if deleted | wasted re-work only | **duplicate creates / lost updates** — this is the real state |

**Checkpoint mechanics** (reuses `ArtifactWriter.is_done`/`mark_done_bulk`, already built and
Volume-safe):
- Key = `component="import:<asset_type>"`, `item_key=natural_key`.
- The **outcome is stored alongside the key** (`mark_done_bulk(..., results=...)`) — Plan 2 learned
  this the hard way: resume needs each item's `target_id`/`import_status`, and reading them from
  `import_results.json` doesn't work because that file is written only at the *end*, i.e. never
  exists after a crash. The importer stores `{status, target_id, fingerprint, note}` per item.
- **Batched flushes (`CHECKPOINT_BATCH = 200`)** — every write to a UC Volume is a full-file
  rewrite (verified; see memory `uc-volume-file-io-limits`), so per-item flushing is O(n²) bytes.
  Plus a **mandatory flush at every phase boundary** and in a `finally`, so a crash can lose at
  most one partial batch of *bookkeeping* — never a created object's identity (§7 explains why).
- **Resume flow:** load checkpoint → for each unit, `is_done()` → restore the recorded outcome and
  skip the API call. Content bytes already uploaded are not re-uploaded.
- `force_full_import` widget (default `false`) ignores the checkpoint and re-evaluates every unit.
  It is **safe** — see below — just slower.

**Idempotency is stronger than the checkpoint**, and that is the load-bearing guarantee: every
importer decides by `(asset_type, natural_key)` against the migration state table *and* a live existence
check on target, so re-running any phase converges to the same target state whether or not the
checkpoint survived. The checkpoint is a *don't-waste-time* optimisation, never a correctness
crutch. Concretely, every `create_one` is preceded by:
1. control-table lookup → target_id + last fingerprint (§7 decision table), and
2. a **live existence check by natural key** on target (`existing_keys()`), which catches objects
   created outside the tool or created by an attempt that died between the API call and the
   bookkeeping write → **ADOPT** rather than duplicate.
`RESOURCE_ALREADY_EXISTS` from any create is additionally caught and downgraded to an adopt, since
a race between (2) and the create is possible.

---

## 5. Selecting what to import — the `import_assets` widget

**Requirement:** the operator must be able to skip a family (e.g. genie) now and run it later.

**Widget: `import_assets`** — a `dbutils.widgets.multiselect` over the asset families, default
**`all`**:
```
all | identity | compute | workspace | secrets | jobs | sql | dlt | dashboards | genie | serving | misc | acls
```

Design points:
- **It is separate from the `migrate_*` toggles, and narrower.** The toggles are *bundle scope*
  (set identically on both sides — what got exported at all); `import_assets` is *this session's
  work list* over what the bundle already contains. Both apply: a family absent from the bundle
  can't be imported no matter what the selector says, and import reports it as such rather than
  silently doing nothing. Keeping them separate is what lets the operator re-run a single family
  against an existing bundle without touching export config.
- `acls` is selectable **on its own** — the single most common "run it later" case, because ACL
  replay is the pass most likely to need a second attempt after identities are fixed up.
- **Dependency validation, not silent breakage.** Selecting a family whose prerequisites are
  neither selected nor already recorded in the migration state table is a **hard error listing the missing
  prerequisites** — e.g. `jobs` without `compute` present in the migration state table cannot remap
  `existing_cluster_id`. If the prerequisite is *already imported* (present in the migration state table
  for this `source_ws_id`), the id map is loaded from the table and the run proceeds normally.
  This is exactly why the migration state table stores target ids: it's what makes phase-at-a-time
  migration possible at all.
- The declared prerequisite graph (`importers/phases.py`, mirroring §6):
  `identity → {compute, workspace, secrets, jobs, sql, serving}`; `compute → {jobs, dlt}`;
  `workspace → {jobs, dlt}`; `sql → {dlt, dashboards, genie, sql}` (warehouse ids);
  `everything → acls`.
- Unselected families are recorded in `import_results.json` with
  `import_status: "not_selected"` (grey in the report) — visible as deferred work, never a gap.

---

## 6. Phase order (dependency order) and per-asset import spec

Phases run in this order; **within** a phase the order is also fixed. Each phase ends with a
control-table flush and a phase summary line in the log.

```
0  preflight gate  (00_Account_Preflight — §9)
1  identity     users → service principals → groups (empty) → group members (nested-first) → entitlements
2  compute      instance pools → cluster policies → clusters → (cluster libraries deferred to 11)
3  workspace    directories (top-down) → notebooks → files        [repos = MANUAL, §6a]
4  secrets      scopes (+ initial_manage_principal) → secret ACLs        [values = manual]
5  jobs         jobs (remap compute + workspace paths + run_as)
6  sql          warehouses → legacy queries → legacy alerts → alerts v2   [legacy dashboards = MANUAL, §6d]
7  dlt          pipelines (remap notebook paths + clusters)
8  dashboards   lakeview (remap warehouse_id)
9  genie        spaces (remap warehouse_id; serialized_space verbatim)
10 serving      external-model endpoints only (UC-backed → manual)
11 misc         global init scripts → cluster libraries (needs clusters) → workspace conf
12 acls         object permissions + the deferred principal remap  (LAST — needs every id map)
```

**Why ACLs are dead last and separate:** a grant names a *principal* (user/SP/group) **and** an
*object*, so it can only be applied once both id maps exist. Export deliberately put them in their
own `acls.json` for this reason (Plan 2 D5).

### Per-asset spec

Each row: create API → update API (the UPSERT path) → what gets remapped → notes. Update APIs are
called against the **stored `target_object_id`** (master §9), never against a source id.

| asset_type | Create | Update (fingerprint changed) | Remap before call | Notes |
|---|---|---|---|---|
| `user` | `POST scim/v2/Users` (whitelist `userName,displayName,emails,name`) | `PATCH scim/v2/Users/{id}` | — | Entra/SCIM users: **never create** — assign + entitle only (§9). Entitlements/roles are **separate PATCH passes** after create |
| `service_principal` | `POST scim/v2/ServicePrincipals` | `PATCH …/{id}` | — | Account/UMI SP → add by same `applicationId`, never create (a create mints a NEW appId and orphans every ACL). DB-managed → create → record `old_app_id → new_app_id` |
| `group` | `POST scim/v2/Groups` (**empty**) then `PATCH` members | `PATCH …/{id}` (member add/remove diff) | member ids ← user/SP/group maps, matched **by userName/appId/displayName, never source id** | **Two-pass** so nested/cross membership resolves in any order. Built-in groups (`users`,`admins`,`account users`) → `add_members` only, never create |
| `entitlement` | `PATCH` per identity | same | — | workspace-scoped. Known limitation: a role granted BOTH directly and via a group is indistinguishable via the API — only the group grant migrates (documented in the report) |
| `instance_pool` | `POST instance-pools/create` | `POST instance-pools/edit` (needs `instance_pool_id` + full config) | — | same cloud → node types kept verbatim |
| `cluster_policy` | `POST policies/clusters/create` | `POST policies/clusters/edit` | — | send `name` + `definition` (+ libraries) only |
| `cluster` | `POST clusters/create` | `POST clusters/edit` | `policy_id`, `instance_pool_id`, `driver_instance_pool_id` | **stop the cluster immediately after create** (don't burn DBUs); **re-pin** pinned clusters (`clusters/pin`); when a pool is set, strip `node_type_id`/`driver_node_type_id`/`enable_elastic_disk`; creator not preservable → `OriginalCreator` tag |
| `directory` | `POST workspace/mkdirs` | n/a (idempotent) | path (if `user_id_mapping`) | **top-down**; `/Users/<email>` home dirs **cannot be mkdir'd** — they exist once the user is provisioned (so phase 1 must precede this); skip `/Repos`, `/Projects`, Trash roots |
| `notebook` | `POST workspace/import` (base64, ≤10 MB) | same call with `overwrite=true` | path | `format=SOURCE`; oversize units were never exported (Plan 2 §5a) → `manual` |
| `workspace_file` | `POST workspace-files/import-file/{path}` (streaming) | same, `overwrite=true` | path | picks the route from the unit's recorded `content_route` |
| `repo` | **NOT IMPORTED — manual** (§6a) | — | — | Out of scope per customer decision. Export keeps the metadata as the manual runbook; import records `manual` and creates nothing |
| `secret_scope` | `POST secrets/scopes/create` | n/a (no edit API) → recreate only on explicit opt-in | `initial_manage_principal` ← identity map | `users:MANAGE` **must** be set at create, it cannot be patched later. **AKV-backed scopes need the `backend_azure_keyvault` payload** and an optional **cross-region vault remap** (see §11 D4) |
| `secret_value` | — | — | — | **always manual** (no API returns a value). One `manual_actions.md` row per scope listing its key names |
| `secret_acl` | `POST secrets/acls/put` | same (declarative) | principal ← identity map | |
| `job` | `POST jobs/create` (2.1) | `POST jobs/reset` | `existing_cluster_id`, `new_cluster.policy_id`/`instance_pool_id` across **both** `job_clusters` **and** `tasks`; notebook/file paths; **`run_as` ← SP map**; `IS_OWNER` ← identity map | **force-pause `schedule` AND `continuous`** on import (`pause_job_schedules`); a job with no `IS_OWNER` ACL is malformed → log + skip; duplicate job names disambiguated via `name:::source_id` internally and renamed back |
| `sql_warehouse` | `POST sql/warehouses` | `POST sql/warehouses/{id}/edit` | — | keep `warehouse_type` (same cloud) |
| `legacy_query` | `POST sql/queries` | `POST sql/queries/{id}` | `data_source_id`/warehouse ← warehouse map, owner ← identity map | |
| `legacy_alert` | `POST sql/alerts` | `PUT sql/alerts/{id}` | query id ← query map | |
| `legacy_dashboard` | **NOT IMPORTED — manual** (§6d) | — | — | The create endpoint is deprecated/absent on modern workspaces (verified live) → skipped by decision, reported `manual`, never attempted |
| `alert_v2` | `POST alerts` (v2) | `PATCH alerts/{id}` | warehouse ← warehouse map | |
| `dlt_pipeline` | `POST pipelines` | `PUT pipelines/{id}` | notebook paths, `policy_id`, pool ids, `run_as` | UC-referencing pipelines will fail on target (UC out of scope) → the failure carries that reason explicitly, not a raw API error |
| `lakeview_dashboard` | `POST lakeview/dashboards` | `PATCH lakeview/dashboards/{id}` | `warehouse_id` ← warehouse map | `serialized_dashboard` verbatim; UC tables must pre-exist |
| `genie_space` | `POST genie/spaces` (`create_space`, `serialized_space`) | `update_space` | `warehouse_id` | **auto-migratable** (verified live 2026-08-01). Caveat carried into the note: `serialized_space` references UC tables by FQN which must pre-exist on target |
| `serving_endpoint` | `POST serving-endpoints` | `PUT serving-endpoints/{name}/config` | — | **only** external-model endpoints; UC-model-backed → `manual` with the exported `migration_note`; skip `databricks-*` managed endpoints |
| `global_init_script` | `POST global-init-scripts` | `PATCH global-init-scripts/{id}` | — | body from the exported base64; `position` preserved |
| `cluster_library` | `POST libraries/install` | uninstall+install on change | `cluster_id` ← cluster map | **needs a RUNNING cluster** — clusters are stopped right after create (above), so libraries are recorded `deferred` with the reason rather than force-starting compute. `library_force_start_clusters` widget (default `false`) opts in |
| `workspace_conf` | `PATCH workspace-conf` | same (declarative) | — | documented key set only; never blanket-write unknown keys |
| object ACLs | `PUT permissions/{type}/{id}` | same (declarative) | principal ← identity map; `object_id` **re-resolved by path/name on target** | see **§6b** for exactly which grants are sent and why — the goal is apple-to-apple parity, verified by a post-pass diff |

### 6a. Git repos — OUT OF SCOPE (customer decision 2026-08-05)

**Import: nothing. Never attempted.** Repos are a **manual step**. The importer records every repo
unit as `import_status: "manual"` with the recreate instruction, so it stays a countable,
reconcilable line — never a silent gap.

**Export: KEEP it (recommendation), because of what it actually is.** Verified against the built
code before deciding — export captures **metadata only**:

```jsonc
// export/workspace/repos.json — one unit, this is the whole payload
{"path": "/Repos/me@co.com/my-repo", "url": "https://github.com/org/my-repo",
 "provider": "gitHub", "branch": "main", "sparse_checkout": {...}}
```
- **No file bytes, ever.** `workspace_collector` explicitly **does not descend into a git folder**
  ("its contents are the cloned repo, not ours"), so the content pass never fetches a single repo
  file. Repos cost **a few hundred bytes each** and **zero** content-fetch API calls — they are not
  why an export is slow or large.
- **The metadata IS the manual runbook.** The person doing the manual step needs exactly this list:
  which repo, which URL, which provider, which branch, at which path, and who had access (the
  ACLs). Dropping it from export would mean hand-reconstructing that list from the inventory report
  — which is the same data, one file further away.
- So the recommendation is: **`migration_mode: "manual"`** (was `auto`) and
  **`import_action: "manual"`** on every repo unit, keeping the payload. One line in
  `manual_actions.md` per repo with its URL/branch/path. Cost is negligible; the alternative is a
  worse manual step.

**Consequences to apply in the code** (small, all in export + the report):
- `asset_export._workspace_units`: repo units become `mode="manual"` unconditionally (today it's
  `auto` when a URL exists) with note `"repos are out of scope — recreate manually (Plan 3 §6a)"`.
  The existing no-URL branch already yields `manual`.
- Repo **ACLs stay in `acls.json`** but are **not replayed** — they fall under the general "skip a
  grant whose object this run did not create" guard (§6b), and the grants are surfaced in the
  manual runbook so whoever recreates the repo can reapply access.
- Inventory is **unchanged** — repos remain fully inventoried (customer explicitly wants them
  visible), including the `is_git_folder` detection fix from Plan 1a.
- `migrate_workspace` still governs the family; there is no separate repo toggle to add.

> **If you'd rather drop repos from export entirely,** the only change is emitting an index row with
> no payload (the `dab` pattern). Say so and it's a two-line change — but then the manual runbook
> loses the URLs/branches and someone rebuilds that list by hand.

### 6b. Object ACLs — what "drop" meant, and the apple-to-apple goal

Fair challenge. **The goal is exactly what you said: ACLs match apple-to-apple between source and
target**, and the plan is now written to that standard, with a verification pass to prove it (§11).
Clarifying the two words:

**"Drop" = do not SEND in the PUT body. Not "delete from target", and not "exclude from the
report".** `PUT permissions/{type}/{id}` is **declarative and absolute**: the body you send becomes
the object's complete explicit ACL, so anything omitted is *removed*. That makes the exact body
contents load-bearing — which is why each omission needs a real reason. Every grant stays visible in
`acls.json` and in the report regardless.

| Grant | Sent? | Why | Net effect on parity |
|---|---|---|---|
| **`inherited: true`** | **No** | These are not the object's own ACL — they're a *computed echo* of an ancestor's grant that the API returns on GET as read-only. The target recomputes them from its own tree, and a directory's grants are themselves being migrated. Sending them **fails or silently creates a spurious explicit grant** that source didn't have — i.e. sending them is what *breaks* parity. | **Parity preserved.** Inherited access reappears on target because the *ancestor's* explicit grant is migrated. |
| **`admins` group** | **No** | `admins` is a **built-in, workspace-local** group that always exists on target with unconditional admin. It's never created by identity import (already handled: `BUILTIN_GROUP` class), so there is no source→target id to remap. It typically appears as an implicit `CAN_MANAGE` the API reports but rejects on write. | **Parity preserved** — target admins already have this access by construction. |
| **`/Shared` root ACL** | **No** | The API rejects writes to it (immutable). Attempting it is a guaranteed failure, not a migration. | Unchangeable on either side; recorded as `manual` if source differs from the default. |
| **Object this run didn't create/adopt** | **No** | Can't apply a grant to an object that doesn't exist on target. `.bundle/` content is the **main** case but not the only one — see the enumeration below. | Reported as `skipped_no_object` **with which case applied**; parity is restored once the object exists (bundle redeploy, or the manual step). |
| **Everything else** — every explicit user/SP/group grant at every permission level | **YES, verbatim** (principal remapped) | This is the actual ACL. | The apple-to-apple content. |

**How we prove parity rather than assert it.** Because the above is a judgement call per category, the
ACL phase ends with a **post-apply diff** (`acl_parity_report.{json,html}`): re-GET
`permissions/{type}/{id}` for every object touched, normalise both sides (resolve principals through
the identity map, sort, **drop `inherited` on both sides so like is compared with like**), and diff
against the exported source ACL. Output per object: `match` / `extra_on_target` /
`missing_on_target`, with a workspace-wide count. Anything not `match` is listed with the principal
and level. This is the check that turns "we think ACLs match" into evidence, and it's the one report
to read after an import.

**"Object this run didn't create/adopt" — the full enumeration (your question 3).** You're right that
`.bundle/` is the case that motivated the rule, and it's the biggest one (44 of 148 units on fvm1, all
23 bundle directories carrying grants). But it is **not the only** one, which is why the guard is
written as a general predicate — "was this object created or adopted in this run?" — rather than a
`.bundle/` path check. If it were path-based, each case below would have become its own silent bug:

| Case | Why the object isn't on target | Frequency |
|---|---|---|
| **`.bundle/` content** | deliberately skipped; the customer's `bundle deploy` recreates it | **the main case** |
| **Git repos** | now out of scope (§6a) — never created, but their ACLs were still collected | every workspace with repos |
| **Legacy SQL dashboards** | create endpoint gone (§6d) | wherever they exist |
| **Any unit that FAILED earlier in this run** | e.g. a cluster whose create errored — its ACL grant has no object to attach to | any run with a failure |
| **Units skipped by `import_assets`** | operator ran `acls` alone, or deferred a family — those objects may not exist yet | whenever phases are run separately |
| **Oversize notebooks** (>10 MB) | never exported, so never created (Plan 2 §5a) | rare |
| **UC-backed serving endpoints** | `manual` — can't be recreated | wherever they exist |

The 4th and 5th rows are the ones that make a path-based check inadequate: they're *dynamic* — the same
object is creatable on one run and absent on another. Hence the rule is **"skip a grant whose target
object this run did not create or adopt, and say which case applied"**, and each skipped grant is
reported as `skipped_no_object` with the reason. Parity is restored on the run where the object appears.

### 6b-i. ACLs get their OWN state rows, so a skipped grant is retryable (your point 3 — YES)

**Yes — and this closes a hole the plan had.** Until now ACLs were only going to appear in
`acl_parity_report` + `import_results.json`; the migration state table had no row for them. That would
have made a skipped grant **invisible to `retry_mode`** — the very units most likely to need a second
pass (the whole reason ACLs are separately selectable in `import_assets`) would have had no state to
query. Fixed:

**ACLs are first-class state rows**, using the same table and the same PK shape:

| Column | Value for an ACL row |
|---|---|
| `asset_type` | **`acl`** (one row per **object**, not per grant — see below) |
| `natural_key` | `<perm_object_type>:<target-object natural_key>` e.g. `clusters:etl-cluster`, `directories:/Shared/finance` |
| `source_object_id` / `target_object_id` | the object's source id / its resolved target id |
| `last_source_fingerprint` | hash of the object's **normalised grant set** → an ACL changed on source moves it, so re-runs replay only genuinely-changed ACLs |
| `last_action` | `created` (applied) / `skipped` / `failed` / **`skipped_no_object`** / `manual` |
| `failure_category` | for `skipped_no_object`, **which of the seven cases applied** (`dab_redeploy`, `repo_out_of_scope`, `legacy_dashboard`, `unit_failed_earlier`, `family_not_selected`, `oversize`, `uc_backed`) |
| `last_error` | the human-readable reason + remediation |

**One row per object, not per grant.** `PUT permissions` is declarative over the whole object, so the
object is the unit of work — a per-grant row would imply we can retry a single grant, which the API
doesn't allow, and would multiply the table by ~4–10x for no gain. The individual grants live in
`acls.json` (source) and `acl_parity_report` (verification); the state row tracks *did this object's ACL
get applied, and if not why*.

**What this buys, concretely:** after the customer's `bundle deploy` lands, or a repo is recreated, or a
deferred family is imported, **`retry_mode=failed_and_skipped` + `import_assets=acls`** replays exactly
the grants that were previously skipped — no full re-run, nothing else touched. And
`SELECT * FROM wsmig_migration_state WHERE asset_type='acl' AND last_action='skipped_no_object'` answers
"which permissions are still outstanding, and why" across every workspace pair.

**`skipped_no_object` is deliberately its own `last_action`, not `failed`.** It isn't an error — the
object legitimately doesn't exist yet (usually by design, e.g. DAB). Filing it as `failed` would make
every bundle-using workspace show permanent red. It sits in the `skipped_only` retry bucket, which is
where "take it up later" belongs.

**One genuinely unfixable case, stated plainly** (inherited from `databrickslabs/migrate`, master
§10a): a role granted **both** directly *and* via a group is indistinguishable through the API — only
the group grant migrates. It surfaces in the parity report as `missing_on_target` with that reason
rather than being quietly written off.

### 6c. AKV-backed secret scopes — same vault, warn-and-continue (D4)

Per the customer: **link the scope to the Azure Key Vault it already points at; if that isn't
possible, warn and proceed.** No remap in v1.

```
for each secret_scope unit:
    if backend_type == AZURE_KEYVAULT:
        aad = mint_aad_token(app_id="2ff814a6-3304-4ab8-85cb-cd0e6f879c1d")   # run-as identity, headless
        POST secrets/scopes/create  { scope, scope_backend_type: "AZURE_KEYVAULT",
                                      backend_azure_keyvault: { resource_id, dns_name },  # verbatim from source
                                      initial_manage_principal: <remapped> }   # Bearer = aad
        on failure → import_status="failed", note=<which step + exact remediation>, CONTINUE
    else:                                    # DATABRICKS-backed (normal Databricks token)
        POST secrets/scopes/create  { scope, initial_manage_principal: <remapped> }
    always → emit a manual_actions.md row listing this scope's KEY NAMES (values are never exportable)
```

**Two implementation traps, both verified live** (memory `fvm1-test-fixtures-and-akv-state`) — these
are why AKV gets its own section:

**1. Yes — creating a Databricks secret scope that is BACKED BY an Azure Key Vault requires an Azure
AD (Entra) token, not a Databricks token.** Confirming your reading, precisely:

- This is **only** about the *linking* call — `POST /api/2.0/secrets/scopes/create` with
  `scope_backend_type=AZURE_KEYVAULT`. Databricks has to prove to Azure that the *caller* is allowed
  to read that vault, so it needs an identity Azure recognises. A Databricks OAuth/context token
  carries **no Azure AD identity**, so the call fails with the (unhelpful) error
  `"must have userAADToken defined!"`.
- What works: an **AAD access token for the AzureDatabricks first-party app
  `2ff814a6-3304-4ab8-85cb-cd0e6f879c1d`**, sent as the Bearer on that one call. Verified end-to-end
  on fvm1.
- **Databricks-backed scopes are unaffected** — normal Databricks token, no Azure involvement.
- Note this is **separate from vault permissions**: the AAD token is *who is asking*; the vault
  access policy / RBAC grant is *whether they're allowed*. Both are needed.

**Your proposal is exactly right and is now the plan:** if the run-as SP is an **Azure managed
identity / Entra SP**, it can mint that AAD token itself, headlessly, via client-credentials —
no `az login`, no laptop. Ask the customer to grant that identity **`get` + `list` on the Key
Vault's secrets** (an access policy or the *Key Vault Secrets User* role — on the **vault**, not the
Databricks scope; the Databricks scope is what we're creating). Then:

```
try:    aad_token = mint_aad_token(app_id="2ff814a6-…")     # client-credentials, run-as identity
        POST secrets/scopes/create  (Bearer = aad_token, backend_azure_keyvault{...})
except: import_status = "failed",  note = <which step failed + the exact remediation>
        CONTINUE to the next scope        # never aborts the run
```
So: **attempt it, and on any problem fail that scope, record it, report it, keep going.** The
`note` distinguishes the two causes so the customer knows what to fix — *"could not mint an AAD
token: the run-as identity is not an Entra SP/managed identity"* vs *"AAD token OK but the vault
refused: grant `<identity>` get/list on `<vault dns_name>`"*. Preflight probes the token-mint path
up front (§9) so this is known before the secrets phase rather than during it.

`mint_aad_token` reuses the `direct`-mode client-credentials code (§2), pointed at Azure AD instead
of the workspace `/oidc` endpoint — a small helper, not a new subsystem.

**2. `initial_manage_principal` must be set at create.** `users:MANAGE` cannot be patched
afterwards; getting it wrong means deleting and recreating the scope.

**Cross-region reality check, recorded so nobody is surprised:** carrying the vault over verbatim
means the region-2 workspace reads a **region-1 vault**. That works if the target workspace identity
is granted access and the network path allows it, and it is the customer's stated preference — but
it leaves a cross-region dependency, which the preflight flags as a WARN (§9) and the report states
plainly rather than burying.

### 6d. Legacy SQL dashboards — SKIPPED (customer decision 2026-08-05)

**Terminology first, since I used jargon:** "RPC" (remote procedure call) was just a sloppy way of
saying **"the API endpoint"**. Concretely: `POST /api/2.0/preview/sql/dashboards` — the create call
for the *old* Redash-style SQL dashboards — **no longer works on modern workspaces**. Verified live
while building the fvm1 fixtures: a legacy SQL dashboard could not be created on a fresh workspace at
all, which is why there's no live fixture for it (memory `fvm1-test-fixtures-and-akv-state`). The
read/list side still works, so these dashboards are inventoried and exported fine; only *creation* is
gone. Databricks' replacement is **AI/BI (Lakeview) dashboards**, which we do migrate automatically.

**Decision (as instructed — skip it since the API doesn't support creation):** the importer **does not
attempt** legacy SQL dashboards. Each unit is recorded `import_status: "manual"` with the note
*"legacy SQL dashboard creation is not supported by the API on modern workspaces — recreate as an
AI/BI dashboard, or migrate the underlying query and rebuild"*, plus one row in `manual_actions.md`
carrying its name, its underlying query names, and its warehouse — enough to rebuild without going
back to the source.

**Why skip beats attempt-and-fail:** attempting produces a red `failure` on every run forever, which
trains the operator to ignore red. A `manual` with a reason is honest and stays actionable. The
**underlying `legacy_query` objects still migrate normally**, so the data half is preserved and only
the visual layout is rebuilt by hand.

**`import_action` is the branch, never `migration_mode`.** Export leaves DAB-owned workspace
content at `migration_mode: auto/content` on purpose (so it keeps travelling with its ACL grants)
and signals the skip **only** via `import_action: "dab_redeploy"`. An importer that branched on
`migration_mode` would import bundle state files and corrupt the customer's next
`databricks bundle deploy` — it maps resources to **source-workspace object ids** (verified live).
This is asserted in a unit test, not just documented.

---

## 7. The migration state table (Delta) — and WHEN to write it

`src/state/state_store.py`, table FQN built from the `state_catalog` / `state_schema` /
widgets (§7b) — **one catalog+schema shared across all workspace pairs, assumed to exist**; the tool
owns the table names. One row
per migrated object:

| column | purpose |
|---|---|
| `source_workspace_id`, `asset_type`, `natural_key` | **PK** |
| `source_object_id`, `target_object_id` | both ids — the target id is what the UPDATE path calls |
| `last_source_fingerprint` | change detection (sha256 of the normalized payload, Plan 2 §5) |
| `last_action` | `created`\|`created_with_warning`\|`updated`\|`skipped`\|`adopted`\|`failed`\|`manual`\|`not_selected`\|`skipped_no_object`\|`deleted_in_source` — the vocabulary the three `retry_mode` buckets query (§7d) |
| `last_error` | **human-readable** cause + remediation (raw API text kept in `last_error_raw`) |
| `failure_category` | `prerequisite_missing`\|`api_error`\|`dependency_unresolved`\|`permission_denied`\|`not_supported`, plus the seven `skipped_no_object` sub-cases for ACL rows (§6b-i) — makes "show me every pair blocked on a prerequisite" a SQL query |

**Rows exist for EVERY unit, not just successes** — including `acl` rows (§6b-i), `manual` units, and
families skipped by `import_assets`. That is what makes `retry_mode` (§7d) a query rather than a guess:
a unit with no row is invisible to a retry.
| `last_run_id`, `first_seen_utc`, `last_seen_utc` | audit + the change report |
| `connectivity_mode`, `tool_version` | provenance |

### 7.1 The identity-map table — written the moment each SP/group is created

Plus a second, tiny table for the **persistent identity map** — `old_app_id → new_app_id`,
`old_group_id → new_group_id`.

**Yes, and this is a correctness requirement, not a nicety** (your point 3). It is written **during
phase 1, per identity, on the same cadence as the main table** (batched + flushed at the phase
boundary + `finally` + recovery replay, §7a) — **never** deferred to the end of the run. Reasoning:

- **Every later phase reads it.** Job `run_as`, job `IS_OWNER`, secret-scope
  `initial_manage_principal`, group membership, and *every* ACL grant remap resolve through this
  map. It is the single most-read structure in the run.
- **Losing it is the worst failure in the tool.** A Databricks-managed SP recreated on target gets a
  **brand-new `applicationId`**. If that mapping isn't durable, the next run cannot recognise the SP
  it already created → it **creates a second one**, and every ACL that pointed at the first is now
  attached to an orphan. Unlike most assets, you cannot recover the mapping by re-reading the target,
  because the new appId has no visible link back to the source appId — the map *is* the only record.
  That asymmetry is precisely why identity is phase 1 and why its map is flushed hardest.
- **Phase-boundary flush is therefore mandatory here specifically**: phase 1 must end with a
  complete, queryable map before phase 2 starts, which is also what makes `import_assets=identity`
  as a standalone run useful.

**What gets a row** (every mapping any later phase might need to resolve):

| entity | key | value | when written |
|---|---|---|---|
| Databricks-managed SP | source `applicationId` | **new** target `applicationId` + target SCIM `id` | immediately on create |
| Account/UMI SP | source `applicationId` | **same** appId + target SCIM `id` | on assign/adopt — the appId is unchanged but the **target SCIM `id` differs**, and ACL/permission calls need that id, so the row is still required |
| Databricks-managed group | source `displayName` | new target group `id` | on create (before the member PATCH) |
| Built-in group (`users`/`admins`/`account users`) | `displayName` | target group `id` | on adopt — never created, but still needed for remap |
| User | `userName` (email) | target SCIM `id` | on assign/adopt — email is stable, the id is not |

So it is not only "new SPNs created on target" — **every** identity the run touches gets a row,
including the ones it deliberately does *not* create, because the **target-side id** always differs
even when the natural key doesn't. An adopt-without-recording would leave later ACL remaps unable to
resolve the principal.

`identity_map.json` in the bundle stays the **per-run view** (human-readable, travels with the
bundle); the table is the **durable cross-run truth**. On a re-run the table is loaded first and the
JSON is regenerated from it, so the two can't drift.

**Decision table** (master §9): no row + not on target → **CREATE**; no row + on target →
**ADOPT** (record, then compare); row + unchanged fingerprint → **SKIP**; row + changed →
**UPDATE against `target_object_id`**; row whose key is absent from this bundle →
**`deleted_in_source`, reported, never auto-deleted** (opt-in `allow_deletes`, default `false`).

### 7a. When to write it — the open question, answered

**Recommendation: write it INCREMENTALLY during the run — batched, with a mandatory flush at every
phase boundary — never only at the end.** Concretely:

**CONFIRMED by the customer (2026-08-05): periodic writes, and any failure flushes before it
propagates.** The cadence below is the agreed behaviour.

```
per object   → outcome appended to an in-memory pending list + the checkpoint JSON (cheap)
every 200    → MERGE the pending batch into the Delta migration state table
phase end    → MANDATORY MERGE (flush) before the next phase starts
ON ANY FAILURE → FLUSH FIRST, then let the error propagate/record (see below)
run end      → final MERGE in a `finally:`, so even an aborted run persists what it did
run start    → RECOVERY REPLAY: any checkpoint outcome not yet in the table is merged first
```

**Flush-on-failure, concretely** (the customer's explicit requirement — implemented at three levels
so no failure shape can bypass it):
1. **Per-object failure** (an API error on one asset): the failure outcome itself is recorded to the
   pending list, and — because the *preceding* successes in that batch are what matter — a flush is
   forced when a failure occurs and the pending list is non-empty. So a poison asset can never
   strand the 199 successes before it.
2. **Phase-level abort** (an unexpected exception escaping an importer): `try/finally` around each
   phase flushes both tables before the exception continues upward.
3. **Run-level abort** (`KeyboardInterrupt`, job timeout, driver kill where Python still unwinds):
   outermost `try/finally` in `import_runner.run()` flushes, then writes a partial
   `import_results.json` marked `run_status: "aborted"` so the run is visibly incomplete rather
   than looking clean.
A hard driver kill (SIGKILL / OOM) unwinds nothing — that is the case the **startup recovery
replay** from the checkpoint covers, which is why both durability layers exist.

**Why not end-of-run only.** The migration state table is not a report — it is *the* record of which target
object corresponds to which source object. If the run dies at 90% and nothing was written, the next
run has no `target_object_id` for ~everything it created. That is not merely "redo some work": it is
**wrong behaviour**. It cannot take the UPDATE path (it doesn't know which target object to edit),
so a later source edit silently fails to propagate, and for any asset whose natural key isn't
enforced-unique by the API it creates a **duplicate**. A migration tool whose state is only durable
if the run completes has no usable state at all.

**Why not per-object either.** Each Delta `MERGE` is a transaction that rewrites data files and adds
a log entry; at ~1s and one commit per object, 5,000 objects is hours of pure bookkeeping plus a
transaction log that needs frequent compaction. Same shape as the export checkpoint problem, where
per-item flushing measured ~3.7 GB of rewrites vs ~20 MB batched (memory:
`uc-volume-file-io-limits`).

**Why the batch is safe** — this is the crux: the **checkpoint JSON is the per-item durability
layer**, and it records the same `(natural_key, target_id, fingerprint, status)` tuples the table
needs. So the true window of loss is not "one batch of created objects" but "one batch of Delta
rows that can be **replayed from the checkpoint**". The startup **recovery replay** does exactly
that, which is why the checkpoint stores outcomes rather than bare done-keys (§4). Two cheap
durable writes beat one expensive one.

**Why the phase-boundary flush is mandatory and not just "nice".** Phase N+1 *reads* the table for
its id map (§5 makes single-phase runs a supported flow), so an unflushed phase means the next
phase remaps against a stale map. Flushing at boundaries also means a phase is atomic-ish from the
operator's point of view: `import_assets=identity` finishes with a complete, queryable identity map.

**Residual risk, stated plainly:** an object created on target in the instant between the API call
returning and the checkpoint write can end up in neither store. That is unavoidable without
distributed transactions across the Databricks API and Delta, and it is exactly what the **live
existence check + `RESOURCE_ALREADY_EXISTS` adopt** in §4 exists to absorb: the next run finds the
orphan by natural key and adopts it instead of duplicating it. Belt and braces, by design.

**Also:** the table is written under `dry_run` too — but to `last_action="dry_run_*"` rows in a
**separate table** (`wsmig_migration_state_dryrun`) so a rehearsal can never pollute the real map. Simpler
than a `dry_run` column, and it makes "drop the rehearsal state" a one-line `DROP TABLE`.

### 7c. What the fingerprint DOES and DOESN'T catch (your question 1 — a real gap found)

**Short answer: it catches any change to the *migratable payload*, and your specific example — a
client id/secret created for an existing SPN — is NOT caught today.** I checked the built code rather
than assuming.

**How it works** (`transforms.fingerprint`): `sha256` over the canonical JSON of the **stripped
create payload** — order-normalised, server ids/timestamps/state removed. So it changes **iff a field
that we actually send to the target's create/update API changes**. That's the right definition: it
answers "would the target object be different?", not "did anything at all change on source".

Caught, therefore: a renamed/edited cluster policy definition, a new job task, a changed schedule, an
added group member, an edited entitlement, a new warehouse size, an edited notebook's bytes (content
units fingerprint the content), a changed `serialized_dashboard`/`serialized_space`. **Even a
one-character change** — it's a hash, so there is no threshold.

**NOT caught — and this is the gap your example exposes:**

| Source change | Caught? | Why |
|---|---|---|
| **OAuth client secret added to an existing SPN** | **NO** | `has_secrets` is collected by `identity_collector._sp_has_secrets` but is **not part of the SP's export payload** — it's used only to emit a note. The payload is the stripped SCIM object, which is byte-identical before and after a secret is created. So the fingerprint doesn't move, the state store says SKIP, and the note never resurfaces on a later run. |
| Secret *values* inside a scope | NO — by design | never readable via any API; the scope's key-name list *is* fingerprinted, so an **added/removed key** is caught, a changed **value** is not |
| Things stripped as runtime state | NO — by design | ids, timestamps, cluster state, `num_active_sessions`, etc. Deliberate: they'd re-fingerprint every run and cause endless phantom updates |

**Fix (folded into this plan, small):** include `has_secrets` in the SP unit's **fingerprint input**
(not in the create payload — it isn't a create field). Concretely, fingerprint the SP over
`{**stripped_scim, "_has_secrets": bool}` so flipping false→true moves the hash → the state store
reports **updated** → the SP's manual-action row ("OAuth client secret(s) present — NOT exportable;
recreate on target manually") is re-emitted on that run instead of being silently skipped. Same
one-line pattern is available for any future "metadata that matters but isn't a create field".

> **Worth being explicit about what this fix does and doesn't do:** it makes the *manual action
> resurface*. It cannot migrate the secret — client secrets are never readable — so the customer still
> recreates it by hand. The value is that the report tells them to, on the run where it became true.

### 7c-audit. COMPLETE fingerprint audit across every asset type (2026-08-05)

You asked for a full assessment rather than the one SP case. I audited **every** `asset_type` by
reading `_make_unit`, each unit builder in `asset_export.py`, `STRIP_FIELDS`, and each collector's
record — comparing **what the collector knows** against **what actually reaches `fingerprint()`**.
The rule being tested: *if a source-side change wouldn't move the hash, the state store says SKIP and
that change never reaches the target.*

**Found 4 real gaps, one of them serious.** The SP secret was the smallest of them.

#### 🔴 GAP 1 (SERIOUS) — notebook / workspace-file CONTENT is not fingerprinted at all

`_workspace_units` builds the notebook payload as **`{path, object_type, language}`** and
`_make_unit` hashes *that*. The content pass then sets `content_ref`/`export_status` on the unit but
**never touches `fingerprint`** (verified: no `fingerprint` assignment anywhere in
`content_fetcher.py`, `export_runner.py`'s content pass, or `parallel.py`).

**Consequence:** **edit a notebook's code on source, re-export, re-import → fingerprint identical →
SKIP → the target keeps the OLD code.** For a migration utility whose main job is moving notebooks,
and whose whole selling point is repeatable re-runs picking up source changes, this is the most
damaging silent failure in the tool. It would present as a completely clean, all-green re-run.

**Fix (must land with this plan):** hash the **bytes**. `FetchResult` gains
`content_sha256`, computed in `content_fetcher` where the bytes are already in memory (no extra
API call, no extra read); the runner then sets
`unit["fingerprint"] = fingerprint({**payload, "_content_sha256": sha})`
for every `content` unit, **after** the content pass. The checkpoint's `pending_results` must carry
`content_sha256` too, or a resumed unit would rebuild with the metadata-only hash — the exact class of
bug §4 already warns about.

> Cheap and exact: the bytes are in hand, so it's one `hashlib.sha256(data).hexdigest()` per file.

#### 🟠 GAP 2 — SP `has_secrets` (the case you spotted)

As described in §7c: collected, used only for a `note`, absent from the payload → adding an OAuth
secret to an existing SPN never moves the hash, so the manual action never re-surfaces.
**Fix:** fingerprint over `{**stripped_scim, "_has_secrets": bool}`.

#### 🟠 GAP 3 (ACCEPTED) — `cluster_library`: install *status* changes are invisible

**What the collector captures** (`misc_collector._cluster_libraries`), per library on each cluster:
`cluster_id`, `library` (the spec: `{"pypi": {"package": "requests==2.31.0"}}` or
`{"jar": "dbfs:/…"}`), **`status`** (`INSTALLED` / `PENDING` / `FAILED` / `RESOLVING` / …), and
**`is_library_for_all_clusters`**.

**What reaches the fingerprint:** the payload is `{cluster_id, library}` only, and
`STRIP_FIELDS["cluster_library"] = ["status", "is_library_for_all_clusters", "messages"]` removes the
other two.

**So what's invisible:** a library that went **`FAILED` → `INSTALLED`** on source (or the reverse), or
whose `is_library_for_all_clusters` flag flipped, produces **no fingerprint change** → the state store
says SKIP on the next run.

**Why this is genuinely fine, not a deferred bug** — three independent reasons:
1. **`status` is runtime state, not configuration.** It describes what happened when *the source
   cluster* last tried to install the library. It is not a create field — `POST libraries/install`
   takes `{cluster_id, libraries:[…]}` and nothing else. Including it would be a category error, and
   would make the fingerprint churn on every run as clusters start and stop (exactly the phantom-update
   problem the strip list exists to prevent).
2. **A changed library is already a *different unit*.** The natural key is
   `f"{cluster_id}:{json.dumps(lib, sort_keys=True)}"` — **the library spec IS the key**. So bumping
   `requests==2.31.0` → `2.32.0` doesn't need a fingerprint change: it appears as a **new unit**
   (CREATE) plus a `deleted_in_source` on the old one. The fingerprint has nothing left to detect,
   because every meaningful change to a library alters its identity.
3. **The action is idempotent anyway.** `libraries/install` on an already-installed library is a no-op,
   so even a spurious re-attempt is harmless.

**The only real-world consequence:** if a library failed to install *on source* and the customer later
fixes it there, our re-run won't re-trigger anything — but it doesn't need to, because we install onto
the **target** cluster independently of the source's install outcome. And target-side install status is
tracked separately: libraries land as `deferred` when the target cluster isn't running (D6), which is a
target concern the state table already records.

**Verdict: accepted, no fix.** Documented here so it isn't "discovered" later and fixed into a
phantom-update bug.

#### 🟠 GAP 4 (ACCEPTED) — `secret_scope`: values and scope-ACLs aren't in the fingerprint

**What the collector captures** (`secrets_collector`): `name`, `backend_type`
(`DATABRICKS`/`AZURE_KEYVAULT`), `keyvault_metadata` (`dns_name`, `resource_id`), **`acls`**,
`key_names` (names only — never values), `values_migratable=False`.

**What reaches the fingerprint:** the payload is `{name, backend_type, keyvault_metadata, key_names}`.
`STRIP_FIELDS["secret_scope"] = []` (nothing stripped), but note the payload is **assembled by hand** in
`_secret_units` — so `acls` is excluded simply by not being copied in.

**What IS caught (worth stating, since it's the useful half):**
- a **key added or removed** → `key_names` changes → hash moves ✅ (so "a new secret appeared in this
  scope" *is* detected, and its manual-populate action re-surfaces)
- an **AKV vault re-point** (`resource_id`/`dns_name` change) → hash moves ✅
- a **backend type change** → hash moves ✅

**The two things that don't, and why each is right:**

1. **Secret VALUES — impossible, not an oversight.** No Databricks API ever returns a secret value
   (by design; that's the point of a secret store). So we cannot hash what we cannot read. A value
   rotated on source is undetectable by *any* tool using the API. Already handled the only way it can
   be: every key gets its own `secret_value` unit with `mode="manual"`, so the report lists each value
   to re-populate on target on **every** run, not just the first. **No fix possible.**

2. **Scope ACLs — deliberately elsewhere, and now properly tracked.** Secret-scope ACLs live in
   `acls.json`, not in the scope payload, because their principals need target-side remapping (Plan 2
   D5). They're therefore covered by **the ACL machinery, not the fingerprint**: the ACL parity diff
   (§6b) detects source-vs-target divergence, and — as of your point 3 — they now carry **their own
   state rows** with an ACL-set fingerprint (§6b-i). So "someone changed who can read this scope on
   source" **is** detected; just by the `acl` row's fingerprint rather than the `secret_scope` row's.
   **Correct as-is** — flagged here only so the absence from the scope payload isn't misread as a gap.

**Verdict: accepted.** One is physically impossible; the other is covered by a different (and more
appropriate) mechanism.

#### ✅ Verified CORRECT (no gap) — the reasoning, per asset

| asset_type | Fingerprint covers | Verdict |
|---|---|---|
| `user`, `group` | full stripped SCIM incl. `entitlements`, `roles`, `members` (order-normalised) | ✅ member add/remove, entitlement change → caught |
| `group_membership` | `{displayName, members}` | ✅ built-in group membership change → caught |
| `instance_pool`, `cluster_policy` | full config / `definition` | ✅ an edited policy definition → caught (the master's motivating example) |
| `cluster` | full create config after runtime strip | ✅ node type, autoscale bounds, spark conf, libraries → caught |
| `job` | the entire `settings` (`STRIP_FIELDS["job"]=[]`) incl. `tasks`, `job_clusters`, `schedule`, `continuous` | ✅ any task/schedule edit → caught |
| `sql_warehouse` | config after strip (note `size` stripped, but `cluster_size` — the real create field — is kept) | ✅ resize → caught |
| `legacy_query`/`legacy_alert` | query text, `data_source_id`, options | ✅ |
| `alert_v2` | full config | ✅ |
| `dlt_pipeline` | the whole `spec` | ✅ notebook list, config, target → caught |
| `lakeview_dashboard` | `serialized_dashboard` (whole) | ✅ any dashboard edit → caught |
| `genie_space` | `serialized_space` + title + description + `warehouse_id` | ✅ (explicitly noted in `STRIP_FIELDS`) |
| `serving_endpoint` | the whole `config` | ✅ model/traffic change → caught |
| `global_init_script` | `script_b64` (the body) + `position` + `enabled` | ✅ **body IS hashed** — the one content-bearing asset that already gets this right, which is what made GAP 1 stand out |
| `workspace_conf` | `{key, value}` | ✅ |
| `directory` | `{path}` | ✅ nothing else exists to change |
| `repo` | n/a — now `manual` (§6a) | ✅ not upserted |
| `manual`/`dab` units | `fingerprint({})` — constant by construction | ✅ intentional; they're never upserted by content |

#### Cross-cutting things the audit also confirmed

- **Order-normalisation works** and is load-bearing: `normalize()` recursively sorts lists, which is
  why SCIM's randomly-ordered `members` doesn't re-fingerprint every run (memory
  `export-payload-strip-verification`).
- **Over-stripping is the residual risk, and it's asymmetric.** `strip_runtime` on an unknown
  asset_type strips nothing (safe). But a field wrongly listed in `STRIP_FIELDS` is invisible to the
  fingerprint *forever* — this is precisely how GAPs 2–4 arose. The existing SDK-derived **allowlist**
  check (`tests/live_fvm1_export.py` `CREATE_FIELDS`) validates that payloads carry no *extra*
  fields; it does **not** catch a *missing* one. **Added to the verification matrix:** a per-asset
  "mutate-one-field → fingerprint must change" test, which is the only check that catches this class
  directly.
- **ACLs are correctly outside every fingerprint** — they're covered by the parity diff (§6b). Worth
  stating because "ACL changed on source" is a real re-run scenario and the *fingerprint* is not the
  mechanism that catches it.

**Net:** 1 serious + 1 moderate gap to fix (GAP 1, GAP 2), 2 accepted-and-documented (GAP 3, GAP 4).
Both fixes are small, land in **export-side** code, and need a re-run of the fingerprint-stability
harness (`tests/live_fvm1_resume.py`).

### 7b. Mechanics

**Where the table is configured (customer Q2 — RESOLVED 2026-08-05).** The customer provides **one
catalog + schema shared across all workspace pairs, and both will already exist**. The tool takes
them as **widget input** and **owns the table names itself**. Two widgets, on the target-side
notebooks (`04_Import`, `00_Account_Preflight`, `05_Validate`) — hence also settable as **Job
parameters**, which is how the 100+ pairs are driven:

| Widget | Default | Notes |
|---|---|---|
| `state_catalog` | "" | **required** when `dry_run=false`; the shared catalog (assumed to exist) |
| `state_schema` | "" | **required** when `dry_run=false`; the shared schema (assumed to exist) |

**Table names are decided by the tool, not the operator** — nothing to typo, and every workspace pair
lands in the same place:

| Table | Purpose |
|---|---|
| `wsmig_migration_state` | the main per-object state table (§7) |
| `wsmig_identity_map` | the persistent old→new identity map (§7.1) |
| `wsmig_migration_state_dryrun` | rehearsal rows only; never mixed with real state |

**`ensure_table()` behaviour (as agreed):** check whether the table exists, create it if not
(`CREATE TABLE IF NOT EXISTS`), then proceed. The **catalog and schema are NOT created** — if either
is missing the notebook **fails fast** printing the exact `CREATE SCHEMA <catalog>.<schema>`
statement and the grants needed. Silently creating a catalog in someone's UC isn't the tool's
business, and on these workspaces `CREATE CATALOG` fails anyway (default-storage metastore — memory
`fvm1-test-fixtures-and-akv-state`).

**One shared table for all pairs** works because every row is keyed by `source_workspace_id` and
**every read is filtered by it** (asserted in tests) — one place to answer "where is every pair up
to". The migration SP needs `USE CATALOG` + `USE SCHEMA` + `CREATE TABLE` + `SELECT`/`MODIFY` on that
schema; preflight checks this and reports it as a prerequisite rather than failing mid-import.

- `ensure_table()` — `CREATE TABLE IF NOT EXISTS … USING DELTA` + a `CLUSTER BY (asset_type)`; the
  table is small (thousands of rows), so no partitioning.
- Writes go through **one `MERGE INTO … ON` the PK** per batch, built from a small staging
  DataFrame — never row-by-row `UPDATE`/`INSERT`.
- **One table serves all workspace pairs**, keyed by `source_workspace_id`, which is what makes the
  100+-pair rollout manageable: one place to query "where is pair X up to". Every read is filtered
  by `source_workspace_id`, asserted in tests.
- The **catalog/schema must pre-exist**; `04_Import` fails fast with the exact `CREATE SCHEMA`
  statement to run if not. Note the live-workspace gotcha: `CREATE CATALOG` fails on the test
  workspaces (default-storage metastore) — use the pre-provisioned catalog (memory:
  `fvm1-test-fixtures-and-akv-state`).
- Requires `spark`, which every notebook has. The store is **skipped entirely** when
  `dry_run=true` **and** `state_catalog` is blank, so a first-look rehearsal needs no UC setup at all.

---

## 7d. When the customer HASN'T done their manual steps (your question 2)

Both sub-questions, answered concretely. **Correcting your example first**, because it changes the
answer: a job referring to a notebook *inside a Git folder* does **not** fail at create — the Jobs API
accepts a `notebook_path` (or `git_source`) **without validating that the path exists**. It creates
fine and then **fails at first run**. That distinction matters: the failure we must catch is often
*not* at create time, which is exactly why the report and the state table have to carry
prerequisite gaps explicitly rather than relying on create failures to surface them.

### (1) Are the manual steps prerequisites for import? — Yes, and they're *graded*

**Yes: everything in `manual_actions.md` is declared a prerequisite of a *complete* import.** But
"prerequisite" is graded, because blocking the whole migration on a repo nobody references would be
wrong:

| Grade | Meaning | Enforcement |
|---|---|---|
| **BLOCKING** | import cannot produce a correct target without it (account identities missing/unassigned; state schema missing; bundle incomplete) | `00_Account_Preflight` returns **NO-GO** → with `preflight_enforce=true` (default) `04_Import` never runs |
| **DEGRADING** | import proceeds, but *specific named units* will be incomplete (Git repos not recreated; secret values not populated; AKV vault not permitted; UC tables absent) | preflight **WARN**, listing the affected units; import proceeds and marks exactly those units |
| **COSMETIC** | no effect on other assets (legacy dashboard rebuild) | listed in `manual_actions.md` only |

So the answer isn't "do everything first or don't start" — it's **"preflight tells you which grade you
are in, and blocks only when proceeding would be wrong."** `preflight_report.html` is the go/no-go
document, and it names the units each unmet prerequisite will affect.

**Plus a dependency-aware pre-check.** Because the repo case shows create-success-then-run-failure,
import adds a cheap static pass **before** the jobs/DLT phases: for every job/pipeline
`notebook_path`, resolve it against (a) what workspace content this run imported and (b) what already
exists on target. Unresolvable → the unit is still created (it may be intentional), but it is recorded
`created_with_warning` + `note: "notebook_path <p> does not exist on target — inside a Git folder
that must be recreated manually"`. That converts a silent time-bomb into a named row in the report.

### (1a) THE GENERAL PRINCIPLE — take the point, not the example: **never hard-fail the job**

Taking your point as the general rule (the Git repo was just one instance): **a per-unit problem NEVER
aborts the run.** It is caught, recorded in the migration state table + the report with its reason, and
the run **continues to the next unit**. Stated as the invariant the code is held to:

> **Every importer is fail-soft per unit.** One asset's failure — for *any* reason: missing
> prerequisite, API rejection, permission denied, unresolved dependency, unsupported operation —
> becomes that unit's `failed` row + report line, and **never** propagates. The job exit status
> reflects *the run completing*, not *every unit succeeding*.

This mirrors the collector/exporter contract already proven in Plans 1–2 (`BaseCollector._safe`,
export's fail-soft per-asset handling), so it's the same shape the codebase already uses.

**The only things that DO stop the run** — all *before* any unit is attempted, and all cases where
continuing would produce a wrong target rather than an incomplete one:
1. **Bundle verification fails** (`manifest.json` checksum mismatch — §3): a partial upload must never
   present as a partial migration.
2. **Preflight NO-GO** with `preflight_enforce=true` (§9).
3. **The state schema is unreachable** when `dry_run=false` (§7b): without durable state, every
   subsequent create risks becoming a duplicate on the next run — worse than not starting.
4. **A `direct`-mode source client that can't authenticate** — there'd be nothing to read.

Note the asymmetry, which is the point: those four are **whole-run preconditions**. Everything
per-unit is fail-soft. And even for the aborting cases, the `finally` flush (§7a) persists whatever the
run had already done, so an abort never loses state either.

### (2) Is the failure recorded, reportable, and re-runnable? — Yes to all three

**Recorded — in both stores, with the reason:**
- `wsmig_migration_state` row: `last_action="failed"`, `last_error=<message>`,
  **`failure_category`** (new column: `prerequisite_missing` | `api_error` | `dependency_unresolved` |
  `permission_denied` | `not_supported`), `last_run_id`, `last_seen_utc`. So the table answers *what
  failed, why, and when* — queryable across all 100+ pairs.
- `import_results.json` + a **red row in `import_status.xlsx`** with the same reason, failures sorted
  to the top of the Summary sheet.
- **`last_error` is human-readable, not a raw traceback**: the importer maps known API errors to a
  cause + remediation before storing (e.g. `RESOURCE_DOES_NOT_EXIST` on a notebook path →
  *"target notebook missing — likely an unrecreated Git folder; recreate the repo, then re-run with
  import_assets=jobs"*). Raw text is kept alongside for debugging.

**Re-runnable — a single `retry_mode` widget with the three modes you asked for.** Implemented as one
dropdown rather than three booleans, because booleans allow the nonsense combination
(`failed_only=true` + `skipped_only=true` — is that both, or neither?) and a dropdown can't be set to
an invalid state:

| `retry_mode` | Work list = state rows for this `source_ws_id` where… | Use it when |
|---|---|---|
| `off` *(default)* | — normal full run: every bundle unit, decided by fingerprint | the usual case |
| `failed_only` | `last_action IN ('failed','created_with_warning')` | a prerequisite was fixed (Git repo linked, vault permissioned, identity assigned) — re-attempt just what broke |
| `skipped_only` | `last_action IN ('skipped','manual','not_selected')` | a deferred family is now wanted (genie, or the DAB redeploy has landed), or a `manual` step was completed and you want the tool to re-evaluate |
| `failed_and_skipped` | the union of both above | the general "clean up everything outstanding" pass — the one to reach for after a round of customer fixes |

Design notes that make these safe and useful:
- **`created_with_warning` is included in `failed_only`** deliberately: those units *exist* on target
  but are known-degraded (e.g. the job whose `notebook_path` was unresolvable). After the repo is
  linked, re-attempting them is exactly what you want, and they'd otherwise fall through both buckets.
- **`manual` and `not_selected` are included in `skipped_only`**, not just fingerprint-`skipped` — a
  unit deferred by `import_assets` or parked as `manual` is precisely the "take it up later" case.
- **Retry modes never bypass the upsert decision.** They only narrow the *work list*; each selected
  unit still goes through the full decide-path (fingerprint, live existence check, ADOPT on
  `RESOURCE_ALREADY_EXISTS`). So a retry can't duplicate an object that a previous attempt actually
  created but failed to record — the §4 guarantee still applies.
- **Combinable with `import_assets`** — `retry_mode=failed_and_skipped` + `import_assets=jobs,acls`
  scopes a retry to specific families.
- **A plain re-run still works** and remains the safe default: a `failed` row has no valid
  `target_object_id` so the decision table yields CREATE/ADOPT, while successful units SKIP on
  unchanged fingerprint. `retry_mode` is the *fast, targeted* version, not the only route.
- **Dependencies are still validated** (§5): retrying `jobs` alone requires compute/workspace/identity
  in the state table, else a hard error naming what's missing — a narrowed work list must not mean a
  narrowed id map.

**Why the state table is the right home for this** (rather than only a report): every one of these
modes is a `WHERE last_action IN (…)` query, and at 100+ pairs "show me every workspace with
outstanding failures" has to be SQL, not a hunt through Excel files.

---

## 8. End-to-end orchestration (`00_Main_EndToEnd`) — `direct` mode

Master §4a. One multi-task Job in the target workspace:

| task | notebook | on failure |
|---|---|---|
| `inventory` | `01_Inventory` (reads source via M2M) | stop |
| `export` | `02_Export` → bundle + `LATEST_EXPORT.json` | stop |
| `preflight` | `00_Account_Preflight` | stop if `preflight_enforce` |
| `transform_review` | `03_Transform_Review` (verify + diff) | stop |
| `import` | `04_Import` | stop |
| `validate` | `05_Validate` | report only |

- `run_id` flows via `dbutils.jobs.taskValues` (set by `inventory`, read by all); `LATEST_EXPORT.json`
  remains the durable fallback so a task re-run in isolation still finds the bundle.
- **Widgets are Job parameters** — one JSON to define a pair, which is how 100+ pairs get driven.
- `00_Main_EndToEnd` **asserts `connectivity_mode=direct`** and fails immediately otherwise, naming
  the two-Job `airgap` alternative. A single Job cannot span the manual hop; pretending otherwise
  would produce a job that always "succeeds" with an empty import.
- **Recommended first run per pair:** end-to-end with `dry_run=true` (full rehearsal — real read,
  real bundle, real decisions, zero target writes), then flip to `false`.

---

## 9. `00_Account_Preflight` — the gate (verify only, never mutates)

**Yes — `notebooks/00_Account_Preflight.py` is part of THIS plan (your question 4).** It exists today
as a stub (`raise NotImplementedError`, TODO widgets, and a docstring still describing the air-gap-only
model) and this plan implements it, for two reasons: it's a **target-side notebook**, and its most
valuable checks — account identity presence/assignment — depend on the identity reconciliation logic
built here in phase 1. Building it earlier would have meant writing that logic twice. It is **build
step 5** (§10), deliberately right after identity so it can reuse it.

Its stub docstring also needs the mode update ("run ONCE before workspace #1" → run **per workspace,
before each import**; account-level checks are the once-per-account part, the rest is per-pair).

Runs **before** import (and as a task in the end-to-end Job). Reads the bundle's
`identity_classification.json` + `export_index.json` and checks the target. **It creates nothing.**

| Check | How | Verdict on failure |
|---|---|---|
| Bundle integrity | `verify_manifest()` | **BLOCK** |
| Mode wiring (`direct`) | mint the M2M token; call an admin-only source endpoint | **BLOCK** |
| Target admin rights | the run-as SP can read SCIM + `permissions` | **BLOCK** |
| Migration state table reachable | `state_catalog`.`state_schema` exists and the table is creatable (§7b) | **BLOCK** when `dry_run=false` |
| Staging readable | bundle dir listable from the target | **BLOCK** |
| Account identities present | each Entra user / UMI SP in the bundle exists in the target account and is **assigned to this workspace** | **WARN** (per-identity) → these become `assign_on_target` work or a customer-IT `manual_action` |
| Account-admin capability | can the run-as SP call `PermissionAssignments`? | **WARN** — if not, unassigned account identities are a **customer IT prerequisite**, reported not failed |
| ~~Git credentials~~ | **REMOVED** — repos are out of scope (§6a), so the check has nothing to gate. Repos are listed in the manual runbook instead | — |
| Warehouse/compute capacity | region-2 quotas / warehouse availability | **WARN** |
| Secret scope backends | for each AKV-backed scope, is its vault reachable/permitted from the target? (a **cross-region** dependency by design — §6c/D4) | **WARN** |
| AKV auth path | can an **Azure AD token** for app `2ff814a6-…` be minted headlessly from the run-as identity? (§6c) | **WARN** — if not, AKV scopes become a manual step; each scope still fails individually with remediation and the run continues |
| UC prerequisites | UC tables referenced by genie/lakeview/dlt payloads exist on target | **WARN** — UC is out of scope, so this is a *heads-up*, and it is the single most common cause of a "successful" import producing a broken dashboard |

Two more checks added by the decisions above:

| Check | How | Verdict on failure |
|---|---|---|
| State schema + grants | `state_catalog`.`state_schema` exists; SP has `CREATE TABLE`/`MODIFY` (§7b) | **BLOCK** when `dry_run=false` |
| Job/DLT notebook paths resolvable | static pre-check against imported content + what's on target (§7d) | **WARN**, naming each unresolvable path |

Output: `preflight_report.{json,html}` + a one-line **GO / GO-WITH-WARNINGS / NO-GO**, with every
warning **graded** (BLOCKING / DEGRADING / COSMETIC — §7d) and each unmet prerequisite listing **the
units it will affect**. With `preflight_enforce=true` (default) a NO-GO raises, so `04_Import` cannot
run behind it.

---

## 10. Deliverables & build order

**`src/state/state_store.py`** — implement the stub: `ensure_table`, `decide`, `record_batch`,
`flush`, `recovery_replay`, `get_target_id`, `load_id_map`/`save_id_map`,
`mark_missing_in_source`, `dryrun` variant (§7).

**`src/importers/`** — implement `base_importer.py` (`ImportResult.as_dict`, and `run()` doing
load → decide → dry-run/act → checkpoint → record, never raising) plus:
`phases.py` (order + prerequisite graph + selector validation, §5), `identity_importer.py`,
`compute_importer.py`, `workspace_importer.py`, `secrets_importer.py`, `jobs_importer.py`,
`sql_importer.py`, `dlt_importer.py`, `dashboards_importer.py`, `genie_importer.py`,
`serving_importer.py`, `misc_importer.py`, `acl_importer.py` (new), `import_runner.py` (new —
the orchestrator, mirror of `inventory_runner`/`export_runner`).

**`src/identity/identity_map.py`** — target-side reconciliation + `old→new` maps, backed by the
migration state table (bundle `identity_map.json` is the per-run view; the table is the durable truth).

**`src/transform/transforms.py`** — the remap half (currently stubs): `remap_ids`,
`remap_principals`, `remap_paths`, `pause_schedules`, `apply_excludes`.

**`src/auth/token_manager.py`** — `oauth_m2m_token_provider` + `build_clients` (§2) +
**`mint_aad_token(app_id)`** for AKV-backed scope creation (§6c — client-credentials against Azure AD
using the run-as identity).

**Export-side fixes that land with this plan** (small changes to already-built code; all three need a
re-run of `tests/live_fvm1_resume.py` for fingerprint stability):
- **`content_fetcher.py` + `export_runner.py`** — `FetchResult.content_sha256` folded into each content
  unit's fingerprint after the content pass, and carried in the checkpoint's `pending_results`
  (**GAP 1**, §7c-audit — the serious one: notebook edits are currently invisible).
- **`asset_export.py`** — SP fingerprint over `{**stripped_scim, "_has_secrets": bool}` (**GAP 2**), and
  repo units to `mode="manual"` (D9/§6a).
- **`02_Export`** — write `LATEST_EXPORT.json` after `manifest.json` (§3).

**`src/reports/`** — `import_results.{json,html}` + `import_status.xlsx` (same shape as
`export_status.xlsx`, with **Import Status** + **Action Taken** + **Target Id** columns joined on
`(asset_type, natural_key)`) + **`acl_parity_report.{json,html}`** (§6b — the post-apply
source-vs-target ACL diff) + `manual_actions.md` (appended, not overwritten; carries the repo
runbook rows §6a, legacy-dashboard rebuild rows §6d, secret key-name rows §6c).

**Notebooks** — `00_Account_Preflight`, `04_Import`, `00_Main_EndToEnd` (new), and the mode-aware
guards in `01`/`02`.

**Build order (each step ends with a live test on the target workspace before the next starts):**
0. **EXPORT-SIDE FIXES FIRST (GAP 1 + GAP 2, §7c-audit).** Land these before any importer, because the
   whole upsert design rests on the fingerprint being trustworthy — and because step 6's "edit the
   source asset → re-import → prove UPDATE" test **cannot pass for notebooks** until GAP 1 is fixed.
   Three small changes + a re-run of `tests/live_fvm1_resume.py`:
   - `content_fetcher.py`: add `FetchResult.content_sha256` (hash the bytes already in memory).
   - `export_runner.py`: after the content pass, set
     `unit["fingerprint"] = fingerprint({**payload, "_content_sha256": sha})`; add `content_sha256`
     to the checkpoint's `pending_results` so a **resumed** unit doesn't rebuild with the
     metadata-only hash.
   - `asset_export.py`: SP fingerprint over `{**stripped_scim, "_has_secrets": bool}`; repo units to
     `mode="manual"` (D9); plus the per-asset **fingerprint-sensitivity** tests (§11).
1. Dual-mode auth + config (§2) — testable alone: mint an M2M token, list source groups from the target.
2. `state_store` + `base_importer` + `import_runner` skeleton + selector/prereq validation (§5, §7).
3. `LATEST_EXPORT.json` (a small `02_Export` addition) + run resolution + manifest gate (§3).
4. **Phase 1 identity** — the highest-risk write; proves classification, two-pass groups, the
   old→new maps, and the create/update/skip upsert on a second run.
5. `00_Account_Preflight` (§9) — now that identity reconciliation exists, preflight reuses it.
6. Phase 2 compute → 3 workspace → 4 secrets → 5 jobs (each: import, then **re-run** to prove SKIP,
   then edit the source asset, re-export, re-import to prove UPDATE).
7. Phases 6–11 (sql, dlt, dashboards, genie, serving, misc).
8. Phase 12 ACLs + the `.bundle/`-skip guard + the **`acl_parity_report`** diff (§6b).
9. `import_results.{json,html}` + **`import_status.xlsx`** + `manual_actions.md` + the three
   `retry_mode` paths (§1a, §7d).
10. `00_Main_EndToEnd` + Job JSON for both modes; full `dry_run=true` rehearsal, then live.

---

## 11. Verification matrix (master §6b — verify every API, incl. pagination, before relying on it)

| Concern | What to verify | Where |
|---|---|---|
| OAuth M2M | `/oidc/v1/token` client-credentials against the fvm1 SP works from the target; token refresh fires; admin endpoint reachable | step 1 |
| Update APIs | **every** edit API in §6 — several differ from the create shape (`instance-pools/edit` requires the full config + id; `jobs/reset` replaces wholesale; warehouses use `/{id}/edit`) | per phase |
| Pagination on the target | `existing_keys()` for each family paginates (SCIM `startIndex/count`, cursor APIs) — a bare `get()` that silently truncates would cause **duplicate creates**, the worst failure mode here | per phase |
| Fingerprint stability round-trip | export → import → re-export → re-import ⇒ every unit SKIPs | step 6 |
| **Fingerprint SENSITIVITY (per asset_type)** | for EVERY asset_type: mutate one meaningful source field ⇒ the fingerprint **must** change. This is the only check that catches an over-strip (a field wrongly in `STRIP_FIELDS` is invisible forever); the existing SDK allowlist check only catches *extra* fields, never a *missing* one (§7c-audit) | step 6, per phase |
| UPDATE path | edit a source asset, re-export, re-import ⇒ `updated` against the **stored target id**, no duplicate | step 6 |
| Adopt path | pre-create an object on target by hand, then import ⇒ `adopted`, not duplicated / not failed | step 6 |
| Crash recovery | kill the run mid-phase ⇒ re-run resumes; the recovery replay lands the unflushed batch; no duplicates | step 2 + step 6 |
| Selector | `import_assets=genie` alone with identity+sql already in the table ⇒ works; `jobs` alone with an empty table ⇒ **hard error naming the missing prerequisites** | step 2 |
| DAB skip | a `.bundle/` unit is skipped and its ACL grants ignored; assert on `import_action`, not `migration_mode` | step 8 |
| Notebook round-trip | SOURCE format `.py/.sql/.scala/.r` re-imports as a **notebook** (not an opaque file) | step 6 |
| Cluster post-create | cluster is **stopped** after create; pinned clusters re-pinned | step 6 |
| AKV scope | an AKV-backed scope creates on target (needs an **AAD token for `2ff814a6-…`**, not the Databricks token — memory `fvm1-test-fixtures-and-akv-state`), and the target identity can read the vault | step 6 |
| Volume I/O | no `open(path,"a")` anywhere; checkpoint/log use the local-then-copy pattern; `.xlsx` rendered in `/tmp` first | throughout |
| Dry-run purity | `dry_run=true` end-to-end makes **zero** mutating calls (assert via a client wrapper that raises on POST/PUT/PATCH/DELETE) | step 2 |
| **ACL parity** | after the ACL phase, `acl_parity_report` shows `match` for every object; deliberately break one grant and confirm it shows `missing_on_target` rather than passing (§6b) | step 8 |
| **Secret redaction** | `spn_secret_value` appears in **no** artifact, log line, or notebook output — grep every written file for the literal | step 1 |
| **Identity map durability** | kill the run mid-phase-1 ⇒ the map rows for already-created SPs/groups survive; the re-run **adopts** them and creates no duplicate appId (§7.1) | step 4 |
| **Flush-on-failure** | inject a per-object API failure mid-batch ⇒ the preceding successes are in the migration state table; inject a phase-level exception ⇒ both tables flushed before it propagates (§7a) | step 2 |
| **Repos / legacy dashboards** | both report `manual` with a reason and make **zero** create calls (§6a, §6d) | steps 6–7 |
| **AKV scope AAD token** | `mint_aad_token` works headlessly from the run-as identity; scope links to the source vault; a vault-permission failure yields a per-scope `failed` + remediation and the run **continues** (§6c) | step 6 |
| **SP secret fingerprint** | add an OAuth secret to an existing source SPN, re-export ⇒ fingerprint **changes** and the manual action re-surfaces (D15/§7c) — this currently fails, hence the fix | step 4 |
| **`retry_mode` (all 3)** | `failed_only`: force a failure, fix it, retry ⇒ only that unit attempted. `skipped_only`: defer genie then retry ⇒ only genie. `failed_and_skipped`: union. In every case nothing else is touched and no duplicate is created (§7d) | step 9 |
| **Fail-soft invariant** | inject an API failure into each phase ⇒ the run **completes**, the unit is `failed` in state + report, later phases still run (D21). Then break the manifest ⇒ the run **aborts before any unit** | step 2 + per phase |
| **ACL state rows** | a `.bundle/` object's grant ⇒ `asset_type='acl'` row with `last_action='skipped_no_object'` + `failure_category='dab_redeploy'`; after the object exists, `retry_mode=skipped_only`+`import_assets=acls` applies it (§6b-i) | step 8 |
| **Notebook content fingerprint** | edit a notebook's code, re-export, re-import ⇒ the unit **updates** (currently it SKIPs — GAP 1, the fix's regression test) | step 6 |
| **Unresolvable notebook_path** | a job whose notebook lives in an unrecreated Git folder ⇒ **created** but `created_with_warning` + named in the report (NOT a silent time-bomb, and NOT a create failure) (§7d) | step 6 |
| **Preflight grading** | a missing account identity ⇒ NO-GO; a missing Git repo ⇒ WARN naming affected units; neither is silent (§7d, §9) | step 5 |

---

## 12. Decisions

- **D1 — Two connectivity modes, one bundle.** `direct` mode does **not** skip the bundle or stream
  in memory; it writes the same bundle to `target_staging_location`. Keeps one code path, keeps the
  audit artifact and the resume mechanism, and avoids a second less-tested pipeline. The only thing
  removed is the human file move. (master §1a)
- **D2 — Migration state table written incrementally: batched (200) + mandatory phase-boundary flush +
  `finally` flush + startup recovery replay from the checkpoint.** Not end-of-run (loses the
  source→target id map on a crash ⇒ duplicates and lost updates — a correctness bug, not a
  performance one) and not per-object (a Delta commit per object is hours of bookkeeping). The
  checkpoint JSON provides per-item durability; the table provides cross-run truth. (§7a)
- **D3 — `import_assets` is separate from the `migrate_*` toggles.** Toggles = bundle scope (both
  sides); selector = this session's work list. Selecting a family without its prerequisites is a
  **hard error listing them**, unless the prerequisite is already in the migration state table (which is
  what makes phase-at-a-time migration work). `acls` is independently selectable. (§5)
- **D4 — AKV-backed scopes: RESOLVED (2026-08-05) — link the scope to the SAME vault, minting the
  required Azure AD token from the run-as identity; on any failure, fail that scope, report it, and
  continue.** No remap widget in v1. Behaviour (§6c):
  1. **Mint an AAD token** for app `2ff814a6-3304-4ab8-85cb-cd0e6f879c1d` via client-credentials
     using the **run-as Azure managed identity / Entra SP** (headless — reuses the `direct`-mode
     client-credentials helper). A Databricks token cannot do this call.
  2. Create the scope with `scope_backend_type=AZURE_KEYVAULT` + `backend_azure_keyvault
     {resource_id, dns_name}` **carried over verbatim** from the export.
  3. **On any failure → that scope is `failed` with a note naming which step broke and the exact
     remediation, and the run continues.** The two causes are distinguished: run-as identity isn't
     an Entra SP (can't mint) vs the vault refused (grant it `get`+`list` on the vault).
  4. Databricks-backed scopes are unaffected: created normally, **values always manual** (no API
     returns a value).
  **Customer prerequisite:** grant the run-as identity `get`+`list` on each Key Vault (access policy
  or *Key Vault Secrets User*) — on the **vault**, not the Databricks scope. Probed in preflight (§9).
  A remap option is deferred until a customer asks for a region-2 vault; if they do it's a
  `akv_vault_remap` widget (`old_resource_id=new_resource_id,…`) plus an out-of-band Azure
  vault-to-vault copy.
- **D5 — Deletes are never automatic.** A key present in the migration state table but absent from the
  bundle is reported `deleted_in_source`; deletion requires `allow_deletes=true`.
- **D6 — Cluster libraries are not force-installed.** Clusters are stopped right after create, and
  `libraries/install` needs a running cluster, so libraries record `deferred` with the reason.
  `library_force_start_clusters=true` opts into starting them. Silently burning the customer's DBUs
  is not a default.
- **D7 — Manifest verification is a hard gate**, with `skip_manifest_verify` as a loud,
  report-stamped override. A partial upload must not present as a partial migration. (§3)
- **D8 — Preflight is a real gate** (`preflight_enforce=true` by default), because the failure it
  prevents — importing against a target missing its account identities — produces thousands of
  half-migrated ACLs that are far more work to unwind than to prevent. (§9)

- **D9 — Git repos are OUT OF SCOPE for import** (customer, 2026-08-05): inventoried, exported as
  metadata only (a few hundred bytes, **zero** content bytes — verified), never imported. Recorded
  `manual` with the URL/provider/branch/path so the metadata *is* the manual runbook. (§6a)
- **D10 — Legacy SQL dashboards are SKIPPED** (customer, 2026-08-05): the create endpoint is
  deprecated/absent on modern workspaces (verified live), so the importer never attempts it and
  reports `manual` with the rebuild note. Their underlying `legacy_query` objects still migrate. (§6d)
- **D11 — Source SP secret accepted BOTH ways** (customer, 2026-08-05): `source_sp_secret_scope`+`source_sp_secret_key`
  (preferred when both set) **or** `spn_secret_value`. Scope is recommended (a widget value is
  visible on the run page and kept in run history); the widget path requires redaction in
  `Config.redacted()` + logs, asserted by test. Both set = hard error. (§2a)
- **D12 — Migration state table: one shared catalog+schema for all pairs**, set via `state_catalog` /
  `state_schema` **widgets on the target notebooks** (hence also Job params); table names tool-owned.
  Every row keyed by `source_workspace_id`; every read filtered by it. (§7b)
- **D13 — Object ACL parity is verified, not assumed** (§6b): "drop" only ever meant "don't include
  in the declarative `PUT` body", never "remove from target" or "hide from the report". Only
  `inherited` echoes, the built-in `admins` grant, the immutable `/Shared` root, and grants on
  objects this run didn't create are omitted — each because sending it would fail or would *create*
  a divergence. A post-apply **`acl_parity_report`** re-GETs every touched object and diffs against
  source to prove apple-to-apple.

- **D14 — Manual steps are GRADED prerequisites, not an all-or-nothing gate** (§7d): BLOCKING →
  preflight NO-GO; DEGRADING → proceed but mark the named units; COSMETIC → runbook only. Failures
  carry a `failure_category` + human-readable remediation in the state table. Adds a static
  `notebook_path` resolvability pre-check, because a job referencing a missing notebook **creates fine
  and fails at first run** — create-failure alone would not have caught it.
- **D21 — NEVER hard-fail the run on a per-unit problem** (§7d(1a)): every importer is fail-soft per
  unit; a failure for *any* reason is recorded + reported and the run continues. Only four
  **whole-run preconditions** abort, all before any unit is attempted (bad manifest, preflight NO-GO,
  unreachable state schema when `dry_run=false`, unauthenticatable source client) — cases where
  continuing yields a *wrong* target, not merely an incomplete one. Even then the `finally` flush
  persists what was done.
- **D22 — Three retry modes via ONE `retry_mode` dropdown** (§7d): `off` | `failed_only` |
  `skipped_only` | `failed_and_skipped`. A dropdown, not three booleans, because booleans permit the
  meaningless `failed_only+skipped_only` combination. `failed_only` also picks up
  `created_with_warning` (degraded-but-existing units), and `skipped_only` also picks up `manual` +
  `not_selected` (the actual "take it up later" cases). Retry narrows the *work list* only — every unit
  still runs the full upsert decision, so a retry can never duplicate.
- **D23 — ACLs get their own state rows, one per OBJECT** (§6b-i): `asset_type='acl'`, keyed
  `<perm_object_type>:<object natural_key>`, with `skipped_no_object` as a distinct `last_action` and
  the specific case in `failure_category`. Without this, skipped grants would have been invisible to
  `retry_mode` — the units most likely to need a second pass. Per-object not per-grant, because
  `PUT permissions` is declarative over the whole object.
- **D15 — Fingerprint gaps: full audit done, 2 fixes + 2 accepted** (§7c-audit — every asset_type
  audited against its collector + builder + `STRIP_FIELDS`):
  - **GAP 1 (SERIOUS, must fix): notebook/workspace-file CONTENT is not fingerprinted.** The payload is
    only `{path, object_type, language}` and the content pass never updates `fingerprint`, so **editing
    a notebook on source and re-running SKIPs it — the target keeps the old code**, on an all-green
    report. Fix: `FetchResult.content_sha256` (bytes already in memory) folded into the unit's
    fingerprint after the content pass, and carried in the checkpoint's `pending_results`.
  - **GAP 2 (fix): SP `has_secrets`** — collected but payload-absent, so adding an OAuth secret never
    re-surfaces its manual action. Fix: fingerprint over `{**stripped_scim, "_has_secrets": bool}`
    (fingerprint input only, **not** a create field). Resurfaces the action; cannot migrate the secret.
  - **GAP 3 (accepted, no fix):** `cluster_library` `status`/`is_library_for_all_clusters` are stripped,
    so a source-side `FAILED→INSTALLED` flip isn't caught. Right as-is for three reasons: `status` is
    runtime state and not a create field (hashing it would churn every run); the library spec **is** the
    natural key, so any meaningful library change is already a *different unit* (CREATE +
    `deleted_in_source`); and `libraries/install` is idempotent anyway.
  - **GAP 4 (accepted):** secret **values** are unhashable — no API ever returns one, so this is
    physically impossible, and each key already gets a per-run `manual` unit. **Scope ACLs** are excluded
    from the scope payload by design (principals need remapping — Plan 2 D5) and are instead tracked by
    the ACL machinery: the parity diff (§6b) plus their own `acl` state row + ACL-set fingerprint
    (§6b-i). Added/removed **key names**, AKV vault re-points and backend changes ARE all caught.
  - Everything else verified correct — notably `global_init_script` already hashes its body, `job`
    hashes the whole `settings`, and `genie`/`lakeview` hash their full serialized payloads.
- **D16 — Import owns its own reporting, including Excel** (§1a): `import_status.xlsx` +
  `import_results.{json,html}` + `acl_parity_report` + `manual_actions.md` ship in **this** plan.
  Plan 4 adds only the cross-stage inventoried→exported→imported join, reading `import_results.json`.
- **D17 — The "didn't create/adopt" ACL guard is a runtime predicate, not a `.bundle/` path check**
  (§6b): six other cases exist, two of them *dynamic* (units that failed earlier in the run; families
  skipped by `import_assets`), so a path-based rule would have been a silent bug in each.
- **D18 — `00_Account_Preflight` is implemented in this plan**, at build step 5 (right after identity,
  whose reconciliation logic it reuses). Its stub docstring is corrected: it runs **per workspace
  before each import**, not once per account. (§9)
- **D19 — State table names are owned by the tool, catalog+schema by the customer** (§7b): two widgets
  (`state_catalog`, `state_schema`, both assumed to exist); the tool fixes the names
  (`wsmig_migration_state`, `wsmig_identity_map`, `…_dryrun`) and `ensure_table()` creates the table if
  absent but **never** the catalog/schema — it fails fast with the exact `CREATE SCHEMA` + grants.
- **D20 — No secret-scope naming convention needed** (§2a): the scope/key names are plain widget
  inputs, so whatever the customer already uses works. Three widgets: mandatory
  `source_sp_client_id`, optional `source_sp_secret_scope`+`source_sp_secret_key` (preferred when
  set), and `spn_secret_value` used only when the pair is empty.

### 12a. Open questions remaining

1. **Is the target workspace's run-as identity an Azure managed identity / Entra SP?** If yes (your
   expectation), AKV-backed scope creation works headlessly and the only ask is granting that identity
   `get`+`list` on each Key Vault. If it isn't an Entra identity, AKV scopes degrade to a manual step.
   **Doesn't block:** the importer attempts it and records a per-scope failure with the exact
   remediation while the run continues (D4); preflight probes it up front.
2. **Which catalog + schema** for the shared migration state table (customer said they'll provide),
   plus `USE CATALOG`/`USE SCHEMA`/`CREATE TABLE`/`SELECT`/`MODIFY` for the migration SP. Needed before
   the first live import with `dry_run=false`.
