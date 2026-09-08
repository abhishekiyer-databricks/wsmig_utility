# Workspace Migration Utility

## What this project is
A **notebook-based** Databricks workspace migration utility. It migrates **all non-UC
workspace assets** from a **source workspace** to a **target workspace**, both on Azure,
**Azure region 1 → Azure region 2 (same cloud, cross-region)**. It is designed to be run
**entirely from inside Databricks as notebooks** (no terminal, no local Python), and to
be **generic + config-driven** so the same code migrates 100+ workspace pairs for the
customer.

## Runtime model (REVISED 2026-08-04) — TWO modes, both must work (`connectivity_mode` widget)
The deployment model is not fixed, so the utility supports both. See PLAN_0_master §1a.
- **Mode A `airgap` (default)** — the two-sided model described below.
- **Mode B `direct`** — **all** stages run **inside the TARGET workspace**; `01_Inventory` +
  `02_Export` read the source over REST using a **source workspace-admin SP's client id +
  secret** (OAuth M2M; the secret comes from a target-workspace secret scope OR a widget — both
  supported, always redacted from artifacts/logs — see PLAN_3_import.md §2a). The
  bundle is written straight to the single `staging_location`, so there is **no manual hop** and the
  whole migration can run as **one end-to-end Job** (`jobs/direct_end_to_end_*.job.json`, PLAN 7 §E).
- Both modes produce/consume the **same bundle**, so import/transform/validate are mode-agnostic;
  the mode is recorded in `manifest.json` + `config_resolved.json`. Only `01`/`02` and
  `auth/token_manager.build_clients()` are mode-aware.

### Mode A detail — AIR-GAPPED, two-sided (NO source↔target connectivity)
- **There is NO network connectivity between source and target workspaces.** The pull model
  is dead. The utility runs on **two sides that never talk to each other**:
  - **SOURCE side** (`01_Inventory`, `02_Export`): runs **inside the source workspace**;
    reads source assets; **writes a bundle** to a **source staging location**.
  - **HANDOFF**: the **customer ops team physically moves** the bundle from the source
    location to a **target staging location** (download + upload), made readable by target.
  - **TARGET side** (`04_Import` — preflight + transforms run inside it): runs
    **inside the target workspace**; reads the bundle; writes target.
- The same Git folder is pulled into **both** workspaces; the role is DERIVED from stage + mode
  (PLAN 7 §C — no `role` widget) and guards mis-runs. **No live cross-workspace REST call, ever.**
- **Staging (PLAN 7 §C):** ONE widget — `staging_location` — a **UC Volume path (`/Volumes/…`)**:
  managed OR an ADLS-backed external volume (the airgap hop is "source side sets location A,
  target side sets location B", two separate runs each with their own single value; the old
  `source_/target_staging_location` remain only as an upgrade fallback). Register ADLS as a UC
  external location → external volume. Always FUSE-mounted so file I/O
  is uniform; **raw `abfss://` is not used**. Bundle is run-isolated + self-describing:
  `manifest.json` (asset list, counts, checksums, source ws id, tool version) lets the target
  verify the upload arrived complete before acting.
- **Hive metastore + UC are OUT of scope.** Assets only. (UC Volumes may be used purely as
  staging storage — not UC migration.)

## Auth model (REVISED 2026-08-04) — per-mode
- **PATs are not allowed / disabled in the customer WS** (still true in both modes).
- **`direct` mode DOES use OAuth M2M** (client-credentials) for the **source** workspace only —
  a source workspace-admin SP's `client_id` + secret, the secret read at runtime from a
  **target-workspace secret scope** via `dbutils.secrets.get`. Never a widget value, never in
  `config_resolved.json`. The earlier blanket "no OAuth M2M" applied to the air-gap-only design.
- The workspace a notebook **runs in** is always reached with the run-as SP's notebook-context
  token (SDK ambient auth), in both modes.

### Mode A detail — each side uses its own run-as SP; NO cross-workspace auth
- Each side runs as a **Databricks Job whose run-as identity is a workspace-admin SP** on
  **that** workspace. All API calls use that SP's **notebook-context token** against its own
  workspace only (SDK `WorkspaceClient` ambient auth, context-token fallback).
- A workspace **never authenticates to the other workspace** — the file bundle is the only
  thing that crosses. So the "same Databricks account?" question is irrelevant to the build.

