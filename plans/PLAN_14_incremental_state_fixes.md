# PLAN 14 — Incremental correctness & state-load fixes

**Status:** PROPOSED (bug fixes). Created 2026-10-03 from the live incremental QA pass on the fresh
target `adb-7405610176410928.8` (branch `plan13-backlog-b1-b13`, commit `e33b3263`). Full evidence +
repro for each item is in `plans/PLAN_13_future_backlog.md` under QA-4/QA-5/QA-6/QA-7. This plan is the
dev-facing summary: the bug, what was noticed, the fix. Tester did NOT apply any of these.

**One-line theme:** the state table is the source of truth for incremental runs, and this branch
mis-handles it in two ways — the state-table *name* is resolved inconsistently (dry-run twin vs live),
and *failures in the state path are swallowed silently*. Fixing "resolve the state table once + never
swallow state errors" is the spine of all four fixes. Do them in the order below.

---

## 1. QA-7 — incremental state LOAD returns 0 rows → edits silently dropped  (CRITICAL)
- **Bug:** On an incremental run the import loads **0** prior state rows even though the state table
  holds them, so no object can be decided as UPDATE → every existing object is "Adopted" (not
  re-written) → **source edits silently do NOT migrate; the target keeps stale content.** Also breaks
  `retry_mode=failed_only` (empty state → re-applies everything) and amplifies QA-6.
- **Noticed:** `state loaded | rows=0 identity_rows=196` logged 3× in the live run AND again in a retry
  run, while Delta time-travel + a warehouse query prove the table held **21,264 rows** for the exact
  `source_workspace_id` (never deleted — Delta history is only CREATE/MERGE/OPTIMIZE). Verified on the
  target: edited `py_nb`/`sql_nb`/`config.json`/`README.md` and cluster-policy `wsmig_test_policy`
  (autotermination=30) all stayed at **old content**. The retry wrote 6,825 rows re-applying everything
  before being killed. The job uses `SparkSqlBackend.sql`, which is
  `try: [row.asDict() for row in df.collect()] except Exception: return []` — **it swallows any error
  into an empty list and logs nothing**, so the real `SELECT`/`collect()` failure is invisible.
  (Code is byte-identical to main — a latent bug exposed the first time the state table got large,
  ~21K rows: ~10K content + ~10K ACLs.)
- **Fix:**
  1. **Fail loud:** `SparkSqlBackend.sql` must log + re-raise on exception — never return `[]`. A failed
     state load must never masquerade as an empty table.
  2. **Guard:** on a live (`dry_run=false`) import, if the state load returns 0 rows, cross-check a
     `COUNT(*)` for the pair and ABORT loudly on a 0-vs-non-empty mismatch (don't silently re-apply/drop).
  3. With (1) in place, surface and fix the actual large-table `SELECT *`/`collect()` failure.
  4. `StatementApiBackend` (test/off-cluster path) has a sibling bug: it reads only `result.data_array`
     (chunk 0) and ignores `next_chunk_internal_link` — live-confirmed it returns 16,355 of 21,276 rows.
     Follow the chunk links (or use EXTERNAL_LINKS) so it handles >16K-row state tables too.

## 2. QA-6 — ACL parallelism race drops permission grants  (MED — B6 regression)
- **Bug:** With `parallel_threads>1`, applying ACLs can crash a worker with
  `RuntimeError: dictionary changed size during iteration`, dropping that object's ACL grant
  (non-deterministic which one). 0 occurrences in main; a true branch (B6) regression.
- **Noticed:** 1 ACL failure in the first full run, **6** in the incremental run (QA-7 amplifies it by
  re-applying ALL ACLs each run). Root: `state_store.target_ids_for` (`state_store.py:620`) iterates
  `self._cache` without a snapshot while another worker's `record()` (`:369`) inserts a key.
- **Fix:** snapshot before iterating — `for (at,nk),r in list(self._cache.items())` at `:620`, and the
  same at the sibling unsnapshotted reads `:291 / :596 / :611 / :642` (the code already does this at
  `:545`). Negligible overhead except at extreme scale. (Structural follow-up: PLAN 15.)

## 3. QA-4 — export "skip-unchanged" defeated in the packaged live job  (MED — packaging)
- **Bug:** The export side re-fetches 100% of workspace content on every incremental run instead of
  skipping unchanged content, because it reads prior fingerprints from the wrong (empty) state table.
- **Noticed:** export logged `content pass | to_fetch=9432 unchanged=0` on an incremental run where only
  ~9 items changed. Root: the packaged `direct_end_to_end_live` job gives the **export** task no
  `dry_run` param → it defaults `true` → `Config.state_table_fqn` resolves to the empty `…_dryrun` twin,
  which the live import never populates.
- **Fix:** resolve the state-table name **once, identically for read and write**; wire the live job's
  export task to `dry_run=false` (keep `dry_run=true` only in the dry-run job). Audit every template in
  `job_templates.py` / `00_Install_Jobs` for which tasks receive an explicit `dry_run`. (Same root family
  as QA-7: `dry_run`-derived state-table name resolved in the wrong place.)

## 4. QA-5 — home-dir reporting + dormant in-run re-sweep  (MED — product decisions, user-approved)
- **Bug / noticed:** (A) The report shows `/Users/<user>` directory create/fail, but provisioning a user
  home is the platform's job, not the tool's — it shouldn't be reported as the tool's create/failure.
  (B) After the QA-3 fix, the end-of-content-phase re-sweep is dormant (it was fed only by the removed
  `defer` path), so a home-content `prerequisite_missing` (owner assigned but home not yet writable) is
  not retried within the run.
- **Fix:** (A) do not emit `/Users/<user>` home-root rows at all — the only home-dir outcome the tool
  owns is a `Users_Backup/<owner>/…` divert failure (PLAN 9). (B) collect home-content
  `prerequisite_missing` units during the content phase and re-attempt them once at end-of-content-phase
  **before** the ACL phase (best-effort; explicitly accepted as not 100% fool-proof — a home that never
  materialises during the run still needs a retry run).

---

## Recommended order
1. **QA-7** fail-loud + guard — CRITICAL; stops silent data loss and unblocks correct `retry_mode`.
2. **QA-6** `list()` snapshot — one line; stops ACL drops under parallelism.
3. **QA-4** export `dry_run` wiring — restores incremental export skip.
4. **QA-5** A+B — reporting + in-run re-sweep. Then **PLAN 15** (ACL build-once) as the scale follow-up.

## What was validated GOOD this session (do not regress)
QA-3 home-content fix (homes provision on pass 1, 0 content failures; content phase in minutes vs the
old 7.5h); account-level identity assign-never-recreate (appId/scimId preserved); ACL parity
`match=10,367 / missing=0 / extra=0`; DAB-managed assets correctly skipped; dashboards/genie render
(UC tables present); additions + new identities + dashboard publish all migrate correctly. The failures
above are all about **updates to existing objects on an incremental re-run**, not first-time migration.

See `plans/PLAN_13_future_backlog.md` (QA-4..QA-7 full evidence), `plans/PLAN_15_acl_idmap_build_once.md`
(ACL scale optimization), [[plan13-live-qa-session-state]], [[plan13-live-qa-findings]].
