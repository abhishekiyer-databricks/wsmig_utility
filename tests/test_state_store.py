"""Offline tests for the migration state store (Plan 3 §7, §7a, §7d).

This is the component whose failure is worst: losing the source→target id map means the next run
creates DUPLICATES and silently drops UPDATES. So the tests here focus on the decision table, the
write cadence (batch / flush-on-failure / recovery replay), and the source_workspace_id filter that
keeps 100+ workspace pairs from reading each other's target ids.

The SQL is exercised through a tiny in-memory backend that really parses the MERGE — enough to
prove upsert semantics and to catch a malformed statement. The REAL Delta round-trip is covered by
`tests/live_state_store.py` against the target workspace.
"""
from __future__ import annotations

import pytest

from src.config.config_manager import Config
from src.state.state_store import (ACTION_CREATED, ACTION_FAILED, ACTION_MANUAL,
                                   ACTION_NOT_SELECTED, ACTION_SKIPPED, ACTION_UPDATED,
                                   STATE_BATCH, StateStore, UpsertAction)


class FakeBackend:
    """In-memory stand-in that honours the MERGE's PK semantics.

    It doesn't parse SQL generally — it recognises the two MERGE shapes the store emits and
    applies them to dicts keyed by the same PK, which is what makes upsert behaviour testable
    offline. Every statement is recorded so tests can assert on the WHERE clause (the
    source_workspace_id filter) and on flush counts.
    """

    def __init__(self):
        self.statements: list[str] = []
        self.state: dict[tuple, dict] = {}
        self.identity: dict[tuple, dict] = {}
        self.fail_next = False

    def sql(self, statement: str):
        self.statements.append(statement)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated Delta failure")
        s = statement.strip()
        if s.startswith("CREATE"):
            return []
        if s.startswith("MERGE"):
            rows = self._parse_merge_source(statement)
            target = self.identity if "wsmig_identity_map" in statement else self.state
            keycols = (("source_workspace_id", "entity_type", "source_key")
                       if target is self.identity
                       else ("source_workspace_id", "asset_type", "natural_key"))
            for r in rows:
                target[tuple(r.get(c) for c in keycols)] = r
            return []
        if s.startswith("SELECT"):
            table = self.identity if "wsmig_identity_map" in statement else self.state
            rows = list(table.values())
            # honour the source_workspace_id filter, which is the whole point of the assertion
            if "WHERE source_workspace_id = " in statement:
                want = statement.split("WHERE source_workspace_id = ", 1)[1].strip().strip("'")
                rows = [r for r in rows if r.get("source_workspace_id") == want]
            return rows
        return []

    @staticmethod
    def _parse_merge_source(statement: str) -> list[dict]:
        """Pull `SELECT 'v' AS col, ... UNION ALL ...` out of the MERGE's USING clause."""
        import re
        inner = statement.split("USING (", 1)[1].rsplit(") s", 1)[0]
        rows = []
        for chunk in inner.split(" UNION ALL "):
            row = {}
            for m in re.finditer(r"(NULL|'(?:[^'\\]|\\.)*') AS (\w+)", chunk):
                raw, col = m.group(1), m.group(2)
                if raw == "NULL":
                    row[col] = None
                else:
                    row[col] = raw[1:-1].replace("\\'", "'").replace("\\\\", "\\")
            rows.append(row)
        return rows


def _store(ws_id="111", run_id="r1", **over):
    d = {"role": "target", "source_workspace_id": ws_id, "run_id": run_id,
         "target_staging_location": "/Volumes/a/b/c", "dry_run": False,
         "imports": {"state_catalog": "cat", "state_schema": "sch"}}
    d.update(over)
    cfg = Config.from_dict(d)
    backend = FakeBackend()
    st = StateStore(backend, cfg)
    st.ensure_table()
    st.load()
    return st, backend


# ── the decision table (master §9) ─────────────────────────────────────────

def test_decide_create_when_no_row_and_absent_on_target():
    st, _ = _store()
    assert st.decide("job", "j1", "sha256:a", exists_on_target=False) is UpsertAction.CREATE


def test_decide_adopt_when_no_row_but_object_already_exists():
    """ADOPT is what stops a duplicate when an object was made by hand, or by an attempt that died
    between the API call and the bookkeeping write."""
    st, _ = _store()
    assert st.decide("job", "j1", "sha256:a", exists_on_target=True) is UpsertAction.ADOPT


