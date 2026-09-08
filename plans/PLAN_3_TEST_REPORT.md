# Plan 3 (Import) — Implementation & Test Report

**Date:** 2026-08-05
**Scope:** the whole write half of the utility (`plans/PLAN_3_import.md`)
**Workspaces:** source `fvm1` (`adb-1234567890123456.18`) → target `target_ws` (`adb-1000000000000004.13`)
**Connectivity mode tested:** `direct` (source read over OAuth M2M) — which also exercises the
dual-mode auth path end to end.

---

## 1. What was built

| Deliverable | File(s) | Status |
|---|---|---|
| Dual-mode config | `src/config/config_manager.py` | ✅ `connectivity_mode`, source-SP widgets, state catalog/schema, `import_assets`, `retry_mode`, per-mode `validate()`, `redacted()` |
| Dual-mode auth | `src/auth/token_manager.py` | ✅ `oauth_m2m_token_provider` (cached + auto-refresh), `build_clients`, `mint_aad_token`, `MutationGuard`, `HTTPStatusError` |
| Migration state table | `src/state/state_store.py`, `src/state/sql_backend.py` | ✅ Delta MERGE-on-PK, batched writes, flush-on-failure, recovery replay, retry buckets, identity map, dry-run table |
| Importer framework | `src/importers/base_importer.py`, `phases.py`, `import_runner.py` | ✅ fail-soft per unit, upsert decisions, checkpoint resume, selector + prerequisite validation, run resolution, manifest gate |
| 12 phase importers | `identity`, `compute`, `workspace`, `secrets`, `jobs`, `sql`, `dlt`, `dashboards`, `genie`, `serving`, `misc`, `acl` | ✅ all implemented |
| Preflight gate | `src/importers/preflight.py` | ✅ 13 graded checks (BLOCKING / DEGRADING / COSMETIC), verify-only |
| Import reporting | `src/reports/import_report.py` | ✅ `import_results.{json,html}`, `import_status.xlsx`, `manual_actions_import.md` |
| ACL parity report | `src/importers/acl_importer.py` | ✅ post-apply source-vs-target diff |
| Notebooks | `00_Account_Preflight`, `04_Import`, `00_Main_EndToEnd`, mode-aware guards in `01`/`02` | ✅ |
| Export-side fixes | `content_fetcher`, `export_runner`, `asset_export`, `bundle_state` | ✅ GAP 1 + GAP 2 + repos-manual + `LATEST_EXPORT.json` |

**Still stubs, deliberately (Plan 4):** `03_Transform_Review`, `05_Validate` — the cross-stage
inventoried→exported→imported reconciliation. `import_results.json` is already written in the shape
Plan 4 joins on, so Plan 4 is a reader, not a retrofit.

---

## 2. Offline test suite — 242 tests, all passing

`python3 -m pytest` (safe anywhere; `pytest.ini` excludes the live harnesses).

| File | Tests | Covers |
|---|---:|---|
| `test_state_store.py` | 25 | decision table, batching, flush-on-failure, recovery replay, pair isolation, retry buckets, identity map |
| `test_import_framework.py` | 33 | fail-soft invariant, idempotency, dry-run purity, resume, run resolution, the 4 whole-run gates |
| `test_importers_phase6_12.py` | 40 | sql/dlt/dashboards/genie/serving/misc + all ACL body/skip/parity rules |
| `test_importers_phase2_5.py` | 30 | compute/workspace/secrets/jobs traps (ephemeral, stop-after-create, AKV, remapping) |
| `test_export.py` | 28 | (pre-existing) export engine |
| `test_config_auth.py` | 24 | per-mode validation, secret precedence, redaction, M2M caching, error bodies |
| `test_preflight_and_reports.py` | 22 | preflight GRADING, report shape, manual runbook |
| `test_identity_importer.py` | 17 | create-vs-assign per classification, two-pass groups, entitlements |
| `test_fingerprint_gaps.py` | 10 | GAP 1/2 regressions + a 23-case fingerprint-sensitivity sweep |
| others | 15 | collectors, inventory/export e2e, no-null report |

---

## 3. Live harnesses

