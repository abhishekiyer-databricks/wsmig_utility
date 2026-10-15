# PLAN 16.3 — Customer fixes, wave 2 (release 1.2.0)

**Status:** READY FOR DEV once the user approves (rewritten 2026-10-12, updated 2026-10-13: B9 slimmed, CUST-1 run-as rule). Every API fact the code relies on is verified live (§0); nothing is left for the dev to probe. Master: [PLAN_16_master.md](PLAN_16_master.md).
**Base:** `main` @ 1.1.1 (after the `hotfix/policy_family` merge). **Branch:** `feature/16_3_customer_fixes`.
**Roles:**
- **Dev session:** builds the code and the offline tests from this file (plus `CLAUDE.md` and the master plan).
- **Claude (this session):** reviews the diff (§13), then runs the live QA (§11).
- **The user:** reviews, commits and pushes. Claude never commits.

## Start here (dev session)
1. **Read:** this file, `CLAUDE.md`, and `plans/PLAN_16_master.md` (ground rules).
2. **Branch:** `git checkout main && git pull`. Confirm `VERSION` = `1.1.1`, then `git checkout -b feature/16_3_customer_fixes`.
3. **Baseline:** `python3 -m pytest` → **507 passed**. pip needs the proxy: `pip3 install --index-url https://pypi-proxy.cloud.databricks.com/simple <pkg>`.
4. **Build order** (smallest blast radius first; run the full suite after each item):
   - §8 alert strip
   - §7 error wording
   - §3 report write
   - §2 settings
   - §6 synced pipelines
   - §1 run folder
   - §4 query owner
   - §5 pipelines
5. **Rules:**
   - **No live calls:** offline tests only, using the fakes in `tests/test_plan16_2.py` / `tests/test_plan16_2_1.py`.
   - **No git:** don't commit, push or edit the live bed.
   - **No surprises:** don't touch files outside §9 or add anything not in this plan. If something here is wrong or ambiguous, write it in §9b and stop for review rather than guess.
6. **Done when:**
   - every test listed in this plan exists and passes;
   - the full suite passes;
   - the mutation check is recorded per item in §9b;
   - the fingerprint-stability test passes;
   - the CHANGELOG, VERSION and docs are updated per §10.

   Then hand back to Claude for the review (§13).

