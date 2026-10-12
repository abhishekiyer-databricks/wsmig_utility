# PLAN 16.3 — Customer fixes, wave 2 (release 1.2.0)

**Status:** DRAFT FOR USER REVIEW 2026-10-09. Master: [PLAN_16_master.md](PLAN_16_master.md) (16.3 row, rescoped by the
user 2026-10-09: "Option A").
**Base:** `main` @ `c3421bb` (1.1.0, live-certified). **Branch:** `feature/16_3_customer_fixes`, cut from `main`.
**Roles:** a **dev session** builds the code and offline tests from this file alone (plus `CLAUDE.md`, the master plan
and project memory). **Claude (this session)** is master and reviewer: reviews the dev diff, then runs the live QA in
§11. **The user** reviews, commits and pushes; Claude never commits.

**Scope: nine items, in the user's priority order** (7b added by the user 2026-10-12 after the customer's Genie failures; 7c added by the user 2026-10-12 after the 1.1.1 QA):

| § | Item | One line |
|---|---|---|
| 1 | **B9** run-id folder | The bundle folder is named after the Jobs-UI job run id, not a timestamp. A repair run writes into the same folder. |
| 2 | ~~**B3** policy family~~ → **hotfix 1.1.1** (PLAN_16_2_1) | Cluster policies built on a policy family create and update instead of failing with a 400. |
| 3 | **B4** workspace-conf read-back | Each workspace setting is read back after it is written. If the target didn't change, the row is `failed — not applied`, never `Created`. |
| 4 | **B11** report write | If `import_status.xlsx` (or the results JSON / runbook) doesn't land, the task goes red with `completed_no_report`. |
| 5 | **B2** query owner | A migrated SQL query gets its source owner instead of staying owned by the migration SP. |
| 6 | **CUST-1** pipeline ACLs | When the platform refuses the pipeline-owner change, the ACL is re-sent with the target's current owner, so the other grants still land. |
| 7 | **CUST-2** synced-table pipelines | Pipelines the API can't create (no source code: synced-table pipelines) become a `manual` step with a clear note. The tool never POSTs them. |
| 7b | **QA16.2-1** wrong "folder missing" cause | The "folder does not exist" / "not published" messages are shown only after a live check confirms them; otherwise the server's own error is shown. |
| 7c | **QA16.2.1-1** alerts "updated" every run | An Alert V2 that ran on its schedule on the source is no longer reported (and PATCHed) as `updated` when nothing about it changed. |

**Not in this wave** (back to the backlog, master "Backlog after 16.3"): 16.4 (notification destinations + catalog
rename), B8 home re-sweep, B1, B12, W0-6, W0-7, QA16.2-2…4, the wider B4 audit of every other mutating call.

**Default-behaviour rule (ground rule 1).**
- Apart from the nine items, behaviour stays byte-identical to 1.1.0: the same decisions, natural keys and state
  schema.
- **No fingerprint changes**, except the one stated in §7c (Alert V2 only, a one-time `updated`). No other unit's
  fingerprint may move, otherwise the first upgraded run would mass-UPDATE. Each item below states how it reaches
  already-migrated objects without one.
- **Upgrade rule (ground rule 7).** The customer runs 1.0.0/1.1.0 today. Every item states what a 1.x-written state
  row, bundle or *installed job* does on the first run after a pull, and has an offline test that seeds that shape.

---

## 0. Verified live 2026-10-09 (Claude) — the dev builds on these facts, no further probing needed
Evidence: `~/Desktop/wsmig_runs/plan16_3/api_probes/2026-10-09_premises.md`. Probes ran on scratch objects that were
deleted afterwards.

