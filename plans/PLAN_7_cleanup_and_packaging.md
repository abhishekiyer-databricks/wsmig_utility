# PLAN 7 — Cleanup, widget slimming, staging layout & job packaging

Status: **DRAFT v2 for review** (2026-08-09). No code written yet. This plan is the review gate;
development starts only after sign-off.

Driven by the first full customer-style direct-mode run on `target_ws` (2026-08-09) and the
operator's review. Themes: **(A) behavioural fixes**, **(B) deletions / output slimming**,
**(C) widget slimming + renaming**, **(D) staging-location layout**, **(E) job packaging**.

Operator decisions already locked in this revision:
- A1, A2, A4 → do. **A3 (identity-map slimming) → DROPPED — keep the full identity table as-is
  (more information is better).**
- B1 (delete 5 notebooks) → do. B3 (keep `preflight.py`) → yes.
- **B4 changed:** do NOT add manifest reconciliation. Inventory = the complete list, export states
  export status per asset, import states import status per asset — that chain is sufficient.
  Instead, REMOVE redundant HTML/JSON outputs (see §B).
- C (jobs) → do, all four job types.

---

## 0. Findings that need NO code change (recorded so we don't re-litigate)

- **gson library didn't install (cluster TERMINATED).** Working as designed (D6). The knob
  `library_force_start_clusters` **already exists** as a widget (`04_Import.py:67`). Purely
  operational: set it `true`, re-run with `retry_mode=failed_only`. Tool starts → installs → stops,
  only stopping clusters it started (`misc_importer.py:133-163`). **No change.**
- **ACLs on "Adopted (pre-existing)" resources ARE applied** — declarative phase resolves the target
  id for adopted objects too (`acl_importer.py:108,316-321`). **No change.**
- **Content under a recreated SP home DOES follow to the new appId** (`workspace_importer.py:152-171`).
  **No change** (but A2 clarifies the orphan message).
- **Deleting `00_Account_Preflight` does NOT disable preflight** — `04_Import` runs the gate itself
  (`04_Import.py:226-228`). `src/importers/preflight.py` STAYS.

---

## A. Behavioural fixes

### A1. Separate dry-run report filename so a live run doesn't overwrite it
- **Finding:** `import_status.xlsx` is a fixed name (`import_report.py:124`); a live run overwrites
  the rehearsal's file.
- **Do:** when `summary["dry_run"]` (already at `import_report.py:97`), write
  `import_status_dry_run.xlsx`; live run keeps `import_status.xlsx`. (No HTML twin — see §B, the
  HTML is being removed.)
- **Test:** dry-run build writes `*_dry_run.xlsx` and never touches the live name; live build writes
  the plain name. (offline)

### A2. Orphan-SP-home message: distinguish "deleted in source" vs "not migrated"
- **Finding:** three `/Users/<uuid>` SP-home dirs reported as "SP not in identity map"
  (`workspace_importer.py:191-198`) without saying *why* the appId is missing.
- **Do:** enrich the message using the bundle's `identity_classification.json` (already in the
  bundle): if the appId appears in the source roster → "present in source but not migrated this run
  (identity skipped/filtered)"; if absent → "deleted in source"; else "unknown".
- **Test:** fixtures with (a) appId in roster but not in `sp_mapping`, (b) appId absent from roster →
  two distinct messages. (offline)

### A4. Add a "workspace-local SP recreated → retains source ACL" end-to-end test
- **Finding:** unit path is tested (`test_importers_phase6_12.py:468`), but the live fixture chain
  (ws-local SP recreated with new appId AND holding a grant, asserting the target grant names the
  NEW appId) is unconfirmed.
- **Do:** add that chain to `tests/fixtures_fvm1.py` (a ws-local SP + a grant on a cluster/job) and
  assert ACL parity on the recreated id in `live_e2e_migration.py`.
- **Test:** the live parity assertion + an offline fixture-shape test.

### A3. DROPPED
Identity map stays exactly as-is (`state_store.py:187-199`). Operator prefers retaining full
identity information. No change.

---

## B. Deletions / output slimming

