# PLAN 10 — Incremental AIRGAP testing (real in-workspace jobs) + change-detection audit

**Status:** Active test campaign (2026-08-28). Self-contained so it survives a context clear.
**Role:** Act as a human Databricks platform tester. NO migration code runs on the laptop — everything
runs as **Jobs in the workspace, run-as an SPN**. The CLI is used ONLY as the control plane a human
would use: create the Git folder, run `00_Install_Jobs`, fill widgets, trigger/monitor runs, and (airgap)
transfer the bundle dir from the source Volume to the target Volume. 0 assumptions; find bugs + RCA.

## Operating model
- **Airgap**: run `01_Inventory`+`02_Export` as a Job on **source_ws** (run-as source SPN) → bundle lands
  in a source UC Volume → **CLI-transfer** the run dir to a target UC Volume → run `04_Import` as a Job on
  **target_ws** (run-as target SPN). NO cross-workspace connectivity.
- **Direct** (later): everything on target_ws; source read via OAuth M2M.
- **2 airgap runs**: run 1 = baseline full migration (as-is, INCLUDING the already-staged orphan user +
  orphan SP use case). run 2 = **incremental** after applying the source changes in §Change matrix.
  (The earlier "run1=secret value / run2=secret scope" rule is a DIRECT-mode source-credential thing and
  does NOT apply to airgap — airgap has no source secret; each side uses its run-as SPN's context token.)

## Environment (verify live every run — target may be re-created between modes)
- **Git**: repo `https://github.com/abhishekiyer-databricks/wsmig_utility`, branch
  **`feature/ws_import`**. Create a Repos/Git folder on BOTH workspaces (I create them via Repos API).
- **source_ws**: `adb-1234567890999999.19` (profile `source_ws`, valid). Run-as SPN =
  client_id `11111111-1111-1111-1111-111111111111` (secret in `~/Desktop/Client ID: REDACTED.md`;
  this SPN is in source `admins` = ai27_umi). Ops catalog/schema `catalog_ws_bozvdk.source_operations`.
  Create my own staging Volume there (e.g. `/Volumes/catalog_ws_bozvdk/source_operations/wsmig_stage`).
- **target_ws (current)**: `adb-1000000000000002.6` (profile `target_ws`, valid). Run-as SPN
  `22222222-2222-2222-2222-222222222222` (**present now**; if a re-create removes it, import + make it
  workspace-admin as `abhishek.iyer`). Ops catalog/schema `catalog_6_3aez8m.target_operations` (**exists
  now**). Create my own staging Volume + use `target_operations` (or a wsmig_* schema) for the migration
  STATE tables. **Re-verify host + SPN + catalog every time we switch modes; if missing and no re-create
  was announced, STOP and ask.**
- **Capabilities (2026-08-28, all GREEN — no substitutions needed):** `az` CLI logged in (tenant
  bf465dc7…) and user is **Azure account admin** → Entra group + UMI-SPN creation/edits via `az ad`.
  **`source_acct` re-authed and VALID** → Databricks **account SCIM available** → account-level group
  membership + UMI/account-SPN OAuth secret create/delete + account-SPN create/delete are testable FOR
  REAL. Workspace admin on both workspaces covers all workspace-scoped entitlements/permissions/content.

## Deliverables (all under `~/Downloads/wsmig_runs/`)
1. `airgap_1_import.xlsx` — run-1 import report (copied from the target bundle's `reports/import_status.xlsx`).
2. `airgap_2_import.xlsx` — run-2 (incremental) import report.
3. `airgap_source_changes.xlsx` — the exact list of changes I applied on source before run 2. COLUMNS
   (fixed): **Resource Type | Resource Name | Update Done** | New-or-Existing | How Applied (API/az/acct-SCIM/
   ws-SCIM) | Capability | Timestamp | Expected detection in run 2. (Resource Type + Name + Update Done are
   the three you asked for; the rest are supporting evidence.)
4. `airgap_incremental_test_report.xlsx` — per change: what run-2 captured (status+note+fingerprint move),
   expected vs actual, CAPTURED / MISSED, and BUG? (Y/N) with RCA pointer.
5. Final response: **job-run URLs** (source inventory/export + target import, both runs) + **output Volume
   paths** (source bundle + target bundle).
Any bug → add to a bugfix plan (`plans/PLAN_11_incremental_bugfixes.md` or append here) with RCA.

## Execution checklist
1. **Pre-flight**: verify source_ws/target_ws hosts; target SPN present (import if not); ops catalogs exist;
   create staging Volumes on both; confirm the orphan scenario is intact on source (users mayuresh/yatin
   deprovisioned w/ home content; SP 33333333 deprovisioned w/ orphan-file; pool→policy→job chain).
2. **Git folders**: create Repos on both workspaces (branch feature/ws_import). REPO_PATH = the folder path.
3. **Install jobs**: run `notebooks/00_Install_Jobs` as a one-time job run on each side, widgets filled
   (deploy_jobs selects: source → `airgap_source`; target → `import`). It projects config into
   `jobs/*.job.json` base_parameters and creates the jobs.
4. **Run 1 (baseline)**:
   a. Trigger `airgap_source` (or the inventory+export job) on source_ws; monitor to success; capture run URL.
   b. CLI-transfer `<src Volume>/wsmig/<src_ws_id>/<run_id>/` → `<tgt Volume>/wsmig/<src_ws_id>/<run_id>/`.
   c. Trigger `import` job on target_ws (dry_run=false; state_catalog/schema set); monitor; capture run URL.
   d. Copy target `reports/import_status.xlsx` → `~/Downloads/wsmig_runs/airgap_1_import.xlsx`.
   e. **Validate** against the live workspaces (identities, compute, jobs, workspace content incl.
      `/Users_Backup/<orphans>`, sql, dlt, dashboards, genie, secrets, ACL parity). Cross-check counts +
      spot-check objects. Record findings.
5. **Source changes** (§Change matrix): apply exactly ONE change per listed case to ONE resource; log each
   into `airgap_source_changes.xlsx`. Create resources where needed (jobs, pools, etc.). Use az for Entra
   (account admin), source_acct for account-group membership + account/UMI-SPN secret+create/delete, and
   workspace SCIM/permissions for everything workspace-scoped. All real — no substitutions.
6. **Run 2 (incremental)**: repeat 4a–4d with a NEW run_id (reuse the SAME target state schema so UPSERT
   fingerprints decide create/update/skip). Output → `airgap_2_import.xlsx`.
7. **Validate run 2 + build the incremental report** (§Change matrix expected column): for each change,
   did run 2 show created/updated (or correctly skip)? Cross-check target. Mark CAPTURED/MISSED + BUG?.
8. **Report**: fill `airgap_incremental_test_report.xlsx`; file bugs+RCA; assemble the final test report
   with URLs + Volume paths.

## Available test users (for any "add user / add member" case — exist in both Entra and the DBX account)
`sunny.singh@databricks.com`, `aman.jain@databricks.com`, `kushagra.parashar@databricks.com`,
`shailesh.bobay@databricks.com`, `abhishek.dey@databricks.com`. Use a distinct one per case where possible.

## Change matrix for run 2 (one change per case; names are examples — pick any one resource of that type)
**Create-if-needed principle:** apply each change to a suitable EXISTING resource where one exists; if none
is suitable (or the change kind is inherently a "create", or reusing one would collide with another case),
**CREATE a new fit-for-purpose resource** for it — and record it in `airgap_source_changes.xlsx` with
New-or-Existing=New. Every row must carry Resource Type + Resource Name + Update Done.

Legend cap = capability needed: WS=workspace admin (have), ACCT=account SCIM (source_acct — VALID now),
AZ=Entra via az (logged in, account admin). All capabilities available → every case tested for real.

### Users (WS/ACCT)
- U1 Add a NEW user to the workspace.
- U2 Change a user's ENTITLEMENT (e.g. turn on "Allow instance pool creation").

### Groups
- G1 Account group: ADD a member (ACCT/AZ).
- G2 Account group: REMOVE a member (ACCT/AZ).
- G3 Account group: add an ENTITLEMENT (e.g. allow-cluster-create) (ACCT/WS).
- G4 Account group: grant a PERMISSION (a principal CAN_MANAGE the group) (ACCT/WS).  ⟵ watch (ACL-only)
- G5 Entra group: ADD a member (AZ).
- G6 Entra group: REMOVE a member (AZ).
- G7 Entra group: add an ENTITLEMENT (WS).
- G8 Entra group: grant a PERMISSION (WS).  ⟵ watch (ACL-only)
- G9 Workspace-local group: ADD a member (WS).
- G10 Workspace-local group: REMOVE a member (WS).  ⟵ watch ("members added" messaging)
- G11 Workspace-local group: add an ENTITLEMENT (WS).

### Service principals
- SP1 UMI/account SPN: grant a PERMISSION (CAN_USE) (WS).  ⟵ watch (messaging)
- SP2 UMI/account SPN: add an ENTITLEMENT (WS).
- SP3 UMI/account SPN: CREATE an OAuth secret (ACCT).  ⟵ watch (has_secrets not fingerprinted?)
- SP4 UMI/account SPN: DELETE an OAuth secret (ACCT).  ⟵ watch
- SP5 Account SPN: DELETE the SPN (ACCT).  ⟵ watch (delete surfaced? default no-auto-delete)
- SP6 Add a NEW account/UMI SPN (ACCT/AZ).
  (Note: the source file lists two UMI "grant CAN_USE to a user" lines — that's one change KIND, covered
  once by SP1; likewise two databricks-SPN permission lines → covered by SP7. Deliberate de-dup, not a miss.)
- SP7 Databricks-managed SPN: grant a PERMISSION (CAN_MANAGE) (WS).  ⟵ watch (ACL-only)
- SP8 Databricks-managed SPN: add an ENTITLEMENT (WS).
- SP9 Databricks-managed SPN: CREATE an OAuth secret (WS).  ⟵ watch
- SP10 Databricks-managed SPN: DELETE an OAuth secret (WS).  ⟵ watch
- SP11 Databricks-managed SPN: DELETE the SPN (WS).  ⟵ watch (delete surfaced?)
- SP12 Add a NEW databricks-managed SPN (WS).

### Notebooks & Files (WS)
- N1 Create new notebook(s) in a user home. N2 Create new file(s). N3 Create a new SQL query
  (`.dbquery.ipynb`) ⟵ watch (created but not visible in target folder?). N4 Create an empty dir.
  N5 New dir with 1 notebook + 1 file (+ a SQL query — watch it lands). N6 DELETE a notebook ⟵ watch
  (delete not surfaced in Excel at all?). N7 Empty dir under /Shared. N8 /Shared dir with files.
  N9 EDIT an existing notebook's CONTENT (fingerprint must move — known audit gap risk).

### Jobs (WS) — do "2nd pass" items in ONE shot; create jobs as needed
- J1 Create a NEW job. J2 Add a 2nd TASK to an existing job. J3 ADD a schedule. J4 CHANGE a schedule.
  J5 Add metric thresholds/health. J6 Unpause a job (should re-pause on import). J7 Change a tag VALUE.
  J8 Add a NEW ACL (CAN_MANAGE) ⟵ watch (ACL-only skip?). J9 REMOVE an ACL ⟵ watch. J10 DELETE a job
  ⟵ watch (delete surfaced?). J11 Change run_as. J12 Change a task cluster to SERVERLESS (old cluster
  handling). J13 Add a tag (new key). J14 PAUSE an unpaused job (distinct from J6 unpause — the file
  lists both; verify the pause-state change is detected and import keeps it paused).

### Compute (WS)
- C1 Create new all-purpose cluster. C2 Change autoscale on a cluster. C3 Add spark_conf to a cluster.
  C4 Cluster ACL (CAN_MANAGE) ⟵ watch. C5 Create new SQL warehouse. C6 Change warehouse max clusters.
  C7 Change warehouse t-shirt size. C8 Warehouse ACL ⟵ watch. C9 Create new instance pool. C10 Change
  pool max_capacity. C11 Change pool node_type. C12 Pool ACL ⟵ watch. C13 Create new cluster policy.
  C14 Update a policy definition. C15 Policy ACL ⟵ watch.

### Global init scripts (WS)
- GI1 Add a new GIS. GI2 Update an existing GIS (content/enabled).

### SQL Alerts (WS)
- A1 Update an existing alert. A2 Add a new alert.

### DLT (WS)
- D1 Pipeline ACL (CAN_MANAGE) ⟵ watch. D2 Update pipeline CODE/settings. D3 Create a new pipeline.

### AI/BI Dashboards (WS)
- DB1 Create a new dashboard. DB2 Update a dashboard (add a filter). DB3 Dashboard ACL ⟵ watch.

### Genie (WS)
- GE1 Update a Genie space (remove a table). GE2 Genie ACL ⟵ watch.

### Secret scopes (WS)
- S1 Create a new secret scope. S2 DELETE a secret scope ⟵ watch (delete surfaced?). S3 Add a secret to
  an existing scope (scope fingerprint should reflect key set change).

### Expert additions (not in the user's file — added for coverage)
- X1 Notebook CONTENT edit (N9 above) — the historical fingerprint blind spot; MUST detect.
- X2 Rename/move a resource (e.g. cluster) if feasible — natural_key change behaviour.
- X3 A no-op re-run of an UNCHANGED resource must SKIP (fingerprint match) — negative control.
- X4 Secret VALUE rotation on an existing key (values aren't exported — confirm it does NOT falsely
  report a change, or is correctly a manual note).

## Suspected-bug hotspots (user's prior notes = HYPOTHESES to verify independently, not facts)
- ACL/permission-ONLY changes may not move the fingerprint → wrongly SKIP "unchanged" (jobs J8/J9,
  compute C4/C8/C12/C15, dlt D1, dashboard DB3, genie GE2, group G4/G8, SP SP1/SP7). Likely the biggest
  cluster of real bugs: ACLs live in `acls.json` and are fingerprinted per-object grant-set; verify a
  grant change moves the ACL unit's fingerprint and the report shows updated, not skipped.
- Identity secret add/remove (SP3/4/9/10) — `has_secrets` not fingerprinted (known gap) → messaging.
- DELETES not surfaced (SP5/SP11/J10/N6/S2) — deleted-in-source is report-only by design (no auto-delete),
  but it MUST still appear as `deleted_in_source`, not "unchanged (fingerprint match)" or nothing.
- Group membership messaging (G10 "3/3 members added" wording; entra G5/G6 showing "unchanged").
- SQL query (`.dbquery.ipynb`) created but not visible in the target user folder (N3/N5/N8).
- Job cluster→serverless (J12) leaving an orphan unused cluster.
- Notebook content edit (X1) silently skipping.

## Notes
- Reuse the SAME target state schema across run 1 and run 2 so UPSERT fingerprints drive incremental
  detection (that is the whole point). Use a fresh run_id per run.
- The tool never auto-deletes (allow_deletes=false default); deletes are REPORTED as deleted_in_source.
- Related: [[plan9-orphaned-home-backup]] (orphan divert, live-verified), [[plan8-e2e-testing-fixes]],
  [[import-control-table-write-cadence]], [[fingerprint-blind-spot-sp-secrets]].