| # | Fact | Used by |
|---|---|---|
| V1 | `base_parameters {"job_run_id": "{{job.run_id}}"}` gives every task the **job run id** (e.g. `818095769358527`). A **Repair run** gives the repaired task the **same** value, and `taskValues.get(taskKey="t1")` still returns the value set by the first, not-re-run task. | §1 |
| V2 | **Job run ids are NOT time-ordered.** One daily job got `298288963013477`, `612344363020761`, `345818061134240`, `570225755438119`, `655319886577201` on 5 consecutive days. 16.2 QA Run A `640341627917106` came before Run B `226010421041587` (same job). So "newest = highest name" breaks once folders are named by run id. | §1c |
| V3 | `os.path.getmtime` on a `/Volumes/...` file returns its write time (the Files API also returns `last_modified` for files). | §1c |
| V4 | Cluster-policy **edit** with `{policy_id, name, policy_family_id, policy_family_definition_overrides}` → 200. With `policy_family_id` + `definition` → 400 `policy_family_id and definition cannot be used together`. Create has the same rule (verified 2026-10-01, PLAN_13 B3). | §2 |
| V5 | `GET /api/2.0/sql/queries` (the LIST) returns `owner_user_name` for every query. Today every migrated target query is owned by the runner SP (5/5 on the current target). Query permission levels are `CAN_READ/RUN/EDIT/MANAGE` (no `IS_OWNER`); the owner shows up as an *inherited* `CAN_MANAGE`. The owner PATCH `{query:{owner_user_name}, update_mask:"owner_user_name"}` changes the owner (verified 2026-10-01, PLAN_13 B2). | §5 |
| V6 | The shared sandbox metastore's owner is the `account users` group, so every account identity, including our runner, is a metastore admin. **CUST-1's 403 cannot be reproduced on this bed**; that path is covered offline. | §6, §11 |
| V7 | On a pipeline, `PUT permissions` with **no `IS_OWNER` entry → 400 `The pipeline must have exactly one owner.`** A PUT with `IS_OWNER` = the **current** owner plus the other grants → 200: the owner is unchanged and the grants are exact. So "just leave the owner out" (the master plan's old option (a)) does **not** work. | §6 |
| V8 | Workspace-conf GET returns string values (`"true"`, `"false"`, `"30"`) or `null` for unset keys. Re-verified PATCH→GET read-back is immediate (PLAN_13 B4, 2026-10-01). Whether a key is silently dropped depends on the workspace: `enableWebTerminal` was dropped on one, `enableExportNotebook` on the customer's. | §3 |
| V9 | `jobs/*.job.json` token substitution only matches `{{UPPER_CASE}}` (`job_templates._TOKEN`), so a lowercase `{{job.run_id}}` passes through `render_template` untouched and is **not** a leftover-token error. A non-blank template value is a pin the installer never overwrites. | §1a |

---

## 1. B9 — the bundle folder is named by the job run id
**Today.**
- A blank `run_id` widget mints `YYYYMMDD_HHMMSS` (`config_manager.py:382`). `01_Inventory` publishes it as a task
  value, and export/import read it back (`02_Export.py:108-120`, `04_Import.py:163-174`).
- The Jobs UI shows run `#681909549084095` while the folder is `20261006_033735`. Nothing links the two, so an operator
  can't find a run's reports or know which `run_id` to pass to a standalone `failed_only`.

**1a. Templates + widget.**
- Add `"job_run_id": "{{job.run_id}}"` to the **inventory task's** `base_parameters` in every template that has one:
  `direct_end_to_end_live`, `direct_end_to_end_dry_run`, `inventory`, `airgap_source`.
- Do **not** add it to export/import tasks. A standalone export/import job is its own job run, so its job run id is
  *not* the bundle's id.
- `01_Inventory` gets `dbutils.widgets.text("job_run_id", "", "Job run id (set by the job — leave blank)")`.
- The installer needs no code change (V9). Add a test that `render_template` keeps the pin, adds no other key, and
  doesn't raise.

**1b. Resolution** (`bundle_state.resolve_inventory_run_id`, new arg `job_run_id=""`). Precedence:
1. explicit `run_id` widget → `widget` (unchanged);
2. **`job_run_id`**, if it matches `^\d+$` → `job-run-id`. Anything else (blank, a literal `{{job.run_id}}` from an
   interactive copy) is ignored with a DEBUG line. **A job run id beats "resume the newest incomplete run"
   (decision D2):** a new *Run now* gets a new folder, and resuming is what *Repair run* is for (V1);
3. newest incomplete run → `resume` (unchanged; only reached without a job run id);
4. fresh timestamp → `fresh` (unchanged; interactive runs).

The notebook prints `Run id : 681909549084095 (resolved via: job-run-id)`. The task-value publish is unchanged, so
export and import in the same job use the same id with no change.

**1c. Run-folder ordering by time, not by name** (needed because of V2).
- `bundle_state.list_run_ids` sorts newest-first by **time**: the max `os.path.getmtime` of whichever of
  `misc/{checkpoint,manifest,inventory,import_results}.json` exist, else the directory's own mtime, else 0. Ties break
  by name, descending. Update the module docstring (line 14 says timestamps sort lexicographically).
- **Resume considers only the newest run.** This stops a stale, abandoned folder from hijacking a later run. Abandoned
  folders become common once a new *Run now* no longer resumes (D2). Today's newest-first scan returns the *first*
  incomplete folder, even one older than the latest completed run.
  - Inventory/export resume: only the newest run folder is a candidate. Resume it if it's incomplete (checkpoint
    present, no manifest); otherwise fall through to the pointer.
  - Import resume (`import_runner.resolve_import_run_id`): only the newest run folder **that has a manifest** is a
    candidate. Resume it if its import started and didn't finish (unchanged test); otherwise use `LATEST_EXPORT.json`.
- **Mixed folders** (old `2026…` timestamp folders next to new numeric folders) order correctly because only times
  are compared.

**1d. Actionable manifest-missing error** (`ImportRunner.verify_bundle`).
- When the manifest itself is absent, the message must say: bundle `<run_id>` has no `manifest.json`, so its export
  did not finish.
- It must name the last completed export from `LATEST_EXPORT.json`: "pass run_id=`<id>`, or Repair the job run so
  its export finishes".
- Missing/mismatched *files* keep today's message.
- Reuse `ImportRunner.verify_bundle` and its test from the `plan13-backlog-b1-b13` branch.

**Upgrade path.**
- Jobs installed by 1.0/1.1 have no `job_run_id` parameter, so they keep timestamp folders: pull-only upgrades behave
  exactly as today.
- To switch on run-id folders, the customer re-runs `00_Install_Jobs` (create-or-reset by name: same job ids, history
  kept). Decision D4.
- Old timestamp folders stay readable (explicit `run_id`, `LATEST_*` pointers) and order correctly next to new ones
  (1c). The state table doesn't depend on the folder name.

**Tests** (`tests/test_plan16_3.py`):
- precedence: widget > job_run_id > resume > fresh;
- non-numeric or `{{…}}` job_run_id ignored;
- a job run id is used even when an older incomplete run exists (D2);
- `list_run_ids` orders by mtime: numeric ids out of order, mixed with timestamp folders;
- resume ignores an old abandoned incomplete folder when a newer complete one exists (inventory, export and import);
- the newest folder incomplete → resumed (unchanged);
- the manifest-missing message names `LATEST_EXPORT`'s run id;
- every inventory-carrying template has the pin, and no export/import task has it;
- `render_template` keeps it and doesn't raise;
- 1.1-shaped installed job params (no `job_run_id`) → the timestamp path is unchanged.

---

## 2. B3 — policy-family cluster policies
> **MOVED OUT 2026-10-12 (user, urgent): shipped alone as hotfix 1.1.1, `PLAN_16_2_1_policy_family_hotfix.md`.** 16.3 is built on top of 1.1.1 and skips this section; its QA still re-checks the policy rows in the golden diff.

**Today.**
- `compute_importer._create_policy` (`:137-149`) copies `definition` **and** `policy_family_id` into the create body,
  so every family-based policy fails with a 400.
- `update_one` (`:112-117`) sends `definition` only, which for a family policy either 400s or silently turns it into a
  custom policy.
- The fixture `wsmig_test_policy_family` has FAILED on every QA run so far.

**Change.** Take `ComputeImporter._put_policy_shape(body, payload)` from the `plan13-backlog-b1-b13` branch (its diff
is in that branch) and call it from both create and edit:
- `policy_family_id` present → send `policy_family_id` + `policy_family_definition_overrides` and drop `definition`;
- otherwise → send `definition`.

`description`, `libraries` and `max_clusters_per_user` stay as today on create. The pool-id remap
(`_remap_policy_body_ids`) already covers `policy_family_definition_overrides`.
- Keep `main`'s DEBUG line in `_remap_policy_definition`. The branch version deleted it; don't copy that part.