## Config / driver (decided; trimmed in PLAN 7 §C) — widget-based, no credentials in widgets
- Common widgets: `connectivity_mode` (airgap|direct, **default direct**), `run_id`,
  `source_workspace_id`, and ONE `staging_location`. **No `role` widget** — role is DERIVED from
  the stage (inventory/export/import) + mode (`role_for_stage`); **no `verbose` widget** either.
- Source side (01/02): `max_scim/max_workspace_items/max_ws_api_calls`, `content_fetch_workers`,
  `force_full_*`, and the 11 `migrate_*` per-asset toggles (bundle scope). Target side (04):
  `dry_run`, `account_id`, transform options, `import_assets` (which families to import this
  session), `state_catalog`/`state_schema` (one shared catalog+schema across all pairs, assumed to
  exist; table names tool-owned), `preflight_enforce`, `library_force_start_clusters`,
  `pause_job_schedules`, `allow_deletes`, `retry_mode` (off|failed_only|skipped_only|failed_and_skipped).
  (`skip_manifest_verify` and the `migrate_*` toggles were REMOVED from the import notebook — the
  toggles are source-side bundle scope, and `import_assets` is the target-side selector.)
- `direct` mode only: `source_workspace_url`, `source_sp_client_id`, and the secret **either** as
  `source_sp_secret_scope`+`source_sp_secret_key` (pointer, preferred when both set) **or**
  `spn_secret_value` (widget fallback). Always redacted from artifacts + logs.
- Per-asset toggles (all default TRUE; flip to FALSE to skip), set on both sides. Same values
  usable as Job params. **No credentials in any widget** (run-as SP context token).
- Asset scope decisions: INCLUDE legacy SQL queries/alerts,
  workspace conf, Excel output, global init scripts, cluster libraries.
  **Legacy SQL dashboards: inventoried + exported, NOT imported** (create endpoint deprecated/absent
  on modern workspaces, verified live) → `manual` + rebuild note; underlying queries still migrate.
  **Git repos: OUT OF SCOPE for import** (customer 2026-08-05) — inventoried + exported as metadata
  only (url/provider/branch/path; zero file bytes, the collector never descends into a git folder),
  which is the manual recreate runbook. Never created on target. EXCLUDE PATs/tokens
  (disabled) and **IP access lists** (account-level in this customer — configured in the
  account console / account API; a workspace-scoped tool can't see or migrate them → customer/
  account-admin manual task). REMOVE UC assets (registered models, connections, delta sharing, clean rooms) +
  MLflow. **Agent Bricks agents EXCLUDED** (Multi-Agent Supervisor / Knowledge Assistant /
  Custom LLM / Information Extraction): no workspace REST recreate path — a deployed agent is
  backed by a UC-registered MLflow ResponsesAgent model + UC assets + UI-only orchestration
  (all out of scope); IE agents are a UC Beta `agent-services` object. Model serving endpoints
  (non-agent) still INVENTORIED but **DOWNGRADED to migration-manual/conditional**: an endpoint
  only points at a model — if it serves a UC-registered model (out of scope) it can't be
  auto-recreated on target; only external-model endpoints are auto-migratable. Collector flags
  `migratable` + `migration_note` per endpoint. Apps / Lakebase / Vector Search = inventory-only,
  migration flagged manual for v1. **Genie spaces**: AUTO-MIGRATABLE (verified live fvm1
  2026-08-01). The current Genie API `GET /api/2.0/genie/spaces/{id}?include_serialized_space=true`
  returns the full `serialized_space` JSON, recreatable via `create_space`/`update_space`
  (approach adopted from the `client_shared_utils/workspace_asset_migration` reference). Export
  captures `serialized_space`+title+description+warehouse_id; import remaps `warehouse_id`.
  Caveat: `serialized_space` references UC tables by FQN → those must pre-exist on target (UC out
  of scope). (Supersedes the old "separate Genie repo / un-exportable protobuf" plan.)

## Account-level preflight (decided) — VERIFY only, run once before workspace #1
- Migration is done **one workspace at a time**, but there may be **one-time account-level
  prerequisites** (only relevant if region-2 is a SEPARATE Databricks account): Entra→SCIM
  provisioning, account groups, UMI/Entra SPs must exist in the target account first.
- The preflight gate **verifies** (does NOT perform) these: reads the exported bundle's identity
  classification, lists the account identities referenced, and reports which are
  present/absent/assigned in the target account → **go/no-go gate**. Actual Entra/SCIM setup stays a
  **customer IT one-time task** (needs Entra admin). PLAN 7 §B1 removed the standalone
  `00_Account_Preflight` notebook — the gate now runs INSIDE `04_Import` (`src/importers/preflight.py`).
