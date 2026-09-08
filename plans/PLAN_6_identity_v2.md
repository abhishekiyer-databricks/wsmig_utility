# PLAN 6 — Identity handling v2 (users, groups, SPNs)

Status: PLAN (review gate). Supersedes the identity parts of PLAN_1 and PLAN_3 §6.
All API behaviour verified live against `source_ws` + `source_acct` on 2026-08-06
(probe objects created and deleted; every count returned to zero).

---

## 1. The one table — how each type is handled

| # | Type | How we detect it | What the script does on target | Key kept? | Manual step |
|---|---|---|---|---|---|
| 1 | **User** — Entra/external | workspace SCIM, `externalId` set | `POST` workspace SCIM `/Users` with `userName` → adopts if already at account, else creates + assigns | `userName` | none |
| 2 | **User** — Databricks | workspace SCIM, no `externalId` | identical to #1 — same call, same path | `userName` | none |
| 3 | **SPN** — Entra / UMI | workspace SCIM, `externalId` set | `POST` workspace SCIM `/ServicePrincipals` **with `applicationId`** → adopts the account SPN | `applicationId` | none |
| 4 | **SPN** — Databricks | workspace SCIM, no `externalId` | identical to #3 — same call, same path | `applicationId` | none |
| 5 | **Group** — Workspace | `meta.resourceType == "WorkspaceGroup"` | `POST` workspace SCIM `/Groups` (empty) → then members + entitlements | new id (remapped) | none |
| 6 | **Group** — Account (Databricks) | `meta.resourceType == "Group"`, no `externalId` | resolve id at account → `PUT /permissionassignments/principals/{id}` → entitlements. **Never POST.** | `displayName` | only if absent from target account |
| 7 | **Group** — Account (Entra/external) | `meta.resourceType == "Group"`, `externalId` set | identical to #6 — same call, same path | `displayName` | only if absent from target account (Entra SCIM) |
| 8 | **Group** — System (`admins`, `users`) | name is `admins`/`users` | never create; apply **membership** only | built-in | none |
| 9 | **Workspace ADMIN vs USER** (any of the above) | `GET /permissionassignments` | `PUT /permissionassignments/principals/{id}` with `["ADMIN"]`/`["USER"]` | — | none |

**The whole plan in three lines:**
- **Users and SPNs: one path each, always.** External vs Databricks makes **no difference** — rows 1≡2 and 3≡4. No classification, no manual prerequisite.
- **Groups: only two real kinds** — workspace (recreate) vs account (assign). Entra vs Databricks makes **no difference** — rows 6≡7.
- **Rows 6/7 are the only place a human can be needed**, and only when the group doesn't exist in the target account at all.

---

## 2. Why (the verified facts behind the table)

| ID | Fact | Consequence |
|---|---|---|
| **F1** | `meta.resourceType` on the workspace SCIM group LIST is `"WorkspaceGroup"` or `"Group"` | the deterministic workspace-vs-account signal — rows 5 vs 6/7 |
| **F2** | An Entra group *is* an account group; it resolves by `displayName` **or** `externalId` to the same record | rows 6 ≡ 7; `externalId` selects no code path |
| **F3** | `POST` workspace SCIM `/Users` or `/ServicePrincipals` creates at the **account** and assigns, with the **same id** | **there is no workspace-local user or SPN** → rows 1–4 need no classification |
| **F4** | SPN `POST` **with** `applicationId` adopts the existing account SPN; **omitting** it mints a new appId | the live bug (§3); row 3/4 keeps its appId |
| **F5** | User `POST` dedupes by `userName` | row 1/2 is safely re-runnable |
| **F6** | `POST` workspace SCIM `/Groups` for a name that exists at account **shadows** it, and the shadow **permanently blocks** assigning the real group | rows 6/7 must **never** POST |
| **F7** | `GET`/`PUT`/`DELETE /api/2.0/preview/permissionassignments` all work **from inside the workspace**, no account creds | rows 6/7/9 are automatable in `airgap` mode |
| **F8** | `permissionassignments` is the only place workspace ADMIN-vs-USER appears (not in SCIM `entitlements`) | row 9 exists at all |
| **F9** | Workspace SCIM `DELETE` on a user/SPN is an **unassign**; the account keeps the record | never used as a delete; safe for multi-workspace (§5) |
| **F10** | Account groups carry workspace-scoped `entitlements`, readable from workspace SCIM | rows 6/7 still need an entitlements PATCH after assigning |