| Harness | Result | What it proves |
|---|---|---|
| `tests/live_direct_mode.py` | **13/13 PASS** | M2M token mints against fvm1, reaches admin-only endpoints, two clients bound to different hosts with different identities, secret in no log line |
| `tests/live_state_store.py` | **24/24 PASS** | Real Delta DDL + MERGE-on-PK (upsert in place, no duplicate row), pair isolation in a shared table, quote/newline escaping, identity-map durability, retry work lists, dry-run isolation |
| `tests/live_e2e_migration.py` | **43/43 PASS** | The full migration, twice, with a source mutation in between (see §4) |

---

## 4. End-to-end live migration — **43/43 PASS, 0 failed**

```
  phase 0: 1 passed, 0 failed     source fixture
  phase A: 6 passed, 0 failed     inventory + export over M2M
  phase B: 4 passed, 0 failed     dry run — zero writes
  phase C: 8 passed, 0 failed     live import
  phase D: 3 passed, 0 failed     re-run SKIPs, no duplicates
  phase E: 6 passed, 0 failed     source mutation -> UPDATE
  phase F: 2 passed, 0 failed     adopt, not duplicate
  phase G: 3 passed, 0 failed     retry modes
  phase H: 10 passed, 0 failed    ACL parity + report set
```

Bundle exported from fvm1: **155 units** (130 captured, 0 export failures, 13 DAB-owned, 6 manual)
across 17 identities, 11 compute, 92 workspace objects, 3 secret scopes, 4 jobs, 6 SQL, 3 DLT,
3 dashboards, 2 genie spaces, 1 serving endpoint, 8 misc. Import decided **268 units** including
119 ACL objects.

| Phase | What it asserts | Result |
|---|---|---|
| A | inventory + export over M2M; manifest verifies; `LATEST_EXPORT.json` ties to the manifest; content hashed | 155 units, 0 export failures |
| B | **dry run makes ZERO mutating calls** (counted at the client), still decides every unit, writes only to the `_dryrun` state table | 0 mutations / 268 units decided |
| C | live import completes fail-soft; objects really created; notebook lands as a NOTEBOOK with v1 content; state rows carry BOTH ids; identity map populated | 56 created, 51 adopted, run `completed` |
| D | **re-run SKIPs** unchanged units; no duplicate created | 162 skipped, 3 created, 1 copy of the policy |
| E | **source mutation → fingerprint moves → UPDATE against the STORED target id**; edited notebook content reaches the target | 2 updated, same target id, v2 content live |
| F | an object created by hand is **ADOPTED**, not duplicated | 1 adopted, 1 copy |
| G | `retry_mode=failed_only` attempts only outstanding units; `import_assets=acls` runs ACLs alone | 26 of 268 attempted |
| H | ACL parity diff; the full report set exists and is joinable on `(asset_type, natural_key)` | **43 objects match, 0 divergence** |
| I | cleanup — the target and source are left as found | schema dropped, prefixed objects deleted |

