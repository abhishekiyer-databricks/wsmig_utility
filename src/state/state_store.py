"""
state_store — the TARGET-side Delta migration state table (master §9, Plan 3 §7).

WHY THIS EXISTS. The utility migrates the same workspace MANY times: an initial run, then re-runs
weeks later to carry over source changes. Plain skip-if-exists would silently DROP updates to
assets that already exist on target, so every asset is UPSERTed — and that needs durable memory of
**which target object corresponds to which source object**.

Two tables, one shared catalog+schema across all 100+ workspace pairs (D12/D19 — the customer
provides the catalog+schema, the tool owns the table NAMES):

  `wsmig_migration_state`  — one row per migrated object, PK
      `(source_workspace_id, asset_type, natural_key)`, holding BOTH ids + a content fingerprint.
      Storing both ids is the point: source policy "p1" (src id 3) becomes target id 9; when "p1"
      is edited on source, the importer looks the row up by natural_key, reads `target_object_id=9`
      and calls the EDIT api against 9 — updating the right object instead of creating a duplicate.
  `wsmig_identity_map`     — the durable old→new identity map (§7.1). A recreated
      Databricks-managed SP gets a BRAND-NEW applicationId with no visible link back to the source
      appId, so this map CANNOT be rebuilt by re-reading the target. It is the only record, which
      is why it is written per-identity during phase 1 and flushed hardest.

WRITE CADENCE (D2 — batched, not per-object, not end-of-run):
    per object      → outcome appended to an in-memory pending list (+ the checkpoint JSON)
    every 200       → MERGE the batch
    phase end       → MANDATORY flush before the next phase reads the id map
    on any failure  → FLUSH FIRST, so a poison asset can't strand the successes before it
    run end         → final flush in a `finally`, so even an aborted run persists what it did
    run start       → RECOVERY REPLAY: checkpoint outcomes not yet in the table are merged first
Not end-of-run, because a crash at 90% would leave no target ids — that is a *correctness* bug
(lost updates + duplicates), not a performance one. Not per-object, because a Delta commit per
object is hours of bookkeeping. The checkpoint JSON is the per-item durability layer that makes
the batch safe, and the recovery replay is what cashes that in.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from src.utils.helpers import now_iso, safe_str
from src.utils.logger import get_logger

_LOG = get_logger("state_store")

# MERGE this many pending rows at a time (D2). Same rationale as the export checkpoint batch: a
# Delta MERGE is a transaction that rewrites files and adds a log entry, so one per object is
# hours of pure bookkeeping on a large workspace.
STATE_BATCH = 200


class UpsertAction(str, Enum):
    """What the importer should DO with a unit (the decision), distinct from what happened."""
    CREATE = "create"
    UPDATE = "update"
    SKIP = "skip"
    ADOPT = "adopt"
    DELETED_IN_SOURCE = "deleted_in_source"


# `last_action` vocabulary — the values the three retry_mode buckets query (§7d). Kept as a
# closed set so a typo can't create a row no retry mode will ever find.
ACTION_CREATED = "created"
ACTION_CREATED_WITH_WARNING = "created_with_warning"
ACTION_UPDATED = "updated"
ACTION_SKIPPED = "skipped"
ACTION_ADOPTED = "adopted"
ACTION_FAILED = "failed"
ACTION_MANUAL = "manual"
ACTION_NOT_SELECTED = "not_selected"
ACTION_SKIPPED_NO_OBJECT = "skipped_no_object"
ACTION_DELETED_IN_SOURCE = "deleted_in_source"

LAST_ACTIONS = frozenset({
    ACTION_CREATED, ACTION_CREATED_WITH_WARNING, ACTION_UPDATED, ACTION_SKIPPED, ACTION_ADOPTED,
    ACTION_FAILED, ACTION_MANUAL, ACTION_NOT_SELECTED, ACTION_SKIPPED_NO_OBJECT,
    ACTION_DELETED_IN_SOURCE,
})

# The `last_action` values that mean "went wrong / needs a fix" — the cumulative Outstanding view
# (PLAN 11 Finding-4). Scoped to FAILURES ONLY (customer 2026-09-04): the operator wants the sheet
# to hold nothing but things that actually failed. Everything else is by-design or visible
# elsewhere and only added noise: `created_with_warning` (created-but-degraded — still on its own
# per-asset-type tab), `manual` (human steps — AKV scope, repos, secret values; on the Manual table
# + runbook), `skipped_no_object` (declarative ACLs whose object isn't present yet — on the ACL
# sheet), `not_selected` (deferred family), and `deleted_in_source` (its own section).
OUTSTANDING_ACTIONS = frozenset({
    ACTION_FAILED,
})

# Which `last_action` values each retry_mode picks up (D22).
#   • `failed_only` means LITERALLY failed — {ACTION_FAILED} and nothing else (PLAN 8 Bug 2). It used
#     to also fold in `created_with_warning`, but that made the label lie: a warned-but-created unit
#     re-selected under `failed_only` re-attempts, finds itself unchanged, and shows up as
#     `Skipped (unchanged)` in a report the operator expected to hold ONLY outstanding failures.
#   • `created_with_warning` (a genuinely degraded-but-created unit, e.g. a job whose notebook_path
#     was unresolvable) rides `failed_and_skipped` — the "fix the prerequisite, then re-attempt"
#     case — and stays visible in the report/summary, so it is never silently forgotten (PLAN 8
#     Bug 2, leaning a+c). The permanent OAuth-secret SP is no longer modelled as a warning at all —
#     it is `manual` (Bug 3), so it rides skipped_only/failed_and_skipped and never touches
#     failed_only.
#   • `manual` + `not_selected` sit in skipped_only, because a family deferred by `import_assets`
#     or parked as `manual` IS the real "take it up later" case.
RETRY_BUCKETS = {
    "off": frozenset(),
    "failed_only": frozenset({ACTION_FAILED}),
    "skipped_only": frozenset({ACTION_SKIPPED, ACTION_MANUAL, ACTION_NOT_SELECTED,
                               ACTION_SKIPPED_NO_OBJECT}),
    "failed_and_skipped": frozenset({ACTION_FAILED, ACTION_CREATED_WITH_WARNING, ACTION_SKIPPED,
                                     ACTION_MANUAL, ACTION_NOT_SELECTED,
                                     ACTION_SKIPPED_NO_OBJECT}),
}

# `failure_category` — makes "show me every pair blocked on a prerequisite" a SQL query rather
# than a hunt through Excel files (§7, §7d).
CAT_PREREQUISITE_MISSING = "prerequisite_missing"
CAT_API_ERROR = "api_error"
CAT_DEPENDENCY_UNRESOLVED = "dependency_unresolved"
CAT_PERMISSION_DENIED = "permission_denied"
CAT_NOT_SUPPORTED = "not_supported"
# The seven `skipped_no_object` sub-cases for ACL rows (§6b-i) — WHY a grant had no object.
CAT_DAB_REDEPLOY = "dab_redeploy"
CAT_REPO_OUT_OF_SCOPE = "repo_out_of_scope"
CAT_LEGACY_DASHBOARD = "legacy_dashboard"
CAT_UNIT_FAILED_EARLIER = "unit_failed_earlier"
CAT_FAMILY_NOT_SELECTED = "family_not_selected"
CAT_OVERSIZE = "oversize"
CAT_UC_BACKED = "uc_backed"


def _q(value: Any) -> str:
    """SQL string literal, escaping quotes/backslashes. NULL for None/''.

    Values here are workspace object names, paths and API error text — all attacker-irrelevant but
    absolutely capable of containing a quote (a notebook called `Bob's ETL` is normal), so
    escaping is about correctness, not just safety.
    """
    if value is None:
        return "NULL"
    s = safe_str(value)
    if s == "":
        return "NULL"
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


class StateStore:
    """Delta-backed migration state + identity map, with batched writes and recovery replay.

    Deliberately tolerant of being DISABLED (`backend=None`): a first-look `dry_run` rehearsal
    with no state catalog configured needs no UC setup at all, and every method then becomes a
    no-op returning empty results. `Config.validate()` is what refuses `dry_run=false` without a
    catalog, so "disabled" can never silently apply to a real import.
    """

    def __init__(self, backend, config, table_fqn: str = "", identity_table_fqn: str = "") -> None:
        self.backend = backend
        self.config = config
        self.table_fqn = table_fqn or getattr(config, "state_table_fqn", "")
        self.identity_table_fqn = identity_table_fqn or getattr(config, "identity_map_table_fqn", "")
        self.source_ws_id = safe_str(config.source_workspace_id)
        self.run_id = safe_str(config.run_id)
        # in-memory pending batches (flushed per STATE_BATCH / phase boundary / finally)
        self._pending: list[dict] = []
        self._pending_identity: list[dict] = []
        self._cache: dict[tuple, dict] = {}
        self._identity_cache: dict[tuple, dict] = {}
        self._loaded = False
        self.merges = 0                    # flush count, for the report + tests

    @property
    def enabled(self) -> bool:
        return bool(self.backend and self.table_fqn)

    # ── DDL ───────────────────────────────────────────────────────────────
    def ensure_table(self) -> None:
        """Create BOTH tables if absent. NEVER creates the catalog or schema (D19).

        Silently creating a catalog in someone's UC isn't the tool's business, and on these
        workspaces `CREATE CATALOG` fails anyway (default-storage metastore). So a missing
        catalog/schema is a fail-fast with the exact statement to run.
        """
        if not self.enabled:
            return
        try:
            self.backend.sql(f"""
                CREATE TABLE IF NOT EXISTS {self.table_fqn} (
                    source_workspace_id     STRING,
                    asset_type              STRING,
                    natural_key             STRING,
                    source_object_id        STRING,
                    target_object_id        STRING,
                    last_source_fingerprint STRING,
                    last_action             STRING,
                    last_error              STRING,
                    last_error_raw          STRING,
                    failure_category        STRING,
                    last_run_id             STRING,
                    connectivity_mode       STRING,
                    tool_version            STRING,
                    last_source_detail      STRING,
                    first_seen          STRING,
                    last_seen           STRING
                ) USING DELTA
                CLUSTER BY (asset_type)
            """)
            # PLAN 8 Bug 5: `last_source_detail` (JSON snapshot of the last source members/
            # entitlements/roles) is new, so a control table created by an earlier tool version
            # lacks it — CREATE TABLE IF NOT EXISTS won't add it. Databricks SQL has NO
            # `ADD COLUMNS IF NOT EXISTS` for columns (it is a PARSE_SYNTAX_ERROR — verified live on
            # target_ws 2026-08-18), so idempotency is by swallowing the FIELD_ALREADY_EXISTS error
            # on a re-run. ADD COLUMNS only appends metadata — it never drops or rewrites data — so
            # it is safe on the live customer table.
            try:
                self.backend.sql(
                    f"ALTER TABLE {self.table_fqn} ADD COLUMNS (last_source_detail STRING)")
            except Exception as exc:  # noqa: BLE001
                if "already exists" not in str(exc).lower():
                    raise
            self.backend.sql(f"""
                CREATE TABLE IF NOT EXISTS {self.identity_table_fqn} (
                    source_workspace_id STRING,
                    entity_type         STRING,
                    source_key          STRING,
                    source_id           STRING,
                    target_key          STRING,
                    target_id           STRING,
                    classification      STRING,
                    action              STRING,
                    last_run_id         STRING,
                    first_seen      STRING,
                    last_seen       STRING
                ) USING DELTA
            """)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Could not create the migration state tables in "
                f"{self.config.imports.state_catalog}.{self.config.imports.state_schema}. The "
                f"catalog + schema must ALREADY EXIST and the migration identity needs "
                f"USE CATALOG + USE SCHEMA + CREATE TABLE + SELECT/MODIFY on them. Run:\n"
                f"  CREATE SCHEMA IF NOT EXISTS {self.config.imports.state_catalog}."
                f"{self.config.imports.state_schema};\n"
                f"Underlying error: {exc}") from exc
        _LOG.info("state tables ready", state=self.table_fqn, identity=self.identity_table_fqn)

    # ── load (every read filtered by source_workspace_id — asserted in tests) ────
    def load(self, force: bool = False) -> dict:
        """Load THIS workspace pair's rows into memory. Returns {(asset_type, natural_key): row}.

        One table serves all 100+ pairs, so every read is filtered by `source_workspace_id` —
        without that filter a re-run of pair A would see pair B's target ids and try to edit
        another workspace's objects.
        """
        if not self.enabled:
            return {}
        if self._loaded and not force:
            return self._cache
        rows = self.backend.sql(
            f"SELECT * FROM {self.table_fqn} WHERE source_workspace_id = {_q(self.source_ws_id)}")
        self._cache = {(safe_str(r.get("asset_type")), safe_str(r.get("natural_key"))): r
                       for r in rows}
        idrows = self.backend.sql(
            f"SELECT * FROM {self.identity_table_fqn} "
            f"WHERE source_workspace_id = {_q(self.source_ws_id)}")
        self._identity_cache = {(safe_str(r.get("entity_type")), safe_str(r.get("source_key"))): r
                                for r in idrows}
        self._loaded = True
        _LOG.info("state loaded", rows=len(self._cache), identity_rows=len(self._identity_cache),
                  source_workspace_id=self.source_ws_id)
        return self._cache

    def row(self, asset_type: str, natural_key: str) -> Optional[dict]:
        """The state row for one unit (from the in-memory view, including pending writes)."""
        return self._cache.get((safe_str(asset_type), safe_str(natural_key)))

    def get_target_id(self, asset_type: str, natural_key: str) -> Optional[str]:
        """The stored `target_object_id` — what the UPDATE path calls the edit API against."""
        r = self.row(asset_type, natural_key)
        tid = safe_str((r or {}).get("target_object_id"))
        return tid or None

    # ── the decision (master §9 table) ────────────────────────────────────
    def decide(self, asset_type: str, natural_key: str, fingerprint: str,
               exists_on_target: bool) -> UpsertAction:
        """The upsert decision for one unit.

            no row + not on target      → CREATE
            no row + already on target  → ADOPT   (record it, then compare fingerprints)
            row + unchanged fingerprint → SKIP
            row + changed fingerprint   → UPDATE against the stored target_object_id
            row but gone from target    → CREATE  (recreate; the recorded id is stale)

        ADOPT is what stops the tool duplicating an object that exists because someone made it by
        hand, or because a previous attempt died between the API call and the bookkeeping write.
        """
        r = self.row(asset_type, natural_key)
        if r is None:
            return UpsertAction.ADOPT if exists_on_target else UpsertAction.CREATE
        if not exists_on_target:
            # The row says we made it, but it isn't there now (deleted on target, or the recorded
            # id is stale). Recreating is right; SKIPping would leave a permanent hole.
            return UpsertAction.CREATE
        if safe_str(r.get("last_source_fingerprint")) == safe_str(fingerprint) and fingerprint:
            return UpsertAction.SKIP
        return UpsertAction.UPDATE

    # ── recording (batched) ───────────────────────────────────────────────
    def record(self, asset_type: str, natural_key: str, *, action: str, fingerprint: str = "",
               source_object_id: str = "", target_object_id: str = "", error: str = "",
               error_raw: str = "", failure_category: str = "", source_detail: str = "") -> None:
        """Queue one outcome. Flushed per STATE_BATCH / phase boundary / finally (D2).

        A FAILURE forces a flush: the successes queued BEFORE a poison asset are exactly what must
        not be lost, so a failure never strands the 199 rows in front of it.
        """
        if not self.enabled:
            return
        if action not in LAST_ACTIONS:
            # A typo here would create a row no retry_mode bucket ever finds — i.e. invisible
            # outstanding work. Fail loudly in the one place that can catch it.
            raise ValueError(f"unknown last_action {action!r}; valid: {sorted(LAST_ACTIONS)}")
        key = (safe_str(asset_type), safe_str(natural_key))
        prior = self._cache.get(key) or {}
        now = now_iso()
        row = {
            "source_workspace_id": self.source_ws_id,
            "asset_type": key[0],
            "natural_key": key[1],
            "source_object_id": safe_str(source_object_id) or safe_str(
                prior.get("source_object_id")),
            # Never let a later row blank an id we already know: a failed UPDATE must keep the
            # target id, or the next run loses the object and creates a duplicate.
            "target_object_id": safe_str(target_object_id) or safe_str(
                prior.get("target_object_id")),
            "last_source_fingerprint": safe_str(fingerprint) or safe_str(
                prior.get("last_source_fingerprint")),
            "last_action": action,
            "last_error": safe_str(error),
            "last_error_raw": safe_str(error_raw),
            "failure_category": safe_str(failure_category),
            "last_run_id": self.run_id,
            "connectivity_mode": safe_str(getattr(self.config, "connectivity_mode", "")),
            "tool_version": _tool_version(),
            # PLAN 8 Bug 5: JSON snapshot of the last source members/entitlements/roles, so a later
            # run can diff old-source vs new-source and NAME what changed. Carry the prior value
            # forward when a caller omits it (like the ids above), so a non-identity record() for the
            # same key never blanks a snapshot identity wrote.
            "last_source_detail": safe_str(source_detail) or safe_str(
                prior.get("last_source_detail")),
            "first_seen": safe_str(prior.get("first_seen")) or now,
            "last_seen": now,
        }
        self._cache[key] = row
        self._pending.append(row)
        if action == ACTION_FAILED or len(self._pending) >= STATE_BATCH:
            self.flush()

    def record_identity(self, entity_type: str, source_key: str, *, target_id: str,
                        target_key: str = "", source_id: str = "", classification: str = "",
                        action: str = "") -> None:
        """Queue one identity mapping — written during phase 1, per identity (§7.1).

        EVERY identity the run touches gets a row, including ones it deliberately does NOT create
        (account/UMI SPs, built-in groups, users): the TARGET-side SCIM id differs even when the
        natural key doesn't, and ACL/permission calls need that target id. An adopt-without-
        recording would leave every later principal remap unable to resolve.
        """
        if not self.enabled:
            return
        key = (safe_str(entity_type), safe_str(source_key))
        prior = self._identity_cache.get(key) or {}
        now = now_iso()
        row = {
            "source_workspace_id": self.source_ws_id,
            "entity_type": key[0],
            "source_key": key[1],
            "source_id": safe_str(source_id) or safe_str(prior.get("source_id")),
            "target_key": safe_str(target_key) or key[1],
            "target_id": safe_str(target_id) or safe_str(prior.get("target_id")),
            "classification": safe_str(classification) or safe_str(prior.get("classification")),
            "action": safe_str(action),
            "last_run_id": self.run_id,
            "first_seen": safe_str(prior.get("first_seen")) or now,
            "last_seen": now,
        }
        self._identity_cache[key] = row
        self._pending_identity.append(row)
        if len(self._pending_identity) >= STATE_BATCH:
            self.flush()

    # ── flush (MERGE on the PK) ───────────────────────────────────────────
    def flush(self) -> int:
        """MERGE both pending batches. Returns rows written. Idempotent when nothing is pending.

        Never raises: the caller is often a `finally` during an abort, and a bookkeeping failure
        must not replace the real error. It logs loudly instead, and the checkpoint's recovery
        replay is what recovers the rows on the next run.
        """
        if not self.enabled:
            return 0
        written = 0
        try:
            if self._pending:
                self._merge_state(self._pending)
                written += len(self._pending)
                self._pending = []
            if self._pending_identity:
                self._merge_identity(self._pending_identity)
                written += len(self._pending_identity)
                self._pending_identity = []
            if written:
                self.merges += 1
        except Exception as exc:  # noqa: BLE001
            _LOG.error("state flush FAILED — rows stay pending; the checkpoint recovery replay "
                       "will re-merge them on the next run", error=str(exc),
                       pending=len(self._pending) + len(self._pending_identity))
        return written

    _STATE_COLS = ("source_workspace_id", "asset_type", "natural_key", "source_object_id",
                   "target_object_id", "last_source_fingerprint", "last_action", "last_error",
                   "last_error_raw", "failure_category", "last_run_id", "connectivity_mode",
                   "tool_version", "last_source_detail", "first_seen", "last_seen")

    _IDENTITY_COLS = ("source_workspace_id", "entity_type", "source_key", "source_id",
                      "target_key", "target_id", "classification", "action", "last_run_id",
                      "first_seen", "last_seen")

    @staticmethod
    def _values_clause(rows: list[dict], cols: tuple) -> str:
        """`SELECT ... UNION ALL SELECT ...` staging source for the MERGE.

        Built as a SELECT rather than `VALUES` because the Statement Execution API needs named
        columns on the merge source, and both backends accept this shape identically.
        """
        selects = []
        for r in rows:
            fields = ", ".join(f"{_q(r.get(c))} AS {c}" for c in cols)
            selects.append(f"SELECT {fields}")
        return " UNION ALL ".join(selects)

    def _merge_state(self, rows: list[dict]) -> None:
        # De-duplicate within the batch: MERGE fails if the source matches a target row twice, and
        # a unit can legitimately be recorded more than once in a batch (e.g. created, then its
        # ACL pass updates it). Last write wins, which matches the in-memory cache.
        deduped: dict[tuple, dict] = {}
        for r in rows:
            deduped[(r["asset_type"], r["natural_key"])] = r
        src = self._values_clause(list(deduped.values()), self._STATE_COLS)
        sets = ", ".join(f"t.{c} = s.{c}" for c in self._STATE_COLS)
        cols = ", ".join(self._STATE_COLS)
        vals = ", ".join(f"s.{c}" for c in self._STATE_COLS)
        self.backend.sql(f"""
            MERGE INTO {self.table_fqn} t
            USING ({src}) s
              ON  t.source_workspace_id = s.source_workspace_id
              AND t.asset_type          = s.asset_type
              AND t.natural_key         = s.natural_key
            WHEN MATCHED THEN UPDATE SET {sets}
            WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({vals})
        """)

    def _merge_identity(self, rows: list[dict]) -> None:
        deduped: dict[tuple, dict] = {}
        for r in rows:
            deduped[(r["entity_type"], r["source_key"])] = r
        src = self._values_clause(list(deduped.values()), self._IDENTITY_COLS)
        sets = ", ".join(f"t.{c} = s.{c}" for c in self._IDENTITY_COLS)
        cols = ", ".join(self._IDENTITY_COLS)
        vals = ", ".join(f"s.{c}" for c in self._IDENTITY_COLS)
        self.backend.sql(f"""
            MERGE INTO {self.identity_table_fqn} t
            USING ({src}) s
              ON  t.source_workspace_id = s.source_workspace_id
              AND t.entity_type         = s.entity_type
              AND t.source_key          = s.source_key
            WHEN MATCHED THEN UPDATE SET {sets}
            WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({vals})
        """)

    # ── recovery replay (§7a) ─────────────────────────────────────────────
    def recovery_replay(self, checkpoint_outcomes: dict) -> int:
        """Merge checkpoint outcomes that never reached the table, at run START.

        This is what makes the 200-row batch SAFE rather than a 200-row window of loss: the
        checkpoint JSON records the same (natural_key, target_id, fingerprint, status) tuples the
        table needs, so anything lost with an unflushed batch is replayed from there.

        `checkpoint_outcomes` maps "<asset_type>|<natural_key>" → the recorded outcome dict.
        """
        if not self.enabled or not checkpoint_outcomes:
            return 0
        self.load()
        replayed = 0
        for key, out in checkpoint_outcomes.items():
            if not isinstance(out, dict):
                continue
            asset_type, _, natural_key = key.partition("|")
            if not asset_type or not natural_key:
                continue
            existing = self._cache.get((asset_type, natural_key))
            # Already in the table for THIS run → the batch it belonged to was flushed.
            if existing and safe_str(existing.get("last_run_id")) == self.run_id:
                continue
            action = safe_str(out.get("import_status")) or ACTION_CREATED
            if action not in LAST_ACTIONS:
                continue
            self.record(asset_type, natural_key, action=action,
                        fingerprint=safe_str(out.get("fingerprint")),
                        source_object_id=safe_str(out.get("source_id")),
                        target_object_id=safe_str(out.get("target_id")),
                        error=safe_str(out.get("note")))
            replayed += 1
        if replayed:
            self.flush()
            _LOG.info("recovery replay merged unflushed checkpoint outcomes", rows=replayed)
        return replayed

    # ── deleted-in-source detection (D5 — never automatic) ────────────────
    def mark_missing_in_source(self, asset_type: str, present_keys: set) -> list[str]:
        """Rows whose natural_key is no longer in the bundle → `deleted_in_source`, REPORTED ONLY.

        Deletion on target requires the explicit `allow_deletes` opt-in; the default is to tell the
        operator, because auto-deleting a target object on the strength of an absent source key is
        exactly the kind of irreversible action a migration tool must not take by itself.
        """
        if not self.enabled:
            return []
        gone = []
        for (at, nk), r in list(self._cache.items()):
            if at != asset_type or nk in present_keys:
                continue
            if safe_str(r.get("last_action")) == ACTION_DELETED_IN_SOURCE:
                continue   # already reported on an earlier run
            self.record(at, nk, action=ACTION_DELETED_IN_SOURCE,
                        error="present in the migration state table but absent from this bundle — "
                              "deleted on source. NOT deleted on target (set allow_deletes=true "
                              "to opt into deletion).")
            gone.append(nk)
        return gone

    # ── identity map views ────────────────────────────────────────────────
    def load_identity_map(self) -> dict:
        """The durable old→new map, in the shape the importers/ACL remap consume.

        `identity_map.json` in the bundle is the per-run VIEW; this table is the cross-run truth,
        and the JSON is regenerated from it so the two cannot drift.
        """
        if not self.enabled:
            return {"sp_mapping": {}, "group_map": {}, "user_map": {},
                    "scim_ids": {}, "manual_actions": []}
        self.load()
        sp_mapping, group_map, user_map, scim_ids = {}, {}, {}, {}
        for (etype, skey), r in self._identity_cache.items():
            target_key = safe_str(r.get("target_key")) or skey
            target_id = safe_str(r.get("target_id"))
            if etype == "service_principal":
                sp_mapping[skey] = target_key         # old appId → new appId
                scim_ids[f"service_principal:{target_key}"] = target_id
            elif etype == "group":
                group_map[skey] = target_id           # displayName → target group id
                scim_ids[f"group:{skey}"] = target_id
            elif etype == "user":
                user_map[skey] = target_key
                scim_ids[f"user:{skey}"] = target_id
        return {"sp_mapping": sp_mapping, "group_map": group_map, "user_map": user_map,
                "scim_ids": scim_ids, "manual_actions": []}

    # ── retry work list (§7d) ─────────────────────────────────────────────
    def retry_keys(self, retry_mode: str) -> Optional[set]:
        """`{(asset_type, natural_key)}` to attempt for this retry_mode, or None for "everything".

        Retry narrows the WORK LIST only — each selected unit still runs the full upsert decision
        (fingerprint, live existence check, adopt on RESOURCE_ALREADY_EXISTS), so a retry can never
        duplicate an object a previous attempt created but failed to record.
        """
        actions = RETRY_BUCKETS.get(retry_mode, frozenset())
        if not actions:
            return None
        self.load()
        return {k for k, r in self._cache.items()
                if safe_str(r.get("last_action")) in actions}

    def has_family(self, asset_types: tuple) -> bool:
        """Whether ANY row exists for these asset_types — i.e. is this prerequisite satisfied?

        This is what makes phase-at-a-time migration possible (§5): selecting `jobs` alone is fine
        when compute/workspace are already recorded, because their target ids come from here.
        """
        if not self.enabled:
            return False
        self.load()
        wanted = set(asset_types)
        return any(at in wanted and safe_str(r.get("last_action")) not in (ACTION_FAILED,
                                                                          ACTION_NOT_SELECTED)
                   for (at, _nk), r in self._cache.items())

    def target_ids_for(self, asset_type: str) -> dict:
        """`{natural_key: target_object_id}` for one asset_type — the id map a later phase remaps
        against, whether or not that phase ran in THIS session."""
        if not self.enabled:
            return {}
        self.load()
        return {nk: safe_str(r.get("target_object_id"))
                for (at, nk), r in self._cache.items()
                if at == asset_type and safe_str(r.get("target_object_id"))}

    def outstanding_rows(self) -> list:
        """Every row for THIS pair that is NOT yet successfully migrated — the cumulative
        "Outstanding" view (PLAN 11 Finding-4), across ALL runs, independent of whether this run
        touched it. The STATE TABLE is the cumulative source of truth, so a persistent failure that
        stops being re-attempted (a stale row, exactly BUG-1) never silently drops off future reports.

        Includes `failed` / `created_with_warning` / `manual` / `skipped_no_object`. Excludes items
        that are up-to-date (`skipped`/`created`/`updated`/`adopted`) and `deleted_in_source` (its own
        section). Returns the raw state rows (dicts) so the report can render + derive Origin.
        """
        if not self.enabled:
            return []
        self.load()
        return [dict(r) for r in self._cache.values()
                if safe_str(r.get("last_action")) in OUTSTANDING_ACTIONS]

    def summary(self) -> dict:
        """`{last_action: count}` for this pair — the change report's raw material."""
        out: dict = {}
        for r in self._cache.values():
            a = safe_str(r.get("last_action"))
            out[a] = out.get(a, 0) + 1
        return out


def _tool_version() -> str:
    from src.exporters.artifact_writer import TOOL_VERSION
    return TOOL_VERSION
