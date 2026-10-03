# PLAN 16.1 — Wave 1: step-by-step run-output logging + state-store hardening

**Status:** READY FOR DEV 2026-10-04 — all research done (§3.0, §4.0, §10), all decisions made (§9).
A new session can implement directly from §3–§5 and §7, then run §8. Master: [PLAN_16_master.md](PLAN_16_master.md).
**Branch:** `plan16-1-logging-state`, cut from `main`.
**Behaviour change vs `main`:** none in what gets migrated. More (and better) output lines, no
`execution_*.log` files, and state-table read failures now stop the run instead of silently
looking like an empty table.

---

## 1. Requirements (user, verbatim intent)
- **Logging (B5, revised 2026-10-04):** "every step needs to be logged and displayed in the run output
  in the notebook. Later the logs can be downloaded from the job run if required. Inventory, export &
  import, any task, any point of time we should know what it has done and where it is, so we can
  easily debug failures or a stuck run." **No log files** — there is no reason to write them.
- **State hardening (PLAN_14 QA-7):** an incremental run loaded 0 state rows from a table holding
  21K, because `SparkSqlBackend.sql` turns any error into `[]`. A state read must never fail silently.

## 2. Scope
**In:** a new logger (run output only); full step coverage in all three stages + the HTTP client +
state store + reports (two outputs: a bounded INFO view in the notebook cell + a complete DEBUG log
with a start AND an end line for every object in the driver log); removal of the execution-log files;
an audit
of the silent `except` handlers; state-store fail-loud reads, the count check on every state load,
non-destructive MERGE, visible flush failures, `_cache` snapshot helpers.
**Out:** any change to what gets migrated, decisions, report content, widgets other than
`log_level`. Parallelism (16.5). Export using the state table (16.6).

---

## 3. Part A — Logging

### 3.0 Verified facts this design is built on (classic, DBR 15.4 LTS, `target_ws`, 2026-10-04)
Probe jobs `890117039318697` (run `1101394810113629`) and `146251838020838` (run `963338265814303`):
| Fact | Evidence |
|---|---|
| Job **cell output (stdout) is capped at 30 MB total; exceeding it FAILS the run** | 150K × ~180 B lines and 1M lines → `FAILED: The size of the run output (-1 bytes) exceeds the limit (31457280 bytes)`; 50K lines passed. Docs: 8 MB/cell + 30 MB total → "run is canceled and marked as failed" |
| A large cell output also makes the run **un-exportable** (no download from the run page) | 50K-line run: `export-run` → `Notebook size exceeded the byte limit: 14422611 > 10485760 bytes` |
| Everything printed to the cell (stdout) **is also written automatically to the driver log `stdout` file** | driver `stdout` had exactly 50,000 `CELL` lines for the 50K task |
| Writes to **`sys.__stderr__` do NOT count toward the 30 MB limit**: the cell keeps only a ~50 KB head+tail snippet of stderr, and the **driver log `stderr` file has every line** | 300K lines ≈ 55 MB to `sys.__stderr__` (direct and via a `logging` handler) → run SUCCEEDED, export fine, cell stderr = ~50 KB, driver `stderr` = all lines |
| Databricks writes each `sys.__stderr__` line **twice** in the driver `stderr` file (platform behaviour, both raw writes and logging) | 300K lines → 599,206 / 599,219 occurrences |
| Lines from **worker threads** (plain `print`, logger, write to a captured stream) appear in the cell | small probe: all three present in the cell output → `pin_stdout` is not needed |
| Driver logs (stdout/stderr/log4j) are downloadable from the run's **compute → Driver logs** for **30 days** after the job cluster terminates; viewable by **CAN MANAGE** on the job compute by default; Databricks does **not** redact secrets in them | Azure docs `compute/clusters-manage`. → **no cluster log delivery needed** |
| State-table read on classic returns all rows | §4.0 |