- Account model (same vs new account) is **still unknown**; preflight detects & handles both.

## Why it exists (context that isn't in the code)
- A prior tool by a senior engineer already does workspace migration:
  https://github.com/vivekravichandiran/WorkspaceMigration  (Azure → GCP, cross-cloud).
  We are using it as a **reference/base**, not a dependency.
- Two hard constraints make that tool unusable for this customer:
  1. **No terminal Python.** The customer's workspaces sit behind **front-end private
     connectivity** and are only reachable from inside a **VDI**, where running Python
     on a terminal is not permitted. Everything must run as Databricks notebooks.
  2. **Feature gap — Databricks-managed groups.** The customer has groups created
     *inside* Databricks (Databricks-managed / workspace-local), **not** managed by
     Entra ID / SCIM provisioning. These must be enumerated and recreated with
     membership + entitlements intact. The reference tool delegates identity to the
     `databrickslabs/migrate` terminal tool, which we cannot use here.

## Key differences from the reference tool
| Aspect | Reference tool | This utility |
|---|---|---|
| Runtime | Terminal Python package + bash | Databricks **notebooks** only |
| Cloud path | Azure → GCP (cross-cloud) | Azure region1 → Azure region2 (**same cloud, cross-region**) |
| Cluster/node transforms | Heavy (GCP node mapping, availability, spark conf rewrite) | **Not needed** — same cloud/region; keep transforms minimal |
| Identity engine | `databrickslabs/migrate` (external) | **Own REST/SDK implementation** (needed for DB-managed groups) |
| Config | `gcp_import_config.json` (GCP-specific) | Generic per-pair config; batch-driven for 100+ pairs |
| Scope | UC + non-UC | **Non-UC assets only** |