**Upgrade path.** Family policies have a FAILED state row, and FAILED never advances the fingerprint. So the next
normal run (or `failed_only`) re-decides CREATE and heals them. Custom policies: the request body is byte-identical
(test). No fingerprint change.

**Tests:**
- family create body has family_id + overrides and no `definition`; custom create body is byte-identical to 1.1.0;
- family edit body (V4 shape) and custom edit unchanged;
- a pool id inside the overrides is remapped;
- a 1.1-shaped FAILED row + family unit → CREATE with the correct body.

---

## 3. B4 — workspace-conf read-back (the report must be the truth)
**Today.**
- `misc_importer._set_conf` (`:293-310`) PATCHes one key and returns `note="<key> set to '<v>'"` from the HTTP 200
  alone.
- The unit is recorded `Created` with its fingerprint, so every later run SKIPs it, even when the platform ignored the
  write.
- That is exactly the customer's case: `enableExportNotebook` reported `false`, while the target stayed `true`.

**3a. Read-back.**
- After the PATCH, `GET api/2.0/workspace-conf?keys=<key>` and compare: `str(v).strip().lower()` on both sides,
  `None` = `""`.
- On a mismatch, raise the existing `NotApplied` (16.2, category `not_applied`) with: "`<key>` was set to `<intended>`
  and the API returned 200, but the target still reads `<observed>` — this workspace ignores it through the
  workspace-conf API. Set it in the admin console, then re-run."
- FAILED never advances the fingerprint, so every run re-attempts it and the report stays red until the target really
  matches.
- On a match: `note="<key> set to '<v>' (verified by read-back)"`.
- Take `_read_conf_key` from the branch. Don't add `verify_applied` / `VerificationFailed`: `NotApplied` already does
  the job.

**3b. Heal the 1.x false "Created" rows** (needed for the upgrade).
- `existing_keys` already reads every known key's live target value (`:79-86`). Keep it in `self._target_conf`.
- Implement `recheck_unchanged(unit, row)` for `workspace_conf`: return True when the live target value differs from
  the unit's source value. The SKIP then becomes ADOPT → `_do_declarative` → `_set_conf` → PATCH + read-back.
- Matching keys stay SKIP with zero writes.
- **Decision D3:** this also puts back a setting someone changed by hand on the target. Recommended, because it's the
  only way the customer's existing wrong row ever gets fixed or reported.
- Dry run intent: "would set `<key>` to `<v>` (target currently `<observed>`)".

**Upgrade path.**
- First upgraded run: keys whose target value already matches → SKIP, no write.
- A key that drifted, or was silently dropped earlier → re-PATCH + read-back. The result is `created` (verified), or
  `failed — not_applied` where the workspace really ignores the key. The customer's `enableExportNotebook` row turns
  truthful.

**Tests:**
- match → created with the "(verified by read-back)" note;
- mismatch → FAILED `not_applied` with intended + observed, and no fingerprint stored;
- a 1.1 row `created` + same fingerprint + target differs → recheck → PATCH + verify;
- same but target matches → SKIP, zero PATCH calls (assert on the fake client);
- normalisation (`"False"` vs `"false"`, `null` vs `""`);
- an unknown key is still `PrerequisiteMissing` (unchanged);
- dry run makes no PATCH.

---

## 4. B11 — a failed report write turns the task red
**Today.**
- `write_import_reports` (`import_report.py:122-210`) swallows each artifact's failure as a WARNING: the results
  JSON, the xlsx and the runbook md.
- `ImportRunner.run` (`:279-282`) swallows anything else the same way.
- So a clean-looking green run can leave no `import_status.xlsx`.

**Change.**
- **4a.** `write_import_reports` still writes every artifact it can, so one failure doesn't stop the others.
  - It now logs each failure at **ERROR** with the traceback, the artifact and the path.
  - It returns `written["errors"] = [{"artifact", "path", "error"}]`.
  - After writing, it checks that each written file exists on the Volume with size > 0
    (`os.path.getsize(aw._abs(rel))`); a missing or empty file is added to `errors`.
- **4b.** `ImportRunner.run` `finally`:
  - If the report has errors, or the writer raised, **and** `run_status == "completed"` → set
    `run_status = "completed_no_report"` (new constant `RUN_STATUS_NO_REPORT`) and log ERROR.
  - `completed_state_not_saved` keeps priority (it is more serious); the report error is still logged at ERROR.
  - On an aborting run the original exception is still re-raised; the report failure is logged at ERROR, never
    WARNING.
- **4c.** `04_Import` raises at the end when `run_status == completed_no_report`, next to the existing
  `completed_state_not_saved` raise. The message names the missing files and says: "the target WAS changed and the
  state WAS saved; re-run the import with run_id=`<id>` (it replays from the checkpoint and rewrites the reports) —
  see the driver log for the error".
- Out of scope: inventory/export report writes.

**Upgrade path.** None needed: no state or bundle change.

**Tests:**
- the xlsx render raises → `completed_no_report`, an ERROR log line, and json + md still written;
- the file is written but has size 0 → `completed_no_report`;
- a clean write → `completed`, byte-identical summary keys;
- an aborting run whose report write also fails → the ORIGINAL exception is re-raised;
- `completed_state_not_saved` + a report failure → `completed_state_not_saved`;
- the notebook text contains the new raise (grep test, as for `completed_state_not_saved`).

---

## 5. B2 — legacy SQL queries keep their source owner
**Today.** `owner_user_name` is read-only at create and stripped from the payload (`transforms.py:95`), so every
migrated query is owned by the run-as SP (V5).

**5a. Export.**
- `_sql_units` (`asset_export.py:733`): the `legacy_query` unit gets a top-level field
  `source_owner = raw["owner_user_name"]`, which the LIST already returns (V5).
- It goes in via `extra=` only: **not** in the payload and **not** in the fingerprint (decision note below).
- The GET-by-id enrichment needs no change.

**5b. Import** (`SqlImporter`).
- **Desired owner.** `resolve_principal(source_owner, "user" if "@" in it else "service_principal")`.
  - No owner on the source → nothing to do.
  - An owner whose home is absent from the source roster (`_roster_status(owner) == "absent"`, a deleted user) → no
    PATCH, and a note: "source owner `<x>` is not on target — left owned by the migration SP".
  - The same applies to `/Users_Backup`-diverted queries.
