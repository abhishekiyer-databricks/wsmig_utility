# PLAN 9 — Orphaned-home content backup (`/Users_Backup`)

**Status:** Triaged (agreed shape, not started). Implement + test together at the end, same as PLAN 8.
**Area:** `src/importers/workspace_importer.py` (primary), `src/importers/acl_importer.py`,
`src/config/config_manager.py`, `notebooks/04_Import.py`, `src/utils/job_templates.py` + `jobs/*.job.json`,
tests.
**Depends on:** the existing SP-home remap (`_remap_home_path`, IMP-6) and the home-presence guard
(`_guard_home_present`, PLAN 8 Bug 8/14) — this plan generalises both into one home-target resolver.

---

## 1. The scenario

1. A user (or service principal) is onboarded in the **source** workspace. Databricks
   auto-provisions their home `/Users/<userName>` (a user) or `/Users/<applicationId>` (an SP), and
   the person creates notebooks/files inside it.
2. The user/SP is **removed from the source workspace** (deprovisioned). Their home directory and
   its contents **physically remain** in the source workspace (Databricks does not delete home
   content on deprovision).
3. We run the utility. Because the identity is gone from source **SCIM**, it is **not** in the
   inventory roster and is **not** created on the target. But the orphaned home content **is still
   walked** by `workspace_collector._walk` (it lists `/Users/<owner>/…` like any other content) and
   lands in the bundle as `directory` / `notebook` / `workspace_file` units keyed by their source
   path.
4. On import, `WorkspaceImporter` cannot place that content:
   - `/Users/<owner>` (the home root) can only be *provisioned*, never `mkdir`'d → today it raises
     `PrerequisiteMissing` (`workspace_importer.py:254`–`292`).
   - descendants hit `_guard_home_present` → `PrerequisiteMissing` (`workspace_importer.py:229`–`246`),
     or, before that guard existed, the raw `400 DIRECTORY_PROTECTED Folder Users is protected` /
     `RESOURCE_DOES_NOT_EXIST parent does not exist` errors the user pasted.

   Net effect: **the orphaned files are never migrated** — they show as `prerequisite_missing`
   failures and are effectively dropped.

### Desired behaviour
When the home owner does not (and will not) exist on target, **do not try to recreate the content
under `/Users/`**. Instead recreate it under a top-level backup folder, preserving the sub-tree:

```
source:  /Users/<owner>/<rest…>      →   target:  /Users_Backup/<owner>/<rest…>
```

so the bytes are preserved and the operator can hand them back to whoever should own them.

---

## 2. Confirming the assumption ("mostly it'll be this reason only")

