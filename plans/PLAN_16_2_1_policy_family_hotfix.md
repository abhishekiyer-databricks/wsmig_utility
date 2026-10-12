# PLAN 16.2.1 — Hotfix: policy-family cluster policies (release 1.1.1)

**Status:** IMPLEMENTED offline 2026-10-12 on `hotfix/policy_family` (uncommitted) — Run 1 reproduced live; awaiting user push → Runs 2–3. Urgent: the customer is blocked. The user pulled B3 out of 16.3 to ship
it on its own first.
**Base:** `main` @ `c3421bb` (1.1.0). **Branch:** `hotfix/policy_family`, cut from `main`. Claude codes and tests; the
user reviews, commits, pushes and merges.
**Scope:** B3 only. Nothing else changes: no fingerprint, natural key, state-schema or job-template change. PLAN 16.3
drops B3 (shipped here).

## 1. The customer's situation (what this release must fix)
1. They ran the utility. A cluster policy built on a **policy family** (theirs: `cust-pl-stg-job-compute-jobs`, a Job
   Compute family policy) failed: `POST …/policies/clusters/create -> 400: INVALID_PARAMETER_VALUE policy_family_id and
   definition cannot be used together`.
2. Every job and cluster that uses that policy then failed too: `prerequisite_missing — references cluster_policy
   <name> which is in the bundle but not yet created on target … re-run with retry_mode=failed_only`
   (`BaseImporter.require_remap`). The policy's permissions were left `skipped_no_object`.
3. The state table recorded all of these as `failed` (the permissions as `skipped_no_object`).

**What they will do after the fix:**
1. Pull 1.1.1 into the existing Git folder (no reinstall).
2. Run the existing **import** job with `retry_mode=failed_only`, `dry_run=false`, and their last `run_id` (blank also
   works: it resolves to `LATEST_EXPORT`).

The fix is on the import side only, so their **existing bundle** works as-is: no re-inventory and no re-export.

**Why `failed_only` heals the whole chain in one run** (all already in 1.1.0):
- The compute phase runs before jobs.
- The policy (failed) is created first and its target id is published to the run context. The dependent jobs and
  clusters (failed, `prerequisite_missing`) are then re-attempted and their policy reference now resolves.
- The 1.1.0 rule "permissions follow an object healed in this run" adds the policy's and the jobs' permission units
  to the work list, so their permissions are applied in the same run.

## 2. Cause
`compute_importer.py`:
- **Create** (`_create_policy`, `:137-149`) copies every present field into the body. A family policy's GET/list
  carries `policy_family_id` + `policy_family_definition_overrides` **and** the resolved `definition` (family + overrides
  merged), so the body has both, which the API refuses.
- **Edit** (`update_one`, `:112-117`) sends `name` + `definition` only. For a family policy that would turn it into a
  custom policy, or fail.

## 3. Change
New `ComputeImporter._put_policy_shape(body, payload)`, taken from the `plan13-backlog-b1-b13` branch. Use its
docstring, but keep `main`'s DEBUG line in `_remap_policy_definition` (the branch removed it):
- `payload.policy_family_id` set → `body.policy_family_id` (+ `policy_family_definition_overrides` when present). **No
  `definition`.**
- Otherwise, when `definition` is present → `body.definition` (custom policy, exactly as today).

Callers:
- `_create_policy`: name, then `_put_policy_shape`, then `description`, `libraries`, `max_clusters_per_user` as today.
- `update_one` (`cluster_policy`): `{policy_id, name}`, then `_put_policy_shape`.

The pool-id remap (`_remap_policy_body_ids`) already rewrites ids inside `policy_family_definition_overrides`, so it is
unchanged.