| § | Item | What changes for the customer |
|---|---|---|
| 1 | **B9** find the run's folder | Each notebook also prints the run's folder path at the **end** of its output (today it's only at the top). A standalone import always uses the latest export; no run ever continues an old abandoned folder. |
| 2 | **B4** workspace settings | Each setting is read back after the write. If the target didn't change, the row is `failed`, not `Created`. |
| 3 | **B11** report write | If `import_status.xlsx` doesn't land, the task goes red (`completed_no_report`). |
| 4 | **B2** query owner | A migrated SQL query gets its source owner, not the migration SP. |
| 5 | **CUST-1** pipeline ACLs | The tool never tries to change a pipeline's owner, so the other grants always land. An SP-owned pipeline keeps running as that SP. |
| 6 | **CUST-2** synced-table pipelines | Pipelines the API can't create become a `manual` step. Today they fail on every run. |
| 7 | **QA16.2-1** wrong "folder missing" | "Folder does not exist" is shown only when a check proves it. Otherwise the report shows the server's real error. |
| 8 | **QA16.2.1-1** alerts "updated" | An Alert V2 that only ran on its schedule is `skipped`, not `updated`. |

Cluster-policy families (B3) shipped in 1.1.1. **Not in this wave:**
- 16.4 (notification destinations, catalog rename);
- B1, B8, B12;
- W0-6, W0-7;
- QA16.2-2…4;
- the wider B4 audit.

**Ground rules:**
- **Everything else stays byte-identical to 1.1.1:** decisions, natural keys and state schema.
- **No fingerprint changes, except Alert V2 (§8).** Any other fingerprint change would mass-UPDATE on the first upgraded run.
- **Upgrade safe.** The customer runs 1.1.x. Each item says what a 1.x state row, bundle or installed job does on the first upgraded run, and has an offline test that seeds that shape.
- **No new silent `except`.** Log expected cases at DEBUG and degraded ones at WARNING/ERROR.

---

## 0. Verified live facts (the dev builds on these; no more probing needed)
The evidence is in `~/Desktop/wsmig_runs/plan16_3/api_probes/2026-10-09_premises.md`. All probes used scratch objects, which were deleted afterwards.

| # | Fact | § |
|---|---|---|
| V1 | A *Repair run* keeps the job's task values, so a repaired export/import gets the same `run_id` from the inventory task and continues in the same folder. | 1, 11 |
| V2 | Job run ids are **not** in time order, which is one reason folders keep their timestamp names. | 1 |
| V3 | Workspace-conf GET returns strings (`"true"`, `"30"`) or `null`. A read right after a PATCH sees the new value. Which keys a workspace silently ignores differs per workspace. | 2 |
| V4 | The query LIST returns `owner_user_name`. Queries have no `IS_OWNER` level. The PATCH `{query:{owner_user_name}, update_mask:"owner_user_name"}` changes the owner. | 4 |
| V5 | Pipeline `PUT permissions` with no `IS_OWNER` → 400 `The pipeline must have exactly one owner.` A PUT with `IS_OWNER` = the **current** owner plus the other grants → 200, applied exactly. | 5 |
| V6 | **(2026-10-12)** A UC pipeline **runs as its owner** unless the spec sets `run_as`: changing the owner changed `run_as_user_name`. Spec `run_as` = another **user** → 400 `You can only set run_as to yourself, a service principal, or a group`. Spec `run_as` = an **SP** → 200; it runs as that SP and the owner stays the creator. A **group** can never own a pipeline (`Owner cannot be changed to a group`). **Not verified:** the caller here is a metastore admin, so it is unknown whether a non-admin runner may set `run_as` to another SP (it may need the Service Principal User role on it). | 5 |
| V7 | **(2026-10-13)** The pipelines LIST (`GET /api/2.0/pipelines`) returns `run_as_user_name` per pipeline (source 5/5, target 5/5). The collector keeps that LIST entry as `_raw`. An SP shows as its application id (a UUID). | 5 |
| V8 | On this bed every account identity is a metastore admin (the metastore owner is `account users`), so the customer's 403 can't be reproduced here. With §5 that no longer matters: the tool never asks for an owner change. | 5, 11 |

---

## 1. B9 — say where the run's folder is (folders keep their timestamp names)
**Decision (user, 2026-10-13): keep it simple.**
- No folder renaming.
- No job-run-id recording.
- No template or installer change.

**Today:**
- Every notebook already prints `Run id : <run_id>` at the top.
- `02_Export` and `04_Import` also print `Bundle : <full folder path>`; `01_Inventory` prints `Staging : <full folder path>`.
- **Which folder does a stage use?**
  - **Inside the e2e job:** export and import always use the folder the inventory task just made (passed as a task value). A *Repair run* keeps it.
  - **A standalone import with a blank `run_id`:** 1.1.1 first looks for *any* folder whose import started and didn't finish and continues that one; only if there is none does it use `LATEST_EXPORT.json`.
  - **A standalone or e2e inventory/export with a blank `run_id`:** 1.1.1 continues *any* unfinished folder, or starts fresh.
- `failed_only` works from the state table, so the folder never decides what is retried.
- **What's missing:** the folder line is only at the *top* of a long output, so the operator has to scroll back for it.

**Change.**
1. **Print the folder at the end too.** The last lines of `01_Inventory`, `02_Export` and `04_Import` print:
   ```
   Run id : <run_id>
   Folder : <staging>/wsmig/<source_ws_id>/<run_id>/   (reports/ = the xlsx, misc/ = the JSON + logs)
   ```
   This uses the same `cfg.output_path` the header already prints. It is a notebook-only change.
2. **A standalone import with a blank `run_id` always uses `LATEST_EXPORT.json`** (user, 2026-10-13).
   - Delete the "continue an unfinished import" scan from `resolve_import_run_id`. The order becomes: explicit `run_id` → `LATEST_EXPORT.json` → error.
   - **Nothing is lost.** An import that crashed in the latest export's folder is in that same folder, so it is still continued: that folder's checkpoint and the state table skip the work already done.
   - **Why the old scan was harmful:** it could pick an import that crashed in an *older* folder. That folder holds an older copy of the source, so its objects would be UPDATEd on the target back to that older content.
3. **Inventory/export: continue only the newest folder** (`find_latest_incomplete_run`).
   - With a blank `run_id`, continue the **newest** folder if it is unfinished; otherwise do what 1.1.1 does next (inventory: start fresh; export: `LATEST_INVENTORY.json`).
   - This stops a folder abandoned days ago from being picked up after newer runs finished.

**Dropped (user, 2026-10-13):** a clearer "export didn't finish" message. Import already refuses such a bundle, so this was wording only:
- in the e2e job, import `depends_on` export, so it never starts after a failed export;
- a blank `run_id` resolves to `LATEST_EXPORT.json`, which only ever names a completed export;
- an explicit `run_id` of a failed export is stopped by the manifest gate (`verify_bundle`).

**Upgrade.** Nothing changes in the state, the bundle or the jobs.

**Tests:**
- the notebooks print the folder line at the end (grep test);
- a standalone import with a blank `run_id` → `LATEST_EXPORT.json`, even when an older folder has an unfinished import;
- an unfinished import in the `LATEST_EXPORT` folder → continued (checkpoint used);
- inventory/export ignore an older abandoned folder when the newest is complete;
- the newest folder unfinished → continued, as today;

## 2. B4 — workspace settings are read back
**Problem.** `misc_importer._set_conf` reports "set" on the strength of the HTTP 200 alone. The customer's `enableExportNotebook` reads `Created=false`, but the target is still `true`, and every later run SKIPs it.

**Change.**
1. **After each PATCH,** `GET workspace-conf?keys=<key>` and compare: lower-cased, stripped, `null` = `""`.
   - **Match:** note `<key> set to '<v>' (verified by read-back)`.
   - **Mismatch:** raise `NotApplied`: "`<key>` was set to `<v>` and the API returned 200, but the target still reads `<observed>` — this workspace ignores it through the API. Set it in the admin console, then re-run."
   - A FAILED row stores no fingerprint, so it stays red until the target really matches.
2. **Heal 1.x false `Created` rows.**
   - `existing_keys` already reads every target value; keep them in `self._target_conf`.
   - `recheck_unchanged` returns True when the target value differs from the source. The row is then re-applied and read back.
   - Matching keys stay SKIP with zero writes.
   - This also reverts a hand change on the target (**D2**).
   - Dry run: "would set `<key>` to `<v>` (target currently `<observed>`)".

**Tests:**
- match → `created` with the note;
- mismatch → `failed`/`not_applied`, no fingerprint stored;
- a 1.x `created` row whose target differs → PATCH + read-back;
- a 1.x row whose target matches → zero PATCH calls;
- normalisation (`"False"` vs `"false"`, `null` vs `""`);
- an unknown key → `PrerequisiteMissing`, as today;
- dry run → no PATCH.

## 3. B11 — a failed report write turns the task red
**Problem.** `write_import_reports` and `ImportRunner.run` turn report-write errors into warnings, so a green run can leave no `import_status.xlsx`.

**Change.**
1. `write_import_reports` still writes every artifact it can, but each failure is now logged at ERROR, with the traceback and the path.
   - It returns `errors=[{artifact, path, error}]`.
   - It then checks that each file exists and has size > 0; a missing or empty file is added to `errors`.
2. `ImportRunner.run`: on report errors, `completed` becomes `completed_no_report` (new constant `RUN_STATUS_NO_REPORT`).
   - `completed_state_not_saved` keeps priority.
   - On an aborting run, the original exception is still re-raised.
3. `04_Import` raises on `completed_no_report`, next to the existing `completed_state_not_saved` raise. The message is: "the target WAS changed and the state WAS saved; re-run the import with run_id=`<id>` to rewrite the reports."

The inventory and export reports are out of scope. No upgrade concern.

**Tests:**
- the xlsx render raises → `completed_no_report` with an ERROR line, and the json + md are still written;
- a 0-byte file → `completed_no_report`;
- a clean run → `completed`, with an unchanged summary;
- abort + report failure → the original exception is re-raised;
- state-not-saved + report failure → `completed_state_not_saved`;
- the notebook contains the raise (grep test).

## 4. B2 — SQL queries keep their source owner
**Problem.** The owner is read-only at create, so every migrated query is owned by the migration SP.

**Change.**
1. **Export** (`_sql_units`): the `legacy_query` unit gets a top-level `source_owner` from the LIST's `owner_user_name`. It goes **outside** the payload and the fingerprint.
2. **Import** (`SqlImporter._set_query_owner`): PATCH the owner, then GET the query and compare.
   - **Match:** note `owner set to <x> (verified)`.
   - **PATCH error or mismatch:** `created_with_warning`, "query left owned by the migration SP". The query exists, so this is not a failure.
   - **Owner deleted on the source** (roster `absent`), or the query was moved to `/Users_Backup`: no PATCH, with a note.
3. **When it runs:**
   - after a create;
   - after an update, if the owner differs;
   - on unchanged queries through `recheck_unchanged`. This compares against the owner the target LIST already returns, so it costs no extra calls.
4. **Adopt path.** `legacy_query` joins `reconcile_on_adopt_types`. `adopt_one`:
   - if the fingerprint moved → `update_one`, which also sets the owner (1.1.1's rule, tested);
   - else if the owner differs → owner only;
   - else → `adopted`.

**Why the fingerprint stays out of it:** putting the owner in the fingerprint would content-PATCH every migrated query once. Comparing against the live owner touches only queries whose owner is wrong.

**Upgrade.**
- On a 1.x target, every query is SP-owned. The first upgraded run sets each owner once (rows read `updated`) and then SKIPs.
- An old bundle with no `source_owner` → no-op.

**Out of scope** (Runbook): dashboards and Genie (no owner API), clusters, alerts. Jobs and warehouses already get their owner through ACLs.

**Tests:**
- create → PATCH + GET with the remapped owner;
- a mismatch or a 4xx → warning;
- absent owner → no PATCH;
- no `source_owner` → zero extra calls;
- SKIP + owner differs → owner only, with no content PATCH;
- owner matches → zero writes;
- adopt with a moved fingerprint → update + owner;
- the `legacy_query` fingerprint is identical to 1.1.1's (golden test).

## 5. CUST-1 — pipelines: never change the owner; keep an SP run-as
**Problem.**
- The ACL importer sends the source's `IS_OWNER` in its PUT. On a UC pipeline, that is an owner change, which only a metastore admin may make: `403 Only metastore admins can change pipeline owner`.
- The PUT is all-or-nothing, so **every** grant on the pipeline is lost.
- Leaving `IS_OWNER` out is not possible either (V5).

**Can the owner be changed at all?** Only by a metastore admin (UI, or `PUT permissions`). The migration SP isn't one at the customer, and a utility can't make itself one. **So the tool stops trying** (user decision, 2026-10-12). What *can* be kept is the identity the pipeline **runs as**, when that identity is an SP (V6).

**The rule** (user, 2026-10-13):

| Source owner (= its run-as) | Target owner | Target runs as |
|---|---|---|
| a **user** | the migration identity | the migration identity. A user can't be set as run-as (V6). |
| an **SP** | the migration identity | **the same SP** (remapped through `sp_mapping`), via spec `run_as` |
| a **group** | — | — (a group can't own a pipeline, V6) |

In both cases the source owner gets `CAN_MANAGE` (**D1**).

**Change.**
1. **ACLs** (`AclImporter`; `perm_type == "pipelines"` only, every other type unchanged):
   1. `GET permissions/pipelines/<target id>` and read the current, non-inherited `IS_OWNER`. For a pipeline the tool created, that is the migration identity.
   2. Build the body from the source grants, then:
      - drop the source `IS_OWNER`;
      - add `IS_OWNER` = the current owner;
      - if the source owner is not the current owner, add the source owner as `CAN_MANAGE`, with no duplicate entry.
   3. PUT once (V5: 200, exact). The row is `created`, with the note "owner kept as `<current>` (a pipeline owner change needs a metastore admin); source owner `<x>` given CAN_MANAGE".
   4. If the GET fails or finds no owner → FAILED with that reason. The tool never guesses an owner.
   5. **ACL Parity:** the source `IS_OWNER` → `CAN_MANAGE` is an accepted difference. The verdict is `match`, with `known_gap: owner kept on target (needs a metastore admin)`.
2. **Run-as** (`DltImporter`):
   1. **Export** (`_dlt_units`): the `dlt_pipeline` unit gets a top-level `source_run_as` = `r["_raw"]["run_as_user_name"]`, passed with `extra=`. `_raw` is the pipelines LIST entry, which carries it (V7). It is outside the payload, which is the `spec`, and outside the fingerprint.
   2. **Create/update:** the source spec has no `run_as` and `source_run_as` is an SP application id (a UUID) → set `spec.run_as = {service_principal_name: <remapped id>}`.
      - An explicit source `run_as` keeps today's `_remap_run_as`.
      - A source `run_as` that is a user, and not the migration identity, is dropped, because the target would 400 (V6).
   3. **The create/update is refused because of `run_as`** (e.g. a missing Service Principal User role; V6 "not verified"): retry once without `run_as`. The result is `created_with_warning`: "runs as the migration identity; source ran as SP `<x>` — grant the runner the Service Principal User role on `<x>`, then re-run." The pipeline exists either way.
   4. **Already-migrated pipelines** (1.x targets run them as the migration SP):
      - `existing_keys` keeps `self._target_run_as = {pipeline_id: run_as_user_name}` from the pipelines LIST it already makes (V7, zero extra calls).
      - `recheck_unchanged` returns True when the desired SP run-as differs from the target's.
      - `dlt_pipeline` joins `reconcile_on_adopt_types`. A plain ADOPT with an unchanged fingerprint only records `adopted` (`base_importer` ADOPT branch), so the reconcile hook is required. `adopt_one`:
        - fingerprint moved → `update_one` (keeps 1.1.1's stale-update rule);
        - else if the run-as differs → `update_one` (the full spec PUT, now with `run_as`) → `changed=True`;
        - else → `adopted`.
      - User-owned pipelines never recheck.

**What the customer must know (Runbook, CHANGELOG):**
- User-owned UC pipelines run as the migration identity until a metastore admin transfers them, so that identity needs access to their tables.
- SP-owned pipelines keep their SP as the run-as identity.

**Upgrade.**
- 1.x pipeline-ACL rows are FAILED, so the next run (or `failed_only`) heals them.
- SP-owned pipelines get their run-as back through the recheck: a one-time `updated`.
- Pipelines whose owner was transferred earlier keep it: the current owner is then the source owner, so nothing is reverted.
- No fingerprint change.

**Tests:**
- **ACLs:**
  - a created pipeline → the PUT has `IS_OWNER=<runner>`, the source owner `CAN_MANAGE`, and the other grants verbatim;
  - current owner = source owner → body = source ACL;
  - the source owner already has `CAN_MANAGE` → no duplicate;
  - the GET fails → FAILED, no PUT;
  - other object types → byte-identical to 1.1.1;
  - parity → `match` + `known_gap`;
  - a 1.x FAILED row under `failed_only` → healed.
- **Run-as:**
  - SP-owned source → the create body has the remapped `run_as`;
  - user-owned → no `run_as`;
  - an explicit SP `run_as` → as today;
  - an explicit other-user `run_as` → dropped, with a note;
  - `run_as` refused → retry without it → `created_with_warning`;
  - a 1.x target pipeline running as the runner with an SP source → recheck → update with `run_as`;
  - user-owned → no recheck;
  - the `dlt_pipeline` fingerprint is identical to 1.1.1's.

## 6. CUST-2 — pipelines the API can't create become manual
**Problem.** A `Synced table: <fqn>` pipeline is created by the platform and has no source code. `POST /pipelines` refuses it (`libraries must contain at least one element`), so it fails on every run.

**Change.** One rule, `helpers.pipeline_not_creatable(spec) -> note | ""`. A non-DAB pipeline with no non-empty `libraries` and no `ingestion_definition` is not creatable.
- **Name starts with `Synced table:`** → note: "belongs to the synced table `<fqn>` (UC/Lakebase, out of scope) — recreate the synced table on the target and the platform recreates this pipeline."
- **Has `gateway_definition`** (a Lakeflow Connect ingestion gateway, review 2026-10-14) → note naming its UC connection: the connection is UC (out of scope), create it on the target and recreate the gateway + ingestion pipeline in Lakeflow Connect. Ingestion pipelines (`ingestion_definition`) are still attempted as in 1.1.1; the Runbook documents both as manual.
- **Otherwise** → note: "no source code (`libraries` is empty), so the pipelines API can't recreate it — recreate whatever owns it."
- **Export** (`_dlt_units`): such pipelines get `mode="manual"`. The payload is kept as evidence.
- **Import** (`DltImporter.load`): the same rule turns 1.x bundles' `create` into `manual`, with **no** API call.
- These pipelines' ACL rows still say `object_absent`; the wording fix is in QA16.2-4, a later wave.

**Upgrade.** 1.x FAILED rows become `manual`, so they leave the `failed_only` bucket.

**Tests:**
- export: synced-table → manual with the table name; empty libraries → the generic note;
- an ingestion pipeline → `auto`;
- a normal pipeline → `auto`, with the same fingerprint;
- import: a 1.x unit with `create` and no libraries → `manual`, zero POSTs.

## 7. QA16.2-1 — show "folder missing" only when it is confirmed
**Problem.**
- On 6 Genie spaces, the customer saw "targets workspace folder /Users/<user>, which does not exist on target yet". Those folders existed.
- The real server error was `Table 'cat.schema.tbl' does not exist`: Genie checks its UC tables at create time.
- Two helpers turn any error that contains certain words into our own message, and the server text is lost:
  - `BaseImporter.missing_parent_prerequisite` (`"does not exist"` / `"tree node"`), used by Genie, dashboards, queries and Alerts V2;
  - `DashboardsImporter._not_published_prerequisite` (any 404 from a schedule create).

**Change.** Our message only after a live check returns a real 404; otherwise the server's error.
- **Folder:** call `GET workspace/get-status?path=<the exact parent_path>` directly (not the cached `_home_present`), and tell a 404 apart with `is_not_found`.
  - **404** → `PrerequisiteMissing` with **today's message, word for word**;
  - **200 or any other error** → re-raise the original error, so `classify_error` shows the server's text.
- **Not published:** call `_get_published(did)`, and only a confirmed 404 counts.
  - **404** → today's message;
  - **anything else** → the original error.
- **Cost:** one GET, and only on a create that already failed with those words.

No state, bundle or fingerprint change. 1.x rows that FAILED with the wrong message are retried by `failed_only` (it re-tries every FAILED row), and then show the real error or succeed.

**Tests** (parametrised over Genie, dashboard, query and Alert V2):
- "Table … does not exist" + folder check 200 → FAILED with the server text in `note`/`error_raw`;
- folder check 404 → 1.1.1's message, compared to a golden string;
- folder check 403/500 → the original error;
- an unrelated error, or a successful create → no folder check;
- a missing sub-folder under an existing home → 404 → our message;
- schedule 404 + published 404 → our message; published 200/500 → the original error.

## 8. QA16.2.1-1 — Alert V2 "updated" when only its schedule ran
**Evidence.**
- The 1.1.1 QA showed `wsmig_test_alert_v2 → updated` on two runs, with no edit.
- The only payload difference between the bundles was `evaluation.last_evaluated_at`, which moved by a day each time (the alert runs daily at 09:00).
- The alert that hadn't evaluated was correctly `skipped`.

**Cause.** `STRIP_FIELDS["alert_v2"]` and `strip_runtime` strip **top-level** keys only. `evaluation.state` and `evaluation.last_evaluated_at` sit inside `evaluation`, so they stay in the fingerprint. Every evaluation looks like an edit: a false `updated` and a needless PATCH per alert per run.

**Change.**
- `strip_runtime` supports dotted paths. `STRIP_FIELDS["alert_v2"]` adds `evaluation.state` and `evaluation.last_evaluated_at`.
  - It returns a copy and never mutates the record.
  - All other `evaluation` fields stay: they are create/update fields.
- `_alert_v2_body` also drops the two fields, for 1.x bundles that still carry them.
- **Audit:** in the 1.1.1 runs no other type showed a false update. Add a guard test for every type with GET-by-id enrichment: alerts V2, queries, dashboards, Genie, jobs, pipelines.

**Upgrade (the one allowed fingerprint change, D4).**
- On the first 1.2.0 run, every non-DAB Alert V2 reads `updated` once (a same-content PATCH), then only on real edits.
- In airgap mode, this needs the source-side Git folder upgraded too.

**Tests:**
- two exports that differ only in those two fields → equal fingerprints, SKIP, zero PATCH;
- a real edit (threshold, operator, query, schedule) → UPDATE;
- the stripped payload keeps `source`/`comparison_operator`/`threshold`/`notification` (the PLAN 8 Bug 10 test still passes);
- a 1.x payload with the fields → the body omits them;
- the record is not mutated;
- the per-type guard test.

---

## 9. Files and offline test bar
**Files:**
- `notebooks/01_Inventory.py`, `02_Export.py` and `04_Import.py` (the end-of-output folder line);
- **no** `jobs/*.job.json` or installer change;
- `src/exporters/bundle_state.py` and `src/exporters/asset_export.py`;
- `src/transform/transforms.py`;
- in `src/importers/`: `import_runner`, `misc_importer`, `sql_importer`, `acl_importer`, `dlt_importer`, `base_importer` and `dashboards_importer`;
- `src/reports/import_report.py` and `src/utils/helpers.py`;
- docs, `CHANGELOG.md`, `VERSION`;
- the new `tests/test_plan16_3.py`.

**Test bar:**
- **Tests:** every test above goes in `tests/test_plan16_3.py`, using the fakes from `tests/test_plan16_2.py`.
- **Full suite:** `python3 -m pytest` passes (507 today, plus the new tests).
- **Mutation check:** revert each item and confirm at least one new test fails. Record the results in §9b.
- **Backward compatibility:**
  - a 1.1.1-written state plus a 1.1.1-shaped bundle (no `source_owner`, a synced pipeline marked `create`, timestamp folders) produces only the declared changes;
  - a **fingerprint-stability test**: every asset type except `alert_v2` keeps 1.1.1's fingerprint.

## 9b. Dev notes (the dev session fills this in)
Record judgment calls, deviations (with the reason), mutation-check results and the final test count.

**Dev session 2026-10-13** — branch `feature/16_3_customer_fixes` cut from local `main` @ `51e3a08` (VERSION 1.1.1;
`git pull` could not reach the remote — SSH key not loaded — but local `main` already held the 1.1.1 merge). Baseline
507 passed. **Final: 599 passed** (92 new in `tests/test_plan16_3.py`). Nothing committed.

**Files outside §9 (deviation, intended behaviour changes only):**
- `tests/test_import_framework.py` — `test_run_resolution_resumes_an_incomplete_import` asserted the deleted
  unfinished-import scan (§1.2); rewritten as `..._uses_the_pointer_even_with_an_incomplete_import`.
- `tests/test_importers_phase6_12.py` — (a) the conf test's fake never reflected a PATCH, so read-back (§2) said
  `not_applied`; the fake now reflects writes. (b) two DLT tests used `libraries: []` payloads, which §6 now (correctly)
  marks manual; the fixtures now carry a notebook library.
- `genie_importer.py` (listed) needed no change — the §7 fix lives in `BaseImporter.missing_parent_prerequisite`.

**Judgment calls:**
- §7 not-published: a direct `GET …/published` with `is_not_found`, NOT `_get_published` (it returns `{}` for every
  error, so it can't tell 404 from 500). Only runs when the schedule create itself 404'd (as 1.1.1).
- §7 folder: the probe is uncached on the EXACT `parent_path`; the pre-existing cached home-root probe in
  `remap_parent_path` is unchanged.
- §2: a read-back GET that itself errors propagates → the row is FAILED (`api_error`) — the report must not claim
  success it could not verify. Dry-run wording via `dry_run_intent`.
- §3: `reports["errors"]` is only added when non-empty, so a clean run's summary/reports dict is byte-identical. A
  `write_text_local_then_copy` that renders nothing is also an error. The notebook prints `Run id`/`Folder` BEFORE the
  red-task raises, so the folder is still the last thing printed on a green run and right above the raise on a red one.
- §4: "moved to /Users_Backup" outside a create (recheck/adopt) is approximated with no extra call as "the parent
  home's owner is absent from the source roster" (the backup divert's own precondition). The created query's owner is
  taken from the create response, so a source query already owned by the runner costs zero owner calls.
- §5 ACL: GET failure / no owner → `RuntimeError` with the reason (classified `api_error`, retried by `failed_only`).
  Parity uses the IS_OWNER of the SAME re-read it already makes (no extra GET): expected source IS_OWNER → CAN_MANAGE,
  the kept owner's IS_OWNER left out, `known_gap` also written to the sheet's existing `detail` column (so
  `import_report.py` needed no parity change).
- §5 run-as: an explicit source `run_as` USER other than the migration identity is dropped as a **warning** (amber)
  rather than a plain note — the pipeline now runs as a different identity than on source. Migration identity = SCIM
  `Me.userName`, read lazily once and only when a user `run_as` is present. "Refused because of run_as" = error text
  containing `run_as` / `run as` / `service principal user` / `serviceprincipal.user`; only then is it retried once
  without `run_as` (explicit SP run_as included). Account SPs are not in `sp_mapping` → kept as-is (same appId).
- §6: the export marks the unit manual with its payload, but (like every manual unit) `export_runner` writes only the
  index row for it, not the payload file — so "kept as evidence" = the unit in memory/ledger, not on disk. Not changed
  (export_runner is outside §9).
- §1: the 04 widget label changed from "blank = resume, else LATEST_EXPORT.json" to "blank = LATEST_EXPORT.json"
  (label only; parameter name unchanged, no template/installer change). `_has_import_checkpoint` deleted (no callers).

**Mutation check** (each item reverted alone in a scratch copy; new tests that then fail):
§8 alert strip 8 · §7 error wording 10 · §3 report write 2 · §2 settings 3 · §6 synced pipelines 5 · §1 folder choice 2 ·
§1 notebook end lines 1 · §4 query owner 6 · §5 pipeline ACL 5 · §5 pipeline run-as 4.

**Fingerprint stability:** `test_fingerprint_stability_every_type_but_alert_v2_matches_1_1_1` compares a 25-unit
inventory (every asset type incl. publish/schedule, a synced pipeline, a query with an owner, a pipeline with an SP
run-as) against golden hashes computed by running `main` @ `51e3a08`'s `build_all`: only the Alert V2 carrying
`evaluation.state/last_evaluated_at` moves (D4); the alert without them is unchanged.

## 10. Release (1.2.0)
**`VERSION`** is `1.2.0`. **`CHANGELOG.md`** has Added / Fixed / Changed entries per item, plus these upgrade notes:
- pull-only, no reinstall. Each notebook now ends with the run's folder path;
- the first run sets query owners (`updated`) and re-checks workspace settings against the target;
- every Alert V2 reads `updated` once;
- synced-table pipelines become `manual`;
- a missing report turns the task red;
- failures that said "folder does not exist" now show the server's error, so re-run `failed_only`;
- **pipelines keep the migration identity as owner.** User-owned pipelines also run as it; SP-owned ones keep running as their SP (one-time `updated` on 1.x targets). The source owner gets `CAN_MANAGE`. A metastore admin can transfer ownership afterwards.

**`RUNBOOK.md`:**
- finding a run's folder: open the job run → a task's output → the last `Folder :` line;
- Repair run vs Run now;
- standalone `failed_only`: `run_id` can stay blank;
- `completed_no_report`;
- `not_applied` settings;
- pipeline owner / run-as and how a metastore admin transfers it;
- synced-table pipelines;
- which types keep their owner;
- a Genie space failing on a missing UC table.

**`PERMISSIONS_GUIDE.md`:**
- the migration identity needs access to the tables of user-owned UC pipelines (it runs them);
- to keep an SP run-as, it may need the Service Principal User role on that SP.

---

## 11. Live QA (Claude) — classic job cluster, LIVE + direct, 3 runs, stop after each
Shrunk from 7 runs to 3 (user, 2026-10-15): each run now checks several items. Dropped: Run Z (the merged code is
the code tested here; Run 1 already creates new pipelines and changes owners from scratch → after merge only check
`main`'s VERSION) and the live §3 report-write negative (offline-covered).

**Bed:**
- target `7405614409585920`; state `catalog_3_5djjet.wsmig_state_16_2` (holds 1.1.1's state from PLAN_16_2_1
  Run 3 = the customer's starting point, so no baseline run);
- Git folder `wsmig_cust` + the jobs installed by 1.1.x (pull only, no reinstall);
- target pipelines use catalog `catalog_src_g9c6nd` (schema `wsmig_test`); the runner has `ALL_PRIVILEGES` there;
- evidence in `~/Desktop/wsmig_runs/plan16_3/run{1,2,3}/` + `FINDINGS.md`; checks use live GETs, the state table
  and the driver log.

**Fixtures** (`tests/fixtures_medium.py`, all EXCLUDED from `all`; each shown to the user before it runs):

| Phase | Side | When | What |
|---|---|---|---|
| `cust163_seed` | source | before Run 1 | `wsmig_test_query` owner → tanveer.singh; 3 new UC pipelines (never started): `wsmig_test_pipeline_cust1` (owner = QA user), `…_cust1_sp` (owner = `ai27_acc_spn_1`), `…_cust1_sp3` (owner = `ai27_acc_spn_3`), each CAN_RUN `ai27_account_grp_1` + CAN_VIEW a user; one extra grant on `wsmig_test_pipeline`; `wsmig_test_alert_v2` cron → every 15 min (original saved); Genie `wsmig_qa_163_genie` on `retry_tbl_163` (source-only table) |
| `cust163_target_prep` | target | before Run 1 | `enableResultsDownloading` → `true` (source = `false`); **UC grant (user OK):** `USE CATALOG` + `USE SCHEMA` on `catalog_src_g9c6nd`/`wsmig_test` to `ai27_acc_spn_1` and `ai27_acc_spn_3` — a run-as SP needs them (live 403 otherwise) |
| `cust163_run2` | both | before Run 2 | `wsmig_test_query` owner → aman.bansal; **grant (user OK):** the runner gets `servicePrincipal.user` on `ai27_acc_spn_3`; plant `wsmig/<src>/20200101_000000/` (copied manifest + an unfinished import checkpoint) |
| `cust163_heal` | target | before Run 3 | create the empty `retry_tbl_163` on target |
| `cust163_cleanup` | both | after Run 3 | restore the alert cron; remove the planted folder |

| Run | What | Expect |
|---|---|---|
| **1 — upgrade** (pull 1.2.0, run the e2e job) | the customer's first run on 1.2.0 | **§1:** each task's output ends with `Run id` + `Folder`, and that folder holds this run's reports. **§4:** every query's owner = its source owner (live GET), rows `updated` "owner set … (verified)", no content PATCH. **§2:** `enableResultsDownloading` re-applied and reads `false`; other keys SKIP with no write. **§5:** `_cust1` → IS_OWNER = runner, QA user CAN_MANAGE, other grants exact, runs as the runner; `_cust1_sp` → IS_OWNER = runner, runs as `ai27_acc_spn_1`, SP CAN_MANAGE; `_cust1_sp3` → `created_with_warning` "grant the runner the Service Principal User role", runs as the runner; `wsmig_test_pipeline` → new grant applied, owner unchanged; parity `match` + known_gap. **§7:** the Genie row FAILED with the server's `Table … does not exist`, not "targets workspace folder". **§8:** both Alerts V2 `updated` once (D4). No duplicates; `logcheck.py` clean. |
| **2 — change + repair** (`cust163_run2`, then e2e: cancel during import → Repair run) | an incremental change, idempotency, repair | Only `wsmig_test_query` (owner → aman.bansal, verified) and `_cust1_sp3` (now runs as `ai27_acc_spn_3`) are written; **everything else SKIPs with zero writes** from the new rechecks (queries, settings, pipeline run-as). The alert evaluated since Run 1 and is `skipped`. Inventory/export start a **fresh** folder, not the planted one. The repaired import uses the same folder, logs `resumed from checkpoint`, writes one final xlsx, `rows=N expected=N`, no duplicates. |
| **3 — operator retry** (`cust163_heal`, then standalone import, `failed_only`, blank `run_id`) | the operator flow | `resolved via: LATEST_EXPORT.json` (Run 2's folder, not the planted one); `import_status_retry_<ts>.xlsx` there; only failed units acted on; `wsmig_qa_163_genie` `created` with its ACL. Then `cust163_cleanup`. |

**Not live-testable here:** §6 synced-table pipelines (no synced table, **D3**); Lakeflow Connect gateways (need an
external DB connection); §7's confirmed-missing-folder path (homes exist); a setting the target silently drops (only
if this target drops one); §3 report-write failure (offline).

---

## 12. Decisions (the dev builds the defaults unless the user says otherwise)
- **D1 — §5:** give the source owner `CAN_MANAGE` on the migrated pipeline. **Default: yes.** Without it, the original owner loses all access to their pipeline.
- **D2 — §2:** a setting that differs on the target is re-applied on every run. This heals the false `Created`, and also reverts hand changes on the target. **Default: yes.**
- **D3 — §6:** offline tests, plus the customer's next import as the live proof. **Default: yes.** The alternative, a Lakebase synced table on the source bed, costs money and needs the user's OK.
- **D4 — §8:** accept one `updated` per Alert V2 on the first 1.2.0 run. **Default: yes.**

## 13. Review checklist (Claude, before live QA)
- The diff touches only the files in §9.
- The fingerprint-stability test passes, with only `alert_v2` moving.
- No new silent `except`.
- §1 is the end-of-output print, import = `LATEST_EXPORT` and inventory/export newest-only, nothing else: no template, installer or naming change.
- Resume looks at the newest folder only.
- `legacy_query` adopt keeps the stale-update rule.
- The pipeline ACL rule touches `perm_type == "pipelines"` only, never sends the source `IS_OWNER`, and never guesses an owner.
- `run_as` is set only for an SP source owner, and a refused `run_as` degrades to a warning, never a failure.
- The CUST-2 rule runs at both export and import.
- A report failure can't mask an abort.
- §7: our message only after a real 404, and 1.1.1's text is unchanged.
- §8: only the two `evaluation.*` fields are stripped.
- The CHANGELOG matches §10.
- §9b is filled in.

## 14. Findings
### Review (2026-10-14, Claude) — two small code changes, 601 tests pass
- **Lakeflow Connect gateway** (`gateway_definition`, no `libraries` by design) would have become `manual` with the
  misleading "no source code" note. User decision: keep it `manual` (it needs a UC connection, out of scope) and
  document Lakeflow Connect as a manual step. `pipeline_not_creatable` now returns a note naming the connection;
  test `test_lakeflow_connect_gateway_is_manual_with_a_uc_connection_note`. Docs: Runbook, Permissions Guide,
  Architecture manual table, README, CHANGELOG.
- **Run-as refusal matched on guessed words.** Live probe (runner = workspace admin with only
  `servicePrincipal.manager` on the SP), create AND update → `403 PERMISSION_DENIED Cannot bind the service principal
  provided in 'run_as' field (<sp>) … must have 'servicePrincipal.user' role`. Workspace admin is NOT exempt. A run-as
  SP lacking `USE CATALOG` is a different 403. `_RUN_AS_REFUSED_MARKERS` now requires all of
  `permission_denied` + `'run_as'` + `servicePrincipal.user`; the fake uses the live text; new test
  `test_other_permission_denied_with_run_as_is_not_retried_and_fails`. (Mutation note: the old loose list also let the
  USE CATALOG 403 through — the tightening guards unseen errors.) Evidence:
  `plan16_3/api_probes/2026-10-14_runas_refusal.json`.

(live QA appends here)
