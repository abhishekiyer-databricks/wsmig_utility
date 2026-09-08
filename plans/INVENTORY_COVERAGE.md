# Inventory coverage — reference script vs this utility

Compares the asset/entity coverage of the **customer's existing inventory script**
(`workspace_inventory_nb.ipynb` → `wsinv_lib.py`, the one whose HTML/Excel we now match)
against **this migration utility's `01_Inventory`**.

The reference script's `fetch_all` pulls **29 asset types**. This utility deliberately
**drops UC / MLflow** (out of migration scope — see CLAUDE.md / PLAN_0 §6a) and **adds**
several migration-critical entities the reference script misses (ACLs, entitlements, etc.).

---

## 1. Reference script covers → this utility covers (in-scope, KEPT)

Every one of these is collected here and appears as a card/sheet in the HTML + Excel.

| # | Reference asset | Covered here? | Our collector | Notes |
|---|---|---|---|---|
| 1 | users | ✅ | IdentityCollector | + classification + entitlements |
| 2 | groups | ✅ | IdentityCollector | + membership/nesting + entitlements |
| 3 | service_principals | ✅ | IdentityCollector | + classification |
| 4 | workspace_items (→ notebooks / workspace_files) | ✅ | WorkspaceCollector | split into Notebooks + Workspace Files, same as reference |
| 5 | jobs | ✅ | JobsCollector | API 2.1 `expand_tasks` |
| 6 | clusters (All-Purpose) | ✅ | ComputeCollector | ephemeral flagged |
| 7 | instance_pools | ✅ | ComputeCollector | |
| 8 | cluster_policies | ✅ | ComputeCollector | |
| 9 | sql_warehouses | ✅ | SqlCollector | |
| 10 | sql_queries | ✅ | SqlCollector | legacy |
| 11 | sql_alerts | ✅ | SqlCollector | legacy |
| 12 | dlt_pipelines | ✅ | DltCollector | |
| 13 | lakeview_dashboards (AI/BI) | ✅ | DashboardsCollector | |
| 14 | genie_spaces | ✅ | GenieCollector | migration flagged manual |
| 15 | secret_scopes | ✅ | SecretsCollector | + backend_type + ACLs + key names |
| 16 | repos | ✅ | WorkspaceCollector | `/Repos` + `/Workspace` union |
| 17 | serving_endpoints | ✅ | ServingCollector | model serving only; **Agent Bricks agents excluded** (see §2) |
| 18 | global_init_scripts | ✅ | MiscCollector | |
| 19 | cluster_libraries | ✅ | MiscCollector | flattened per (cluster, library) |

**19 of the reference's asset types are fully covered.**

---

## 2. Reference script covers → REMOVED here (UC / MLflow, out of scope)

Intentionally not collected — this utility migrates **non-UC workspace assets only**.
(You confirmed the UC cards should be omitted, not shown-as-zero.)

| # | Reference asset | Why dropped |
|---|---|---|
| 1 | uc_registered_models | Unity Catalog — out of scope |
| 2 | uc_connections (Lakeflow Connect) | Unity Catalog — out of scope |
| 3 | delta_shares | Delta Sharing (UC) — out of scope |
| 4 | delta_recipients | Delta Sharing (UC) — out of scope |
| 5 | delta_providers | Delta Sharing (UC) — out of scope |
| 6 | clean_rooms | UC / account-level — out of scope |
| 7 | mlflow_experiments | MLflow — separate tooling, never migrated |
| 8 | **Agent Bricks agents** (Multi-Agent Supervisor, Knowledge Assistant, Custom LLM, Information Extraction) | **NOT recreatable via workspace REST** — no create/export API for any Agent Bricks type. A deployed agent (serving endpoint `task=agent/*`) is backed by a UC-registered MLflow ResponsesAgent model + UC volumes/tables/functions/indexes + UI-only orchestration metadata (all UC/UI, out of scope). Information Extraction agents are a UC `agent-services` object (Beta, registry-only). The reference script's `task=agent/*` "agent_endpoints" card was **removed**: since import can't stand one up on target, we don't inventory them. |

---

## 3. Reference script covers → INVENTORY-ONLY here (flagged manual for v1)

