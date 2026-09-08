# PLAN 13 — Future backlog (living list of bugs, gaps & feature additions)

This is a **living backlog**. As we find new bugs, gaps, or feature requests during real runs, we add
them here, triage, and pull them into an implementation pass when we take them up. Nothing here is
implemented yet unless a line says otherwise. Related: [[PLAN_11_incremental_bugfixes]] (the last
fix pass, now fully validated) and [[PLAN_12_optional_scale_and_durability]] (optional hardening).

Severity legend: **HIGH** = correctness / data-loss; **MED** = parity / usability; **LOW** = polish.

---

## Backlog summary

| # | Type | Sev | Title | Status |
|---|---|---|---|---|
| B1 | Feature | MED | `source_run_as_spn` widget for airgap source inventory/export | OPEN |
| B2 | Gap | MED | Object **owner/creator** not preserved for several resource types (owned by run-as SP on target) | OPEN |
| B3 | Bug | HIGH | Cluster-policy create sends `policy_family_id` + `definition` together → 400 for family-based policies | OPEN |
| B4 | Bug | HIGH | Workspace-conf is written without read-back → reports parity from a 200, not the actual value (`enableExportNotebook` stayed `true`, reported `false`) | OPEN |

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

**Open questions to resolve when we take it up.**
- Does the airgap-source install run in the source workspace (so it can bind a source SP as `run_as`),
  or is `airgap_source` deployed by a source-side invocation of `00_Install_Jobs`? Confirm the deploy
  path so the widget lands on the right side.
- Same `servicePrincipal.user` role prerequisite as the target run-as (the creator needs it on the SP)
  — surface it as a clear preflight message rather than a raw 403.

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

**Proposed change (when taken up).**
- **Queries:** after create, set `owner_user_name` to the source owner (remapped) via the Queries
  update API — the field is settable (proven live in the PLAN 11 F8 seed). Exact-or-skip: if the owner
  is absent from the target roster, leave it and note it (don't fail).
- **Lakeview dashboards / Genie spaces:** determine whether their permissions API accepts an ownership
  grant (an `IS_OWNER`-equivalent or a transfer-ownership call); if so, capture + apply it like jobs;
  if not, record the source owner in a durable place and document the limitation (as clusters do).
- **Clusters:** keep the documented `OriginalCreator` tag (API limitation) — just make sure it's called
  out in the not-migrated catalog so it isn't a surprise.
- **Cross-cutting:** decide whether owner-remap should honour the orphaned-owner divert (a deleted
  source owner → don't set owner to a non-existent principal; leave run-as SP + warn).
- Add per-type regression tests + a live re-validation (a query/dashboard/genie whose source owner is a
  DIFFERENT in-roster user than the run-as SP → assert the target owner matches the source owner).

**Verify-first tasks.**
- Confirm which of Lakeview/Genie/alerts expose a settable owner via any API (some may genuinely have
  none, in which case the answer is "documented limitation," like clusters).
- Confirm jobs/pipelines/warehouses ownership actually lands end-to-end on a run where the source owner
  ≠ the run-as SP (Run 5 had the owner ≈ the operator; a cleaner test uses a distinct owner).

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
  `policy_family_id` and NOT `definition`; a custom-definition policy → the reverse. Live re-validate
  against a workspace that has a custom family policy.

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

**Proposed change.**
- **Read-back verify** after a mutating call whose effect is not proven by a returned object id:
  GET the resource (or the specific key/grant), compare to the intended value, and record **FAILED /
  "not applied"** — with the observed vs intended value — when they differ. Never record `Created`/
  `Updated` on an unverified declarative write.
- **Do not store the state fingerprint until the value is verified**, so a re-run re-attempts an
  unapplied unit instead of skipping it (otherwise the gap is self-concealing).
- **Route keys the legacy endpoint silently ignores through the Settings API** — start with
  `enableExportNotebook` (verify live: `PATCH workspace-conf {"enableExportNotebook":"false"}` then
  GET it back; if still `true`, it must go through the Settings API / admin security policy).
- Add a **workspace-conf parity table** to the report (source value vs live target value vs status,
  for all 9 keys) and, more broadly, a **verify-and-diff pass** so the report reflects the target's
  ACTUAL post-run state rather than the tool's intent.
- Also note the scope limit surfaced here: only the 9 hard-coded keys in `misc_collector._WS_CONF_KEYS`
  are collected at all; any other workspace-conf setting on source is never migrated. Widen or
  document the set.

**Verify-first tasks.**
- Determine which conf keys the legacy `workspace-conf` PATCH actually honours vs silently drops
  (test each of the 9 with PATCH→GET), so the fix knows which ones need the Settings API.
- Decide the report vocabulary for "sent but not verified applied" vs a hard API error — both should
  read as NOT parity, distinct from a clean `Created`.