- **`_set_query_owner(unit, qid)`.** PATCH `{query:{owner_user_name: desired}, update_mask:"owner_user_name"}`, then
  GET `api/2.0/sql/queries/<qid>` and compare.
  - Match → note `owner set to <x> (verified)`.
  - PATCH error or read-back mismatch → **warning** (`created_with_warning` / updated-with-warning) naming the owner
    and the observed value: "query left owned by the migration SP". The query itself exists, so this is not a
    failure.
  - Reuse `_set_query_owner` from the branch, but use 16.2's patterns (no `verify_applied`).
- **Where it runs:**
  - after `_create_query`;
  - in `update_one` for `legacy_query`, after the content PATCH, only when the owner differs;
  - and for unchanged queries via `recheck_unchanged`:
    - `existing_keys` keeps `self._target_owner = {target_id: owner_user_name}` from the LIST it already makes (zero
      extra calls);
    - `recheck_unchanged` returns True when the desired owner is resolvable and not absent, and differs from the
      owner on the stored target id.
- **The ADOPT path.** `legacy_query` joins `reconcile_on_adopt_types`, and `adopt_one(unit, target_id)` must:
  1. keep 1.1.0's stale-update rule: a state row whose fingerprint moved → `update_one` (which also sets the owner) →
     `changed=True`;
  2. otherwise set the owner only if it differs → `changed=True` and note "owner set to <x>";
  3. otherwise do nothing → `adopted`.

  Test (1) explicitly: it is the rule the base ADOPT branch applied before the type joined `reconcile_on_adopt_types`.
- Dry run intent: "would set owner to `<x>`".

**Why no fingerprint change:**
- Folding the owner into the fingerprint would UPDATE every already-migrated query (a content PATCH each) on the first
  upgraded run.
- The live compare touches only queries whose owner is actually wrong, and it also catches a later owner change on the
  source.

**Out of scope** (documented in the Runbook): dashboards and Genie spaces (no owner API, PLAN_13 B2), clusters
(creator fixed), alerts V2. Jobs, pipelines and warehouses already get their owner through the ACL phase's `IS_OWNER`.

**Upgrade path.**
- 1.x state: queries are owned by the SP with an unchanged fingerprint → SKIP → recheck → owner PATCH + GET → status
  `updated` with note `owner set to <x> (verified)`. One-time; the next run is SKIP again.
- An airgap target reading an old 1.x bundle (no `source_owner`) → no-op, as today.

**Tests:**
- create → PATCH + GET with the remapped owner;
- read-back mismatch → warning; PATCH 4xx → warning;
- absent owner → no PATCH, with a note;
- no `source_owner` (1.1 bundle) → zero extra calls;
- SKIP + owner differs → adopt → owner only, no content PATCH;
- SKIP + owner matches → zero writes;
- adopt with a moved fingerprint → update + owner;
- the fingerprint of a legacy_query unit is identical to 1.1.0's for the same source (golden assert);
- the export unit carries `source_owner` and the payload still has no owner.

---

## 6. CUST-1 — pipeline ACLs survive a refused owner change
**Today.**
- `AclImporter._acl_body` (`acl_importer.py:229-260`) sends the source's `IS_OWNER` grant verbatim.
- On a UC pipeline the run-as SP owns the new object, so `IS_OWNER=<source owner>` is an owner change. Only a
  metastore admin may make it: `403 PERMISSION_DENIED Only metastore admins can change pipeline owner`.
- `PUT permissions` is one declarative call, so **every** grant on that pipeline is lost.
- Leaving `IS_OWNER` out is rejected too (V7).

**Change (decision D1; recommended option (b)):** for `perm_type == "pipelines"`:
1. Send the body as today (source owner included), which keeps `main`'s behaviour wherever the transfer is allowed:
   a metastore-admin runner, or a non-UC pipeline.
2. If the PUT fails with a 403 whose message contains `can change pipeline owner` (case-insensitive; keep the marker
   in one constant):
   - GET `permissions/pipelines/<id>` and take the current non-inherited `IS_OWNER` principal;
   - rebuild the body with `IS_OWNER` = that principal plus every other grant;
   - PUT again (V7: 200, owner unchanged, grants exact).
3. The row becomes `created_with_warning`: "all N grants applied except the owner: source owner `<x>`; the pipeline is
   still owned by `<current>` (a UC pipeline's owner is also its run-as identity). A metastore admin must transfer it:
   UI → Permissions, or `PUT permissions` as a metastore admin."
4. If the retry PUT also fails → FAILED with both errors, as today.

**Option (a), if the user picks it instead:** always send the target's current owner for pipelines (one GET per
pipeline, every run), and never attempt the transfer.

**ACL Parity.**
- The applied entry carries `known_gap = [(source_owner, "IS_OWNER")]`.
- `write_parity_report` drops that tuple from *expected* and the current owner's `IS_OWNER` from *actual*, then
  computes the verdict on the rest.
- It adds `"known_gap": "owner not transferred (needs metastore admin)"`, so the sheet reads `match` + the gap and not
  `both`.

**Upgrade path.**
- 1.x rows for these pipeline ACLs are FAILED, so the next normal run (or `failed_only`) re-attempts them and heals
  them with the warning.
- No fingerprint change.

