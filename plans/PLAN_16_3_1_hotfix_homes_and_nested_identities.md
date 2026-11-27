# PLAN 16.3.1 — Hotfix release 1.2.1: (A) create user/SP homes with `mkdirs`, (B) keep nested identities nested

**Status:** **SIGNED OFF 2026-11-23 — ready to release as 1.2.1.** 694 offline tests green, 11/11 mutants killed,
live QA §8.1 / §8.2 / §8.3 PASS (§9). All decisions are made (§2) and every premise is verified live (§0). Dev log + judgment calls: §9.
**Base:** `main` @ `4724c59` (1.2.0). **Branch:** `hotfix/1_2_1`, cut from `main`. The user creates the branch.
**Roles:** the dev agent codes and runs the offline tests (§5), then does live QA (§8). The user reviews, commits,
pushes and merges. **The dev agent never commits, pushes or creates branches.**

**Scope:**
- **Part A:** the home-resolution gate (users and account SPs).
- **Part B:** the identity importer's workspace-assignment and entitlement decisions (groups, SPs, users), plus
  **verified entitlement reporting**: every identity row says which entitlements were copied, were already
  present, or failed.
- No fingerprint, natural-key, state-schema, bundle or job-template change.
- Import side only, so **existing 1.2.0 bundles work as-is** (no re-inventory, no re-export, no airgap source upgrade).

**Tickets:**
- Part A: ES-2364565 (Reliance).
- Part B: customer finding 2026-11-18. Source has 75 directly assigned groups, target 81; reconciled exactly as
  75 − 18 missing + 24 nested-on-source groups the tool assigned directly.

**Ground rules the dev must keep** (from the project memory and CLAUDE.md):
- Never break `main`: every default path that is not named here stays byte-identical to 1.2.0.
- Every change must work over **main-written (1.2.0) state and bundles**.
- The report is the source of truth: every write is read back, and every failure carries the server's own message.
- One API call per *owner* or per *list*, never one per content unit (the lesson of the 7-hour regression).

---

## 1. Background and root causes

### 1A. Part A: homes are no longer created on workspace load
- **Before 11/12/2026:** the webapp created `/Users/<email>` when a workspace loaded, shortly after the user was
  assigned. The tool relied on that: an absent home was filed as `prerequisite_missing`, and
  `retry_mode=failed_only` healed it later.
- **Since 11/12/2026 (ES-2364565):** a home appears only when the user **signs in**, or when a **workspace admin
  calls `mkdirs` on `/Users/<user>`**. For owners who never sign in on the target, `failed_only` never heals. All
  their notebooks, files, folders, queries, dashboards and Genie spaces stay `prerequisite_missing` forever.
- **The gate:**
  - `BaseImporter._resolve_home_target` (`src/importers/base_importer.py:676`) returns `prerequisite` on a 404,
    and the `mkdirs` that would create the home is never sent.
  - `_create_directory` (`workspace_importer.py:184`) skips the home ROOT on purpose. That was correct while root
    `mkdirs` returned `DIRECTORY_PROTECTED`.
- **Latent SP bug (verified, P4 + P9):**
  - `state_store.load_identity_map` (`:696`) puts **every** migrated SP into `sp_mapping`. Account SPs keep their
    applicationId, so their entry is the identity mapping `appId → appId`.
  - `_home_present` (`base_importer.py:632`) and `_resolve_home_target` (`:676`) treat **any** `sp_mapping` entry as
    "recreated SP, home auto-provisioned at SP create". The tool therefore never checks or creates an account SP's
    home, and it reports the root as provisioned when it isn't.
  - Live, an SP home is **absent** after account assignment AND after the tool's workspace-SCIM POST (P9). It
    appears only after `mkdirs` (P4).

### 1B. Part B: nested-only identities become directly assigned
**Background:**
- An account group, SP or user is in a workspace either **directly assigned** (listed in
  `GET /api/2.0/preview/permissionassignments`) or **only via a group** (a member of an assigned group).
- SCIM lists both kinds. The UI shows a via-group one only on search, with the 👥 icon.
- Account-group membership, including group-in-group, is **account-global**, so nesting is already identical on
  the target. Only the workspace *assignments* differ.

**Root cause** (verified: code plus the live premises PB0, PB1 and PB3):
1. The collector (`identity_collector.py:163`) reads the whole roster, nested-only identities included. It records
   each identity's direct assignment in `workspace_permissions` (`_permissions_for`, `:93`, fed by `:74`):
   - `["USER"|"ADMIN"]` = direct;
   - **`[]` = nested only**;
   - `None` = could not check, or a pre-Plan-6 bundle without the field.
2. `IdentityImporter._permissions_for_unit` (`identity_importer.py:623-636`) does `if not permissions: return
   ["USER"]`. This turns the recorded `[]` into `USER`.
3. That `USER` is PUT by:
   - `_assign_account_group` (`:565`), on create;
   - `_ensure_assignment` (`:638`), called from `update_one` for account groups (`:312`) on **every re-run**. So a
     manual cleanup is undone by the next 1.2.0 import.
4. **Users and SPs** are POSTed to workspace SCIM (`_create_user` `:348`, `_create_sp` `:389`). **That POST alone
   makes the principal directly assigned, even when it was already listed via a group (PB1).** `_ensure_assignment`
   then PUTs `USER` as well.

**Related gap on the adopt path:**
- An identity that already exists on the target when the phase starts (`existing_keys`, `:191`) goes down the base
  importer's ADOPT branch (`base_importer.py:977-996`). The branch only records `adopted`. It applies **no
  entitlements and no assignment check**, because the identity importer does not opt into
  `reconcile_on_adopt_types`.
- Account groups the customer assigned before the migration hit exactly this branch. This is the most likely cause
  of the customer's original "group A's Workspace access was not copied" symptom.
- Part B needs the adopt path anyway: nested-only identities are often already visible when the phase starts
  (parents pre-assigned, or a re-run), and they must get their entitlements there.

## 0. Premises: all verified live 2026-11-23
**Environment:**
- Account `ccb842e7-2376-4152-b0b0-29fa952379b8`.
- Fresh target `target_ws` = `ai27-westus-target-ws`, https://adb-7405612139613163.3.azuredatabricks.net (westus2).
- Calls made **as the admin SP `ai27_pt_admin_sp`** (49399e58…, workspace ADMIN, OAuth M2M), the same shape as the
  run-as identity.
- Scripts are in `/tmp/pt/` (not committed); their results are reproduced here.

