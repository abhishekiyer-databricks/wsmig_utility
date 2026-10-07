# PLAN 16.2 — Customer release fixes (ship today)

**Status:** READY FOR DEV 2026-10-05. Master: [PLAN_16_master.md](PLAN_16_master.md) (16.2 row, rescoped by the user 2026-10-05).
**Branch:** cut from `feature/logging_and_state_hardening` @ `c2cde5e` (16.1, live-QA'd 2026-10-05, not yet merged) — e.g.
`feature/16_2_customer_release`. 16.1 is a prerequisite of this release (logging), so 16.2 builds on it.
**Goal (user):** a version the customer can run today that (1) logs enough to debug failures, (2) skips `.db_internal`,
(3) migrates dashboard + Genie ACLs, (4) gives dashboards the same publish state, credentials mode, schedules and
subscriptions as the source, (5) applies the ACLs of objects
healed by a retry, (6) refuses to export without an inventory. **The customer has already run `main`**, so every item states its
upgrade path (master ground rule 7).
**Deferred by the user (2026-10-05):** bundle directory = job run id (B9) → 16.3. Everything else from the old 16.2 → 16.3 (via 16.2b, merged into 16.3 by the user 2026-10-05).

**Default-behaviour rule:** apart from the six items below, behaviour stays byte-identical to 16.1: same decisions, same
fingerprints, same natural keys, same state schema. **No fingerprint changes:** no existing unit's fingerprint may move,
otherwise the first upgraded run would mass-UPDATE.

---

## 1. Logging truth (16.1 QA findings QA16.1-2, QA16.1-3)
**1a. Resumed units say so.** `base_importer._process_one` step 3 (checkpoint resume, ~line 865) restores the prior outcome
via `_record(..., checkpoint=False)`, which calls `_log_outcome` → today `notebook X → created … note=187 bytes uploaded`,
indistinguishable from real work (Run 4: 88 such lines, 0 API calls).
- Add `resumed: bool = False` to `_record` → passed to `_log_outcome`. When set, the line is
  `<type> <key> → <status> (resumed from checkpoint — no API call this run)` + the prior target_id. Level rule unchanged
  (a resumed `failed` is still ERROR, so it is still visible in the cell).
- The import report/state are unchanged (the restored outcome is the right one to report).
**1b.** `ImportRunner.verify_bundle` (~line 136): log `Phase complete: verify bundle manifest — <n> files, 0 missing,
0 mismatched (<elapsed>)` on success, and on the skip path `Phase complete: verify bundle manifest — SKIPPED`. The failure path
raises as today (the stage's `live_run` logs it).
**Tests:** resumed unit → driver line contains `resumed from checkpoint`, no `decide` line, no client call; manifest phase
has a `Phase complete:` line (success + skip).

## 2. `.db_internal` — skip completely (B10)
**Today (verified 16.1 QA):**
- Inventory walks `/Users/<u>/.db_internal` and fetches its ACL → **403** per user home (15 WARNINGs per run).
- Export emits a directory unit + an ACL unit.
- Import's `is_skippable_path` does no API write, but the unit is recorded `created` ("workspace root / Trash path — exists
  by construction"), every run (W0-7). Its ACL unit becomes `skipped_no_object`.

**Change:** platform-internal paths (`_INTERNAL_SEGMENTS` = `/.db_internal`, `/.ide`, `/.databricks`; move the matcher to
`src/utils/helpers.py` as `is_platform_internal(path)`, used by collector + exporter + importers) are **skipped end to end**:
- **Inventory** (`workspace_collector`): still list the directory entry, so the object stays in the inventory with
  `migration_note = "platform-internal (Databricks-owned) — not migrated"`. Do **not** descend into it (its children are
  Databricks-owned), and do **not** fetch its ACL (no 403, no WARNING).
- **Export:** keep the directory unit (so it's in the bundle and in `export_status.xlsx`) with `import_action = "skip_internal"`.
  No ACL entry. Its fingerprint is computed as today (no change for `main`-written rows).
- **Import:** a new early branch in `_process_one` next to `skip_generated`: `import_action == "skip_internal"` →
  `_record(unit, ACTION_SKIPPED, note="platform-internal directory (Databricks-owned) — not migrated")`. No existence probe,
  no API call. The report shows **Skipped — platform-internal**, never Created.
- **Upgrade path:** `main` wrote a `created` row per `.db_internal` dir and a `skipped_no_object` ACL row.
  - Dir row → re-recorded as `skipped` on the first upgraded run. It is still in the bundle, so it never becomes
    deleted-in-source (`state_store.mark_missing_in_source` compares against bundle keys).
  - ACL row → no longer in the bundle, so `mark_missing_in_source` would flag it `deleted_in_source` and the report would show
    noise. Exclude platform-internal natural keys from `mark_missing_in_source` (one filter line), so the row is left untouched
    and never shown.
  - `main`-bundle units (no `skip_internal` action) still reach `is_skippable_path` → skipped, never written (unchanged).
  - A `main`-written bundle imported by 16.2 still has the `.db_internal` ACL units. The ACL importer skips any
    platform-internal object path → `skipped` ("platform-internal"), not `skipped_no_object`.
**Tests:**
- Collector with a `.db_internal` dir → no `permissions/directories/<id>` call, no walk below it, inventory entry
  present with the note.
- Export unit has `skip_internal` and no ACL entry.
- Import → `skipped`, zero client calls.
- `main`-shaped state with a `.db_internal` ACL row + a 16.2 bundle → row not marked deleted_in_source.
- `main`-shaped bundle (ACL unit present) → `skipped`, no call.

## 3. Dashboard + Genie ACLs (W0-1, HIGH)
**Cause (verified live, Wave 0 + 16.1 QA):** `export/acls.json` keys dashboards/genie by display name / title
(`acl_writer.collect_acls` ~lines 119/122). The state's `target_id_map("lakeview_dashboard"|"genie_space")` is keyed by PATH
(`<parent_path>/<name>`). So `AclImporter._resolve_target_object` (~line 345) misses and every grant is
`skipped_no_object`, with a misleading reason (`family_not_selected`). 110 + 55 grants on the Wave 0 bed.
**Verified 2026-10-05:** every `acls.json` entry carries `source_id` (= dashboard_id / space_id), and the state row of the object
carries the same value in `source_object_id` (also on `main`-written rows).

**Change:**
- `AclImporter._resolve_target_object`: for `dashboards` and `genie`, resolve by **source id** first:
  `state` row whose `asset_type` matches and `source_object_id == entry.source_id` → its `target_object_id`. Add a
  `StateStore.target_id_by_source_id(asset_type) -> {source_id: target_id}` helper, built once per phase from `_cache`
  (`list(self._cache.items())`).
  - Fallbacks in order: the existing natural-key map, then the context id maps the dashboards/genie importers filled this run
    (a dashboard created THIS run has its row in `_cache` already).
  - Keep the ACL unit's natural key (`dashboards:<name>`) unchanged, so `main`-written ACL rows are found and decided normally.
- Absence reason: when the object unit exists in the bundle and its state row is present with a target id, it can no longer
  say `family_not_selected`. Fix `_ABSENCE_REASON` selection so that `family_not_selected` is used only when the family really
  is not in `import_assets`.
- **ACL Parity** (`_objects_to_verify`, ~lines 472-506): include every dashboard/genie object with a target id (applied
  this run or recorded in state), so they appear on the "ACL Parity" sheet. Today they are silently absent, while the sheet
  shows "all match".
- **Upgrade path:** the customer's state holds these ACL units as `skipped_no_object`. That status is re-evaluated on every
  new run_id (verified on `main`, Wave 0 Run 2), so the first upgraded e2e run applies them. With item 5b they are also
  re-evaluated on a same-run_id re-run.
**Tests:**
- Name-keyed `acls.json` + path-keyed state (`main` shape) → grants applied via `PUT permissions/dashboards/<target>`
  (and genie).
- A dashboard created in the same run → resolved.
- `main`-shaped `skipped_no_object` ACL row + unchanged fingerprint → re-attempted and applied.
- Parity lists dashboards + genie.
- Family really not selected → still `family_not_selected`.

## 4. Dashboard publish + schedule parity (B7, user scope 2026-10-05)
**User requirement:**
1. Draft on source → draft on target.
2. Published on source → published on target.
3. Published + scheduled on source → published + scheduled on target (schedules ARE migrated).
4. ACLs copied as-is.
5. Published with viewer ("user") credentials → same on target.
6. Published with publisher credentials → same on target.

**Today:** `DashboardsCollector` collects only the draft; import creates/PATCHes a draft. So every source-published dashboard is a
DRAFT on the target (verified Wave 0 + 16.1 QA), and no schedule exists on the target.

### 4.0 Verified live 2026-10-05 — the dev builds on these facts, no further probing needed
Evidence: `~/Desktop/wsmig_runs/plan16_2/api_probes/` (`source_dash_read.json` = every source fixture dashboard;
`target_publish_schedule_probe.json` = every write call below, made AS the runner SP on the target, on throwaway dashboards
since deleted).
| # | Fact | Evidence |
|---|---|---|
| F1 | `GET /api/2.0/lakeview/dashboards/{id}/published` on a draft → **404 `NOT_FOUND` "Unable to find published dashboard"**; on a published one → 200 `{display_name, warehouse_id, embed_credentials, revision_create_time}`. **No content and no publisher identity is returned.** | source: 2 drafts → 404, 10 published → 200 |
| F2 | `embed_credentials: true` = "publisher credentials (shared data permissions)"; `false` = "viewer credentials (individual data permissions)". The source bed has both (6 true, 4 false). | source read |
| F3 | `POST …/{id}/published {embed_credentials, warehouse_id}` works **as the runner SP**, in both modes; a GET read-back returns the values sent. Without `warehouse_id` it uses the draft's warehouse. | probes A, B, C |
| F4 | **Publish is NOT idempotent**: every POST creates a new revision (`revision_create_time` changes). A re-POST with a different `embed_credentials` switches the mode. → only POST when something differs (diff-guard). | probe A ×3 |
| F5 | **Publishing does not change ACLs** (permissions identical before/after). | probe A |
| F6 | **ACLs are one object for draft + published**: `permissions/dashboards/{id}` → `object_id=/dashboards/<workspace object id>`, levels CAN_READ / CAN_RUN / CAN_EDIT / CAN_MANAGE (+ inherited). No separate "published" ACL. → item 3 already covers requirement 4; nothing extra. | source + target reads |
| F7 | `PATCH` of the draft does NOT change the published revision (same `revision_create_time`); only the published `display_name` follows a rename. | probe A |
| F8 | **Schedules require a published dashboard**: on a draft, `GET` and `POST …/schedules` → 404 "Unable to find published dashboard". → publish first, then schedules. | probe A |
| F9 | Schedule shape: `{schedule_id, cron_schedule{quartz_cron_expression, timezone_id}, pause_status, display_name, etag, warehouse_id?}`. `POST …/schedules` works as the SP with any tz/pause. **Duplicates are allowed** (the same cron twice → 2 schedules) → re-runs must map, not blindly create. | probes A, B |
| F10 | `PUT …/schedules/{sid}` needs the current `etag` and **replaces the whole schedule** (an omitted `display_name` became ""). → always send the full body. | probe A |
| F11 | Subscriptions: `POST …/schedules/{sid}/subscriptions {subscriber: {user_subscriber: {user_id}} \| {destination_subscriber: {destination_id}}}`. **Re-adding an existing subscriber returns the SAME `subscription_id`** (idempotent). | probe C |
| F12 | **On a dashboard published with PUBLISHER credentials** (embed=true) the SP CAN subscribe other users and notification destinations. **With VIEWER credentials** (embed=false) NOBODY (SP or admin user) can subscribe another user: "only allows users to self-subscribe". Destinations are rejected too: "only allows workspace user subscribers". A platform rule. (Cross-check 2026-10-05: the user added 2 subscribers in the UI to the target `wsmig_dash_pub_individual`, which the UI had published with embed=true, its default. The source original is embed=false, so it is consistent.) | probes A, C + UI |
| F13 | Notification destinations: `GET /api/2.0/notification-destinations/{id}` returns the config incl. email addresses (the create response shows `config.email {}`). | probe |
| F14 | The source fixture schedules (3) have **no subscribers** → the bed needs a subscription fixture (§8). | source read |

**Consequences the user must know:**
- **Publisher credentials (user decision 2026-10-05: document it in `docs/`, NO per-dashboard report note):** on the target
  the dashboard is published BY the migration SP. Viewers of an `embed=true` dashboard query with the **SP's** data access, not
  the original publisher's (no API publishes "as" someone else; the publisher isn't even readable, F1). Add a section
  "Dashboards: publish, credentials, schedules" to `docs/RUNBOOK.md` (+ a known-limitations line in `docs/README.md`):
  what is migrated, this credential caveat ("have the owner re-publish to restore their credentials"), the draft-content
  caveat, the viewer-credential subscriber rule, schedules created PAUSED.
- **Published content = the source DRAFT content.** No API returns the published revision's content (F1). If the source
  draft has unpublished edits (`draft update_time > revision_create_time`), the target's published version includes them. Flag
  it per dashboard as a WARNING note ("source draft has unpublished changes — target published = current draft").
- **Viewer-credential ("Individual data permissions") subscribers can't be recreated** (F12). User decision 2026-10-05:
  ONE note on that schedule's row in `import_status.xlsx` listing every affected user, comma-separated (see §4.1), plus the
  same single line in `manual_actions_import.md`.

### 4.1 Design — two new asset types in the dashboards phase (state, retry, report and checkpoint come for free)
Publish and schedules become their **own units**, not facets of the dashboard unit. So the `lakeview_dashboard` unit, its
natural key and **its fingerprint stay byte-identical to `main`** (no mass UPDATE on upgrade), and the existing upsert machinery
handles create / update / skip / retry / dry-run / report rows.

| Asset type | Natural key | One unit per | Payload (fingerprinted) | Present when |
|---|---|---|---|---|
| `lakeview_dashboard_publish` | `<dashboard natural_key>#published` | published source dashboard | `embed_credentials`, `warehouse_name` (resolved from the source id like the draft's), `revision_create_time` | source GET /published = 200 |
| `lakeview_dashboard_schedule` | `<dashboard natural_key>#schedule:<source schedule_id>` | source schedule | `cron_schedule`, `pause_status` (source value), `display_name`, `warehouse_name` (if set), `subscribers` = sorted list of `{kind: user, user_name}` / `{kind: destination, display_name, destination_type}` | per schedule of a published dashboard |
- **DAB:** both inherit the parent dashboard's `import_action == dab_redeploy` (bundle redeploy owns them).
- **Order** in `DashboardsImporter.load()`: all `lakeview_dashboard` units, then `…_publish`, then `…_schedule` (F8).
- Report: one sheet each ("Dashboard Publish", "Dashboard Schedules") via `import_report._ASSET_TYPE_TO_CARD`; inventory
  Dashboards sheet gains columns `Published`, `Credentials` (publisher/viewer), `Schedules`, `Subscribers`.
- **Collector** (`DashboardsCollector`, per dashboard):
  - `GET …/published`: 200 → published; **404 only** → draft; any other error → unknown + WARNING (no unit emitted, so
    nothing is changed on the target).
  - If published: `GET …/schedules`, and per schedule `GET …/subscriptions`.
  - `user_subscriber.user_id` → `userName` via the inventory's identity roster (an unresolvable id → subscriber kept as
    `{kind: user, user_id, unresolved: true}` → manual at import).
  - `destination_subscriber.destination_id` → `GET notification-destinations/{id}` → `display_name` + `destination_type`.
  - Reuse `plan13-backlog-b1-b13`'s `DashboardsCollector._published_state` / `_schedules`, with these fixes: only a 404
    means draft, and subscriptions are included.

**Import — `lakeview_dashboard_publish`** (existence probe = target `GET …/published` of the parent's target id):
- **CREATE** (target is a draft): `POST …/published {embed_credentials, warehouse_id: remap(warehouse_name)}` → GET read-back
  must match (else FAILED `not_applied`). Note: `published (publisher credentials | viewer credentials)`, plus the two
  consequence notes above when they apply (→ `created_with_warning`).
- **ADOPT** (target already published): compare `embed_credentials` + warehouse. Same → adopted, no POST (F4). Different →
  POST with the source values → `updated`.
- **UPDATE** (fingerprint moved = the source republished, switched mode or warehouse): POST → read back. This republishes the
  target's current draft, which the dashboard unit just PATCHed if the source draft changed.
- **SKIP** (fingerprint same): no call.
- Parent dashboard has no target id (failed/not selected) → `skipped_no_object` (healed by item 5 on a retry).
- **Source unpublished** (the publish unit disappears from the bundle) → `deleted_in_source` row (existing machinery). **Never
  unpublish** (no DELETE), even with `allow_deletes=true` — the delete hook for this type is a no-op flag:
  `published on target but no longer on source — unpublish by hand if intended`.

**Import — `lakeview_dashboard_schedule`** (needs the parent's publish unit done first, F8):
- Existence probe: `GET …/schedules` of the parent's target dashboard. Match in this order: the state's `target_object_id`
  (= target schedule_id), else an exact match on `(quartz_cron_expression, timezone_id, display_name)` → ADOPT. So the tool
  never duplicates a schedule (F9), including one a human already recreated.
- **CREATE:** `POST …/schedules` with the cron, tz, display_name and remapped warehouse.
  - **`pause_status`: if `pause_job_schedules=true` (the existing import widget, default true) → `PAUSED`, else the source
    value.** Same rule and reason as jobs: during a migration the source keeps running, so an UNPAUSED target copy would
    refresh twice and email subscribers twice. Note `created PAUSED (pause_job_schedules=true; source is UNPAUSED)`.
- **UPDATE:** `GET` for the current `etag` → `PUT` with the FULL body (F10), same pause rule.
- **Subscriptions** (on create, update AND adopt; idempotent per F11): for each source subscriber:
  - user → target user id by `userName` (SCIM lookup) → POST. User not on target → manual line.
  - destination → a target destination with the same `display_name` + `destination_type` → POST; none → manual line
    `create notification destination <name> on target, then re-run`. **Destinations are not created by the tool** (out of
    scope).
  - **Parent published with viewer credentials ("Individual data permissions") → no subscriber POST at all** (F12). The
    schedule row gets ONE note (user decision 2026-10-05) listing ALL its user subscribers, comma-separated, in the
    import Excel's Note column for that row:
    `published with Individual data permissions — subscribers must re-subscribe themselves (Databricks only allows
    self-subscription): alice@x.com, bob@x.com`. The same text is one line in `manual_actions_import.md` (one per schedule,
    not per user). Destination subscribers on such a schedule can't exist on the source either (F12), so none are expected.
  - Any manual subscriber (missing user, missing destination, viewer-credentials list) → status `created_with_warning`,
    with all of them in the row's note. Extra target subscribers are never removed.
- Source schedule deleted → `deleted_in_source` flag. Never deleted on the target (as jobs).

**Upgrade path (customer ran `main`):** `main` state has no rows of the two new types → the first upgraded run CREATEs them:
the `main`-created draft dashboards get published with the source's mode, then get their schedules + subscriptions. The
dashboard units themselves are SKIPPED (fingerprint unchanged), so nothing else is touched. A `main`-written bundle has no
publish/schedule units → nothing happens until the next export (the e2e job always re-exports).

**Tests:**
- Collector: 404 → draft, no schedule calls; 200 → publish unit + schedules + subscribers resolved to user_name /
  destination name; another error → no unit + WARNING.
- `lakeview_dashboard` fingerprint is identical to `main` for the same source.
- Publish: create → 1 POST + read-back; adopt-same → 0 POST; adopt-diff → 1 POST; fingerprint moved → 1 POST; skip → 0 calls;
  parent failed → `skipped_no_object`; source unpublished → flag, never DELETE (also with `allow_deletes=true`).
- Draft newer than the published revision → unpublished-changes WARNING; embed=true alone → no warning.
- Schedule: create PAUSED when `pause_job_schedules=true`, source value when false; an identical existing schedule →
  ADOPT (no POST); update uses etag + full body.
- Subscriptions: user remapped by user_name; missing user → manual; destination by name → POST, missing → manual;
  viewer-credentials → 0 subscriber POSTs + ONE note on the schedule row listing all subscribers comma-separated (and one
  `manual_actions_import.md` line); a re-run → 0 new subscriptions.
- `main`-shaped state (dashboard rows only) + 16.2 bundle → publish + schedule CREATE, dashboard SKIP.
- Dry run → `would publish` / `would create schedule`, no POST.

## 5. ACLs follow a successful retry (16.1 QA, user: "retry should create the object and also apply ACLs")
**Today (verified 16.1 QA Runs 3 + 4):**
- `retry_mode=failed_only` narrows the work list to `failed` rows (`state.retry_keys`). The ACL units of those objects are
  `skipped_no_object`, so they are left out. Run 3 healed 143 objects and applied **0** of their ACLs.
- A same-run_id normal re-run then replays those ACL units from the checkpoint (`staging.is_done` = any recorded outcome), so
  they stay `skipped_no_object` until a new run_id.

**Change:**
- **5a.** `ImportRunner`: before the ACL phase, compute `acted = {(asset_type, key)}` of every unit recorded this run as
  `created`, `updated`, `adopted` or `created_with_warning` (from the importers' `result` rows). When `retry_keys` is a set,
  add to it every ACL unit whose object is in `acted`.
  - The ACL unit → object mapping is the same one the ACL importer resolves with: `perm_object_type` + natural key → the
    object's (asset_type, natural_key). For dashboards/genie, use the source id (item 3).
  - Implement the mapping once, as an `AclImporter.object_ref(unit)` helper, and test it per object type.
  - Mode-independent: works for `failed_only`, `skipped_only`, `failed_and_skipped`.
  - **Same rule for the dashboard child units (§4):** when a `lakeview_dashboard` is acted on this run, its
    `…_publish` and `…_schedule` units join the work list too, so a retried dashboard comes back published and scheduled.
- **5b.** Checkpoint resume (`_process_one` step 3): a prior outcome of `skipped_no_object` is **not** treated as done. The unit
  falls through to the normal decision, so a same-run_id re-run re-checks it (cheap: the object either exists now or not).
  `failed` is already re-attempted on retry; keep `main`'s behaviour for every other status.
- **Upgrade path:** no state/schema change; `main` checkpoints simply have `skipped_no_object` entries that are now
  re-checked.
**Tests:**
- `failed_only` with a failed notebook + its `skipped_no_object` ACL row; the notebook now succeeds → the ACL unit is in
  the work list and gets applied.
- The notebook fails again → the ACL is not attempted (still not in the work list).
- A same-run_id re-run with a `skipped_no_object` checkpoint entry → re-decided, not replayed.
- Units not acted on are still not in the work list.

## 6. Export hard-fails without an inventory (user 2026-10-04)
`ExportRunner._load_inventory` (~line 86): replace the fallback (today it re-runs the whole inventory, then continues with an
EMPTY inventory) with `raise InventoryMissingError(...)`. Raise when the file is absent, unreadable/bad JSON, or has no
`objects_by_type`. The message names the path, the resolved run_id and says "run 01_Inventory first (or pass the run_id of a
completed inventory)".
- The export task goes red. Nothing is written to the bundle. `InventoryRunner` is never constructed by export.
- **Upgrade path:** none needed (it only affects a broken run).
**Tests:** absent / corrupt / missing-`objects_by_type` → raise with path + run_id; no `InventoryRunner` import/construct;
a valid inventory → unchanged.

---

## 7. Files touched
`src/importers/base_importer.py` (1a, 2 import branch, 5b) · `src/importers/import_runner.py` (1b, 5a) ·
`src/utils/helpers.py` (`is_platform_internal`) · `src/collectors/workspace_collector.py` (2) · `src/exporters/asset_export.py` /
`acl_writer.py` (2: `skip_internal`, no ACL entry) · `src/importers/workspace_importer.py` (2: use the helper) ·
`src/state/state_store.py` (`target_id_by_source_id`, the `mark_missing_in_source` filter) · `src/importers/acl_importer.py` (2, 3, 5a
helper, parity) · `src/collectors/dashboards_collector.py` + `src/exporters/asset_export.py` (4: the two new unit types) ·
`src/importers/dashboards_importer.py` (4) · `src/importers/phases.py` (the two asset types in the dashboards family) · `src/exporters/export_runner.py` (6) · `src/reports/import_report.py` (status label `Skipped — platform-internal`, the publish
note) · `docs/RUNBOOK.md` + `CHANGELOG.md` · tests in `tests/test_plan16_2.py`. Full offline suite must pass.

## 8. Live QA (Claude) — fresh target, classic job cluster, LIVE + direct
Same harness as 16.1 (`~/Desktop/wsmig_runs/plan16_1/setup/`: `reset_jobs.py`, `collect.sh`, `logcheck.py`, `cellout.py`;
audit + `DESCRIBE HISTORY` queries). Reports → `~/Desktop/wsmig_runs/plan16_2/`. Source bed unchanged (`catalog_src_g9c6nd`).
0. **Fixture addition (source, before Run A)** — new idempotent phase `dash_subscriptions` in `tests/fixtures_medium.py`:
   - on `wsmig_dash_pub_sched` (publisher credentials): a user subscriber (`tanveer.singh`) + an EMAIL notification
     destination `wsmig_test_dest` as a destination subscriber;
   - a new `wsmig_dash_pub_sched_viewer` (viewer credentials, published, one schedule) that the QA user self-subscribes to;
   - re-run the `acls` phase for it.
   Expected on target: the first → schedule + both subscribers (pre-create `wsmig_test_dest` on the target for the adopt
   path); the second → schedule + the comma-separated "re-subscribe" note naming the QA user.
1. **Setup** (fresh `target_ws`): runner `ensure` + `scope`, `target_uc_prep`, state schemas `wsmig_state_main`,
   `wsmig_state_16_2`, staging volume, Git folders `main` @ `924d4f8` and the 16.2 commit, install jobs (16.2 installer), plus a
   `main` e2e job (as in 16.1 compat). (The dashboard APIs are already verified, §4.0 — no probing needed.)
2. **Run A — `main` e2e** → `wsmig_state_main`. This is the customer's existing state, and the new **golden baseline on THIS
   bed** (snapshot it). Expected `main` behaviour: dashboards are drafts, dashboard/genie grants `skipped_no_object`,
   `.db_internal` dirs `Created`, home-delay failures.
3. **Run B — 16.2 e2e on top of `wsmig_state_main` (THE UPGRADE TEST, new run_id).** Expect:
   - state load `rows=N expected=N`;
   - dashboard + Genie grants applied (live `GET permissions/dashboards/<id>` = source grants, principals remapped), and the
     ACL Parity sheet lists them, all match;
   - every source-published dashboard is published on target, with the same `embed_credentials` (live GET `/published`),
     drafts stay drafts; every source schedule exists on the target (cron/tz/display_name, PAUSED per `pause_job_schedules`),
     no duplicates on a re-run; subscribers per the §4 rules (user + destination on the publisher-credential dashboard,
     for the viewer-credential one ONE note listing the subscribed users comma-separated; a manual line for the missing
     destination); dashboard ACLs = source;
   - `.db_internal` rows `Skipped — platform-internal`, no 403 WARNINGs in inventory, no new deleted-in-source rows;
   - **no duplicates** (object counts per type unchanged, except the publish state);
   - audit: writes only = ACL PUTs on dashboards/genie + publish POSTs + retried home-delay failures. Unchanged objects
     untouched.
4. **Seed** a failure that heals: home-delay content usually provides it. If homes are already provisioned before Run A,
   use the incremental seed's H (library on a terminated cluster) plus withhold `servicePrincipal.user` on `ai27_acc_spn_1`
   for the run-as job (grant back before Run C).
5. **Run C — standalone import `retry_mode=failed_only`, `dry_run=false`, run_id = Run B.**
   - Only failed units acted on.
   - The ACLs of the healed objects are applied in the same run (live GET permissions).
   - A same-run_id re-run afterwards → resumed lines say `resumed from checkpoint — no API call`, and `skipped_no_object`
     units are re-decided.
6. **Run D — 16.2 e2e on a FRESH schema `wsmig_state_16_2`** → golden diff vs Run A. Expected diffs only:
   - dashboard/genie ACL units Created instead of no-object;
   - `.db_internal` Skipped instead of Created;
   - the publish notes;
   - ACL parity gains the dashboards/genie rows.
7. **Export hard-fail:** standalone export with a run_id that has no inventory → task red, message names path + run_id.
8. **Logging:** `logcheck.py` on every run (one start + one outcome per object, ERROR per failure, every Phase paired incl.
   the manifest phase).
9. Close-out: `plan16_2/FINDINGS.md`, a verdict per item, bugs → §10.

## 9. Decisions (user, 2026-10-05)
- Scope = the six items above; B9 → 16.3; other old 16.2 items → 16.3 (16.2b merged into 16.3, user 2026-10-05).
- `.db_internal`: skip completely (dir, contents, ACLs) but keep it visible in the reports as skipped.
- Dashboards (user 2026-10-05): draft→draft, published→published, same credentials mode (publisher/viewer), schedules
  + subscriptions migrated, ACLs as-is (verified: one ACL for draft+published, F6). Never unpublish / never delete a schedule
  (flag only). Schedules follow `pause_job_schedules` (default PAUSED, like jobs). Notification destinations are not
  created (adopted by name, else manual). Viewer-credential subscribers are manual (platform rule F12): one comma-separated note per schedule row. The
  publisher-credential (SP) caveat goes in `docs/`, not in report rows. A cutover "resume schedules" option is deferred
  (master plan 16.7).
- DEBUG lines in the cell's stderr panel (QA16.1-1): accepted by the user, no change.

## 9b. Dev notes (implemented offline 2026-10-05 on `feature/dashboard_publish_and_acl`)
All six items done; **497 offline tests pass** (441 + 56 in `tests/test_plan16_2.py`), key fixes
mutation-checked. Judgment calls the plan left open — check these during live QA:
- **§2** A `main`-bundle content unit under `.db_internal` (no `skip_internal` action) is ALSO recorded
  `skipped — platform-internal` (by path), not `created`. Export never fetches content under an internal
  folder (only an older inventory can carry any). The workspace importer's up-front existence probe skips
  internal paths too (zero calls).
- **§3** An ACL whose object is absent while its family IS selected is now `object_absent` (new category);
  `family_not_selected` only when the family really isn't in `import_assets`. A DAB dashboard/genie ACL
  reads `dab_redeploy` (from its bundle unit's action). Parity also lists a dashboard/genie that is on
  target but whose ACL was not applied (verdict `missing_on_target`).
- **§4** Publish/schedule existence is probed lazily per unit (`probe_existing` hook): a SKIP costs one
  read (`GET …/published` or `…/schedules`), zero writes. Read-back mismatch after publish → FAILED
  `not_applied` (new category). A schedule on a target dashboard that is not published → `prerequisite_missing`
  (retryable). Schedule fingerprint includes the parent's `embed_credentials`, so a credentials-mode
  switch re-evaluates subscribers. A schedule left `created_with_warning` (pending subscribers / the
  viewer-credential note) and a child left `skipped_no_object` are RE-CHECKED on every run as an adopt
  (subscribers only — no PUT, the target pause state is never touched), so a destination/user created
  later heals on a plain re-run. Target user id: identity map `scim_ids`, else a SCIM `userName eq` lookup.
  Source `user_id → userName` is resolved in `InventoryRunner` from the identity roster. If every source
  dashboard is unpublished, the publish rows are still compared for deleted-in-source.
- **§5** ACLs follow via the runner (`AclImporter.retry_keys_following` + `object_ref`); dashboard
  children follow inside `DashboardsImporter` (same phase — parents run first). A `main` checkpoint row
  without `asset_type` in the dashboards family replays as `lakeview_dashboard`.
- **§6** `tests/test_config_auth.py` redaction test wrote `inventory.json` at the wrong path and silently
  relied on the removed fallback — fixed to `BP.INVENTORY_JSON`.

## 10. Findings
(live QA appends here)
