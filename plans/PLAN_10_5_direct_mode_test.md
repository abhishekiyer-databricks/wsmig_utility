# PLAN 10.5 — Direct-mode incremental testing (exact mirror of PLAN 10, direct connectivity)

**Status:** ✅ DONE 2026-08-29. 79/79 verdicts match airgap (77 exact + 2 improved: G1, DB2). BUG-1
reproduced in direct (mode-independent). Both credential paths proven. Deliverables `direct_*` in
`~/Downloads/wsmig_runs/`. Full write-up in memory `plan10-5-direct-mode-test`. (Original plan below.)
**Goal:** Reproduce the PLAN 10 airgap campaign in **`direct` mode** and prove the outcomes are
**identical** — because `04_Import` is mode-agnostic, only `01_Inventory`/`02_Export` differ (they
read the SOURCE over OAuth M2M instead of reading the local workspace + writing an air-gapped bundle).
Same methodology, same change matrix (same KINDS), same deliverables — with the **`direct_` prefix**.

## Operating model (what "direct" changes vs airgap)
- **Everything runs as in-workspace Jobs in the TARGET workspace, run-as the target SPN**
  (`wsmig_target_spn` / `22222222`). `01`+`02`+`04` all run there.
- `01_Inventory` + `02_Export` read the **source over REST via OAuth M2M** using the source
  workspace-admin SP `11111111` (`ai27_umi`) client_id + secret (secret in
  `~/Desktop/Client ID: REDACTED….md`, value `REDACTED…`). Verified live earlier that this SP mints a
  token and reads source SCIM/assets.
- **No air-gap transfer** — the bundle is written straight to the single target `staging_location`
  Volume, so there is no source-Volume→target-Volume CLI hop. Each "run" is one trigger of the
  packaged **`direct_end_to_end_live`** job (01→02→04) with a fresh `run_id`.
- CLI stays control-plane only (create Git folder, `00_Install_Jobs`, trigger/monitor, read Volumes).
- **Source↔target network reachability is required** in direct mode — both workspaces are public
  Azure Databricks endpoints, so the target can reach the source. (Confirm at pre-flight.)

## Direct-specific credential rule (from the original brief)
- **Run A (baseline) uses the secret VALUE directly** — `spn_secret_value` widget with
  `allow_secret_in_job_params=true` (or passed as a run-now `notebook_params` override so it isn't
  persisted in the job def).
- **Run C (incremental) uses the SECRET-SCOPE pointer** — create a Databricks-backed secret scope on
  the TARGET, put the source SP secret in it, and pass `source_sp_secret_scope` + `source_sp_secret_key`.
- This exercises BOTH source-credential paths (widget value + scope pointer), always redacted from
  artifacts/logs. (Run B seed-catch-up can use either; use the scope path to warm it.)

## Environment (fresh target — verify every value at pre-flight; STOP if a mismatch is unexplained)
- **Git:** repo `https://github.com/abhishekiyer-databricks/wsmig_utility`, branch
  **`feature/ws_import`** (same commit as PLAN 10 = `6b08015`, PLAN 9 code). Create a Repos/Git
  folder on the target only (direct runs everything there). **Code version: run direct on the SAME
  code as airgap** (pre-PLAN-11) so it is a true mirror AND so BUG-1 is expected to REPRODUCE in
  direct (confirming the bug is mode-independent). PLAN-11 fixes get their own validation later.
- **source_ws:** `adb-1234567890999999.19` (profile `source_ws`), UNCHANGED. Source SP `11111111`
  (`ai27_umi`, source admin). This is the shared source that the airgap campaign already mutated —
  see "Source-state handling" below.