### Part A
| # | Premise | Result |
|---|---|---|
| P1 | `mkdirs /Users/<user>` for an **assigned user who never signed in** | ✅ 200 → `DIRECTORY`; the user holds **direct `CAN_MANAGE`** (`admins` inherited). Real home. |
| P2 | `mkdirs` on an **existing** home | ✅ 200 no-op: same object id `950159028105365`, ACL unchanged |
| P3 | `mkdirs` for (a) an account user **not assigned** to the target, (b) an email that **doesn't exist** | ✅ both **400 `DIRECTORY_PROTECTED`** ("Folder Users is protected"). **Nothing is created** (`get-status` stays 404). The platform itself refuses, so no folder can be created for an unconfirmed owner. |
| P4 | `mkdirs /Users/<appId>` for an **assigned account SP that never ran anything** (its home was absent) | ✅ 200 → **real SP home**: the SP holds **direct `CAN_MANAGE`** |
| P5 | The owner's **first sign-in after the tool created the home** reuses it | ✅ `abhishek.iyer@databricks.com`: home created by the admin SP (id `950159028105391`) with a marker notebook (id `950159028105395`), then the user signed in. Afterwards: same home id, marker intact, owner direct `CAN_MANAGE`, no second or renamed home, and the webapp created its own `.db_internal` / `.conversations` **inside** that home. |
| P6 | A **non-admin** caller runs `mkdirs` on another user's home | ✅ **403 `PERMISSION_DENIED`** "`<appId>` does not have View permissions on `<object id of /Users>`". Nothing is created. |
| P7 | `mkdirs /Users/<u>/a/b` while the home is absent | ✅ 200, and it also creates the **real** home root (owner direct `CAN_MANAGE`). The design still creates the root first explicitly. |
| P8 | `mkdirs` for a user / SP that is **only via a group** on the target | ✅ real home for both (owner direct `CAN_MANAGE`) |
| P9 | Whether the tool's **workspace-SCIM POST** creates a home (users and SPs; not visible, or visible via a group) | ✅ **No**: all four homes are absent after the POST. Homes come only from sign-in or `mkdirs`. |
| P10 | A **recreated** SP (workspace-SCIM POST **without** `applicationId`, which mints a NEW applicationId: the IMP-6 case) | ✅ home `/Users/<newAppId>` is **absent** right after the create (the old "auto-provisioned at SP create" claim is false today); `mkdirs` creates a **real** home, the new SP holds direct `CAN_MANAGE` (`ai27_pt_sp_recreated`, fffc56f6…) |

### Part B
| # | Premise | Result |
|---|---|---|
| PB0 | Assigning a parent does **not** directly assign a nested child (group, SP, user) | ✅ child listed in target SCIM, **absent** from `permissionassignments`, v2 GET 404 (2026-11-18 probe + 2026-11-23 `ai27_pt_child`, `ai27_pt_sp_nested`, `ai27.pt.u.nested`) |
| PB0b | Removing a nested group's direct assignment keeps it listed via its parent, keeps its SCIM **entitlements** and its object **ACLs**; entitlements can be PATCHed on it while nested only | ✅ (2026-11-18, probe states B→C→D) |
| PB1 | A workspace-SCIM **POST** of an existing account SP (`applicationId`) or user (`userName`) | ✅ **creates a DIRECT assignment**, returns **201** (not 409), **even when the principal was already listed via a group**. So nested-only SPs and users must **never** be POSTed. |
| PB2 | Entitlements PATCH on a nested-only SP / user | ✅ 200, persists, and the principal stays nested only |
| PB3 | What the **real 1.2.0 collector** records in `workspace_permissions` | ✅ account group direct `['USER']`, **nested `[]`**; SP direct `['USER']`, **nested `[]`**; user direct `['USER']`, **nested `[]`**; **workspace-local group `['USER']`** (they ARE in `permissionassignments`); `users` / `admins` **`[]`**; pre-Plan-6 bundle → field absent → `None` |
| PB4 | Object ACL grants to nested-only principals | ✅ notebook, cluster policy, job, secret scope: every grant to a nested group, SP and user is 200 and reads back |
| PB5 | A job whose `run_as` is a **nested-only SP** | ✅ created and run → `TERMINATED SUCCESS` (the SP's `workspace-access` was inherited from its parent). **Unchanged platform rule:** the job creator must hold `roles/servicePrincipal.user` on the run-as SP. This applies to direct SPs too (control verified), so it is not a Part B concern. |
| PB6 | Visibility lag (parent assigned or nested → child listed in target SCIM) | ✅ **3-6 s** for groups, SPs and users, the same as a direct assignment. The wait budget below has a wide margin. |

## 2. Decisions (all made by the user)
### Part A
- **D1, switch:** **always on, no widget.** Where the platform refuses (`DIRECTORY_PROTECTED` for an owner who is not on the
  target, `PERMISSION_DENIED` for a non-admin run-as), the outcome is exactly today's `prerequisite_missing`.
- **D2, owners not usable on the target:**
  - **Owner deleted in the source** (absent from the source roster): divert to `/Users_Backup` (PLAN 9,
    unchanged). This is decided **before** any `mkdirs`. With `workspace_home_backup=false` it stays today's
    prerequisite.
  - **Owner still on the source but not assigned on the target:** `prerequisite_missing`. Per P3 the platform
    refuses with `DIRECTORY_PROTECTED` and creates nothing, so **no pre-check is needed**. The tool attempts
    `mkdirs` and, on `DIRECTORY_PROTECTED`, words the row per §3A step 5.
- **D3, SP homes:** **included, for both kinds of SP.**
  - **Account SP** (keeps its applicationId; `sp_mapping[owner] == owner`, the normal case in one account): handled
    exactly like a user. Check `/Users/<appId>`, and `mkdirs` it if absent (P4).
  - **Recreated SP** (`sp_mapping[owner]` is a different, new applicationId; IMP-6): content is still remapped
    `/Users/<oldAppId>/…` → `/Users/<newAppId>/…`. Its **new** home is checked and created the same way (P10),
    instead of being assumed present.

### Part B
- **DB1, parent never reaches the target:** fail the row as `prerequisite_missing`, naming the parents. **Never**
  fall back to a direct assignment.
- **DB2, scope:** **groups, SPs and users**.
- **DB3, targets already migrated with 1.2.0:** **never remove** extra direct assignments. Report each one once with
  the note in §3B. Removal is a manual or reconciliation-notebook step outside the tool.
- **DB4, switch:** **always on, no widget.** Whatever is nested on the source is nested on the target, never
  directly assigned.
- **DB5, entitlement reporting (user, 2026-11-23):**
  - Every entitlement/role write is **verified by a GET** after the PATCH.
  - Each identity row reports **copied / already present / failed**, with the server's message for each failure.
  - An entitlement or role that failed, or that a fresh GET does not show, makes the row **`failed`** (category
    `not_applied`). `retry_mode=failed_only` then retries it; the identity exists, so the retry goes down the adopt
    path (§3B.4), which re-applies and re-verifies.

## 3. The change
### 3A. Part A: `src/importers/base_importer.py` and `src/importers/workspace_importer.py`
1. **Recreated-SP test (fixes the latent SP bug).**
   - Add `_is_recreated_sp(owner) -> bool`:
     `new = sp_mapping.get(owner); return bool(new) and new != owner`.
   - Replace `sp_map.get(owner)` with `_is_recreated_sp(owner)` in:
     - `_home_present` (`:642-644`);
     - `_resolve_home_target` (`:694-702`).
   - An account SP (identity mapping) now goes through the presence check and provisioning like a user.
   - A recreated SP keeps the IMP-6 path remap (`_remap_home_path`). Its remapped root `/Users/<newAppId>`:
     - `_home_present` checks it with `get-status` (no "auto-provisioned" shortcut, P10);
     - `_resolve_home_target` returns `skip_root` / `remapped_sp` only when it is present;
     - when absent and `provision` is true, `_provision_home(<newAppId>, /Users/<newAppId>)` creates it, and the
       kinds become `provisioned_root` (root) / `remapped_sp` (descendant);
     - otherwise `prerequisite`.
   - Fix the misleading `skip_root` note "auto-provisioned when the SP was created": it becomes "SP home root — already
     present" (only reached when the home exists).
2. **New method `_provision_home(owner, home_root) -> bool`** (base class).
   - At most **one attempt per `home_root` per pass**, cached in `self._home_provision_cache`. A re-sweep or retry
     pass resets it, the same way `_home_present_cache` is reset today.
   - Returns `False` **without any call** when `self.dry_run` is set.
   - Otherwise:
     1. `POST api/2.0/workspace/mkdirs {"path": home_root}`.
     2. On an exception, store the classified server message in `self._home_provision_error[home_root]` (use
        `classify_error`; keep `error_code` and `message` verbatim), log a WARNING
        `could not create home <home_root>  error=<server message>`, and return `False`.
     3. Read back with an **uncached** `_get_status(home_root)`.
     4. If present: set `_home_present_cache[home_root] = True`, append `owner` to
        `self.context.setdefault("homes_provisioned", [])`, log at INFO
        `created home <home_root> (owner had not signed in)`, and return `True`.
     5. If absent after a 200: store "mkdirs returned 200 but the home is not present" as the error, log a WARNING,
        and return `False`.
3. **`_resolve_home_target(path, provision: bool = False)`.** New order:
   1. Recreated SP: unchanged IMP-6 remap.
   2. Home present: unchanged (`skip_root` / `normal_home`).
   3. Backup on **and** the owner is absent from the source roster: `backup` (unchanged).
   4. **New:** `provision` is true, the owner is not roster-absent, and `_provision_home` succeeds:
      - for the root: kind `provisioned_root`, note `home created by the migration (mkdirs as workspace admin; owner
        had not signed in)`;
      - for a descendant: `normal_home`.
   5. Otherwise: `prerequisite` (unchanged kind).

   Update the `HomeResolution` kind docs (`:52-62`) with `provisioned_root`.
4. **Callers.**
   - `existing_keys` (`workspace_importer.py:123`) keeps `provision=False`. The existence check must have no side
     effects, and it runs in dry-run too.
   - `_create_directory` (`:189`) and `_upload_content` (`:222`) pass `provision=True`.
     - For `provisioned_root`, `_create_directory` returns `{"target_id": home_root, "note": res.note}` with no
       second `mkdirs`, so the row is `created`.
   - `remap_parent_path` (`base_importer.py:494`) calls `_resolve_home_target(raw, provision=True)`. It runs only
     inside `create_one`, so never in dry-run. Treat `provisioned_root` like `normal_home`.
5. **Wording.**
   - `_raise_home_prerequisite` (`workspace_importer.py:145`) and `missing_parent_prerequisite`
     (`base_importer.py:532`) must stop claiming "a user home is created on first login" or "appears only once
     provisioned".
   - If `_home_provision_error[home_root]` contains `DIRECTORY_PROTECTED`, use:
     "home `<home_root>` could not be created: the owner is not assigned to the target workspace (server:
     `<message>`). Assign `<owner>` to the target, then re-run with `retry_mode=failed_only`."
   - Any other error: "home `<home_root>` could not be created (server: `<message>`). Confirm the run-as identity is
     a workspace admin, then re-run with `retry_mode=failed_only`."
   - Not attempted (dry-run): "home `<home_root>` does not exist yet; a live run creates it."
   - Category stays `prerequisite_missing` in every case, so `failed_only` sweeps these rows up.

