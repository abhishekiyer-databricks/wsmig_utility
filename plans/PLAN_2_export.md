# Plan 2 — Export  (SOURCE side)

> Sub-plan of `plans/PLAN_0_master.md` (master). Review gate for the second slice of real code.
> Scope: the **`02_Export`** notebook + the `src/exporters/` export engine.
> **Runs INSIDE the source workspace** (air-gapped model — master §1, §3), right after `01_Inventory`.
> Builds directly on the completed Plan 1 inventory (collectors + `inventory.json`).

---

## 1. Objective

Inventory (Plan 1) produced **metadata** about every in-scope asset (`inventory.json`:
counts, natural keys, ACLs, DAB flags, and the full raw API payload under `_raw`). Export turns
that metadata into a **portable, self-describing bundle** the target side can recreate assets
from — the actual migration payload:

For every **migratable unit** in the inventory, Export must:
1. Produce a **relevant exported object** = a normalized, runtime-stripped, **create-ready
   payload** (JSON) — not just metadata. Written to a per-asset file under `export/`.
2. For **content-bearing** assets (notebooks, workspace files, global-init scripts) also dump the
   **actual bytes** (notebook SOURCE / file content), which inventory did NOT capture. This is a
   **second pass** (one API call per object) and is the slowest step, so it is **parallelized**
   (§7c) — deliberately kept in Export, not Inventory (§2a). (DBFS is out of scope for v1 — §5b.)
3. Emit, per unit, a stable **natural key** + a content **fingerprint** (master §9) so the
   target-side state store can decide create/update/skip on re-runs.
4. Write **ACLs to a SEPARATE file** (`export/acls.json`), not inside each unit's payload —
   because on the target side each grant's principal (SP/group/user id) must be **remapped to
   the new target entity id** before the ACL is applied, and that remap can only happen after
   identity import builds the old→new map (master §7). Keeping ACLs separate means the ACL-apply
   pass runs after objects + identities exist, reading its own remappable file (see D5, §5).
5. Write an **`export_index.json`** — the tie-back ledger: one row per unit keyed by
   `(asset_type, natural_key)`, recording `export_status` (success/failure/skip/manual/dab) +
   reason. This is the reconciliation baseline the final report + the post-export Excel (§6a)
   join against to show **exported** and later **imported** status beside every inventoried unit.
6. Honour the **per-asset toggles**, **DAB** flags, and **migratability** flags (skip what the
   tool doesn't migrate, but still record it in the index so nothing silently disappears).
7. **Record every failure with a reason** — a per-unit exception becomes `export_status:failure`
   + `note` in the index, a **WARNING log line** in `execution_export.log`, and a highlighted
   row in the post-export Excel (§6a, point 4 from review).
8. Be **idempotent + checkpointed** (resume a re-run), **fail-soft** (one asset's failure never
   aborts the bundle), and finish by writing **`manifest.json`** (checksums) for handoff integrity.

Export does **not** talk to the target and does **not** mutate the source — it only reads
(content bytes) and writes files to `source_staging_location`.

---

## 2. Relationship to Plan 1 (the key design decision)

**Inventory already did the "read the source" work.** Every collector stores the full raw API
object under `_raw` (jobs `settings`, DLT `spec`, dashboards `serialized_dashboard`, compute /
warehouse / SCIM raw, secret backend + ACLs + key names, GIS `script_b64`, workspace-conf
values, etc.), plus `natural_key`, `acl`, and DAB flags. So Export does **not** re-list the
workspace. Its inputs are:

- **`inventory.json`** (same run dir) — the source of truth for all asset metadata + `_raw`.
- A **live API client** — used ONLY to fetch the two things inventory doesn't hold: **notebook/
  workspace-file content bytes** (`workspace/export`). Everything else is transformed from `_raw`.

**Why reuse inventory rather than re-fetch everything:** single "read source" layer (collectors),
no duplicated list logic, no double API load, and the tie-back is automatic — Export inherits the
exact same `natural_key` + id inventory recorded, so the reconciliation report lines up by
construction.

**Coherence guard (D1 — RESOLVED: reuse):** `02_Export` **reuses `inventory.json`** if it exists
for the resolved `run_id` (see §2b for how the run_id is found when 01 and 02 are separate runs);
if it's missing, the export runner invokes `InventoryRunner` first so a run is always internally
consistent. Confirmed at review: reuse the inventory snapshot (single read-source layer, automatic
tie-back), never a second full re-read.

Runtime-field **stripping** and **fingerprinting** are Export's new responsibility (master §9:
the fingerprint is computed over the *normalized importable payload, after runtime-field strip,
before target-id remap*). **Reference remapping** (source ids → target ids) stays target-side
(`03_Transform_Review` / `04_Import`) — Export's payloads still reference source ids.

### 2a. Why content bytes live in Export, not Inventory (review Q1)

**Decision: content bytes are pulled in Export, never folded into Inventory.** Rationale:
- **Inventory is the read-only scoping / go-no-go artifact** the operator reviews *before*
  committing to a migration. It needs paths, sizes, ACLs, classification — enough to decide *what*
  to migrate — not the file contents themselves. You don't read notebook source to decide whether
  to migrate it.
- **Byte capture would bloat `inventory.json` by orders of magnitude** (every notebook's full
  source inline), slowing the scoping pass and the HTML/Excel render for zero scoping value.
- Bytes are **migration payload**, which is definitionally Export's concern.