def test_decide_skip_when_fingerprint_unchanged():
    st, _ = _store()
    st.record("job", "j1", action=ACTION_CREATED, fingerprint="sha256:a", target_object_id="9")
    assert st.decide("job", "j1", "sha256:a", exists_on_target=True) is UpsertAction.SKIP


def test_decide_update_when_fingerprint_changed():
    st, _ = _store()
    st.record("job", "j1", action=ACTION_CREATED, fingerprint="sha256:a", target_object_id="9")
    assert st.decide("job", "j1", "sha256:CHANGED", exists_on_target=True) is UpsertAction.UPDATE


def test_decide_recreates_when_the_recorded_object_is_gone_from_target():
    """A row saying we created it, but it isn't there now (deleted on target / stale id): CREATE.
    SKIPping would leave a permanent hole that no later run ever notices."""
    st, _ = _store()
    st.record("job", "j1", action=ACTION_CREATED, fingerprint="sha256:a", target_object_id="9")
    assert st.decide("job", "j1", "sha256:a", exists_on_target=False) is UpsertAction.CREATE


def test_the_worked_example_from_the_master_plan():
    """Source policy "p1" (src id 3) → target id 9; later edited on source ⇒ UPDATE against 9.

    This is the scenario the whole both-ids design exists for, so it gets an explicit test.
    """
    st, _ = _store()
    st.record("cluster_policy", "p1", action=ACTION_CREATED, fingerprint="sha256:v1",
              source_object_id="3", target_object_id="9")
    st.flush()
    # a later run: the policy was edited on source
    assert st.decide("cluster_policy", "p1", "sha256:v2", exists_on_target=True) is \
        UpsertAction.UPDATE
    assert st.get_target_id("cluster_policy", "p1") == "9", \
        "the UPDATE path must edit target id 9, not create a duplicate"


# ── write cadence (D2) ─────────────────────────────────────────────────────

def test_writes_are_batched_not_per_object():
    """A Delta MERGE per object is hours of bookkeeping on a large workspace, so nothing is
    written until STATE_BATCH rows accumulate."""
    st, backend = _store()
    merges_before = len([s for s in backend.statements if s.strip().startswith("MERGE")])
    for i in range(STATE_BATCH - 1):
        st.record("notebook", f"/n{i}", action=ACTION_CREATED, fingerprint="f")
    assert len([s for s in backend.statements if s.strip().startswith("MERGE")]) == merges_before
    st.record("notebook", "/n_last", action=ACTION_CREATED, fingerprint="f")
    assert len([s for s in backend.statements if s.strip().startswith("MERGE")]) > merges_before


def test_a_failure_flushes_the_successes_before_it():
    """The customer's explicit requirement: a poison asset must never strand the 199 successes
    queued in front of it."""
    st, backend = _store()
    for i in range(5):
        st.record("job", f"ok{i}", action=ACTION_CREATED, fingerprint="f", target_object_id=str(i))
    assert not [s for s in backend.statements if s.strip().startswith("MERGE")], \
        "nothing should be flushed yet"
    st.record("job", "poison", action=ACTION_FAILED, error="API said no")
    assert [s for s in backend.statements if s.strip().startswith("MERGE")], \
        "a failure must force a flush of the preceding successes"
    assert ("111", "job", "ok3") in backend.state


def test_flush_never_raises_so_it_is_safe_in_a_finally():
    """flush() runs in `finally` during an abort — a bookkeeping error must not replace the real
    error, so it logs and leaves the rows pending for the recovery replay."""
    st, backend = _store()
    st.record("job", "j", action=ACTION_CREATED, fingerprint="f")
    backend.fail_next = True
    st.flush()   # must not raise
    assert st._pending, "rows must stay pending after a failed flush so replay can recover them"


def test_recovery_replay_merges_unflushed_checkpoint_outcomes():
    """The batch is only safe because the checkpoint is the per-item durability layer: anything
    lost with an unflushed batch is replayed from it at run start."""
    st, backend = _store()
    replayed = st.recovery_replay({
        "job|j-from-checkpoint": {"import_status": ACTION_CREATED, "target_id": "77",
                                  "fingerprint": "sha256:f", "source_id": "1"},
        "cluster|c1": {"import_status": ACTION_CREATED, "target_id": "88", "fingerprint": "f2"},
    })
    assert replayed == 2
    assert st.get_target_id("job", "j-from-checkpoint") == "77"
    assert ("111", "cluster", "c1") in backend.state