**Unchanged on purpose:**
- The recreated-SP *path remap* (IMP-6): `/Users/<oldAppId>/…` → `/Users/<newAppId>/…`.
- Orphan backup (PLAN 9).
- `.db_internal` / Trash skips.
- A present home: byte-identical calls, with **no** `mkdirs` on the root.

### 3B. Part B: `src/importers/identity_importer.py` and `src/reports/import_report.py`
1. **`_is_nested_only(unit) -> bool`.** True **only** when ALL of these hold:
   - asset type is `user` or `service_principal`, **or** a `group` whose `_kind_of(unit) == KIND_ACCOUNT`;
   - the natural key is not `users` / `admins`;
   - the recorded `workspace_permissions` (payload first, then unit, the same lookup as `_permissions_for_unit`) is
     **exactly `[]`** (a list, empty).

   It is never true for:
   - `None` (unknown or a pre-Plan-6 bundle): keep today's `USER` fallback;
   - workspace-local groups: they record `['USER']` (PB3), and the kind check is a second guard;
   - `group_membership` units.
2. **Source parents** (for notes, the DB1 message and the report), derived from the bundle with no API call:
   - group: every bundle group whose `members` contains it (`kind == "group"`, match on `display` = displayName);
   - user / SP: `group_memberships` (`identity_collector.py:183`) intersected with the bundle's group names.
   - Build once in `load()` as `self._parents_by_key`.
3. **Create path (`create_one`), nested-only unit:**
   - **no** `permissionassignments` PUT for groups;
   - **no** workspace-SCIM POST for users and SPs (PB1);
   - append `(unit, parents)` to `self._nested_pass`;
   - return a marker so the base class records nothing yet. Implement it as a new
     `DeferredToNestedPass(Exception)` that `_do_create` treats like the existing deferral path: no row now; pass 3
     writes the row via `_record` / `_amend_row`.
4. **Adopt path.**
   - Add `"user"`, `"service_principal"` and `"group"` to `reconcile_on_adopt_types`, and implement
     `adopt_one(unit, target_id)`:
     - **Nested-only on source:**
       - Apply entitlements (`_apply_entitlements`). **No assignment call.**
       - If the target already has a **direct** assignment for it (from `self._target_assignments`), add the
         DB3 note: "directly assigned on target, nested only on source (via `<parents>`); left unchanged — remove
         its workspace assignment manually if the source layout must be mirrored (RUNBOOK §Nested identities)".
       - `changed = True` only if an entitlement was added.
     - **Direct on source** (`["USER"]` / `["ADMIN"]`):
       - Apply entitlements.
       - **Ensure the direct assignment** (`_ensure_assignment`). This fixes the reverse case, where the identity is
         direct on the source but only via a group on the target.
     - **`None`:** apply entitlements; `_ensure_assignment` with today's `USER` fallback (today's create
       behaviour).
     - **System groups** (`admins` / `users`): unchanged. They keep `_add_builtin_members` via the declarative path.
   - Return `{"target_id": target_id, "changed": ..., "note": ..., "source_detail": _snapshot_json(...)}`, the same
     shape as `update_one`.
5. **Update path (`update_one`, every re-run).** For a nested-only unit, **do not call `_ensure_assignment`**
   (`:312` for account groups; users and SPs never called it). Keep the entitlement re-apply and the diff note.
   Add the DB3 note when a direct assignment already exists.
6. **New PASS 3, `_apply_nested_pass()`.** Call it in `run()` right after `_apply_deferred_members()`.
   - If `_nested_pass` is empty, log `nested-identity pass 3: none` and return.
   - **Poll** every `NESTED_POLL_SEC = 10` up to `NESTED_WAIT_SEC = 300` (PB6 measured 3-6 s). Each poll reads
     **one SCIM list per needed kind** (`get_scim("Groups")` / `("ServicePrincipals")` / `("Users")`), never one call
     per identity, and resolves keys (displayName / applicationId / userName). Stop as soon as all are resolved.
   - **Resolved:**
     - store the id in `_target_groups` / `_target_sps` / `_target_users` (and `_target_by_display`);
     - `_apply_entitlements`;
     - record `ACTION_ADOPTED`, `target_id` = the id, note `nested on source via <parents>; reached through its
       group on the target, not directly assigned (as on source)`.
   - **Unresolved after the budget** (DB1): record `ACTION_FAILED`, category `prerequisite_missing`, note
     `nested on source via <parents>; none of them is on the target, so <key> is not visible here. Import or assign
     a parent, then re-run with retry_mode=failed_only.` For each parent, append its outcome in this run
     (`created` / `adopted` / `failed` / `not_selected` / `not in bundle`), read from `self.result.units`.
   - **Dry-run:** no polling. Record `would_adopt (nested on source via <parents>; no direct assignment)`, the
     existing dry-run row convention.
   - Log one INFO progress line per poll (`nested-identity pass 3: <resolved>/<total> visible`) and one summary line.