The second live import (phase E's, after preflight ran for real) finished with **zero failures**:
`{skipped_no_object: 75, skipped: 44}` — i.e. everything importable was already correct on target and
the only outstanding rows were the by-design DAB ones.

### The headline result

**Phase E is the one that matters most.** GAP 1 (notebook content not fingerprinted) was the most
damaging silent failure in the design: editing a notebook on source produced an identical
fingerprint, so the importer SKIPped it and the target kept the old code — on a fully green report.
Live, after the fix:

```
[PASS] an EDITED NOTEBOOK's fingerprint CHANGED — v1=sha256:b6897bb810749… v2=sha256:3075488444f9e…
[PASS] the changed units were UPDATED — updated=2
[PASS] the UPDATE edited the stored target id (still ONE policy)
[PASS] the state row kept the SAME target id across the update
[PASS] the notebook's EDITED content reached the target (the GAP 1 failure mode)
```

---

## 5. Bugs found by live testing, and fixed

Every one has a regression test. These are the ones no offline test would have caught.

### 5.1 `checkpoint.json` in the manifest broke the SECOND import (blocker)
The manifest checksummed `checkpoint.json`, but **import writes to it**. So the first import changed
the file, and the manifest gate then refused the bundle on every later run — a perfect bundle
reported as corrupt.
**Fix:** `artifact_writer._excluded_from_manifest` now excludes `checkpoint.json` and the import-side
outputs. The manifest attests to the *exported* bundle, not to per-attempt bookkeeping.
**Test:** `test_import_framework.py::test_the_manifest_survives_an_import_writing_its_own_files`.

### 5.2 API errors carried no explanation (made every failure unactionable)
`raise_for_status()` yields `400 Client Error: Bad Request for url: …`. Nine different live failures
were indistinguishable, and — worse — `classify_error`/`is_already_exists` match on body text, so the
**adopt-on-race path could never fire**.
**Fix:** `ApiClient` folds `error_code` + `message` into `HTTPStatusError`.
**Test:** `test_config_auth.py::test_an_api_error_carries_the_servers_explanation` +
`test_an_already_exists_error_is_recognised_from_the_body`.

### 5.3 A >10 MB workspace file could be exported but never imported
Export works to the 500 MB workspace-files ceiling; `workspace/import` caps its **base64 body** at
10 MB. A 120 MB file exported fine then failed with
`File size imported is (125829120 bytes), exceeded max size (10485760 bytes)`.
**Fix:** files over the cap use the streaming `workspace-files/import-file` route (verified live with
the 120 MB file). Notebooks have no such escape hatch, which is why >10 MB notebooks are recorded
oversize at export instead.

### 5.4 Group membership was not actually two-pass
Members were patched during each group's own create, so a group whose members appeared **later** in
the bundle was silently under-populated — exactly the failure two-pass exists to prevent.
**Fix:** a real second pass after every identity exists.
**Test:** `test_identity_importer.py::test_nested_group_membership_resolves_in_either_order`.

### 5.5 Secret-scope ACLs were sent to the wrong API
A scope's ACL is `secrets/acls/put` — a different endpoint, **one call per principal**, and additive
rather than declarative. Sending it to `PUT permissions/...` 404s. Separately, nothing populated the
`initial_manage_principal`, which **cannot be patched after create**.
**Fix:** dedicated path in the ACL importer; the secrets importer reads MANAGE grants from
`export/acls.json` (needed at create time — the ACL phase runs last by design). Scope ACLs are also
excluded from the parity diff, since an additive ACL would produce false `extra_on_target`.
**Test:** `test_secret_scope_acls_use_their_own_api_and_are_excluded_from_the_diff`.

### 5.6 The ACL parity report went empty on every re-run
It verified only what *this run* applied. Once unchanged ACLs correctly started SKIPping, the report
came back "0 objects checked" — the verification evidence vanished exactly when a re-run should
confirm it.
**Fix:** verify every object with a recorded ACL state row, not just this run's applies. (Also added:
unchanged ACLs now SKIP rather than being re-PUT every run — one API call per object per run was the
slowest thing in the tool for no benefit.)
**Test:** `test_the_parity_report_still_verifies_on_a_run_where_every_acl_SKIPPED`.

### 5.7 Platform-internal directories were being created
`mkdirs` on `.db_internal` returns a bare 400 — Databricks owns those paths.
**Fix:** skipped as a path segment, for both directories and content beneath them.

### 5.8 An overridden preflight check logged as `<lambda>`
`check.__name__` gave `<lambda>` for a wrapped check, so the operator could not tell which check
broke. **Fix:** checks are named explicitly.

---

## 6. Expected (correct) failures in the live run

The live target is a **shared sandbox that is not a migration target**, so a number of per-unit
failures are the tool behaving correctly. Each is categorised and carries a remediation — which is
itself the thing being tested (§7d/D14: *never hard-fail the run on a per-unit problem*).

Final live run: **20 failed, 75 skipped_no_object, 7 manual** out of 268 units, broken down as
`prerequisite_missing: 16, permission_denied: 2, api_error: 2`.