def test_recovery_replay_skips_rows_already_written_this_run():
    st, _ = _store()
    st.record("job", "j1", action=ACTION_CREATED, fingerprint="f", target_object_id="9")
    st.flush()
    replayed = st.recovery_replay({"job|j1": {"import_status": ACTION_CREATED,
                                              "target_id": "9", "fingerprint": "f"}})
    assert replayed == 0, "a row already flushed for this run must not be replayed"


def test_an_unknown_last_action_is_rejected_loudly():
    """A typo'd action would create a row NO retry_mode bucket ever finds — invisible outstanding
    work. Better to fail at the one place that can catch it."""
    st, _ = _store()
    with pytest.raises(ValueError, match="unknown last_action"):
        st.record("job", "j", action="sort-of-worked")


def test_a_later_row_never_blanks_a_known_target_id():
    """A FAILED update must keep the target id, or the next run loses the object and duplicates."""
    st, _ = _store()
    st.record("job", "j1", action=ACTION_CREATED, fingerprint="v1", target_object_id="9",
              source_object_id="3")
    st.record("job", "j1", action=ACTION_FAILED, error="edit rejected")
    assert st.get_target_id("job", "j1") == "9"
    assert st.row("job", "j1")["source_object_id"] == "3"
    assert st.row("job", "j1")["last_source_fingerprint"] == "v1"


def test_first_seen_is_preserved_across_updates():
    st, _ = _store()
    st.record("job", "j1", action=ACTION_CREATED, fingerprint="v1", target_object_id="9")
    first = st.row("job", "j1")["first_seen"]
    st.record("job", "j1", action=ACTION_UPDATED, fingerprint="v2", target_object_id="9")
    assert st.row("job", "j1")["first_seen"] == first


# ── the 100+-pair guarantee: every read filtered by source_workspace_id ────

def test_every_read_is_filtered_by_source_workspace_id():
    """One table serves all pairs. Without this filter a re-run of pair A would see pair B's target
    ids and try to edit ANOTHER workspace's objects."""
    st, backend = _store(ws_id="AAA")
    st.load(force=True)
    selects = [s for s in backend.statements if s.strip().startswith("SELECT")]
    assert selects, "no SELECT was issued"
    for s in selects:
        assert "WHERE source_workspace_id = 'AAA'" in s, f"unfiltered read: {s[:120]}"


def test_pairs_are_isolated_in_a_shared_table():
    st_a, backend = _store(ws_id="AAA")
    st_a.record("job", "shared-name", action=ACTION_CREATED, fingerprint="fa",
                target_object_id="targetA")
    st_a.flush()
    # a second pair, SAME table (same backend), same natural_key
    cfg_b = Config.from_dict({"role": "target", "source_workspace_id": "BBB", "run_id": "r1",
                              "target_staging_location": "/Volumes/a/b/c", "dry_run": False,
                              "imports": {"state_catalog": "cat", "state_schema": "sch"}})
    st_b = StateStore(backend, cfg_b)
    st_b.load()
    assert st_b.get_target_id("job", "shared-name") is None, \
        "pair BBB must not see pair AAA's target id"
    st_b.record("job", "shared-name", action=ACTION_CREATED, fingerprint="fb",
                target_object_id="targetB")
    st_b.flush()
    st_a.load(force=True)
    assert st_a.get_target_id("job", "shared-name") == "targetA", "pair AAA's row was clobbered"


# ── retry modes (§7d / D22) ────────────────────────────────────────────────

def test_retry_buckets_pick_up_exactly_the_documented_actions():
    st, _ = _store()
    st.record("job", "failed-one", action=ACTION_FAILED, error="x")
    st.record("job", "warned-one", action="created_with_warning", target_object_id="1")
    st.record("job", "skipped-one", action=ACTION_SKIPPED, fingerprint="f", target_object_id="2")
    st.record("repo", "manual-one", action=ACTION_MANUAL)
    st.record("genie_space", "deferred-one", action=ACTION_NOT_SELECTED)
    st.record("job", "happy-one", action=ACTION_CREATED, fingerprint="f", target_object_id="3")
    st.flush()

    assert st.retry_keys("off") is None, "off means 'everything', not 'nothing'"
    failed = st.retry_keys("failed_only")
    # PLAN 8 Bug 2: failed_only means LITERALLY failed — created_with_warning is NOT in it (it used
    # to be, which made the label lie: a warned-but-created unit re-selected under failed_only came
    # back as `Skipped (unchanged)` in a report meant to hold only outstanding failures).
    assert failed == {("job", "failed-one")}
    assert ("job", "warned-one") not in failed
    skipped = st.retry_keys("skipped_only")
    assert skipped == {("job", "skipped-one"), ("repo", "manual-one"),
                       ("genie_space", "deferred-one")}
    # created_with_warning rides failed_and_skipped (the "fix the prerequisite, then re-attempt"
    # case) and stays visible there, so it is never silently forgotten.
    both = st.retry_keys("failed_and_skipped")
    assert ("job", "warned-one") in both
    assert both == failed | skipped | {("job", "warned-one")}
    for bucket in (failed, skipped):
        assert ("job", "happy-one") not in bucket, "a clean unit must never be retried"