The cost of this split is a **second pass**: one `GET /api/2.0/workspace/export` per notebook/file.
Because the current codebase is **fully serial** (no thread pool anywhere), a naive loop over
thousands of objects would be the tool's slowest step by far. So the content pass is **explicitly
parallelized** with a bounded thread pool (§7c) — the transform-from-`_raw` work stays serial
(it's in-memory and cheap).

### 2b. Resolving the run_id when Inventory and Export are separate runs (review Q3 — D6)

`02_Export` must read the **same** bundle `01_Inventory` wrote (`.../wsmig/<src_ws_id>/<run_id>`),
but the two notebooks may **not** run as one 2-task job — so the `run_id` can't be assumed to flow
between them. Options considered:

| Option | Mechanism | Assessment |
|---|---|---|
| A. Same 2-task job | Inventory passes `run_id` to Export via `dbutils.jobs.taskValues` | cleanest *when* they're one job; but we can't assume that |
| B. Operator retypes `run_id` | manual widget on both notebooks | works but typo-prone → empty/wrong bundle |
| C. Discover newest dir | Export scans `wsmig/<src_ws_id>/*` by mtime | fragile — FUSE/Volume mtimes aren't reliably ordered |
| **D. Pointer file** | Inventory writes `wsmig/<src_ws_id>/LATEST_INVENTORY.json = {run_id, generated_utc, counts}`; Export reads it when `run_id` widget is blank | deterministic, no mtime reliance, survives the air-gap |

**RESOLVED — D, with A/B as explicit overrides.** Resolution order in `02_Export` (full precedence
in §7a, which adds the completion-state resume step):
1. If the **`run_id` widget is set** → use it verbatim (covers same-job task-values wiring **and**
   deliberate operator control / resume of a specific run).
2. Else, if the latest bundle for this `source_ws_id` is **incomplete** (checkpoint present, no
   `manifest.json`) → **resume it** (§7a) — this is what makes a whole-job re-run continue rather
   than restart.
3. Else → read **`<staging>/wsmig/<src_ws_id>/LATEST_INVENTORY.json`** (dropped by `01_Inventory`
   at the end of its run) and take its `run_id`.
4. Else (no widget, no incomplete bundle, no pointer) → **fail loudly** ("run 01_Inventory first, or
   pass run_id"). Never silently invent a new run_id that would produce an empty bundle.

Export **prints the resolved run_id + how it was resolved** (widget / resume / pointer) and
proceeds, so the operator sees exactly which snapshot is being exported before any work happens.
`01_Inventory` gains two tiny additions: write `LATEST_INVENTORY.json` (a 3-field pointer, not data)
at the end, and — for the whole-job-re-run case — reuse an existing **incomplete** bundle's `run_id`
instead of minting a new snapshot (§7a).

**Publishing the run_id (both channels, unconditionally).** `01_Inventory` publishes its `run_id`
**two ways at the end of every run**, with no branching on how it's invoked:
- **`LATEST_INVENTORY.json` pointer (primary / authoritative)** — survives everything: separate
  runs, the air-gap, single-task jobs, interactive runs. This is the mechanism Export relies on.
- **`dbutils.jobs.taskValues.set("run_id", …)` (optimization for the 2-task-job case)** — read by
  the Export task when the two share a job. Always called, because outside a job context the `set`
  is a harmless no-op (it neither raises nor persists anywhere), so there's no need to detect "am I
  in a 2-task job?" — just always set it. Wrapped in try/except so an unusual runtime that doesn't
  expose `jobs.taskValues` can never break the read-only inventory (degrade-gracefully, like the
  rest of the tool).

Both channels carry the **identical** `run_id` written at the same point, so they can never
disagree; the pointer is the source of truth and the task value is a same-job shortcut.

---

## 3. The export record + the tie-back ledger (what "ties it back to inventory")

The user requirement — *"a way to tie it back to the inventory (name, id) so we can build the
report which shows True/False for exported and imported"* — is met by a **stable per-unit key**
carried end to end:

> **unit key = `(asset_type, natural_key)`**, with `source_id` kept alongside for
> disambiguation and human display.

`asset_type` is a **canonical, fine-grained taxonomy** (finer than the collectors' coarse
`object_type`), because the report reconciles at this granularity:

| Collector `object_type` | Export `asset_type`(s) |
|---|---|
| `identity` | `user`, `service_principal`, `group` |
| `compute` | `instance_pool`, `cluster_policy`, `cluster` |
| `workspace_object` | `directory`, `notebook`, `workspace_file`, `repo` |
| `secret_scope` | `secret_scope` (values → `secret_value` manual unit) |
| `job` | `job` |
| `sql` | `sql_warehouse`, `legacy_query`, `legacy_alert`, `legacy_dashboard`, `alert_v2` |
| `dlt_pipeline` | `dlt_pipeline` |
| `lakeview_dashboard` | `lakeview_dashboard` |
| `genie_space` | `genie_space` |
| `serving_endpoint` | `serving_endpoint` |
| `misc` | `global_init_script`, `cluster_library`, `workspace_conf` |
| `app`, `lakebase_project` | `app`, `lakebase_project` (inventory-only) |

**Per-unit export record** (the shape written into each per-asset file's `units` list and
summarized in `export_index.json`):

```jsonc
{
  "asset_type": "job",
  "natural_key": "Nightly ETL",          // from inventory (job name / path / appId / scope name…)
  "source_id": "428193",                 // server id — for tie-back + duplicate disambiguation
  "fingerprint": "sha256:ab12…",          // hash of the normalized payload (master §9)
  "migratable": true,
  "migration_mode": "auto",              // auto | content | manual | dab   (see §5)
  "export_status": "success",            // success | failure | skip | manual | dab | incomplete
  "import_action": "create",             // what the TARGET side does with it — see §6a table
  "artifact": "export/jobs.json",         // file the payload lives in
  "content_ref": null,                    // for content assets: path to the bytes file
  "note": "",                             // reason for failure/skip/manual/dab/incomplete/partial
  "acl_grants": 4                          // count only; the grants live in export/acls.json (D5)
}
```

`export_status` values (drive the Excel "Export Status" column, point 1 from review):
`success` (payload written) · `failure` (errored — `note` = reason, also a WARNING log) ·
`skip` (toggle off) · `manual` (Genie / secret values / app / lakebase / UC-backed serving) ·
`dab` (customer redeploys via Azure DevOps) · `skipped_oversize` (workspace content over the
API size limit that even the streaming fallback can't carry — WARNING + listed in
`oversize_artifacts.json`, NOT a failure; see §5a) · `incomplete` (a traversal cap cut a listing
off mid-way — §7b).

`import_action` answers the SEPARATE question "what will the target side do with this unit?" and is
carried on **every** unit (not just identity), in both the index and the per-asset payload files.
It is derived from `classification` (identity) → `migration_mode` → `asset_type` overrides, then
overridden by a status that means "nothing to import" (`skip`/`failure` → `none`,
`skipped_oversize` → `manual`). The runner **re-derives it after toggles and the content pass**, so
a unit whose status changed late never advertises a stale action. Full value table in §6a.

**`export_index.json`** (bundle root) — the ledger:
```jsonc
{
  "run_id": "...", "source_workspace_id": "...", "generated_utc": "...",
  "tool_version": "0.1.0",
  "units": [ <export record>, ... ],       // EVERY inventoried unit, migratable or not
  "counts": { "notebook": {"total":40,"success":38,"skipped_oversize":2,"failure":0,"skip":0}, ... }
}
```

**Three-column reconciliation model** (finalized in Plan 8's validate/report, *seeded here*):
`inventoried` (present in inventory) → `exported` (`export_status` from `export_index.json`) →
`imported` (from target-side `import_results.json`), joined on `(asset_type, natural_key)`.
Export owns the **`exported`** column and establishes the canonical unit key both other columns
use. Units that are `manual`/`dab`/`skip` carry a `note` (e.g. "handled by DAB redeploy",
"secret values re-populated manually", "toggle migrate_jobs=false") so the report + Excel show
*why*, never a silent gap.

---

## 4. Bundle layout written by Export

Under the run-isolated dir `output_path = <source_staging>/wsmig/<src_ws_id>/<run_id>/`
(seeded by inventory; `ArtifactWriter.ensure_output_path()` already creates the subdirs):

```
<run dir>/
├── inventory.json / .xlsx / .html          # (Plan 1 — Export reads inventory.json)
├── identity_classification.json            # (Plan 1)
├── config_resolved.json                    # (Plan 1; Export appends export options)
├── export/
│   ├── identity/
│   │   ├── users.json                       # {units:[…]} — each unit.payload = SCIM create body
│   │   ├── service_principals.json
│   │   └── groups.json                      # members kept (remap is target-side)
│   ├── compute/
│   │   ├── instance_pools.json
│   │   ├── cluster_policies.json
│   │   └── clusters.json                     # create-config whitelist only (ephemeral already excluded)
│   ├── workspace/
│   │   ├── objects.json                      # dir/notebook/file metadata + content_ref (NO acl — see acls.json)
│   │   ├── repos.json                        # url/provider/branch (contents re-cloned on target)
│   │   └── content/<mangled_path>.source|.bin   # ACTUAL notebook/file bytes (SOURCE format)
│   ├── secrets/
│   │   └── scopes.json                       # names + backend_type + AKV metadata + key names; NO values, NO acl
│   ├── jobs.json
│   ├── sql/
│   │   ├── warehouses.json
│   │   ├── legacy_queries.json / legacy_alerts.json / legacy_dashboards.json
│   │   └── alerts_v2.json
│   ├── dlt/pipelines.json
│   ├── dashboards/lakeview.json              # serialized_dashboard + warehouse_id
│   ├── genie/spaces.json                     # MANUAL (serialized_space not exportable)
│   ├── serving/endpoints.json                # CONDITIONAL (UC-backed → manual)
│   ├── misc/
│   │   ├── global_init_scripts.json          # includes script body (from inventory script_b64)
│   │   ├── cluster_libraries.json
│   │   └── workspace_conf.json
│   ├── acls.json                            # ALL object + secret-scope ACLs, keyed by (asset_type,natural_key,source_id) → grants; principals remapped target-side (D5)
│   ├── oversize_artifacts.json              # notebooks/files over the API size limit → manual copy (§5a)
│   └── manual/
│       ├── apps.json / lakebase.json         # inventory-only; index rows, no create payload
│       └── manual_actions.md                 # secret values, Genie, repo git creds, UC-backed serving, DAB notes, oversize copies
├── export_index.json                        # the tie-back ledger (§3)
├── export_status.xlsx                        # post-export Excel: inventory rows + Export Status column (§6a)
├── execution_export.log
├── export_checkpoint.json                    # resumable state (§7a) — read on re-run to skip done units
└── manifest.json                            # file list + sha256 + counts (handoff integrity)
```

> **Volume/openpyxl gotcha DOES affect the post-export Excel** (`export_status.xlsx`): openpyxl
> needs a seekable disk, so render to `/tmp` then byte-copy to the Volume via
> `ArtifactWriter.write_text_local_then_copy(...)` (already handles this — same path inventory uses).
> The JSON + raw content bytes are plain writes (`write_json` / `write_bytes`), unaffected.

---

## 5. Per-asset export spec (the engineering detail)

For each `asset_type`: **input** (from `inventory.json` `_raw`/mapped fields, or a live content
fetch) → **strip runtime fields** → **payload** (create-ready, source-id references intact) →
**fingerprint** → **mode**. Strip lists live in `src/transform/transforms.py` (per-asset registry).

| asset_type | Payload source | Strip (runtime → not fingerprinted) | Mode | Notes |
|---|---|---|---|---|
| `user` | SCIM `_raw` | `id`, `groups`, meta | auto | create whitelist `userName,displayName,emails,externalId,active`; entitlements/roles kept for target PATCH passes |
| `service_principal` | SCIM `_raw` | `id`, meta | auto/manual | DB-managed → new appId target-side; OAuth secrets (`has_secrets`) → manual note |
| `group` | SCIM `_raw` | `id`, member `value` ids kept-but-not-fingerprinted-as-id | auto | members kept (remap by display/email is target-side, nested-first) |
| `instance_pool` | `_raw` | `instance_pool_id`, `stats`, `status`, `default_tags` | auto | same cloud → keep node types |
| `cluster_policy` | `_raw` | `policy_id`, `created_at_timestamp` | auto | send only `name`+`definition`+libraries; ACLs separate (target) |
| `cluster` | `_raw` | `cluster_id`, `state*`, `*_time`, `*_by_user*`, autoscale current, `spark_context_id`, `default_tags` | auto | **create-config whitelist** (master §10a); ephemeral already excluded by inventory; keep `policy_id`/`instance_pool_id` refs (remap target-side) |
| `directory` | inventory record | — | auto | `mkdirs` path only |
| `notebook` | **live fetch** `workspace/export` | — | content | format **SOURCE** (D2 RESOLVED); size-tiered fetch (§5a): base64 → streaming fallback → `skipped_oversize`+warning if >500 MB; bytes → `content/`; `content_ref` set |
| `workspace_file` | **live fetch** (FILE) | — | content | non-notebook files; same size-tiered fetch (§5a); >500 MB → `skipped_oversize` + warning + `oversize_artifacts.json` row |
| `repo` | inventory record | `id`, `head_commit_id` | auto/manual | only URL-backed recreate; git creds are a target manual prerequisite → note |
| `secret_scope` | inventory record | — | auto | name + `backend_type` + `keyvault_metadata` + `key_names`; ACLs → `acls.json` (D5); **values NOT exported** → emit `secret_value` manual unit per key (values cap 128 KB, never readable) |
| `job` | `settings` (`_raw`) | `job_id`, `created_time`, `creator_user_name`, run/trigger state | auto/dab | 2.1 `settings` already has `tasks` (expand_tasks); keep cluster/pool/policy/notebook/`run_as` refs (remap target-side); pause schedules is a target transform |
| `sql_warehouse` | `_raw` | `id`, `state`, `health`, `num_active_sessions`, `num_clusters` | auto | keep `warehouse_type` (same cloud) |
| `legacy_query` | `_raw` | `id`, timestamps, `user`, `last_modified_by` | auto | keep query text + `data_source_id`/warehouse ref (remap target-side) |
| `legacy_alert` | `_raw` | `id`, timestamps, state | auto | references a query (remap target-side) |
| `legacy_dashboard` | `_raw` | `id`, timestamps | auto/dab | DAB-deployed → dab mode |
| `alert_v2` | `_raw` | `id`, timestamps, state | auto/dab | Alerts V2 surface; DAB-deployed → dab |
| `dlt_pipeline` | `spec` (`_raw`) | `pipeline_id`, `state`, `cluster_id`, `latest_updates` | auto/dab | keep notebook/cluster refs; `deployment.kind==BUNDLE` → dab |
| `lakeview_dashboard` | `_raw` (`serialized_dashboard`) | `dashboard_id`, timestamps, `path` | auto/dab | keep `serialized_dashboard` + `warehouse_id`; under `.bundle/` → dab |
| `genie_space` | `serialized_space` + title/desc/warehouse_id (per-space GET `include_serialized_space=true`) | — | **auto** (manual only if API returns no `serialized_space`) | **UPDATED (verified live fvm1 2026-08-01):** `serialized_space` IS exportable via the current Genie API and recreatable via `create_space`/`update_space` (approach from the `client_shared_utils/workspace_asset_migration` reference). Payload keeps `serialized_space` verbatim; target remaps `warehouse_id`. Caveat: `serialized_space` references UC tables by FQN → those must pre-exist on target (UC out of scope) |
| `serving_endpoint` | `config` (`_raw`) | `state`, `*_timestamp`, `config_version` | auto/manual | inventory's `migratable`/`migration_note` carried through; UC-backed → manual |
| `global_init_script` | `script_b64` (inventory) | `script_id`, timestamps | auto | body already captured by inventory; >64 KB → `skipped_oversize` + warning (rare) |
| `cluster_library` | `library` (inventory) | `status` | auto | install-after-clusters on target; cluster ref remap target-side |
| `workspace_conf` | key/value (inventory) | — | auto | documented default key set |
| `app`, `lakebase_project` | — | — | manual | inventory-only; index rows + `manual/…json`, no create payload (master §6a) |

**DAB-deployed units** (D3 RESOLVED) (`deployed_by_dab == true`, already flagged by inventory for
jobs, pipelines, lakeview dashboards, legacy dashboards, alerts V2): mode = **`dab`**. For this
customer, bundle code is redeployed to the target by their **Azure DevOps release pipelines**,
not migrated file-by-file by this tool. Export records the unit with `export_status:"dab"` + note
"handled by DAB redeploy", and **does not** emit a create payload — so the reconciliation report
shows it as intentionally DAB-managed rather than a gap.

**Fingerprint** (`transforms.fingerprint(payload)`): canonical JSON (sorted keys, no whitespace)
of the stripped payload → `sha256`. Deterministic and stable across runs iff migratable content
is unchanged (master §9). Server ids/timestamps/state are stripped *before* hashing so a re-run
of an unchanged asset produces the identical fingerprint.

**ACLs — SEPARATE file (D5 RESOLVED).** ACLs are written to **`export/acls.json`**, NOT inside
each unit's payload. Reason: on the target side each grant's principal is a **source** entity id
(SP id, group id, user id) that must be **remapped to the new target entity id** — and for
Databricks-managed SPs/groups that new id doesn't exist until identity import runs and builds the
old→new map (master §7). So ACL application is its own target-side pass, after both the objects
and the identities exist. Keeping ACLs in one remappable file (rather than scattered across
payloads) makes that remap pass clean and lets each object's create payload stay principal-free.
`acls.json` shape: `[{asset_type, natural_key, source_id, perm_object_type, grants:[{principal, principal_type, permission_level}]}]`.
The export record keeps only an `acl_grants` **count** for the report. The `admins` group grant
and `inherited` entries are dropped **on import** (master §10a), not here — Export captures them
verbatim so the target has full information.

### 5a. Workspace content: fetch routes + oversize handling (UPDATED — verified live fvm1 2026-08-01)

Oversize workspace content must NOT read as a scary `failure` that makes a clean run look broken,
but must also not be *truly* silent. The route behaviour was **verified live** and the earlier
"tiered streaming rescues big notebooks" design was found **wrong** and removed:

**VERIFIED route facts (fvm1):**
- `GET /api/2.0/workspace/export?path=…&direct_download=true` returns **raw bytes**.
  - For a **FILE**, it carries the whole file with **no 10 MB cap** (tested an 11 MB CSV round-trip
    in a single call), up to the 500 MB workspace-files ceiling.
  - For a **NOTEBOOK** (add `format=SOURCE`), the base64/notebook body is capped at **~10 MB**;
    a larger notebook raises `MAX_NOTEBOOK_SIZE_EXCEEDED`.
- A **>10 MB notebook has NO API round-trip**: base64 `workspace/import` **rejects** it
  (HTTP 400, "exceeded max size"), and the streaming `workspace-files/import-file` route stores it
  as a plain **FILE**, not a notebook. So a big notebook simply cannot be recreated *as a notebook*.
- `workspace-files/export-file` does **not** exist (404) — that was a build-time bug, now removed.

**Fetch behaviour (`content_fetcher.py`), per unit:**
1. **FILE:** single `GET workspace/export?direct_download=true` (≤ 500 MB) → bytes to `content/` →
   `success`, `content_route="direct_download"`. >500 MB (Content-Length/stream guard) →
   `skipped_oversize` (out-of-band UC-Volume/cloud copy note).
2. **NOTEBOOK ≤ 10 MB:** `GET workspace/export?format=SOURCE&direct_download=true` → `success`.
3. **NOTEBOOK > 10 MB:** `export_status:"skipped_oversize"`, **NO bytes exported** (customer
   decision) — recorded with the reason "a >10 MB notebook cannot be migrated as a notebook via
   any workspace API; split it or recreate manually", listed in `oversize_artifacts.json` +
   `manual_actions.md`. The metadata row is still written (countable, reconcilable — never a gap).

**Why oversize isn't a `failure`:** size-policy skips, not errors — the run is still healthy. The
Excel colours `skipped_oversize` **amber-with-warning** (distinct from red `failure`); the Summary
sheet lists them under "Oversize — manual copy needed", separate from the failures table.

### 5c. Non-content workspace objects — dedup, don't drop (UPDATED — verified live)

The recursive `workspace/list` walk surfaces object types beyond DIRECTORY/NOTEBOOK/FILE/REPO —
observed live: **DASHBOARD** (`.lvdash.json`), **ALERT** (`.dbalert.json`), **MLFLOW_EXPERIMENT**.
These must be recorded (no-silent-gaps → 1:1 reconciliation) but NOT re-exported as file bytes:

- **DASHBOARD / ALERT files** are the on-disk *twins* of assets already exported via their NATIVE
  API (`lakeview_dashboard` / `alert_v2`). Export **dedupes by path**: if the file's path matches a
  native unit's `path`/`parent_path`, the file unit is marked **`covered`** ("exported via native
  `<type>` unit, not re-uploaded") — counted once, no double-create. If there's **no** native match
  (e.g. the Alerts V2 API returns `parent_path=None`, or a `.bundle/` dashboard the Lakeview API
  doesn't list), the file unit is `manual` ("no native asset match — review") so it's never a
  silent gap. Uploading the raw `.lvdash.json` is deliberately NOT done — it lands as a plain FILE,
  skipping the warehouse binding + UC deps the native create path handles.
- **MLFLOW_EXPERIMENT** → `manual`, labelled "MLflow is out of scope (assets-only migration)"; also
  verified not file-readable (`No access to read file`). Never exported.

### 5c. DAB bundle content — EXPORTED but NEVER IMPORTED (decided 2026-08-03)

Workspace content living under a bundle root (`<home>/.bundle/<bundle>/<target>`, the CLI's default
`workspace.root_path`) is captured in the bundle for reference but carries
`import_action: "dab_redeploy"` — the same action as the bundle-owned jobs/pipelines — so the whole
DAB story reads consistently. On fvm1 that is **44 of 148 units**: 21 files/notebooks + 23
directories, 34 KB of bytes.

**Why not import it.** Not merely redundant — actively harmful. `state/terraform.tfstate` maps
bundle resources to **SOURCE-workspace object ids** (verified live: `databricks_job` →
`627957782356291`). Landing it on the target makes the customer's next `databricks bundle deploy`
believe those resources already exist under those ids, so it updates or deletes the wrong objects.
`bundle deploy` recreates every one of these files anyway. Previously these read
`CREATE + UPLOAD`, and the bundle's own directories read `CREATE`.

**Why the bytes are still exported.** 34 KB, and if a customer's deployed bundle has drifted from
its git source, the exported `databricks.yml` is the only record of what was actually deployed.
Exporting is safe; *importing* is what is not.

**Detection = the `/.bundle/` path segment only** — deliberately simple, matched on the DIRECTORY
rather than on state filenames. The state format is already mid-migration (CLI ≥1.x
`state/resources.json` vs older `state/terraform.tfstate` — both appear live on fvm1) and the newer
direct-deployment engine drops Terraform altogether, so a filename-keyed rule would silently stop
covering what it protects. A customer overriding `root_path` is not detected, but the consequence
is a redundant upload, **never a skipped asset**: real DAB ownership of jobs/pipelines/warehouses
comes from `dab_registry` parsing the state files (§ DAB detection), not from this path check.

**`migration_mode` is deliberately left `auto`/`content`.** Changing it would drop these units out
of the per-asset payload files (`_write_artifact_files` writes only `auto`/`content`) and strand
their ACL grants — all 23 bundle directories carry grants. The units keep travelling with their
permissions; `import_action` is the only skip signal. **Import contract:** branch on
`import_action`, not `migration_mode`, and skip ACL replay for any object the importer did not
create. Recorded in project memory for the importer work.

Each bundle root gets ONE row in `manual_actions.md` ("redeploy this bundle against the target"),
not one row per file.

### 5b. DBFS files — OUT OF SCOPE for v1 (review Q4 — D7 RESOLVED)

**DBFS is out of scope for v1** — not inventoried, not exported. DBFS has a REST API (`dbfs/list`,
`dbfs/read`; writes via `dbfs/put` 1 MB base64 cap or the streaming `create`→`add-block`→`close`),
so it *could* be migrated, but it's deliberately excluded because it's legacy (superseded by UC
Volumes), typically holds large data/jars impractical to move file-by-file through an API, and this
customer is already on UC Volumes. Migrating it now would add cost for little value.

**Deferred as a future feature** (customer-gated): if the customer confirms they need it, DBFS
becomes a **cross-module addition** — a `DbfsCollector` (inventory), a `dbfs_fetcher` (export of
`dbfs:/`-referenced artifacts via the streaming API), and a matching importer — added to all
stages together. Not built until then. No `migrate_dbfs` widget in v1.

> **Note for whoever picks this up later:** the natural v1.x scope is *referenced-only* — export
> just the small jars/init-scripts that migrated clusters/jobs/GIS point at (`dbfs:/...` in their
> specs), since those are what actually break on target; leave bulk data to out-of-band storage
> tooling. Full DBFS byte-migration is unlikely to ever be worth it for a UC-Volumes customer.

---

## 6. Deliverables (files)

**Export engine (`src/exporters/`):**
- `export_runner.py` — orchestrator, mirrors `collectors/inventory_runner.py`. Loads
  `inventory.json` (or triggers inventory), iterates asset types honouring toggles, calls each
  normalizer, fetches content, collects ACLs → `acls.json`, writes per-asset files +
  `export_index.json`, renders `export_status.xlsx` (§6a), then `ArtifactWriter.write_manifest(...)`.
  Fail-soft + resumable via `export_checkpoint.json` (§7a). Returns a summary dict.
- `asset_export.py` — the **spec-driven normalizer registry**: `ASSET_SPECS[asset_type]` =
  `{source_object_type, unit_builder, strip_fields, mode_fn, natural_key_fn, size_limit}`. Most
  assets are declarative (strip + copy); a few (identity multi-type, jobs, dashboards) get small
  custom builders. Produces the per-unit export records (§3).
- `content_fetcher.py` — notebook/workspace-file **bytes** via the size-tiered fetch (§5a):
  base64 `workspace/export` → streaming fallback → `skipped_oversize`; path mangling → `content/`
  filenames; `content_kind`/`content_route` tags; case-insensitive collision guard; returns
  `content_ref`. **Runs under the parallel fetch pool (§7c)** — it is the hot path.
- `parallel.py` (or a helper in `utils/`) — a small bounded-`ThreadPoolExecutor` map used by the
  content pass (§7c): submit one fetch task per content unit, gather results, thread-safe
  index/checkpoint updates. New shared utility (the codebase has no concurrency today).
- `acl_writer.py` — collects every object + secret-scope ACL from inventory into `export/acls.json`
  keyed by `(asset_type, natural_key, source_id)` (D5). Principals stay as source ids (remap is
  target-side). Surfaces the per-object `acl_grants` count onto each export record.
- `base_exporter.py` *(optional)* — thin base if custom builders want shared strip/fingerprint
  helpers; may be folded into `asset_export.py` if it stays small.

**Inventory addition (`01_Inventory`):** write **`LATEST_INVENTORY.json`** (a 3-field pointer:
`run_id`, `generated_utc`, `counts`) at `wsmig/<src_ws_id>/` at the end of the run, so Export can
resolve the run_id when the two run separately (§2b). Tiny, not a data file.

**Transform helpers (`src/transform/transforms.py`):** `strip_runtime(asset_type, payload)`
(per-asset strip registry) + `fingerprint(payload)` (canonical-JSON sha256) + `normalize()`.
(Reference remap functions in this module stay stubs — they're target-side, Plans 3–7.)

**Report (`src/exporters/export_excel.py` or extend `excel_generator.py`):** `export_status.xlsx`
(§6a) — the inventory workbook + an **Export Status** column per row. Rendered via the same
`/tmp`→Volume byte-copy path inventory uses (openpyxl-on-FUSE gotcha).

**Notebook:** `notebooks/02_Export.py` — widgets (same as inventory + toggles, plus
`content_fetch_workers` [default 8, §7c], `force_full_export` [default false, §7a]) → bootstrap `src/` onto
`sys.path` → `%pip install -r requirements.txt` → `Config.from_dbutils` (assert `role == "source"`)
→ **resolve run_id (§2b): widget, else `LATEST_INVENTORY.json`, else fail** → print resolved run_id
→ `build_client` → `ExportRunner(...).run()` → print summary. Thin. `run_id` widget help documents
the "blank = use latest inventory / set to resume a specific run" behaviour.

**Report seed:** `export_index.json` + `export_status.xlsx` (this plan). The JSON feeds Plan 8's
full three-column reconciliation report; the Excel is the immediate human artifact the operator
reviews right after export.

---

## 6a. Post-export Excel (`export_status.xlsx`) — review point 1

The operator's checkpoint after export. Base = the **inventory Excel** (same sheets, order,
icons, columns — reuse `reports/inventory_view.py` + `excel_generator.py`), with **two columns
added to every per-asset sheet**, both joined from `export_index.json` on
`(asset_type, natural_key)`:

**(1) Export Status** — did EXPORT capture this unit?

| Export Status | Meaning | Cell colour |
|---|---|---|
| `Success` | create-ready payload (and content bytes, if applicable) written | green |
| `Failure` | errored during export — cell shows the `note` (reason) | red |
| `Skip` | asset toggle off (`migrate_<x>=false`) | grey |
| `Manual` | Genie / secret values / app / lakebase / UC-backed serving | amber |
| `Skipped (DAB)` | bundle-owned: recorded in the ledger, create payload deliberately **not** captured (the customer's bundle redeploy owns the definition) | blue |
| `Covered (native)` | the on-disk twin of an asset exported via its native API (no double-create) | cyan |
| `Skipped (oversize)` | workspace content over the API size limit → copy manually (§5a) | amber + ⚠ |
| `Incomplete` | a traversal cap cut a listing off mid-way — partial, `note` explains | orange |

**(2) Import Action** — what will the TARGET side DO with it? (`import_action` in the index)

| Import Action | Meaning | Cell colour |
|---|---|---|
| `CREATE on target` | the utility creates it via REST | green |
| `CREATE + UPLOAD content` | create the object, then push its bytes (notebooks / files) | green |
| `INSTALL on target` | attached to an existing object (cluster libraries) | green |
| `SET on target` | written via the workspace conf API | green |
| `APPLY ACL on target` | permission grants replayed once the objects exist | green |
| `ASSIGN (must pre-exist)` | account-level identity: assign + entitle, **never** create (creating an Azure UMI mints a new applicationId and orphans its ACLs) | blue |
| `ADD MEMBERS (group exists)` | built-in group: PATCH members onto the existing group | green |
| `DAB REDEPLOY (import skips)` | the customer's bundle pipeline recreates it | blue |
| `NONE — via native asset` | created as a side effect of its native asset | cyan |
| `MANUAL on target` | no REST path — a human does it | amber |
| `REVIEW REQUIRED` | low-confidence classification; confirm before import | amber |
| `NOT EXPORTED (nothing to import)` | toggled off, or export failed | grey |

**Two columns because one cannot carry both meanings.** A bundle-owned job is `Skipped (DAB)` on
export yet still lands on target — via the bundle redeploy, which only the action column says.
The action column originally existed on the identity sheets alone, which left every other tab's
intent to be inferred from its status (the customer read `DAB` as "not exported"). Actions are a
closed vocabulary (`asset_export.IMPORT_ACTIONS`) and the Excel label map is asserted against it
at import time, so a new action can't be added without a label and silently render `—`.

**(3) `Deployed by DAB` on every tab whose asset CAN be bundle-owned (added 2026-08-04).** The
status/action pair told the reader what happens to a bundle-owned asset but never that it *was*
bundle-owned — only jobs, pipelines, alerts, dashboards, Genie spaces and workspace content had the
flag column, so on the warehouse/cluster/pool/scope/serving tabs "Skipped (DAB) + DAB REDEPLOY" was
the *first* hint, and it had to be inferred. Those five are exactly the **pathless** assets whose
DAB-ness cannot come from a `.bundle/` path and is instead stamped from the bundle state files by
`inventory_runner._stamp_dab_ownership` (+ legacy SQL dashboards, from their `parent` path). The
data was already collected; only the column was missing. Declared once in
`inventory_view._COLUMNS` and populated in `adapt()`, so it propagates to **all three** renderers
(inventory HTML, inventory Excel, `export_status.xlsx`) — this is an inventory-layer change that
Export inherits. Same `Manual / DAB (Shared) / DAB (User)` vocabulary as the jobs tab
(`helpers.dab_deploy_label`).

> **`apps` deliberately excluded.** `apps` IS a DAB resource type in
> `dab_registry._RESOURCE_KIND_TO_ASSET` but is NOT in `_DAB_STAMP_TARGETS`, so app records are
> never stamped. Giving that tab the column would print "Manual" for a bundle-owned app — a new
> way to mislead. Adding `app` to the stamp targets needs a live check that the state `__id__`
> matches the app `name`; until then the column stays off apps.

**ACL-sheet rows on bundle content say `DAB REDEPLOY`, not `APPLY ACL` (fixed 2026-08-04).** The
"Object Permissions (ACLs)" sheet joins nothing from the index — `_resolve_status` hardcoded
`("success", "apply_acl")` for **every** grant. But `_flatten_acls` emits a row per grant for every
workspace object including `DIRECTORY`, so grants on bundle-root content (all 23 bundle directories
on fvm1 carry them) read `APPLY ACL on target` — an action the importer will never take, since it
replays ACLs only for objects it created and it creates nothing under a bundle root (§5c). The
**status stays `success`** (`acls.json` genuinely captured the grant; export is not at fault); only
the action changes, matching how bundle-owned jobs read. Keyed off the row's `object_key` path via
the same `is_dab_content_path` predicate the units use, so the two can't drift.

**`is_dab_content_path` matches the `.bundle` container dir itself (fixed 2026-08-04).** The
predicate tested for the `"/.bundle/"` segment *with* a trailing slash, so `/Shared/.bundle` and
`/Users/<email>/.bundle` — the directories that exist purely to hold bundle state — fell through to
`import_action: "create"` while every file inside them correctly read `dab_redeploy`. Affected
`export_index.json` directly.

Where a caveat exists the unit's `note` is appended to the action cell — e.g.
`CREATE on target — UC tables in serialized_space must pre-exist on target`.

Plus a **Summary sheet** roll-up: per asset_type counts of each status; an **Import Action
roll-up** (how much the tool does vs. the bundle vs. a human — the one question the per-type
status table can't answer, since one asset_type spans several actions); a top **failures**
table (red rows, with reason) so real problems surface first; and a separate **"Oversize — manual
copy needed"** table (the `skipped_oversize` rows with source path + size), kept apart from
failures so the two aren't conflated.
Every inventoried row gets **both** a status and an action — no blank cells — which is exactly the
"True/False for exported" tie-back the review asked for, made human-readable.

---

## 6b. User-scoped content lands in the exact user path — review point 3

Notebooks/files under `/Users/<email>/…` must recreate at the **same path** on target, and that
path only exists once the **user exists** there. This is fundamentally a **target-side (import)**
concern, but Export's job is to make it possible and to flag the prerequisite:

- **Export captures the full absolute source path verbatim** on every workspace object (already
  in inventory: `path`, `is_user_root`), so import can recreate at the identical path.
- **The path IS the create target.** `POST /api/2.0/workspace/import` (and mkdirs) take the
  destination `path` directly — so import writes user content to `/Users/<email>/…` by supplying
  that path. There's no separate "owner" field on import; the containing `/Users/<email>` home
  dir must simply already exist. (Home dirs **cannot be mkdir'd** — master §10a; they're auto-
  created when the user is provisioned/first assigned.)
- **Prerequisite chain (enforced by import order, master §4):** identity import (users assigned)
  → their home dirs exist → workspace content import can target `/Users/<email>/…`. Export makes
  this checkable by emitting, per user-scoped content unit, the **owning user** (derived from the
  `/Users/<email>/` path segment) into the export record, and by ensuring that user appears as a
  `user` unit. If a content path references a user **not** in the identity export, Export flags it
  `note: "owner <email> not in identity export — home dir won't exist on target"` so the gap is
  visible before import, not discovered as a failure during it.
- Any per-user domain/email **remapping** (`user_id_mapping`, `user_domain_mapping`) that changes
  the target path is a **target-side transform** (`03_Transform_Review`) — Export keeps the
  original source path; the remap rewrites it at import. (Same-account, same-region: paths are
  expected to be identical, so remap is usually a no-op here.)

---

## 7. Cross-cutting mechanics (carried from master §8–§9)

- **Toggles:** `export_runner` skips an entire asset family when its toggle is `false` (e.g.
  `migrate_jobs=false`). Skipped units are recorded with `export_status:"skip"` (so the Excel/
  index still show the row + reason), not silently dropped. Toggles come from the widgets.
- **Manifest last:** after all writes, `write_manifest(asset_counts)` walks the dir and records
  sha256 per file so the target can `verify_manifest()` before importing (master §8 handoff
  integrity).
- **Auth / no cross-workspace:** same context-token client as inventory; content fetch hits only
  the source workspace. No target calls, no secrets, no OAuth M2M.
- **Logging:** `execution_export.log`, alongside inventory's `execution_inventory.log` (they used
  to share one filename, interleaving two runs' records in one file).
  **Records are appended to a LOCAL `/tmp` file and mirrored onto the Volume** every 25 records,
  on every WARNING/ERROR, and on the final `flush_log_file()` the notebook calls. Reason: a UC
  Volume rejects `open(path, "a")` once the file exists, and the logger deliberately swallows its
  own errors (logging must never break the pipeline) — so a whole live export wrote a **one-line**
  log with every subsequent record silently lost (observed on fvm1 2026-08-03, runs `20260803_131153`
  and `20260803_150903`). Same local-then-copy pattern as the .xlsx writer. Regression-tested
  against a simulated append-rejecting filesystem in
  `test_export::test_logger_survives_a_filesystem_that_rejects_append`.
  `manifest.json` **excludes** `execution_*.log`: the log is still being written after the manifest
  is built (the final flush), so checksumming it would fail `verify_manifest()` on every run. The
  log is a diagnostic, not part of the handoff.

### 7a. Resumable checkpointing — read state from the Volume, pick up where it left off (review)

A full export of 100+ workspaces can be interrupted (job timeout, transient API failure, operator
stop). A re-run must **not** re-do completed work — it must read the prior progress **from the
bundle in the Volume** and continue. Mechanism:

- **`export_checkpoint.json` lives in the run's bundle dir on the Volume** (not in memory, not
  local `/tmp`) — so it survives job death and is the same file a re-run reads. `ArtifactWriter`
  already has `is_done(component, item_key)` / `mark_done(...)` backed by a JSON file on the
  Volume; Export uses it keyed by `component="export:<asset_type>"`, `item_key=natural_key`.
- **Content-pass granularity, flushed in BATCHES (`CHECKPOINT_BATCH = 200`).** Only the content
  pass is checkpointed — everything else is an in-memory transform of `inventory.json`, rebuilt in
  seconds, so tracking it would cost more than redoing it. A unit is recorded only once its bytes
  are on disk, so an interruption mid-write never marks a partial unit complete.
  **Why batches:** every flush REWRITES the whole checkpoint (append does not work on a UC
  Volume), so a flush per file is O(n²) bytes — ~3.7 GB for 5k notebooks — while a single flush at
  the end of the pass loses all download progress on a crash. 200 costs ~26 rewrites (~20 MB) for
  5k files and bounds the re-fetch after a crash to one batch.
- **The checkpoint stores each item's OUTCOME, not just its key** (`"export:content:results"`,
  written atomically with the key list). Without this, resume was **dead code for the case it
  existed for**: rebuilding a resumed unit needs its `export_status`/`content_ref`, which used to
  come from the previous `export_index.json` — but the index is written only AFTER the content
  pass, so following a crash it is absent and every file was re-fetched despite a valid
  checkpoint. Caught by simulating a real mid-pass crash; regression-tested in
  `test_export::test_content_pass_resumes_after_a_crash`. The prior index is still consulted as a
  fallback so checkpoints from older runs keep working.
- **Re-run flow:** on start, `export_runner` loads `checkpoint.json` from the Volume; for
  every unit it first checks `is_done(...)` and **skips** already-completed units (their recorded
  outcome is restored), only exporting the remainder. Content bytes already in `content/` are
  left untouched. The final `export_index.json`, `export_status.xlsx`, and `manifest.json` are
  always regenerated at the end so they reflect the **complete** bundle (done-before + done-now).
- **Fingerprint interplay:** because the fingerprint is deterministic, a resumed unit produces the
  same fingerprint it would have on a clean run — so resume is content-identical to a full run.
- **Fresh-run override:** a `force_full_export` widget (default `false`) ignores the checkpoint and
  re-exports everything (for when the operator wants a guaranteed-clean bundle). Deleting the run
  dir also forces a clean run.

**Idempotency is stronger than resume (review — always-safe re-runs).** Re-exporting is **always
safe** regardless of resume: every unit is content-addressed by `(asset_type, natural_key)` and
written **deterministically** (same source input → identical bytes + identical fingerprint), so a
redo overwrites like-for-like and never duplicates or corrupts. The checkpoint is purely a
*don't-waste-time* optimization layered on top of that guarantee — not a correctness crutch. So the
worst case of a "missed" resume is wasted work, never a bad bundle.

**Resume is driven by bundle completion state, NOT `run_id` equality (review — survive a full-job
re-run).** The earlier "must reuse the same `run_id`" model only resumed on a *task-level* re-run;
a **whole 2-task job** re-run would have Inventory mint a new snapshot `run_id` and Export start
over. To fix that, resume keys off whether the latest bundle is **finished**:
- A bundle is **complete** only once **`manifest.json`** is written (the very last step). So a
  bundle dir with an `export_checkpoint.json` but **no `manifest.json`** is provably an *interrupted*
  run.
- **`02_Export`** (blank `run_id`): find the latest bundle for this `source_ws_id`; if it's
  **incomplete** → resume it (read its checkpoint); if complete → nothing to do / start a new run
  only when asked.
- **`01_Inventory`** is made idempotent to match: on re-run, if a recent **incomplete** bundle
  exists for this `source_ws_id`, **reuse its `run_id`** (continue the attempt) rather than minting a
  new snapshot — unless the operator passes an explicit `run_id` or sets `force_full_export`. It
  refreshes its own inventory artifacts in place (deterministic), so the bundle stays coherent.
- **Net:** a plain **whole-job re-run now auto-resumes** the in-flight migration. A brand-new
  snapshot happens only when the operator explicitly sets a new `run_id` (or `force_full_export`,
  or the previous bundle already completed). Explicit `run_id` always wins, so deliberately
  concurrent migrations of the same workspace just use distinct ids.
- **Precedence recap:** explicit `run_id` widget/task-value → else latest **incomplete** bundle
  (resume) → else `LATEST_INVENTORY.json` pointer (for Export-only, §2b) → else fresh run. This is
  the "auto-detect latest incomplete run" model (previously flagged open in §11b) — now adopted.

### 7b. Failure recording — loud + noted everywhere (review point 4 + D4)

Nothing is *truly* silent, but outcomes are graded so real errors stand out from routine skips.
Every non-success outcome is captured in **three** places (index + log + Excel):
- **`export_index.json`** — the unit's `export_status` + `note` = the concrete reason.
- **`execution_export.log`** — a log line with asset_type, natural_key, reason (severity per below).
- **`export_status.xlsx`** — colour-flagged row + inline reason, rolled up in the Summary sheet.

Three distinct severities so the operator isn't cried-wolf at:
- **`failure` (ERROR, red):** an unexpected exception (API error, malformed payload). Fail-soft —
  caught, recorded, run continues; one asset never aborts the bundle (collectors' `_safe`).
- **`skipped_oversize` (WARNING, amber+⚠):** workspace content past the API size limit that even
  the streaming fallback can't carry (§5a). Expected + common → a warning, not an error; listed in
  `oversize_artifacts.json` with the manual-copy alternate. NOT counted as a failure.
- **`incomplete` (WARNING, orange):** a **traversal/listing cap** (`max_workspace_items` /
  `max_ws_api_calls`) cut a listing off mid-way, so some units were never enumerated. Raised as an
  explicit `INCOMPLETE —` warning (inventory's `warnings` convention) — a partial listing is always
  visibly partial (fail-loud, D4). Distinct from per-object oversize skips.

> **Design note (the "2nd thought"):** per-object oversize is deliberately its own
> `skipped_oversize` category, kept OUT of the failure count, because it's routine. The
> fail-loud rule still applies to *listing truncation* (`incomplete`) — that's a different risk
> (assets you never saw at all) and must stay loud.

### 7c. Parallel content fetch — the hot path (review Q1)

The content pass (one `workspace/export` call per object) is the tool's
slowest step and the **only** part worth parallelizing (everything else is in-memory transform of
`inventory.json`, which stays serial). The codebase has **no concurrency today**, so this is a new,
contained mechanism:

- **Bounded `ThreadPoolExecutor`** (`content_fetch_workers` widget, default **8**; tunable, dial
  down if the API pushes back). Submit one fetch task per content unit; gather as they complete.
- **Thread-safety:**
  - The `ApiClient` wraps a `requests.Session` (connection-pooled; concurrent GETs are safe) and the
    existing `with_retry` / 429-backoff wraps every call, so throttling is handled per worker.
    `client.warnings` appends must go under a lock (append is not atomic across threads).
  - Byte writes are **per-file** into `content/` → no contention.
  - The **shared** structures — `export_index` unit list, `export_checkpoint.json`,
    `oversize_artifacts.json` — are updated under a single lock (the logger already models this with
    its module `_LOCK`). Checkpoint flush is debounced (batch, not once-per-file) to avoid hammering
    the Volume; final flush guaranteed at the end.
- **Fail-soft per task:** a worker exception becomes that unit's `failure`/`skipped_oversize` record
  (§7b) and never propagates to kill the pool — one bad notebook doesn't stop the other 999.
- **Ordering-independent:** results are keyed by `(asset_type, natural_key)`, so completion order
  doesn't matter; the index is assembled by key, not append order.
- **Rate-limit safety valve:** if 429s spike, workers already back off individually; the worker
  count cap bounds total concurrent pressure. (A future refinement could auto-throttle the pool, but
  v1 keeps it a fixed cap for predictability.)

Net effect: thousands of notebooks fetch in parallel batches instead of a serial crawl, while the
bundle-integrity guarantees (checkpoint, index, manifest) stay correct under concurrency.

> **`parallel.py` is a shared, reusable primitive — Plan 1 follow-up (inventory enrichment).**
> The bounded-pool + thread-safe-collect helper built here is deliberately generic (not
> export-specific), because inventory has the *same* "N independent GETs" hot path: the per-object
> **ACL/detail enrichment** (`permissions/<type>/<id>` per object, plus DLT/dashboard/repo/GIS
> detail fetches) that today runs serially and dominates inventory's wall-clock. A recommended
> **Plan 1 revision** would parallelize only inventory's **`enrich()` phase** (the flat, ordering-
> independent per-object fetches) using this exact helper — while keeping the recursive
> `workspace/list` **discovery walk serial** (it owns the `max_ws_api_calls` budget counter and
> `_repo_ids` set, which would race under threads, and its truncation must stay deterministic).
> That change also needs `base_collector.run()`'s per-collector `warnings`-length snapshot made
> thread-safe. **Out of scope for Plan 2 — flagged here so the primitive is designed for reuse and
> the inventory speedup is tracked as its own follow-up, not mixed into export.**

---

## 8. API size limits (review point 2) — verified per element

Researched against docs.databricks.com (verified 2026-07-31; saved to project memory). Export
uses the size-tiered fetch (§5a) to carry as much as the APIs allow, and only past the largest
route does it record a non-alarming `skipped_oversize` (never a hard failure):

| Element | Create/import API | Documented limit | Export behaviour past limit |
|---|---|---|---|
| **Notebook** | `workspace/import` (base64) → **fallback** `workspace-files` streaming | base64 **~10 MB**; streaming **500 MB** | >10 MB → streaming route (still `success`); >500 MB → `skipped_oversize` + warning (§5a) |
| **Workspace file** (non-notebook) | `workspace-files/import-file/<path>` (streaming) | **500 MB / file** (raisable) | >500 MB → `skipped_oversize` + `oversize_artifacts.json` (manual cloud-storage copy) |
| **Secret value** | `secrets/put` | **128 KB / value** | N/A — never exported (always `manual`); note the cap in `manual_actions.md` |
| **Global init script** | `global-init-scripts` | **64 KB** | >64 KB → `skipped_oversize` + warning (rare) |
| **Cluster policy definition** | `policies/clusters/create` | no documented byte limit | none; export full definition |
| **(reference) DBFS put** | `dbfs/put` base64 `contents` | 1 MB (streaming for larger) | not used by Export (workspace-files is the file route) |

> **Import route matters:** small notebooks go through base64 `/workspace/import` (10 MB); larger
> notebooks + all non-notebook files go through the **path-based streaming** route (500 MB). Export
> records each content unit's `content_kind` (`notebook`|`file`) **and** the `content_route`
> (`base64`|`streaming`) it used, so the import side picks the matching endpoint. Past 500 MB →
> out-of-band cloud-storage/Volume copy (listed in `oversize_artifacts.json`), never a failure.

## 8a. API / behaviour verification matrix (customer instruction — master §6b)

Export adds exactly **one** new source API beyond inventory; everything else is transformed from
`inventory.json`. Verify + test on a real workspace before relying on it:

| Concern | API / behaviour | Action |
|---|---|---|
| Notebook content | `GET /api/2.0/workspace/export?path=…&format=SOURCE&direct_download=true` | confirm returns raw bytes (not base64-wrapped) with `direct_download`; test a notebook + a plain FILE |
| Notebook format | SOURCE (D2 resolved) | confirm SOURCE round-trips on import (Plan 5) for `.py`/`.sql`/`.scala`/`.r` |
| **Streaming fallback (§5a tier 2)** | `workspace-files` streaming export/import | **VERIFY** a SOURCE notebook >10 MB fetched via the streaming route round-trips as a *notebook* (not opaque file) on import; if not, that object degrades to tier-3 `skipped_oversize` (never false `success`) |
| Size caps | §8 table | test the tier boundaries: ≤10 MB base64, 10–500 MB streaming, >500 MB → `skipped_oversize`+warning+`oversize_artifacts.json` (NOT failure) |
| Listing-cap truncation | `max_workspace_items` / `max_ws_api_calls` | verify a truncated *listing* raises `incomplete` (fail-loud) — distinct from per-object oversize |
| Fingerprint stability | strip completeness | round-trip test: export an unchanged asset twice → identical fingerprint (no volatile field leaked into the payload) |
| GIS body | reuse `script_b64` | confirm inventory's captured base64 body is complete (no per-id re-fetch needed) |
| Secret values | (negative) | confirm the API exposes **no** value read path → `secret_value` units are always manual |
| Resume (completion-state) | `export_checkpoint.json` + `manifest.json` | interrupt mid-export, then test BOTH: (a) re-run just the Export task, and (b) re-run the whole 2-task job — confirm each resumes the incomplete bundle (Inventory reuses its run_id), skips done units, finishes complete (§7a) |
| **Parallel fetch (§7c)** | `ThreadPoolExecutor` + `requests.Session` | confirm concurrent GETs are safe; measure speedup vs serial; verify index/checkpoint stay correct under threads; confirm 429 backoff still fires per worker |
| **run_id via task values (2-task job)** | `dbutils.jobs.taskValues` | confirm the Inventory task can `set` run_id and the Export task `get`s it → widget populated automatically, no typing (§2b path 1) |
| **run_id via pointer (separate runs)** | `LATEST_INVENTORY.json` | run 01 then 02 as **separate** runs (blank run_id on 02) → confirm Export resolves + prints the right run_id; blank+no pointer → fails loudly (§2b path 2) |

---

## 9. Build order within Plan 2

1. `transform/transforms.py`: `strip_runtime` registry + `fingerprint` + `normalize` (+ unit tests
   on sample `_raw` payloads).
2. `01_Inventory` addition: write `LATEST_INVENTORY.json` pointer at run end (§2b).
3. `exporters/asset_export.py`: `ASSET_SPECS` registry + unit builders (declarative first, then
   identity/jobs/dashboards custom).
4. `exporters/parallel.py` (or `utils/`): bounded `ThreadPoolExecutor` map + thread-safe collectors
   (§7c). Small, tested in isolation first (the codebase has no concurrency yet).
5. `exporters/content_fetcher.py`: size-tiered fetch (§5a — base64 → streaming fallback →
   `skipped_oversize` + `oversize_artifacts.json`) + path mangling + `content_route`/`content_kind`
   tags; runs under the §7c pool. **Smoke test** a notebook + file; **verify streaming round-trip (§8a)**.
6. `exporters/acl_writer.py`: collect ACLs → `export/acls.json` + `acl_grants` counts (D5).
7. `exporters/export_runner.py`: orchestrate — resolve run_id (§2b), load inventory.json, iterate,
   honour toggles, run parallel content pass, resumable checkpoint (§7a), write per-asset files +
   `export_index.json`, `write_manifest`.
8. `exporters/export_excel.py`: `export_status.xlsx` = inventory workbook + Export Status column
   + Summary/failures/oversize sheets (§6a).
9. Wire `notebooks/02_Export.py` (thin) incl. run_id resolution, `content_fetch_workers`,
   `force_full_export` widgets; wire the 2-task-job task-values handoff (§2b path 1).
10. Run on a real source workspace right after `01_Inventory` — test **both** as a 2-task job
    (task-values auto-pick) **and** as separate runs (pointer auto-pick, §2b); verify §8/§8a matrix
    incl. parallel speedup; verify `export_index.json` reconciles 1:1 against `inventory.json`
    counts; interrupt + resume test (§7a).

---

## 10. Definition of done

- `02_Export` runs end-to-end **inside a real source workspace** (run-as workspace-admin SP,
  context token), right after inventory, writing the `export/` tree + `acls.json` +
  `export_index.json` + `export_status.xlsx` + `manifest.json` to `source_staging_location`.
  No target calls; no secrets/OAuth M2M.
- Every **migratable** unit has a **create-ready payload** (not just metadata) in its per-asset
  file; content assets have their **bytes** in `export/workspace/content/` with a `content_ref`.
- **ACLs are in `export/acls.json`** (separate from payloads), principals as source ids, ready for
  target-side remap (D5).
- **Every inventoried unit** appears in `export_index.json` **and** as a coloured row in
  `export_status.xlsx` with an explicit `export_status`
  (success/failure/skip/manual/dab/skipped_oversize/incomplete) + (when not success) a human `note`
  — the exported True/False tie-back, no silent gaps.
- **Outcomes are graded** (§7b): real errors → `failure` (ERROR, red); routine oversize workspace
  content → `skipped_oversize` (WARNING, amber) in `oversize_artifacts.json`, NOT a failure; a
  truncated *listing* → `incomplete` (WARNING, fail-loud). All recorded in index + log + Excel.
- **Oversize content is rescued where possible** (§5a): >10 MB notebooks go via the streaming route
  and still succeed; only >500 MB → `skipped_oversize` with a manual cloud-storage copy note.
- Each unit carries a stable **`natural_key` + `fingerprint`**; re-exporting an unchanged asset
  yields the **identical fingerprint** (state store can skip on re-run — master §9).
- **Content fetch is parallelized** (§7c): a bounded thread pool (`content_fetch_workers`) drives
  the per-object `workspace/export` calls; index/checkpoint/oversize collectors are thread-safe;
  measurable speedup vs serial; 429 backoff still fires per worker.
- **run_id auto-resolves both ways** (§2b): in a 2-task job the Inventory task publishes run_id via
  task values → Export widget auto-populated; run separately, Export reads `LATEST_INVENTORY.json`;
  widget override always wins; no source → fails loudly. Resolved id is printed either way.
- **DBFS is out of scope** (§5b, D7) — not inventoried/exported in v1; deferred as a customer-gated
  future feature.
- **Resumable + idempotent:** re-exporting is always safe (content-addressed + deterministic — no
  dupes/corruption). Resume is driven by **bundle completion state** (`manifest.json` present?), so
  an interrupted run auto-resumes on **either** a task-level **or** a whole-job re-run — Inventory
  reuses the incomplete bundle's `run_id` rather than minting a new snapshot (§7a).
- User-scoped content records its owning user + path; content whose owner isn't in the identity
  export is flagged (home-dir prerequisite, §6b).
- Toggles honoured; DAB / manual / UC-backed units recorded (not created) with reasons.
- Fail-soft (one asset failing never aborts the bundle); `manifest.json` checksums verify via
  `ArtifactWriter.verify_manifest()`.

---

## 11. Decisions (resolved at review 2026-07-31)

- **D1 — Freshness vs reuse → RESOLVED: REUSE.** Export reuses `inventory.json` from the same
  `run_id` (single read-source layer; automatic tie-back), and only re-runs inventory if absent. (§2)
- **D2 — Notebook wire format → RESOLVED: SOURCE.** Export notebooks as `SOURCE`
  (`.py`/`.sql`/`.scala`/`.r`), git-folder friendly and diff-able (master §11.4). No DBC in v1. (§5)
- **D3 — DAB-deployed units → RESOLVED: index-only, no payload.** Do not emit create payloads for
  `deployed_by_dab` jobs/pipelines/dashboards/alerts (customer redeploys via Azure DevOps bundle
  pipelines); record them as `export_status:"dab"` for reconciliation only. (§5)
- **D4 — Large trees / oversize objects → RESOLVED: GRADED (revised at 2nd-thought review).**
  Two distinct cases, deliberately handled differently (§7b):
  (a) **Per-object oversize workspace content** is routine → rescued via the streaming fallback
  where possible (§5a); only past 500 MB does it become a low-key **`skipped_oversize` WARNING**
  (listed in `oversize_artifacts.json` for manual copy), **NOT** a failure and **not** counted as
  one. (b) **Listing truncation** from `max_workspace_items` / `max_ws_api_calls` stays **fail-loud**
  as `incomplete` (assets never enumerated is a real gap). Neither is ever silent. (§5a, §7b, §8)
- **D5 — ACL placement → RESOLVED: SEPARATE FILE.** ACLs go to `export/acls.json` (not inside
  payloads) because target-side ACL application must first remap each grant's principal (SP/group/
  user id) to the new target entity id, which only exists after identity import. Payloads stay
  principal-free; the export record keeps an `acl_grants` count. (§5)
- **D6 — run_id across separate runs → RESOLVED: task-values (2-task job) + pointer (separate) +
  widget override.** Two auto-pick paths, both zero-typing: (1) in a **2-task job**, the Inventory
  task publishes run_id via `dbutils.jobs.taskValues`, the Export task reads it → widget populated
  by the job wiring; (2) run **separately**, `01_Inventory` writes `LATEST_INVENTORY.json` and Export
  reads it when the widget is blank. An explicitly-set widget always wins (deliberate control /
  resume of a specific run); no source → fail loudly. Options A–D weighed in §2b.
- **D7 — DBFS → RESOLVED: OUT OF SCOPE for v1.** Not inventoried or exported. Deferred as a
  customer-gated future feature (cross-module: collector + fetcher + importer, added together only
  if the customer confirms the need). No `migrate_dbfs` widget in v1. (§5b)

### 11a. Content-fingerprint (review Q2 — confirmation, not a change)

Confirmed: the **content fingerprint IS the change-detection signal**. It is `sha256` of the
normalized, runtime-stripped payload; on re-run the target state store compares stored vs new →
**same = skip, different = the asset changed = update** via its edit API (master §9). `natural_key`
does identity/matching; the fingerprint does "did it change". No change to the plan — this just
confirms the intent.

### 11b. Resume model (review — RESOLVED: auto-detect incomplete run)

Resume is driven by **bundle completion state**, not `run_id` equality — so **both** a task-level
re-run and a **whole 2-task-job re-run auto-resume** the in-flight migration (§7a). A bundle is
"complete" only when `manifest.json` is written; an incomplete bundle (checkpoint, no manifest) is
resumed. `01_Inventory` reuses an incomplete bundle's `run_id` rather than minting a new snapshot.
A fresh snapshot happens only on an explicit new `run_id` or `force_full_export`. Underlying
everything: re-export is always idempotent (content-addressed + deterministic), so resume is an
optimization, never a correctness requirement.

### 11c. Confirmed defaults

- **Parallelism:** `content_fetch_workers=8` starting cap — confirmed acceptable (review); tune per
  workspace if the API pushes back.
```
