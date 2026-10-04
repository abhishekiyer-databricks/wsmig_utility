# PLAN 16.0 — Medium-size, re-usable source fixture bed (for all PLAN 16 QA)

**Status:** SPEC agreed with the user 2026-10-04. Script: `tests/fixtures_medium.py` (new, reusable for
any fresh source workspace). Replaces the oversized PLAN 13 bed (~150 users, ~10K objects) for PLAN 16
testing. Reference implementation to adapt (not copy blindly): `plan13-backlog-b1-b13:tests/fixtures_fvm1.py`.

## Goals
- **Medium size**: big enough to cover every asset type and every code path the waves touch, small
  enough that a full inventory → export → import runs in minutes.
- **Re-usable**: run against ANY fresh source workspace by setting a profile; every phase idempotent
  (re-running creates nothing twice — several Databricks creates do NOT dedupe by name, e.g.
  `service_principals.create`).
- **Known source truth**: the script writes `fixtures_manifest.json` (every object created, its id,
  owner, ACL grants, expected migration outcome) so QA validates the reports against it.
- **Account-level identities only** (user decision 2026-10-03/04): no workspace-local groups or SPs.

## Inputs (env vars / CLI args; nothing hard-coded to one workspace)
`WSMIG_PROFILE` (source workspace CLI profile), `WSMIG_ACCT_PROFILE` (account-console profile for that
workspace's account), `WSMIG_CATALOG` (source catalog for UC tables), `WSMIG_TARGET_PROFILE` +
`WSMIG_TARGET_CATALOG` (only for the `target_uc_prep` phase), `WSMIG_USERS` (comma-separated 15 Entra
user emails; default list in the script), Azure CLI login (`az`) for the Entra/UMI checks, a recent
Databricks CLI (`WSMIG_CLI`) for the DAB phase.

## Identities
| What | Names | Notes |
|---|---|---|
| 15 Entra ID users | from `WSMIG_USERS` | must exist in Entra + the account; **if missing → FLAG and continue** (do not create Entra users). Assign to the workspace; spread entitlements (`workspace-access`, `databricks-sql-access`, `allow-cluster-create`, `allow-instance-pool-create`) |
| 3 Databricks-managed account SPs | `ai27_acc_spn_1..3` | create-or-adopt at ACCOUNT level (dedupe by displayName first), assign to the workspace, varied entitlements; one of them is a workspace admin |
| 2 Azure UMI-backed SPs | `ai27_umi_1`, `ai27_umi_2` | look up with `az identity list`; **if missing → FLAG** (the user creates UMIs); if present, add to the account by the UMI's client id (applicationId) and assign to the workspace |
| 2 Databricks-managed account groups | `ai27_account_grp_1`, `ai27_account_grp_2` | account level, assigned to the workspace; **grp_2 nested in grp_1**; members = users + SPs |
| 2 Entra groups | `ai27_entragrp_1`, `ai27_entragrp_2` | look up with `az ad group show`; **if missing → FLAG** (the user creates them); provision to the account with `externalId` = Entra object id, assign to the workspace, members = some of the 15 users |
| Orphaned users | 2 of the 15 (configurable) | after their content is created (below), REMOVE them from the workspace (workspace assignment only; leave Entra/account untouched) so their homes become "deleted in source" → PLAN 9 backup path. Last phase before ACLs; idempotent |

## Workspace content
- **Per user (all 15):** under `/Users/<email>/wsmig/`: a Python, a SQL and a Scala notebook, each with a
  few real cells (not empty); a `.py` and a `.csv` workspace file; one sub-folder with one more notebook.
- **Legacy SQL queries:** 1 per user (in the user's folder) + 2 in `/Shared`, against a warehouse below.
- **`/Shared`:** a folder tree with notebooks + files, with ACLs, left as-is (its root ACL is immutable).
- **DAB-deployed:** real `databricks bundle deploy` of (a) a bundle rooted under `/Shared/...` and (b) a
  bundle under a user directory, each containing a job, a DLT pipeline and a dashboard (all expected to be
  SKIPPED on import as DAB-owned); keep the existing path-less DAB case.
- **Big file:** one workspace file > 10 MB (streaming upload route) + one notebook over the export cap
  (expected manual).
- **1 Git folder** (metadata only; expected manual).

## Cross-user ACLs (explicit, in addition to §ACL matrix)
- user2's notebooks → **user1 CAN_MANAGE**.
- `ai27_account_grp_1` → **CAN_MANAGE on the `wsmig` directories of user4 and user5**.
- one Entra group → CAN_RUN on a `/Shared` folder; one SP → CAN_EDIT on a user notebook.

## Compute
- Instance pools (2), cluster policies: custom (incl. one pinning a pool id) **+ one policy-FAMILY policy
  ("Job Compute" family, `policy_family_id` + overrides — the B3 case; expected to FAIL on `main`)**.
- All-purpose clusters (as in the reference bed), left TERMINATED.
- **Cluster library case:** a TERMINATED cluster with a valid library (PyPI package). On import with the
  default `library_force_start_clusters=false` it is not installed (failed/skipped); a retry with
  `library_force_start_clusters=true` + `retry_mode=failed_only` installs it and the cluster is stopped
  again.

## Jobs + DLT
All job shapes and DLT variants from the reference bed (`phase_jobs`, `phase_dlt`): notebook / python /
SQL / dbt / run_job / pipeline tasks, schedules, continuous, existing vs new clusters, run_as a user and an
SP; DLT serverless, classic, continuous.

## SQL + AI/BI + Genie
- SQL warehouses: 1 pro + 1 serverless. Legacy queries (above). Alerts v2: 2.
- **UC tables** (`phase_uc`): the tables dashboards/Genie/DLT read, in `WSMIG_CATALOG`. The
  `target_uc_prep` phase creates the same catalog/schema/EMPTY tables on the target (UC is out of scope
  for the tool; without them dashboards/Genie don't render).
- **AI/BI dashboards** (must RENDER — verify, the API accepts invalid widgets): each has **1–2
  visualizations (e.g. bar + counter) and 1–2 filters**, dataset query named `main_query`. Combinations:
  1. DAB-deployed (in the bundles above) → expected SKIPPED
  2. draft only
  3. published, Shared (embed credentials)
  4. published, Individual (per-viewer credentials)
  5. published + scheduled (schedule needs publish first)
  6. published + scheduled + ACLs (users, group, SP at different levels)
- **Genie spaces:** 2, over the UC tables, with ACLs (Genie has no CAN_VIEW).

## Secrets, misc, serving
- Secret scopes: 1 Databricks-backed with 2 secrets + ACLs (user, group, SP); 1 AKV-backed (expected manual).
- Global init scripts: 2 (different position/enabled).
- **Workspace conf: set to NON-DEFAULT values** so a reflected change is visible on the target (user
  2026-10-04). At least: `enableExportNotebook=false`, `enableResultsDownloading=false`,
  `enableNotebookTableClipboard=false`, `maxTokenLifetimeDays=30`, `enableWebTerminal=false` (one that some
  workspaces silently drop — B4), and keep the reference bed's other keys. Record the source value of
  each in the manifest.
- 1 external-model serving endpoint (as in the reference bed).

## ACL matrix (all resources × users, groups, SPs)
Every object type gets grants from a user, an account group, an Entra group and an SP across its
permission ladder (adapt the reference `phase_acls`, scoped to this bed): directories, notebooks, files,
clusters, pools, policies, jobs, pipelines, warehouses, queries, alerts, dashboards, Genie, secret scopes,
serving endpoints. Plus an IS_OWNER on a job/pipeline/warehouse held by a different user than the runner.

## Incremental seed (for Run 2)
Keep `phase_incremental` (excluded from `all`): edits to existing notebooks/files/a policy/a query, a new
notebook + file + user, a group membership change, a dashboard draft → publish, an ACL change.

## Script behaviour
- Phases run in dependency order; `python tests/fixtures_medium.py [phase ...|all|check]`.
- `check` = read-only prerequisite report: CLI profiles reachable, account access, the 15 users, the 2
  UMIs, the 2 Entra groups, catalog — prints a clear FLAG list of what the user must create.
- Missing prerequisites never abort the whole run: the dependent objects are skipped and listed in the
  final FLAG summary + manifest.
- No secrets printed; nothing destructive outside what the script itself created, except the deliberate
  orphan-user workspace un-assignment.
- Known gotchas to respect: see memories `fixture-dashboard-genie-render`, `fixture-api-gotchas-2026-08-06`,
  `source-fixtures-identity-ownership`, `dab-detection-and-cli-version`, `fvm1-test-fixtures-and-akv-state`.
