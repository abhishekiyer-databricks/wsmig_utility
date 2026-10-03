# PLAN 13 — Future backlog (living list of bugs, gaps & feature additions)

This is a **living backlog**. As we find new bugs, gaps, or feature requests during real runs, we add
them here, triage, and pull them into an implementation pass when we take them up. Nothing here is
implemented yet unless a line says otherwise. Related: [[PLAN_11_incremental_bugfixes]] (the last
fix pass, now fully validated) and [[PLAN_12_optional_scale_and_durability]] (optional hardening).

Severity legend: **HIGH** = correctness / data-loss; **MED** = parity / usability; **LOW** = polish.

**How to read this doc:** the [Backlog summary](#backlog-summary) table is the index; each **B\<n\>**
section below is the full write-up (reported-by, root cause verified in code, proposed change,
acceptance, verify-first). [Implementation sequencing](#implementation-sequencing) proposes an order;
[Testing](#testing) gives a concrete test case for **every** item (offline regression + live
verification) and points at the QA agent contract in [`plans/qa-testing-agent.md`](qa-testing-agent.md).

---

## Backlog summary

| # | Type | Sev | Title | Status |
|---|---|---|---|---|
| B1 | Feature | MED | `source_run_as_spn` widget for airgap source inventory/export | DONE (`test_b1_*` test_plan13.py; `job_templates.run_as_for_job` + `source_run_as_spn` widget in 00_Install_Jobs, airgap-source only) |
| B2 | Gap | MED | Object **owner/creator** not preserved for several resource types (owned by run-as SP on target) | DONE (`test_b2_*` test_plan13.py; legacy-query owner PATCH read-back verified, orphan→run-as-SP warning; jobs/pipelines/warehouses already IS_OWNER via ACL; dashboards/genie/clusters not settable=documented) |
| B3 | Bug | HIGH | Cluster-policy create sends `policy_family_id` + `definition` together → 400 for family-based policies | DONE (`test_b3_*` test_plan13.py; `_put_policy_shape` family_id+overrides XOR definition in create+edit) |
| B4 | Bug | HIGH | Workspace-conf is written without read-back → reports parity from a 200, not the actual value (`enableExportNotebook` stayed `true`, reported `false`) | DONE (`test_b4_*` in test_plan13.py; `verify_applied`+`VerificationFailed`+`CAT_NOT_APPLIED`, `_set_conf` read-back) |
| B5 | Feature | MED | Structured per-step logging **matching the UC utility's shipped `logging_util.py`** — live in the notebook cell (not a file), per-object action lines, run_id+stage, DEBUG default; handles the live-stdout + `notebook.exit`-cell gotchas; stuck/failed run diagnosable from the cell alone | DONE (`test_b5_*` in test_plan13.py; logger.py ported UC infra + bridge, notebooks wired, per-unit line) |
| B6 | Feature | MED | Bounded thread-pool parallelism — inventory/export enrichment + **super-safe** intra-sub-level import parallelism (barriers between dependency sub-levels), reusing the existing retry/backoff; no silent failures | DONE (`test_b6_*` test_plan13.py). Scope 1: `parallel_threads`+BaseCollector.map_parallel, workspace ACL enrichment parallelised. Scope 2: sub-level parallel import (asset_type sublevels + barriers, lock-guarded recording); identity+misc stay serial (parallel_safe=False); threads=1 byte-identical; forced-parallel tests prove no lost/dup rows + barrier ordering |
| B7 | Gap | HIGH | AI/BI dashboard **publish-state parity** — only the draft migrates; a source-published dashboard lands draft-only on target (consumers see nothing). Must preserve published-vs-draft, owner, schedule(s), permissions, and publisher/viewer credentials | DONE (`test_b7_*` test_plan13.py; collector captures publish+schedules, importer reconcile: idempotent diff-guard publish, read-back verify, Shared→run-as-SP, flag-only unpublish, schedule recreate, atomic status, fp folds facets) |
| B8 | Bug | HIGH | User-home content fails → endless `failed_only`: user homes are **PROTECTED + lazily provisioned** (`mkdir` impossible — 200/200 `DIRECTORY_PROTECTED` at scale; 0/100 homes 8 min after provisioning, live-verified), and the tool **can't create or force** them, yet it hard-fails + **caches the miss for the whole run**. Fix = **never mkdir**; defer home content → **one end-of-run re-sweep** → heal rest via `failed_only`; kill the stale cache | DONE (`test_b8_*` test_plan13.py; `DeferredHome`+`defer` resolution + re-sweep runs PRE-ACL (ImportRunner._resweep_home_content, just before the ACL phase = max lazy-provisioning time, still before ACLs); `_home_present` no longer pins absent; never mkdir a home) |
| B9 | Feature | MED | run_id is auto-managed within a job (inventory mints a **timestamp**, shares via taskValues — operator types nothing) but the dir name is an opaque timestamp with **no mapping to the Jobs-UI run id** → can't locate the bundle/report, and a **standalone `failed_only`** (no taskValues) resolves to the wrong dir. Make the auto value `{{job.run_id}}` so dir = job-run-id (correlatable), repair→same dir, new run→new dir; + actionable manifest-missing error | DONE (`test_b9_*` in test_plan13.py; jobs/*.json pin `{{job.run_id}}`, verify_bundle names LATEST_EXPORT) |
| B10 | Bug | MED | ACL phase attempts `.db_internal` **platform-internal** dirs → unfixable **403 PERMISSION_DENIED** (not even a workspace admin has Manage). Filed as `permission_denied` (failed/retryable) → never clears, endlessly re-attempted by `failed_only`, pollutes the success signal. Skip them at collection + import like `/Shared` | DONE (`test_b10_*` test_plan13.py; acl_importer skips `is_platform_internal` as not_supported; workspace_collector skips fetch_acl for internal paths) |
| B11 | Bug | MED | Import report-write failure is **swallowed** (`import_runner.run()` → `_write_reports` in a try/except that only logs a WARNING) → a run can report success with **no `import_status.xlsx`** written. Must be loud: surface in the notebook + mark the run not-cleanly-complete (report is the source of truth, ties B4) | DONE (`test_b11_*` in test_plan13.py; run_status→completed_no_report, ERROR+exc_info, post-write verify, notebook warning) |
| B12 | Feature | LOW | **Widget labels are ungrouped** — relabel every widget across all notebooks to the UC convention `"<N><letter>. <Group> · <desc>"` so Databricks sorts them into logical groups (all **Source** widgets together, then **Output** = staging+state, Bundle scope, Import, Run). Label-only; no behaviour change | DONE (`test_b12_*` test_plan13.py; each widget keeps its ORIGINAL display name with ONLY a numeric `<N><letter>. ` grouping prefix — no injected `Group ·` tag; names/defaults unchanged; new log_level/parallel_threads/catalog_mapping_json/source_run_as_spn widgets added) |
| B13 | Feature | MED | **Catalog rename on target** — a BU is changing its catalog name in the new region. Add a `catalog_mapping_json` widget (flat shape `{"source_catalog_name":"target_catalog_name"}`; blank = identity) and remap catalog references in **exactly three asset types: AI/BI dashboards, Genie spaces, DLT pipelines** (user-scoped 2026-10-01). All other catalog-bearing assets (legacy queries, job SQL tasks, notebook/file content, cluster/policy conf) are **documented OUT OF SCOPE**. Reuse the UC utility's `rewrite_catalog_references`; `parse_catalog_mapping` adapted to the flat shape | DONE (`test_b13_*` test_plan13.py; parse_catalog_mapping+remap_catalog_refs helpers, applied in dashboards/genie/dlt importers only, blank=identity) |

---

## B1 (FEATURE, MED) — airgap source side has no `run_as` SP widget for inventory/export

**Reported by the customer/user 2026-09-07.**

**What's missing.** In **airgap** mode, `01_Inventory` + `02_Export` run **inside the source
workspace** as a job, and the run-as identity for that source-side job is not something the user can
set cleanly today:
- `00_Install_Jobs` exposes a single `run_as_sp` widget documented as *"the identity each created job
  runs as — a **TARGET** workspace-admin SP"*, and the installer itself is meant to run in the target.
  So there is no first-class, source-scoped way to say "run the airgap SOURCE job (`airgap_source`,
  tasks 01→02) as **this SOURCE workspace-admin SP**."
- Result: an operator setting up the source side has to hand-edit the deployed `airgap_source` job's
  `run_as`, or accept whatever identity the source-side install defaults to.

**Proposed change.**
- Add a **`source_run_as_spn`** widget (applicationId of a **source** workspace-admin SP) used when
  deploying / running the **airgap source** job, so the source-side inventory+export runs as the
  intended source SP without hand-editing.
- Wire it through `00_Install_Jobs` (project it onto the `airgap_source` job's `run_as` when
  `connectivity_mode=airgap`, instead of reusing the target-scoped `run_as_sp`), and document that in
  `direct` mode this widget is ignored (01/02 run in the target and read source over OAuth M2M).
- Keep it credential-free (applicationId only; no secret in a widget), consistent with the auth model.

**Resolved (user 2026-10-01).**
- **Deploy path:** `source_run_as_spn` is a **widget in `00_Install_Jobs`**, used **only when
  `connectivity_mode=airgap`**, projected onto the **`airgap_source`** job's `run_as`. It is **not used
  by any other job** (direct-mode jobs keep the target `run_as_sp`). No separate source-side installer
  concept — one installer, this widget just routes to the airgap-source job's `run_as`.
- **`servicePrincipal.user` prerequisite:** **document it** (the creator needs `servicePrincipal.user`
  on the SP) — **no preflight test needed**: the job simply won't be created without it, so the Jobs API
  error at install time is the signal. Add the prerequisite + the exact grant to the docs/runbook.

---

## B2 (GAP, MED) — object owner/creator is NOT preserved for several resource types (they end up owned by the run-as SP on target)

**Found while answering the ownership question, 2026-09-07 — verified in code + against the PLAN 11
Run 5 bundle.**

**Current behaviour (accurate, per type).** Every object is CREATED by the run-as SP (the API
attributes a new object to its caller). Ownership is only restored afterwards if an **`IS_OWNER`**
grant is captured in `acls.json` and re-applied in the ACL phase's declarative `PUT permissions` body
(`acl_importer._acl_body` carries every explicit grant verbatim, principal remapped). So:

| Resource type | `IS_OWNER` captured? | Owner on target today |
|---|---|---|
| **jobs** | ✅ yes (14 grants in Run 5) | **restored** to source owner (remapped) via ACL phase |
| **DLT pipelines** | ✅ yes (5) | **restored** via ACL phase |
| **SQL warehouses** | ✅ yes (5) | **restored** via ACL phase |
| **clusters** | ❌ no | **run-as SP** — creator can't be set via API; source creator saved only in an `OriginalCreator` tag (documented, `compute_importer`) |
| **legacy queries** | ❌ no | **run-as SP** — `owner` is stripped as read-only at create; NOT re-set (even though the Queries **update** API exposes `owner_user_name`, which the F8 seed used) |
| **Lakeview (AI/BI) dashboards** | ❌ no | **run-as SP** — no `IS_OWNER` captured/applied |
| **Genie spaces** | ❌ no | **run-as SP** |
| **alerts (v2 / legacy)** | ❌ no | **run-as SP** (v2 create exposes `run_as_user_name`, not owner; legacy is manual) |

So the answer to *"is the owner maintained on target?"* is **type-dependent**: **jobs, DLT pipelines
and SQL warehouses keep their original owner**; **clusters, queries, Lakeview dashboards, Genie spaces
and alerts are left owned by the run-as SP.**

**Why it matters.** On target, an object owned by the run-as SP (rather than the original user/SP) can
change who can manage it, break "my objects" views, and diverge from source governance — a parity gap,
though not data loss (the object + its other ACLs migrate).

**Research — DONE (live-verified on source_ws 2026-10-01). This is now a dev spec, not a question.**
`GET /permissions/<type>/<id>/permissionLevels` + the create/update API shapes give the definitive
owner-settability per type:

| Type | Owner mechanism | Settable? (verified) | Action |
|---|---|---|---|
| **jobs** | `IS_OWNER` permission level **exists** (`CAN_VIEW, CAN_MANAGE_RUN, CAN_MANAGE, IS_OWNER`) | **YES** | already set via ACL phase (IS_OWNER grant) — keep |
| **DLT pipelines** | `IS_OWNER` **exists** (`CAN_VIEW, CAN_RUN, CAN_MANAGE, IS_OWNER`) | **YES** | already set via ACL phase — keep |
| **SQL warehouses** | `IS_OWNER` **exists** (`CAN_MANAGE, CAN_USE, IS_OWNER, CAN_MONITOR, CAN_VIEW`) | **YES** | already set via ACL phase — keep |
| **legacy queries** | `owner_user_name` on the Queries **update** API | **YES** (F8-proven) | **NEW:** after create, PATCH `owner_user_name` to the remapped source owner |
| **Lakeview dashboards** | permissionLevels are **`CAN_READ, CAN_RUN, CAN_EDIT, CAN_MANAGE` — NO `IS_OWNER`**; the dashboard object has **no owner field** (keys: `dashboard_id, display_name, warehouse_id, parent_path, serialized_dashboard, …`) | **NO** | **documented limitation.** No owner to set; CAN_MANAGE grants (ACL phase) + the home it lives in (`parent_path`, handled by workspace path-remap) are the only identity signals |
| **Genie spaces** | `create_space`/`update_space` take **no owner**; no `IS_OWNER` | **NO** | documented limitation (same as dashboards) |
| **clusters** | creator fixed by the API | **NO** | keep the `OriginalCreator` tag |
| **alerts (v2/legacy)** | v2 create exposes `run_as_user_name` (not owner); legacy = manual | partial | best-effort `run_as_user_name` on v2; legacy stays manual |

**Proposed change (dev spec).**
- **Queries — why a PATCH, and it MUST be read-back verified.** `owner_user_name` is **read-only at
  CREATE** (stripped), so the only way to set the source owner is a **follow-up PATCH** via the Queries
  update API (F8-proven). Per the report-truth rule (B4), that PATCH must be **GET-verified**: after it,
  re-GET the query and confirm `owner_user_name` == intended; record FAILED (observed vs intended) on
  mismatch — never report "owner set" from a bare 200. Don't advance the fingerprint until verified.
- **Any mutating call that isn't proven by a returned object id must GET-verify (ties B4).** The B4 audit
  already enumerates the PATCH/PUT/POST sites that currently assume success from a 200 (workspace-conf,
  global-init-scripts, permissionassignments, SCIM entitlements/members, ACL PUT, sql/genie/dlt/
  dashboards/serving updates — and now this query-owner PATCH). The rule is uniform: **PATCH → GET →
  compare → report the truth.** This B2 query-owner PATCH is one more entry under that same rule.
- **jobs / pipelines / warehouses:** no change (ACL phase already applies IS_OWNER) — add a regression +
  live assert with a **distinct** owner (below) to prove it, since Run 5's owner ≈ the operator.
- **Dashboards / Genie / clusters:** owner is **not settable** — document the limitation in the
  not-migrated catalog; dashboards/clusters already carry CAN_MANAGE/`OriginalCreator`.
- **Orphaned / left-the-org owner (user decision 2026-10-01):** when the source owner is **absent from
  the target roster** (deleted in source / left the org), set the owner to **the run-as SP** (the
  creator default) — never fail, never set a non-existent principal. This is the SAME rule for every
  settable-owner type (queries, and the IS_OWNER types where the grant's principal is unresolvable), and
  it matches the home-divert behaviour (PLAN 9 sends an orphaned owner's content to `/Users_Backup`).
- Regression tests per type + a live re-validation: a query/job/pipeline/warehouse whose source owner is
  a **DIFFERENT in-roster user than the run-as SP** → assert target owner == source owner (remapped);
  a query whose source owner is **absent** → assert target owner == run-as SP (not a failure).

**LIVE-VERIFIED (source_ws 2026-10-01) — both owner mechanisms actually change the owner (read-back):**
- **Jobs:** `PUT /api/2.0/permissions/jobs/{id}` with `{user_name:<new>, permission_level:"IS_OWNER"}`
  → re-GET shows owner **changed** to `<new>` (then restored). So the ACL-phase `IS_OWNER` grant genuinely
  transfers ownership (jobs/pipelines/warehouses all expose `IS_OWNER` — the mechanism is identical).
- **Queries:** `POST /api/2.0/sql/queries` → owner = caller; `PATCH /api/2.0/sql/queries/{id}` with
  `{query:{owner_user_name:<new>}, update_mask:"owner_user_name"}` → re-GET shows owner **changed**. So
  the post-create owner PATCH works (and must be read-back verified, per the rule above).

---

## B3 (BUG, HIGH) — cluster-policy create fails for policy-FAMILY-based policies (`policy_family_id` + `definition` sent together)

**Found in the customer (the customer) import run 2026-09-08** — report `import_status.xlsx`,
source ws `2182859919784521`, run `20260901_155308`, direct mode. One custom policy
`cust-pl-stg-job-compute-jobs` FAILED; the 5 built-in policies adopted fine.

**Symptom (verbatim).**
```
POST https://centralindia.azuredatabricks.net/api/2.0/policies/clusters/create
  -> 400: INVALID_PARAMETER_VALUE policy_family_id and definition cannot be used together
```

**Root cause.** A policy built on a **policy family** (e.g. the "Job Compute" family) is returned by
`GET/list` carrying BOTH `policy_family_id` (+ `policy_family_definition_overrides`) AND a fully
**resolved `definition`** (the family merged with the overrides). `compute_importer._create_policy`
(`src/importers/compute_importer.py:141-144`) blindly copies **every** present field into the create
body:
```python
for field in ("definition", "description", "libraries", "max_clusters_per_user",
              "policy_family_id", "policy_family_definition_overrides"):
    if payload.get(field) is not None:
        body[field] = payload[field]
```
The create endpoint forbids `policy_family_id` together with `definition` → 400. The docstring even
says *"a policy-FAMILY policy is a different shape"*, but the code never branches on it. `update_one`
(`:112-116`) has the mirror-image risk (it sends `definition` on an edit without checking for a family
policy). A re-run does NOT fix this — it needs the code change.

**Proposed change (when taken up).**
- Make the two shapes **mutually exclusive** in both `_create_policy` and `update_one`:
  - if `policy_family_id` is present → send `policy_family_id` + `policy_family_definition_overrides`
    (+ name/description/libraries/max_clusters_per_user) and **DROP `definition`**;
  - else (custom policy) → send `definition` as today.
- Keep the existing pool-id remap (`_remap_policy_body_ids`) but apply it to
  `policy_family_definition_overrides` for family policies (it already covers that field).
- Add a regression test: a family-based policy unit → assert the create body contains
  `policy_family_id` and NOT `definition`; a custom-definition policy → the reverse.

**LIVE-VERIFIED (source_ws 2026-10-01) — the fix shape is proven, no experimentation needed:**
- Family = **"Job Compute"** (`policy_family_id="job-cluster"`).
- Create with `policy_family_id` + `policy_family_definition_overrides` and **NO `definition`** → **OK**.
- `GET /api/2.0/policies/clusters/get` returns **BOTH** `policy_family_id` **and** a resolved
  `definition` (+ `policy_family_definition_overrides`) → this is exactly why the collector captures both
  and the blind create 400s.
- Create with `policy_family_id` **and** `definition` together → **400 "policy_family_id and definition
  cannot be used together"** (bug reproduced). So the implementer: branch on `policy_family_id` present
  → send family_id + overrides, drop `definition`; else send `definition`. Done — this is settled.

**Note — full customer-run failure taxonomy (2026-09-08, for context; see [[cust-import-run-rca-2026-09-08]]).**
Of 1,592 failures in that run: ~1,566 were uncreated **service-principal home dirs** (SPs filed as
`manual`, never created — the headline identity issue); ~22 were **orphaned/gap user homes** (4 users
absent from the exported roster + 2 legacy queries targeting their `/Drafts`); **3 jobs** failed on
`run_as` binding (2× the migration identity lacked `servicePrincipal.user` on the run-as SP
`cust-sp-stg-job-admin`; 1× the `run_as` user was **inactive** on target); **1** was this policy bug;
**1** was a cluster library needing the jar in the target Volume + a running cluster. Only B3 is a NEW
code fix — the rest are the identity re-run + prerequisites (grant `servicePrincipal.user`, active
users, jar in Volume, `library_force_start_clusters=true`).

---

## B4 (BUG, HIGH) — the report must be the SOURCE OF TRUTH: mutating calls claim success from HTTP 200, not from a verified read-back

**Found in the customer (the customer) run 2026-09-08.** `enableExportNotebook` was `false` on
source; the report says *"workspace_conf enableExportNotebook — Created — set to 'false'"*, but the
**target actually stayed `true`**. The tool sent the correct PATCH (lowercase `"false"`, confirmed by
the note) and got a 200, but the platform did not honour it for that key — and the report still
declared parity.

**Root cause — a general reporting-integrity flaw, not one key.** `misc_importer._set_conf`
(`:308-309`) does `PATCH /api/2.0/workspace-conf {key: value}` and immediately returns
`note=f"{key} set to {value!r}"` — the success message is derived from *the call not raising*, never
from re-reading the value. `_do_declarative` (`base_importer.py:935`) then records it `Created` **and
stores a state fingerprint**, so a later run sees a fingerprint match and **SKIPs** it — the wrong
value is now permanent AND invisible (the tool believes it is done).

**This pattern must be audited across EVERY mutating call, not just conf.** The report is supposed to
be the source of truth; any place that infers "done" from a non-erroring response can lie the same
way — especially **declarative / fire-and-forget** calls where a 200 does not guarantee the state
changed or fully applied:

| Call site | Risk |
|---|---|
| `misc_importer.py:308` workspace-conf PATCH | **CONFIRMED** silent no-op (this bug) |
| `misc_importer.py:142` global-init-scripts PATCH | fire-and-forget |
| `identity_importer.py:646/561` permissionassignments PUT (workspace ADMIN/USER, account-group assign) | 200 ≠ effective grant |
| `identity_importer.py:838` SCIM entitlements/roles PATCH | already best-effort + swallowed → success unverified |
| `identity_importer.py:753` SCIM group-members PATCH | partial add reported as full |
| `acl_importer.py:200` `PUT permissions/{type}/{id}` | ACL parity claimed from 200, not a read-back diff |
| `sql_importer.py:118/122/130`, `genie_importer.py:68`, `dlt_importer.py:45`, `dashboards_importer.py:66`, `serving_importer.py:59` update/PUT | value/owner/config assumed applied |

**Research — DONE (live-verified on target_ws 2026-10-01).** For each key, PATCH→GET→restore:

| Key | PATCH false → readback | Verdict |
|---|---|---|
| `enableExportNotebook` | false (honoured here) | honoured on target_ws; **DROPPED on the customer's ws** (their report) |
| `enableWebTerminal` | **stayed `true`** | **SILENTLY DROPPED** (200, no change) — reproduced live |
| `enableResultsDownloading` | false | honoured |
| `enableDbfsFileBrowser` | false | honoured |
| others | — | honoured when set, varies by workspace |

**The decisive finding: whether a key is silently dropped is WORKSPACE/account-policy-dependent and
unpredictable** — `enableWebTerminal` drops on target_ws, `enableExportNotebook` drops on the customer's
ws, both return 200. So a **static "route these keys through the Settings API" list cannot work** — the
drop set differs per workspace. **Read-back is the ONLY reliable mechanism.** This makes the fix a hard
requirement, not an optimization.

**Proposed change (dev spec).**
- **Read-back verify is MANDATORY for workspace-conf** (and every declarative mutating call whose effect
  isn't proven by a returned object id): after the PATCH, `GET` the key and compare to the intended
  value. On mismatch, record **FAILED / "not applied (observed=`X`, intended=`Y`)"** — never `Created`
  from a bare 200.
- **Do NOT advance the state fingerprint until the value is verified**, so a re-run re-attempts the
  unapplied key instead of skipping it (self-concealing today).
- **Workspace-conf parity table** in the report: source value vs **live target value** vs status, for
  all 9 keys — the operator sees exactly which keys didn't take.
- **Settings-API routing is a SECONDARY enhancement, not the primary fix**: if read-back shows a key
  NEVER honours via `workspace-conf` on a given workspace, try the Settings API for it; but read-back is
  what guarantees the report is truthful regardless.
- Audit the same read-back pattern across the other declarative call sites in the table above
  (global-init-scripts PATCH, permissionassignments PUT, SCIM entitlements/members PATCH, ACL PUT,
  sql/genie/dlt/dashboards/serving update) — each must verify-or-report-not-applied.
- Note the scope limit: only the 9 hard-coded `misc_collector._WS_CONF_KEYS` are collected; document
  that any other workspace-conf setting is not migrated (widen later if a customer needs it).
- Report vocabulary: "sent but not verified applied" and a hard API error both read as **NOT parity**,
  distinct from a clean `Created` (reuse the FAILED status + a precise note).

---

## B5 (FEATURE, MED) — structured per-step logging matching the UC utility (live in the cell, not a file)

**Reported by the user 2026-09-24** (same request being tracked for the UC governance migration
utility, so the two tools stay consistent — an operator reads the same logging style across both).

**What's missing.** The run output is sparse/ad-hoc `print`s. Today's `src/utils/logger.py` is
**file-centric** (it mirrors an `execution_*.log` to the Volume via a local-then-copy dance) — the
OPPOSITE of what's wanted: the operator wants logs **visible live in the notebook cell output**, not in
a separate file. So a stuck or failing run can't be diagnosed from what the cell shows.

**The bar (user directive 2026-09-29).** Match the UC governance-migration utility's **already-shipped**
logging **exactly** — same helper, same look, same guarantees — so an operator reads one style across
both tools. Concretely:
1. **Every step announces itself, live in the cell:** inventory / export / import each print a phase
   header, then per-object lines, then a completion line — e.g. *"> Importing users"* → *"user1 →
   created"*, *"user2 → created"* → *"users complete: 2 ok"* → *"> Importing groups"* → … . The
   operator watches progress happen.
2. **If the job is stuck, the last line shows where** — lines stream live and are flushed per record,
   so the final printed line is exactly the step it hung on.
3. **If the job fails, the logs alone pinpoint it** — every failure logs the object, the classified
   error code, and the raw server message (via `log.exception(...)`/`exc_info=True`, never a bare
   `print(str(exc))`).
4. **Cell output, not a file** — the log file / capture buffer is a *secondary* artifact; the primary
   sink is the running notebook cell.
5. **Dense, per-step coverage so "where are we / where is it stuck" is ALWAYS answerable** (user
   directive 2026-10-01 — the real intent). Every meaningful **step of the code** logs — not just phase
   boundaries and per-object results, but the intermediate steps *within* a phase/importer/collector
   (e.g. "resolving warehouse_id", "reading content_ref", "PUT permissions", "state flush") at INFO or
   DEBUG, and **every worker thread** logs its own per-item progress (B6). The bar: at any instant the
   **last line in the cell names the exact step in flight**, so a stuck job is located instantly and a
   failing job is diagnosable from the log alone — no silent multi-second gaps during normal operation.

**Port the UC utility's `logging_util.py` verbatim in design** (rename the app logger `uc_sync → wsmig`;
its code is the reference — `uc_migration/src/uc_sync/logging_util.py`):
- **Named app logger + per-module child loggers** via `get_log(__name__)` (`getChild`), so every line
  carries the emitting module in `%(name)s`. Format: `%(asctime)s %(levelname)-7s [%(name)s]
  run=%(run_id)s stage=%(stage)s | %(message)s` — greppable, with **run_id + stage on every line**.
- **`_LiveStdoutHandler` — the crux for cell visibility.** Databricks swaps `sys.stdout` per command
  cell; a normal `StreamHandler(sys.stdout)` binds the stdout of the cell where `configure_logging` ran
  (an *earlier* cell), so the working cell looks **silent** for the whole run. The handler must resolve
  `sys.stdout` **per record at emit time** (and `flush()` each line) so lines land in the
  currently-running cell and stream live. This is what makes #1/#2 work — copy it as-is.
- **`configure_logging`** once per run: idempotent (replaces only its own handlers), `propagate = False`
  (Databricks' root logger already has a handler → otherwise every line prints twice), **default level
  `DEBUG`** so a run shows everything (per-object INFO + the API call/SQL at DEBUG) with no opt-in —
  the user explicitly wants all the debug detail visible. Optional `StringIO` capture buffer returned so
  the notebook can also write the full run log alongside the report on the Volume (secondary).
- **`_ContextFilter`** injects `run_id`/`stage` from a mutable module context, so `get_log(__name__)`
  works at import time (before the run id exists) and `set_context(run_id=…, stage=…)` fills it in later.
- **`_RedactFilter` + `register_secret`** scrub the direct-mode SP secret (and any registered value)
  from every emitted line (ties to the existing redaction rule).

**The two Databricks cell gotchas — both must be handled (the UC utility hit and fixed them):**
- **Live-stdout binding** (above): a configure-time-bound `StreamHandler` writes to the wrong cell →
  the `_LiveStdoutHandler` per-record resolution is the fix.
- **`dbutils.notebook.exit()` in the same cell as the work SUPPRESSES that cell's streamed stdout in a
  job run** — on a clean success only the exit value renders, hiding every per-object line. Rule
  (verbatim from UC `03_Import.py`): keep the work + its logs in cells ABOVE, and put
  `dbutils.notebook.exit(...)` **alone in a trailing cell**. This project's notebooks (01/02/04) don't
  call `notebook.exit` today, so apply this proactively if/when an exit payload is added (e.g. for the
  packaged multi-task jobs).

**Wiring / scope.**
- New `src/utils/logger.py` (or a sibling) built on the UC design; **absorb/replace** the current
  file-centric logger — keep its local-then-copy Volume mirror ONLY as the optional secondary capture
  sink, not the primary path.
- `get_log(__name__)` used by the notebooks (01/02/04) and every `src/` module (collectors, importers,
  exporters, state, reports). Levels: **INFO** per-object create/skip/update with target name + phase
  start/end counts + delta decision on incremental; **WARNING** for recoverable/degraded (retry/backoff
  fired, best-effort write skipped, unverified declarative write — see B4); **ERROR** for every failure
  with object + code + raw server message; **DEBUG** for the underlying API call. **No silent
  `except: pass`** — every caught exception logs at WARNING/ERROR.
- Widget `log_level` (default **DEBUG**, matching UC) to raise the floor if ever needed.

**Thread-safety — foolproof under B6 parallelism (user directive 2026-10-01).** Logging must work
correctly when per-object work runs on `ThreadPoolExecutor` workers:
- **No corruption:** stdlib `logging` handlers are internally lock-guarded, so concurrent `emit` from
  many worker threads serializes — lines never interleave mid-line. Workers log through the SAME
  `get_log(__name__)` app logger; no per-thread logger setup.
- **Worker lines must still reach the running cell.** Databricks can redirect `sys.stdout`
  **thread-locally**, so a worker thread's `sys.stdout` may NOT be the cell's — re-resolving
  `sys.stdout` inside a worker could miss the cell. Fix: when a parallel section starts, **capture the
  cell's stdout on the MAIN thread and pin the live handler to that captured stream** for the section's
  duration (workers write to the real cell stream, serialized by the handler lock); the main thread
  keeps per-record resolution. Verify live that worker logs appear in the cell before relying on it.
- **Attributable interleaving:** every line already carries `run_id`+`stage`+`[module]`; add the
  **object identity** (and, where useful, a short worker/thread tag) to each per-object line so
  interleaved parallel output is still unambiguous (`user42 → created` not just `created`).
- **No worker swallows an error:** a worker logs **ERROR with `exc_info=True`** before handing the error
  back to `parallel_map` (which yields `(item, result, error)`); the main thread never loses a worker's
  failure. (Ties B6's "no silent failures".)

**Acceptance.**
- Watching a live run, the operator sees each phase and per-object line stream **in the running cell**
  (inventory/export/import): *"> Importing users" → "user1 → created" → … → "users complete: N ok"*.
- A **stuck** run's last cell line names the exact step it hung on.
- A **deliberately broken** run is diagnosable from the cell output alone (no workspace access needed);
  every `except` logs; format is consistent + greppable; `run_id`+stage on every line; secrets redacted.
- No double-printed lines (`propagate=False`); a `dbutils.notebook.exit` (if added) does not swallow the
  run's logs (exit isolated in its own trailing cell).

Related: mirrors the UC governance migration utility's **shipped** logging (`logging_util.py` +
`03_Import.py` exit-cell pattern). Keep the two implementations' format + helper API aligned.

---

## B6 (FEATURE, MED) — bounded thread-pool parallelism (inventory/export enrichment + super-safe import parallelism)

**Reported by the user 2026-09-24** (mirrors the UC governance migration utility's `plans/backlog.md`
Item 3 — keep the two tools' parallelism model + `parallel_threads` knob aligned).

**Current state (verified in code).** Retries + exponential backoff already exist and are **universal**;
parallelism is almost entirely **absent**:
- **Retry/backoff — DONE, do not rebuild.** `src/utils/retry.py::with_retry` retries on
  429/500/502/503/504 with exponential backoff `min(backoff·2^attempt, max_sleep)` (base 1s, cap 30s,
  5 attempts), honouring `Retry-After`. It wraps the single `ApiClient._request` in
  `src/auth/token_manager.py` that **every** REST call funnels through (`get`/`post`/`download_bytes`
  and the OAuth token fetch), so all three stages inherit it. The retry loop is per-call and stateless
  → inherently thread-safe. **No new retry code is needed; parallelism only bounds concurrency.**
- **Parallelism — one site only.** `src/exporters/parallel.py::parallel_map` (bounded
  `ThreadPoolExecutor`, fail-soft per task, completion-order yield, with a `Locked` helper) is used at
  exactly **one** place: `export_runner.py:236`, the notebook/workspace-file **content byte download**,
  gated by `content_fetch_workers` (default 8). Everything else is serial:
  - **Inventory** collectors (`src/collectors/base_collector.py::enrich`, the `for o in objects:` loop)
    — serial. This is the customer-scale hotspot: ~852K serial per-object **ACL enrichment** calls ran
    ~11h at RRL scale (see [[PLAN_12_optional_scale_and_durability]] — already half-scoped there).
  - **Export** metadata listing/serialization — serial (only content bytes are threaded).
  - **Import** — fully serial: `base_importer.run()` does `for unit in units:`, one unit at a time,
    within each phase; phases run in strict dependency order (`phases.py::PHASE_ORDER`).

### The model (confirmed with the user 2026-09-24)
Parallelize the object processing **within a dependency sub-level**, with a **barrier between
sub-levels**, reusing the existing per-call retry. The barrier is not an optimization choice — it is
**required** by the dependency graph. Same primitive as UC-sync Item 3 (parallel within a level,
sequential across levels), extended to two scopes:

**Scope 1 — inventory + export enrichment (do first; safest, biggest win).** The enrichment/listing
calls are **read-only and dependency-free** → embarrassingly parallel, no ordering hazards. Fan the
per-object enrichment loop (esp. ACL enrichment) out through `parallel_map` with a bounded pool. This
is the ~11h → ~1-2h win and carries none of the import caveats. (Consolidate with PLAN_12's ACL-enrich
item — same work.)

**Scope 2 — super-safe import parallelism (do second).** Parallelize *within* a sub-level only, with a
hard barrier before the next. Critically, **"within a family" is NOT uniformly independent** — several
families decompose into ordered sub-levels that each need their own barrier. The abstraction is
*"parallelize a set of mutually-independent units → barrier → next set,"* not *"one pool per family."*
Concrete sub-levels in this tool:
- **identity:** parallel **users** → barrier → parallel **SPs** → barrier → **groups** (nested-first,
  so groups is itself sub-ordered by nesting depth) → barrier → **membership two-pass** (must run
  after all groups exist — the "1/3 members added" bug came from patching membership too early).
- **compute:** parallel **pools** → barrier → parallel **policies** → barrier → parallel **clusters**
  (clusters reference both).
- **workspace:** **directory tree first** (parents before children) → then parallel notebooks/files
  within an existing dir.
- Independent families (jobs, dlt, dashboards, genie, serving, misc) can parallelize their units
  freely, but the **cross-family `PHASE_ORDER` barriers stay** (identity → compute → workspace →
  secrets → sql → dlt → jobs → dashboards → genie → serving → misc → **acls last**).

### Thread-safety work this requires (the real cost — parallelism itself is the easy part)
1. **Shared identity maps** (`sp_mapping`, `group_map`, the displayName index PASS-1 builds for PASS-2)
   — guard with a lock or accumulate per-worker and merge at the barrier.
2. **In-memory results + checkpoint** — result accumulation and the Delta **state store** upserts must
   be thread-safe; the state store already **batches + flushes at phase boundaries**, which lines up
   exactly with a barrier (flush at each barrier). Assign any deterministic ordinal AFTER the sub-level
   completes, so results/state are stable regardless of completion order (see UC-sync Item 3's
   `import_order` hazard).
3. **Client sharing** — confirm the `ApiClient`/`requests.Session` is safe to share across threads (the
   Session is connection-pooled → concurrent requests are fine; don't mutate its config concurrently),
   or give each worker its own — decide in the plan, not at impl time (this bit UC-sync Item 3 too).
4. **Rate-limit amplification / thundering herd** — more workers = more 429s; the backoff absorbs
   bursts but an unbounded per-sub-level pool can outrun it. Keep a **sane cap** (start at the export
   default of 8) and prefer a **global concurrency budget** over unbounded per-sub-level pools.
5. **Intra-sub-level independence must actually hold** — the sub-levels above are chosen so it does;
   if any residual same-level dependency appears (mirrors UC-sync's view-on-view / function-on-function
   risk), add a **failed-set retry pass within the sub-level** rather than assuming independence.

### No silent failures (user directive 2026-09-24) — carry through under threads
This tool's existing contract is *"failures never stop the pipeline"* — keep it, and make it loud:
- **Fail-soft, never fail the job:** one unit's failure must not abort the pool or the run
  (`parallel_map` already yields `(item, result, error)` per task — map over that, don't let an
  exception escape the pool).
- **Every error is surfaced twice:** logged at **ERROR** with full context (object, raw exception,
  classified error code, consequence — see **B5**) **and** written as a FAILED row in the import Excel.
  No silent `except: pass`; a swallowed/degraded path is at least a WARNING.
- **The report stays the source of truth** (see B4): a unit that errored under a worker is FAILED in
  the report, and its state fingerprint is NOT advanced, so `retry_mode=failed_only` re-attempts it.
- Concurrent completion interleaves log lines → this depends on B5's per-object-tagged, greppable log
  format to stay diagnosable. Sequence B6 with/after B5.

### Design knobs
- **One widget: `parallel_threads`** (install-jobs + notebooks), default small (e.g. 8 to match
  `content_fetch_workers`; consider unifying the two). `parallel_threads=1` reproduces today's exact
  sequential behaviour — the safe fallback / kill-switch.
- Barrier between every dependency sub-level (above); cross-family `PHASE_ORDER` barriers unchanged.
- No change to retry/backoff — each worker inherits the per-call 429/5xx backoff; the pool size only
  bounds concurrency.

### Acceptance
- **Inventory/export:** a large-workspace enrichment runs materially faster than serial with an
  **identical bundle** (same objects, same ACLs, byte-identical manifest modulo ordering); results are
  order-independent.
- **Import:** level-grouping preserves `PHASE_ORDER` + the intra-family sub-level order; a forced-slow
  unit in a sub-level does not reorder results or state; shared maps (`sp_mapping`/`group_map`),
  results, and checkpoint are correct under a forced-parallel test (many small units) with no
  lost/duplicated rows; membership two-pass still reaches parity.
- **Fault injection:** a retryable 429/5xx on one worker is ridden out by the existing backoff without
  failing the sub-level; a deterministic error fails only its own unit, is logged ERROR, appears as a
  FAILED row in the Excel, and does NOT advance state (so `retry_mode=failed_only` re-attempts it).
- **`parallel_threads=1` reproduces today's sequential behaviour exactly.** Full suite green.

**Sequencing.** Do **Scope 1 (inventory/export enrichment) first** — safe, dependency-free, biggest
customer-scale win, and folds in the PLAN_12 ACL-enrich item. Do **Scope 2 (import) after B5** so the
per-object logging is in place and the concurrent run stays diagnosable.

---

## B7 (GAP, HIGH) — AI/BI (Lakeview) dashboards migrate as DRAFT ONLY; publish state / schedule / owner / credentials are not preserved

**Reported by the user 2026-09-29 — flagged super-critical.** A source **published** dashboard must
land **published** on target; a source **draft** stays draft. Owner, schedule(s), permissions, and the
publisher/viewer-credentials mode must all match source. This is a parity gap that reads as **broken
output to end users**: consumers open a migrated dashboard on target and see nothing, because only the
editable draft came over.

**What's true today (verified in code).**
- **Only the draft migrates.** `dashboards_collector.py:38-49` captures `serialized_dashboard`,
  `warehouse_id`, path, and ACLs. `dashboards_importer.py` does `POST /api/2.0/lakeview/dashboards`
  (create) / `PATCH .../dashboards/{id}` (update) only. The strings `publish` and `schedule` appear
  **nowhere** in the dashboards collector or importer — publish state, schedules, `embed_credentials`,
  and owner are neither captured nor applied.
- **Consequence:** every migrated AI/BI dashboard is **draft-only** on target regardless of its source
  state; a source-published dashboard is **unpublished** on target. On incremental re-runs, a source
  publish / unpublish / republish / schedule change is never detected either (the fingerprint is built
  from the draft payload only — `export_runner._apply_content_fingerprint` folds content for
  notebooks/files, but the dashboard payload is just `serialized_dashboard`+`warehouse_id`).
- **Already works — do NOT re-scope:** dashboard **permissions/ACLs already migrate** via the ACL phase
  (`acl_importer.py:68` maps `dashboards → lakeview_dashboard`). The **owner** is the only permissions
  gap, and it is already tracked as **B2**. So "maintain permissions" ≈ done; "maintain owner" = B2's
  work, cross-linked here.

**Required scope (dev spec — reconciled with the VERIFIED API above).**
1. **Published stays published, draft stays draft.** If source is published, publish on target after
   create/update; if source is draft-only, leave it unpublished.
2. **Owner — NOT settable for dashboards (verified): documented limitation.** Dashboards have no owner
   field and no `IS_OWNER`. The only identity signals are (a) the **CAN_MANAGE grants** (migrated by the
   ACL phase) and (b) the **home** the dashboard lives in (`parent_path`, remapped by the workspace
   phase — incl. the PLAN 9 orphaned-home divert). So there is nothing to "set" — scope #2 is satisfied
   by the ACL phase + home remap, and we note "owner not preservable (API has no owner)" like clusters.
3. **Schedule(s) preserved** — recreate every schedule (`cron_schedule` quartz+tz, `pause_status`,
   `warehouse_id` remapped) + its subscriptions (recipient remapped), via `create_schedule` /
   `create_subscription`.
4. **Permissions preserved — YES, including the PUBLISHED dashboard (verified).** Publishing creates no
   separate permissions object; the published view is governed by the **dashboard's** ACL
   (`object_type=dashboard`), which the ACL phase already migrates (`acl_importer.py:68`). So a published
   dashboard's permissions ARE covered — just verify parity end-to-end for dashboards. No owner gap to
   close (per #2).
5. **Publisher/viewer credentials — preserve the MODE; Shared resolves to the run-as SP (user rule
   2026-10-01, reconciled with the API).** The publish API has **only `embed_credentials` + `warehouse_id`**
   — no field to name an arbitrary SP. So:
   - **Individual** (`embed_credentials=false`, "each viewer uses their own credentials") → target
     **Individual** (`embed_credentials=false`). Preserved exactly. *(= the user's "user credentials
     kept, maintain it".)*
   - **Shared** (`embed_credentials=true`), whether the source embedded a **publisher-user** or **a
     specific SP** → target **Shared** (`embed_credentials=true`), and the embedded credential becomes
     **the run-as SP (this migration tool)** — because `POST /published` embeds the **caller**, and the
     API cannot target an arbitrary principal. *(= the user's "publisher credentials → in our case the
     SPN running this tool"; and the SP case collapses to the same because the API can't set a third SP.)*
   - **Why this is correct for the customer (user rationale):** their SPNs are Azure-UMI-backed in a
     subscription that is **deleted after migration**, so retaining the old SP as the credential is
     wrong anyway; the migration run-as SP already carries the needed UC privileges (it runs the UC
     migration too). So "Shared → run-as SP" is the desired end state, not a compromise.
   - **The one thing the API cannot do:** set the embedded credential to a **third, specific SP** that
     isn't the run-as SP. If a future customer needs that, it's a **manual re-publish as that SP**
     (documented) — out of scope here. So we capture ONE bit from source — `embed_credentials` (Shared
     vs Individual) — and nothing else; no credential-principal capture/remap is needed.

**API surface — VERIFIED LIVE (source_ws, 2026-10-01; SDK `databricks.sdk.service.dashboards`).**
- **Publish:** `GET/POST/DELETE /api/2.0/lakeview/dashboards/{id}/published`. The body is **ONLY
  `{embed_credentials: bool, warehouse_id: str}`** — `PublishedDashboard` =
  `{display_name, embed_credentials, warehouse_id, revision_create_time}`. **There is NO
  credential-principal / SP / run_as field** — `embed_credentials=true` embeds the **caller's**
  (publisher's) credentials. `GET` 404s when not published.
- **Schedules:** `create_schedule(dashboard_id, Schedule)` where `Schedule =
  {cron_schedule: {quartz_cron_expression, timezone_id}, display_name, pause_status, warehouse_id}`;
  subscriptions via `create_subscription(dashboard_id, schedule_id, Subscription)` where
  `Subscription = {subscriber (destination_id OR service-principal), skip_notify}`.
- **Permissions / owner:** dashboard permissionLevels are **`CAN_READ, CAN_RUN, CAN_EDIT, CAN_MANAGE` —
  NO `IS_OWNER`**; the dashboard object has **no owner field**. The permissions object is on the
  **dashboard** (`object_type=dashboard`, `/api/2.0/permissions/dashboards/{id}`); **publishing does NOT
  create a separate permissions object** — the published view is governed by the dashboard's ACL.

**Proposed change (dev spec).**
- **Collector:** capture `is_published` + published `warehouse_id` + `embed_credentials` (via
  `get_published`, treating 404 as draft-only); the dashboard's **schedules + subscriptions**. **No
  owner** (dashboards have none) and **no credential-principal** (the API has no such field) — just the
  `embed_credentials` bit.
- **Importer:** after create/update, add a **publish + schedule reconcile** step:
  - **Publish** — compare source `embed_credentials`/`warehouse_id` vs the target's `get_published`;
    if source published and target differs (not published, or different `embed_credentials`/warehouse),
    `publish(embed_credentials=<src>, warehouse_id=require_remap("sql_warehouse", src_wh))` **as the
    run-as SP** (so Shared embeds the run-as SP, per scope #5; warehouse is exact-or-fail like everywhere
    else). **If source is now draft-only but target is published → FLAG ONLY, never unpublish target
    (user decision 2026-10-01).** The tool records a drift row ("published on source → unpublished;
    target still published — manual unpublish if desired") and does **NOT** call `delete /published`. This
    is non-destructive by default, consistent with `allow_deletes=false` and the F3 "treat as deleted,
    don't delete" stance — a target-side unpublish is never automated in v1. **Idempotent:** only
    (re)publish on an actual diff, so a re-run mints no new revision.
  - **Schedules** — recreate each source schedule (`warehouse_id` via `require_remap` exact-or-fail;
    subscription recipient via the identity maps); reconcile changes on re-runs.
- **Fingerprint/state:** fold `embed_credentials` + published-`warehouse_id` + the schedule set into the
  dashboard fingerprint (separate facets, UC-sync-Item-8 style) so a source publish/unpublish/schedule
  change is change-detected on incremental runs.
- **Read-back, not fire-and-forget (ties to B4):** confirm via `get_published` after publishing; record
  FAILED (observed vs intended) rather than declaring parity from the call not raising; don't advance
  the fingerprint until verified.

**LIVE-VERIFIED (source_ws 2026-10-01) — settles the key behaviors:**
- **Publish is NOT idempotent — the diff-guard is MANDATORY.** Publishing twice with identical
  `{embed_credentials, warehouse_id}` **minted a new revision** (`revision_create_time` changed between
  calls). So the importer MUST compare source vs `get_published` and publish **only on an actual diff**,
  or every re-run churns a new published revision. (This was a design assumption; now proven required.)
- **Schedule create works** — `POST .../schedules` with `{cron_schedule:{quartz_cron_expression,
  timezone_id}, display_name, pause_status, warehouse_id}` returns `schedule_id` + those fields.
- **Unpublish (`DELETE .../published`) + republish with `embed_credentials=false`** both succeed → the
  draft-only and Individual states are reachable on target.

**The one honest limitation — the embedded credential principal.** `publish()` embeds the **caller's**
credentials and exposes no principal field, so on target a **Shared** dashboard always runs as the
**run-as SP** — we cannot reproduce an arbitrary source user or a third source SP as the credential.
Per the user rationale (old UMI-backed SPNs are deleted post-migration; the run-as SP already holds the
UC privileges), this is the **desired** outcome, not a gap. A third-SP credential, if ever needed, is a
manual re-publish as that SP.

**Can we DETECT a third-SP credential and flag it proactively? NO — and here's why (user question
2026-10-01).** The published-dashboard API (`GET /published` / `PublishedDashboard`) returns **only**
`{display_name, embed_credentials, warehouse_id, revision_create_time}` — it does **NOT** expose WHICH
principal is embedded when `embed_credentials=true`. So we **cannot distinguish** "Shared → publisher
user" from "Shared → a specific SP" at the API level; there is nothing to key a targeted flag off.
Therefore we **cannot** flag only the third-SP ones. Decision (user 2026-10-01):
- **Document it once — do NOT stamp a note on every Shared dashboard.** The docs/runbook (and the
  not-migrated catalog) state: *"a Shared-data-permission dashboard is published on target with the
  run-as SP as its embedded credential; if the source used a specific different service principal's
  credential, re-publish manually as that SP."* No per-dashboard report note.
- For THIS customer it won't actually break: the run-as (migration) SP carries the UC privileges, so a
  Shared dashboard running as it works — which is why a one-time doc line is enough, not a per-row flag.

**Warehouse_id (user point 2026-10-01): already universal + exact-or-fail — the NEW surfaces inherit it.**
`require_remap("sql_warehouse", …)` (PLAN 11 F10, exact-or-fail: hard-fail if the source warehouse isn't
in the bundle) is ALREADY applied to the draft dashboard, Genie, jobs (`sql_task`/`dbt_task`), and legacy
queries/alerts/alert_v2 `warehouse_id`. B7's **new** warehouse references — the **published snapshot's
`warehouse_id`** and each **schedule's `warehouse_id`** — MUST use the **same `require_remap`** (exact-
or-fail), so a published/scheduled dashboard never points at a source warehouse id, and the resource
FAILS loudly if source `123` was recreated as `456` but `123` is unresolvable.

### Report / status model (kept simple — user directive 2026-09-29)
No dedicated publish parity sheet. The dashboard stays **one atomic row** on its asset-type tab, using
the existing fixed status vocabulary; the outcome is combined across facets:

| Source state | Target outcome | Row `Import Status` |
|---|---|---|
| Draft only | Draft created | **success** → "Created" (or "Updated"/"Skipped (unchanged)" on re-runs) |
| Draft only | Draft create failed | **"FAILED"** |
| Draft + published | Draft + published both succeed | **success** → "Created"/"Updated" |
| Draft + published | **Any** required facet fails (draft OR publish) | **"FAILED"** |

- **Atomic, no half-success:** a dashboard whose draft imported but whose publish failed is **`FAILED`**,
  not "Created (warning)". Any required facet failing fails the row. (Decision: **schedule + owner
  failures also fail the row**; flip them to non-gating only if the user says so.)
- **`Note / reason`** enumerates every facet — what worked and what failed, e.g. *"draft: created;
  published: FAILED; schedule: created; owner: set"* — and **`Actual server error`** carries the raw
  error of the failing facet.
- **State:** a `FAILED` row does NOT advance the fingerprint, so `retry_mode=failed_only` re-attempts it;
  the already-created draft is adopted (existing target id) on retry, so only the failed facet re-runs —
  no duplicate dashboard.

### Backward compatibility — customers who already ran the utility have DRAFT-ONLY dashboards on target
This heals automatically on the next fixed run — **no duplicates, no manual recreate:**
1. **No duplication.** The target dashboards already exist and are **already adopted in the state
   store** (natural_key → target `dashboard_id`). The fixed run resolves them via
   `existing_keys()`/`folder_existing_keys` → **UPDATE path, never CREATE**.
2. **A fresh export is required** — the *old bundle* never captured publish/schedule/owner, so healing
   cannot come from it. In **direct** mode this is one normal end-to-end re-run; in **airgap** mode,
   re-run the source side (fixed collector) → new bundle → import.
3. **What triggers the heal:** the fixed collector folds publish/schedule/owner into the payload, so the
   fingerprint differs from the stored one → the dashboard is classified **UPDATE** → the reconcile step
   publishes/schedules/sets-owner on the already-existing target dashboard.
4. **Deterministic safety net (mirror UC-sync Item 8 NULL=re-verify):** treat a **missing
   publish/schedule facet in a pre-fix state row as "unknown → must reconcile,"** so healing fires even
   if fingerprint logic misses it. No `uc-sync_state`-style wipe needed.
5. **Idempotency:** reconcile only on an actual source-vs-target diff (`GET .../published`), so a re-run
   over already-healed dashboards makes zero writes and mints no new revision.
6. Optional: a one-shot **"reconcile publish state for all previously-migrated dashboards"** convenience
   over the state table, if we want to heal without waiting for the next natural incremental run.

**Acceptance.**
- Source **published** → target published with the same `embed_credentials` **mode** + remapped
  warehouse; a **Shared** dashboard runs as the **run-as SP** (verified: API embeds the caller); source
  **draft-only** → target left unpublished.
- **Combined status:** draft-only that creates → success ("Created"); draft-only that fails → "FAILED";
  draft+published where both succeed → success; draft+published where **any** required facet fails →
  "FAILED", with the per-facet breakdown in `Note / reason` and the raw error in `Actual server error`.
- Dashboard **schedules** recreated (cron / pause / subscriptions) with remapped warehouse + recipients;
  a source schedule change is reconciled on the next run; a source **unpublish is FLAGGED ONLY — never
  unpublished on target** (user decision 2026-10-01): the report shows the drift, the tool does not call
  `delete /published`.
- **Permissions** (incl. the published view) match source via the ACL phase; **owner** is noted as
  not-settable for dashboards (documented limitation), content under a departed owner's home is
  preserved under `/Users_Backup` (PLAN 9).
- **Backward-compat:** a dashboard migrated by the OLD version (draft-only) is healed to
  published + scheduled on the next fixed inventory→export→import run **without duplication**.
- **Incremental:** publish/schedule changes on source are detected and reconciled; a source **unpublish
  is detected and flagged (drift row), but the target is NOT unpublished**; an unchanged, already-healed
  dashboard produces no writes and no new published revision.
- **Retry:** a dashboard that failed only on publish is re-attempted by `retry_mode=failed_only`, adopts
  the existing draft, and publishes without creating a duplicate.

### Owner left the org — a published dashboard whose owner is gone (user point 2026-10-01)
A dashboard lives under `/Users/<owner>`. If that owner is **absent from the source roster** (left the
org / deleted in source), the workspace phase's **PLAN 9 orphaned-home divert** already sends its draft
(`.lvdash.json`) to `/Users_Backup/<owner>/…`, and the ACL phase attaches its permissions to that
diverted path (`workspace_path_remap`). So the content is preserved, not dropped. Since the dashboard
has **no owner field**, there is nothing further to "transfer" — and because a Shared publish embeds the
**run-as SP** (scope #5), the published credential is already the tool's SP. So this case is **handled**:
divert (PLAN 9) + run-as-SP credential. For asset types that DO have a settable owner (queries, and the
IS_OWNER types), the **B2 orphaned-owner rule** applies — absent owner → set the run-as SP, never fail.

**Research — all verify-first tasks DONE (live, 2026-10-01).** Nothing left to confirm before building:
- publish/schedule/permissions shapes + `embed_credentials` semantics — **verified** (see "API surface").
- credential-principal field — **verified it does not exist**; Shared embeds the caller (→ run-as SP).
- `IS_OWNER` for dashboards — **verified absent** (owner not settable → documented limitation).
- published-vs-draft permissions — **verified** they share the dashboard ACL (already migrated).
- target-side unpublish on a source unpublish — **DECIDED: never auto-unpublish (flag-only), user
  2026-10-01.** A source unpublish is reported as a drift row; the tool does not call `delete /published`
  regardless of `allow_deletes`. (Supersedes the earlier "gated by `allow_deletes=true`" idea.)

---

## B8 (BUG, HIGH) — user-home content fails because homes are PROTECTED + lazily provisioned (we cannot create them): defer + end-of-run re-sweep + heal

**Reported by the user 2026-09-29; root-caused by escalating live tests on source_ws, settled at scale
2026-10-01.** After bulk user provisioning, a migration reports many `/Users/<email>/…` content units as
`prerequisite_missing`, so the operator runs `retry_mode=failed_only` repeatedly.

**Live tests (source_ws) — the small-scale readings were a RACE; the 100-user test is definitive:**
| Test | Result |
|---|---|
| 6 users, mkdir shortly after SCIM create (2026-09-30) | mkdir "OK" ≤10s — **misleading** |
| 1 user srikanth, mkdir immediately then polled (2026-10-01) | mkdir `PROTECTED` until ~29s, then "OK" — **misleading** |
| **100 users, Group A = get-status only (NEVER mkdir) vs Group B = before-mkdir race (2026-10-01)** | **Group B: 200/200 mkdir → `DIRECTORY_PROTECTED` ("Folder Users is protected"); 0 succeeded. Group A: 0/50 homes appeared. And 0/100 homes existed even 8 MINUTES after provisioning.** |

**Definitive conclusion (confirms Glean + the user, and corrects my earlier two readings).**
- **`mkdir` CANNOT create a user home** — `/Users/<user>` is a protected, system-managed folder; every
  attempt returns `DIRECTORY_PROTECTED`. The small-scale "mkdir OK" was the **race** the user predicted
  (lazy provisioning had completed in the gap for those few users; at scale it hasn't → always protected).
- **Homes are created LAZILY on first login / workspace access** — which the tool **cannot trigger** (it
  can't log in as the user), and which does **not** happen from SCIM provisioning alone within any
  usable window at bulk scale (**0/100 after 8 minutes**). There is **no mkdir shortcut and no reliable
  short poll** — the earlier "bounded poll until mkdir works" plan is WRONG (mkdir never works).

So the tool **cannot make a user's home exist**. It can only import home content **once the home has
been lazily provisioned by the platform**, on a timeline the tool does not control.

**Root cause (in code).** `base_importer._home_present` pre-probes `get-status` and
`_resolve_home_target` turns absence into a hard `PrerequisiteMissing` mid-phase, and the miss is
**cached for the whole run** (`_home_present_cache`) — so a home that lazily appears LATER in the same
run is never retried, forcing `failed_only`. (The customer's 1,566 SP-home failures were a *separate*
identity-classifier issue — SPs filed `manual`/never created — not this.)

**Proposed change — the user's design: NEVER mkdir a home; defer + ONE end-of-run re-sweep + heal.**
1. **Never attempt to create a user home** (`mkdir` is impossible — remove any such idea). Drop the old
   `workspace_importer.py:12-14` "home can't be mkdir'd" ambiguity into a crisp "homes are
   platform-created lazily; we never create them".
2. **Attempt home content; on absence, DEFER (don't fail-hard mid-phase).** A home-content unit whose
   home isn't present yet is parked on an in-run **deferred list**, not immediately failed — so it
   doesn't poison the phase and isn't pinned by the cache. **This covers EVERY home-descendant unit, not
   one asset type:** the gate is path-based (`_resolve_home_target` on any target under `/Users/<owner>/`),
   so notebooks, workspace files, sub-directories, dashboard files, and repo content under a user home are
   ALL deferred + re-swept + healed by the same mechanism. The original `prerequisite_missing` symptom hit
   all of these the same way, so fixing it here resolves all of them — there is no per-type special-casing.
3. **One re-sweep at the END of the run** (after all other content + phases — maximising the elapsed time
   lazy provisioning had): re-check each deferred home **fresh** (no cache); if it now exists → import
   the content; if still absent → record a clean `prerequisite_missing`.
4. **Heal on a later run via `failed_only`** — a still-missing home becomes `prerequisite_missing` (as
   today), which `failed_only` picks up on a subsequent run, by which point the user may have accessed
   the workspace and the home exists.
5. **Fix the `_home_present` cache** so a late-appearing home is used by the re-sweep (only cache a
   genuinely-settled "present"; never pin a transient "absent" for a this-run owner).
6. **Keep the true-absent path** for an owner genuinely not created / deleted-in-source → immediate
   `prerequisite_missing` or PLAN 9 backup divert; it does not go on the deferred list.

**Honest limitation (document it — platform constraint, not a tool bug).** Because homes are
platform-lazy and we can't force them, at bulk scale **few or no** homes may provision during a short
run (0/100 in 8 min observed), so the re-sweep is a **partial** recovery: it catches within-run
provisioners; the rest legitimately can't be imported until those users access the workspace, and heal
on a later `failed_only` run. This is the ceiling of what's possible — the fix removes the false
`failed_only` loop (cache) and recovers whatever provisioned during the run, and reports the rest
honestly. (Glean's "shared `/Workspace` location for bulk" is NOT an option — home-path parity matters.)

**Acceptance.**
- No home-content unit is pinned `prerequisite_missing` by a **stale cache**: a home that appears later
  in the same run IS imported by the end-of-run re-sweep (no manual `failed_only` for within-run
  provisioners). This holds for **all** home-descendant content (notebooks, workspace files, directories,
  dashboard files, repo content) — the one path-based mechanism fixes every type that was failing.
- The tool **never** calls `mkdirs` on a `/Users/<user>` home (no `DIRECTORY_PROTECTED` noise).
- A home still absent at the re-sweep is a clean `prerequisite_missing` that `failed_only` heals later.
- Regression: an owner genuinely absent (not created / deleted-in-source) → immediate
  `prerequisite_missing` or PLAN 9 backup (not deferred, not re-swept).
- The platform constraint (homes lazy-created on first access; not forceable) is documented in the
  runbook + not-migrated catalog.

---

## B9 (FEATURE, MED) — default `run_id` to the JOB RUN ID (repair→same dir, new run→new dir); actionable manifest-missing error

**Reported by the user 2026-09-28/29; framing corrected 2026-09-30.**

**What's already true (the operator does NOT type a run_id on the happy path).** In an end-to-end job,
`01_Inventory` mints the run_id and **publishes it as a taskValue** (`01_Inventory.py:128`), and
`02_Export` + `04_Import` read it back (`taskValues.get(taskKey="inventory", key="run_id")` —
`02_Export.py:106`, `04_Import.py:159`). So within a single job run all three tasks share ONE run_id
automatically — nobody enters anything. **This item is NOT about forcing the operator to type a run_id.**

**The real problems (two).**
1. **The auto-managed value is an opaque TIMESTAMP with no mapping to the Jobs UI.** A blank widget
   mints `YYYYMMDD_HHMMSS` (`config_manager.py:382` → `run_id = w("run_id") or now_compact()`; the tool
   never uses the platform job run id anywhere — verified by grep). The Jobs UI identifies runs by
   **job run id** (e.g. `#4471`); the volume dir is a timestamp. There is **no way to correlate** the UI
   run to the directory → operators can't find the bundle or the report, or know which run to retry.
2. **taskValues sharing holds only WITHIN one job run.** A **standalone `retry_mode=failed_only`** (just
   the import, not the full end-to-end job) has no upstream `inventory` task → `taskValues.get` returns
   the empty `debugValue` → import falls back to `resolve_import_run_id` (resume-incomplete →
   LATEST_EXPORT), which can resolve the **wrong** dir; and the operator can't pass the right one because
   the timestamp isn't shown anywhere. Pointing at an incomplete-export dir then fails with a bare "could
   not find manifest.json".

**The fix keeps it auto — it just changes the auto value from a timestamp to the job run id**, so the
dir becomes correlatable and repair-/retry-stable (details below).

**Where manifest.json fits (so the error makes sense).** `misc/manifest.json` is written by export as
its LAST step (`export_runner.py:129-131` — its presence marks the bundle complete), and
`LATEST_EXPORT.json` (wsmig root) is written AFTER it. A run that failed before export finished leaves a
dir with **no manifest**, so import preflight's `verify_manifest()` (`artifact_writer.py:189-191`)
returns manifest=None → NO-GO. "Could not find manifest.json" therefore means *you pointed at a dir
whose export never completed*, not a code bug.

**The intended semantics (confirmed with the user 2026-09-30).** The bundle directory is named by the
run id, and the run id defaults to the **job run id**:
- **First run of a job** → new job run id → **new** `<staging>/wsmig/<ws>/<run_id>/` directory.
- **Repair run** (Databricks "Repair run" re-runs the failed tasks under the **same job run id**) →
  **same** `run_id` → reports and all files land in the **existing** run-id directory (continues it).
- **`retry_mode=failed_only` with an explicit `run_id`** → that existing run-id directory is used; its
  reports are (re)written in place there.
Reports already live under `<run_dir>/reports/`, so once `run_id` = job run id the report location
follows automatically — a repair/retry writes back into the same run dir with no extra logic.

**Proposed change.**
- **Source `run_id` from the job run id.** Set each job task's `run_id` `base_parameters` value to the
  dynamic reference **`{{job.run_id}}`** in the shipped `jobs/*.job.json` (today it is `""` — verified),
  and have `00_Install_Jobs` project it (instead of a blank/timestamp). At launch every task in a
  multi-task job receives the SAME parent job run id; a **repair run re-substitutes the same
  `{{job.run_id}}`** → same dir. The notebook's existing precedence treats this as an explicit `run_id`
  (widget value), so no notebook logic changes. **This SUPERSEDES the inventory-timestamp +
  taskValues-sharing mechanism** — still fully auto (the operator types nothing), but every task now
  derives the SAME id directly from the platform rather than relying on inventory to mint + publish it,
  so it also survives a standalone retry (which has no upstream `inventory` task). Keep the taskValues
  publish as a harmless fallback.
- **Interactive / non-job fallback** (no job context): keep the timestamp mint (or the resume-incomplete
  path) as today, documented — the job-run-id default applies when running as a job.
- **Explicit widget `run_id` still wins** (deliberate control / re-import a specific bundle), unchanged.
- **Actionable manifest-missing error:** when a resolved `run_id` has no `manifest.json`, replace "could
  not find manifest.json" with: *"bundle `<run_id>` has no manifest — its export did not complete. Use
  the last COMPLETED export `<LATEST_EXPORT run_id>`, or re-run export."* Point straight at
  `LATEST_EXPORT.json`.

**LIVE-VERIFIED (source_ws serverless run 2026-10-01).** A one-time run with
`base_parameters={"run_id":"{{job.run_id}}"}` had the notebook receive **`run_id` = the job run id**
(submitted run id `396325515157188`, notebook read `396325515157188`, exact match). So `{{job.run_id}}`
resolves into `base_parameters` as intended → the bundle dir name = the Jobs-UI run id. (Repair-run keeps
the same job run id, and a fresh trigger / standalone run gets a new one — platform guarantees; a repair
therefore reuses the same dir, a new run gets a new dir, confirming the design.)

**Acceptance.**
- Running any shipped job names the bundle dir with the **job run id**; the value shown in the Jobs UI is
  exactly what an operator passes to `retry_mode=failed_only`.
- A **repair run** of a failed job writes into the **same** run-id dir (reports refreshed in place), not
  a new timestamp dir; a **fresh trigger** creates a **new** run-id dir.
- `retry_mode=failed_only <run_id>` writes its reports into that existing run-id dir.
- Pointing import at an incomplete-export dir yields the actionable message naming `LATEST_EXPORT`, not a
  bare manifest-missing error.
- Interactive (non-job) runs behave as today (timestamp / resume); an explicit widget `run_id` still wins.

**The directory-sharing rule (confirmed with the user 2026-09-30 — the model to implement to).** *The
run id IS the directory; whatever shares that run id shares the directory.*
- **One run id → one directory** (`<staging>/wsmig/<ws>/<run_id>/`), always.
- **Repair run** re-runs the failed tasks under the **same** job run id → **same directory**; the
  re-run tasks refresh their reports in place (canonical `import_status.xlsx`). Automatic — no input.
- **Standalone `retry_mode=failed_only`** shares the directory **because the operator passes the
  original run id** (a standalone import is its OWN job run, so `{{job.run_id}}` would otherwise mint a
  new dir). Its report is written as `import_status_retry_<ts>.xlsx` into that **same** dir and does NOT
  clobber the canonical full-run report — each retry keeps its own timestamped set (verified:
  `bundle_paths.with_variant`, `import_report.py:127-129`).
- **New run of the job** → **new** job run id → **new directory**.

So a repair shares the dir automatically; a standalone retry shares it because it's handed that run id.
The report-naming question is therefore already settled in code (canonical stays put; a retry adds
`_retry_<ts>` alongside it) — no new decision needed.

---

## B10 (BUG, MED) — ACL phase attempts `.db_internal` platform-internal dirs → permanent 403s that never clear under `failed_only`

**Reported by the user 2026-09-30** from a customer run (`jioindiawest`, report `import_statusv1.xlsx`).
The run identity `16e6c390-…` is a **workspace admin**, yet 12 ACL rows failed:
```
PUT https://jioindiawest.azuredatabricks.net/api/2.0/permissions/directories/1903008081923171
  -> 403: PERMISSION_DENIED 16e6c390-… does not have Manage permissions on
     /Users/prayank.chandorkar@ril.com/.db_internal. Please contact the owner or an administrator.
```
Scope confirmed from the report: **all 12 `permission_denied` rows are `.db_internal`; there are no
other 403s anywhere.** So this is one benign class, not a broad permissions problem.

**Why a workspace admin gets 403 "on a directory".** `.db_internal` is **not a normal directory** — it
is a **Databricks platform-internal / system folder** (internal notebook/IDE state), auto-created by the
platform under each user's home. Databricks **locks its permissions**: **no principal — not even a
workspace admin — has Manage on it.** Workspace-admin ≠ Manage on platform-owned objects. So the 403 is
the platform correctly refusing; the defect is that the tool **attempts it at all**.

**Root cause (in code).** The tool already knows `.db_internal` is platform-internal —
`workspace_importer.is_skippable_path()` matches `_INTERNAL_SEGMENTS = ("/.db_internal", "/.ide",
"/.databricks")`, and the **workspace importer skips creating** these. But the ACL path doesn't apply
the same guard:
- **The workspace collector still captured these dirs' ACLs** into `acls.json` (it walks the tree and
  calls `fetch_acl` for every directory, `.db_internal` included).
- **The ACL importer only skips `_IMMUTABLE_PATHS = ("/Shared",)`** (`acl_importer.py:53,379-381`).
  `.db_internal` isn't in that list, so it resolves the target directory id (it EXISTS on target because
  the platform auto-created `.db_internal` under the provisioned homes) and issues the `PUT` → 403.

**Why it matters (retry-integrity, ties to B4).** These aren't retry-caused — they fail on any run — but
they're classified `permission_denied` (a **failed/retryable** category), so `retry_mode=failed_only`
**re-attempts them forever and they never go green**, permanently polluting the "did the migration
succeed?" signal and blocking a clean sign-off. They should be *skipped / not-supported*, like `/Shared`.

**Proposed change (belt-and-suspenders, both sides).**
1. **ACL importer:** in `create_one`, extend the skip beyond `/Shared` to also skip
   `is_skippable_path(object_key)` for `directories`/`notebooks`/`files` → raise `SkippedNoObject` with a
   "platform-owned; ACL is fixed by the platform on both sides, not a parity gap" note (the `/Shared`
   treatment, category `CAT_NOT_SUPPORTED`). Heals existing bundles too. Note `.db_internal` is
   **per-user**, so it can't be a fixed literal in `_IMMUTABLE_PATHS` — it must use the segment-based
   `is_skippable_path` (which matches any `/.db_internal`, `/.ide`, `/.databricks` segment).
2. **Workspace collector:** don't `fetch_acl` for `.db_internal`/`.ide`/`.databricks` in the first place
   (guard with `is_skippable_path`), so they never enter `acls.json` — cleaner, and it trims the
   ACL-enrichment volume (ties to the PLAN_12 / B6 ACL-enrich cost).

**Acceptance.**
- A run where user homes contain `.db_internal` produces **no `permission_denied` failures** for those
  dirs; they appear as clean "skipped — platform-owned" rows (or not at all, once dropped at collection).
- `retry_mode=failed_only` no longer re-attempts them (they are not a failure category).
- Regression: a genuine directory-ACL failure on a NON-internal path still reports as a real failure.

---

## B11 (BUG, MED) — import report-write failure is SWALLOWED → a "successful" run can leave no `import_status.xlsx`

**Found while diagnosing the user's missing-import-report incident 2026-09-30.** A customer ran the
utility, the job reported success and printed everything correctly, but the run's `reports/` folder held
only the inventory + export xlsx — **no `import_status.xlsx`**.

**Root cause (in code).** `ImportRunner.run()` (`src/importers/import_runner.py:237-246`) writes the
reports in a `finally` block wrapped in a try/except that only logs a WARNING:
```python
finally:
    if self.state is not None:
        self.state.flush()
    summary.update(self._summarize(t0))
    try:
        self._write_reports(summary)
    except Exception as exc:  # noqa: BLE001 — reporting must not mask the real error
        _LOG.warning("report writing failed", error=str(exc))
```
So if `_write_reports` (→ `write_import_reports`, the xlsx/json/manual-actions writer) fails — e.g. a
FUSE/Volume write hiccup under the customer's networking (cf. PLAN_12 Finding-6), or any exception in
report assembly — **the run still returns success, the notebook cells still print, and the import report
silently never lands.** The intent of the swallow was "reporting must not mask the real error" on an
already-aborting run; the side effect is that on a *clean* run a report-write failure is invisible.

**Note the interaction with the missing-report incident.** The other way the import report "goes
missing" is the run_id split in **B9** — a standalone retry / re-run resolving to a *different* dir than
the operator is looking at. B11 is the second, independent way: the write genuinely failed and nobody
was told. Both must be closed to trust "the report is the source of truth" (B4).

**Proposed change.**
- **Make a report-write failure LOUD**: on a clean run, a failed `_write_reports` must (a) **log at
  ERROR** with the exception (traceback) and which artifact + path failed, (b) surface in the notebook
  and set a non-clean `run_status` (e.g. `completed_no_report`), and (c) **NOT** let the run present as
  fully successful. Keep the swallow ONLY on the already-aborting path (re-raising the original error
  matters more there), and even there log at ERROR, not WARNING.
- *(The broader "see every step / where the job is stuck" logging the user asked for is **B5** —
  pervasive per-step INFO/DEBUG/ERROR on every thread. B11 is only about not hiding a report-write
  failure behind a clean-looking run.)*
- **Verify the report actually landed** (ties B4): after writing, confirm the xlsx exists + is non-empty
  at its `reports/` path (post-write size check, consistent with PLAN_12's local-first + verify idea),
  and report `completed_no_report` if not.
- Consider writing the report **local-first then copy** with a synchronous commit (same pattern the
  xlsx path already uses) so a FUSE flush stall can't lose it — and if the copy fails, fail loud.

**Acceptance.**
- A forced `_write_reports` failure on an otherwise-successful run does NOT print as a clean success — it
  is logged at ERROR, surfaced in the notebook, and the run status reflects "import ran but the report
  was not written."
- When report writing succeeds, behaviour is unchanged.
- Regression: an already-aborting run still re-raises its ORIGINAL error (report failure never masks it).

---

## B12 (FEATURE, LOW) — group widget labels logically (all Source widgets together, etc.) — match the UC utility convention

**Reported by the user 2026-10-01.** The notebooks' widgets appear in an arbitrary order in the
Databricks widget bar, so related inputs are scattered — the operator hunts for "which widgets are the
source ones?". The UC governance-migration utility already solved this (merged to its `main`); adopt the
**same convention** so an operator sees the same grouped layout across both tools.

**The convention (verified in the UC codebase, `notebooks/*.py`).** Databricks renders widgets **sorted
by their label text**, so the group+order is encoded in the label as a prefix:
```
"<group-number><order-letter>. <Group> · <description>"
```
Examples straight from UC: `1a. Source · Connectivity mode`, `1b. Source · Workspace URL (blank =
current)`, `2a. Scope · Catalogs`, `3a. Output · Output volume path`, `6a. Warehouse · Source`,
`9a. Run · Preflight enforce`. The numeric prefix groups + orders; the `· ` separates the group tag
from the human description. **Same widget NAME (the code key) — only the label string changes**, so no
code that does `dbutils.widgets.get("<name>")` is affected.

**Proposed change.** Relabel every widget in `00_Install_Jobs`, `01_Inventory`, `02_Export`,
`04_Import` to this scheme, with a group taxonomy fit to THIS tool (exact numbering is an impl detail;
the rule is "Source widgets grouped together, logical order"):

| # | Group | Widgets (this tool) |
|---|---|---|
| 0 | **Install** (00 only) | `deploy_jobs`, `run_as_sp`, `source_run_as_spn` (B1), job name prefix, `run_now` |
| 1 | **Source** | `connectivity_mode`, `source_workspace_id`, `source_workspace_url`, `source_sp_client_id`, `source_sp_secret_scope`, `source_sp_secret_key`, `spn_secret_value` |
| 2 | **Output** | `staging_location`, `state_catalog`, `state_schema`, `catalog_mapping_json` (B13) |
| 3 | **Bundle scope** (01/02) | the 11 `migrate_*` toggles, `force_full_*`, `max_scim`/`max_workspace_items`/`max_ws_api_calls`, `content_fetch_workers` |
| 4 | **Import** (04) | `dry_run`, `import_assets`, `allow_deletes`, `pause_job_schedules`, `library_force_start_clusters`, `preflight_enforce`, `retry_mode`, transform options, `account_id` |
| 5 | **Run** | `run_id`, `log_level` (B5), `parallel_threads` (B6) |

- **`staging_location` + `state_catalog` + `state_schema` go under ONE group — "Output"** (user
  2026-10-01; this is exactly what the UC utility calls the analogous group — `output_volume_path` +
  `ops_catalog` + `ops_schema` are all "Output" there). `catalog_mapping_json` (B13) also lives here.
- Keep the convention **consistent across all four notebooks** — a widget shared by several notebooks
  (e.g. `connectivity_mode`, `staging_location`, `run_id`) carries the **same label** everywhere.
- Pure labels: no widget NAME changes, no default changes, no reads/job-params touched. Confirm against
  `job_templates.py` / `00_Install_Jobs` that nothing keys off the label text (it keys off the name).

**Acceptance.**
- In each notebook's widget bar, widgets appear **grouped and ordered** (all Source together, then
  Output = staging+state, Bundle scope, Import, Run) via the label prefixes.
- Every `dbutils.widgets.get(...)` / job `base_parameters` key is unchanged → a diff shows **only label
  strings changed**; a full run (and `00_Install_Jobs` deploy) behaves identically.
- The labelling matches the UC utility's style so the two tools read the same.

---

## B13 (FEATURE, MED) — catalog rename on target (`catalog_mapping_json`): remap UC catalog references in migrated assets

**Reported by the user 2026-10-01.** One BU is **renaming its catalog in the new region**, so any
migrated asset that references the source catalog by fully-qualified name will point at a catalog that
doesn't exist on target. UC is out of scope for this tool (tables aren't migrated), but several
**workspace** assets embed UC FQNs, and today they're carried **verbatim** — so a renamed target catalog
silently breaks them. The UC governance-migration utility already has this feature; reuse its shape.

**The widget + config.** Add **`catalog_mapping_json`** — inline JSON with the **flat, simple shape the
user specified (2026-10-01):** `{"source_catalog_name": "target_catalog_name"}` — one key per catalog,
source name → target name. **This is a FLAT map, NOT UC's nested `{"catalogs": {...}}` shape** — simpler
for the operator; `parse_catalog_mapping()` here accepts the flat dict directly (adapt UC's helper, don't
copy the nested-key expectation). **Blank = identity (today's behaviour, no remap)**; parse → `{src: tgt}`
dict on the `Config`. Reuse UC's **`rewrite_catalog_references(text, mapping)`** (UC `package_import.py:609`)
as the token-safe rewriter (replace `` `src`.`` / `src.` / `USE CATALOG src` at identifier boundaries,
never a substring of a longer name). Lives in the **Output** widget group (B12). Record the resolved
mapping in `config_resolved.json`.

**Scope — EXACTLY three asset types (user decision 2026-10-01).** Remap the catalog reference in **AI/BI
dashboards, Genie spaces, and DLT pipelines** only. Everything else is **documented OUT OF SCOPE** for
catalog remap in v1.

| Asset | Where the catalog ref lives | In scope? |
|---|---|---|
| **AI/BI dashboards** | `serialized_dashboard` → dataset query text (`catalog.schema.table`) | **YES** — rewrite catalog tokens in the serialized JSON's dataset queries (a surgical edit to the otherwise-verbatim blob). Interacts with B7. |
| **Genie spaces** | `serialized_space` → table identifiers / FQNs | **YES** — rewrite catalog tokens in `serialized_space`. |
| **DLT pipelines** | spec `catalog` (UC target catalog) + `target`/`schema` | **YES** — remap the structured `catalog` field. |
| Legacy SQL queries (`query_text`) | FQNs in SQL | **OUT OF SCOPE — documented.** |
| Jobs (`sql_task` inline SQL, params) | FQNs in SQL | **OUT OF SCOPE — documented.** |
| Notebook / file CONTENT (`USE CATALOG`, 3-part names) | code | **OUT OF SCOPE — documented** (blind content rewrite is unsafe). |
| Clusters / policies (default-catalog spark conf) | conf key | **OUT OF SCOPE — documented.** |
| SQL warehouses / serving / secrets / IP / workspace-conf / repos | none | n/a. |

**Proposed change (dev spec).**
- Config: `catalog_mapping_json` widget + `parse_catalog_mapping` → `Config.catalog_mapping` dict;
  blank → `{}` → **identity (no behavioural change — byte-identical to today)**.
- A shared `remap_catalog_refs(text, mapping)` (port UC's `rewrite_catalog_references`; token-boundary
  safe) applied in **exactly** the dashboards importer (`serialized_dashboard` dataset queries), the
  genie importer (`serialized_space`), and the DLT importer (`catalog` field). No other importer calls it.
- **Document the OUT-OF-SCOPE set** in the not-migrated catalog + runbook: a renamed catalog is NOT
  rewritten in legacy queries, job SQL tasks, notebook/file content, or cluster/policy conf — the
  operator handles those by hand if the BU renames a catalog that those reference.
- **Standing UC-FQN caveat:** even with remap, the **target** catalog + schema + tables must exist on
  target (UC out of scope) — remap points the asset at the right *name*; it doesn't create the data. The
  QA fixtures pre-create the renamed target catalog/schema/empty tables.
- Validation: reject a mapping with blank src/tgt (UC does); log the active mapping at INFO.

**LIVE-VERIFIED (source_ws 2026-10-01) — where the catalog ref lives in `serialized_dashboard`.** A
dashboard with a dataset `SELECT * FROM main.default.mytable` stores it under the dataset's **`queryLines`**
array (SQL text) in `serialized_dashboard` — confirmed in the stored blob. So the dashboard remap
rewrites catalog tokens inside each dataset's `queryLines` (SQL text), not a structured `catalog` field.
(Genie's `serialized_space` is the analogous serialized blob; DLT's `catalog` is a structured field.)

**Acceptance.**
- `catalog_mapping_json` blank → **byte-identical** behaviour to today (no asset changed).
- With `{"src":"tgt"}` (flat shape): a migrated **dashboard / Genie space / DLT pipeline** that referenced
  `src.schema.obj` references `tgt.schema.obj` on target; identifiers that merely contain `src` as a
  substring are untouched (token-boundary safety).
- The three in-scope types only — legacy queries / job sql_tasks / notebooks / cluster conf are **not**
  rewritten (verified unchanged) and the OUT-OF-SCOPE limitation is documented.
- Live: with the target catalog renamed + the mapping set, a dashboard/Genie/DLT points at the renamed
  catalog and works (given pre-created target catalog/tables); unit tests cover the rewriter (boundary
  cases) + the three importers' remap.

---

## Implementation sequencing

Proposed order — front-load the foundations that make the rest safe to build and diagnose. Nothing is
implemented yet; this is triage, not a commitment.

1. **B5 (logging)** — do FIRST. Independent, low-risk, high-leverage: real per-step cell logs make every
   other change and every live QA pass diagnosable, and are a prerequisite for B6 (parallel runs stay
   readable) and B11 (a report-write failure needs a loud ERROR line).
2. **B9 (run_id = job run id) + B11 (loud report-write)** — the run/report-integrity pair. Together they
   end the "successful run, missing/misplaced report" class and make retry/repair correlatable. Small,
   high-value, unblock trustworthy QA of everything else.
3. **B8 (home-provisioning) + B10 (`.db_internal` ACL skip)** — the two "endless `failed_only`" bugs.
   Both are contained, both remove false failures that block a clean sign-off.
4. **B3 (cluster-policy family) + B4 (workspace-conf read-back)** — the two HIGH correctness bugs with
   known repros; B4 also seeds the read-back-verify pattern reused by B7.
5. **B7 (dashboard publish parity)** — HIGH, larger surface (collector + importer + state + report).
   Depends on B4's read-back pattern and B2's owner work.
6. **B2 (owner preservation)** — cross-cuts several types; B7 consumes its owner mechanism.
7. **B13 (catalog rename)** — shares the dashboard/genie/DLT/query/job-sql_task importer surface with
   B7, so do it **right after B7+B2** (same files, same serialized-blob handling; one careful pass over
   those importers covers publish-state, owner, and catalog remap together).
8. **B6 (parallelism)** — biggest/riskiest; do after B5 (logging) so concurrent runs stay diagnosable
   and after B4/B11 (report + state integrity) so the thread-safety work builds on settled aggregation.
9. **B1 (airgap `source_run_as_spn`) + B12 (widget grouping)** — independent quick wins; do together
   (B1 adds the `source_run_as_spn` widget, B12 labels it + regroups the rest; label-only + one wire-up).

**Testing throughout:** each item ships with its own offline regression test (below) + a live
verification. Run the full human-style live QA pass (per [`plans/qa-testing-agent.md`](qa-testing-agent.md))
after each batch lands — the stop-and-review cadence from PLAN 10 / 10.5 / 11.

---

## Single-session implementation protocol (the "0 bugs at the end" bar)

The user's requirement: implement **all** of B1–B13 in **one session, with zero bugs at the end**. That
is achievable because the work decomposes into independent, individually-tested units behind flags.
The protocol:

**Per-item loop (never deviate).** Take ONE item in the sequencing order; then:
1. **Build** the change (code only for that item).
2. **Unit-test it** — add/extend the item's named regression test (the "Test case per scenario" row).
3. **Run the FULL offline suite** (`python3 -m pytest`), not just the new test. It must be **100% green**
   — the ~243 existing tests + the new ones. A red suite blocks moving on. This is the 0-bug gate.
4. **Checkpoint** (leave the tree working + committed-by-the-user per the no-push rule) and record the
   item DONE in the summary table (Status → DONE + test name), then take the next item.

**Definition of Done, per item:** code + named unit test + full suite green + the plan row's Status
flipped to DONE with the test name. An item is NOT done on a green *new* test alone — the whole suite
must stay green (no regressions).

**Why 0 bugs is realistic here — every item is flag-guarded / behaviour-neutral by default:**
- **B12** label-only; **B13** `catalog_mapping_json` blank → identity (byte-identical); **B6**
  `parallel_threads=1` → serial (byte-identical); **B5** adds logging (no logic change) with
  `log_level`; **B1** `source_run_as_spn` only affects the airgap-source job. So each lands "off by
  default" and is provable byte-identical to today until explicitly exercised — no item can regress a
  passing run just by being merged.
- The **behaviour-changing** items (B3, B4, B7, B8, B9, B10, B11, B2) are **narrow and have a precise
  repro + a regression test** that pins the new behaviour and guards the old (e.g. B10: internal-dir ACL
  skipped AND a non-internal ACL still applies; B8: fresh-user home attempt-succeeds AND genuine-absent
  still prerequisite).

**Build order within the session (shared foundations first, so later items reuse tested code):**
1. **B5 logging** (foundation; `logging_util.py` ported + wired) → then everything logs.
2. **B4 read-back helper** (`verify_applied(get, intended)` + report-not-applied) — reused by B7's
   publish read-back and the declarative-call audit.
3. **B13 `remap_catalog_refs` + `parse_catalog_mapping`** (pure helpers, fully unit-tested in isolation)
   — reused by B7's dashboard/genie remap.
4. **B9 + B11** (run_id = job run id; loud report-write) — run/report integrity.
5. **B8 + B10** (home attempt-based; `.db_internal` ACL skip).
6. **B3** (cluster-policy family) + **B2** (owner, incl. queries `owner_user_name` + orphan→run-as-SP).
7. **B7** (dashboards: publish/schedule reconcile) **then B13's dashboard/genie/query/dlt remap** in the
   same importer pass (shared files).
8. **B6** (parallelism) last of the behaviour changes (reuses B5 logging + settled state aggregation).
9. **B1 + B12** (airgap SP widget + relabel/regroup all widgets) — the trivial finish.

**Hard rules for the session:** keep `dry_run`/`parallel_threads=1`/blank-mapping defaults; never push
(user does git); don't touch unrelated code; if an item turns out bigger than its spec, STOP and surface
it rather than half-landing it. **Live QA (the `qa-testing-agent.md` pass) is separate** and runs after
the offline session — "0 bugs at end of session" = full offline suite green + every item's DoD met.

---

## Testing

### Verification status (what's live-proven vs code-structure — a new session can trust these)

| Item | Behavior the fix depends on | Status |
|---|---|---|
| **B2** | `IS_OWNER` PUT changes a job's owner; `owner_user_name` PATCH changes a query's owner (both read-back) | ✅ **LIVE** source_ws 2026-10-01 |
| **B3** | family-create (family_id+overrides, no definition) works; GET returns both; both-together → 400 | ✅ **LIVE** source_ws 2026-10-01 |
| **B4** | `workspace-conf` PATCH silently drops some keys (200, no change) → read-back mandatory | ✅ **LIVE** target_ws (`enableWebTerminal` false dropped) |
| **B5** | levelled per-step logging + the two Databricks cell gotchas | 🔧 code-structure — port the UC **shipped** `logging_util.py` (proven there) |
| **B6** | bounded thread-pool + barriers + thread-safe state | 🔧 code-structure — reuses existing `parallel_map`/`with_retry` |
| **B7** | publish NOT idempotent (diff-guard required); schedule shape; dashboards have no owner; shared embeds caller | ✅ **LIVE** source_ws 2026-10-01 |
| **B8** | user homes are PROTECTED + lazily provisioned; `mkdir` impossible; not forceable | ✅ **LIVE at scale** 2026-10-01 (200/200 protected; 0/100 in 8 min) |
| **B9** | `{{job.run_id}}` resolves into `base_parameters` = the job run id | ✅ **LIVE** source_ws 2026-10-01 (exact match) |
| **B10** | `PUT permissions` on `.db_internal` → 403 (protected; even ws-admin lacks Manage) | ✅ customer run evidence (`jioindiawest`) |
| **B11** | report-write failure must be loud (don't swallow) | 🔧 code-structure — `import_runner.run()` finally block |
| **B12** | widget labels only (names/defaults unchanged) | 🔧 code-structure — label strings only |
| **B13** | catalog FQN lives in `serialized_dashboard` dataset `queryLines`; Genie `serialized_space`; DLT `catalog` | ✅ **LIVE** source_ws 2026-10-01 |

The 🔧 items are **engineering** (code shape / porting a proven impl), not external-API behaviors — there's
no live probe to run; they're validated by their offline regression tests + the live QA pass.

### Two testing layers

Two layers, mirroring the UC governance-migration utility:
- **Offline regression tests** — `python3 -m pytest` (the repo's ~243 offline tests today; `pytest.ini`
  keeps them safe to run anywhere). Every fix/feature below adds a named regression test so the bug
  can't silently return. Live harnesses (`tests/live_*.py`) are invoked explicitly, never in the
  offline suite.
- **Live human-style QA pass** — a tester (or the QA agent) exercises the scenario end-to-end on real
  `source_ws` → `target_ws`, validating every report Sheet against the live workspace. The operating
  contract — allowed actions, identities/secrets, hard rules, stop-and-ask cadence — is
  **[`plans/qa-testing-agent.md`](qa-testing-agent.md)** (the canonical QA agent prompt for this tool,
  the analogue of the UC utility's `plans/qa-testing-agent.md`). The live pass uses the **`direct`**
  mode by default (import is mode-agnostic; PLAN 10.5 proved direct == airgap), with an **`airgap`** pass
  where the item touches the source-side job/handoff (B1). Follow the PLAN 10 / 10.5 / 11 cadence:
  **Run 1 baseline → stop for review; apply the change → Run 2 incremental → stop; Run 3 retry/heal →
  stop**, `plan13_` deliverables.

### Fixtures (what the source bed must contain)

Build on the existing bed — **extend `tests/fixtures_fvm1.py`** (the 19 dependency-ordered, idempotent
phases) rather than forking a new one; add a phase per new scenario so the bed stays the cumulative
superset. **Non-negotiable: the bed keeps EVERY asset type the v1 fixtures already covered as the base**
(the fixtures that proved the shipped functionality). The B1–B13 scenarios **ADD** to that base; they
never replace it. **This change-set must not regress anything that worked in v1 — ensuring that (full
base coverage + the full offline suite green) is part of the implementer's Definition of Done, not just
the tester's.** The live QA pass has **two run shapes**, so the bed has two parts:

**A) Initial-run fixtures — the FULL base (every type the tool migrates) + the B-scenario adds.**
- **Identity — all four kinds (the core of this tool):**
  - **Entra-ID-backed users** (SCIM-provisioned, `externalId` set — needs the ACCOUNT profile so SCIM
    keeps `externalId`),
  - **Entra-ID-backed account groups** (security groups created in Azure AD, provisioned into the
    Databricks account via SCIM, assigned to the workspace),
  - **Databricks-managed (workspace-local) groups**, including **nested** groups,
  - **Azure UMI / Entra service principals** (account-level, stable `applicationId`) **and**
    **Databricks-managed SPs** (workspace-local, recreated with a new appId → `sp_mapping`);
  - plus: a group whose members span users + SPs + nested groups (membership parity); the full
    **entitlement** ladder (`allow-cluster-create`, `databricks-sql-access`, `workspace-access`,
    `allow-instance-pool-create`); and an object **owned by an in-roster user ≠ the run-as SP** (B2).
  - **Scenario add:** a **bulk batch of N freshly-created users** who own notebooks/files (B8).
- **Workspace content:** directories, notebooks (multiple languages), workspace files, a `>10 MB` file
  (streaming-import path), a `/Shared` object, Git repos (metadata-only), and **`.db_internal` under a
  home** (B10). ACLs across every object type + principal kind (the v1 `acls` phase's full ladder).
- **Compute:** instance pools; cluster policies — **a policy-FAMILY policy (e.g. "Job Compute") + a
  custom-definition policy** (B3); all-purpose + job clusters referencing the policy/pool; cluster
  libraries; global init scripts.
- **Jobs — the job types the tool handles:** multi-task jobs covering `notebook_task`, `spark_python`/
  wheel, `sql_task` (query/warehouse), `dbt_task`, `pipeline_task`, `run_job_task`; a job whose `run_as`
  is a DB-managed SP; schedules/pauses.
- **SQL / DLT:** SQL warehouse(s); legacy query + legacy alert + alert_v2 + (inventory-only) legacy
  dashboard; a DLT pipeline (with a `catalog` for B13).
- **Dashboards / Genie:** AI/BI dashboards (the B7 matrix below) + a Genie space (serialized, warehouse).
- **Serving / misc:** a model-serving endpoint (external-model = migratable; UC-model = manual); the 9
  **workspace-conf keys** incl. **one set to a non-default value differing from target** (B4,
  e.g. `enableWebTerminal`/`enableExportNotebook`).
- **Secrets:** a Databricks-backed scope (migrates) + an AKV-backed scope (manual).
- **B13:** a dashboard/Genie/DLT referencing a catalog that will be **renamed** on target.

(These are the standing v1 types — Entra vs DB-managed identities, every job/compute type, etc. — kept
intact so the regression suite proves the base still works; the parentheticals mark the B-scenario adds.)

**B) Incremental / repair / retry fixtures (mutations applied BEFORE Run 2 / Run 3).**
The incremental run proves update / skip-unchanged / delete-in-source / retry-heal detection — so after
Run 1 completes and we stop for review, apply a controlled set of source mutations, then Run 2:
- **Edited** cluster policy definition; **new** job + **new** pool + **new** policy (chain); an **edited
  notebook's content** (content-fingerprint change); an **edited** workspace-local group's membership.
- A **renamed** object (→ deleted-in-source + created, per PLAN 11 F3) and a **deleted** source object
  (→ `deleted_in_source`, report-only unless `allow_deletes`).
- **Unchanged** objects (must become `skipped` with zero writes).
- **Dashboard mutations** (below).
- For **Run 3 (retry/heal):** deliberately leave a fixable failure in Run 2 (e.g. a withheld
  `servicePrincipal.user` grant, or a UC table a Genie space references not yet present), fix it, then
  run `retry_mode=failed_only` and confirm only the failed set re-runs and heals.

### Dashboard fixtures (the scenario to test especially — B7)

Because B7 covers publish-state, data-permission principal, owner, schedule and permissions — **and**
backward-compat for already-migrated draft-only dashboards — the bed needs a full matrix, exercised
across BOTH run shapes.

**Initial-run dashboard matrix (Run 1).** Create AI/BI (Lakeview) dashboards covering every axis:

| # | Publish state | Data-permission mode | Schedule | Owner | Proves |
|---|---|---|---|---|---|
| D1 | **Published** | Individual (`embed_credentials=false`) | none | run-as SP | published stays published; viewer-cred mode |
| D2 | **Published** | **Shared → specific SP** | daily cron | in-roster user ≠ run-as SP | SP credential **remapped**; owner parity; publish |
| D3 | **Published** | **Shared → publisher (user)** | none | that publishing user | the honest **publisher-USER limitation** (falls back to run-as SP) |
| D4 | **Draft only** | n/a | none | in-roster user | draft stays draft (not published on target) |
| D5 | **Published** | Shared → SP | **paused** schedule + a subscription | in-roster user | schedule pause-state + subscription recipient remap |
| D6 | DAB-deployed, published | any | — | — | DAB dashboards still handled correctly (not duplicated) |

Each references a UC table by FQN → **pre-create the referenced catalog/schema + EMPTY tables on
target** (UC out of scope) so the dashboard imports without the "renders empty" caveat masking a real
failure. Live-validate per D-row: target published-vs-draft, `embed_credentials` mode, the credential
principal (remapped SP for D2/D5; run-as SP + note for D3), schedule (cron/pause/subscription with
remapped warehouse), owner, and the **combined atomic status** (draft+publish both ok → success; either
fails → FAILED with the per-facet note).

**Incremental dashboard mutations (Run 2) — the publish-state deltas B7 must detect:**
- **D4 draft → publish it** on source → Run 2 must publish it on target (facet change detected).
- **D1 published → unpublish it** on source → Run 2 must **detect the change and FLAG it (drift row);
  the target stays published — the tool must NOT unpublish it** (user decision 2026-10-01, flag-only).
- **D2 republish** with a different warehouse / flip `embed_credentials` → Run 2 reconciles the change,
  and does **NOT** mint a new revision when nothing changed (idempotency).
- **D5 schedule change** (cron edit, or pause→unpause, or add/remove a subscription) → reconciled.
- **Owner change** on D2 (reassign to a different in-roster user) → re-applied.
- **Unchanged dashboard** → `skipped`, zero writes, no new published revision.

**Backward-compat fixture (the pre-fix state):** take a dashboard **migrated draft-only by the OLD
build** (or simulate it: create the target draft + a state-store row with no publish facet), then run
the FIXED inventory→export→import → it must **heal to published/scheduled/owned on the existing target
dashboard, with NO duplicate** (UPDATE path via the adopted target id), per B7's backward-compat section.

### Test case per scenario

| Item | Offline regression test (pytest) | Live verification (per QA agent) |
|---|---|---|
| **B1** source `run_as` SP (airgap) | `test_install_jobs`: `source_run_as_spn` is projected onto the `airgap_source` job's `run_as` when `connectivity_mode=airgap`; ignored (blank) in `direct`. | Deploy `airgap_source` with a **source** ws-admin SP; assert the deployed job's `run_as` = that SP; run 01→02 on source → bundle produced; a missing `servicePrincipal.user` surfaces as a clear preflight message, not a raw 403. |
| **B2** owner preservation | Per-type unit: after create, `owner_user_name` is set to the **remapped source owner** for queries (settable via update API); exact-or-skip when the owner is absent from the target roster; clusters record the `OriginalCreator` tag. | Create a query / dashboard / Genie space on source owned by an **in-roster user ≠ the run-as SP**; migrate; assert the **target owner == source owner** (remapped). Cluster → `OriginalCreator` tag present + documented. |
| **B3** cluster-policy family | Unit: a **family-based** policy unit → create body contains `policy_family_id` (+ overrides) and **NOT** `definition`; a **custom** policy → the reverse; `update_one` mirrors it; pool-id remap applied to `policy_family_definition_overrides`. | Source workspace with a policy built on the **"Job Compute" family**; migrate; policy is **created (no 400)**; overrides land with remapped pool ids. |
| **B4** workspace-conf read-back | Unit: `_set_conf` where PATCH returns 200 but a follow-up GET shows the value **unchanged** → recorded **FAILED** (observed vs intended), fingerprint **NOT advanced** → next run re-attempts. | Set `enableExportNotebook=false` on source; migrate; the tool PATCHes then **reads back**; if the platform ignored it, the report row is FAILED with observed-vs-intended, and the **workspace-conf parity table** shows source vs live-target vs status for all 9 keys. |
| **B5** per-step cell logging | Unit: `configure_logging` idempotent + `propagate=False`; `_LiveStdoutHandler.emit` resolves **current** `sys.stdout` per record + flushes; `_ContextFilter` stamps `run_id`+`stage`; `_RedactFilter` scrubs a registered secret; `get_captured_log()` returns the buffer. | Watch a live job: each phase + per-object line **streams in the running cell** (`> Importing users` → `user1 → created` → `users complete: N ok`). Kill mid-run → the **last cell line** names the stuck step. Break a step → the **cell output alone** pinpoints it (object + code + raw error). No double-printed lines; a `notebook.exit` (if any) sits alone in a trailing cell and does not swallow logs. |
| **B6** bounded parallelism | Unit: `parallel_map` fail-soft + completion-order; sub-level barrier ordering preserved; shared identity maps / results / checkpoint correct under a **forced-parallel** test (many small units) with no lost/dup rows; **`parallel_threads=1` reproduces serial output exactly**. | Large workspace: enrichment materially faster than serial with a **byte-identical bundle** (modulo ordering); import sub-levels stay ordered (users→SPs→groups→membership; pools→policies→clusters); membership reaches parity; fault injection — a 429 is ridden out, a deterministic error fails only its unit (ERROR + FAILED row) and does not advance state. |
| **B7** dashboard publish parity | Unit: collector captures `is_published` + `embed_credentials` + data-permission principal + schedule + owner; importer reconcile **publishes idempotently** (only on a real diff), and on a source unpublish **flags drift only — never unpublishes target** (user decision 2026-10-01); **combined atomic status** (draft ok + publish fail → **FAILED**, per-facet note); Shared→SP credential **remapped**; publish/schedule folded into the fingerprint. | Source dashboards covering each **data-permission mode** (Individual / Shared→publisher / Shared→SP), **published AND draft**, at least one **scheduled**; migrate; assert target published-vs-draft, `embed_credentials` mode, schedule (cron/pause/subs, remapped warehouse), owner. **Backward-compat:** take a dashboard migrated draft-only by the OLD build → re-export+import with the fix → it heals to published/scheduled/owned **without duplication**. |
| **B8** home-provisioning (defer + re-sweep) | Unit: a home-content unit whose home is absent is **deferred** (not hard-failed) and **re-swept at end of run** against a FRESH `get-status` (home appeared late → imported; still absent → clean `prerequisite_missing`); the tool **never** calls `mkdirs` on a `/Users/<u>` home; `_home_present` never pins a transient "absent" for a this-run owner; a genuinely-absent owner → immediate prerequisite/PLAN-9 backup (not deferred). | Bulk-migrate users + home content in ONE run → a home that lazily appears during the run is imported by the end-of-run re-sweep (no manual `failed_only` for within-run provisioners); still-absent homes are clean prerequisites that heal on a later `failed_only`. *(Live-verified 2026-10-01: `mkdir` on a home is impossible — 200/200 `DIRECTORY_PROTECTED` at 100-user scale; 0/100 homes 8 min after provisioning → homes are platform-lazy, un-forceable; re-sweep is a partial recovery by design.)* |
| **B9** run_id = job run id | Unit: shipped `jobs/*.job.json` carry `run_id="{{job.run_id}}"`; `00_Install_Jobs` projects it; the manifest-missing error names `LATEST_EXPORT`. | Run the end-to-end job → the bundle **dir name == the Jobs-UI run id**. **Repair** the failed job → same dir (reports refreshed in place). **New trigger** → new dir. **Standalone `failed_only <run_id>`** → `import_status_retry_<ts>.xlsx` in that **same** dir (canonical report preserved). Point import at an incomplete-export dir → the **actionable** message naming `LATEST_EXPORT`, not a bare "manifest.json". |
| **B10** `.db_internal` ACL skip | Unit: the ACL importer **skips** `is_skippable_path` dirs (`/.db_internal`, `/.ide`, `/.databricks`) as `SkippedNoObject`/not-supported (like `/Shared`); the workspace collector does **not** `fetch_acl` for them; a NON-internal directory ACL still applies. | Source with user homes containing `.db_internal`; migrate; **zero `permission_denied` rows** for those dirs (clean "skipped — platform-owned", or absent); `retry_mode=failed_only` does **not** re-attempt them; a genuine directory-ACL failure elsewhere still reports as a real failure. |
| **B11** loud report-write | Unit: force `_write_reports` to raise on an otherwise-successful run → `run_status` reflects **not-cleanly-complete** (e.g. `completed_no_report`), an **ERROR** is logged; the success path is unchanged; an aborting run still re-raises its ORIGINAL error. | Make the `reports/` write fail (e.g. a read-only path / induced I/O error) → the run does **NOT** present as a clean success; the notebook surfaces "import ran but the report was not written"; a post-write existence+size check confirms the xlsx landed on a healthy run. |
| **B12** widget grouping | Unit: snapshot each notebook's widget definitions → every widget NAME + default + type unchanged vs today, only label strings changed; labels match `"<N><letter>. <Group> · <desc>"`; a widget shared across notebooks has an identical label; staging+state widgets all carry the **Output** group prefix. | Open each notebook's widget bar → widgets appear **grouped + ordered** (Source together, then Output/Bundle/Import/Run); a full run + `00_Install_Jobs` deploy behave identically (the diff is label text only). |
| **B13** catalog rename | Unit: `parse_catalog_mapping` (flat `{"src":"tgt"}`, blank→{}); `remap_catalog_refs` rewrites `src.sch.tbl` / `` `src`.`` but NOT a substring of a longer name; **exactly the 3 importers (dashboards/genie/dlt)** apply it and no others; blank mapping → byte-identical payloads; legacy-query/job-sql_task payloads are **unchanged** (out of scope). | Rename the target catalog + set `catalog_mapping_json`; migrate → a **dashboard / Genie space / DLT pipeline** points at the renamed catalog and works (target catalog/schema/empty tables pre-created); blank mapping run is byte-identical. |

### Bugs identified (populated during testing)

*(The QA agent appends confirmed bugs here — one subsection per bug, with repro + evidence — and reports
them in its stop-and-ask summary after each run.)*

#### QA-1 (BUG, MED) — B6 `parallel_threads` (and `log_level`) are not exposed by the job templates or `00_Install_Jobs`, so the shipped jobs always run SERIAL

- **Commit under test:** `70c980d` (branch `plan13-backlog-b1-b13`), live on target `ai27-target-ws`.
- **What:** `parallel_threads` and `log_level` appear in the notebook widgets (`01_Inventory`,
  `02_Export`, `04_Import` — defaults `"1"` / `"DEBUG"`) and are read by `Config.from_dbutils`, but
  they are absent from **every** `jobs/*.job.json` task `base_parameters` AND from the installer's
  `_params` projection in `notebooks/00_Install_Jobs.py`. Verified live:
  `grep -rn "parallel_threads\|log_level" jobs/*.json notebooks/00_Install_Jobs.py src/utils/job_templates.py`
  → no matches.
- **Impact:** a customer who deploys and runs the shipped jobs can NEVER enable B6 parallelism — the
  inventory/export enrichment and the import always run at `parallel_threads=1` (serial). The entire
  B6 feature is unreachable through the product's only supported run path (the installed jobs). At the
  RRL ~1M-asset / ~850K-ACL scale this is the difference between the ~1–2h target and the ~11h serial
  run B6 was built to fix. `log_level` is likewise pinned to the `DEBUG` default.
- **Repro:** run `00_Install_Jobs` → inspect any deployed job's import/inventory/export task
  `base_parameters` → no `parallel_threads` key → notebook falls back to `"1"`.
- **Fix (suggested):** add `parallel_threads` (and `log_level`) to the installer widgets + `_params`,
  and to the `base_parameters` of the inventory/export/import tasks in the six `jobs/*.job.json`.
- **Test workaround (so B6 could still be exercised live):** patched the three deployed jobs via
  `POST /api/2.2/jobs/reset` to inject `parallel_threads=8` on every notebook task. NOT a product fix —
  filed here, not applied to source.
- **RESOLVED 2026-10-01:** `parallel_threads` + `log_level` added to `00_Install_Jobs` widgets +
  `_params`, and to the `base_parameters` of every notebook task in all six `jobs/*.job.json`. Labels
  are verbatim the existing notebooks' (no plan tags in widget text). Test:
  `test_qa1_installer_and_job_templates_carry_parallel_threads_and_log_level`.

#### QA-2 (BUG, HIGH) — `WorkspaceImporter.existing_keys()` is a SERIAL, SILENT, un-parallelized per-unit `get-status` pre-pass that does not scale (B6 does not cover it)

- **Commit under test:** `70c980d` (branch `plan13-backlog-b1-b13`), live on target `ai27-target-ws`,
  live run_id `133961554258847` (`parallel_threads=8`), source bed ~9,500 workspace objects across 150
  user homes.
- **What:** `src/importers/workspace_importer.py:144 existing_keys()` loops over **every** workspace
  unit and issues a serial `workspace/get-status` per unit (workspace_importer.py:162), AND for each
  unit first calls `_resolve_home_target(path)` → `base_importer._home_present()` (base_importer.py:636)
  which, because it deliberately does **not** cache a transient ABSENT home (correct for the B8
  self-heal re-sweep), re-probes the home root on **every** descendant unit. Net: ≈2 serial
  `get-status` calls per unit (≈19,000 for this bed; ~9,400 of them redundant re-probes of the same 150
  absent home roots). B6 Scope-2 parallelism only wraps the per-unit *processing* loop
  (`_run_parallel`), NOT `existing_keys()` — so this pass runs single-threaded regardless of
  `parallel_threads`.
- **Impact (live-observed):** on this first-time migration the import sat in `existing_keys()` for
  **~20+ minutes with ZERO target writes and ZERO log output** — the phase's `importing |
  component=workspace …` line prints only *after* `existing_keys()` returns, so the notebook cell froze
  on `compute phase done` and the run was indistinguishable from a hang. At the RRL ~1M-asset scale this
  is **hours** of serial existence-probing — the precise failure mode B6 was built to fix, left
  uncovered. On a FIRST run the whole pass is also wasted: every probe returns absent and the create
  path's `RESOURCE_ALREADY_EXISTS` adopt would reach the "equivalent outcome, cheaper" the docstring
  itself cites.
- **Repro:** direct-mode live import of a bundle with thousands of workspace objects under
  not-yet-provisioned target homes → long silent stall before the workspace phase logs anything.
- **Fix (suggested):** (a) parallelize `existing_keys()` with the same bounded pool B6 uses (it is pure
  independent reads); (b) cache the absent-home result *for the duration of `existing_keys()`* so 150
  homes are probed once, not 9,400×; (c) emit periodic progress during the pre-pass; and/or (d) on a
  first run (empty state) skip the probe and rely on the create-path adopt the docstring already
  endorses. Relates to QA-1 (parallelism not wired into jobs) and PLAN_12 (ACL-enrichment parallelization).
- **RESOLVED 2026-10-01 (chosen fix — control table IS the source of truth, user-directed):**
  `WorkspaceImporter.existing_keys()` now derives existence from the in-memory control table
  (`StateStore.migrated_keys("directory","notebook","workspace_file")`) with **ZERO** live API calls —
  the per-object `get-status` probe is gone, so the workspace phase starts and logs a per-object line
  immediately. The CHANGE SIGNAL moved from a content SHA to the source **`modified_at`** (free in the
  bulk `workspace/list`, verified live to bump on edit), folded into the fingerprint at export; content
  bytes are still migrated but no longer hashed into the signal. Create-path `RESOURCE_ALREADY_EXISTS`
  adopt remains the untracked-object safety net; `force_full_import` re-evaluates everything. Direct-mode
  export additionally **skips re-fetching** unchanged bytes (fingerprint matches the control table) with
  per-object progress logging (Edit E), guarded so `force_full_import` pairs with `force_full_export`.
  Tests: `test_qa2_existing_keys_derives_from_control_table_with_zero_api_calls`,
  `test_qa2_create_skip_update_decided_from_control_table`,
  `test_direct_mode_export_skips_refetch_of_unchanged_content`, and the modified_at pair in
  `test_fingerprint_gaps.py`. Full offline suite: 436 passed.

#### QA-3 (BUG, CRITICAL) — B8's premise is FALSE: a user home IS created by importing content into it; the defer logic skips that write, so blank-target home content migrates 0% and NEVER heals

- **Commit under test:** `dc94fae` (branch `plan13-backlog-b1-b13`), live on target `ai27-target-ws-3`
  (`adb-7405616024522912.12`), live run_id `406713219208188` (`parallel_threads=4`, direct mode),
  source bed 10,555 workspace objects across ~150 freshly-assigned Entra user homes.
- **What B8 assumed (line 715 premise, and the live tests behind it):** "`/Users/<user>` is a protected,
  system-managed folder; `mkdir` always returns `DIRECTORY_PROTECTED`; the tool CANNOT make a home exist;
  homes are provisioned lazily on first login, outside the tool's control." The 100-user test that
  "settled" this only ever tried **`mkdir`** (and `get-status`). **It never tested `workspace/import` of
  real content** — which is what `main` actually did and what provisions a home.
- **What is actually true (proven live 2026-10-02, two independent identities):** importing a notebook
  into a non-existent home **creates the home**. `mkdir` is protected; `workspace/import` is not.
  - As workspace admin (abhishek.iyer): `/Users/aaziz.ikhmir@databricks.com` → `Path doesn't exist`;
    `workspace/import .../wsmig_home_probe` → **home now `DIRECTORY 102621185048350` + notebook
    `102621185048353`**.
  - As the **run-as SP** `0baffec3…` via OAuth M2M (the job's exact identity):
    `/Users/alex.hsieh@databricks.com` → `RESOURCE_DOES_NOT_EXIST`; SP `workspace/import
    .../wsmig_sp_probe` → `{"object_id":102621185048357}`; **home now `DIRECTORY 102621185048354` +
    notebook present.** Neither user had logged in; no lazy provisioning involved.
- **Root cause (in code):** `base_importer._resolve_home_target()` (lines ~731–745) returns `kind="defer"`
  for any in-roster/unknown owner whose home is not *already* present (gated by `_home_present()` →
  `get-status`). `WorkspaceImporter._create_*` raises `DeferredHome` instead of attempting the write; the
  `and not is_user_home(parent)` guard added at the parent-mkdir site also suppresses the write. So on a
  blank target the content write — **the only thing that creates the home** — is never issued. `main` had
  no such gate: it wrote optimistically (`if parent and not is_skippable_path(parent): mkdir(parent)` then
  import), the import created the home, and >90% landed on pass 1 (<10% retried).
- **Why it never heals:** the end-of-phase re-sweep (`_resweep_deferred_homes`) and `retry_mode=failed_only`
  only **re-check presence** via a fresh `get-status`; they never retry the deferred write. Nothing ever
  provisions the home, so every subsequent run re-defers → permanent `prerequisite_missing`. Blank-target
  home-content migration is therefore **completely broken on this branch** (this run: 10,001 of 10,555
  workspace objects deferred → re-swept → `prerequisite_missing`, 0 user homes created; only `/Shared`,
  SP-uuid homes, and the already-provisioned admin home imported).
- **Impact:** this is the customer's primary scenario (migrate user home content to a new workspace).
  `main` worked (>90%/pass-1); B8 regressed it to 0%/never-heals. Separate from and more severe than the
  7h timing regression (which is independently fixed).
- **Repro:** direct-mode live import of a bundle with content under not-yet-provisioned target homes →
  `home re-sweep (pre-ACL): deferred=N` → every `/Users/<email>/…` unit `→ failed category=prerequisite_missing`
  "USER HOME directory, which cannot be created"; target `/Users` stays empty of those homes across retries.
- **Fix (suggested, for the dev — NOT applied; tester role):** drop the `_home_present`/defer gate for
  in-roster/unknown owners and restore `main`'s optimistic write (the content `workspace/import` creates
  the home); keep a clean `prerequisite_missing`/backup path only for genuinely-absent (deleted-in-source)
  owners, and only when the write *actually* fails. The whole B8 defer + re-sweep machinery can be removed
  once the write is attempted again. Note the related `.db_internal → created` FALSE POSITIVE: `mkdirs` of
  `/Users/<email>/.db_internal` under an unprovisioned home returns success but persists nothing
  (verified `NOT FOUND` after a reported `created`), so the report over-counts `created`; `.db_internal`
  is also still being attempted at content import despite B10's skip intent — audit both against B4
  (report = verified read-back).
- **Test setup note:** left probe artifacts on target (`/Users/aaziz.ikhmir@databricks.com/wsmig_home_probe`,
  `/Users/alex.hsieh@databricks.com/wsmig_sp_probe`, `/Users/april.song@databricks.com/wsmig_mkdir_probe`)
  and a run-as-SP OAuth secret minted for the probe — moot once the target is recreated for the re-test.
- **RESOLVED 2026-10-03** (user authorised the code change). `base_importer._resolve_home_target` no longer
  returns `defer`: an in-roster/unknown **USER** home resolves to `normal_home` (descendant) / `skip_root`
  (root) so the content write is attempted — the write provisions the home. Order is now: SP-remap →
  **present-home wins** (`_home_present`, cached ~1/owner — keeps re-runs + "home already there") →
  deleted-in-source owner → backup/prerequisite → **SP appId not in `sp_mapping`** → prerequisite (a write
  can't provision a non-existent SP's home) → **USER** → write. A home ROOT is a no-op `skip_root`
  (recorded `skipped`, never mkdir'd). Separately, no-op "creates" (workspace roots, Trash,
  `.db_internal`/`.ide`, home roots) now return `{"skipped": True}` and record as `skipped`, fixing the
  phantom `.db_internal → created` (B10 was already skipping the mkdir; only the LABEL was wrong). The B8
  defer/re-sweep machinery (`DeferredHome`, `_resweep_deferred_homes`, runner `_resweep_home_content`) is
  now **vestigial** — the resolver never returns `defer` — and can be deleted in a follow-up. Tests:
  `test_b8_home_content_is_written_optimistically_not_deferred_no_root_mkdir`,
  `test_b8_home_descendant_dir_is_mkdird_at_its_own_path_not_the_root`,
  `test_b8_home_root_unit_is_skipped_not_a_phantom_create`, `test_qa3_db_internal_directory_is_skipped_not_created`,
  `test_b8_runner_imports_home_content_in_the_workspace_phase_before_acls`, plus updated
  `test_importers_phase2_5` home tests. Full offline suite: **437 passed**. UNCOMMITTED (user pushes).

#### QA-4 (BUG, MED) — in the packaged end-to-end LIVE job the EXPORT task reads a different state table (`…_dryrun`) than the IMPORT task writes (live), so export's "skip re-fetch unchanged content" is permanently defeated on every incremental run

- **Commit under test:** `e33b3263` (branch `plan13-backlog-b1-b13`), live on target `adb-7405610176410928.8`,
  live run_id `987031501233917`, job `wsmig - direct end-to-end (LIVE)` (`645993248120715`), direct mode,
  `parallel_threads=4`.
- **What was observed (export task start-up logs):**
  - `state tables ready | state=catalog_4_vrjcth.wsmig_state.wsmig_migration_state_dryrun identity=…wsmig_identity_map`
  - `state loaded | rows=0 identity_rows=0` → `Incremental state : …wsmig_migration_state_dryrun (0 rows — unchanged content will skip re-fetch)`
  - `content pass | to_fetch=9428 resumed=0 unchanged=0 workers=8` (i.e. it re-fetched everything).
- **Root cause (in code):**
  1. `config_manager.py:279` derives the state-table name from `dry_run`: the `…_dryrun`-suffixed table when
     `dry_run=True`, else the live `wsmig_migration_state`.
  2. `config_manager.py:400` defaults `dry_run` to `"true"` when the widget is absent.
  3. In the installed `direct_end_to_end_live` job, the **import** task sets `dry_run='false'` (→ live table),
     but the **export** task is deployed with **no `dry_run` base-parameter** (→ defaults true → `_dryrun` table).
  So export resolves `wsmig_migration_state_dryrun` while import resolves `wsmig_migration_state`.
- **Why it matters:** export is **read-only** to the state table (it only reads `last_source_fingerprint` in
  `export_runner._unchanged_in_state` to skip re-fetching unchanged bytes); **import** does all state writes.
  Because they resolve to different tables, in a live run import never populates the table export reads, so
  export's skip-unchanged sees 0 relevant rows on **every** run → it re-fetches 100% of workspace content on
  every incremental 2nd+ run, even when nothing changed. Verified live post-export: only
  `wsmig_migration_state_dryrun` + `wsmig_identity_map` exist in `catalog_4_vrjcth.wsmig_state` (the live
  `wsmig_migration_state` is created only once the import task runs).
- **Severity / scope:** NOT a correctness or data-loss bug. Run 1 must fetch everything regardless (no loss),
  and import-side incrementality still works via its own live table. It is a **performance defect** that
  silently defeats the export-side incremental optimization (the B6 / incremental-export work) in the
  shipped live job, and makes the `(0 rows — unchanged content will skip re-fetch)` log line misleading
  (it reads as if a skip happened; nothing is skipped). The headline home-content test is unaffected.
- **Repro:** install `direct_end_to_end_live` via `00_Install_Jobs`; run it twice with no source changes;
  observe the export task logging `…_dryrun`, `unchanged=0`, and `to_fetch` equal to the full content count
  on the second run (no re-fetch savings), while the live `wsmig_migration_state` table holds the fingerprints.
- **Fix (suggested, for the dev — NOT applied; tester role):** wire the end-to-end LIVE job's export task to
  the SAME state table the import task uses — e.g. pass `dry_run=false` to the export task in
  `direct_end_to_end_live` (and keep `dry_run=true` only in `direct_end_to_end_dry_run`), OR decouple
  export's state-table selection from `dry_run` so the export side always reads the live table. The single-
  task `export` job and the `airgap_source` job likely have the same gap — audit all job templates in
  `job_templates.py` / `00_Install_Jobs` for which tasks receive an explicit `dry_run`.

#### QA-5 (BUG, MED → two parts: reporting + lost in-run healing) — after the QA-3 fix the home-content re-sweep is dormant, so `prerequisite_missing` home content is no longer retried in-run; and `/Users/<user>` directory create/fail should not be reported at all

- **Commit under test:** `e33b3263` (branch `plan13-backlog-b1-b13`), live run_id `987031501233917` on target `adb-7405610176410928.8`. Also cross-referenced against a MAIN-branch run report (RRL, 2026-09-19, `import_status.xlsx`).
- **Evidence (MAIN report):** 276 home-content failures — 184 `directory` + 79 `notebook` + 13 `workspace_file`, all `prerequisite_missing` — across 151 distinct user homes. All 151 owners were `Created on target` in the identity phase (so NOT "never assigned" — a propagation/materialization lag between account-level assignment and the home being writable). The home-ROOT `/Users/<owner>` rows are themselves reported as `directory → FAILED`, and `.db_internal/*` rows are reported `Created` with the note "exists by construction, not created" (a mislabel; already fixed to `skipped` by QA-3).
- **Part A — reporting (user decision 2026-10-03):** the tool must **not report `/Users/<username>` home-directory creation at all** — provisioning a user home is the PLATFORM's job, not the utility's. A home root should never appear as `Created` or `FAILED`. The ONLY home-directory outcome the utility should report is a failure of its OWN construct — the PLAN 9 orphaned-home backup root (`/Users_Backup/<owner>/…`). On the current branch the home root is already a no-op `skip_root`; this item asks to go further and **omit the `/Users/<user>` root row entirely** (keep descendants) so the report stops implying the tool created/owns the home.
- **Part B — lost in-run healing (regression of B8's intent):** the end-of-content-phase re-sweep (`import_runner._resweep_home_content` → `_resweep_deferred_homes`, which DOES re-attempt the write via `_process_one`) is now **dormant**: it only consumes `_deferred_units`, which are populated solely from `except DeferredHome`, raised solely when `_resolve_home_target` returns `kind="defer"` — and QA-3 made the resolver never return `defer`. A home-content write that fails now routes through `missing_parent_prerequisite → PrerequisiteMissing → ACTION_FAILED` and is **never queued for re-sweep**. Net: a provisioning-lag straggler (owner assigned, home not yet writable when the content pass hit it) is NOT retried within the run — it stays `prerequisite_missing` and needs a separate `retry_mode=failed_only` run. MAIN had no in-run heal either (these were plain FAILs there too), but B8's whole point was to add one; QA-3 removed the only thing feeding it.
- **Fix (suggested, for the dev — user-approved direction):** collect home-content units that fail `prerequisite_missing` during the content phase and **re-attempt their write once at end-of-content-phase, before the ACL phase** (best-effort; a short backoff optional), independent of the removed `defer` path. Explicitly accepted as **not 100% fool-proof** — owners whose home never materializes during the run still need a retry run — but it heals the lag cases in a single run, restoring B8's intended benefit on top of QA-3's optimistic write. Pair with Part A so the home-root row is not reported.
- **This run:** 0 home-content failures (156 same-account users provisioned instantly → no lag), so the gap did not bite here — but it is unexercised, not fixed. Do NOT mark home-content behavior fully validated until Part B is decided/implemented.

#### QA-6 (BUG, MED) — B6 parallelism: unsynchronized read/write race on `state_store._cache` drops ACLs under `parallel_threads>1` ("dictionary changed size during iteration")

- **Commit under test:** `e33b3263` (branch `plan13-backlog-b1-b13`), live run_id `987031501233917`, `parallel_threads=4`. Observed exactly one ACL failure: `acl | alertsv2:wsmig_test_alert_v2 | api_error | "dictionary changed size during iteration"`. **This error class does not appear in the MAIN-branch RRL report (0 occurrences) — it is a B6 regression.**
- **Root cause (confirmed by code trace):** `AclImporter` inherits `parallel_safe=True` (never overrides it), so with `parallel_threads=4` the runner executes ACL units concurrently. The ACL hot path `AclImporter.create_one → _resolve_target_object (acl_importer.py:364, "everything else" branch) → base_importer.target_id_map (base_importer.py:459) → state_store.target_ids_for()` **iterates `self._cache` WITHOUT a snapshot** (`state_store.py:620`: `{nk: … for (at,nk),r in self._cache.items() if …}`), while another worker finishing its unit calls `_record → state_store.record()` which **inserts a new key** (`state_store.py:369`: `self._cache[key] = row`). Growing the dict mid-iteration raises `RuntimeError`. `state_store.record()` is NOT lock-guarded (the base_importer lock at ~927 guards only the result counters, not the cache). Serial (`parallel_threads=1`) can never hit it (recording happens after the iteration returns).
- **Why `alertsv2`:** not special — a timing lottery. Every non-content, non-secret-scope ACL type (jobs, clusters, warehouses, DLT, dashboards, genie, alertsv2, legacy_alert, pools, policies) routes through `target_id_map` and is an eligible victim; content ACLs (resolve by path via `_get_status`) and secret-scope ACLs are immune. Next run it could be any of the others.
- **Impact:** non-deterministic; silently drops the ACL(s) that lose the race (reported `failed`, recoverable via `retry_mode=failed_only`). 1 of ~10,370 here (ACL parity still `missing_on_target=0` because the failed grant happened to re-read clean / was a counter), but at customer scale with threads>1 it can drop several grants/run → parity gaps that only a retry closes.
- **Fix (AGREED correctness fix — ship it; for the dev, NOT applied by tester):** snapshot before iterating at `state_store.py:620` — `for (at,nk),r in list(self._cache.items())` (the codebase already does exactly this at line 545). Apply the same `list(...)` snapshot to the sibling unsnapshotted `_cache` iterations at `state_store.py:291, 596, 611, 642` (same latent race on any parallel phase), or guard all `_cache` reads+writes with one lock. Overhead is negligible (one extra copy per call) except at extreme scale.
- **Deeper performance fix (separate):** the per-unit full-`_cache` rescan this race rides on is itself an O(N·M) cost at scale; building the per-asset-type id-map ONCE at ACL-phase start (O(1) lookups) removes BOTH the cost and the race surface — tracked in **`plans/PLAN_15_acl_idmap_build_once.md`**. Do the snapshot first; PLAN 15 is the optional scale follow-up.

#### QA-7 (BUG, CRITICAL) — the import's state LOAD returns 0 rows against a populated state table → incremental skip + UPDATE-detection + retry_mode all break; edits silently NOT migrated (stale target)
- **Commit under test:** `e33b3263` (branch `plan13-backlog-b1-b13`), live incremental Run-2 (`618917344635774`) on target `adb-7405610176410928.8`, + a `retry_mode=failed_only` repro run (`431909668211194`, killed).
- **Symptom (deterministic, reproduced):** on an incremental run the import logs `state loaded | rows=0 identity_rows=196` for the migration-state table — THREE times in Run-2 (15:57/16:02/16:08) AND again in the retry run — while the live table provably held **21,264 committed rows** for exactly `source_workspace_id='7405618632322944'` (Delta time-travel to the load instant; the SAME filter returns 21,276 from a SQL warehouse NOW; Delta HISTORY shows only CREATE/MERGE/OPTIMIZE — the rows were **never deleted**). The identity-map table (same `load()` code, same `WHERE source_workspace_id` filter, no `_dryrun` twin) loaded 196 correctly — so it is specific to the migration-state read.
- **Code path:** `04_Import` builds the backend via `build_sql_backend(cfg, spark=spark, …)`, which **prefers Spark** (`if spark is not None: return SparkSqlBackend(spark)`), so a Databricks job uses `SparkSqlBackend.sql` = `spark.sql(stmt)` → `try: [row.asDict() for row in df.collect()] except Exception: return []`. **It silently returns `[]` on ANY exception, logging nothing.** The migration `SELECT *` returned 0 on the job's Spark compute while the warehouse returns 21K — so the SELECT failed-and-was-swallowed (or returned empty) and the exact underlying error is **unrecoverable from the artifacts because the code discards it.** `SparkSqlBackend`, `StatementApiBackend`, `state_store.load/decide`, and `config_manager` dry_run/state-table are **byte-identical to main** — NOT a branch code regression; a pre-existing latent bug exposed now because this is the first bed whose state table is large (~21K rows: ~10K content + ~10K ACLs). (NOTE: the Statement-API backend has a SEPARATE, also-real bug — it reads only `result.data_array` chunk 0 and ignores `next_chunk_internal_link`; live-confirmed it returns 16,355 of 21,276 rows. That path is the test/off-cluster backend, not what the job used, but fix it too.)
- **Consequences (all live-verified this run):**
  1. **Incremental UPDATES silently dropped — stale target (data-correctness):** empty state → `decide()` can never return UPDATE → every existing object is "Adopted" (seen as present, NOT re-written). Verified on target: edited `py_nb`/`sql_nb`/`config.json`/`README.md` and cluster-policy `wsmig_test_policy` (autotermination=30) all retained **old Run-1 content** on target; source edits lost. A customer re-running after edits would silently not get them.
  2. **Full re-apply every run (perf):** 11,176 created + 9,654 adopted + only 377 skipped + **0 updated**; all ~10K ACLs re-applied + ~760 dirs re-created (~34 min; hours at RRL scale).
  3. **`retry_mode=failed_only` broken:** empty state → empty `retry_keys` → instead of retrying the ~10 failed units it re-applied everything (repro run wrote 6,825 state rows before being killed).
  4. **Amplifies QA-6:** re-applying ALL ACLs each run multiplies the race window (6 ACL `dictionary changed size` drops in Run-2 vs 1 in Run-1).
- **What DID land (not affected — new objects + identity):** additions (new notebooks/files, byte-verified), new user (Srikanth assigned), group change (Aman→admins), dashboard draft→publish (`wsmig_inc_dash_published` published). Only *updates to existing objects* are dropped.
- **Fix (for the dev — tester role, NOT applied):**
  1. **Fail loud:** `SparkSqlBackend.sql` must NOT swallow exceptions into `[]` — log + re-raise. A failed state load must never masquerade as an empty table.
  2. **Guard:** on a live (`dry_run=false`) import, if the state load returns 0 rows, cross-check with a `COUNT(*)` for the pair and ABORT loudly on a 0-vs-nonempty mismatch rather than silently re-applying/dropping updates.
  3. With (1) in place, surface + fix the actual `SELECT *`/`collect()` failure on large state tables (candidates to check: schema/column handling on the partitioned state table, `row.asDict()` behaviour, a schema-evolution ALTER mid-session).
  4. Fix the `StatementApiBackend` chunk-pagination gap (follow `next_chunk_internal_link`) so the off-cluster/test backend also handles >16K-row state tables.
- **Severity CRITICAL:** silently drops customer edits on every incremental re-run AND breaks the documented retry/repair path — the two things incremental migration exists to do. At customer scale (RRL ~1M state rows) it would fire on the first incremental.
