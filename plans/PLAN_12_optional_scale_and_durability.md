# PLAN 12 — OPTIONAL scale & durability hardening (large-workspace + networking-restricted sites)

**Status:** OPTIONAL / decide-later. None of this is needed for correctness on a typical workspace
pair. Split out of PLAN 11 on 2026-09-03 because it is a distinct concern (source-side handoff
durability + inventory speed at scale) from PLAN 11's import-side correctness fixes, and because the
originating symptom was **already resolved environmentally** (see below).

**Origin:** the the customer customer-scale run, 2026-09-01→09-02 — the first real
1M-asset / networking-restricted deployment. Formerly PLAN 11 "Finding-6".

---

## ✅ Environmental fix already RESOLVED the blocking symptom (2026-09-03)

The empty-bundle failure was **fixed on the customer workspace by cluster networking config**, not
code. After setting, on the cluster running the migration:
- `http_proxy` / `https_proxy` (the customer's mandated egress proxy), and
- **`no_proxy`** including the UC-Volume backing-storage endpoints —
  `*.azuredatabricks.net,*.databricks.azure.com,169.254.169.254,127.0.0.1`
  **plus `.dfs.core.windows.net,.blob.core.windows.net`** (required here because the staging
  Volume is an **EXTERNAL** ADLS-backed volume — a managed volume would not need the ADLS hosts),

the **export wrote the full bundle successfully** — `export/` + `misc/` populated, `manifest` files > 0.
So the direct-FUSE writes DO persist once the Volume's backing storage is reachable without going
through the egress proxy.

**Consequence:** everything below is now **optional hardening**, not a required fix. It makes the tool
resilient *regardless* of the networking config and turns a silent failure into a loud one, but the
customer is unblocked today via the `no_proxy` setting. Set `no_proxy` as cluster **Environment
variables** (NOT Spark config); check `DESCRIBE VOLUME` to see if the staging Volume is managed
(needs nothing extra) or external (add the two ADLS/blob suffixes).

---

## Background — why the FUSE write failed (kept for context)

the customer direct-mode dry runs produced a bundle where the human reports (`inventory.xlsx`,
`export_status.xlsx`, `*.md`) were present but the **machine handoff was empty** — `export/` empty,
`misc/` empty, `manifest written | files=0`, and `[Errno 11] Resource temporarily unavailable`
(EAGAIN) on the Volume path. Reproduced at **76 MB / 17K items** (not just the first 663 MB / 1.1M
run) → **not size or OOM.** Root cause was the WRITE PATH crossed with the proxy restriction:

- Every machine file is written via **raw `open()` straight to the FUSE-mounted `/Volumes/...` path**
  (`artifact_writer.write_json` line 90, `write_bytes` line 104). FUSE buffers locally and flushes to
  the backing ADLS **asynchronously**; if that flush is throttled/proxied/blocked, the bytes never
  durably land and later ops return EAGAIN. ("Wrote N bytes" in the log is the FUSE layer's own
  write-back accounting, NOT our code — it does not prove durability.)
- The split was exactly on write method, NOT on stage: files that go through
  **`write_text_local_then_copy`** (render to `/tmp`, then byte-copy) — the xlsx reports — **survived**;
  files written by direct `write_json`/`write_bytes` (inventory.json, identity_classification.json,
  config_resolved.json, manifest.json, checkpoint.json, export/acls.json, export_index.json, and ALL
  the exported content bytes) **did not**. Tiny single-shot writes (the `.md`) happened to flush;
  large or streamed (`json.dump indent=2` = thousands of tiny writes) saturated the buffer and hit
  EAGAIN.
- **Databricks Support confirmed the supported pattern** (customer ticket): *"point the migration
  tool's output location off the UC volume (e.g. a workspace/DBFS path), then move the finished bundle
  into the volume afterward. The supported pattern for volumes is to write files on local disk first
  and then copy them in."* — https://learn.microsoft.com/en-us/azure/databricks/volumes/volume-files
  (limitations section). This is exactly what `write_text_local_then_copy` already does for xlsx.

This is why the reports looked complete but export was empty: reports render from in-memory data
through the surviving copy path; the machine JSON never persisted; export's only input is
`inventory.json`, so it read nothing → 0 units → but `export_status.xlsx` (surviving path) still
wrote, truthfully showing "0 of everything." A green-looking report over an empty handoff.

---

## Section A (OPTIONAL) — route EVERY Volume write through the local-first path that already works

Make `write_json`/`write_bytes` follow the same render-local-`/tmp`-then-copy contract that
`write_text_local_then_copy` uses (Support's "supported pattern"). Two sub-options for the copy step,
decide at implementation:
- (a) minimal: build the file in `/tmp`, then chunked `open(dst,"wb")` byte-copy (what the xlsx path
  does today) — already proven to survive on the customer's mount; or
- (b) stronger: `/tmp` render then **`dbutils.fs.cp("file:/tmp/x", "dbfs:/Volumes/…")`** (or the Files
  API `PUT /api/2.0/fs/files/…`) for **synchronous commit** — the copy either succeeds durably or
  errors, instead of FUSE fire-and-forget. (Caveat: Files API traffic uses the workspace REST
  endpoint — if that too is proxied it can hit the same wall, but it FAILS LOUD and is retryable.)
- Consider a config knob to point staging output at a **workspace/DBFS path** and copy the finished
  bundle into the Volume at the end (Support's primary suggestion) — heavier; keep as a fallback.
- Add **post-write durability verification**: after close/copy, re-`stat`/re-open and confirm the size
  matches; raise if not (a failed flush is currently invisible to us).
- Regression test: a `write_json` whose destination stat mismatches the intended bytes must RAISE,
  not return silently.

## Section B (OPTIONAL) — fail loud at every stage boundary; each job does ONLY its own work

**Principle (customer-stated 2026-09-03):** each stage is responsible for its own work and nothing
else. **Export runs only if inventory is present; import runs only if export is present.** A stage
that finds its input missing must **RAISE with an actionable message**, never silently produce an
empty result and never do the previous stage's work.
- **Fail loud on an absent/short handoff.** `export_runner._load_inventory` does
  `inv = read_json(INVENTORY_JSON) or {}` → a missing/garbled `inventory.json` becomes an empty dict
  and exports 0 items silently (the exact 22h-then-nothing the customer failure). Change to: if
  `inventory.json` is missing OR present-but-empty/too-small vs the recorded inventory counts,
  **RAISE with an actionable message** ("inventory.json did not persist to <path> — likely a UC Volume
  FUSE write failure; see PLAN 12") instead of proceeding with `{}`. Same fail-loud check anywhere a
  critical bookkeeping JSON is read back.
- **Do not re-run the entire inventory inside export.** Today export, on not finding `inventory.json`,
  silently **re-scans the whole source** (why the customer spent ~11h in inventory AND ~11h again in export).
  Instead: export must rely on the inventory stage's output and **fail fast** if it isn't durably
  present (a wasted 11h re-scan that also can't persist is pure loss). If a consistency re-run is ever
  desired it must be an **explicit opt-in flag**, not a silent fallback.
- Regression test: `_load_inventory` with a missing/empty `inventory.json` RAISES (does not return
  `{}` and does not trigger a full inventory re-scan).
- **Symmetric IMPORT guard — import runs only if the export bundle is present.** Import already runs
  `verify_manifest()` in preflight (a missing `manifest.json` → NO-GO), but that only blocks when
  `preflight_enforce=true`, AND both `notebooks/04_Import.py:191` and `preflight.py:296` still do
  `read_json(BP.EXPORT_INDEX_JSON) or {}` — the SAME silent-empty antipattern: a missing/short
  `export_index.json` becomes `{}` and import proceeds with 0 units instead of failing. Fix: treat an
  absent/empty export bundle (`manifest.json` missing OR `export_index.json` missing/empty) as a HARD
  FAILURE that RAISES regardless of `preflight_enforce`, with a message pointing at the missing export
  output ("export bundle not found/incomplete at <path> — run export first; import does not re-export").
  Import must NEVER fall back to running export/inventory itself.
- Regression test: preflight/import with a missing or empty `export_index.json`/`manifest.json` RAISES
  (does not return `{}` and does not proceed with zero units), independent of `preflight_enforce`.

## Section C (OPTIONAL) — parallelize the per-object ACL enrichment (the actual ~11h bottleneck at scale)

**This is a SPEED optimization, separate from Sections A/B (which are durability/correctness); it does
not affect what the bundle contains.** At the customer scale the ~11h inventory was NOT compute or memory — it
was **852K serial `GET /api/2.0/permissions/<type>/<id>` calls** in `base_collector.fetch_acl()`
(called once per object during enrich). Each is a ~30–80 ms round-trip with the driver idle in
between; 852K × ~45 ms ≈ 11h of pure waiting with a single request in flight.

**Why it helps despite rate limits + our existing retry/backoff (the counter-intuitive part):** the
bottleneck is LATENCY-bound wall-clock, not the API ceiling. With one request in flight we are
nowhere near the limit — just waiting. Running 8–16 concurrent read requests fills that idle time and
lifts throughput until it approaches the *actual* server limit → realistically **~5–10× on the
enrich-heavy phases** (≈11h → ≈1–2h; sub-linear because you eventually meet the ceiling). Retry/backoff
does NOT speed anything up on its own — it is the **safety net that makes running near the ceiling
safe** (absorbs the occasional 429, honoring `Retry-After`, which `with_retry` already does). So:
serial+retry = slow+safe; **parallel+retry = fast+safe.** Backoff is the enabler, not a substitute.

**Precedent (already in our code AND the reference tools):** Export's content fetch already runs on a
bounded `ThreadPoolExecutor` (`content_fetch_workers`, default 8) with a single-writer checkpoint
(`content_fetcher` + `parallel_map`). `databricks-labs/migrate` exports on a `ThreadPoolExecutor`
(`num_parallel`, ~4) with a thread-safe JSONL writer; `WorkspaceMigration` inherits it. Parallel READS
are the established, blessed pattern — this change just extends it to ACL enrichment.

**Approach:**
- Parallelize **reads only** — the per-object ACL fetch in `enrich` (idempotent GETs, low-risk). Bound
  the pool (8–16; reuse the `content_fetch_workers`-style knob or a new `acl_fetch_workers`).
- **Single-writer rule:** worker threads only FETCH and RETURN the ACL; one thread assembles it onto
  the object and writes state/checkpoint — exactly the `parallel_map`-yields-to-main pattern the
  content fetcher already uses. No shared-mutable writes from workers.
- **Gotcha to fix first:** `base_collector.run()` snapshots `len(client.warnings)` to attribute
  warnings per-collector — that attribution breaks under concurrency (warnings from parallel fetches
  interleave). Make warning capture concurrency-safe (e.g. per-call collection) before parallelizing
  inside a collector.
- Honor `Retry-After` (already done in `with_retry`); keep the pool bounded so we approach but don't
  hammer the limit.
- **Import stays ordered** — dependency order is a correctness constraint, so this is a READ-side /
  inventory-enrich win only, NOT an import change.
- Regression test: enrich of N objects issues N ACL fetches concurrently (bounded by the pool) and
  produces byte-identical assembled output to the serial path; warning attribution stays correct.

**Scope/urgency:** only matters at large scale (thousands+ of objects with ACLs). Like the
cluster-sizing/streaming note, it is a "nice-to-have for huge workspaces," not needed for correctness
on typical pairs.

---

## Relationship to PLAN 11 & the rest
- **Independent of PLAN 11.** PLAN 11 is import-side correctness on a bundle that DID persist; PLAN 12
  is source-side/handoff durability + inventory speed. Sequencing within PLAN 12: Section B (fail-loud)
  is cheap and high-value (turns a silent 22h no-op into an immediate, correct error) and could ship
  first; Section A is the durable write fix; Section C is a pure speed optimization that only bites at
  scale. All three are mutually independent and OPTIONAL pending a decision to implement.
- **Streaming/sharding is explicitly NOT on the roadmap** (kept from the master CLAUDE.md note): it
  would only matter for a 600 MB+/1M-asset workspace on a small (16 GB) cluster, and that memory
  dimension is solved by a larger driver (64–128 GB) since migrations are infrequent. Documented
  last-resort optimization, not planned work.
- Related: [[plan10-incremental-airgap-test]] [[cust-dry-run-rca-empty-bundle]]
  [[uc-volume-file-io-limits]].