**Tests** (offline only; V6 means the bed can't produce the 403):
- 403 with the customer's verbatim message → GET → second PUT carries `IS_OWNER=<current>` plus all other grants →
  `created_with_warning` with the note;
- the parity verdict is `match` with `known_gap`;
- a different 403 → no retry, FAILED as today;
- a non-pipeline 403 → no retry;
- a successful first PUT → one call, unchanged;
- the retry fails → FAILED with both errors.

---

## 7. CUST-2 — pipelines the API can't create become a manual step
**Today.**
- A `Synced table: <catalog.schema.table> <suffix>` pipeline is created by the platform with a UC/Lakebase synced
  table. Its spec has no source code.
- `POST /api/2.0/pipelines` refuses it: `400 INVALID_PARAMETER_VALUE libraries must contain at least one element`.
- Export marks it `auto` (`asset_export._dlt_units`, `:760-771`), so import POSTs it every run and it fails every run.

**The rule** (`helpers.pipeline_not_creatable(spec) -> str`, one function used by export and import):
- A non-DAB pipeline whose spec has no non-empty `libraries` and no `ingestion_definition` can't be created through
  the pipelines API → return a note.
- If the name starts with `Synced table:`: "this pipeline belongs to the synced table `<fqn>` (UC/Lakebase). Recreate
  the synced table on the target and the platform recreates this pipeline. UC and Lakebase are out of scope for this
  tool."
- Otherwise: "this pipeline has no source code (`libraries` is empty), so the pipelines API cannot recreate it — it is
  platform-managed. Recreate whatever owns it."
- Return `""` for creatable pipelines.

**Change.**
- **Export:** `_dlt_units` emits it as `mode="manual"`, `migratable=False`, with the note. The payload is kept as
  today, for evidence only.
- **Import** (needed for the customer's existing 1.x bundles, which say `create`): `DltImporter.load()` sets
  `import_action="manual"` + the note on any unit the rule matches. `_process_one` step 1 then records it `manual` with
  **no API call**.
- The ACL units of these pipelines will still read `object_absent` (QA16.2-4 is not in this wave). See decision D5.

**Upgrade path.** 1.x state rows for these pipelines are FAILED; the next run records them `manual`. That puts them in
the skipped/manual retry buckets, so they no longer show in `failed_only`.

**Tests:**
- export: synced-table spec → manual with the table name in the note; empty libraries + no ingestion → generic note;
  ingestion pipeline (no libraries, has `ingestion_definition`) → `auto`; normal pipeline → `auto`, fingerprint
  unchanged;
- import: a 1.x-shaped bundle unit with `import_action=create` + no libraries → `manual`, zero POSTs;
- a normal pipeline → POSTed as today.

---

## 7b. QA16.2-1 — our message only when the cause is confirmed, otherwise the server's error
> Added 2026-10-12 (user). The customer hit this on 6 Genie spaces in 6 users' folders: the report said
> `prerequisite_missing: targets workspace folder /Users/<user>, which does not exist on target yet`, but the folders
> existed (their dashboards and files imported fine). `failed_only` fixed 1 of 6 and repeated the same message for
> the other 5. 16.2 QA reproduced it: the server actually said `403 … Table 'cat.schema.retry_tbl' does not exist`
> (a UC table Genie checks at create time).

**Today.** Two helpers replace the server's error with our own guess whenever certain words appear in it, and the
server text is then lost from both `note` and `error_raw`:
- `BaseImporter.missing_parent_prerequisite` (`base_importer.py:530-538`): any error containing `"does not exist"` or
  `"tree node"` becomes "the folder does not exist". Used by Genie (`genie_importer.py:55`), Lakeview dashboards
  (`dashboards_importer.py:108`), SQL queries (`sql_importer.py:158`) and Alerts V2 (`sql_importer.py:225`).
- `DashboardsImporter._not_published_prerequisite` (`dashboards_importer.py:381-387`): any 404 from a schedule create
  becomes "the dashboard is not published".

**Change (user 2026-10-12): keep our wording, but only when we have CONFIRMED the cause.**
The rule: show our message only when a live check proves it; in every other case show the server's error unchanged.

- **Folder** (`missing_parent_prerequisite`, all four callers unchanged). When the create error contains
  `"does not exist"` / `"tree node"`, check the folder right then: `GET workspace/get-status?path=<body parent_path>`
  (the exact resolved parent, not just the home root; called directly, not through the cached `_home_present`).
  - **404** → the folder really is missing → raise `PrerequisiteMissing` with **today's message, word for word**
    (the customer's 1 genuine home case keeps exactly the text in the screenshot).
  - **200** → the folder exists, so the error is about something else → `return`, and the caller re-raises the
    original error → `classify_error` shows the server's text verbatim (+ its normal hint).
  - **any other probe error** (403, 500, timeout) → not sure → the server's original error. Never guess.
  - Use `is_not_found(exc)` (`helpers.py:79`) to tell a 404 from other errors; don't use `_get_status`, which
    returns `{}` for every error and would turn a failed check into "folder missing". One DEBUG line with the result.
- **Not published** (`_not_published_prerequisite`). On a 404 from the schedule create, check
  `GET lakeview/dashboards/<id>/published` with the existing `_get_published(did)` (`dashboards_importer.py:251`):
  - it 404s (draft) → raise with **today's message, word for word**;
  - it returns a published doc, or the check fails → the server's original error.
  - (`_get_published` returns `{}` for every error too, so the same rule applies: only a confirmed 404 counts.)
- Cost: one GET, only on a create that already failed with those words. Successful creates make no extra call.

**Why this is safe.**
- A confirmed missing folder still gets `prerequisite_missing` and the same text as 1.1.0; only the misdiagnosed cases
  change, and they now show what the server said.
- `failed_only` re-tries every FAILED row whatever its category (`state_store.py:129`), so the category of the
  re-labelled rows never affects a retry.
- No report, sheet or test depends on the removed (wrong) text for the misdiagnosed cases (checked `src/` and
  `tests/`, 2026-10-12).

**Rest of the importers, checked 2026-10-12, no change needed:** they already pass the server text through
`classify_error`. The few keyword checks elsewhere only tolerate a known harmless error and never replace a message:
`"already exists"` on SP create (followed by a lookup), and `"already"` on `clusters/start`. Workspace content and the
jobs notebook-path check decide "home/path missing" from a `get-status` probe, not from error text.

**Upgrade path.** No state, bundle or fingerprint change. 1.x rows that FAILED with the wrong message stay FAILED;
the next `failed_only` re-attempts them and records either success or the server's real error.

**Tests** (`tests/test_plan16_3.py`, parametrised over Genie / dashboard / query / alert V2):
- create fails with `403 … Table 'c.s.t' does not exist`, folder check → 200 → FAILED with the server text in `note`
  and `error_raw`; no "targets workspace folder" text;
- create fails with "… does not exist", folder check → 404 → `prerequisite_missing` with **exactly** 1.1.0's message
  (golden string assert);
- folder check → 403/500 → the server's original error, not `prerequisite_missing`;
- create fails with an unrelated error → no folder check (assert on the fake client);
- successful create → no folder check;
- the check uses the full parent path: a missing sub-folder under an existing home → 404 → today's message;
- schedule 404 + published check 404 → today's "not PUBLISHED" message, word for word; published check 200 or 500 →
  the server's original error;
- a 1.1-shaped FAILED `genie_space` row under `failed_only` → re-attempted.

---

## 7c. QA16.2.1-1 — an Alert V2 is "updated" on every run although nothing changed
> Added 2026-10-12 (user). Found in the 1.1.1 hotfix QA (`PLAN_16_2_1` Run 3). Pre-existing: identical on 1.0/1.1.

**What happened (live evidence, bundles on target `7405614409585920`).**
- Run 3 reported `alert_v2 wsmig_test_alert_v2 → updated` ("source changed since the last import — updated in place"),
  and the 1.1.0 run before it reported both test alerts V2 `updated`. Nobody edited either alert.
- Field-by-field diff of the exported payload (`export/sql/alerts_v2.json`) between runs — the ONLY difference:

  | Alert | Previous run → this run | Changed field |
  |---|---|---|
  | `wsmig_test_alert_v2` (cron `0 0 9 * * ?`, daily 09:00 UTC) | `20261012_073721` → `20261012_092155` | `evaluation.last_evaluated_at` `2026-10-11T09:00:33Z` → `2026-10-12T09:00:31Z` |
  | same | `20261006_033735` → `20261012_073721` | `evaluation.last_evaluated_at` `2026-10-05T09:00:32Z` → `2026-10-11T09:00:33Z` |
  | `wsmig_test_alert_v2_b` | `20261012_073721` → `20261012_092155` | none → fingerprint equal → correctly **skipped** |

- So "updated" fired exactly when the source alert had **run on its schedule** between two migration runs. Nothing a
  user can set changed.

**Is it a bug? Yes (LOW–MEDIUM).** No data is lost or duplicated, and the PATCH re-sends the same settable content
(the server ignores the read-only fields). But:
- the report says `updated` for objects nobody touched. A customer with many scheduled alerts sees every one of them
  "updated" on every run, which buries real changes and breaks "the report is the source of truth";
- one needless PATCH per scheduled alert per run;
- `evaluation.state` (`OK` / `TRIGGERED`) is in the payload too, so a pure state flip does the same.

**Cause.** `STRIP_FIELDS["alert_v2"]` (`src/transform/transforms.py:103`) lists only top-level fields, and
`strip_runtime` (`:136-147`) strips top-level keys only. The two read-only fields live **inside** `evaluation`
(`evaluation.state`, `evaluation.last_evaluated_at`), so they stay in the payload and in the fingerprint. The
collector's GET-by-id enrichment (PLAN 8 Bug 10) is what brings `evaluation` in, which is correct (create needs it).
The legacy-alert strip list already drops its equivalents (`state`, `trigger_time`).

**Change.**
- Strip `evaluation.state` and `evaluation.last_evaluated_at` from the `alert_v2` payload at export, before
  fingerprinting. Keep it small: a nested-path entry in `STRIP_FIELDS["alert_v2"]` (e.g. `"evaluation.state"`) handled
  by `strip_runtime`, returning a copy (never mutate the record). Every other `evaluation` field (`source`,
  `comparison_operator`, `threshold`, `notification`, `empty_result_state`, …) stays: they are create/update fields.
- Also drop the two fields from the create/PATCH body (`_alert_v2_body`) for **1.x bundles** that still carry them.
  This is defensive and changes nothing on the target (the server ignores them today).
- **Audit the other families for the same nested-runtime-field pattern** while here: in this bed only Alerts V2 showed
  false updates (Run 1/3 `updated` rows: the 2 alerts + home folders that genuinely healed + the seeded policy). Add the
  regression test below for every asset type that has a GET-by-id enrichment (alerts V2, legacy queries, dashboards,
  Genie, jobs, pipelines).

**Upgrade path (the one allowed fingerprint change, decision D7).**
- The first 1.2.0 export gives every Alert V2 a new fingerprint (two fields fewer). The first upgraded run therefore
  records each non-DAB Alert V2 `updated` **once** (a PATCH with the same content). From then on an alert is `updated`
  only when the source really changed.
- This is no worse than today: today the same alerts are `updated` on every run in which they evaluated.
- Airgap with a 1.x **source** side: the bundle's fingerprints come from the old export, so the noise stays until the
  source-side Git folder is upgraded too. Note it in the CHANGELOG.

**Tests** (`tests/test_plan16_3.py`):
- two exports of the same alert that differ only in `evaluation.last_evaluated_at` and/or `evaluation.state` → equal
  fingerprints; the run decides SKIP with zero PATCH calls;
- a real edit (`threshold`, `comparison_operator`, `query_text`, `schedule`) → fingerprint moves → UPDATE;
- the stripped payload still has `evaluation.source`, `comparison_operator`, `threshold`, `notification`, and create
  still sends them (the PLAN 8 Bug 10 test still passes);
- a 1.x-shaped unit whose payload carries both fields → the create / PATCH body omits them;
- the strip never mutates the collector record;
- for every enriched asset type: an export pair differing only in the type's known runtime fields → equal
  fingerprints (the guard against this class of bug coming back).

**Live QA check (§11).** Run C (and every later run): an Alert V2 that evaluated on the source between runs is
`skipped`, not `updated` (compare `evaluation.last_evaluated_at` in the two bundles to prove it ran). Run B is the
one-time `updated` per D7.

---

## 8. Files touched
- `notebooks/01_Inventory.py` (job_run_id widget + resolve call)
- `jobs/direct_end_to_end_live.job.json`, `jobs/direct_end_to_end_dry_run.job.json`, `jobs/inventory.job.json`,
  `jobs/airgap_source.job.json` (inventory task pin)
- `src/exporters/bundle_state.py` (1b, 1c)
- `src/importers/import_runner.py` (1c import resume, 1d, 4b)
- `notebooks/04_Import.py` (4c)
- `src/reports/import_report.py` (4a)
- `src/importers/compute_importer.py` (§2)
- `src/importers/misc_importer.py` (§3)
- `src/exporters/asset_export.py` (§5a, §7)
- `src/importers/sql_importer.py` (§5b)
- `src/importers/acl_importer.py` (§6)
- `src/utils/helpers.py` (`pipeline_not_creatable`)
- `src/importers/dlt_importer.py` (§7)
- `src/importers/base_importer.py` (§7b `missing_parent_prerequisite`), `src/importers/dashboards_importer.py` (§7b `_not_published_prerequisite`)
- docs: `docs/RUNBOOK.md`, `docs/CONFIGURATION_GUIDE.md`, `docs/PERMISSIONS_GUIDE.md` (§10)
- `CHANGELOG.md`, `VERSION` (§10)
- `src/transform/transforms.py` (§7c nested strip) and `src/importers/sql_importer.py` (§7c body guard)
- `tests/test_plan16_3.py` (new)

## 9. Offline test bar (dev session)
- Every test listed in §1–§7c goes in `tests/test_plan16_3.py`. Use `tests/fakes.py` (fake client with recorded calls)
  as `test_plan16_2.py` does.
- **Full suite passes:** `python3 -m pytest` (497 today + the new ones).
- **Mutation check:** for each item, revert the fix locally, confirm at least one new test fails, then restore it.
  List the result per item in §9b.
- **Backward compatibility** (ground rule 7), at least one test each:
  - a 1.1-written state table (rows with `created`/`failed`/`skipped` + fingerprints) plus a 1.1-shaped bundle (no
    `source_owner`, `create` on a synced pipeline, timestamp run folders, no `job_run_id` param) runs through the new
    code with only the declared changes;
  - a **fingerprint-stability test**: for every asset type in the fixture bundle, the 16.3 export fingerprint equals
    1.1.0's.
- No new silent `except` (ground rule 5). Every new `except` logs at DEBUG (expected) or WARNING/ERROR (degraded).

## 9b. Dev notes (the dev session fills this in)
Judgment calls, deviations from this plan (with reason), the mutation-check results, and the final test count.

## 10. Docs, CHANGELOG, VERSION
- `VERSION` = `1.2.0`. `CHANGELOG.md` `## [1.2.0] - <date>`:
  - Upgrade notes:
    - pull-only works; run-id folders need a re-run of `00_Install_Jobs`;
    - the first upgraded run sets query owners (rows read `updated`);
    - workspace settings are re-checked against the target;
    - synced-table pipelines become manual;
    - a failed report now turns the task red;
    - Genie / dashboard / query / alert failures now show the server's real error instead of "folder does not exist"; re-run `failed_only` on rows that FAILED with that message.
    - every Alert V2 reads `updated` once on the first 1.2.0 run (fingerprint fix, §7c); after that only real source edits show as `updated`. In airgap mode this needs the source-side Git folder upgraded too.
  - Added / Fixed / Changed per item.
- `RUNBOOK.md`:
  - finding a run's folder (= the job run id);
  - Repair run vs Run now;
  - standalone `failed_only` with the job run id;
  - `completed_no_report`: what to do;
  - `not_applied` workspace settings;
  - the pipeline owner needing a metastore admin;
  - synced-table pipelines;
  - which object types keep their owner;
  - a Genie space that fails on a missing / unreadable UC table: create or grant the table (UC is out of scope), then `failed_only`.
- `CONFIGURATION_GUIDE.md`: the `job_run_id` parameter (set by the job, leave blank).
- `PERMISSIONS_GUIDE.md`: a UC pipeline owner transfer needs a metastore admin.

---

## 11. Live QA (Claude) — classic job cluster, LIVE + direct, stop after each run
Rules: `plans/qa-testing-agent.md`.
- Harness: copy `~/Desktop/wsmig_runs/plan16_2/setup/` → `plan16_3/setup/` and update the catalog, warehouse and
  workspace id.
- Reports go to `~/Desktop/wsmig_runs/plan16_3/`, with `FINDINGS.md` and one folder per run.
- `system.access.audit` is no longer readable by the QA user. Write checks use live GETs, `DESCRIBE HISTORY` on the
  state table, and the driver DEBUG log.

**Q0 — setup + fixtures** (Claude; fixture code in `tests/fixtures_medium.py` is test code, not utility code).
- **Target.** A fresh target if the user recreates one; otherwise the current target with new state schemas (ask).
  Then:
  - runner `ensure` + `scope`;
  - `target_uc_prep` (recreate `catalog_src_g9c6nd`);
  - state schemas `wsmig_state_main` + `wsmig_state_16_3`;
  - a staging volume;
  - Git folder at `main` `c3421bb`.
- New source phase **`query_owner`** (in `all`, before `acls`): set the owner of `wsmig_test_query` to `tanveer.singh@databricks.com`.
  The other queries stay owned by the QA user, so the bed has two distinct owners, neither of them the runner.
- New phases **`seed_16_3`** and **`target_conf_drift`**, both excluded from `all`:
  - `seed_16_3` (source): `wsmig_test_policy_family` overrides `autotermination 30 → 45`; `wsmig_test_query` owner →
    `aman.bansal@databricks.com` (another fixture user);
  - `target_conf_drift` (target): `enableResultsDownloading` → `true` (source = `false`).

**Run A — 1.1.0 (`main` `c3421bb`) e2e, installed by the 1.1.0 installer → `wsmig_state_main`.** This is the
customer baseline and the golden baseline on this bed. Expected 1.1.0 behaviour:
- the bundle folder is a timestamp;
- `wsmig_test_policy_family` is FAILED (400);
- every target query is owned by the runner;
- workspace settings are `Created` without verification;
- pipelines' `IS_OWNER` is transferred (the runner is a metastore admin, V6).

Save `golden_snapshot.json`. **Stop.**

**Run B — the upgrade.** Seed `target_conf_drift` first; this simulates the customer's false "Created". Then pull the
16.3 commit into the same Git folder and run the same 1.1.0-installed job, with no reinstall and the same state. Expect:
- **B9 off by default:** a timestamp folder, `resolved via: fresh`;
- **B3:** the family policy is `created`; a live GET shows `policy_family_id=personal-vm` + overrides;
- **B2:**
  - every migrated query owner = the source owner (tanveer for `wsmig_test_query`, the QA user for the rest), from
    live GETs;
  - rows `updated` with `owner set to … (verified)`;
  - no content PATCH (DEBUG log);
- **B4:**
  - `enableResultsDownloading` is re-applied and reads back `false` (live GET);
  - the other keys are SKIP with no write;
- **CUST-1:** pipeline ACL rows are unchanged; the owner is still transferred (live GET permissions);
- **CUST-2:** no change (no such pipeline in the bed);
- **7b (QA16.2-1):** seed `retry_seed` on the source before this run (`RETRY_GENIE` is built on `retry_tbl`, which is missing on target). Its row is FAILED with the server's `Table … does not exist` text, and **not** "targets workspace folder". (The confirmed-missing-folder path is offline only: homes on the bed are already provisioned.) Then `retry_heal` + `failed_only` → created;
- **no duplicates:** object counts per type are unchanged apart from the new policy;
- `logcheck.py` clean.

**Stop.**

**Run C — reinstall + run-id folder.** Run the 16.3 `00_Install_Jobs` (same names, same state schema), then the e2e
job. Expect:
- the job ids are unchanged;
- the inventory task prints `resolved via: job-run-id`;
- the folder name = the Jobs-UI run id;
- export/import used the same folder;
- `LATEST_*` pointers name it;
- the run is near-idempotent: SKIP everywhere, no writes apart from rechecks that are expected to be 0.

**Stop.**

**Run D — Repair.**
1. Start the e2e job again (a new *Run now*, which must give a **new** folder, D2).
2. **Cancel** it while the import task is running.
3. *Repair run* it.

Expect:
- the repaired import task gets the same `job_run_id`/task value → the same folder;
- checkpoint lines `resumed from checkpoint`;
- one final `import_status.xlsx` in that folder;
- state `rows=N expected=N`;
- no duplicates.

**Stop.**

**Run E — standalone `failed_only`** with `run_id` = Run D's job run id, copied from the Jobs UI, and
`dry_run=false`. Expect:
- `import_status_retry_<ts>.xlsx` in Run D's folder; the canonical report is untouched;
- only failed units are acted on.

**Stop.**

**Run F — incremental.** Seed `seed_16_3` on the source, then run the e2e job. Expect:
- a new folder;
- the family policy `updated` through the family-shape edit (live GET overrides = 45);
- `wsmig_test_query` owner = aman.bansal (verified);
- the other queries SKIP with zero writes.

**Stop.**

**Run G — negative paths.**
- **B11:** create the directory `<Run F folder>/reports/import_status_dry_run.xlsx/` (an obstacle; nothing on the
  target is touched), then run the standalone import with `dry_run=true`. This is the one deliberate dry run: it
  writes nothing to the target. Expect:
  - the task is **red**, `run_status=completed_no_report`;
  - the ERROR line names the file;
  - the json + md still land.

  Remove the obstacle (our own test artifact) and re-run: green.
- **1d:** point the standalone import at a folder that has only an inventory: run the standalone `inventory` job → its
  own run-id folder. Expect the actionable message naming `LATEST_EXPORT`'s run id.
- **1c:** run the standalone export with a blank `run_id`. Expect it to resolve to the newest inventory by time (the
  pointer), not by name, with timestamp folders from Runs A/B present.

**Stop.**

**Run Z (recommended final certification) — released `main` 1.2.0 on a fresh target and fresh schema → golden diff
vs Run A.** Expected diffs only:
- `cluster_policy` failed → created;
- legacy_query notes `owner set …`;
- workspace settings `(verified by read-back)`;
- the bundle folder = the job run id.

**Not live-testable here** (stated in FINDINGS):
- CUST-1's 403 path (V6). A stand-in is the customer's next import, or a non-metastore-admin runner if the user wants
  to set one up.
- CUST-2: the bed has no synced table (D5).
- A workspace-conf key the target silently drops (V8): it only shows up if the fresh target happens to drop one.

---

## 12. Decisions for the user (defaults = recommended; dev builds the defaults unless the user says otherwise)
- **D1 — CUST-1:**
  - **(b) default:** try the source owner; on the "only metastore admins" 403, re-send with the target's current owner
    and a warning;
  - (a): always keep the target's current owner for pipelines.

  (Plain "omit the owner" is not an option: the API rejects it, V7.)
- **D2 — B9:** a new *Run now* after a failed run starts a **new** folder; continuing the same folder is done with
  *Repair run*. **Default yes.**
- **D3 — B4:** on every run, a workspace setting that differs from the source on the target is re-applied and read
  back (heals the customer's false `Created`; also reverts a hand change on the target). **Default yes.**
- **D4 — B9 activation:** run-id folders only after re-running `00_Install_Jobs`; pull-only upgrades keep timestamp
  folders. **Default yes.** The alternative, reading the run id from the notebook context with no reinstall, works
  (V1) but relies on an undocumented context API.
- **D5 — CUST-2 coverage:**
  - **default:** offline tests, plus the customer's next import as the live proof;
  - optional: add a Lakebase instance + synced table to the source bed. That costs money, so it needs the user's OK.
  - Optional add-on: fix the `object_absent` wording on these pipelines' ACLs (part of QA16.2-4, small).

- **D7 — 7c alert fingerprint:** accept the one-time `updated` of every Alert V2 on the first 1.2.0 run (the
  only way to remove the per-run noise; today the same alerts are already `updated` whenever they evaluated).
  **Default yes.**

## 13. Review checklist (Claude, before live QA)
- Diff touches only the files in §8.
- Every 1.x fingerprint is unchanged (the test exists and passes).
- No new silent `except`.
- `job_run_id` exists only on inventory tasks.
- Resume rules match 1c.
- `adopt_one` for `legacy_query` keeps the stale-update rule.
- CUST-1 retry fires only on the exact marker.
- The CUST-2 guard runs at both export and import.
- The report-write failure can't mask an abort.
- 7b: our message only after a check returns a real 404; a 200 or a failed check → the server's original error;
  1.1.0's message text unchanged (golden test); no extra call on success.
- 7c: only `evaluation.state` + `evaluation.last_evaluated_at` are stripped; every other fingerprint unchanged (the
  stability test covers all asset types except Alert V2).
- CHANGELOG upgrade notes match §10.
- The suite and the mutation checks are reported in §9b.

## 14. Findings
(live QA appends here)