**Verified live** (PLAN_16_3 §0 V4, 2026-10-09; create 2026-10-01):
- create and edit with family id + overrides and no definition → 200;
- family id + definition → 400;
- the bundle payload already carries `policy_family_id` + overrides (that's why the 400 says "used together").

**Upgrade path.**
- The failed state rows re-decide to CREATE on `failed_only` or on a normal run (FAILED never stores a fingerprint).
- Custom policies: byte-identical request bodies.
- No fingerprint change, so nothing else is re-touched.

## 4. Offline tests (`tests/test_plan16_2_1.py`)
1. Family policy create: the body has `policy_family_id` + overrides and **no** `definition`; name and description
   kept.
2. Custom policy create: the body is byte-identical to 1.1.0's (assert against the old field loop's output).
3. Family edit: `{policy_id, name, policy_family_id, overrides}`, no `definition`. Custom edit unchanged.
4. A pool id inside the overrides is remapped to the target pool.
5. Family-only policy (no overrides) → `policy_family_id` only.
6. **The customer chain, end to end on fakes:**
   - a 1.1-shaped state (policy `failed` with the 400; job `failed` with `prerequisite_missing`; the policy's and the
     job's ACL rows `skipped_no_object`) plus the bundle;
   - `retry_mode=failed_only` → policy created with the family body, the job created with the **target** policy id in
     its job cluster, both ACL units applied;
   - a unit not in the retry list is not touched.
7. Fingerprint of a family `cluster_policy` unit is unchanged versus 1.1.0 (export side untouched).
8. Mutation check: revert the fix → tests 1, 3 and 6 fail. Record this in §7.
9. Full suite `python3 -m pytest` passes (497 + new).

## 5. Live QA (Claude) — current setup, classic job cluster, LIVE + direct
Target `7405614409585920`, state `catalog_3_5djjet.wsmig_state_16_2`, Git folder
`/Users/abhishek.iyer@databricks.com/wsmig_cust` (today at released `main`), and the installed jobs from 16.2 Run D.
That state already holds the 1.1.0 failure for `wsmig_test_policy_family` (the same 400) and its ACL
`skipped_no_object`. Reports go to `~/Desktop/wsmig_runs/plan16_2_1/`. Stop after each run.

- **Q0 — the user re-logs `source_ws`** (`databricks auth login --profile source_ws`; the refresh token expired).
- **Q1 — fixture (source, test code in `tests/fixtures_medium.py`).** New phase `policy_family_job`, idempotent and
  in `all` after `b3_policy_family`. It mirrors the customer:
  - `wsmig_test_policy_family_job` from the **Job Compute** family (`job-cluster`; confirm the id with
    `policy-families list`) with one override;
  - job `wsmig_test_policy_family_dep_job`: one notebook task on `/Shared/wsmig_test/py_nb`, a job cluster with
    `policy_id` = that policy, `apply_policy_default_values=true`;
  - 2–3 ACL grants on the policy and on the job (re-run the `acls` phase for them).

  Verify on the source with GET (family id + overrides; the job's `policy_id`).
- **Run 1 — reproduce the customer on 1.1.0.** Run the installed `wsmig - direct end-to-end (LIVE)` job (Git folder
  still at `main`) on the existing state. Expect:
  - both family policies `failed` with the 400;
  - the new job `failed — prerequisite_missing` naming the policy;
  - their ACL rows `skipped_no_object`;
  - everything else SKIP (incremental).

  Save the reports, the state rows and the target counts. **Stop** and show the user it matches the customer.
- **User:** reviews, commits and pushes `hotfix/policy_family`. **Claude:** moves the same Git folder to that commit
  (pull only, no reinstall).
- **Run 2 — the customer's fix path.** Standalone `wsmig - import`, `retry_mode=failed_only`, `dry_run=false`,
  `run_id` = Run 1's bundle. Expect:
  - **both family policies `created`.** A live GET shows `policy_family_id` + the source overrides, and the
    effective definition equals the source's;
  - **the dependent job `created`**, with its job cluster's `policy_id` = the new target policy (live GET);
  - **ACLs of both policies and the job applied** in this run; live `GET permissions` = source (remapped), and the
    ACL Parity sheet says `match`;
  - only failed units acted on. The other by-design failures are re-attempted and stay failed, with unchanged reasons:
    `wsmig_test_cluster_policied`, `wsmig_test_ext_endpoint`, any home delay;
  - no duplicates: policy and job counts each +the healed ones only;
  - `logcheck.py` clean; the report goes to `import_status_retry_<ts>.xlsx` in Run 1's folder.

  **Stop.**
- **Run 3 — normal run + update.** Seed on the source: change the override on `wsmig_test_policy_family_job` (e.g.
  `autotermination` value or a `spark_version` constraint). Run the e2e job. Expect:
  - that policy `updated` through the family-shape edit (live GET shows the new override, still a family policy);
  - everything else SKIP;
  - no duplicates.

  **Stop** → FINDINGS verdict, ready to release.

Not covered live: a family policy that pins a pool id in its overrides (offline test 4).

## 6. Release
- `VERSION` = `1.1.1`.
- `CHANGELOG.md` `## [1.1.1] - <date>`:
  - **Fixed:** cluster policies built on a policy family (e.g. Job Compute, Personal Compute) failed to create
    (`policy_family_id and definition cannot be used together`) and blocked the jobs and clusters that use them.
  - **Upgrade note:** pull into the existing Git folder, then run the import job with `retry_mode=failed_only`,
    `dry_run=false` and your last `run_id`. The policy is created first, then the jobs/clusters that use it, then their
    permissions, all in that one run. No reinstall, no re-export.
- `docs/RUNBOOK.md`: one line in the retry section.
- The user merges to `main`. Customer message (Claude drafts): the steps above, plus how to confirm (the policy and
  jobs `Created` in `import_status_retry_<ts>.xlsx`).

## 7. Dev notes (2026-10-12, Claude)
- `src/importers/compute_importer.py`: new `_put_policy_shape`, called by `_create_policy` and the `cluster_policy` branch
  of `update_one`. Custom-policy bodies keep 1.1.0's field order (byte-identical, test 2). `main`'s DEBUG line in
  `_remap_policy_definition` kept.
- `tests/test_plan16_2_1.py`: 10 tests. `ChainTarget` (extends 16.2's `FakeTarget`) enforces the live API rule, so a
  body with family_id + definition gets the real 400.
- **Mutation check:** with 1.1.0's `compute_importer.py` → **6/10 fail** (all body-shape tests + both customer-chain tests:
  policy failed, both jobs `FAILED prerequisite_missing`, ACLs untouched — the customer's exact state). The 4 that pass on
  both are the compatibility guards (custom create/edit unchanged, export fingerprint unchanged, fake sanity).
- Full suite: **507 passed** (497 + 10).
- Fixture (test code): `tests/fixtures_medium.py` phase `policy_family_jobs` (in `all`): `wsmig_test_policy_family_job`
  (Job Compute family, 1 override) + 3 jobs — `wsmig_test_polfam_job_nb` (job cluster), `…_multi` (2 tasks, shared job
  cluster), `…_newcluster` (task-level `new_cluster`) — + ACLs on all four.
- Docs: CHANGELOG `[1.1.1]`, VERSION `1.1.1`, Runbook retry-section note.

## 8. Findings
### Run 1 — 1.1.0 reproduces the customer (2026-10-12) — PASS
Fixture built on source (policy `00139AD5952831B1` family `job-cluster`; jobs 239237525041733 / 877219713881027 /
379208038239421, each `policy_id` = it). Installed `wsmig - direct end-to-end (LIVE)` (Git folder at 1.1.0) → job run
58264920787453 SUCCESS, bundle `20261012_073721`. State (`wsmig_state_16_2`):
- `cluster_policy` `wsmig_test_policy_family` + `wsmig_test_policy_family_job` → **failed**, `400 INVALID_PARAMETER_VALUE
  policy_family_id and definition cannot be used together`;
- `job` `wsmig_test_polfam_job_nb` / `_multi` / `_newcluster` → **failed**, "`a job cluster` references cluster_policy
  `wsmig_test_policy_family_job` which is in the bundle but not yet created on target … re-run with retry_mode=failed_only";
- their 5 ACL rows → `skipped_no_object` ("the object FAILED to import earlier in this run … the ACL follows");
- target: no family policy, no `polfam` job (18 jobs). Evidence `~/Desktop/wsmig_runs/plan16_2_1/run1/`.

### Run 2 — 1.1.1 `failed_only`, the customer's fix path (2026-10-12) — PASS
Git folder `wsmig_cust` moved to `hotfix/policy_family` @ `d844e72` (pull only, no reinstall). Standalone `wsmig - import`
(job 554860399019081), `retry_mode=failed_only`, `dry_run=false`, `run_id=20261012_073721` → job run 938905596072973
SUCCESS, `run_status=completed`. Report `import_status_retry_20261012_085422.xlsx` (+ json/md) in Run 1's folder; canonical
`import_status.xlsx` untouched.
- Work list = 15 units: **10 created** — 2 family policies, the 3 dependent jobs, their 5 ACLs (joined via "ACLs follow a
  healed object") — and 5 by-design failures re-attempted with unchanged reasons (`wsmig_test_cluster_policied`, 3
  libraries on TERMINATED clusters, `wsmig_test_ext_endpoint`). Nothing else touched.
- Live vs source (`run2/live_verify.json`): both policies family + overrides + effective definition **equal**
  (`job-cluster`, `personal-vm`); all 3 jobs' `policy_id` = the new target policy `000D08F53F9F2D84` (job cluster, 2-task
  shared cluster, task `new_cluster`); ACLs on all 5 objects **equal** to source (incl. job `IS_OWNER`).
- State rows: all 10 `created` with target ids. No duplicates: target jobs 18 → 21, policies 7 → 9, all names unique.
- State roll-up: `failed` 5 (the by-design ones only).

### Run 3 — normal e2e after a source edit to the family policy (2026-10-12) — PASS
Seed (user-approved): source `wsmig_test_policy_family_job` override `custom_tags.wsmig_policy` `family_job` →
`family_job_v2` (still family `job-cluster`). `wsmig - direct end-to-end (LIVE)` on 1.1.1 → job run 996922402993745
SUCCESS, bundle `20261012_092155`, 720 units, `run_status=completed`.
- `cluster_policy wsmig_test_policy_family_job` → **updated** via the family-shape edit: same target id
  `000D08F53F9F2D84`, still `job-cluster`, override now `family_job_v2`; overrides + effective definition = source.
- The other family policy, the 3 jobs (still pointing at `000D08F53F9F2D84`) and all 5 ACLs → **skipped** (unchanged);
  live ACLs still = source.
- Roll-up: skipped 643 · updated 2 · adopted 1 · created_with_warning 2 (the 2 dashboard schedules' pending-subscriber
  re-check, by design) · manual 9 · skipped_no_object 58 · failed 5 (the same by-design ones). No duplicates: jobs 21,
  policies 9, names unique.
- **Observation (pre-existing, not this hotfix):** `alert_v2 wsmig_test_alert_v2` reads `updated` with no source edit.
  It did on 1.1.0 too (Run 1: both alerts V2 `updated`), so it's not caused by this release (no alert code touched).
  → root-caused 2026-10-12: the nested read-only `evaluation.last_evaluated_at` / `evaluation.state` are in the
  fingerprint, so every scheduled evaluation on the source looks like an edit. Planned as PLAN_16_3 §7c (QA16.2.1-1).

### Verdict — 1.1.1 READY TO RELEASE
The customer's chain heals with one `failed_only` run (Run 2), a later edit of a family policy updates it in place
(Run 3), custom policies and everything else are unchanged, and there are no duplicates.
