# Plan 1 — Setup (Foundation) + Inventory  (SOURCE side)

> Sub-plan of `plans/PLAN_0_master.md` (master). Review gate for the first slice of real code.
> Scope: the reusable **foundation** (`src/` core) + the **`01_Inventory`** notebook.
> **Runs INSIDE the source workspace** (air-gapped model — see master §1, §3).
> After this is approved and built, Plan 2 (Export, also source-side) follows.

---

## 1. Objective

Stand up the shared plumbing every later stage depends on, and deliver a **read-only
inventory** of the source workspace that:
- runs **inside the source workspace** as a **workspace-admin run-as SP**, authenticating with
  the **notebook-context token** — no OAuth M2M, no cross-workspace calls, no secrets,
- enumerates every **in-scope non-UC asset**,
- **classifies identities** (Entra user / UMI-SP / Databricks-managed SP / Databricks-managed group),
- surfaces migration-relevant gaps (ACLs, entitlements) the customer's existing script misses,
- writes **HTML + Excel + JSON** reports to the **source staging location** (a UC Volume path),
- with **verified pagination** and 429/5xx handling for every API used.

This is the scoping + go/no-go artifact the operator reviews before exporting. It does NOT
talk to the target workspace at all.

---

## 2. Reference material

- **Customer's `workspace_inventory_nb.ipynb`** (`~/Downloads`, extracted to
  `/tmp/wsinv_lib.py` + `/tmp/wsinv_driver.py` during design). We ADAPT it — its
  `DatabricksClient` (OAuth + pagination + 429 retry), `WorkspaceInventory.fetch_all`, and
  HTML/Excel renderers are strong. We restructure into `src/` and change scope (see §5).
- **`uc-inventory-migration`** (`/tmp/uc-inventory-migration_ref`) — house style: BaseCollector,
  Config dataclass, ArtifactWriter, report generators.

---

## 3. Deliverables (files)