6b. **Verified entitlements: `_apply_entitlements` returns a result instead of being fire-and-forget**
    (`identity_importer.py:834`). Every caller (create, adopt, update, pass 3) uses the same function.
    - **Source values:**
      - `entitlements` from the payload (string values);
      - `roles` from the payload, minus `_ACCOUNT_LEVEL_ROLES` (still dropped and logged, as today).
      - If both are empty: **no call at all**, the same as 1.2.0. The result is `{copied: [], present: [],
        failed: []}`.
    - **Otherwise:**
      1. `GET {_SCIM}/{resource}/{target_id}?attributes=entitlements,roles` → `before`.
      2. `missing = source − before`, per attribute.
      3. One PATCH per attribute **only for the missing values**, the same body as today (`op: add`). A PATCH
         exception records every value it carried as `failed: <value> (<classified server message>)`, then
         continues with the other attribute.
      4. If any PATCH was sent: `GET` again → `after`, polling up to 3 times, 3 s apart, until every sent value
         is present.
      5. Result:
         - `copied` = sent and present in `after`;
         - `present` = in `before` already;
         - `failed` = PATCH errors plus "sent, but not present after a fresh GET".
    - **Calls per identity:** 1 GET when nothing is missing; otherwise 1 GET + ≤2 PATCH + 1 GET (more GETs only
      while polling). No extra calls for identities with no direct entitlements or roles (most users).
    - **Row fields** (result rows / `import_results.json` / xlsx only; the Delta state table is **not** changed):
      - `entitlements_copied`, `entitlements_present`, `entitlements_failed` (comma-separated; roles prefixed
        `role:`);
      - the note gains `entitlements: copied <…>; already present <…>; failed <…>`.
    - **Outcome:** if `failed` is non-empty, the row is recorded `ACTION_FAILED`, category `CAT_NOT_APPLIED`,
      `error_raw` = the first server message, note `<identity> exists on target, but entitlements could not be
      applied: <failed list>. Re-run with retry_mode=failed_only.` This replaces today's warning-only path
      (`self.result.warnings.append`, `:871-874`), which never reached the report.
    - Dry-run: no calls. Row fields `entitlements_would_copy` = the source values.
    - Thread the result into `_record` via a new optional kwarg `entitlements: dict = None` on
      `BaseImporter._record` (`base_importer.py:1118`). It only adds the three keys to the result row when given,
      so every other importer's rows are byte-identical.
7. **Cross-phase consumers.** After pass 3, re-publish `self.context["target_identities"]` (`:229-231`), so the
   jobs `run_as` remap, the secret-scope MANAGE principal and the ACL principal resolution all see the nested-only
   identities. PB4 and PB5 prove those writes succeed for them.
8. **`_permissions_for_unit`.** Behaviour unchanged. Add an assertion-style guard at the top: if
   `_is_nested_only(unit)`, raise `RuntimeError("nested-only identity reached the assignment path")`. That makes a
   future regression fail loud in tests instead of silently re-assigning.
9. **Report (`import_report.py`).** Identity sheets (users, service principals, groups) get four new columns
   after "Note / reason": **"Entitlements copied"**, **"Already present"**, **"Entitlements failed"** (from the row
   fields in 6b), and **"Assignment"**, filled from the row note or source detail:
   - `direct`;
   - `via group (<parents>)`;
   - `direct on target (pre-existing); nested on source`.

**Unchanged on purpose:**
- Direct identities on the create path: byte-identical calls.
- Workspace-local groups: POST plus members in pass 2.
- Account-group membership is never patched.
- `None` keeps the `USER` fallback.
- **No removal of any assignment, ever.**

## 4. Upgrade (1.2.0 → 1.2.1)
The customer pulls 1.2.1 into the Git folder (no reinstall).

**Part A:**
- 1.2.0 home rows are `failed` with `prerequisite_missing`.
- Run the import job with `retry_mode=failed_only`, `dry_run=false` and the **same `run_id`**. Homes are created
  and the content lands. Permissions follow the healed objects in the same run (already in 1.2.0).
- Home-root rows become `created`, with the provisioned note.

**Part B:**
- Nothing is removed.
- Identity rows that are `adopted` / `created` in state with an unchanged fingerprint are **skipped** (base-class
  rule), so a plain re-run does not touch them. That is intended (no surprise writes).
- The DB3 note is written whenever such a row is next updated or adopted.
- Targets that need a full identity reconcile now, like the customer's, use the standalone identity-reconciliation
  notebook (outside this release).
- After a manual cleanup, 1.2.1 re-runs **keep** the identities nested. 1.2.0 re-added them.

**Both:** no fingerprint or natural-key change, so nothing else turns `updated`.

## 5. Offline tests: `tests/test_plan16_3_1.py` (new), using the existing fakes in `tests/`
### Part A
1. `test_absent_user_home_is_created_with_mkdirs_then_content_imports`
2. `test_home_root_unit_records_created_note_not_prerequisite`
3. `test_home_created_once_per_owner_per_pass`: N content units under one absent home → exactly 1 root `mkdirs` + 1
   read-back.
4. `test_directory_protected_is_prerequisite_saying_owner_not_assigned` (P3 wording)
5. `test_other_mkdirs_error_is_prerequisite_with_server_message` (e.g. P6 `PERMISSION_DENIED`)
6. `test_mkdirs_200_but_readback_absent_is_prerequisite`
7. `test_dry_run_never_calls_mkdirs_on_a_home_root`
8. `test_existence_check_never_creates_a_home`
9. `test_owner_deleted_in_source_goes_to_backup_without_mkdirs` (D2, PLAN 9 order)
10. `test_account_sp_home_with_identity_mapping_is_checked_and_created` (D3 + latent bug)
11. `test_recreated_sp_remaps_to_new_app_id_and_creates_its_absent_home` (IMP-6 remap kept; P10 provisioning)
12. `test_query_dashboard_genie_parent_home_is_created` (all three `remap_parent_path` callers)
13. `test_upgrade_failed_only_heals_1_2_0_home_prerequisite_rows`: seed 1.2.0-shaped rows
14. `test_present_home_call_sequence_unchanged`: matches the 1.2.0 call log

### Part B
15. `test_nested_only_account_group_create_sends_no_permissionassignments_put`
16. `test_nested_only_sp_and_user_create_send_no_scim_post` (PB1)
17. `test_nested_pass_resolves_via_one_list_per_kind_and_applies_entitlements`
18. `test_nested_pass_polls_until_visible_within_budget` (visible on the 2nd poll)
19. `test_nested_pass_parent_missing_is_prerequisite_naming_parents_and_their_outcome` (DB1)
20. `test_failed_only_retries_a_nested_prerequisite_and_heals_it`
21. `test_nested_only_update_path_never_reassigns` (the re-run regression)
22. `test_adopt_nested_only_applies_entitlements_without_assignment`
23. `test_adopt_direct_on_source_via_group_on_target_ensures_direct_assignment`
24. `test_adopt_preexisting_direct_on_1_2_0_target_left_unchanged_with_db3_note`
25. `test_unknown_permissions_none_keeps_user_fallback` (pre-Plan-6 bundle)
26. `test_workspace_local_group_is_never_nested_only` (records `['USER']`; also `[]` with kind workspace_local)
27. `test_system_groups_are_never_nested_only`
28. `test_direct_identities_create_call_sequence_unchanged` (`["USER"]` / `["ADMIN"]` match the 1.2.0 log)
29. `test_nested_identity_published_for_acl_run_as_and_secret_scope_phases`
30. `test_dry_run_nested_rows_are_would_adopt_and_never_poll`
31. `test_permissions_for_unit_raises_if_reached_for_nested_only` (the §3B.8 guard)
32. `test_entitlements_get_patch_get_reports_copied_and_present` (DB5)
33. `test_entitlement_patch_error_fails_row_not_applied_with_server_message`
34. `test_entitlement_patch_200_but_missing_after_get_fails_row`
35. `test_identity_without_entitlements_makes_no_entitlement_calls` (1.2.0 call log unchanged)
36. `test_failed_only_retries_entitlement_failure_via_adopt_and_heals`
37. `test_account_level_role_still_dropped_and_logged`
38. `test_report_identity_sheets_have_entitlement_and_assignment_columns`
39. `test_non_identity_rows_have_no_entitlement_keys` (other importers byte-identical)