### 3.1 Two outputs (user decision 2026-10-04)
| Output | Handler | Level | Content | Size bound |
|---|---|---|---|---|
| **Notebook cell** (the run page) | stdout (resolved per record, as `main` prints) | **`log_level` widget, default `INFO`** | stage + phase start/end with counts and elapsed; **progress line every 500 objects** in any family with > 500 units (`Phase import notebooks: 4,500/10,555 — created 48, unchanged 4,440, failed 12 (3m10s)`), one at the end otherwise; every WARNING/ERROR with object + category + raw server error, **capped at 2,000 failure/warning lines per stage**, then one line `… further failures are in the driver log (stderr) and import_status.xlsx` and the progress lines keep the running failure count | ≤ ~2 MB at 1M objects (≈2,000 progress lines + ≤2,000 failure lines + phase lines), far below 30 MB |
| **Driver log `stderr`** (downloadable) | `logging.StreamHandler(sys.__stderr__)` | **always `DEBUG`**, independent of `log_level` | the COMPLETE log: every INFO/WARNING/ERROR line above **plus** per-object start + end lines for every object in every family, every API call, decision inputs, tracebacks, and every failure (no cap) | unbounded; does not count toward the 30 MB limit |

**Why the driver log is always DEBUG:** a customer running at the default never has to re-run to give
us the detail. They download **Driver logs → Standard error** for the failed/stuck task and send it.
It contains the cell lines too, so it is the one file needed. Each line appears twice in that file
(platform behaviour, §3.0); the line format carries a monotonically increasing sequence number `#n` so
duplicates are obvious and easy to de-duplicate (`sort -u -t'#' …`).

**Not done:** no extra stdout→stderr copy of cell lines beyond the single stderr handler, no
`pin_stdout`, no heartbeat thread, no cluster log delivery, no log files of our own.

### 3.2 The logging model: say what it is doing NOW, then how it ended (user design 2026-10-04)
Every phase and every object gets a **start line before the work** and an **end line after it**. In the
**driver log** (DEBUG) this holds for every object, so its last line is always the object + API call in
flight. A stuck run shows `importing X` with no matching end line; a failed run shows the object, raw
error and traceback. The **cell** shows phases + every-500 progress + failures, so it always names the
current phase and how far it got.

Driver log (stderr) — import:
```
… #10231 INFO  [identity_importer] run=… stage=IMPORT | Phase: import users — 156 units
… #10232 DEBUG [identity_importer] run=… stage=IMPORT | importing user abhishek.iyer@databricks.com
… #10233 DEBUG [api]               run=… stage=IMPORT | GET  api/2.0/preview/scim/v2/Users?filter=… → 200 (142 ms)
… #10234 DEBUG [base_importer]     run=… stage=IMPORT | decide user abhishek.iyer@…: state_row=no exists=no → CREATE
… #10235 DEBUG [api]               run=… stage=IMPORT | POST api/2.0/preview/scim/v2/Users → 201 (388 ms)
… #10236 DEBUG [identity_importer] run=… stage=IMPORT | user abhishek.iyer@databricks.com → created  target_id=7712…
… #10237 DEBUG [identity_importer] run=… stage=IMPORT | importing user aman.bansal@databricks.com
…
… #10712 INFO  [identity_importer] run=… stage=IMPORT | Phase complete: import users — created 150, adopted 6, failed 0 (42s)
```
Cell (INFO) — same run:
```
… INFO  [identity_importer] stage=IMPORT | Phase: import users — 156 units
… INFO  [identity_importer] stage=IMPORT | Phase complete: import users — created 150, adopted 6, failed 0 (42s)
… INFO  [workspace_importer] stage=IMPORT | Phase: import notebooks — 10,555 units
… INFO  [workspace_importer] stage=IMPORT | import notebooks: 500/10,555 — created 480, unchanged 18, failed 2 (41s)
… ERROR [workspace_importer] stage=IMPORT | notebook /Users/b@x.com/nb7 → FAILED prerequisite_missing: <raw server error>
```
Per-object outcome levels (one rule, everywhere): `failed` → ERROR; `created_with_warning` and any
degraded/best-effort outcome → WARNING; everything else (`created`, `updated`, `adopted`, `skipped`,
`manual`, `skipped_no_object`, `not_selected`) → DEBUG. So failures and warnings reach the cell, and
successes stay in the driver log. Start lines are always DEBUG.