def test_source_detail_column_round_trips_and_is_carried_forward():
    """PLAN 8 Bug 5: last_source_detail persists through the MERGE, and a later record that omits it
    does NOT blank it (carry-forward, like the ids) — so a non-identity re-record can't lose the
    snapshot identity wrote."""
    st, _ = _store()
    st.record("group", "g", action=ACTION_CREATED, fingerprint="f", target_object_id="g1",
              source_detail='{"members": ["a", "b"]}')
    st.flush()
    st.load(force=True)
    assert st.row("group", "g")["last_source_detail"] == '{"members": ["a", "b"]}'
    # a later record for the same key that omits source_detail keeps the prior snapshot
    st.record("group", "g", action=ACTION_SKIPPED, fingerprint="f", target_object_id="g1")
    st.flush()
    st.load(force=True)
    assert st.row("group", "g")["last_source_detail"] == '{"members": ["a", "b"]}'


def test_ensure_table_adds_the_source_detail_column_with_supported_syntax():
    """PLAN 8 Bug 5: the live control table gains last_source_detail via `ADD COLUMNS (...)` —
    NOT `ADD COLUMNS IF NOT EXISTS`, which is a PARSE_SYNTAX_ERROR on Databricks SQL (verified live
    2026-08-18). Never a drop/recreate."""
    st, backend = _store()
    alters = [s for s in backend.statements if s.strip().startswith("ALTER")]
    assert any("ADD COLUMNS (last_source_detail STRING)" in s for s in alters), \
        "ensure_table must ALTER with the SUPPORTED ADD COLUMNS syntax"
    assert not any("IF NOT EXISTS" in s for s in alters), \
        "ADD COLUMNS IF NOT EXISTS is unsupported for columns and would PARSE_SYNTAX_ERROR live"


def test_ensure_table_is_idempotent_when_the_column_already_exists():
    """PLAN 8 Bug 5: a re-run's ALTER hits FIELD_ALREADY_EXISTS — that is swallowed (idempotent),
    while any OTHER DDL failure still surfaces as the catalog/permissions guidance."""
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r1",
                            "target_staging_location": "/Volumes/a/b/c", "dry_run": False,
                            "imports": {"state_catalog": "cat", "state_schema": "sch"}})

    class AlterAlreadyExistsBackend(FakeBackend):
        def sql(self, statement):
            if statement.strip().startswith("ALTER"):
                raise RuntimeError("[FIELD_ALREADY_EXISTS] Cannot add column, because "
                                   "`last_source_detail` already exists. SQLSTATE: 42710")
            return super().sql(statement)

    st = StateStore(AlterAlreadyExistsBackend(), cfg)
    st.ensure_table()   # must NOT raise — the duplicate-column error is benign on a re-run


# ── prerequisite satisfaction (§5) — what makes phase-at-a-time work ───────

def test_has_family_and_target_ids_enable_phase_at_a_time_migration():
    """Selecting `jobs` alone is legitimate when compute is already recorded — its target ids come
    from the table, not from having run compute in this session."""
    st, _ = _store()
    assert st.has_family(("cluster", "instance_pool")) is False
    st.record("cluster", "etl", action=ACTION_CREATED, fingerprint="f", source_object_id="src-1",
              target_object_id="0101-target")
    st.flush()
    assert st.has_family(("cluster", "instance_pool")) is True
    assert st.target_ids_for("cluster") == {"etl": "0101-target"}


def test_failed_rows_do_not_satisfy_a_prerequisite():
    """A cluster that FAILED to create cannot be a satisfied prerequisite for jobs that remap onto
    it — otherwise the jobs phase would remap against an id that doesn't exist."""
    st, _ = _store()
    st.record("cluster", "etl", action=ACTION_FAILED, error="quota")
    assert st.has_family(("cluster",)) is False