**Confirmed — for a normal end-to-end run it is the *only* cause, and it is deterministically
detectable.** (An earlier draft of this plan listed two other "causes"; discussion 2026-08-23 showed
both are non-issues — recorded here so we don't re-litigate them.)

- A user's `/Users/<email>` home is provisioned **when the identity is added to the workspace**, not
  on interactive login. So once the identity phase imports/assigns a user, the home exists and
  content lands — this is the observed happy path in every live test (nobody logged in). The
  "appears only on first login" note at `workspace_importer.py:221` is a *defensive reason to probe
  rather than assume* (as the code does for SPs), **not** a verified failure mode.
- Running workspace content **without** the identity family in the same session is operator error
  and out of scope: the end-to-end job always runs identity first, and we assume identity is
  imported. (We may even drop any code that tolerates the opposite.)

So content under `/Users/<owner>` only fails to place when **`<owner>` is not an identity on target
— i.e. it was deleted in source and never imported.** That is exactly the signal: the owner segment
is **absent from `identity_classification.json`** (the source-workspace SCIM roster captured at
inventory time; it already contains users by `userName`+`emails` and SPs by `applicationId`, and the
importer already reads it in `_sp_roster_status`, `workspace_importer.py:152`).

Why the roster check still earns its place (rather than just "is the home present on target?"):
divert must fire for **deleted-in-source** owners but NOT for the rare case where the owner *is* in
the roster yet its identity import genuinely **failed** this run (per-unit fail-soft). The latter is
a real `prerequisite_missing` that `retry_mode=failed_only` fixes into the **real** home — diverting
it would scatter a live user's files and then duplicate them on the fixed re-run. So:

- owner **absent** from roster → **divert to `/Users_Backup`** (deleted in source; the only place the
  bytes can go).
- owner **present** in roster but home not on target → **`prerequisite_missing`** (identity import
  failed/pending; recovers into the real home on retry — no bytes lost, just deferred).

### Config decision
Just **on/off** — no multi-mode selector (the "capture-everything" mode existed only to cover the
two non-causes above). Add:
- `workspace_home_backup: bool = True` *(default on)* — divert orphaned (roster-absent) home content
  to the backup root; `False` restores today's `prerequisite_missing` behaviour.
- `workspace_home_backup_root: str = "/Users_Backup"`.

---

## 3. Current code — the exact touch-points

- `is_user_home` / `home_owner` / `_looks_like_app_id` — `workspace_importer.py:68`–`87`.
- `_sp_roster_status(app_id)` — reads `identity_classification.json`, indexes **SP** applicationIds
  only — `:152`–`180`. **Generalise to users too.**
- `_remap_home_path(path)` — remaps a **recreated SP**'s home to its new appId — `:182`–`202`.
- `_home_present(home_root)` / `_guard_home_present(path)` — the presence guard — `:205`–`246`.
- `_create_directory(unit)` — home-root handling + `_guard_home_present` + `mkdirs` — `:249`–`300`.
- `_upload_content(unit, …)` — `_guard_home_present` + `_remap_home_path` + import — `:303`–`369`.
- `existing_keys()` — probes the (remapped) target path so re-runs adopt — `:106`–`127`.
- ACL resolution by path — `acl_importer._resolve_target_object`, `acl_importer.py:316`–`336`
  (directories/notebooks/files resolved via `workspace/get-status` on the **source** path).

Reporting/state need **no new enum**: a diverted object is recorded as
`created_with_warning` (`ACTION_CREATED_WITH_WARNING`, already surfaced in the report's warnings and
the per-asset-type Excel sheets), with a note naming the backup path and the reason. Idempotency is
free: `natural_key` stays the **source** path, `target_id` becomes the backup path — identical to
how the SP-home remap already works.

---

## 4. Design

### 4.1 One home-target resolver
Replace the scattered home logic in `_create_directory` / `_upload_content` with a single decision
function:

```python
def _resolve_home_target(self, path) -> HomeResolution:
    # returns (target_path, kind, note) where kind ∈
    #   "not_home"        – path is not under /Users → caller proceeds unchanged
    #   "skip_root"       – the /Users/<owner> ROOT itself, provisioned not created
    #   "remapped_sp"     – owner is a recreated SP (sp_mapping) → /Users/<newAppId>/… (IMP-6)
    #   "normal_home"     – owner's real home exists on target → use it as-is
    #   "backup"          – divert to <backup_root>/<owner>/… (this plan)
    #   "prerequisite"    – owner absent on target and NOT eligible for backup → PrerequisiteMissing
```

Decision order for a `/Users/<owner>/<rest>` path (root or descendant):
1. Owner in `sp_mapping` (recreated SP present on target) → **`remapped_sp`** (unchanged IMP-6).
2. Else owner's home present on target (`_home_present`, i.e. a real user/SP that got provisioned)
   → **`normal_home`**.
3. Else compute `roster = _roster_status(owner)`:
   - `workspace_home_backup` off → **`prerequisite`** (today's behaviour).
   - `roster == "absent"` (deleted in source) → **`backup`**.
   - else (`in_roster`/`unknown`: identity import failed or pending) → **`prerequisite`** (recovers
     into the real home on `retry_mode=failed_only`; never a silent divert).
4. The bare `/Users/<owner>` root maps to `<backup_root>/<owner>` under `backup`; under `remapped_sp`
   / `normal_home` / `skip_root` it is a no-op create (auto-provisioned), exactly as today.

`_roster_status(owner)` = the generalised `_sp_roster_status`: index the roster by **applicationId**
(SPs) **and** by `userName` + every `emails[].value` (users). Returns `in_roster` / `absent` /
`unknown` (missing/garbled classification file → treated as `unknown`, which under `orphaned_only`
falls through to `prerequisite`, never a silent divert).

### 4.2 Backup path construction
`backup_path = posixpath.join(backup_root, owner, <path-relative-to-/Users/owner>)`. `backup_root`
defaults to `/Users_Backup`. The importer `mkdirs` the backup root and every intermediate dir
top-down (the existing depth-sorted `load()` ordering already gives parents-first; the home ROOT
unit becomes the `<backup_root>/<owner>` dir, so descendants land into an existing tree). The
"cheap insurance" parent-`mkdirs` in `_upload_content` (`:332`) is reused for the backup parent.

> **Live-verify before implementation:** confirm a top-level `/Users_Backup` can be created via
> `POST /api/2.0/workspace/mkdirs` (it is *not* one of the protected roots; only `/Users` is). If a
> top-level create is refused on the customer's target, fall back to a `/Shared/Users_Backup`
> default (guaranteed writable by workspace admins). Keep it configurable either way.

### 4.3 `_create_directory` / `_upload_content` changes
Both call `_resolve_home_target(path)` up front:
- `not_home` → unchanged flow (still runs `is_skippable_path`, `_guard_home_present` for the
  non-home `/Users` edge cases is now folded into the resolver).
- `skip_root` / `normal_home` / `remapped_sp` → as today.
- `backup` → create/upload at `backup_path`; record `created_with_warning` with note:
  `"owner '<owner>' was deleted in source (absent from the source roster) — its home cannot be
  recreated under /Users/; content preserved at <backup_path>. Reassign these files to the intended
  owner if needed."` (For `on_any_failure` on a still-in-roster owner, word it as "owner not present
  on target yet; content backed up to <backup_path> — move it to the real home once the owner is
  provisioned.")
- `prerequisite` → raise `PrerequisiteMissing` with the existing, already-actionable messages
  (`:273`–`292`).

### 4.4 existence / idempotency
`existing_keys()` (`:106`) already probes `_remap_home_path(path)`. Extend the same call to probe the
**resolved** target (`_resolve_home_target`) so a re-run **adopts** the backup copy instead of
re-creating it. Because `natural_key` stays the source path and `target_id` = backup path, the state
store converges across runs with no schema change.

### 4.5 ACLs follow the divert
When the importer creates content at a diverted/remapped path it already adds it to
`context["workspace_paths"]`. Add a sibling `context["workspace_path_remap"] = {source_path:
target_path}` populated for **every** unit whose target path differs from its natural key (both the
SP-remap and the backup divert). In `acl_importer._resolve_target_object` (`:324`), for
`directories`/`notebooks`/`files`, look up `object_key` in `workspace_path_remap` first, then
`get-status` the resolved path. Effect: an orphaned object's ACL attaches to the backup object.
Individual grants naming the **deleted owner** principal won't resolve (`_remap_principal` returns it
unchanged and the API rejects/ignores it) — those grants are dropped/warned per-grant, which is
correct (the owner is gone). The ACL unit as a whole becomes `created`/`skipped_no_object`, never a
hard failure.

---

## 5. Config wiring

- `ImportOptions` (`config_manager.py:145`): add
  `workspace_home_backup: bool = True` and `workspace_home_backup_root: str = "/Users_Backup"`.
- `from_dbutils` (`:282`): parse the two widgets (`parse_bool` for the flag) and normalise the root
  in `validate()` (`:415`) — strip trailing `/`, ensure a leading `/`.
- `notebooks/04_Import.py` (near `:63`): add
  `dbutils.widgets.dropdown("workspace_home_backup", "true", ["true","false"], …)`
  and `dbutils.widgets.text("workspace_home_backup_root", "/Users_Backup", …)`.
- `src/utils/job_templates.py` / `jobs/*.job.json`: add both keys to the import task's declared
  `base_parameters` so `00_Install_Jobs` projects them (installer already fills only declared keys,
  `job_templates.py:112`–`117`).

---

## 6. Reporting

- Diverted objects appear as `created_with_warning` rows on their existing per-asset-type sheets,
  `target_id` = backup path, with the reason in the note — no new sheet strictly required.
- **Optional (nice-to-have):** a one-line run summary in `04_Import.py`'s tail
  (`print(f"Home backups: {n} objects diverted to {backup_root} (orphaned owners: …)")`) and/or a
  small "Home Backups" section in the import report keyed off
  `failure_category`/`import_status == created_with_warning` + the note prefix. Decide during build;
  keep it light.

---

## 7. Edge cases & decisions

1. **Root creatability** — verify `/Users_Backup` top-level create live (see §4.2); `/Shared/Users_Backup`
   is the fallback default.
2. **Owner segment collisions** — an email owner (`a@x.com`) and a UUID owner never collide; using the
   raw owner segment as the backup sub-dir keeps them separate and human-readable.
3. **Trash / `.bundle/` / `.db_internal` under a home** — `is_skippable_path` and the `import_action`
   branches (`base_importer._process_one` steps 1–2b) run **before** the resolver, so bundle-owned
   and platform-internal content is still skipped, never backed up.
4. **`unknown` roster** (classification file missing/garbled) — stays `prerequisite` (never a silent
   divert). Documented in the note.
5. **Deleted-after-inventory owner** — owner is still in the roster (so no divert) but was never
   imported → `prerequisite_missing` (recoverable framing on retry). Acceptable: this is a rare
   race, and the report names the owner.
6. **dry_run** — the resolver runs; the dry-run report shows "would CREATE at `<backup_path>`" via the
   existing dry-run path (no mutation).

---

## 8. Tests (offline, `tests/`, mirroring PLAN 8 style)

New unit tests (fixtures: fake bundle units + a stub `identity_classification.json` roster + stub
client):
1. `orphaned_user_home_content_diverted_to_backup` — `/Users/gone@x.com/proj/nb` →
   `/Users_Backup/gone@x.com/proj/nb`, recorded `created_with_warning`.
2. `orphaned_sp_home_content_diverted_to_backup` — `/Users/<absent-appId>/f` → `/Users_Backup/<appId>/f`.
3. `home_owner_in_roster_but_import_failed_stays_prerequisite` — no divert (recovers on retry).
4. `recreated_sp_home_still_remaps_to_new_appid_not_backup` — sp_mapping wins over backup.
5. `present_user_home_uses_real_home_not_backup` — `_home_present` true → normal path.
6. `workspace_home_backup_false_preserves_current_prerequisite_behaviour`.
7. `unknown_roster_does_not_silently_divert` — missing classification file → prerequisite, not backup.
8. `backup_root_is_configurable` — custom root honoured; trailing-slash normalised.
9. `backup_subtree_hierarchy_preserved` — nested dirs land parents-first under the backup root.
10. `rerun_adopts_backup_copy_not_duplicated` — `existing_keys` probes the backup path.
11. `acl_for_diverted_content_resolves_via_path_remap_and_never_hard_fails`.
12. `roster_status_indexes_users_by_username_and_email_and_sps_by_appid`.

Live smoke (append to `live_e2e_migration.py`): seed a source home, deprovision the owner, run
import, assert the content exists under `/Users_Backup/<owner>/…` on target and shows as a warning
row (also confirms `/Users_Backup` is creatable live — see §4.2).

---

## 9. Summary of files touched

| File | Change |
|---|---|
| `src/importers/workspace_importer.py` | `_resolve_home_target`, generalise `_sp_roster_status`→`_roster_status`, route `_create_directory`/`_upload_content`/`existing_keys` through it, publish `workspace_path_remap`. |
| `src/importers/acl_importer.py` | consult `context["workspace_path_remap"]` in `_resolve_target_object` for directory/notebook/file ACLs. |
| `src/config/config_manager.py` | `workspace_home_backup` + `workspace_home_backup_root` in `ImportOptions`, `from_dbutils`, `validate`. |
| `notebooks/04_Import.py` | two new widgets + surface the value in the run banner. |
| `src/utils/job_templates.py`, `jobs/*.job.json` | declare the two keys in the import task `base_parameters`. |
| `tests/…` | the 13 offline tests above + a live smoke. |