| Category | Count | Why this is correct |
|---|---:|---|
| `prerequisite_missing` — account identities | 5 | 4 Entra users + 1 UMI SP are not assigned to the target. The tool **must not** create them: creating an account SP mints a new `applicationId` and orphans every ACL. Reported for customer IT / an account admin. |
| `prerequisite_missing` — user home dirs | 8 | `/Users/<email>` cannot be `mkdir`'d; it appears when the user is provisioned. A direct consequence of the row above. |
| `prerequisite_missing` — cluster libraries | 2 | Clusters are stopped right after create (so the migration doesn't burn DBUs), and `libraries/install` needs a RUNNING cluster (D6). Opt in with `library_force_start_clusters=true`. |
| `prerequisite_missing` — AKV scope | 1 | Needs an Azure AD token for app `2ff814a6-…`; the run-as identity here is a user, not an Entra SP (§6c). |
| `permission_denied` — genie / DLT | 2 | The run-as identity lacks create permission for those on this shared sandbox. Correctly categorised (it was an indistinguishable `api_error` before fix 5.2). |
| `api_error` — legacy alert + serving endpoint | 2 | Both diagnosed after fix 5.2 exposed the real messages, and both now handled properly (§5.9/§5.10) — they are `not_supported` with a remediation rather than a bare 400. |
| `manual` | 7 | Repos (out of scope, D9), secret values (never readable by any API), legacy SQL dashboards (create endpoint gone, D10). |
| `skipped_no_object` | 75 | ACL grants whose object is legitimately absent — mostly `dab_redeploy` (bundle-owned content the customer's `bundle deploy` recreates). Deliberately **not** `failed`, or every bundle-using workspace would show permanent red (§6b-i). |

**None of these aborted the run** — that is the D21 fail-soft invariant working as designed, and the
run status was `completed` throughout. On the SECOND live import (after the fixes) the same bundle
produced **zero failures**.

### 5.9 An external-model serving endpoint cannot be created without its provider key
Exposed by fix 5.2. The export payload carries `external_model{provider, name, task}` but **not** the
credential — the API never returns it, exactly like a secret value — so the create failed with
"Empty or wrong type of config provided for openai". That is a manual credential step, not a
malformed payload. Now `not_supported` with the remediation (supply the key, ideally as a
secret-scope reference).

### 5.10 Legacy SQL alerts: the v1 create wants a shape the read API no longer returns
Also exposed by fix 5.2. `POST sql/alerts` requires the old flat `options{column, op, value}` and
rejects the modern payload with "Missing alert definition", but the current read surface only returns
`condition{op, operand, threshold}`. The condition is now translated where it maps cleanly;
anything that doesn't is reported as a manual rebuild rather than guessed at — an alert's **trigger**
must not be approximated.

---

## 7. How to run it

```bash
# offline suite (safe anywhere)
python3 -m pytest

# live harnesses (need the fvm1 + target_ws CLI profiles)
python3 -m tests.live_direct_mode        # OAuth M2M
python3 -m tests.live_state_store        # real Delta state table
python3 -m tests.live_e2e_migration      # the full migration (add --keep to inspect the bundle)
```

`live_e2e_migration.py` needs a source SP secret in `/tmp/wsmig_fvm1_sp_secret.txt`
(`<app_id>\n<secret>`), mintable via
`POST /api/2.0/accounts/servicePrincipals/<sp_id>/credentials/secrets`. It creates only
`wsmig_e2e_`-prefixed objects and drops its own state schema, so it leaves both workspaces as found.

**In a workspace**, run the notebooks: `01_Inventory` → `02_Export` → `00_Account_Preflight` →
`04_Import` (or `00_Main_EndToEnd` in `direct` mode). Start with `dry_run=true`.

---

## 8. Known limitations (unchanged, by decision)

- **UC is out of scope**, so genie spaces / Lakeview dashboards / DLT pipelines that reference UC
  tables import successfully but are unusable until those tables exist. Preflight WARNs; it is the
  single most common cause of a "clean" import producing a broken dashboard.
- **Secret values** are never readable via any API — manual on every run, by design.
- **Git repos** and **legacy SQL dashboards** are not imported (D9/D10).
- **A role granted both directly and via a group** is indistinguishable through the API; only the
  group grant migrates. Surfaced in the parity report rather than written off.
- `03_Transform_Review` / `05_Validate` are Plan 4.