**Where the per-object lines live (implement once, not per importer):**
- Import: `BaseImporter._process_one` logs the start line (`importing <asset_type> <natural_key>`) and the
  decision inputs (DEBUG); `BaseImporter._record` logs the outcome line with the level rule above and
  calls `progress()`. Every importer inherits it; no per-importer edits are needed for per-object lines.
- Export content pass: the `parallel_map` consumer loop in `ExportRunner._fetch_content` (runs on the
  main thread) logs `fetched <path> (size)` / failure; `ContentFetcher.fetch` logs `fetching <path>` (DEBUG,
  worker thread, which is fine per §3.0).
- Inventory: `workspace_collector._walk` (per directory) and `fetch_acl` (per object); plus every collector
  loop that issues one GET per object (find them with `grep -n "for .* in" src/collectors/*.py` near a
  `self.client.get(`: jobs `jobs/get`, SQL queries/alerts by id, dashboards, genie, pipelines, serving).

### 3.3 Logger module (`src/utils/logger.py`, rewritten)
Reuse from `plan13-backlog-b1-b13:src/utils/logger.py`: `_ContextFilter`, `_RedactFilter`,
`register_secret`, `set_context`, `get_log`, the stdout handler that resolves `sys.stdout` per record
(`_LiveStdoutHandler`, keep it; it costs nothing), the `configure_logging` shape (minus capture
buffer/`pin_stdout`). **Delete** (both the branch's and `main`'s): `set_log_file`, `flush_log_file`,
`log_file_paths`, the local-then-copy mirror, JSON records, `_MIRROR_EVERY`, the capture `StringIO`,
`pin_stdout`.

- App logger `wsmig` at level DEBUG, `propagate=False`; two handlers: **cell** (stdout, level =
  `log_level`, plus the per-stage cap filter for WARNING/ERROR lines) and **driver** (`sys.__stderr__`,
  level DEBUG). Both get the context + redaction filters (secrets are not redacted by Databricks in
  driver logs, so ours must be).
- `StructuredLogger.info/warning/error/debug(msg, **fields)` stays the call-site API (98 call sites
  unchanged), a thin wrapper over `wsmig`; it must NOT apply its own threshold (branch bug).
- Line format: `%(asctime)s #%(seq)d %(levelname)-5s [%(name)s] run=%(run_id)s stage=%(stage)s | msg  k=v…`
  (`seq` = process-wide counter added by the context filter).
- `progress(phase, done, total, **counts)` helper: emits the INFO progress line when `done % 500 == 0`
  or `done == total` (the 500 is a module constant `PROGRESS_EVERY`). No timers, no threads.
- `configure_logging(run_id, stage, level)` is called in each stage notebook **exactly where
  `_logger.set_log_file(...)` is today** (`01_Inventory.py:144`, `02_Export.py:133`, `04_Import.py:189`),
  so every later cell (bundle summary, state table, preflight, the run, the summaries) logs through it.
  `run_id`/`stage` come from `cfg`. Idempotent (replaces only its own handlers).
- `live_run(stage)` context manager wraps ONLY the main work call (`InventoryRunner(...).run()`,
  `ExportRunner(...).run()`, `runner.run()`): logs `Stage <X> started` / `Stage <X> finished (status,
  elapsed)`, on an exception logs it at ERROR with traceback and re-raises. It resets the per-stage cell cap.