Live evidence for the two that drive everything:

```
F4:  POST /ServicePrincipals {"applicationId":"43b3a3fa-…"} → same appId   ADOPTED ✅
     POST /ServicePrincipals {"displayName":…}              → appId 133e831a-…  DUPLICATE ❌
F6:  account group zz_probe_grp2      → id 155518418599891
     POST ws /Groups zz_probe_grp2    → id 2125035821970675, rt=WorkspaceGroup   (shadow)
     PUT /permissionassignments/principals/155518418599891
         → Error: Workspace group with name zz_probe_grp2 already exists.        (blocked)
```

---

## 3. What is broken today

| current behaviour | rows affected | impact |
|---|---|---|
| `_SP_CREATE_FIELDS = ("displayName", "active")` — **omits `applicationId`** | 3, 4 | **mints a new appId**, orphaning every ACL, job `run_as` and secret grant. Cause of the 13 duplicate `wsmig_test_db_sp` appIds already at account level. |
| classifier uses `externalId`, so an account group with no `externalId` → `DB_MANAGED_GROUP` | 6 | **POSTs a shadow group** → the real account group becomes permanently unassignable (F6) |
| user with no `externalId` → `NEEDS_REVIEW` | 2 | false alarms on every Databricks-native user |
| account identities raise `PrerequisiteMissing` | 1–4 | manual account-admin work that F3/F4/F5 make unnecessary |
| workspace ADMIN-vs-USER never exported | 9 | an admin-by-assignment silently migrates as a plain USER |

---

## 4. Code changes

### 4.1 Inventory — `src/collectors/identity_collector.py`, `src/identity/classifier.py`

1. **Capture `meta.resourceType`** in `_map_group` as `resource_type` (already in the LIST response, free).
2. **New: read workspace permission assignments.** `GET /api/2.0/preview/permissionassignments`; join on SCIM `id` (== `principal_id`) and stamp each identity with `workspace_permissions` (`["ADMIN"]`/`["USER"]`). On 403 degrade to `None`, **not `[]`**, and warn once — `[]` would read as "no permissions" and silently downgrade every admin (same discipline as `_sp_has_secrets`).
3. **Shrink the classifier.** Delete `classify_user` and `classify_service_principal` entirely (F3 — nothing to classify). `classify_group` becomes:

```python
if displayName in {"admins", "users"}:      return SYSTEM            # row 8
if resource_type == "WorkspaceGroup":       return WORKSPACE_LOCAL   # row 5
if resource_type == "Group":                return ACCOUNT           # rows 6/7
return NEEDS_REVIEW    # resource_type missing (old/edge workspace) — never guess
```
Keep `entra_backed = bool(externalId)` as a **reported attribute** (used only for error wording and as a fallback lookup key).
4. **Report**: identity matrix (type × kind) + a "needs account action" list = the only human worklist.

### 4.2 Export — `02_Export`

1. `identity_classification.json` gains `kind`, `resource_type`, `entra_backed`, `workspace_permissions`. Bump the bundle `schema_version` (an old bundle has no `resource_type` → import must fall back, not mis-assign).
2. **New artifact `workspace_assignments.json`** — `[{kind, natural_key, permissions}]`. This is the only carrier of ADMIN-vs-USER (F8).
3. Keep exporting account-group members for inventory value, but flag `members_are_account_owned: true` (§5).
4. **Add `workspace_permissions` to the identity fingerprint** — otherwise a USER→ADMIN promotion has an unchanged fingerprint and silently SKIPs on re-run.

### 4.3 Import — `src/importers/identity_importer.py`

Phase 1 order:

```
1. users   → POST ws SCIM (adopts by userName)                    rows 1,2
2. SPNs    → POST ws SCIM *with applicationId* (adopts, appId kept) rows 3,4
3. groups  → WORKSPACE_LOCAL: POST empty.   ACCOUNT: never POST     rows 5,6,7
4. ASSIGN  → PUT /permissionassignments for account groups + ADMIN/USER deltas  rows 6,7,9
5. members → workspace-local + system groups ONLY                   rows 5,8
6. entitlements → all kinds                                         F10
```