- **target_ws (fresh, blank, NEW REGION):** host **`adb-1000000000000005.10`** — profile `target_ws`
  (user is re-authing; a typo `targst_ws` was created first — use `target_ws`). NEW REGION ⇒ a NEW
  regional **metastore** (distinct from the airgap target's). Cross-region source↔target OAuth M2M is
  fine (public endpoints). Run-as SPN `22222222` (`wsmig_target_spn`): if the fresh workspace lacks
  it, create/import it + make it a **workspace admin**; grant the CLI user `servicePrincipal.user` on
  it so jobs can bind run_as (as in PLAN 10).
- **Target UC for state + staging:** user-provided catalog **`catalog_8_kowyhc`**. Create a NEW schema
  **`wsmig_direct`** in it for the state tables + a fresh staging Volume
  **`/Volumes/catalog_8_kowyhc/wsmig_direct/wsmig_stage`**. Grant the target SPN
  `USE_CATALOG/USE_SCHEMA/CREATE_TABLE/MODIFY/SELECT` on the schema and `READ/WRITE_VOLUME` on the Volume.
- **UC objects referenced by assets — I MUST CREATE THESE (user instruction):** DLT pipelines + Genie
  (+ the trips-referencing alerts A1/A2) reference **`catalog_ws_bozvdk.wsmig_test.trips` / `.zones`**
  by FQN (Genie serialized_space + alert `query_text` are literal FQNs; UC is out of scope so the
  tool does NOT remap them). On this NEW metastore they don't exist → without them DLT×3 + Genie
  (+ alert validity) fail `permission_denied`. **Pre-flight step 0 (below): create catalog
  `catalog_ws_bozvdk` + schema `wsmig_test` + EMPTY tables `trips`,`zones` on the new metastore,
  mirroring the SOURCE table schemas, and grant the target SPN `USE/SELECT`.** Try a managed catalog
  first (`CREATE CATALOG catalog_ws_bozvdk` — works if the metastore has default managed storage);
  if the metastore has no default storage, build an ADLS-backed UC stack in THIS region (access
  connector + storage credential + external location + catalog) per [[target-ws-uc-stack-bozvdk-2026-08-18]].

## Structure (IDENTICAL to PLAN 10 — 3 runs, fresh EMPTY state schema, NO target deletion/reset)
- **Run A = baseline** (`direct_1`) — full migration; secret-value credential path.
- **Seed** the update/delete-target identity resources (fresh, `wsmig_p10d_*`), then **Run B =
  catch-up** (`direct_1b`) — folds seeds into the SAME state store.
- **Apply the change matrix** (same KINDS as PLAN 10, re-targeted — see below) → `direct_source_changes`.
- **Run C = incremental** (`direct_2`) — secret-scope credential path; same state store, fresh run_id.
- Reuse the SAME (new) target state schema across all three so UPSERT fingerprints drive detection.

## Source-state handling (the ONE real decision — see "Open decisions")
The shared **source was already mutated by the airgap campaign** (77 changes + deletes + renames +
new resources + seeds). **Recommended = Approach A (fresh direct campaign, no source reset):**
- Direct **baseline captures the current source as-is** (fine — a baseline just needs current state).
- The change matrix is **re-applied with the same KINDS/families/count, re-targeted** so each case is
  a genuine delta against the direct baseline: CREATE cases use fresh names (`*_d` suffix), DELETE
  cases delete fresh throwaway seeds (`wsmig_p10d_*`), UPDATE cases change an as-yet-unchanged
  attribute / use a member or value not already present (e.g. a different test user).
- This proves **mode-equivalence per change kind** (the actual objective) without a risky source
  reset. Resource instances differ from airgap by design; verdicts should match.
- **Approach B (reset source to baseline first)** = only if the user wants literally identical
  resource instances. Heavy + risky (must recreate deleted SPs/job/notebook/scope, revert renames,
  strip added members/entitlements/ACLs, restore schedules/tags). Not recommended.

## Deliverables (all under `~/Downloads/wsmig_runs/`, `direct_` prefix — same shapes as airgap)
1. `direct_1_import.xlsx` — Run A baseline import report.
2. `direct_1b_import.xlsx` — Run B seed-catch-up report.
3. `direct_2_import.xlsx` — Run C incremental import report.
4. `direct_source_changes.xlsx` — change log (cols: Case · Resource Type · Resource Name · Update
   Done · New-or-Existing · How Applied · Capability · Timestamp · Expected).
5. `direct_incremental_test_report.xlsx` — per-change verdict (Captured / Correct-skip / By-design /
   Bug / N-A) with a column comparing to the airgap verdict (expected: identical).
6. Final response: **job-run URLs** (target end-to-end job, all 3 runs) + **output Volume paths**
   (single target bundle per run — no source-side bundle in direct mode).

## Execution checklist (mirror of PLAN 10, adapted for direct)
0. **UC prep on the new metastore (do FIRST):**
   a. Verify target host `adb-1000000000000005.10` + catalog `catalog_8_kowyhc` reachable.
   b. Create schema `catalog_8_kowyhc.wsmig_direct` + Volume `.../wsmig_direct/wsmig_stage`.
   c. Read the SOURCE schemas of `catalog_ws_bozvdk.wsmig_test.trips` and `.zones` (via `source_ws`),
      then CREATE catalog `catalog_ws_bozvdk` + schema `wsmig_test` + EMPTY `trips`/`zones` mirroring
      those columns on the new metastore. Managed catalog first; ADLS-stack fallback if no default
      storage ([[target-ws-uc-stack-bozvdk-2026-08-18]]).
   d. Grant target SPN: state schema `USE_CATALOG/USE_SCHEMA/CREATE_TABLE/MODIFY/SELECT` + Volume
      `READ/WRITE_VOLUME`; `catalog_ws_bozvdk` `USE_CATALOG/USE_SCHEMA/SELECT` (+ `CREATE`/`MODIFY` if
      the DLT pipelines write there).
1. **Pre-flight:** verify target SPN present (create+admin if missing) + grant CLI user
   `servicePrincipal.user` on it; **verify direct connectivity** by minting an `ai27_umi` M2M token
   against the source and reading source SCIM (1-line curl, control-plane) before installing jobs.
2. **Git folder** on target (branch `feature/ws_import`).
3. **Install jobs** (`00_Install_Jobs` as a job on target): `deploy_jobs=direct_end_to_end_live`,
   `connectivity_mode=direct`, `run_as_sp=22222222`, `source_workspace_id=1234567890999999`,
   `source_workspace_url=https://adb-1234567890999999.19.azuredatabricks.net`,
   `source_sp_client_id=11111111-…`,
   `staging_location=/Volumes/catalog_8_kowyhc/wsmig_direct/wsmig_stage`,
   `state_catalog=catalog_8_kowyhc`, `state_schema=wsmig_direct`. Leave the credential to run-now overrides.
4. **Run A (baseline):** `run-now` the end-to-end job with `notebook_params` overriding `run_id`
   + `spn_secret_value=<value>` (+ `allow`-equivalent) and `dry_run=false`; monitor 01→02→04; capture
   URLs; download `reports/import_status.xlsx` → `direct_1_import.xlsx`; validate vs live target.
5. **Seed** (`wsmig_p10d_*` account/entra groups + SPs, originals) → **Run B** (scope credential
   path) → `direct_1b_import.xlsx`.
6. **Apply the re-targeted change matrix** (reuse `/tmp/wsmig_changes/` drivers with `_d` names / new
   members+values) → `direct_source_changes.xlsx`. Same families/kinds/count as airgap.
7. **Create target secret scope** + put the source SP secret (for the Run-C scope path).
8. **Run C (incremental):** `run-now` with `run_id` + `source_sp_secret_scope`/`source_sp_secret_key`
   (scope path), `dry_run=false`; monitor; download → `direct_2_import.xlsx`.
9. **Audit + build** `direct_incremental_test_report.xlsx`; compare each verdict to the airgap
   result. **Expectation: identical detection outcomes.** Any delta MUST trace to environment (UC
   catalog presence on the fresh target, fresh target freshness) — never to the mode.
10. **Bug re-confirmation:** BUG-1 (alert update dropped) is expected to REPRODUCE in direct
    (mode-independent) → strengthens the RCA. Record it.
11. **Final report:** URLs + Volume paths + per-change comparison + any environmental deltas explained.

## Locked decisions (confirmed by user 2026-08-28/29)
1. **Target = fresh workspace in a NEW REGION**, host `adb-1000000000000005.10`, profile `target_ws`.
   State catalog = **`catalog_8_kowyhc`** (+ new schema `wsmig_direct` + Volume). I also CREATE
   `catalog_ws_bozvdk`.`wsmig_test` + empty `trips`/`zones` on the new metastore for DLT/Genie/alerts
   (user instruction). Nothing else needed to start.
2. **Source-state = Approach A (CONFIRMED):** the current (airgap-mutated) source IS the new
   baseline. **Add new resources as needed (like last time) and RE-TEST EVERY CHANGE in the matrix**
   — all cases, exactly like the airgap run, re-targeted to fresh instances/values where a resource
   was consumed or an attribute already matches so each case is a genuine delta. No source reset.
3. **Code version = SAME pre-PLAN-11 code (CONFIRMED):** true mirror; BUG-1 is expected to reproduce
   in direct, confirming it is mode-independent. PLAN-11 fixes validated separately later.

## Notes
- Import is mode-agnostic by construction, so `direct_*` verdicts should match `airgap_*` verdicts
  case-for-case; the value of this run is proving the **direct source-read + straight-to-Volume**
  path (01/02) works in-workspace run-as-SPN and yields the same import behavior.
- Related: [[plan10-incremental-airgap-test]] [[not-migrated-catalog-for-prod-docs]]
  [[two-connectivity-modes-airgap-and-direct]] [[direct-mode-source-sp-credential]]
  [[direct-mode-source-sp-must-be-ws-admin]].