- `seq` counter: `itertools.count()` (atomic under the GIL), shared by both handlers so the same record
  carries the same `#n` in the cell and the driver log.
- **API call line (DEBUG, `[api]`, in `ApiClient._request`):** method, path, query params, status, ms;
  on a 4xx/5xx also the server `error_code` + `message` (already folded into `HTTPStatusError` on main).
  **Never** request/response bodies or headers. The OAuth M2M token mint logs only `token minted
  (expires in Ns)`. `with_retry` logs WARNING `retrying <method> <path> after <status> in <s>s
  (attempt n/5)`.
- **Notebook `print()` calls:** convert every `print(` in 01/02/04 to the logger at INFO (so the driver
  log is self-contained). The existing summary loops are already capped (`[:40]`, `[:25]`, `[:15]`) and
  stay so; add a `[:200]` cap to the preflight-items loop (`04_Import.py:236-239`, it lists one item per
  missing identity). `00_Install_Jobs` keeps its prints (not a job stage).
- Performance: one formatted line per event to each handler; at DEBUG that's a few µs per line vs
  100+ ms per API call, i.e. negligible. The stderr stream is buffered by the platform.

### 3.4 Step coverage — every stage (files to instrument)
**Inventory** (`notebooks/01_Inventory.py`, `src/collectors/inventory_runner.py`, `base_collector.py`,
each collector):
- runner: start, each collector **start** (today only "discovered" after the fact → a long workspace
  walk is silent), end with count + elapsed + errors; DAB registry; account-group summary; report
  writes; pointer write.
- `base_collector.run`: `Phase: collect <type>` → `discovered N` → per-object enrich start/end (DEBUG)
  + `progress()` → `Phase complete: collect <type> (N, errors, elapsed)`.
- `workspace_collector._walk`: `listing <dir>` → `listed <dir> (N objects)` per directory (DEBUG) +
  `progress()` every 500 directories; budget-truncation WARNING (exists).
- ACL fetch (`fetch_acl`): `fetching ACL <type> <id/path>` → `ACL <type> <id/path> → N grants`;
  failures WARNING (exists).
- identity/jobs/sql/etc. collectors: `Phase:` start/end per collector; a start + end line per item
  wherever a collector makes one GET per object (jobs `jobs/get`, alerts by id, queries by id,
  dashboards, genie).

**Export** (`notebooks/02_Export.py`, `src/exporters/export_runner.py`, `content_fetcher.py`,
`artifact_writer.py`):
- inventory load (path, size, object count); unit build per family (counts); toggles applied; ACL
  collection (grant count); content pass `Phase:` start (to_fetch / resumed / workers) →
  `fetching <path>` / `fetched <path> (size)` per object (DEBUG) + `progress()` → `Phase complete` (fetched, oversize,
  failed, MB); each fetch failure WARNING with path + error; checkpoint flushes
  (batch n); oversize table; manifest build (files, bytes) + write; pointer write.

**Import** (`notebooks/04_Import.py`, `src/importers/import_runner.py`, `base_importer.py`,
`preflight.py`, each importer, `src/reports/import_report.py`):
- run_id resolution; bundle summary; manifest verify (files checked, mismatches); state ensure/load
  (rows, duration); recovery replay (rows); each preflight check (name → GO/NO-GO/WARN + detail).
- per phase: `Phase: import <family> — N units` → **existence check** start + `checking <path>` per
  probe at DEBUG + `progress()` + end with counts (today silent; the QA-2 "20-minute silent stall") →
  start + end line per object (DEBUG; failures ERROR) + `progress()` (§3.2) → `Phase complete` counts +
  elapsed → state flush start/end.
- identity: pass-1 / pass-2 membership start + end; account-identity assignment results.
- deleted-in-source summary; report build (rows, sheets) + write path + bytes; final totals.

**Shared:** `ApiClient` (DEBUG per call: method, path, status, ms, error body on failure; WARNING per
retry with status + wait); `retry.with_retry` (WARNING before each sleep); `StateStore` (load/flush/merge
durations, row counts).