**Foundation (`src/`):**
- `utils/logger.py` — structured logger → stdout + `execution_export.log`.
- `utils/retry.py` — 429/5xx exponential backoff (adapt the inventory script's `get()` retry).
- `utils/helpers.py` — `now_iso`, `safe_str`, `parse_kv_list`, `parse_csv`, `strip_fields`.
- `config/config_manager.py` — finalize `Config.from_dbutils()` (role + staging Volume path +
  toggles; workspace URL/token from notebook context). `redacted()` for `config_resolved.json`.
- `auth/token_manager.py` — `ContextTokenProvider` (this workspace's run-as SP token via SDK
  `WorkspaceClient` ambient auth, with context-token fallback — see inventory driver lines
  27–49), `ApiClient` (get/paginated/scim + retry), `build_client(config)`. **No OAuth M2M,
  no cross-workspace client.**
- `exporters/artifact_writer.py` — `ensure_output_path()` (UC Volume path), `write_json/
  read_json`, `write_bytes`, checkpoint helpers, `write_manifest()`. Handles the
  **Volume .xlsx gotcha** (§7).
- `collectors/base_collector.py` — finalize discover→enrich→validate→run + `stats()`.

**Inventory (`src/` + notebook):**
- `collectors/*.py` — implement the in-scope collectors (§5).
- `identity/classifier.py` — implement classification (§6).
- `reports/html_generator.py::render_inventory` — adapt the script's `_render_html`.
- `exporters/excel_generator.py::generate_excel` — adapt the script's `_render_excel`.
- `notebooks/01_Inventory.py` — widgets → bootstrap → build config → run collectors →
  classify → write reports. Thin.

---

## 4. Widgets for `01_Inventory` (subset of master §5)

`role` (must be `source`), `source_workspace_id`, `source_staging_location` (a UC Volume path
`/Volumes/…` to WRITE to — managed, or an external Volume over ADLS), `run_id` (blank=auto),
plus safety caps carried over from the script: `max_scim` (0=all), `max_workspace_items`
(0=all), `max_ws_api_calls` (0=unlimited), `verbose` (true/false).

**No credentials** — the notebook runs as the source workspace's run-as SP (context token).
No asset toggles at inventory time (inventory is always full-scope); toggles apply from
Export onward.

---

## 5. Asset scope for inventory (from master §6a)

**Collect (KEEP):** users, groups, service_principals, workspace_items (notebooks/files),
jobs, clusters, instance_pools, cluster_policies, sql_warehouses, dlt_pipelines,
lakeview_dashboards, genie_spaces, secret_scopes, repos, serving_endpoints, sql_alerts,
sql_queries, global_init_scripts, cluster_libraries.

**ADD (missing in the script, needed for migration):**
- object **ACLs/permissions** for notebooks, dirs, jobs, clusters, pools, policies, warehouses,
  pipelines, repos, serving endpoints (`GET /api/2.0/permissions/<type>/<id>`),
- per-identity **entitlements** (SCIM attribute),
- **secret scope ACLs** (`GET /api/2.0/secrets/acls/list`),
- **secret scope `backend_type`** (`DATABRICKS` vs `AZURE_KEYVAULT`) + Key Vault metadata
  (`keyvault_metadata`: dns_name, resource_id) — REQUIRED so the import side knows which
  create payload to build (AKV-backed scopes need `backend_azure_keyvault`, per master §10a),
- **group membership + nesting** (full expansion),
- **IP access lists** (`GET /api/2.0/ip-access-lists`),
- **workspace conf** (`GET /api/2.0/workspace-conf?keys=...`, enumerate known keys).

**Inventory-only, flag migration as manual (v1):** apps, lakebase_projects, vector_search_endpoints.

**REMOVE (UC / account / MLflow — out of scope):** uc_registered_models, uc_connections,
delta_shares, delta_recipients, delta_providers, clean_rooms, mlflow_experiments.

**Natural key per asset (for later incremental upsert — master §9):** every collected object
must carry a stable `natural_key` — the identity that survives across runs and workspaces
(server id is stripped anyway): job/policy/pool/warehouse *name*, notebook/file *path*, scope
*name*, group *displayName*, SP *applicationId*, etc. Inventory only *records* it; fingerprint
+ upsert are Export/Import (Plans 2–7). This is the one thing incremental re-runs need from Plan 1.

---

## 6. Identity classification (inventory-time, read-only)

For each identity produce a `classification` field used by later stages. **Primary signal =
`externalId`**: Entra/SCIM-provisioned identities carry an `externalId` (Entra object id);
Databricks-managed (workspace-local) ones do NOT.
- **user** → `ENTRA_USER` (account-managed; email stable). (All users are Entra in this customer.)
- **service_principal** → has `externalId` ⇒ `UMI_OR_ENTRA_SP`; else ⇒ `DB_MANAGED_SP`
  (workspace-local, new appId on target).
- **group** → has `externalId` ⇒ `ACCOUNT_GROUP`; else ⇒ `DB_MANAGED_GROUP` (workspace-local,
  recreate on target). This is exactly the customer's "groups created inside Databricks" case.
- **Determinism:** if the running SP additionally has **account-level read**, confirm via the
  set diff (`workspace − account = local`). With workspace-admin only, mark low-confidence
  cases `NEEDS_REVIEW` and surface them for operator confirmation.
- Capture `entitlements` and (for groups) `members` + nesting on every identity.

Inventory REPORTS the classification counts + writes `identity_classification.json`; it does
not act on them.

---

## 7. Pagination + platform gotchas — explicit test matrix (customer instruction)

For **every** endpoint, confirm paging behaviour and test across a page boundary:

| Endpoint | Paging | Action |
|---|---|---|
| SCIM Users/Groups/ServicePrincipals | `startIndex`/`count` | keep script's `get_scim` (count=500); test >500 |
| `jobs/list` | cursor `next_page_token` | keep; test >100 (`expand_tasks` as needed) |
| `pipelines`, `lakeview/dashboards`, `genie/spaces`, `sql/queries`, `sql/alerts` | cursor | keep `get_paginated`; test >1 page |
| `repos` | cursor + path_prefix union | keep script's `/Repos`+`/Workspace` union |
| `clusters/list`, `instance-pools/list`, `policies/clusters/list`, `sql/warehouses`, `secrets/scopes/list`, `serving-endpoints`, `all-cluster-statuses`, `global-init-scripts` | **assumed none** | **VERIFY against docs**; add paging if the API supports it; else document why not |
| `ip-access-lists`, `workspace-conf`, `permissions/*` | verify each | new endpoints — confirm shape + paging |

**Identity-classification test (also required):** on a real customer workspace, verify that
`externalId` is present on Entra-synced users/SPs/groups and absent on Databricks-managed
groups/SPs — before relying on it in `classifier.py`. Record findings in the plan.

Also carry over from the driver:
- **Auth:** SDK `WorkspaceClient` ambient auth first, context-token fallback (works on
  serverless/shared/single-user). This is the run-as SP's token for THIS (source) workspace —
  no OAuth M2M, no cross-workspace calls.
- **Excel-on-Volume gotcha:** openpyxl needs a seekable disk; writing `.xlsx` straight to a
  FUSE `/Volumes` path corrupts it. Render to local `/tmp`, then byte-copy to the staging
  Volume (no `dbutils.fs`). HTML/JSON can be written directly.
- **`_safe` wrapper:** one collector failing never aborts the run; errors captured into the
  report's warnings box; pagination-truncation raised as an explicit `INCOMPLETE —` warning.

---

## 8. Reports (adapt the script's, keep the style)

- `inventory_<host>_<ts>.html` — summary cards + per-asset detail panels + fetch-warnings box
  + **identity classification** section (new) + ACL coverage (new).
- `inventory_<host>_<ts>.xlsx` — Summary sheet + one sheet per asset + Identity/Classification
  sheet + a "Migration Plan" checklist sheet (per master §2 conventions).
- `inventory.json` — full raw data. `config_resolved.json` — redacted effective config.
- `identity_classification.json` — per-identity class (feeds the target-side import later).
- All under `output_path = <source_staging_location>/wsmig/<source_ws_id>/<run_id>/`
  (the staging location is a UC Volume path). Inventory seeds the bundle dir that
  Export (Plan 2) fills and the manifest describes.

---

## 9. Build order within Plan 1

1. `utils/` (logger, retry, helpers) → 2. `config_manager.from_dbutils` (role + locations) →
3. `auth/token_manager` (context-token client + ApiClient) → **smoke test** (list SCIM Users
+ `current_user.me()` on the source workspace) → 4. `artifact_writer` (UC Volume write,
xlsx gotcha) → 5. `base_collector` → 6. in-scope collectors + ACL/entitlement enrichment →
7. `identity/classifier` → 8. report generators → 9. wire `01_Inventory.py` → 10. run on a
real workspace, verify pagination cases in §7, iterate.

---

## 10. Definition of done

- `01_Inventory` runs end-to-end **inside a real source workspace** via a Job (run-as
  workspace-admin SP) using the context token, writing HTML+Excel+JSON to the
  `source_staging_location` (a UC Volume path — managed or ADLS-backed external volume).
- No cross-workspace calls; no OAuth M2M / secrets used.
- Every §7 endpoint's paging is verified/tested or documented; no silent truncation.
- Identity classification counts appear in the report + `identity_classification.json`; ACLs +
  entitlements + secret ACLs + IP lists + workspace conf are captured; UC/MLflow assets absent.
- Every collected object carries a stable `natural_key` (for later incremental upsert, §5).
- One collector failing does not abort; warnings surfaced. Excel opens uncorrupted.

---

## 11. Resolved decisions (from review)

1. **Identity classification signal (workspace-admin SP).** Reading all users/groups/SPs via
   workspace SCIM is fully doable as a workspace-admin SP. To distinguish **Databricks-managed
   (workspace-local)** groups/SPs from **Entra/SCIM-provisioned** ones we use **`externalId`**:
   Entra-synced identities carry an `externalId` (their Entra object id); workspace-local ones
   do NOT. This is the primary signal. If the running SP is *also* granted **account-level
   read**, classification is fully deterministic (`workspace set − account set = local`); with
   workspace-admin only, ambiguous cases are **flagged for operator confirmation** in the
   report. **Plan 1 must empirically verify `externalId` behaviour on a real customer
   workspace** before relying on it (test step in §7).
2. **Workspace-conf keys.** `workspace-conf` = the admin-console Settings toggles
   (`/api/2.0/workspace-conf`), e.g. `enableTokensConfig`, `maxTokenLifetimeDays`,
   `enableIpAccessLists`, `enableExportNotebook`, `enableResultsDownloading`,
   `enableWebTerminal`, `enableDbfsFileBrowser`, `enableUploadDataUis`,
   `storeInteractiveNotebookResultsInCustomerAccount`. We migrate a **documented default key
   set** so the target comes up with the same security/feature posture; the operator can trim
   it. (Inventory just reads + reports them; applying is target-side, later plan.)
3. **Inventory compute = serverless.** Running as a Job with run-as = the SP means SDK ambient
   auth / notebook context resolve to that SP's identity automatically. SDK-first (works on
   serverless) with context-token fallback — already the plan.
4. **Staging = always a UC Volume path (`/Volumes/…`).** If the storage is ADLS, it is
   registered as a UC **external location** and exposed as an **external Volume**, so the tool
   always writes to a FUSE-mounted `/Volumes/...` path with uniform plain-file I/O (and the
   openpyxl `/tmp`→copy trick). We **drop raw `abfss://` handling** — the widget takes a
   Volume path only. `artifact_writer` is simpler for it.