**Existing tests to update** (intentional behaviour change; record each one in §9):
- `tests/test_importers_phase2_5.py:315` (`…prerequisite_not_mkdird`): split into "created" plus a
  "DIRECTORY_PROTECTED → prerequisite" variant.
- `tests/test_importers_phase2_5.py:376` (`…content_under_an_absent_user_home…`): same split.
- `tests/test_plan16_3.py:578`: wording.
- **Before coding Part B:** `grep -rn "workspace_permissions" tests/`. Every test that feeds `[]` and expects a
  `USER` PUT or POST either:
  - meant "unknown": change the fixture to `None`; or
  - meant nested: flip the expectation to the new behaviour.

  List each file and line in §9.
- **Before coding the D3 fix:** `grep -rn "auto-provisioned when the SP was created\|sp_mapping" tests/`. Tests that
  model an account SP with an identity `sp_mapping` and expect `skip_root` flip to the presence-check behaviour;
  tests with a *different* mapped appId stay as they are.

**Mutation checks** (each must make the named tests fail):

| Mutation | Tests that fail |
|---|---|
| Remove the provision call | 1, 2, 12 |
| Remove the provision cache | 3 |
| Remove the dry-run guard | 7 |
| Revert `_is_recreated_sp` | 10 |
| Restore the "SP home auto-provisioned" shortcut in `_home_present` | 10, 11 |
| Remove the `update_one` guard | 21 |
| Treat `None` like `[]` | 25 |
| Drop the kind check | 26 |
| Remove `reconcile_on_adopt_types` | 22, 23 |
| Skip the GET after PATCH | 34 |
| Keep the old warning-only path for a failed PATCH | 33 |

**Baseline:** 651 passed. Done = baseline plus the 39 new tests, all passing (`python3 -m pytest`).

## 6. Docs and release
- `VERSION` → `1.2.1`.
- `CHANGELOG.md` `[1.2.1]`: Part A, Part B, the D3 latent SP fix, the adopt-path reconcile, and the upgrade notes
  from §4.
- `docs/RUNBOOK.md`:
  - **Homes:** replace every "home appears after a delay / on first login" line. Add failure-table rows for:
    - "home could not be created: owner not assigned" (assign the owner, then `failed_only`);
    - "home could not be created: other error" (check the run-as SP is a workspace admin, then `failed_only`).
  - **Nested identities** (new section):
    - what "via group" and "directly assigned" mean;
    - the DB3 note, and how to remove an extra direct assignment safely: target workspace **Settings → Identity and
      access → Groups → Remove from workspace**, **never** delete the account group; first check on the source that
      the identity's parent is assigned on the target;
    - that 1.2.1 keeps the cleanup in place.
  - **Job `run_as` SPs (unchanged rule, newly documented):** the import run-as identity needs
    `roles/servicePrincipal.user` on each run-as SP (PB5).
- `CLAUDE.md`: status line for 1.2.1.

## 7. Files touched
- `src/importers/base_importer.py`
- `src/importers/workspace_importer.py`
- `src/importers/identity_importer.py`
- `src/reports/import_report.py`
- `tests/test_plan16_3_1.py` (new)
- the existing test files named in §5
- `VERSION`, `CHANGELOG.md`, `docs/RUNBOOK.md`, `CLAUDE.md`

Nothing else.

## 8. Live QA (direct mode)
### 8.0 Environment (prepared 2026-11-23)
- **Source** `source_ws` = https://adb-7405618455700955.15.azuredatabricks.net (workspace id 7405618455700955).
  - Seeded. The manifest with every id is `/tmp/hf_seed/manifest.json`; the seed script is
    `/tmp/hf_seed/seed.py` (idempotent).
  - Users `ai27.hf.u01…u10@example.com`. Each is directly assigned, has `workspace-access` +
    `databricks-sql-access`, and a home containing nested `a/b/c`, one notebook per language (PYTHON, SQL, SCALA,
    R), `data/sample.csv`, `empty_folder`, and `extra_<n>` on even users.
  - In `u01`'s home `sql_assets/`: a query, a published Lakeview dashboard and a Genie space (all on
    `samples.nyctaxi.trips`, warehouse `f063fd8e380c4a16`).
  - Account SP `ai27_hf_sp_home`: directly assigned, home content under `/Users/<appId>/jobs_code/`. Its home did
    not exist before the seed's `mkdirs`.
  - `ai27.hf.u_deleted@example.com`: content under its home, **workspace assignment removed** (absent from source
    SCIM, so it is deleted in source for the roster).
  - Nested pattern:
    - parents `ai27_hf_P1`, `ai27_hf_P2`, `ai27_hf_P3` **direct**;
    - children `ai27_hf_C1` ⊂ P1, `ai27_hf_C2` ⊂ P2, `ai27_hf_C3` ⊂ P3, **nested only**;
    - SP `ai27_hf_S1` ⊂ P1 and user `ai27.hf.u_nested@example.com` ⊂ P2, **nested only** (all verified absent from
      `permissionassignments`).
  - Entitlements: P1 `workspace-access`, C1 `databricks-sql-access`, S1 `workspace-access`, P2 `workspace-consume`.
  - Job `ai27_hf_job_runas_S1` (`run_as` S1): ran SUCCESS on the source. C1 has `CAN_MANAGE_RUN` and S1 `CAN_VIEW`
    on it.
  - Workspace-local `ai27_hf_local` (members u03, u04, C2; `workspace-access`).
  - ACLs: C1 `CAN_READ` on `u01/a`; `u_nested` `CAN_EDIT` on `u02/a/b/nb_python`; S1 `CAN_RUN` on
    `u03/a/b/c/nb_python`.
- **Target** `target_ws` = https://adb-7405612139613163.3.azuredatabricks.net (7405612139613163).
  - The `ai27_pt_*` premise fixtures are removed before handover (§9). After that it holds no `ai27_hf_*` principal
    and no `ai27_hf_*` home.
  - `abhishek.iyer@databricks.com` signed in once (P5); no other human signs in.
- **Second target, for the upgrade paths** (§8.3): the user provides a second fresh workspace in the same account at
  QA time, logged in as CLI profile `target_ws_2`.
- **Run-as identity:** a workspace-admin SP on each target (OAuth M2M), with `roles/servicePrincipal.user` on
  `ai27_hf_S1` (needed for the `run_as` job, PB5).
- **Import config:** defaults (`workspace_home_backup=true`, `import_assets=all`, `retry_mode=off`).
- **Target pre-steps before the FIRST 1.2.1 run on `target_ws`** (all reversible; they set up the adopt path, the
  reverse case and DB1 in one run):
  1. Account level: add `ai27_hf_sp_home` as a member of `ai27_hf_P1` (`databricks account groups patch
     <P1 id> … add members`), and **assign `ai27_hf_P1` to `target_ws`** (`account workspace-assignment update
     7405612139613163 <P1 id> USER`).

     At phase start this gives:
     - **P1** exists on the target already (adopt path, direct on source: entitlements must be applied);
     - **C1 and S1** are visible via P1 (adopt path, nested only);
     - **`ai27_hf_sp_home`** is visible only via P1 although direct on source (the reverse case);
     - **C2 and `u_nested`** are not visible (create path → pass 3, once P2 is assigned).
  2. Target workspace SCIM: create a **workspace-local** group named **`ai27_hf_P3`** (`POST /api/2.0/preview/scim/v2/
     Groups {"displayName":"ai27_hf_P3"}`). This is the Plan 6 F6 "shadow": the importer refuses to assign the
     account group P3 (`prerequisite_missing`), so C3's only parent never reaches the target (DB1).
  3. Wait until P1, C1, S1 and `ai27_hf_sp_home` are listed in target SCIM (PB6: seconds).