### 3.5 Silent-exception audit (51 handlers on `main` that `pass`/`return`/`continue`)
Go through every `except Exception` in `src/` and put it in one of three buckets, with a one-line
comment explaining why:
- **Expected + harmless** (e.g. a 404 meaning "not published", an optional enrichment) → keep the
  behaviour, add a DEBUG line.
- **Degraded result the run continues with** (e.g. a GET-by-id enrichment fails so a field is missing)
  → keep the behaviour, add a WARNING with object + error, so the gap is visible.
- **A read the run depends on** → in THIS wave only the **state load** changes behaviour (§4). Other
  dependency reads that silently fall back today (e.g. `ExportRunner._load_inventory` re-running a full
  inventory when `inventory.json` is absent, `aw.read_json(...) or {}` on bundle files in `04_Import`)
  get an ERROR/WARNING line naming the file and the fallback taken, but their behaviour is UNCHANGED
  (changing it is PLAN_12 work, out of scope here, rule: don't break `main`).
Files with the most handlers: `misc_collector`, `preflight`, `base_importer`, `bundle_state`,
`workspace_collector`, `sql_collector`, `secrets_collector`, `dab_registry`, `sql_backend`.
Each changed handler keeps its exact control flow unless it is in the third bucket.

### 3.6 Remove the execution log files
- `notebooks/01_Inventory.py:144,161`, `02_Export.py:133,163-164`, `04_Import.py:189,347,364`: remove
  `set_log_file` / `flush_log_file` and the `EXECUTION_IMPORT_LOG` entry in the file list.
- `src/exporters/bundle_paths.py:45-47`: keep the three constants (old bundles in the wild contain these
  files) but nothing writes them. `artifact_writer._excluded_from_manifest` keeps excluding them, so an
  old bundle still verifies.
- Tests: `tests/test_export.py:903-971`, `tests/test_import_framework.py:484`,
  `tests/run_against_fvm2.py:53` updated accordingly.

### 3.7 Notebook-side rules
- `dbutils.notebook.exit` (if ever added) goes alone in a trailing cell (it suppresses that cell's
  streamed output in a job run).
- `log_level` widget (default `INFO`; controls the CELL only — the driver log is always DEBUG) on
  01/02/04, projected by `00_Install_Jobs` into every task (the branch's QA-1 lesson: a widget the jobs
  don't pass is unreachable).
- Each stage notebook prints, at the end of its run cell, one line telling the operator where the
  full log is: `Full DEBUG log: this run → Compute → Driver logs → Standard error (kept 30 days).`

---

## 4. Part B — State-store hardening

### 4.0 Step 0 — DONE 2026-10-04 (no further investigation needed)
Reproduction attempt on **classic** (DBR 15.4 LTS, `target_ws`, probe job `890117039318697` run
`1101394810113629`): the exact `main` load (`spark.sql("SELECT * FROM <state> WHERE source_workspace_id =
'…'")` → `[row.asDict() for row in df.collect()]`) against the real 21,276-row QA state table (~12 MB)
returned **all 21,276 rows** on a dedicated cluster (1.1 s) and on a standard-access cluster (1.8 s), no
error. The branch's "loaded 0 rows" incident happened on **serverless** (where that QA ran), which is
out of scope (classic only). We still ship the fixes below: `main` has the same error-swallowing code,
and any future read failure would otherwise silently look like an empty table again.

### 4.1 `src/state/sql_backend.py`
- Split the interface: `execute(stmt)` for DDL/DML (result ignored; errors **raise**) and
  `query(stmt) -> list[dict]` for SELECT (errors **raise**). `SparkSqlBackend.query` =
  `[r.asDict() for r in spark.sql(stmt).collect()]` with **no** try/except. `execute` = `spark.sql(stmt)`
  with no try/except. Keep `sql()` as an alias of `execute` so existing DDL call sites keep their shape;
  the two SELECT sites in `state_store.load()` switch to `query()`.
- `StatementApiBackend`: **out of scope.** It is only used by the laptop test harness (no SQL warehouse
  is ever asked of the customer; jobs always use Spark on the job cluster). Leave it unchanged.

### 4.2 `src/state/state_store.py`
- **Count check on every load (user design 2026-10-04).** `load()` does, for each of the two tables
  (migration state, identity map), filtered by `source_workspace_id`:
  1. `SELECT count(*) AS n FROM <table> WHERE source_workspace_id = '<id>'` → `expected`;
  2. `SELECT * FROM <table> WHERE source_workspace_id = '<id>'` → `rows`;
  3. if `len(rows) != expected` → raise `StateLoadError("state table <fqn> has <expected> rows for
     source workspace <id> but only <len(rows)> were loaded — refusing to continue: an incomplete state
     load would re-create/adopt objects and silently drop source edits. Check the table / permissions /
     cluster.")`;
  4. log `state loaded | table=<fqn> rows=<n> expected=<n> (<ms> ms)` at INFO.
  `expected == 0` and 0 rows loaded = a genuine first run → continue as today.
  **First run: the tables don't exist yet.** On `main`, `ensure_table()` (`CREATE TABLE IF NOT EXISTS`
  for both tables + the `last_source_detail` `ADD COLUMNS`) already runs before each of the three
  explicit loads (`04_Import` cell, `preflight.check_state_table`, `ImportRunner.run`), so the count
  runs against an existing (empty) table → 0/0 → continue. To make this hold on EVERY path (the lazy
  `self.load()` calls inside `retry_keys`/`target_ids_for`/`has_family`/…, tests, harnesses), `load()`
  itself calls `ensure_table()` once first (an `_ensured` flag, so no extra statements after the first).
  A missing catalog/schema or missing privileges still fails loud in `ensure_table()` with its existing
  "create the schema / grant USE CATALOG…" message; the count never runs against a table that isn't
  there. The dry-run twin (`…_dryrun`) follows the same path. When the state store is disabled (dry run
  with no `state_catalog`), `load()` returns empty exactly as today and nothing is created or counted. Safe because nothing
  writes the table between the count and the select: all three loads (notebook cell, preflight, runner)
  happen before the first phase writes anything. The cost is one extra `count(*)` per load (well under
  a second for 21K rows). Applies to dry runs too (they read the `_dryrun` twin the same way).
- Any `query()` failure in `load()` raises `StateLoadError` (table name + underlying error) instead of
  returning empty. The notebook cell, preflight (`check_state_table`) and runner all surface it as a hard
  stop (preflight records it BLOCKING as today); no change to how many times we load.
- **Non-destructive MERGE** (`_merge_state`): for `target_object_id`, `source_object_id`,
  `last_source_fingerprint`, `first_seen` use `t.c = COALESCE(NULLIF(s.c, ''), t.c)`; all other columns
  as today. A blank value can never wipe a stored id/fingerprint (the corruption the branch's QA run
  caused). `record()` already carries prior values forward from the cache, so this only matters when
  the cache is wrong, which is exactly the case it guards against.
  Same for `_merge_identity` on `target_id`, `source_id`.
- `flush()` keeps its "never raises in `finally`" contract, but: logs rows + duration on success; on
  failure it still logs ERROR **and** sets `self.flush_failed=True`. The runner puts that into
  `run_status=completed_state_not_saved` and the notebook's last cell raises, so the job goes red.
- `_cache` iteration helpers (`target_ids_for`, `retry_keys`, `has_family`, `outstanding_rows`,
  `summary`) iterate `list(self._cache.items())` (no behaviour change; prepares 16.5).

### 4.3 Not in this wave
Export reading the state table (16.6); the dry-run twin design (unchanged).

---

## 5. Files touched (summary)
`src/utils/logger.py` (rewrite), `src/auth/token_manager.py` (DEBUG/WARNING lines, `register_secret`), `src/utils/retry.py`, `src/state/sql_backend.py`, `src/state/state_store.py`,
`src/collectors/{base_collector,inventory_runner,workspace_collector,…}.py` (start/end lines),
`src/exporters/{export_runner,content_fetcher,artifact_writer}.py`,
`src/importers/{base_importer,import_runner,preflight,identity_importer,workspace_importer}.py`,
`src/reports/import_report.py`, the three stage notebooks + `00_Install_Jobs.py` + `jobs/*.job.json`
(`log_level`), the silent-except files in §3.5, tests. Plus copied as-is from the branch for QA:
`tests/fixtures_fvm1.py` (`plans/qa-testing-agent.md` is already copied over and updated).

---

## 6. Live verification tasks — DONE 2026-10-04
All step-0 research is complete (results in §3.0, §4.0, §10). Nothing remains to verify before coding.

---

## 7. Offline tests (new, in `tests/test_plan16_1.py`)
- logger: format has run/stage/module; redaction; `propagate=False` (no doubles); level from
  `configure_logging`, not `StructuredLogger`; no file is ever written (`tmp` dir stays empty).
- `live_run`: logs stage start/finish; on an exception logs it with traceback and re-raises; starts no
  thread.
- two handlers: cell handler level = `log_level` (default INFO), driver handler always DEBUG; a DEBUG line
  reaches only the driver stream, an ERROR reaches both; `seq` increases by 1 per record.
- cell cap: after 2,000 WARNING/ERROR lines in a stage the cell gets one "further failures…" line and no
  more failure lines; the driver stream still gets all of them; `live_run` resets the cap.
- `progress()`: emits at every 500th item and at the last one, not otherwise.
- every importer / collector / export step: a fake run asserts (driver stream) a `Phase:` + `Phase
  complete:` pair and, per object, an `importing …`/`fetching …` line followed by exactly one outcome
  line for the same object (also for failed and skipped objects); (cell stream) phase lines + progress
  lines + failures only.
- no secret/token in any line of either stream; no file is written.
- `SparkSqlBackend.query` raises on a failing `collect` (fake spark); `execute` raises on DDL error.
- `StateStore.load` count check: fake backend returning count=21,276 but 0 rows (and 16,355 rows) →
  `StateLoadError` naming both numbers; count=0/rows=0 → loads fine (first run); count=N/rows=N → loads;
  same for the identity table; a raising `query()` → `StateLoadError`, never an empty cache.
- First run: fake backend where the tables don't exist until `CREATE TABLE IF NOT EXISTS` runs → `load()`
  (called directly, without a prior `ensure_table()`) creates both tables, counts 0, loads 0, no error;
  a second `load()` issues no further DDL. State disabled → no DDL, no count, empty cache.
- MERGE SQL contains the COALESCE for the four columns; a blank `target_object_id` in a batch does not
  blank the stored value (backend fake that applies the MERGE semantics).
- flush failure → `run_status=completed_state_not_saved`.
- Regression: the full existing suite passes unchanged except the deliberately-updated log-file tests.
- A synthetic 25K-row state generator (`tests/gen_state_rows.py`) for the live QA load test.

## 8. Live QA (Claude, after the user pushes the branch)
All runs on a **classic job cluster** (no serverless runs).
1. **Wave 0 golden baseline on `main`** (master plan) if not already done.
2. Branch at the pushed commit: dry-run job → live job → seed incremental edits → live job →
   `retry_mode=failed_only`.
3. **Golden diff:** per-asset-type status counts identical to Wave 0 (expected diff: none).
4. **Output review**, for each of inventory/export/import:
   - cell: every phase shows `Phase:` + `Phase complete:` and progress lines every 500; the run exports
     (`jobs export-run` succeeds, i.e. cell output well under the limits);
   - driver log `stderr` (downloaded): every object has a start line and exactly one outcome line (script
     check after de-duplicating by `#seq`);
   - every failure row in `import_status.xlsx` has a matching ERROR/WARNING line with the same object
     and error.
5. **Stuck-run drill:** make one object slow/blocked (e.g. a very large file) and confirm the cell names
   the current phase and the driver log's last line names that object and the API call in flight.
6. **Root-cause drill:** deliberately break a few objects (missing warehouse, withheld permission, a bad
   policy); give Claude ONLY the downloaded driver log and confirm each root cause is identifiable from it.
7. **Fail-loud drill:**
   - revoke SELECT on the state table → the run stops at state load with a clear error (not 0 rows);
   - first run on a fresh pair (tables absent, e.g. a new `state_schema`): tables are created and the
     log shows `rows=0 expected=0`, run continues normally;
   - count check: confirm the `state loaded | rows=N expected=N` line on every load; force a mismatch
     (e.g. a test-only hook / fake that drops rows) → the run stops with the count error before any phase;
   - synthetic 25K-row state table → loads in full, with the row count logged.
8. `misc/` contains no `execution_*.log`; old bundles (from `main`) still verify and import.
9. **Scale check:** a synthetic run that logs ≥300K objects (fixture or a test-only loop) passes and stays
   exportable; the driver log has every object.

## 9. Decisions (user, 2026-10-04) — all settled (nothing left to research or decide for dev)
1. **Cell = INFO by default** (`log_level` widget): phases, progress every 500 objects, warnings and
   errors (capped at 2,000 per stage). **Driver log `stderr` = always DEBUG**: per-object start + success
   lines for every object, every API call, everything. The customer downloads Driver logs → Standard
   error to share.
2. **No heartbeat**, no `pin_stdout`, no cluster log delivery (driver logs are kept 30 days), no log files.
3. **State load = `count(*)` + `SELECT *`, stop on mismatch** (§4.2), with `load()` creating the tables
   on a first run (§4.2).
4. Behaviour changes in this wave are limited to: state load fails loud / count check, non-destructive
   MERGE, failed save turns the job red, no `execution_*.log`. Everything else is logging only.

**Prerequisites for LIVE QA (not for dev):** the Wave 0 golden baseline run of `main` (master plan), and
the user's answer on the QA fixture identity question (workspace-local groups/SPs in the source bed, or
account-level only per the 2026-10-03 decision; see `plans/qa-testing-agent.md`).

