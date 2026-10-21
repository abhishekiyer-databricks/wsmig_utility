# 📋 Runbook

The step-by-step guide to running a real migration. Follow it top to bottom. Every step names the
**exact widget values** to set and **what you should see** before moving on.

> 🧭 **First time?** Read the [Architecture overview](ARCHITECTURE.md#-the-big-picture) (5 min) and
> confirm access with the [Permissions Guide](PERMISSIONS_GUIDE.md). Then start here.

## 📖 Contents

1. [Before you begin — prerequisites](#-before-you-begin--prerequisites)
2. [Choose your mode](#-choose-your-mode)
3. [Path A — `direct` mode (recommended)](#-path-a--direct-mode-recommended)
4. [Path B — `airgap` mode](#-path-b--airgap-mode)
5. [Reading the outputs](#-reading-the-outputs)
6. [Handling failures & manual actions](#-handling-failures--manual-actions)
7. [Dashboards: publish, credentials, schedules](#-dashboards-publish-credentials-schedules)
8. [Owners & run-as identities](#-owners--run-as-identities)
9. [Re-running (incremental migrations)](#-re-running-incremental-migrations)
10. [Quick reference card](#-quick-reference-card)

---

## ✅ Before you begin — prerequisites

Tick every box before your first run. Details are in the [Permissions Guide](PERMISSIONS_GUIDE.md).

| ☐ | Prerequisite | How to satisfy |
|---|--------------|----------------|
| ☐ | **Target run-as SP is a target workspace-admin** | Add the SP to the target `admins` group |
| ☐ | **Staging UC Volume exists** and the run-as SP has `READ`/`WRITE VOLUME` | Create a `/Volumes/<cat>/<sch>/<vol>` path |
| ☐ | **State catalog + schema exist** (for live runs) and the SP can `USE` + `CREATE TABLE` + `MODIFY`/`SELECT` | Pre-create the shared catalog+schema; the tool creates the tables |
| ☐ | **Repo pulled into the target** as a Git folder | Databricks → Repos → add this repo |
| ☐ *(direct only)* | **Source read SP is a source workspace-admin** with a Databricks **OAuth M2M secret** (`client_id` + secret) | See [Permissions › source SP](PERMISSIONS_GUIDE.md#-direct-mode-the-source-read-sp) |
| ☐ *(direct only)* | **Source secret stored in a target secret scope** (preferred), run-as SP has `READ` on it | Store the secret in a scope → set `source_sp_secret_scope` / `source_sp_secret_key` |

> ⚠️ **Do the dry run first, always.** Import defaults to `dry_run=true` — a full rehearsal that
> writes nothing. Never set `dry_run=false` until a dry run reads clean.

### 🔒 Restricted-network environments (read this if egress is locked down)

Private, VDI-only workspaces with an outbound proxy and no public PyPI need three things set up
before the first run. All are one-time cluster configuration.

**1. Use an all-purpose (classic) cluster — not serverless.**
If the workspaces are not reachable over the public internet **and** the Databricks serverless IP
ranges are **not** whitelisted, run these notebooks/jobs on an **all-purpose (classic) cluster** in
the workspace's own network. Serverless compute may not be able to reach the workspace/control plane
or the staging Volume. Point the jobs at an existing all-purpose cluster.

**2. Set `no_proxy` as cluster _Environment variables_ (not Spark config).**
If the cluster's egress goes through an HTTP proxy, traffic to the Databricks control plane **and**
to the staging Volume's storage must bypass it — otherwise Volume writes can stall or fail and the
bundle lands empty. Add `no_proxy` (and `NO_PROXY`) under the cluster's **Advanced options →
Environment variables**:

| Staging Volume type | `no_proxy` entries |
|---------------------|--------------------|
| **Managed** | `*.azuredatabricks.net,*.databricks.azure.com,169.254.169.254,127.0.0.1` *(Databricks only)* |
| **External (ADLS-backed)** | the managed list **plus** `.dfs.core.windows.net,.blob.core.windows.net` *(Azure storage)* |

> It must be an **OS environment variable**, not Spark config (Spark config does nothing here). Run
> `DESCRIBE VOLUME <catalog>.<schema>.<volume>` to check whether your staging Volume is managed or
> external.

**3. Handle the libraries when `%pip install` is blocked.**
The notebooks run `%pip install databricks-sdk openpyxl requests`. On DBR 15.4 LTS only `openpyxl` and
`et-xmlfile` are actually downloaded — the rest ship with the runtime. Two options:
- **Pre-install `openpyxl`** (it brings `et-xmlfile`) as a cluster library on the all-purpose cluster, so the
  `%pip` cell downloads nothing — simplest when PyPI is fully blocked.
- **Allow PyPI on the proxy — BOTH hosts:** `pypi.org` (the index pip looks packages up in) **and**
  `files.pythonhosted.org` (where the files are downloaded from). With `pypi.org` alone the lookup works but the
  download fails with connection or timeout errors. Details and a check for your own runtime:
  [Permissions › Python libraries through a proxy](PERMISSIONS_GUIDE.md#-python-libraries-through-a-proxy-pypi-allow-list).

> 📈 **Very large source workspaces:** for a workspace with hundreds of thousands of assets, give the
> cluster a **larger-memory driver** (e.g. 64–128 GB). Migrations are infrequent, so size up for the
> rare huge workspace rather than tuning.

---

## 🔀 Choose your mode

![Choose airgap when there is no connectivity between source and target; choose direct otherwise](diagrams/which-mode.png)

| If… | Use | Why |
|-----|-----|-----|
| The target **can reach** the source over the network | **`direct`** *(default)* | One end-to-end Job, no manual file hop |
| There is **no connectivity** between source and target | **`airgap`** | Two sides + a manual bundle handoff |

---

## 🟢 Path A — `direct` mode (recommended)

Everything runs in the **target** workspace. The whole migration is two Job runs: a **dry run**,
then a **live run**.

![Direct-mode sequence: install jobs, dry-run job (inventory, export, import with no writes), review, then the live job](diagrams/direct-sequence.png)

### Step 1 · Install the jobs

Open **`notebooks/00_Install_Jobs`** in the target workspace and set:

| Widget | Value |
|--------|-------|
| `deploy_jobs` | `direct_end_to_end_dry_run`, `direct_end_to_end_live` |
| `run_as_sp` | the target workspace-admin SP's `applicationId` |
| `connectivity_mode` | `direct` |
| `source_workspace_id` | the numeric source workspace id |
| `staging_location` | `/Volumes/<cat>/<sch>/<vol>` |
| `source_workspace_url` | `https://adb-<source>.azuredatabricks.net` |
| `source_sp_client_id` | the source read SP's `applicationId` |
| `source_sp_secret_scope` / `source_sp_secret_key` | pointer to the stored secret |
| `state_catalog` / `state_schema` | your shared, pre-existing catalog + schema |

**Run it.** ✅ *Expected:* the two jobs appear under **Jobs & Pipelines**, with config baked into
their parameters. You won't need to re-enter widgets on the jobs themselves.

> 💡 Prefer the secret **scope pointer** over `spn_secret_value`, and keep
> `allow_secret_in_job_params=false` so no secret is ever written into a job definition.

### Step 2 · Run the dry run

Run the **`wsmig - direct end-to-end (DRY RUN)`** job. It runs `01 Inventory → 02 Export →
04 Import` with the import task pinned `dry_run=true`.

✅ *Expected:* the job succeeds; the import task **writes nothing to the target**; a
`import_status_dry_run.xlsx` and a preflight verdict are produced.

### Step 3 · Read the dry-run reports

Open the bundle at `<staging_location>/wsmig/<source_workspace_id>/<run_id>/`:

1. **Preflight verdict** (printed in the import task output + `misc/preflight_report.json`) — must
   be **GO**. Fix anything 🔴 **BLOCKING** first (see [Permissions troubleshooting](PERMISSIONS_GUIDE.md#-troubleshooting-symptom--cause--fix)).
2. **`reports/import_status_dry_run.xlsx`** — one sheet per asset type, showing the **intended
   action** (create / update / skip / manual) for every unit. Scan for surprises.

> 🚦 **Gate:** proceed only when preflight is **GO** and the intended actions look right.

### Step 4 · Run the live migration

Run the **`wsmig - direct end-to-end (LIVE)`** job (identical, but import is pinned
`dry_run=false`). ✅ *Expected:* assets are created/updated on the target in dependency order,
ACLs replayed last, and `reports/import_status.xlsx` + `reports/manual_actions_import.md` are
written.

### Step 5 · Review & finish

Work through [Reading the outputs](#-reading-the-outputs) and
[Handling failures & manual actions](#-handling-failures--manual-actions).

> ℹ️ Prefer to run stages individually (e.g. re-import only)? Use the single-task `inventory` /
> `export` / `import` jobs, or open the notebooks directly — the widget values are the same.

---

## 🔵 Path B — `airgap` mode

Two sides that never connect. A human moves the bundle between them.

![Airgap-mode sequence: source side runs inventory and export to staging A, operator moves the bundle to staging B, target side imports](diagrams/airgap-sequence.png)

### On the SOURCE side

**Step 1 · Pull the repo into the source workspace** as a Git folder.

**Step 2 · Install + run the source job.** In `00_Install_Jobs` set `deploy_jobs=airgap_source`,
`connectivity_mode=airgap`, `source_workspace_id`, `staging_location = <location A>`, `run_as_sp` =
the **source** workspace-admin SP. Run the **`wsmig - airgap_source`** job (`01 Inventory →
02 Export`). *(No `source_*` widgets — airgap runs inside the source.)*

✅ *Expected:* a complete bundle under `<location A>/wsmig/<source_workspace_id>/<run_id>/` with a
`misc/manifest.json`.

### The HANDOFF

**Step 3 · Move the bundle.** Download the entire run directory from **location A** and upload it to
the target-readable **location B**, preserving the folder structure.

> ⚠️ Move the **whole** run directory (including `misc/manifest.json`). The target verifies counts +
> checksums against the manifest before it acts — an incomplete upload is caught at preflight.

### On the TARGET side

**Step 4 · Import (dry run).** Pull the repo into the **target**. Open `04_Import` (or install the
`import` job) with `connectivity_mode=airgap`, `source_workspace_id`, `staging_location = <location
B>`, `run_id = <the run id from location A>`, `dry_run=true`. Run it and read the dry-run reports
(same as [direct Step 3](#step-3--read-the-dry-run-reports)).

**Step 5 · Import (live).** Re-run with `dry_run=false` + `state_catalog`/`state_schema`. Review as
below.

---

## 📂 Reading the outputs

Everything lands in the bundle at `<staging_location>/wsmig/<source_workspace_id>/<run_id>/`:

> 📍 **Finding a run's folder:** open the job run → click a task → its output **ends** with
> `Run id : <run_id>` and `Folder : <full path>/` (`reports/` = the xlsx, `misc/` = the JSON + logs).
> The same two lines are also at the top of the output.

| File | Where | What it tells you |
|------|-------|-------------------|
| **`reports/import_status.xlsx`** *(live)* / **`import_status_dry_run.xlsx`** *(dry)* | `reports/` | One sheet **per asset type** — action, status, and notes for every unit. Includes the **ACL Parity** sheet (source-vs-target permission diff). **Read this first.** |
| **`reports/manual_actions_import.md`** | `reports/` | The checklist of steps only a human can do (secret values, AKV scopes, legacy dashboards, Git repos). Also have a workspace admin review the **workspace settings** (best effort — see below). |
| **`misc/preflight_report.json`** | `misc/` | The graded go/no-go verdict. |
| **`misc/import_results.json`** | `misc/` | Machine-readable per-unit results. |
| **`misc/execution*.log`** | `misc/` | Full execution log. |

**Import statuses you'll see:**

| Status | Meaning |
|--------|---------|
| `created` / `updated` / `adopted` | Succeeded (new / changed / matched an existing target object) |
| `skipped` | Unchanged since last run (fingerprint match) |
| `created_with_warning` | Exists on target but is known-degraded — **verify before use** |
| `manual` | Needs a human step (see `manual_actions_import.md`) |
| `deleted_in_source` | Existed on a previous run but is **now gone from the source** — **reported only**, never auto-deleted on the target (unless `allow_deletes=true`) |
| `skipped_no_object` | A permission waiting on an object this run didn't create (normal for DAB/repos) |
| `not_selected` | Not in this session's `import_assets` list |
| `failed` | A per-unit failure — fixable, then retry (**does not** mean the run broke) |

> 🟥 **`run_status = completed_no_report`** (import task red): every phase ran and the state **was**
> saved, but a report file (`import_status.xlsx`, `import_results.json` or `manual_actions_import.md`)
> did not land — the ERROR line in the output names the file and the cause. The target **was**
> changed. Fix the cause (usually the staging Volume), then re-run the import with the **same
> `run_id`**: every object SKIPs and the reports are rewritten.

---

## 🔧 Handling failures & manual actions

A `failed` unit is **not** a broken run — it's recorded with its reason and the run continues.
Fix the cause, then re-run only the outstanding units.

![Failure loop: fix the prerequisite, then re-run with retry_mode=failed_only](diagrams/retry-loop.png)

**The pattern:**

1. Open `reports/import_status.xlsx` and read the failure note (it always carries the **verbatim
   server message** + a remediation hint).
2. Fix the cause (grant a permission, assign an identity, create an AKV scope, etc.).
3. Re-run `04_Import` with **`retry_mode=failed_only`** — only the failed/degraded units re-run.
   Every re-run still makes the full upsert decision, so a retry can **never** duplicate. When a
   retry **heals** an object, its permissions are applied in the same run (and a healed AI/BI
   dashboard comes back published and scheduled) — no second `import_assets=acls` pass needed.

**Common manual actions** (from `manual_actions_import.md`):

| Manual action | What to do | Then |
|---------------|-----------|------|
| **Secret *values*** | Scope names + ACLs migrated; set the actual values on the target | — |
| **Azure Key Vault-backed scope** | Create by hand (UI → Create Scope → Azure Key Vault), naming the vault | `retry_mode=failed_only` |
| **Legacy SQL dashboards** | Rebuild the visual layout (underlying queries already migrated) | — |
| **Git repos** | Recreate from the exported metadata (url / provider / branch / path) | — |
| **Account identities not assigned** | Customer IT/SCIM assigns them (or grant account-admin) | re-run identity |
| **Permissions waiting on a DAB redeploy** | Redeploy the bundle to the target, then | `import_assets=acls` + `retry_mode=skipped_only` |

> 🔁 After you fix identities or redeploy a DAB, re-running just **`import_assets=acls`** with
> `retry_mode=skipped_only` back-fills the permissions that were waiting.

> ⚙️ **A cluster policy built on a policy family** (e.g. Job Compute, Personal Compute) that FAILED on 1.1.0 or
> earlier with `policy_family_id and definition cannot be used together` — and every job/cluster that uses it,
> which failed `prerequisite_missing` — is fixed in **1.1.1**. Pull 1.1.1 into the Git folder and re-run the
> import with **`retry_mode=failed_only`** (`dry_run=false`, same `run_id`, no re-export): the policy is created
> first, then the jobs/clusters that use it, then their permissions — all in that one run.

> ⚙️ **Workspace settings (workspace conf) are best effort — have a workspace admin review them.** The
> tool sets each setting with an API call and then reads it back. The read can lag the write, so a
> setting that does not show its new value at once is re-checked every 15 s for **up to 3 minutes**
> before it is reported. The report then says `set … (verified by read-back)` (or `… after N s`) or
> `failed` / `not_applied`. A target value that differs from the source is re-applied on each run
> (this also reverts a hand change made on the target). Because a workspace can ignore a setting
> through the API, or take longer than 3 minutes to show it, **a workspace admin should check the
> migrated settings in the admin console after the migration.**
>
> **A setting that is `failed` / `not_applied`** ("… the API returned 200, but the target still reads
> … after 3 min"): the target did not take that key through the API. Set it by hand in the **admin
> console**, then re-run — the row turns green once the target really holds the source value.

> 🧞 **A Genie space failing with `Table '<catalog>.<schema>.<table>' does not exist`**: Genie checks
> its Unity Catalog tables when the space is created. UC is out of scope, so create (or grant) the
> table on the target first, then `retry_mode=failed_only`. The tool only says "targets workspace
> folder … which does not exist" when a live check of that exact folder returned 404 — otherwise the
> note is the server's own error.

> 🔄 **Synced-table pipelines** (`Synced table: <catalog>.<schema>.<table>`) are `manual`: the platform
> creates them, they have no source code, and the pipelines API cannot recreate them. Recreate the
> **synced table** on the target and the platform recreates its pipeline. Any other pipeline with no
> source code (`libraries` empty) is `manual` the same way — recreate whatever owns it.

> 🔌 **Lakeflow Connect pipelines are a manual step.** Both halves depend on a Unity Catalog
> **connection**, and UC is out of scope:
> - an **ingestion gateway** (spec has `gateway_definition`) is `manual` with a note naming its
>   connection;
> - an **ingestion pipeline** (spec has `ingestion_definition`) is still attempted, but fails unless
>   the same-named connection already exists on the target — and a database-connector one always
>   fails, because it points at the *source* gateway's pipeline id (`ingestion_gateway_id`), which the
>   tool does not remap.
>
> Create the connection on the target, then recreate the gateway and its ingestion pipeline in
> Lakeflow Connect. Treat any failed ingestion pipeline row as this manual step.

> ⚙️ **A job that 403s on a SQL warehouse** ("not authorized to use or monitor this SQL Endpoint")
> is usually just an **ordering effect**, not a missing permission: jobs are created before the
> warehouse's `CAN_USE` grant is replayed (**ACLs run last**). A plain **`retry_mode=failed_only`**
> re-run fixes it — the grant is now in place. No manual grant is needed when that `CAN_USE` existed
> on the source (it's migrated with the warehouse's ACLs).

> 💥 **If the import job died mid-run** (driver out of memory, node lost, job killed or timed out):
> just re-run it — with the **same `run_id`** if you can, so it resumes from the checkpoint. The state
> table is saved every **200 objects or 5 minutes**, whichever comes first, plus at the end of every
> phase, so at most ~5 minutes / < 200 objects of bookkeeping are lost; the re-run finds those objects
> on the target and adopts them instead of creating them again. **One exception:** AI/BI dashboards,
> Genie spaces, SQL alerts and legacy SQL queries created in those last minutes can't be told apart by
> name, so the re-run may create an **exact duplicate**. After a crashed run, check the dashboards /
> Genie spaces / alerts / queries it created and delete any duplicate by hand.

> 🧾 **`02_Export` refuses to start without an inventory.** If `misc/inventory.json` for the
> resolved `run_id` is missing, unreadable or incomplete, the export task goes red with the path and
> `run_id` in the message and writes nothing. Run `01_Inventory` first, or pass the `run_id` of a
> completed inventory. Export never re-runs the inventory by itself.

> 📁 **`.db_internal`, `.ide` and `.databricks` folders** are Databricks-owned. They are listed in
> the inventory and reports as **Skipped — platform-internal**, and are never read, copied or
> permissioned.

---

## 📊 Dashboards: publish, credentials, schedules

AI/BI (Lakeview) dashboards arrive on the target in the **same state as on the source**:

| On the source | On the target |
|---------------|---------------|
| Draft only | Draft only |
| Published | Published, with the **same credentials mode** |
| Published + scheduled | Published + every schedule (cron, timezone, name, warehouse) + its subscribers |
| Permissions | Copied as-is. Draft and published share one ACL, so there is nothing extra to copy |

Publish state and schedules show up as their own rows in `import_status.xlsx`, on the
**Dashboard Publish** and **Dashboard Schedules** sheets.

**What you need to know:**

- **Publisher credentials ("Shared data permissions").** The target dashboard is published **by the
  migration service principal**, because no API can publish as another user (and the API does not
  even say who the source publisher was). Viewers of a dashboard published with *publisher
  credentials* query with the **service principal's** data access, not the original publisher's.
  To restore the original owner's credentials, **have the owner re-publish it** on the target.
- **Published content = the source draft.** No API returns the content of a published revision,
  so the target is published from the source's **current draft**. If the source draft has edits
  that were never published, the target's published version includes them. The row is flagged
  `Created (warning)` with "source draft has unpublished changes".
- **Viewer credentials ("Individual data permissions") subscribers can't be recreated.** On such a
  dashboard Databricks only lets each user subscribe **themselves**. The schedule row carries ONE
  note listing every subscriber (also a line in `manual_actions_import.md`). Ask those users to
  re-subscribe.
- **Schedules are created PAUSED** when `pause_job_schedules=true` (the default, same rule as jobs):
  the source keeps running during the migration, and an unpaused copy would refresh twice and email
  every subscriber twice. Resume them at cutover.
- **Notification destinations are not created by the tool.** A destination subscriber is matched to
  a target destination with the same name and type. If there is none, the row says
  `create notification destination <name> on target, then re-run`. A plain re-run then adds it.
  The same applies to a subscribed user who is not on the target yet.
- **Nothing is ever unpublished or deleted.** If the source dashboard is unpublished or a schedule
  is deleted, the report flags it as **Deleted in source**. Unpublish / delete it by hand if that
  is intended, even with `allow_deletes=true`.
- An identical schedule that already exists on the target (same cron, timezone and name, for
  example one a person recreated by hand) is **adopted**, never duplicated.

---

## 👤 Owners & run-as identities

| Object | Owner on the target |
|--------|--------------------|
| **SQL queries** | The **source owner** — set after create and verified (`owner set to … (verified)`). If the change is refused the row is `created_with_warning` and the query stays owned by the migration SP. An owner deleted on the source is left alone. |
| **Jobs, SQL warehouses** | The source owner, through their permissions (`IS_OWNER` travels with the ACLs). |
| **DLT / Lakeflow pipelines** | The **migration identity** (see below). The source owner gets `CAN_MANAGE`. |
| **AI/BI dashboards, Genie spaces, clusters, Alerts V2** | The migration identity — there is no owner API; reassign by hand if it matters. |

**Pipelines — why the owner is kept.** On a Unity Catalog pipeline, changing the owner needs a
**metastore admin**, and the permissions call is all-or-nothing — so asking for the source owner cost
the pipeline **every** grant (`403 Only metastore admins can change pipeline owner`). The tool never
asks: it keeps the current owner, gives the source owner `CAN_MANAGE`, and applies every other grant
exactly. The **ACL Parity** sheet reads `match` with the note *owner kept on target (needs a metastore
admin)*.

A UC pipeline **runs as its owner** unless its spec sets `run_as`:

| Source pipeline ran as | Target runs as |
|------------------------|----------------|
| a **user** | the **migration identity** (a user can't be set as run-as) — so it needs access to the pipeline's tables |
| a **service principal** | **the same SP** (remapped if it was recreated), via the spec's `run_as` |

To set an SP as run-as, the migration identity needs the **Service Principal User** role on that SP
— being a workspace admin, or holding Service Principal **Manager**, is not enough. Without it the
target refuses (`403 PERMISSION_DENIED Cannot bind the service principal provided in 'run_as' field
… must have 'servicePrincipal.user' role`), and the pipeline is created without it, as
`created_with_warning` with the fix in the note; grant the role and re-run. Any other error (e.g.
the SP lacks `USE CATALOG`) fails the row with the server's message, so the pipeline never silently
runs as the wrong identity.

**Transferring a pipeline to its source owner afterwards** (a metastore admin): open the pipeline →
**Permissions** → make the source owner **Is Owner**; or call `PUT /api/2.0/permissions/pipelines/<id>`
naming the new `IS_OWNER` plus every other grant. The next import run keeps that owner (it is now the
current owner) and changes nothing.

---

## ♻️ Re-running (incremental migrations)

Re-runs are **expected and safe** — the same pair is migrated repeatedly over its life. Every asset
is UPSERTed against the state table:

- **New** source assets → created
- **Changed** source assets → updated (the stored target object, not a duplicate)
- **Unchanged** → skipped
- **Deleted in source** → reported (never auto-deleted unless `allow_deletes=true`)

Just run the live job again. Each run emits a fresh change report. See
[Architecture › Idempotent & incremental](ARCHITECTURE.md#-idempotent--incremental-runs).

> 🧪 Incremental diffs only show on **live** runs against the real state catalog/schema. A dry run
> uses a separate `…_dryrun` state table and will show everything as `create`.

### Targeting a specific bundle with `run_id`

Import doesn't ask which bundle to act on — it **resolves one automatically**, in this order:

1. An **explicit `run_id`** you pass (deliberate control — re-import a specific bundle).
2. Otherwise, the **`LATEST_EXPORT.json`** pointer → the newest bundle whose export *completed*. An
   import that crashed in that folder is **continued** from its checkpoint (work already done is
   skipped). An unfinished import in an *older* folder is never picked up — it holds an older copy of
   the source.
3. Otherwise it **fails loudly** — a `run_id` is never invented (that would import an empty bundle
   and report a falsely clean run).

Inventory and export with a blank `run_id` continue the **newest** folder only if it is unfinished;
otherwise inventory starts a fresh folder and export uses `LATEST_INVENTORY.json`.

**Standalone `retry_mode=failed_only`:** `run_id` can stay **blank** — it then uses the latest export.
What is retried comes from the state table, not from the folder.

**Repair run vs Run now (job runs):**

- **Repair run** (re-run only the failed tasks of a job run) keeps the inventory task's values, so the
  repaired export / import use the **same `run_id` and folder** and continue from the checkpoint.
- **Run now** starts a new job run: the inventory continues the newest folder only if its export
  never finished; otherwise it starts a **new** folder, then a fresh export and the import.

**When do you set `run_id` yourself?**

- **Direct end-to-end job** — **you don't.** `01_Inventory` generates the `run_id` and publishes it
  to the `02_Export` and `04_Import` tasks automatically, so all three act on one bundle.
- **Re-importing an older/specific bundle** — set it explicitly:
  - *Notebook:* set the **`run_id`** widget on `04_Import`.
  - *Job:* use **"Run now with different parameters"** and set the `run_id` parameter.
- **Airgap target import** — give `04_Import` the **`run_id` from the source side** (it travels with
  the bundle; the moved `LATEST_EXPORT.json` also names it).

---

## 🗂️ Quick reference card

**Direct mode, from zero to migrated:**

```
1. 00_Install_Jobs   → deploy_jobs = direct_end_to_end_dry_run, direct_end_to_end_live
                       + run_as_sp, connectivity_mode=direct, source_workspace_id,
                         staging_location, source_workspace_url, source_sp_client_id,
                         source_sp_secret_scope/key, state_catalog, state_schema
2. Run "direct end-to-end (DRY RUN)"      → read import_status_dry_run.xlsx + preflight = GO
3. Run "direct end-to-end (LIVE)"         → read import_status.xlsx + manual_actions_import.md
4. Fix failures → 04_Import retry_mode=failed_only
5. After DAB redeploy → 04_Import import_assets=acls retry_mode=skipped_only
```

**Widget cheat-sheet** (full detail → [Configuration Guide](CONFIGURATION_GUIDE.md)):

| Goal | Widget change |
|------|---------------|
| Rehearse, write nothing | `dry_run=true` |
| Go live | `dry_run=false` (+ `state_catalog`/`state_schema`) |
| Retry only failures | `retry_mode=failed_only` (`run_id` can stay blank) |
| Replay only permissions | `import_assets=acls` (+ `retry_mode=skipped_only`) |
| Skip an asset family (bundle scope) | `migrate_<family>=false` on `02_Export` |
| Limit this session's work | `import_assets=identity,compute,…` on `04_Import` |
| Don't pause migrated schedules | `pause_job_schedules=false` |

---

## 📚 Related

- ⚙️ Every widget explained → **[Configuration Guide](CONFIGURATION_GUIDE.md)**
- 🔐 Access & troubleshooting → **[Permissions Guide](PERMISSIONS_GUIDE.md)**
- 🏗️ How it works underneath → **[Architecture](ARCHITECTURE.md)**