### 8.1 Fresh target, 1.2.1: Part A
1. **Before:** `get-status` on every `ai27.hf.*` home and `/Users/<ai27_hf_sp_home appId>` on the target. All 404.
2. **Dry run:** zero homes created (re-check `get-status`). Home rows say "does not exist yet; a live run creates it".
3. **Live run:**
   - every assigned owner's home is created and owned (direct `CAN_MANAGE`);
   - **zero** home `prerequisite_missing` rows on pass 1;
   - all content lands at its source path: every notebook language, the CSV and the empty folder;
   - the query, dashboard and Genie space sit in `u01/sql_assets` (the dashboard renders);
   - the SP home is created and its notebook lands;
   - `u_deleted` content is in `/Users_Backup/ai27.hf.u_deleted@example.com/…` as `created_with_warning`, and **no**
     `mkdirs` was sent for `/Users/ai27.hf.u_deleted@example.com`;
   - ACLs land: C1 on `u01/a`, `u_nested` on the u02 notebook, S1 on the u03 notebook.
4. **Re-run:** all `skipped`; no root `mkdirs` for homes that now exist; no duplicates.
5. **D2 not-assigned owner:** before the run, remove `ai27.hf.u10` from the target (if assigned). Its home rows are
   `prerequisite_missing` with the "not assigned to the target workspace" wording and the server's
   `DIRECTORY_PROTECTED` text; no folder appears. Assign u10, run `failed_only`, and everything heals.
6. **Non-admin run-as (P6):** one import as a non-admin SP. The home rows carry the server's `PERMISSION_DENIED`
   text and category `prerequisite_missing`.

### 8.2 Fresh target, 1.2.1: Part B (same run as 8.1)
1. `permissionassignments` on the target gains **only** the direct identities (P2, the `ai27.hf.u01…u10` users,
   `ai27_hf_sp_home`; P1 was pre-assigned) plus `ai27_hf_local` (workspace-local groups are listed there, PB3). It
   gains **none** of C1, C2, S1, `u_nested`
   (v2 `GET /api/2.0/identity/workspace-assignments/{id}` → 404 for each).
2. C1, C2, S1 and `u_nested` are listed in target SCIM via their parents. Their rows are `adopted` (resolved in
   pass 3) — or `updated` when they were already visible at phase start and an entitlement was copied (adopt
   path, §9 J5) — with the "nested on source via …" note and the Assignment column `via group (…)`.
3. Entitlements are on the target: C1 `databricks-sql-access`, S1 `workspace-access`, P1 `workspace-access`, P2
   `workspace-consume`, u01…u10 `workspace-access` + `databricks-sql-access`.
   - Each of those rows shows them in **"Entitlements copied"**, and a re-run shows them under **"Already present"**
     only after a fingerprint change (otherwise the row is `skipped`).
   - **DB5 failure paths** (PATCH error; PATCH 200 but missing on the GET; healing via `failed_only`) are proven
     offline by tests 33, 34 and 36. The platform offers no deterministic way to make a valid entitlement PATCH
     fail, so live QA checks the success path and the report columns only.
4. **DB1:**
   - Group P3's row is `prerequisite_missing` (the shadow, Plan 6 F6).
   - C3's row is `prerequisite_missing`; its note names `ai27_hf_P3` with outcome `failed`.
   - Delete the workspace-local shadow `ai27_hf_P3` on the target, run `retry_mode=failed_only`: P3 becomes
     `created` (assigned), and C3 becomes `adopted` via group with **no** direct assignment.
5. Job `ai27_hf_job_runas_S1` is created with `run_as` S1 and a manual run succeeds. Its ACLs (C1, S1) land.
6. `ai27_hf_local` is created with members u03, u04 (C2 — a nested-only member — is reported unresolved in
   pass 2; workspace-local groups are out of scope, §9 J1).
7. **Re-run:** zero PUTs to `permissionassignments` and zero SCIM POSTs for C1, C2, S1, `u_nested` (check the
   driver log). All identity rows `skipped`.
8. **Adopt path and reverse case** (set up by the §8.0 pre-steps):
   - **P1** (pre-assigned): row `updated` (an entitlement was copied, §9 J5), its `workspace-access` entitlement is
     applied on the target, and its direct assignment is unchanged. This is the customer's original "group A
     entitlements not copied" case.
   - **C1 and S1** (visible via P1 at phase start): rows `updated` with the nested note; entitlements applied;
     still **no** direct assignment.
   - **`ai27_hf_sp_home`** (direct on source, only via P1 on the target at phase start): row `adopted` or `updated`,
     and it is now **directly assigned** on the target (`permissionassignments` contains it).
   - **C2 and `u_nested`** (not visible at phase start): resolved in pass 3, `adopted` via group.

### 8.3 Upgrade paths, on `target_ws_2` (the customer's sequence)
1. **Run 1.2.0** (main @ `4724c59`): install jobs from the 1.2.0 Git folder, then run inventory → export → import
   (live) against the seeded source. Record:
   - every `ai27.hf.*` and `ai27_hf_sp_home` home row, and everything under them (folders, the 4 notebook languages,
     the CSV, `u01/sql_assets` query/dashboard/Genie), is `failed` / `prerequisite_missing`;
   - `get-status` on every such home is 404;
   - C1, C2, S1 and `u_nested` are **directly assigned** (reproduces the customer);
   - note the `run_id`.
2. **Upgrade in place:** pull 1.2.1 into the same Git folder. No reinstall, no re-inventory, no re-export.
3. **Run the import only**, with `retry_mode=failed_only`, `dry_run=false` and the **same `run_id`**. Expect:
   - every previously failed home row is now `created` (root: provisioned note) or `created` content, in one run;
   - **zero** `prerequisite_missing` home rows remain;
   - the content lands at its source path, the folder-placed assets sit in `u01/sql_assets`, and their ACLs land;
   - `u_deleted` content goes to `/Users_Backup/…` (if it failed in 1.2.0 because backup was off, it is now
     diverted; if it was already diverted, its row is unchanged);
   - identity rows that 1.2.0 recorded as `created` / `adopted` are **not** retried by `failed_only`. C1, C2, S1 and
     `u_nested` **stay directly assigned** (DB3: never removed).
4. Re-run `failed_only` once more: nothing to do (no failed rows).
5. Run a normal 1.2.1 import (`retry_mode=off`). Force a fingerprint change on C1 at the source first (add an
   entitlement, then re-run inventory + export), so its row goes through update. C1's row carries the DB3 "left
   unchanged" note, and the new entitlement shows in "Entitlements copied".
6. On `target_ws_2`, remove C1's workspace assignment manually (Groups → Remove from workspace). Change C1 at the
   source again and re-export, then run 1.2.1: C1 **stays** nested (no PUT; check the driver log). With 1.2.0 it
   would be re-assigned.

### 8.4 Customer cross-check (read-only)
- If the customer agrees: run 1.2.1 import as a **dry run** against their target.
- The 24 groups + 1 SP report `direct on target (pre-existing); nested on source`.
- No identity write is planned.

### 8.5 Sign-off
- §8.1–8.3 all pass.
- `python3 -m pytest` is green.
- Record every observation in §9.