# ── deleted-in-source (D5) ─────────────────────────────────────────────────

def test_deleted_in_source_is_reported_never_deleted():
    st, _ = _store()
    st.record("job", "still-there", action=ACTION_CREATED, fingerprint="f", target_object_id="1")
    st.record("job", "gone-now", action=ACTION_CREATED, fingerprint="f", target_object_id="2")
    st.flush()
    gone = st.mark_missing_in_source("job", present_keys={"still-there"})
    assert gone == ["gone-now"]
    assert st.row("job", "gone-now")["last_action"] == "deleted_in_source"
    assert "NOT deleted on target" in st.row("job", "gone-now")["last_error"]
    # reporting is idempotent — a second run must not re-report it
    assert st.mark_missing_in_source("job", present_keys={"still-there"}) == []


# ── identity map (§7.1) ────────────────────────────────────────────────────

def test_identity_map_records_every_touched_identity_including_adopted_ones():
    """Every identity gets a row, even ones deliberately NOT created: the target SCIM id differs
    even when the natural key doesn't, and ACL remap needs that id."""
    st, _ = _store()
    st.record_identity("service_principal", "old-app-id", target_id="scim-1",
                       target_key="new-app-id", classification="db_managed_sp", action="created")
    st.record_identity("service_principal", "umi-app-id", target_id="scim-2",
                       target_key="umi-app-id", classification="umi_or_entra_sp", action="adopted")
    st.record_identity("group", "finance", target_id="grp-9", classification="db_managed_group",
                       action="created")
    st.record_identity("user", "a@b.com", target_id="usr-3", classification="entra_user",
                       action="adopted")
    st.flush()

    m = st.load_identity_map()
    # a recreated DB-managed SP maps old appId → NEW appId (this is the mapping that cannot be
    # recovered by re-reading the target, hence the durable table)
    assert m["sp_mapping"]["old-app-id"] == "new-app-id"
    # an adopted account SP keeps its appId but still needs its TARGET scim id recorded
    assert m["sp_mapping"]["umi-app-id"] == "umi-app-id"
    assert m["scim_ids"]["service_principal:umi-app-id"] == "scim-2"
    assert m["group_map"]["finance"] == "grp-9"
    assert m["user_map"]["a@b.com"] == "a@b.com"
    assert m["scim_ids"]["user:a@b.com"] == "usr-3"


def test_identity_rows_survive_a_reload_from_the_table():
    """The map must be durable, not just in-memory — a fresh store on the same table sees it."""
    st, backend = _store()
    st.record_identity("service_principal", "old-app", target_id="s1", target_key="new-app",
                       action="created")
    st.flush()
    fresh = StateStore(backend, st.config)
    fresh.load()
    assert fresh.load_identity_map()["sp_mapping"] == {"old-app": "new-app"}


# ── disabled store (first-look dry run needs no UC setup) ──────────────────

def test_a_disabled_store_is_a_safe_no_op():
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "1", "run_id": "r",
                            "target_staging_location": "/Volumes/a/b/c", "dry_run": True})
    st = StateStore(None, cfg)
    assert st.enabled is False
    st.ensure_table()
    st.record("job", "j", action=ACTION_CREATED)
    st.record_identity("group", "g", target_id="1")
    assert st.flush() == 0
    assert st.load() == {}
    assert st.decide("job", "j", "f", exists_on_target=False) is UpsertAction.CREATE
    assert st.retry_keys("failed_only") is None or st.retry_keys("failed_only") == set()


# ── SQL literal escaping ───────────────────────────────────────────────────

def test_names_with_quotes_round_trip():
    """A notebook called `Bob's ETL` is entirely normal — the MERGE must not break on it."""
    st, backend = _store()
    nasty = "/Users/bob/Bob's \"ETL\" \\ pipeline"
    st.record("notebook", nasty, action=ACTION_CREATED, fingerprint="f", target_object_id="1")
    st.flush()
    st.load(force=True)
    assert st.get_target_id("notebook", nasty) == "1"


def test_error_text_with_quotes_and_newlines_round_trips():
    st, _ = _store()
    st.record("job", "j", action=ACTION_FAILED,
              error="can't find 'x'", error_raw='{"error":"nope"}\nline2')
    st.flush()
    st.load(force=True)
    assert "can't find 'x'" == st.row("job", "j")["last_error"]