### B1. Delete 5 notebooks (operator-approved)
Delete: `05_Validate.py`, `00_Main_Source.py`, `00_Main_Target.py`, `00_Main_EndToEnd.py`,
`00_Account_Preflight.py`. Keep `src/importers/preflight.py` (used inside `04_Import`).
- **Touch:** delete files; update references in CLAUDE.md notebook table, PLAN_0 §4, README, and any
  `%run`/`dbutils.notebook.run` (only `00_Main_EndToEnd` did that, and it's going). The stitching it
  provided moves into the packaged all-in-one JOB (§E).
- **Test:** repo-wide grep shows no reference to any deleted file; offline suite green.

### B2. Remove redundant HTML/JSON outputs — keep the chain lightweight
The reporting chain is: **inventory (complete list) → export_status (per-asset export status) →
import_status (per-asset import status)**. That is sufficient; everything else is redundant
presentation. Remove:
- `import_results.html` — the xlsx already carries every unit + status. **Remove.**
- `preflight_report.html` — preflight prints its graded verdict inline in the notebook; the HTML is
  redundant. **Remove.** Keep `preflight_report.json` in `misc/` (small, machine-readable, for
  audit — the manifest already excludes it).
- `acl_parity_report.html` — **Remove.**
- `acl_parity_report.json` — **RECOMMENDATION: fold the parity result into `import_status.xlsx` as
  an "ACL Parity" sheet, then remove the standalone .json+.html.** Rationale: parity is NOT the same
  as import status — import_status says "we attempted to apply N grants", parity RE-READS every
  touched target object and diffs it against source (independent proof grants actually landed,
  `acl_importer` parity pass). That verification is worth keeping, but it doesn't need its own file;
  a sheet in the workbook keeps it lightweight. *(Decision D-1 below — confirm fold-vs-drop.)*
- `inventory.html` — **comment out** its generation (leave the code + a one-line switch) so it's
  trivially re-enabled if a customer asks. `inventory.xlsx` remains.
- **Net effect on `src/reports/html_generator.py`:** it becomes (near-)unused. Keep the module but
  stop calling it (or gate all callers behind a `write_html=False` default), so re-enabling is a
  one-liner rather than a resurrection.
- **Touch:** `src/reports/import_report.py` (drop html writer; add ACL Parity sheet if D-1=fold),
  `src/importers/preflight.py` / `acl_importer.py` (stop writing their html/json standalone),
  `notebooks/04_Import.py` (drop the html/parity-json read+print blocks at :297-314, :323-325),
  `notebooks/01_Inventory.py` (gate inventory.html).
- **Test:** an import run writes `import_status.xlsx` (with the ACL Parity sheet if folded) and NONE
  of the removed files; a preflight writes only `preflight_report.json`. (offline)

### B3. Keep `src/importers/preflight.py`
Noted explicitly so it's not deleted with the notebook.

---

## C. Widget slimming + renaming (all three notebooks)

**DECISION (D-3): TRIM ONLY — do NOT rename widgets.** Param names already follow best practice; only
the display labels were long. So: keep every widget NAME as-is, just shorten the long display labels
(free, no job-param impact) and REMOVE the widgets marked below. The rename columns in the tables
below are therefore informational only and will NOT be applied.

**Principles (operator directive):**
1. Keep existing snake_case widget names (they double as job params); just shorten overlong display
   labels. No renames.
2. Collapse `source_staging_location` + `target_staging_location` → **one `staging_location`**. Each
   run writes/reads exactly one location; the airgap file hop is just "source side sets location A,
   target side sets location B" — two separate notebook runs each with their own single value. The
   `Config.staging_location` property already collapses them internally, so the second widget was
   always redundant. **Agree — collapse.**
3. Remove `role` — it is derivable. Inventory/Export are always the source-reading stages
   (role=source in airgap, role=target in direct); Import is always role=target. Derive role from
   `connectivity_mode` + which notebook, inside `Config`/each notebook. The old role guard becomes an
   internal assertion, not a widget. **Agree — remove.**
4. Remove `verbose` (inventory) — logging verbosity shouldn't be a customer knob. **Agree — remove.**
5. Keep `connectivity_mode` (D-2) — **default `direct`**. It decides whether inventory/export read
   the LOCAL workspace (airgap) or reach the SOURCE over REST via OAuth M2M (direct), and whether
   there's a manual file hop. Explicit widget, no auto-derive.
6. `force_full*` widgets keep their existing per-notebook names (no rename per D-3).

### Proposed unified widget names (confirm the renames — D-3)

**Common to all three notebooks:**
| Current | Proposed | Default | Keep? |
|---|---|---|---|
| `connectivity_mode` | `connectivity_mode` | `airgap` | keep |
| `role` | — | — | **REMOVE** (derive) |
| `source_workspace_id` | `source_workspace_id` | "" | keep |
| `source_staging_location` + `target_staging_location` | `staging_location` | "" | **MERGE to one** |
| `run_id` | `run_id` | "" (blank = auto/resume) | keep |
| `source_workspace_url` | `source_workspace_url` | "" | keep (direct) |
| `source_sp_client_id` | `source_sp_client_id` | "" | keep (direct) |
| `source_sp_secret_scope` | `source_sp_secret_scope` | "" | keep (direct) |
| `source_sp_secret_key` | `source_sp_secret_key` | "" | keep (direct) |
| `spn_secret_value` | `source_sp_secret_value` | "" | keep, **rename** for prefix consistency |

**Inventory-only:**
| Current | Proposed | Default | Keep? |
|---|---|---|---|
| `max_scim` | `max_identities` | `0` | keep (rename) |
| `max_workspace_items` | `max_workspace_items` | `0` | keep |
| `max_ws_api_calls` | `max_list_calls` | `0` | keep (rename) |
| `force_full` | `force_full_refresh` | `false` | keep (rename) |
| `verbose` | — | — | **REMOVE** |

**Export-only:** same `max_*` renames, `content_fetch_workers` (default `8`, keep),
`force_full_export` → `force_full_refresh`, plus the 11 `migrate_*` toggles (keep — bundle scope
belongs on the source side).

**Import-only:**
| Current | Proposed | Default | Keep? |
|---|---|---|---|
| `dry_run` | `dry_run` | `true` | keep |
| `state_catalog` / `state_schema` | same | "" | keep (required when live) |
| `import_assets` | `import_assets` | `all` | keep |
| `retry_mode` | `retry_mode` | `off` | keep |
| `preflight_enforce` | `preflight_enforce` | `true` | keep |
| `library_force_start_clusters` | `library_force_start_clusters` | `false` | keep (the gson knob) |
| `pause_job_schedules` | `pause_job_schedules` | `true` | keep |
| `account_id` | `account_id` | "" | keep (optional) |
| `user_domain_mapping` / `user_id_mapping` | same | "" | keep (transform) |
| `force_full_import` | `force_full_refresh` | `false` | keep (rename) |
| `allow_deletes` | `allow_deletes` | `false` | keep (safety) |
| `skip_manifest_verify` | — | — | **REMOVE** (debug-only) |
| `migrate_*` (11) | — | — | **REMOVE from import** (redundant with `import_assets`; keep on source) |

**Direct-mode minimal set the customer actually fills** (matches operator's target):
`source_workspace_id`, `staging_location`, `source_workspace_url`, `source_sp_client_id`, and the
secret (either `source_sp_secret_scope`+`source_sp_secret_key` OR `source_sp_secret_value`).
Everything else has a working default.

- **Touch:** all three notebooks' widget blocks; `src/config/config_manager.py`
  (`from_dbutils` reads the merged `staging_location`; derive `role`; drop `verbose`/`role`/
  `skip_manifest_verify`; rename map). `_widget(..., default)` already tolerates absent widgets, so
  removed widgets fall back cleanly.
- **Migration nicety:** `from_dbutils` should read `staging_location` first, and fall back to the
  old `source_staging_location`/`target_staging_location` if only those are present, so an in-flight
  job param JSON doesn't break on upgrade. (Remove the fallback in a later cleanup.)
- **Test:** `Config.from_dbutils` builds from the trimmed set; a direct config with only the minimal
  widgets validates; role is derived correctly for each (notebook, mode) pair; the old→new staging
  fallback works. (offline)

---

## D. Staging-location layout (operator-requested reorg)

**Today:** everything lands flat in the run dir, plus an `export/` subtree. **Target layout:**
```
<staging_location>/wsmig/<source_workspace_id>/
  LATEST_INVENTORY.json          # pair-level pointers stay ABOVE the run dir
  LATEST_EXPORT.json
  <run_id>/
    export/                      # exported asset payloads + bytes + acls.json  (UNCHANGED)
      identity/ compute/ workspace/ secrets/ dashboards/ misc/ ...
    reports/                     # human-facing outputs
      inventory.xlsx
      export_status.xlsx
      import_status.xlsx         (or import_status_dry_run.xlsx on a rehearsal)
      manual_actions_import.md
      # inventory.html           (generation commented out — enable if a customer asks)
    misc/                        # machine / bookkeeping
      inventory.json
      identity_classification.json
      export_index.json
      config_resolved.json
      manifest.json
      checkpoint.json
      import_results.json
      preflight_report.json
      # acl_parity_report.json   (only if D-1 = keep-standalone; default is folded into xlsx)
      execution_inventory.log / execution_export.log / execution_import.log
```
- **Design recommendation — a central path registry.** Introduce a small
  `src/exporters/bundle_paths.py` (or constants on `ArtifactWriter`) mapping logical name → relative
  path (`REPORTS/…`, `MISC/…`, `EXPORT/…`). Every collector/exporter/importer/report writes/reads via
  the registry instead of hardcoded strings, so this layout lives in ONE place and future moves are
  a one-line change. This is the safe way to do an otherwise-invasive path change.
- **`ArtifactWriter` changes:** `ensure_output_path` creates `reports/` + `misc/` alongside
  `export/`; `manifest.json` moves to `misc/manifest.json` and `verify_manifest`/`build_manifest`
  read/write it there (manifest still records paths RELATIVE to the run root, so files moving into
  `reports/`/`misc/` is fine — the rel paths just gain a prefix). `_excluded_from_manifest` updated
  for the new locations (still exclude `manifest.json`, `checkpoint.json`, `execution*.log`,
  import-side outputs).
- **Everything that reads a moved file** (notably `04_Import` reading `export_index.json`,
  `config_resolved.json`, `import_results.json`; importers reading `export/acls.json` — which stays
  under `export/`) switches to the registry.
- **Test:** a full inventory→export→import cycle writes each file to its new home; `verify_manifest`
  passes against the relocated `misc/manifest.json`; import reads the relocated index/config; the
  pair-level `LATEST_*.json` pointers still resolve. (offline + the live e2e)

---

## E. Pre-packaged jobs (non-DAB, Git-folder friendly) — operator-approved full set

### Approach (recommended)
The repo is a **Git folder** in the workspace (no DAB, no CLI/terminal available). Package jobs as
**checked-in JSON templates + an idempotent installer notebook**:
1. **`jobs/*.job.json`** — one Jobs API 2.2 definition each. Task `notebook_task.notebook_path` is
   the repo-relative notebook; `base_parameters` carry the trimmed §C widget set with blanks.
   Placeholders (`{{REPO_PATH}}`, `{{RUN_AS_SP}}`) for workspace-specific bits.
2. **`notebooks/00_Install_Jobs.py`** — the customer fills EVERY value ONCE here as widgets; the
   installer writes them into each created job's `base_parameters`, so the jobs come out pre-filled
   and the customer never re-types per run. Its widgets = the full config set: `run_as_sp`,
   `connectivity_mode`, `source_workspace_id`, `staging_location`, `source_workspace_url`,
   `source_sp_client_id`, the secret (`source_sp_secret_scope`+`source_sp_secret_key` OR
   `spn_secret_value`), `state_catalog`, `state_schema`, `account_id`, and the common defaults
   (`dry_run`, `import_assets`, `retry_mode`, `pause_job_schedules`, …). It resolves the repo path
   from the notebook context, then for each template projects ONLY the params that template's tasks
   declare (an inventory job gets no `state_catalog`; an import job gets no `content_fetch_workers`),
   fills `run_as`, and calls `POST /api/2.2/jobs/create` — or `jobs/reset` when a job of that name
   already exists (idempotent, keyed by name). No warehouse input (state uses Spark in a notebook;
   `state_warehouse_id` was only for the non-notebook harness). Prints created/updated job ids. Runs
   entirely in-workspace.
   - **Secret handling:** prefer the `source_sp_secret_scope`+`source_sp_secret_key` pointer — the
     secret is then NEVER written into `base_parameters` (job params are visible on the run/job page).
     If the customer supplies `spn_secret_value`, the installer WARNS that it will be stored in job
     params in cleartext and recommends the scope path. (Consistent with the existing redaction rule.)
3. **The installer lets the customer SELECT which job(s) to deploy** (a `multiselect` widget over the
   job list below — deploy one, some, or all). It doesn't make sense to create every job in every
   workspace; most customers pick the direct-mode dry-run + live pair.

### Jobs to ship (selectable in the installer)
Two full end-to-end jobs differing ONLY in the import task's `dry_run`, so "rehearse first, run live
later" is just "run the dry job, then run the live job" — no parameter to flip:
- **`jobs/direct_end_to_end_dry_run.job.json`** — `01 → 02 → 04` (`depends_on`), direct mode, import
  task pinned `dry_run=true`. The safe first run; writes to the `_dryrun` twin state table and
  `import_status_dry_run.xlsx`, so it can't pollute real state. Customer reads that report, and if
  satisfied can either run the live job OR simply **"Run now" the import task alone with
  `dry_run=false`** (the bundle is already staged, so inventory/export needn't repeat).
- **`jobs/direct_end_to_end_live.job.json`** — identical graph, import task `dry_run=false`. Together
  these replace the deleted `00_Main_EndToEnd` notebook; stitching lives in the job graph, each task
  independently re-runnable/checkpointed. Preflight still runs inside `04_Import`.
- **`jobs/inventory.job.json`** — single task `01_Inventory`.
- **`jobs/export.job.json`** — single task `02_Export`.
- **`jobs/import.job.json`** — single task `04_Import` (`dry_run` is a normal param on this one, for
  running import in isolation / retries).
- **`jobs/airgap_source.job.json`** — `01 → 02` for the source-side airgap deployment.

Why two whole jobs instead of one job-level `dry_run` param: it keeps the customer flow literal and
foolproof — no editing a parameter at "Run now", no chance of a live run mislabelled as a rehearsal.
The `dry_run` value is baked into each end-to-end job's import-task `base_parameters`. (This design
relies on nothing beyond the existing twin-state-table + separate `import_status_dry_run.xlsx`
isolation, so the dry job never touches real state.)

### Notes
- No preflight/validate tasks (preflight runs inside import; validate deleted).
- `run_as` per template = placeholder SP the installer fills (target workspace-admin SP in direct).
- **Touch:** new `jobs/` dir, new `notebooks/00_Install_Jobs.py`, small substitution/create-or-reset
  helper in `src/utils/`. Update CLAUDE.md notebook list.
- **Test:** offline — each template is valid JSON with expected task/notebook_path/param keys and no
  leftover placeholders after substitution against a fake context; the dry job's import task has
  `dry_run=true` baked in and the live job's has `dry_run=false`; the installer's job-selection widget
  deploys exactly the chosen subset. Live smoke — installer creates then idempotently resets one job.

---

## Decisions — ALL RESOLVED
- **D-1 (ACL parity):** RESOLVED — fold parity into `import_status.xlsx` as an "ACL Parity" sheet;
  drop standalone `acl_parity_report.json` + `.html`.
- **D-2 (`connectivity_mode`):** RESOLVED — keep as explicit widget, **default `direct`**.
- **D-3 (widget renames):** RESOLVED — **TRIM ONLY, no renames.** Keep all widget names; shorten long
  display labels; remove the widgets marked in §C.
- **D-4 (job installer inputs):** RESOLVED — installer collects the FULL config set as widgets ONCE
  and writes it into each created job's `base_parameters` (projected to what each job needs), so jobs
  come out pre-filled. No warehouse input. Secret via scope pointer preferred (never persisted into
  params); raw `spn_secret_value` warned about.
- **D-5 (dry-run UX):** RESOLVED — ship TWO end-to-end jobs (dry + live) that differ only in the
  import task's baked `dry_run`, rather than a flip-at-runtime param. The installer has a
  job-selection `multiselect` so the customer deploys only the jobs they want.

## Test budget
Offline: ~10 new/updated tests (A1, A2×2, A4-shape, B2 output-set, C config-build + role-derivation +
staging fallback, D layout + manifest relocation, E template + substitution). Live: extend
`live_e2e_migration.py` with A4 SP-ACL parity + the E install-jobs smoke, and re-verify the D layout
end to end. Existing 243-test offline baseline behaviour preserved (paths updated via the registry).
