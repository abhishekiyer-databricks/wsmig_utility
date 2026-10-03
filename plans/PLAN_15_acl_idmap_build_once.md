# PLAN 15 — ACL / id-map "build-once" lookup (optional scale optimization)

**Status:** PROPOSED (optimization, not a correctness fix). Split out of QA-6 (PLAN_13) on 2026-10-03.
**Note:** renamed from PLAN_14 → PLAN_15 on 2026-10-03; PLAN_14 is now the incremental correctness/state-load fix plan (QA-4/5/6/7).
**Relationship:** QA-6's one-line `list()` snapshot is the CORRECTNESS fix (ships first, cheap, stops the
parallel crash). THIS plan is the PERFORMANCE fix that also happens to remove the race surface entirely.
Do QA-6 first; this is a follow-up only if ACL-phase runtime at customer scale matters.

## Problem
`state_store.target_ids_for(asset_type)` (`state_store.py:620`) builds `{natural_key: target_id}` by
**scanning the ENTIRE `_cache`** (every asset type, every row) and filtering to one `asset_type`. It is
called via `base_importer.target_id_map` on the **per-unit hot path** of the ACL importer
(`AclImporter.create_one → _resolve_target_object → target_id_map → target_ids_for`). So every single
ACL unit triggers a full O(cache-size) rescan.

Two consequences:
1. **Cost:** O(N) per unit × M units = **O(N·M)**. At RRL scale (`_cache` ≈ 1.1M rows, ≈ 850K ACL
   units) this is a dominant contributor to the ~11h ACL-phase runtime already noted in CLAUDE.md /
   [[rrl-dry-run-rca-empty-bundle]]. Re-reading the same million-row dict hundreds of thousands of times.
2. **Race surface:** because the rescan runs on parallel ACL workers (B6, `parallel_threads>1`) while
   other workers write `_cache` via `record()`, it is also the exact site of **QA-6**
   ("dictionary changed size during iteration"). QA-6's `list()` snapshot stops the crash but still pays
   the O(N·M) rescan cost (now ~2× the constant factor, since it also copies).

## Proposed optimization — build the id-map ONCE per phase
Compute each asset_type's `{natural_key: target_id}` lookup **once, up front** (at ACL-phase start, or
lazily memoised on first use per asset_type), then have every ACL unit do an **O(1) dict lookup** instead
of an O(N) rescan.

Sketch (dev to design properly — tester-authored sketch):
- Add a phase-scoped cache on the importer/state: `self._idmap_by_type: dict[str, dict]`, built from a
  SINGLE pass over `_cache` that buckets rows by `asset_type` → `{at: {nk: target_id}}`.
- Build it after the create/update phases have finished writing `_cache` and BEFORE the ACL phase starts
  (ACL is last in the dependency order, so `_cache` is complete by then — the whole premise of resolving
  ACLs against earlier-created ids). `target_id_map`/`target_ids_for` read from `_idmap_by_type[at]`.
- Still merge the in-session `context[f"{at}_target_ids"]` overlay (as `target_id_map` does today).
- Because the map is built once and only read (not rebuilt) during the parallel ACL phase, there is **no
  concurrent write to the structure being iterated → the QA-6 race cannot occur** even without the
  snapshot. (Keep the QA-6 snapshot anyway as defence-in-depth for any other parallel phase.)

## Expected gains
- ACL id resolution drops from **O(N) per unit → O(1) per unit**; the single build pass is O(N) once.
  At RRL scale this turns ~850K full-million-row rescans into one build + 850K hash lookups — a large cut
  in ACL-phase wall time (complements PLAN_12's ACL-enrichment parallelization; different bottleneck).
- Removes the QA-6 race surface structurally (no live-dict iteration on the hot parallel path).

## Scope / non-goals
- IN: `target_ids_for` / `target_id_map` resolution path and its callers (ACL importer + any folder-placed
  importer that remaps by state id). Preserve exact semantics (same `{nk: target_id}`, same in-session
  overlay, same "only non-empty target ids" filter).
- OUT: the state table schema, fingerprint logic, the DBFS/FUSE durability work (PLAN_12), the ACL
  enrichment parallelization (PLAN_12). This is purely the lookup data-structure.
- Must stay correct for **phase-at-a-time** runs (the map must include earlier-session rows from the state
  table, exactly as `target_id_map` merges today).

## Risks
- **Staleness within a run:** if any id is written to `_cache` AFTER the map is built but needs to be
  resolved by a later ACL unit, the pre-built map would miss it. Mitigation: build the map at ACL-phase
  start (ACL is the LAST phase; all creates are done), and/or fold the in-session `context` overlay on
  read (which already carries this-run creations). Verify no phase writes new remappable ids during ACL.
- Memory: one extra `{at: {nk: id}}` structure (~same footprint as the data already in `_cache`). Fine at
  the driver sizes migrations use (64–128 GB per PLAN_12 note).

## Test plan
- Unit: build-once map equals the old per-call `target_ids_for` output for every asset_type (property
  test over a seeded `_cache` + context overlay). Phase-at-a-time: map includes prior-session rows.
- Regression: QA-6 scenario (`parallel_threads=4`, ACL phase over jobs/clusters/warehouses/DLT/dashboards/
  genie/alertsv2/pools/policies) runs with **0** "dictionary changed size" errors AND identical ACL
  parity (`missing_on_target=0`) vs a serial run.
- Scale (optional, in-workspace): time the ACL phase before/after at ≥100K units to confirm the O(N·M)→
  O(N)+O(M) improvement.

See [[plan13-future-backlog]] QA-6 (the snapshot correctness fix), [[plan12-optional-scale-and-durability]]
(ACL enrichment parallelization — a different ACL bottleneck), [[rrl-import-run-rca-2026-09-08]].