Changes:
- **`_SP_CREATE_FIELDS += ("applicationId",)`** and set `body["applicationId"] = source_app_id`. ⭐ The single highest-value change in this plan: the SPN keeps its appId, so `sp_mapping` becomes **identity** and SPN ACL remap disappears. Treat `"already exists"` as **adopt** (resolve id from the list), not a failure.
- **Delete the `_ACCOUNT_MANAGED` → `PrerequisiteMissing` branches** in `_create_user` / `_create_sp` (F3/F4/F5 — the POST *is* the assign).
- **New pass 4** (groups only): resolve the account group id by `displayName` (or `externalId`), `PUT` the assignment, then entitlements. Not found at account → `PrerequisiteMissing` worded by `entra_backed`.
- **Pre-write conflict guard (F6):** before touching an `ACCOUNT` group, if a same-named `WorkspaceGroup` exists on target, **refuse to POST** and report a blocking conflict naming the remedy ("delete workspace-local group `X` on target, then re-run with `retry_mode=failed_only`"). Silently proceeding makes the account group unassignable forever.
  **⚠️ Implementation note — a bug this caught.** The guard must live in `_process_one`, NOT only in
  `_create_group`. A shadow group IS present in `existing_keys()`, so the base class classifies the
  unit as ADOPT and never calls the create path at all — silently binding the migration to the
  shadow, whose members and ACLs are unrelated to the real account group. Regression test:
  `test_an_account_group_shadowed_by_a_workspace_group_on_target_is_BLOCKING`.
- `_sync_members` skips `members_are_account_owned` groups with a plain note (not a warning).
- Keep the existing two-pass membership design for workspace-local groups (nesting still needs it).
- State store: `record_identity` gains `kind`; add an `assignment` asset type so pass 4 is checkpointed and retryable.