## 10. Findings during 16.1
- **2026-10-04 state read on classic (§4.0):** full 21,276/21,276 rows on dedicated (1.1 s) and standard
  (1.8 s) access mode, DBR 15.4 LTS. The branch's 0-row load does not reproduce on classic; it was a
  serverless-only occurrence. Fixes in §4.1–4.2 still ship.
- **2026-10-04 Databricks docs (Azure):** job notebook output limit = **8 MB per cell, 30 MB total;
  exceeding either cancels the run and marks it failed**. Driver logs (stdout/stderr/log4j) are
  downloadable from the run's compute page for **30 days** after the job cluster terminates; viewable by
  CAN MANAGE on the job compute by default; Databricks does not redact secrets in them (our redaction
  filter covers both handlers). → cluster log delivery NOT needed.
- **2026-10-04 output probes (classic):** see the §3.0 table (30 MB cell limit fails the run; stdout is
  mirrored to driver stdout; `sys.__stderr__` is uncapped, not counted, kept fully in driver stderr, each
  line twice; worker-thread lines reach the cell). Probe artifacts left on `target_ws`: jobs
  `890117039318697`, `146251838020838`, notebooks under `/Users/abhishek.iyer@databricks.com/wsmig_step0/`,
  logs under `/Volumes/catalog_ws2_0cyzw9/wsmig_test/staging/plan16_step0/` (safe to delete).