## 9. Judgment calls, deviations and handover log
- **2026-11-23 (planning):** all premises verified live (§0). P5 passed: first sign-in reused the tool-created home.
- **2026-11-23 (planning):** premise fixtures removed. On `target_ws`: jobs, cluster policy, secret scope,
  `/Shared/ai27_pt`, the marker notebook, the workspace-local group, and the 6 test homes. At account level: 6 users,
  7 SPs, 2 groups. Verified: no `ai27_pt*` group, SP or user remains on the target.
  - The user's own target home `/Users/abhishek.iyer@databricks.com` remains (empty apart from webapp folders); it is
    not part of the QA set.
  - Observation: a home root could only be deleted **after** its owner principal was deleted (`400` while the owner
    existed, `200` after). QA cleanups must delete principals first.
- **2026-11-23 (planning):** source seeded for §8 (manifest `/tmp/hf_seed/manifest.json`). It was not re-checked
  after seeding; the dev re-runs `/tmp/hf_seed/seed.py` (idempotent) before QA to confirm the source is intact.
- **2026-11-23 (dev):** implemented §3A, §3B, §6 on the working tree of `feature/group_entitlement_sync`
  (the user had not cut `hotfix/1_2_1`; that tree == `main` @ `4724c59` + untracked files, so the changes carry
  over unchanged to the branch the user creates). **Nothing committed** (user does all git).
  - Offline: **651 → 694 passed**. `tests/test_plan16_3_1.py` has the 39 planned tests as 40 functions (test 12 is
    parametrized over query / dashboard / Genie = 3 cases; one extra: `test_dry_run_intent_is_only_for_nested_creates`)
    = 42 cases; one existing test split into two (+1).
  - **Mutation checks: all 11 killed** (script `/tmp/wsmig_scen/mutate.py`; each file restored after). Three tests
    had to be strengthened, because the planned assertion could not see the mutation:
    test 3 (provision cache only matters for a REFUSED home — the presence cache short-circuits after a success),
    test 7 (a dry run never reaches create, so the guard is defence in depth — asserted by a direct
    `_provision_home` call), test 23 (1.2.0's post-adopt hook already PUT the assignment — asserted the adopt note).
  - Tests 14 and 28 goldens were **captured from 1.2.0** (`git archive 4724c59` → `/tmp/wsmig_main_1_2_0`,
    read-only; scenario `/tmp/wsmig_scen/scen.py`) and the 1.2.1 call logs are identical.
  - **Existing tests changed (intentional behaviour change):**
    - `test_importers_phase2_5.py`: `RecordingClient` gained opt-in `mkdirs_creates` / `mkdirs_errors`;
      `…user_home_directory_is_reported_as_a_prerequisite_not_mkdird` split into
      `test_an_absent_user_home_directory_is_created_with_mkdirs` + `test_a_user_home_the_platform_refuses_is_a_prerequisite_naming_the_reason`;
      `…content_under_an_absent_user_home…`, `…unmigrated_sp_home…`, `…orphan_sp_home_message…` (case a),
      `…home_owner_in_roster_but_import_failed…`, `…unknown_roster_does_not_silently_divert` now expect ONE root
      mkdirs refused with `DIRECTORY_PROTECTED` and the "not assigned" wording;
      `…sp_home_content_is_remapped…` and `…recreated_sp_home_still_remaps…` now create the NEW home (P10).
    - `test_plan16_3.py:578` `_FOLDER_MSG`: wording (J12).
    - `test_plan16_2.py` `test_schedule_on_unpublished_parent…`: pre-seeds the owner's home (the new home mkdirs
      shifted the fake's id counter, so the dashboard was no longer `d1`).
    - `test_identity_importer.py`: `FakeScimClient` now stores PATCHed entitlements/roles and returns them on GET
      (like the real API; `lose_patches` models "2xx but not applied"); `…failed_entitlement_patch_does_not_fail_the_identity`
      → `…fails_the_row_but_keeps_the_identity` (DB5).
    - The `grep -rn "workspace_permissions" tests/` step found **no** fixture feeding `[]` (all use `None` or
      `["ADMIN"]`); the `sp_mapping` grep found no identity-mapping + `skip_root` test beyond the phase2_5 ones above.
  - **Judgment calls:**
    - **J1 — (reverted 2026-11-23, user):** PASS 3 runs after pass 2, exactly as §3B.6. Consequence accepted by the
      user (customer has account groups only): a workspace-local group whose member is a nested-only identity
      gets that member reported unresolved in pass 2 (`ai27_hf_local` ⊃ C2 in §8.2.6 is not expected to resolve).
    - **J2 — parents come from the bundle groups' `members`**, matched by source SCIM id (`value` == the child's
      `source_id`, display name only as a fallback for an id-less member). Users' `group_memberships` is not
      usable: `groups` is stripped from the export payload (`transforms.STRIP_FIELDS`) and not carried. System and
      generated groups are never parents.
    - **J3 — a third re-assignment path:** besides `_assign_account_group` and `update_one`, the 1.2.0 post-adopt
      hook (`IdentityImporter._process_one`, `_ensure_assignment` after every ADOPT **and SKIP**) re-PUT `USER` for
      nested identities on every re-run. Guarded too (covered by test 21's skip half).
    - **J4 — `_ensure_assignment` runs at most once per principal per run** (`_ensured`) and records a successful PUT
      in `_target_assignments`, so `adopt_one` + the post-adopt hook never double-PUT. Direct call logs unchanged
      (test 28).
    - **J5 — adopt `changed` = "an entitlement was copied"** (§3B.4 wording) → `updated`; an adopt that only fixed
      the direct assignment stays `adopted` (1.2.0 semantics). §8.2.2 / §8.2.8 expectations corrected above.
    - **J6 — the DB5 `failed_only` retry goes through UPDATE, not ADOPT:** a failed row stores no fingerprint, so
      `decide()` returns UPDATE (row exists + on target). Same re-apply + re-verify; test 36 accepts
      `updated`/`adopted`.
    - **J7 — `EntitlementsNotApplied(NotApplied)`** carries `target_id`, the per-value result and the first server
      message; `base_importer._failure_extras` uses them, every other exception records exactly as before. On a
      create the assignment is applied BEFORE the row fails (the identity is fully in place; only entitlements are
      outstanding).
    - **J8 — dry-run nested rows** keep the base dry status (`created`, `dry_run=true`) with the note
      `dry run: would ADOPT (nested on source via …; no direct assignment)` — no new status value
      (`LAST_ACTIONS` unchanged). The Entitlements-copied cell shows `(dry run) would copy: …`.
    - **J9 — no re-sweep exists on main** (PLAN 13 B8 was abandoned), so the provision cache is per importer instance
      = per pass. Each folder-placed importer has its own cache and sees a home created earlier via `get-status`.
    - **J10 — the 1.2.0 "cheap insurance" parent `mkdirs` in `_upload_content` is kept** (byte-identical present-home
      calls, test 14): a notebook directly in a home root still sends one no-op parent mkdirs per notebook; the
      PROVISIONING mkdirs is once per root (test 3 uses a sub-folder).
    - **J11 — SP-home wording:** the old "present in the source roster but NOT migrated in this run" text is replaced
      by the platform's refusal (`DIRECTORY_PROTECTED` → "not assigned … Assign `<appId>`"). Deleted-in-source with
      backup off keeps a "deleted in source" reason.
    - **J12 — `missing_parent_prerequisite`:** the home reason is used only when the HOME is absent. When the home
      exists and only a sub-folder is missing, the text is now "import the folder's content family (workspace)
      first" (the "a user home is created on first login" clause is gone).
    - **J13 — workspace dry-run home rows** get `WorkspaceImporter.dry_run_intent`: `would CREATE — home <root> does
      not exist yet; a live run creates it.` (SQL / dashboard / Genie dry-run rows keep the generic text.)
    - **J14 — (dropped, user):** workspace-local-only nesting is out of scope (customer has account groups only).
- **2026-11-23 (live QA, target_ws, code uploaded to `/Shared/wsmig_hf_1_2_1`, jobs run as `ai27_hf_runas_sp`):**
  - Setup: run-as SP `ai27_hf_runas_sp` (ADMIN on target), source reader SP `ai27_hf_src_reader` (ADMIN on source,
    secret in target scope `ai27_hf_qa`), state + staging in `ai27_westus_target_ws_7405612139613163.wsmig_hf_qa`.
    §8.0 pre-steps done (P1 pre-assigned with `sp_home` as member; workspace-local shadow `ai27_hf_P3`). Source seed
    re-verified read-only (`/tmp/hf_qa/verify_source.py`; the seed script itself is NOT safe to re-run — it re-POSTs
    the query / dashboard / Genie space).
  - The P3 shadow makes **preflight NO-GO** (1.2.0 behaviour), so the QA runs use `preflight_enforce=false`.
  - Dry run (§8.1.2) PASS: no home, assignment or entitlement written; home rows "does not exist yet; a live run
    creates it"; nested rows "would ADOPT (nested on source via …; no direct assignment)". Observation (pre-existing,
    1.2.0, not changed): the shadow guard records P3 `failed` with `dry_run=false` inside a dry run.
  - Live: an identity-only run (job run 1084940841724012) started before the user stopped it, then the full
    end-to-end live run (938151212565548, SUCCESS). The identity rows are therefore in bundle `20261123_065809`
    and `skipped` (unchanged) in the full run `20261123_070643`; the target state is the same as one full run.
  - **Part B PASS:** C1, C2, S1, `u_nested` never directly assigned (`via group`); C1 `databricks-sql-access`, S1
    `workspace-access` copied; P1 (pre-assigned) `workspace-access` copied (the "group A" case); `sp_home` (direct on
    source, via P1 on target) now directly assigned; C3 `prerequisite_missing` naming `ai27_hf_P3: failed`, no
    fallback; users u01–u10 entitlements copied and shown in the new columns. `ai27_hf_local` 2/3 members (C2
    unresolved — J1, accepted).
  - **Part A PASS:** 10 user homes + `sp_home` + reader SP home created (`created`, provisioned note), each owner
    direct `CAN_MANAGE`; 0 home `prerequisite_missing`; all 4 notebook languages, CSV, empty_folder, extra_N landed;
    query / dashboard (published) / Genie in `u01/sql_assets` (same placement as source); `u_deleted` →
    `/Users_Backup`, no `/Users` home created; ACLs on nested principals landed (C1 on u01/a, `u_nested` on the u02
    notebook, S1 on the u03 notebook); job `ai27_hf_job_runas_S1` created once, `run_as` S1, ACLs C1/S1, manual run
    SUCCESS.
  - **Re-run PASS (§8.1.4 / §8.2.7)** (e2e live 482230906557731, bundle `20261123_073627`): every row `skipped` except
    the expected P3 / C3 failures; target direct assignments identical before/after (20 → 20, none new / removed /
    changed); C1, C2, S1, `u_nested` still not directly assigned; home roots `skipped` (no root mkdirs).
  - **DB1 heal PASS (§8.2.4)** (import job 24296085375212, `retry_mode=failed_only`, same run_id): shadow deleted →
    P3 `created` (directly assigned, the only new assignment); C3 `adopted`, `via group (ai27_hf_P3)`, NOT directly
    assigned.
  - Skipped by the user's decision: §8.1.5 (u10 not assigned — the platform refusal is premise P3, the wording is
    offline test 4) and §8.1.6 (non-admin run-as). Remaining: §8.3 upgrade on the user's new target.
  - Account-level leftovers kept for §8.3: QA SPs `ai27_hf_runas_sp` / `ai27_hf_src_reader`, `sp_home` ∈ P1.