### 4.4 Preflight — `00_Account_Preflight`
Emit `account_principal_ids.json` (resolved account ids for the bundle's account groups). Lets pass 4 run fully automatically in `airgap` mode, since F7 makes the `PUT` itself workspace-side — only *enumeration* needs account read.

**⭐ F14 (found by the live run) — usually no resolution is needed at all.** An account group's
WORKSPACE SCIM id **is** its ACCOUNT id: `wsmig_acc_mixed_grp` is `152592557989155` in source
workspace SCIM, in account SCIM, and as the `principal_id` target's `permissionassignments` PUT
accepts. So when source and target share an account, the exported `source_id` is already the account
principal id. `_resolve_account_group_id` therefore falls back to `source_id` when no account client
is configured.

Without this fallback **every account group failed** with "does not exist in the TARGET account"
*even though it did* — under workspace-admin-only there was no resolution path at all. The dry run
could not catch it (it performs no writes); only the wet live run did. The PUT is now wrapped so a
wrong id degrades into the same actionable prerequisite rather than a raw API error.
Regression tests: `test_an_account_group_resolves_via_its_source_id_with_no_account_credentials`,
`test_a_failed_assignment_PUT_becomes_an_actionable_prerequisite`.

### 4.4b Members resolve by DISPLAY NAME, not just userName (live-run bug)
`admins` imported **0/5** members and `users` **1/13** — i.e. a source workspace admin was silently
not an admin on target, the exact failure the `group_membership` unit exists to prevent. Cause: a
SCIM member entry identifies the member by `display`, which for a USER is their **display name**
("Aman Bansal"), not their `userName`; `_target_users` is keyed by `userName`, so no human member
ever matched. Fix: build a `displayName → target id` index for users AND SPs and consult it in
`_resolve_member`. Regression test: `test_members_resolve_by_DISPLAY_NAME_not_just_userName`.
Live result after the fix: `admins` 5/5, `users` 13/13, nothing missing.

### 4.4c Report WORDINGS and import actions (inventory + export)
The internal `kind` change is only half the job — the operator-facing text had to change with it, or
the reports describe the old behaviour:

- **New `import_action` value `adopt_or_assign`**, split out from `assign_on_target`. The old single
  label "ASSIGN (must pre-exist)" in blue *account/IT prerequisite* colouring was now wrong for users
  and SPNs (they are fully automatic) and right only for account groups. Now:
  `adopt_or_assign` → "AUTO — adopt/assign (no action needed)", **green** (the utility does it);
  `assign_on_target` → "ASSIGN (must pre-exist in account)", **blue**, and only ever on account
  GROUPS. Net effect: exactly one row in the export workbook still reads as a human prerequisite.
- **"Managed By" labels.** HTML and Excel each kept their OWN copy of the label map, and both knew
  only the pre-Plan-6 vocabulary — so a `kind` of `account` rendered as the raw string `"account"`.
  Now a single `MANAGED_BY_LABEL`/`managed_by_label()` in `inventory_view` is shared by both, with
  the legacy values kept so older reports still read.
- **Inventory HTML "Identity classification" section** showed raw kinds with no explanation. It now
  shows the friendly label, a "What the utility does" column per kind, and states plainly that only
  an account group missing from the target account needs a human.
- **⚠️ `export_runner._artifact_unit` dropped the new fields.** Its `keep` allowlist governs the
  per-asset PAYLOAD files, and it omitted `kind`/`entra_backed`/`members_are_account_owned` — so
  anything reading the payload file (which the import runner does) saw no `kind`. It only worked
  earlier because the value also came from the index. Both allowlists are now pinned together by
  `test_payload_files_carry_the_fields_import_branches_on`.

Tests: `test_import_action_labels_cover_the_closed_vocabulary` (every action has a label AND a
colour, `adopt_or_assign` is green like `create`, `assign_on_target` still says "pre-exist") and
`test_managed_by_labels_cover_every_kind_in_BOTH_html_and_excel` (no kind renders as its raw value,
and the two renderers agree character-for-character).

### 4.5 The adopt path must also apply permissions (live-run bug)
`_ensure_assignment` was called only from the CREATE paths, so an identity already present on target
was adopted with whatever ADMIN/USER it happened to have — a source ADMIN silently stayed a plain
USER. Adoption is the common case (re-runs, and any workspace in a shared account), so
`_process_one` applies the permission on every non-create path too. Regression tests:
`test_an_ADOPTED_identity_still_gets_its_workspace_permission` (plus an idempotency test asserting an
already-correct permission is not re-PUT).

---

## 5. Multi-workspace safety (the utility runs once per workspace pair)

Account users, SPNs and groups are shared across many workspaces, and this tool will run 100+ times against the same target account. Every rule below exists to stop run N from damaging workspaces 1..N-1.

| Rule | Why |
|---|---|
| **Never delete or modify an account principal.** Only ever *assign* it to this workspace. | A `DELETE` at account level removes it from **every** workspace. Workspace SCIM DELETE is only an unassign (F9) — and we don't use it either. |
| **Never PATCH members of an account group** (rows 6/7). | Membership is account-global: changing it in run N alters that group in every other workspace. For Entra groups it also fights SCIM, which reverts it. |
| **Do PATCH entitlements** (rows 5/6/7). | Entitlements are **workspace-scoped**, so they're safe and are the correct per-workspace setting. |
| **Always pass `applicationId` on SPN create** (row 3/4). | Workspace #2 must *adopt* the SPN that workspace #1's run already put in the account. Omitting it duplicates per workspace — exactly the 13-appId outcome. |
| **Row 5 groups are per-workspace by nature.** | Two source workspaces may each own a `team_a` workspace-local group; they land in different target workspaces and never collide. |
| **Assignments are per-workspace.** | `permissionassignments` is scoped to the workspace the call is made in, so row 9 can't leak ADMIN into another workspace. |
| **Never grant account-level roles.** | Only workspace-scoped entitlements migrate; `account_admin` in SCIM `roles` must be filtered out, or run N could escalate an identity account-wide. |
| **State store already keyed by `(source_ws_id, asset_type, natural_key)`.** | Per-pair state, so 100 pairs share one catalog without collisions; the identity map is per source workspace. |
| **Every write is adopt-or-assign, so runs are order-independent.** | Workspace #7 migrating first works the same as #1; no run depends on another having gone first. |

**Net effect:** run N is *additive only* at account level. The only account-level side effect the tool can ever have is creating a principal that didn't exist (rows 1–4), which is the same operation an admin would do by hand.

---

## 6. Tests

**Offline (extend the 243):**
1. account group, no `externalId` → `ACCOUNT`, **not** recreated ← the live bug
2. account group + `externalId` → `ACCOUNT`, **same code path** (rows 6≡7)
3. `WorkspaceGroup` → `WORKSPACE_LOCAL` → created
4. `admins`/`users` → `SYSTEM` whatever `resource_type` says
5. `resource_type` missing → `NEEDS_REVIEW`, never a guess
6. ⭐ **SPN create body always contains `applicationId`**; `sp_mapping` is identity
7. SPN POST `"already exists"` → adopt, not fail
8. no `PrerequisiteMissing` is ever raised for a user or SPN (rows 1–4)
9. account-group members are **never** PATCHed (multi-workspace guard)
10. `account_admin` role is filtered out of entitlement PATCHes
11. USER→ADMIN promotion changes the fingerprint (no silent skip)
12. assignment pass is idempotent — already-assigned principal issues no `PUT`
13. `permissionassignments` 403 → `None`, not `[]`
14. ⭐ `ACCOUNT` group + same-named `WorkspaceGroup` on target → blocking conflict, **no POST** (F6)

**Live (`live_e2e_migration.py`):** fixtures already cover all 9 rows — `wsmig_test_*` (row 5), `wsmig_acc_*` (row 6), `ai27_entra_grp` (row 7), `admins`/`users` (row 8), `ai27_acc_spn` (row 4 — the principal today's code duplicates). Assert:
- every SPN on target has the **same `applicationId`** as source — no new appIds anywhere;
- account groups resolve to the **same account id**; workspace-local groups get new ids;
- target `permissionassignments` matches the export, **including ADMIN vs USER**;
- re-run is a clean no-op (no duplicate SPNs, no shadow groups);
- **multi-workspace:** import the same bundle into a second target workspace and assert the account SPN/group count is **unchanged** (adopted, not duplicated).

**Still owed:** all probes ran on `source_ws`/`source_acct`. The `[DEFAULT]` (target) profile's refresh token is expired (`invalid_request: Refresh token is invalid`), so the assign-into-a-fresh-workspace direction is verified by construction only. Re-auth (`databricks auth login -p DEFAULT`) and re-run the F4/F6/F7 probes on target before sign-off.

---

## 6a. Live verification (source_ws → target_ws, 2026-08-07)

Final live state, all fixes applied:

| check | result |
|---|---|
| identity phase | **29 units, 0 failed, 0 warnings** |
| SPN applicationIds preserved | **7/7**, zero duplicates minted |
| group parity (all 4 kinds) | **0 problems** — account groups keep their account id, workspace-local get new ids |
| `admins` / `users` membership | **5/5** and **13/13**, nothing missing |
| workspace ADMIN vs USER parity | **25/25 match, 0 differ, 0 missing** |
| ACL phase | 292 created, **0 failed** (no SP remap needed — appIds preserved) |
| offline suite | **261 passed** (baseline 243) |

Five bugs were found during implementation; **three of them only the wet live run could catch**
(the dry run performs no writes, and all 243 pre-existing unit tests passed throughout):
1. shadow guard in the wrong place (§5.2) — silent ADOPT of a shadow group
2. `kind` not carried onto units (§4.3) — every group would have been skipped
3. legacy bundles unimportable (§4.3) — an old `db_managed_sp` would have duplicated
4. account groups unresolvable under workspace-admin (§4.4/F14)
5. adopted identities never got their permission (§4.5); members never resolved (§4.4b)

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| a shadow `WorkspaceGroup` permanently blocks the real account group (F6) | classify from `resource_type` before any write + pre-write conflict check that refuses and names the remedy (§4.3) |
| setting `applicationId` on SPN create is undocumented behaviour we now rely on | offline test 6 + live appId-preservation assertion; a rejecting workspace fails loudly rather than silently duplicating |
| `airgap` can't enumerate the account to resolve a group id | `00_Account_Preflight` emits `account_principal_ids.json`; F7 keeps the `PUT` workspace-side; rows 1–4 need no resolution at all |
| `resource_type` absent on some workspace version | explicit `NEEDS_REVIEW` fallback (§4.1) |
| assigning a group grants workspace access to all its members | intended — reproduces source, and matches the exported assignment list |
| old bundles re-imported after upgrade | `schema_version` bump + fallback |
| target-direction probes not yet live | §6 "still owed" |