## Reference repos
- **WorkspaceMigration** (senior's, Azure→GCP, terminal): https://github.com/vivekravichandiran/WorkspaceMigration
  — reuse the REST export/import PATTERNS (`workspace_export/exporters/simple.py`,
  `workspace_import/extra_importers.py`, `sp_migrator.py`) and config concepts. Drop the
  GCP/bash/`databrickslabs-migrate` parts.
- **uc-inventory-migration** (senior's, notebook-based UC tool — our HOUSE STYLE to match):
  https://github.com/vivekravichandiran/uc-inventory-migration — adopt its structure and
  conventions so this utility is consistent with the customer's other tool. Cloned locally
  at `/tmp/uc-inventory-migration_ref` during design.

## Repo layout & storage (decided — mirror the UC tool; notebooks slimmed in PLAN 7)
Thin notebooks + importable `src/` package. Same deploy pattern the customer already knows.
Deployed twice — same Git folder pulled into BOTH workspaces; the role is DERIVED from
`connectivity_mode` + which stage (PLAN 7 §C — no `role` widget any more).
```
notebooks/   01_Inventory, 02_Export, 04_Import   (thin; widgets)
             00_Install_Jobs (idempotent Jobs-API installer; deploys jobs/*.job.json)
jobs/        *.job.json  (checked-in Jobs API 2.2 definitions; installed by 00_Install_Jobs)
src/         config/  auth/(context-token client for THIS ws)  collectors/(read this ws)
             importers/(write this ws)  identity/  state/(target Delta upsert state)
             transform/  reports/  exporters/(incl. bundle_paths.py — the layout registry)  utils/
requirements.txt
```
Config is **widget-based** — no config files; the same widget values double as job params.
PLAN 7 removed the standalone orchestrator/validate/preflight notebooks (`00_Main_*`,
`05_Validate`, `00_Account_Preflight`): preflight runs INSIDE `04_Import`, and stitching lives in
the packaged multi-task Jobs (`jobs/`, installed by `00_Install_Jobs`).

### Staging bundle layout (PLAN 7 §D — via `src/exporters/bundle_paths.py`)
Under `<staging_location>/wsmig/<source_workspace_id>/<run_id>/`: `export/` (the exported bundle,
the only thing the air-gap moves), `reports/` (human-facing xlsx + the import runbook), `misc/`
(machine/bookkeeping JSON — inventory.json, export_index.json, config_resolved.json, manifest.json,
checkpoint.json, import_results.json, preflight_report.json — + the execution logs). Every read/write
goes through the `bundle_paths` registry so the layout lives in ONE place. The pair-level pointers
`LATEST_INVENTORY.json` / `LATEST_EXPORT.json` sit ABOVE the run dir at the wsmig root.

Planning docs live in `plans/`: `plans/PLAN_0_master.md` is the MASTER plan;
`plans/PLAN_<n>_*.md` are the per-feature sub-plans (each is the review gate before that
feature's code). Plan 1 = setup + inventory (source side).

Conventions carried over from the UC tool:
- **`Config` dataclass** built via `from_dbutils(..., stage=)`/`from_dict()`; holds the derived
  `role`, the single `staging_location`, per-asset toggles, transform options. Auth = this
  workspace's run-as SP context token (no creds in config).
- **Bootstrap**: the repo is a Git folder in each workspace; each notebook prepends the repo
  root to `sys.path` — no zip/init-script. Optionally wrap the source-side notebooks and the
  target-side notebooks as two multi-task Databricks **Jobs** (run-as workspace-admin SP).
- **BaseCollector-style** abstract class (discover→enrich→validate→run, per-collector stats,
  failures never stop the pipeline). Mirror it with a `BaseImporter`.
- **Output**: run-isolated dir per run, `execution.log`, HTML + Excel + JSON artifacts, a
  "Migration Plan"-style checklist. Reuse their reports/exporters style.
- **requirements**: `requests`, `openpyxl`, `databricks-sdk` (drop `networkx` unless we build a
  dependency graph). Installed via `%pip`/bootstrap.

## Reference tool — what to reuse vs drop
Cloned locally at `/tmp/WorkspaceMigration_ref` during design (re-clone from the URL above).
- **Reuse the patterns** in `workspace_export/exporters/simple.py` and
  `workspace_import/extra_importers.py`: list → strip runtime fields → save JSON;
  import = load → skip-if-exists → POST. These cover SQL warehouses, DLT pipelines,
  repos, Lakeview/AI-BI dashboards, Genie spaces, model serving endpoints.
- **Reuse the config concepts**: `workspace_excludes` (regex path/job/cluster filters),
  `user_id_mapping`, `user_domain_mapping`, PAUSE-schedules-on-import, skip/force-recreate jobs.
- **Drop**: all GCP cluster rewriting, node_type_mapping.csv, Azure→GCP availability
  mapping, bash orchestrators, the `databrickslabs/migrate` dependency and its stubs.
- **Genie spaces (UPDATED — now auto-migratable, verified live 2026-08-01)**: the OLD caveat
  ("`serialized_space` is an un-exportable protobuf") is **obsolete** — the current Genie API
  `GET /api/2.0/genie/spaces/{id}?include_serialized_space=true` returns the full serialized_space
  JSON, recreatable via `create_space`/`update_space`. Export captures it; import remaps
  warehouse_id. Only caveat left: the serialized_space references UC tables by FQN (must pre-exist
  on target; UC out of scope).
- **Secret values caveat (still applies)**: secret scope *values* are never exported by
  the API — only scope names + ACLs migrate; values must be re-populated on target.

## Notebooks (PLAN 7 §B1 slimmed the set to four + the job installer)
`airgap` mode: `01_Inventory`/`02_Export` run in the SOURCE; the rest run in the TARGET.
`direct` mode (default): ALL of them run in the TARGET (01/02 read the source over REST).
Role is DERIVED from stage + mode (no `role` widget).
- `01_Inventory` — read-only enumeration + identity classification → report (Plan 1)
- `02_Export` — dump enabled assets → staging **bundle** (JSON + notebook SOURCE/DBC) + manifest/checksums. Checkpointed.
- `04_Import` — create on target in dependency order; idempotent + checkpointed + dry-run. **Preflight AND the transforms (mappings/excludes/pause) run INSIDE it.**
- `00_Install_Jobs` — idempotent Jobs-API installer: fills every config value ONCE and deploys the selected `jobs/*.job.json` (PLAN 7 §E).
DELETED in PLAN 7 §B1: `00_Account_Preflight` (preflight is inside `04_Import`), `05_Validate` and
`03_Transform_Review` (both deferred to Plan 4 — manifest verify + transforms already run inside
`04_Import`, so the stub had no unique job left), `00_Main_Source`/`00_Main_Target`/`00_Main_EndToEnd`
(stitching moved into the packaged multi-task Jobs). Reusable logic lives in the importable `src/`
package; notebooks stay thin. Plan 4 will add the cross-stage review/reconciliation report as a new notebook when that work is built.

### Packaged jobs (PLAN 7 §E — `jobs/*.job.json`, non-DAB, Git-folder friendly)
Checked-in Jobs API 2.2 definitions, installed by `00_Install_Jobs` (which projects the ONE-time
config into each job's `base_parameters`, prefers the secret-scope pointer, and creates-or-resets by
name). Shipped: `direct_end_to_end_dry_run` + `direct_end_to_end_live` (01→02→04, differing only in
the import task's baked `dry_run`), single-task `inventory`/`export`/`import`, and `airgap_source`
(01→02 for the source side). The installer's `deploy_jobs` multiselect picks which to create.

### Asset dependency order (non-UC; Hive metastore + UC OUT of scope)
Identity (users → SPs → groups incl. nested + entitlements) → Compute (pools → policies →
clusters) → Workspace content (dirs → notebooks → files → repos → ACLs) → Secrets (scopes +
ACLs; values manual) → Jobs → SQL (warehouses, queries, alerts, legacy dashboards) → DLT →
AI/BI dashboards → Genie (manual) → model serving → misc (global init scripts, cluster
libraries, workspace conf). PATs + IP access lists (account-level) excluded.

## Identity model (decided — the core of this utility)

Split across the air-gap: **classification is done SOURCE-side** (during inventory/export,
written into the bundle as `identity_classification.json`); **reconciliation + creation are
TARGET-side** (during import, reading the bundle). Read the roster from the **source
WORKSPACE SCIM** (`/api/2.0/preview/scim/v2/...`), NOT account level — we reproduce exactly
the identities assigned to that workspace. **Entitlements are workspace-scoped**: captured
per identity on source, applied on target (`allow-cluster-create`, `databricks-sql-access`,
`workspace-access`, `allow-instance-pool-create`). Classify each identity and act accordingly:

| Type | Scope | New ID on target? | Action | ACL remap? |
|---|---|---|---|---|
| Entra/SCIM users | Account | No (email stable) | Ensure assigned to target WS + set entitlements | No |
| Azure UMI / Entra SPs | Account | No (`applicationId` stable) | Add to target WS by same applicationId + entitlements | No |
| Databricks-managed SPs | Workspace-local | **Yes** (new applicationId) | Recreate; build `old→new` map | **Yes** |
| Databricks-managed groups | Workspace-local | Yes (new id) | Recreate: members (users/SPs/nested groups) + entitlements + roles, nested-first | reference remap |

**Detection-driven, so we DON'T need the "same account?" answer to build:** at runtime the
utility lists what already exists on the target and only creates/assigns what's missing.
- Same account → Entra users/UMI-SPs/account-groups already exist at account level; if SCIM
  already assigns them to target WS we skip create+assign and only set entitlements + ACLs.
- Different account, or not yet assigned → account identities must be provisioned/assigned
  first. If the running SP has **account-admin**, the utility can assign them
  (PermissionAssignments API); if only **workspace-admin**, it **detects the gap and reports
  it** as a prerequisite for customer IT/SCIM rather than failing silently.
- Credential baseline = **workspace-admin**; account-admin is optional and unlocks
  auto-assignment of account identities. Utility degrades gracefully + reports either way.

Persist a per-pair `identity_map.json`: `sp_mapping` (old→new appId), `group_map`,
`user_map` (mostly identity), and a `manual_actions` list for anything requiring
account-admin / customer IT.

## Incremental / repeatable runs (decided — the utility runs the SAME workspace many times)
- Re-runs must carry over source changes over time (new jobs/pools, **edited policies**, etc.).
  Skip-if-exists alone would silently drop UPDATES → wrong. So every asset is **UPSERTed**.
- **TARGET-side Delta state store** (`src/state/state_store.py`), keyed by
  `(source_ws_id, asset_type, natural_key)`, storing **BOTH source and target object ids** +
  content **fingerprint**. Decision per asset: create / update (asset's edit API against the
  stored **target** id) / skip (fingerprint unchanged) / report deleted-in-source (never
  auto-delete by default). Storing both ids lets a re-run edit the right target object (e.g.
  source policy "p1" → target id 9; a later source edit updates target id 9, not a duplicate).
  Keeps the air-gap: source never needs target state.
- Export emits a stable **natural_key** + fingerprint per asset into the bundle. **Identity map
  (old→new SP/group ids) is persisted in the state store** so re-runs reuse, not duplicate.
- Every re-run emits a **change report** (created/updated/unchanged/deleted-in-source).
- Inventory (Plan 1) is read-only: its only obligation is recording a stable `natural_key`
  per asset. State/fingerprint/update-APIs are Export (Plan 2) + Import (Plans 3–7).

## Conventions / decisions
- Notebooks must be runnable with **no terminal**; any pip installs use `%pip`.
- Everything **idempotent** (skip-if-exists) and **checkpointed** so a re-run resumes; plus
  **cross-run UPSERT** via the Delta state store (see above).
- **Dry-run** supported on every mutating step.
- Auth = the run-as workspace-admin SP's **notebook-context token** for the workspace the notebook
  runs in. Never hard-code credentials; no PATs. **`airgap` mode additionally never calls the other
  workspace; `direct` mode DOES call the source, via OAuth M2M** (see the Auth model section).
- Generic first: no customer- or workspace-specific values in code; all in widgets/config.

## Status (2026-08-06)
- **Plans 1, 2 AND 3 are IMPLEMENTED and live-tested**. The whole pipeline runs:
  inventory → export → preflight → import (all 12 phases) → reports.

### PLAN 7 (cleanup + packaging) IMPLEMENTED 2026-08-09
- **A** behavioural: dry-run report is `reports/import_status_dry_run.xlsx` (never clobbers the live
  one); the orphan-SP-home message distinguishes "in source roster but not migrated this run" from
  "deleted in source" via `identity_classification.json`.
- **B** slimming: deleted 5 notebooks (`00_Main_*`, `05_Validate`, `00_Account_Preflight`); removed
  `import_results.html`, `preflight_report.html`, standalone `acl_parity_report.{json,html}` and
  gated `inventory.html` off (generator kept behind `WRITE_INVENTORY_HTML`). ACL parity is now the
  **"ACL Parity" sheet** of `import_status.xlsx` (fed via the runner's shared context).
- **C** widgets: one `staging_location` (old two kept only as an upgrade fallback in `from_dbutils`);
  `role` DERIVED from stage+mode; `verbose`/`skip_manifest_verify`/`migrate_*` dropped from import.
  `connectivity_mode` defaults to `direct`.
- **D** layout: `src/exporters/bundle_paths.py` is the single path registry; the bundle is now
  `export/` + `reports/` + `misc/`, with `manifest.json`/`checkpoint.json`/all bookkeeping in `misc/`.
- **E** jobs: `jobs/*.job.json` (Jobs API 2.2) + `notebooks/00_Install_Jobs.py` +
  `src/utils/job_templates.py`; installer projects config into each job and creates-or-resets by name.

### Review-driven fixes 2026-08-08 (9 items, all live-verified on target_ws_3; 280 offline tests)
Found by the first customer-style run (source_ws → target_ws_3, direct mode). Each has a regression test.
1. **INV-1 jobs `run_as`** — `jobs/list` OMITS run-as; only `jobs/get` returns it, top-level as
   `run_as_user_name` (email=user, bare UUID=SP). Collector now enriches per-id and normalises into
   a typed `settings.run_as` dict so the report shows it AND the importer remaps a db-managed SP's
   appId (`jobs_collector._normalise_run_as`).
2. **INV-2 "Deployed by DAB" column** — shown only for DAB-capable asset types
   (`dab_registry.DAB_CAPABLE_ASSET_TYPES`, sourced from the CLI bundle schema); dropped from
   instance pools + legacy dashboards, per-row "NA" on the mixed SQL-alerts tab (legacy=NA, V2=real).
3. **IMP-1 import Excel** — now one sheet PER ASSET TYPE (like inventory/export), not per family
   (`import_report._ASSET_TYPE_TO_CARD`).
4. **IMP-2 error surfacing** — `classify_error` ALWAYS carries the actual server message verbatim;
   the `_ERROR_MAP` entry only appends a remediation HINT (was replacing it — a UC-table genie
   failure showed as the false "needs workspace-admin"). Raw error added to report rows/columns.
5. **IMP-3 `library_force_start_clusters`** — implemented start→poll-RUNNING→install→stop; only
   stops clusters IT started (never one already running). Verified live (gson installed, cluster
   left TERMINATED).
6. **IMP-4 AKV secret scope — DETERMINISTICALLY MANUAL, never attempted** (investigated live
   2026-08-08). AKV-backed scope creation needs an **Azure AD** token for app 2ff814a6… — and NO
   credential available in this customer's setup can produce one: a Databricks SPN secret mints a
   *Databricks* token (`iss=<ws>/oidc`, wrong issuer), and the SPN is backed by an Azure **managed
   identity** that refuses secret auth (`AADSTS7000232`) and can only use Azure **IMDS**
   (169.254.169.254) — unreachable from a front-end-private / VDI / notebook-only workspace. All
   three facts proven live. So there is NO token-minting code (removed `mint_aad_token`): AKV-backed
   scopes are marked `manual` at EXPORT (naming the vault) and never attempted at import;
   Databricks-backed scopes migrate normally.
7. **IMP-5 legacy SQL alerts** — marked `manual` at EXPORT (v1 create API obsolete), no longer
   attempted at import (like legacy dashboards).
8. **IMP-6 SP home dirs** — a recreated SP's `/Users/<oldAppId>/…` content is remapped to
   `/Users/<newAppId>/…` via `sp_mapping`; the home ROOT is skipped (auto-provisioned at SP create,
   verified live); an unmigrated-SP home is a clear prerequisite, not an opaque failure.
9. **IMP-7a/7b identity** — (a) Databricks-generated groups (`*-clone-*UTC` / "(created by
   Databricks)") detected by PATTERN (never the literal `users`) and skipped (`skip_generated`
   action); (b) PASS-1 created users/SPs now indexed by displayName so group members resolve in
   PASS-2 (fixes the `1/3 members added` under-population). `users`/`admins` membership reaches parity.
- **Direct-mode source credential**: the old source SP secret rotated; a new workspace-admin SP
  `wsmig_e2e_source_reader` (account REDACTED-ACCT…) is assigned to source_ws with an OAuth secret at
  `/tmp/wsmig_source_sp_secret.txt` (client_id line 1, secret line 2) for live runs.
- **Fixtures are now workspace-agnostic** and rebuilt on a fresh source workspace
  (`source_ws` profile, a DIFFERENT account from the old fvm1). `tests/fixtures_fvm1.py` resolves
  profile / catalog / identity / Azure tenant / CLI at runtime (`WSMIG_PROFILE`,
  `WSMIG_ACCT_PROFILE`, `WSMIG_CATALOG`, `WSMIG_CLI`) instead of hardcoding fvm1, so the same
  file populates any pair. 19 dependency-ordered phases, every one **idempotent** (several
  creates — notably `service_principals.create` — do NOT dedupe by name and silently doubled
  fixtures on re-run). Latest coverage report: **88/88 PASS, 176 export units, 0 failures**.
  Entra-backed groups need the ACCOUNT profile (workspace SCIM drops `externalId`); a new
  `acls` phase grants each object type its FULL permission ladder across every principal kind
  (239 grants / 15 object types / 59 principal-kind×level pairs, vs the old CAN_MANAGE-skewed set).
- **Plan 3 (import) is complete**: `src/state/state_store.py` + `sql_backend.py`, all 12 importers
  in `src/importers/` (identity, compute, workspace, secrets, jobs, sql, dlt, dashboards, genie,
  serving, misc, acls) + `base_importer` / `phases` / `import_runner` / `preflight`,
  `src/reports/import_report.py`, and the notebook `04_Import` (preflight runs inside it).
- **Test suite**: 243 offline tests (`python3 -m pytest`), plus live harnesses:
  `live_direct_mode.py` (OAuth M2M, 13/13), `live_state_store.py` (real Delta MERGE, 24/24),
  `live_e2e_migration.py` (the full migration + idempotency + update + adopt + retry + ACL parity).
  `pytest.ini` keeps the offline suite safe to run anywhere; live harnesses are invoked explicitly.
- **pip needs the Databricks proxy**: `pip3 install --index-url https://pypi-proxy.cloud.databricks.com/simple <pkg>`.
- **Bugs found and fixed by the live E2E run** (each has a regression test):
  1. `checkpoint.json` was checksummed in `manifest.json`, but IMPORT writes it — so the first
     import invalidated the bundle and every later run was refused. Now excluded, along with the
     import-side outputs (`artifact_writer._excluded_from_manifest`).
  2. API errors carried no server explanation ("400 Client Error: Bad Request for url: …"), making
     every failure unactionable AND breaking `RESOURCE_ALREADY_EXISTS` detection. `ApiClient` now
     folds `error_code` + `message` into the raised error (`HTTPStatusError`).
  3. A >10 MB workspace FILE exports fine (500 MB ceiling) but `workspace/import` caps its base64
     body at 10 MB. Large files now use the streaming `workspace-files/import-file` route
     (verified with 120 MB).
  4. Platform-internal dirs (`.db_internal`) were being mkdir'd and 400ing — now skipped.
  5. Group membership was patched during each group's own create, which silently under-populated any
     group whose members came later in the bundle. Now a true second pass after all groups exist.
  6. Secret-scope ACLs were being sent to `PUT permissions/...` (which 404s) instead of
     `secrets/acls/put`, and nothing populated the MANAGE principal — the secrets importer now reads
     it from `export/acls.json` (it is needed at create time; the ACL phase runs last).
- **Bug found by the 2026-08-06 fixture rebuild** (regression test:
  `test_dab_alert_v2_is_stamped_from_bundle_state_not_path`): a DAB-deployed **Alerts V2** was
  classified Manual → `import_action=create`, so import would DUPLICATE an alert the customer's
  bundle redeploys. Cause: `sql_collector` detects DAB by path, but the LIST call
  `GET /api/2.0/alerts` **omits `parent_path`** entirely (only GET-by-id returns it), so the check
  could never fire. Fixed by stamping alerts from the bundle state file
  (`InventoryRunner._DAB_STAMP_TARGETS` + `_SQL_TYPE_FOR`, which also keeps the mixed `sql` bucket
  from letting a warehouse inherit an alert's claim on the same id).
- **Deferred to Plan 4**: the cross-stage inventoried→exported→imported reconciliation + a
  review/sign-off diff report. `misc/import_results.json` is already written in the shape Plan 4
  joins on. (Both stub notebooks that used to hold this — `05_Validate` and `03_Transform_Review` —
  were deleted in PLAN 7 §B1 since manifest-verify + transforms already run inside `04_Import`; Plan
  4 adds a fresh notebook for the reconciliation/diff when that work is actually built.)

### UC-Volume write durability at customer scale (the customer, 2026-09-03) — see PLAN_11 Finding-6
First real customer-scale run (the customer) produced an EMPTY bundle: human reports (xlsx/md)
present, but `export/`+`misc/` empty, `manifest files=0`, `[Errno 11]` (EAGAIN) on the Volume.
Reproduced at BOTH 663 MB/1.1M-asset AND 76 MB/17K-asset → **root cause is the WRITE METHOD, not
size.** All machine files are written via raw `open()` straight to the FUSE `/Volumes` path
(`artifact_writer.write_json`/`write_bytes`); FUSE buffers and flushes to backing ADLS async, and
under the customer's networking restrictions that flush fails/stalls → nothing lands. Outputs split
exactly on write path: `write_text_local_then_copy` (render /tmp → byte-copy: the xlsx reports)
SURVIVE on the customer's own mount; direct-FUSE writes do NOT. Databricks Support endorsed the local-first
pattern (write to local disk, then copy into the volume).
- **THE fix (PLAN_11 Finding-6, OPTIONAL/decide-later):** route EVERY Volume write through the
  local-first path that already works (render /tmp → `dbutils.fs.cp`/Files API, synchronous commit) +
  post-write size verification; and FAIL LOUD when inventory.json is absent/short instead of the
  silent `read_json() or {}` (and stop export from silently re-running the whole inventory).
- **Environmental quick fix:** the customer runs clusters behind an http_proxy; the SAME
  `no_proxy = *.azuredatabricks.net,*.databricks.azure.com,169.254.169.254,127.0.0.1` already fixed
  this customer's UC-Volume writes in the shipped data_processing_framework SFTP job → set it as
  cluster **Environment variables** (NOT Spark config). Add `.dfs.core.windows.net,.blob.core.windows.net`
  ONLY if the staging volume is EXTERNAL (managed needs nothing extra — `DESCRIBE VOLUME` to check).
- **Streaming/sharding is NOT on the roadmap.** It would only matter for a 600 MB+/1M-asset workspace
  on a small (16 GB) cluster, and that memory dimension is **solved by a larger driver** (64–128 GB)
  since migrations are infrequent. Kept as a documented last-resort optimization, not planned work.
  (Note: the ~11h runtime at 1.1M assets was the 852K SERIAL ACL calls — an API-latency problem needing
  parallel enrichment, NOT solved by RAM and unrelated to streaming; separate future optimization.)