- **2026-11-23 (live QA §8.3, upgrade, `target_ws_2` = 7405612484606198, folder `/Shared/wsmig_hf_upgrade`):**
  - 1.2.0 (`main` @ `4724c59`, read-only export) e2e live (426940852738003, bundle `20261123_085945`): **reproduces the
    customer** — 126 home-content rows + the query / dashboard / Genie space `failed` / `prerequisite_missing`
    ("USER HOME directory, which cannot be created…"), all 10 user homes 404; C1, C2, C3, S1, `u_nested` directly
    assigned (25 direct assignments). Deviation from the §8.3.1 expectation: `sp_home` content did NOT fail on
    1.2.0 — its home row falsely said "auto-provisioned when the SP was created", but the sub-folder mkdirs created
    the home as a side effect (P7), so its notebook landed.
  - Upgrade in place (1.2.1 uploaded over the same folder; no reinstall, no re-inventory, no re-export), import only,
    `retry_mode=failed_only`, `dry_run=false`, same run_id (1014767422148377): **PASS** — all 259 outstanding rows
    `created` (10 home roots with the provisioned note, 76 dirs, 40 notebooks, 10 files, query / dashboard + publish /
    Genie, 129 ACLs), 0 failed; no identity row retried; direct assignments 25 → 25 (nothing removed, DB3).
    Target checks (owner CAN_MANAGE, 4 languages, CSV, empty_folder, nested-principal ACLs, job run_as S1 run
    SUCCESS) pass.
  - §8.3.4 second `failed_only` (736664264804193): nothing to do (0 in-scope rows).
  - §8.3.5 PASS (e2e live 562462970835710): `workspace-access` added to C1 on source → C1 `updated`, copied
    `workspace-access`, already present `databricks-sql-access`, DB3 note "directly assigned on target, nested only on
    source (via `ai27_hf_P1`); left unchanged", Assignment `direct on target (pre-existing); nested on source`;
    direct assignments 25 → 25. Every other row `skipped` — 1.2.1 fingerprints equal 1.2.0's (no fingerprint change).
  - §8.3.6 PASS (e2e live 709978682784651): C1's workspace assignment removed by hand (still visible via P1), C1
    changed on source again → C1 `updated`, Assignment `via group (ai27_hf_P1)`, `allow-cluster-create` copied, NOT
    re-assigned (24 → 24); C1's ACL on `u01/a` intact. Source C1 restored to `databricks-sql-access` afterwards.
- **2026-11-23 SIGN-OFF (§8.5):** §8.1, §8.2, §8.3 pass; `python3 -m pytest` 694 passed. The code tested live
  (`/Shared/wsmig_hf_1_2_1` on target_ws, `/Shared/wsmig_hf_upgrade` on target_ws_2) is byte-identical to the
  working tree (`src/`, `notebooks/`, `jobs/`, `VERSION`). Not run live, by the user's decision: §8.1.5 (owner not
  assigned — platform refusal = premise P3, wording = offline test 4), §8.1.6 (non-admin run-as), §8.4 (customer dry
  run — optional, the customer's call). Known and accepted: J1 (a workspace-local group's nested-only member is
  reported unresolved); DB5 failure paths are proven offline only (tests 33/34/36); preflight is NO-GO while a
  workspace-local shadow exists (unchanged 1.2.0 behaviour; per-unit failures with `preflight_enforce=false`);
  pre-existing 1.2.0 quirk: the shadow guard records its row `failed` with `dry_run=false` inside a dry run.
  QA leftovers (not cleaned yet): account SPs `ai27_hf_runas_sp`, `ai27_hf_src_reader` (ADMIN on source, OAuth
  secret), `sp_home` ∈ P1, target_ws_2 folders/jobs/schema `wsmig_hf_qa`.