The reference lists these as cards; our design keeps them **inventory-only, migration
flagged manual** (PLAN_0 §6a). **Now wired** as collectors + cards (each shows
`Auto-Migratable = No` so the result report can reconcile them as manual items).

| # | Reference asset | Status |
|---|---|---|
| 1 | apps (Databricks Apps) | ✅ collected (AppsCollector), inventory-only card |
| 2 | lakebase_projects (managed Postgres) | ✅ collected (LakebaseCollector), inventory-only card |
| 3 | vector_search_endpoints | ❌ **DROPPED** (out of scope) — see below |

**Vector search dropped (decided 2026-07-30):** the *endpoint* is workspace-level, but the
thing that holds data — the **vector search index** — is a Unity Catalog object
(`catalog.schema.index`). Since UC is out of scope, only an un-migratable endpoint shell
would remain, so we treat vector search as out-of-scope, consistent with the other UC drops.

---

## 4. This utility covers → MISSING in the reference script (our additions)

Migration-critical entities the reference inventory script does **not** capture. **All are
now VISIBLE, countable inventory items** (their own card and/or column) — not buried in
JSON — because the inventory is the reconciliation baseline for the final result report
(every migratable unit needs a "migrated: yes/no" line beside it).

| # | Added entity | Where it shows | Why it matters for migration |
|---|---|---|---|
| 1 | **Object ACLs / permissions** (notebooks, dirs, jobs, clusters, pools, policies, warehouses, pipelines, repos, serving) | own **"Object Permissions (ACLs)"** card (one row per object×principal×permission) **+ an "ACL Grants" count column** on each object | the migratable UNIT for permissions; each grant reconciled on target |
| 2 | **Per-identity entitlements** | **Entitlements column** on Users, Service Principals, Groups | workspace-scoped grants must be reproduced |
| 3 | **Secret scope ACLs** | ACL Grants column on Secret Scopes + rows in the ACL card | reference gets scope names only |
| 4 | **Secret scope `backend_type` + Key Vault metadata** | Backend / Key Vault / Secret Keys columns | AKV-backed scopes need a different create payload |
| 5 | **Group membership + nesting** | Members / Nested Groups columns | needed by the identity engine (nested-first recreate) |
| 6 | ~~IP access lists~~ | — | **DROPPED (2026-07-30):** account-level in this customer (account console / account API); a workspace-scoped tool can't see/migrate them → customer/account-admin manual task |
| 7 | **Workspace conf** (admin settings toggles) | own card | INCLUDED per decision; reference omits |
| 8 | **Legacy SQL dashboards** | own card | reference has queries+alerts but not legacy dashboards |
| 9 | **Identity classification / "Managed By"** (Entra vs Databricks-managed) | **"Managed By" column** on Users/SPs/Groups + classification summary section | the core enhancement; drives create-vs-assign on target |

### Migration-critical metadata surfaced per item (customer instruction)

Each inventory row now carries the metadata that decides how it migrates:
- **Groups / SPs** → *Managed By* (Entra/Account vs Databricks-managed vs Built-in), entitlements, member count, nesting.
- **Clusters** → *Ephemeral* (job/DLT/model clusters excluded from migration), *Pinned*, ACL grants.
- **Jobs** → *Format* (MULTI_TASK), *Run As*, *Owner ACL* present, ACL grants.
- **Secret scopes** → *Backend* (Databricks vs Azure Key Vault), secret-key count, *Values Migrate* (always No — manual), ACL grants.
- **Genie / Apps / Lakebase** → *Auto-Migratable* flag (No → manual in the result report).

---

## 5. Known gaps in THIS utility right now

- **vector_search_endpoints** — intentionally dropped (UC-coupled; see §3).
- Everything else in scope is covered and visible as a countable inventory item.

---

### One-line summary

- Reference script: **29** asset types (incl. 7 UC/MLflow + 3 inventory-only-modern).
- This utility: **19** fully covered + **9 migration-critical additions** the reference
  misses (all now visible/countable) + **2 inventory-only** (apps, lakebase, flagged
  manual); **8 dropped** by design (7 UC/MLflow + vector search).
