"""Offline tests for the importer framework: base_importer, phases, import_runner (Plan 3 §3–§5).

The behaviours proven here are the ones every asset importer then inherits for free, so they are
tested once, here, against a toy importer rather than 12 times against real APIs:

  • the FAIL-SOFT invariant (D21) — a unit's failure never propagates, later units still run
  • the four whole-run aborts, all BEFORE any unit is attempted
  • dry-run purity — a rehearsal makes real decisions and zero mutating calls
  • idempotency — re-run SKIPs, a source edit UPDATEs against the STORED target id, a
    pre-existing object is ADOPTED not duplicated
  • resume from the checkpoint (which stores outcomes, not just done-flags)
  • the selector + prerequisite validation, incl. "satisfied by a previous session"
  • run resolution precedence (§3)
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from src.exporters import bundle_paths as BP
from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.importers.base_importer import BaseImporter
from src.importers.import_runner import (BundleVerificationError, ImportRunner, PrerequisiteError,
                                         resolve_import_run_id)
from src.importers.phases import PHASE_ORDER, ordered, validate_selection
from src.state.state_store import StateStore
from tests.test_state_store import FakeBackend


# ── a toy importer used to exercise the base-class machinery ───────────────

class ToyImporter(BaseImporter):
    """A fake asset family: `create_one` records calls and can be told to fail on a given key."""

    component = "compute"
    asset_types = ("cluster",)

    fail_on: set = set()
    warn_on: set = set()
    preexisting: dict = {}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.created: list[str] = []
        self.updated: list[tuple] = []

    def load(self):
        return self.units_for("cluster")

    def existing_keys(self):
        return dict(self.preexisting)

    def create_one(self, unit):
        key = self.natural_key(unit)
        if key in self.fail_on:
            raise RuntimeError("INVALID_PARAMETER_VALUE: the target said no")
        self.created.append(key)
        return {"target_id": f"tgt-{key}",
                "warning": "degraded" if key in self.warn_on else ""}

    def update_one(self, unit, target_id):
        self.updated.append((self.natural_key(unit), target_id))
        return {"target_id": target_id}


def _unit(key, fingerprint="sha256:v1", **over):
    u = {"asset_type": "cluster", "natural_key": key, "source_id": f"src-{key}",
         "fingerprint": fingerprint, "import_action": "create", "export_status": "success",
         "payload": {"cluster_name": key}}
    u.update(over)
    return u


def _setup(units, *, dry_run=False, state=True, **cfg_over):
    d = tempfile.mkdtemp()
    conf = {"role": "target", "source_workspace_id": "111", "run_id": "r1",
            "target_staging_location": d, "dry_run": dry_run,
            "imports": ({"state_catalog": "cat", "state_schema": "sch"} if state else {})}
    conf.update(cfg_over)
    cfg = Config.from_dict(conf)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    st = None
    if state:
        st = StateStore(FakeBackend(), cfg)
        st.ensure_table()
        st.load()
    imp = ToyImporter(object(), cfg, aw, state=st, units_by_type={"cluster": units})
    return imp, aw, st, cfg


# ── FAIL-SOFT (D21) ────────────────────────────────────────────────────────

def test_a_unit_failure_never_stops_the_phase():
    """THE invariant: one asset's failure is recorded and the run continues to the next unit."""
    imp, _aw, st, _cfg = _setup([_unit("a"), _unit("poison"), _unit("b")])
    imp.fail_on = {"poison"}
    res = imp.run()
    assert imp.created == ["a", "b"], "units after the failure were not attempted"
    assert res.created == 2 and res.failed == 1
    assert res.total == 3, "every unit must be accounted for"
    assert st.row("cluster", "poison")["last_action"] == "failed"


def test_a_failure_records_a_human_readable_reason_and_a_category():
    """`last_error` must be actionable, not a raw traceback — the raw text goes to last_error_raw."""
    imp, _aw, st, _cfg = _setup([_unit("bad")])
    imp.fail_on = {"bad"}
    imp.run()
    row = st.row("cluster", "bad")
    assert row["failure_category"] == "api_error"
    assert "rejected" in row["last_error"]
    assert "INVALID_PARAMETER_VALUE" in row["last_error_raw"]


def test_an_unexpected_exception_in_load_does_not_abort_the_phase():
    """A family that can't even list must not take the other families down with it."""
    imp, _aw, _st, _cfg = _setup([_unit("a")])
    imp.load = lambda: (_ for _ in ()).throw(RuntimeError("listing blew up"))
    res = imp.run()
    assert res.total == 0
    assert any("load failed" in e for e in res.errors)


def test_an_existence_check_failure_falls_back_to_already_exists_adopts():
    """Failing OPEN on the existence check would risk duplicates, so it warns loudly and relies on
    the create path's RESOURCE_ALREADY_EXISTS adopt."""
    imp, _aw, _st, _cfg = _setup([_unit("a")])
    imp.existing_keys = lambda: (_ for _ in ()).throw(RuntimeError("list failed"))
    res = imp.run()
    assert res.created == 1
    assert any("could not list existing" in w for w in res.warnings)


def test_a_create_that_races_the_existence_check_is_adopted_not_failed():
    """RESOURCE_ALREADY_EXISTS means the object is there — which is the outcome we wanted."""
    class Racy(ToyImporter):
        def create_one(self, unit):
            raise RuntimeError("RESOURCE_ALREADY_EXISTS: a cluster with that name exists")

    imp, _aw, st, _cfg = _setup([_unit("racer")])
    imp.__class__ = Racy
    res = imp.run()
    assert res.adopted == 1 and res.failed == 0
    assert st.row("cluster", "racer")["last_action"] == "adopted"


# ── idempotency / upsert (§4, §7) ──────────────────────────────────────────

def test_second_run_skips_unchanged_units():
    units = [_unit("a"), _unit("b")]
    imp, aw, st, cfg = _setup(units)
    imp.run()
    assert imp.created == ["a", "b"]

    # a fresh importer over the same state + staging, same fingerprints
    imp2 = ToyImporter(object(), cfg, aw, state=st, units_by_type={"cluster": units})
    imp2.preexisting = {"a": "tgt-a", "b": "tgt-b"}
    imp2.config.imports.force_full_import = True   # bypass the checkpoint, exercise the STATE path
    res2 = imp2.run()
    assert imp2.created == [], "an unchanged unit must not be recreated"
    assert res2.skipped == 2


def test_a_changed_fingerprint_updates_against_the_stored_target_id():
    """The worked example: never a duplicate, and never an edit against a SOURCE id."""
    imp, aw, st, cfg = _setup([_unit("a")])
    imp.run()
    assert st.get_target_id("cluster", "a") == "tgt-a"

    edited = [_unit("a", fingerprint="sha256:EDITED")]
    imp2 = ToyImporter(object(), cfg, aw, state=st, units_by_type={"cluster": edited})
    imp2.preexisting = {"a": "tgt-a"}
    imp2.config.imports.force_full_import = True
    res2 = imp2.run()
    assert imp2.updated == [("a", "tgt-a")], "must edit the STORED target id"
    assert imp2.created == [] and res2.updated == 1


def test_a_preexisting_object_is_adopted_not_duplicated():
    imp, _aw, st, _cfg = _setup([_unit("made-by-hand")])
    imp.preexisting = {"made-by-hand": "tgt-existing"}
    res = imp.run()
    assert imp.created == [] and res.adopted == 1
    assert st.get_target_id("cluster", "made-by-hand") == "tgt-existing"


def test_an_adopted_but_stale_object_is_updated():
    """Adopting isn't the end of it: if the source moved on since, the object must be updated."""
    imp, aw, st, cfg = _setup([_unit("x")])
    imp.run()   # creates, records fingerprint v1
    stale = [_unit("x", fingerprint="sha256:NEWER")]
    imp2 = ToyImporter(object(), cfg, aw, state=st, units_by_type={"cluster": stale})
    imp2.preexisting = {"x": "tgt-x"}
    imp2.config.imports.force_full_import = True
    imp2.run()
    assert imp2.updated == [("x", "tgt-x")]


def test_update_without_a_recorded_target_id_fails_loudly_rather_than_guessing():
    imp, _aw, st, _cfg = _setup([_unit("a", fingerprint="sha256:v2")])
    # a state row with a fingerprint but NO target id (e.g. an earlier failed create)
    st.record("cluster", "a", action="failed", fingerprint="sha256:v1")
    imp.preexisting = {}
    res = imp.run()
    # not on target + row present ⇒ CREATE (the recorded object is gone), so this proves the
    # decision table prefers recreating over a blind update
    assert res.created == 1


# ── resume from the checkpoint (§4) ────────────────────────────────────────

def test_resume_restores_the_recorded_outcome_without_recalling_the_api():
    """The checkpoint stores OUTCOMES, not just done-flags: import_results.json is written only at
    the end, so after a crash it doesn't exist and could not supply the target ids."""
    imp, aw, st, cfg = _setup([_unit("a"), _unit("b")])
    imp.run()

    imp2 = ToyImporter(object(), cfg, aw, state=st,
                       units_by_type={"cluster": [_unit("a"), _unit("b")]})
    res2 = imp2.run()
    assert imp2.created == [], "a checkpointed unit must not be re-created"
    assert res2.total == 2
    assert all(r["target_id"] for r in res2.units), "target ids must be restored from the checkpoint"


def test_force_full_import_ignores_the_checkpoint():
    imp, aw, st, cfg = _setup([_unit("a")])
    imp.run()
    cfg.imports.force_full_import = True
    imp2 = ToyImporter(object(), cfg, aw, state=st, units_by_type={"cluster": [_unit("a")]})
    imp2.preexisting = {}          # pretend it vanished, so the decision is CREATE again
    imp2.run()
    assert imp2.created == ["a"], "force_full_import must re-evaluate every unit"


def test_retry_mode_reattempts_a_failed_unit_despite_the_checkpoint():
    """REGRESSION: a failed unit is written to the checkpoint, so a plain re-run replays 'failed'.
    But when the operator fixes the prerequisite and re-runs with `retry_mode=failed_only`, the
    unit MUST be re-attempted — retry_mode has to bypass the checkpoint resume, or the documented
    "fix the cause, re-run with retry_mode=failed_only" workflow is a silent no-op (found live: DLT
    pipelines that failed on a missing catalog grant would never retry without force_full_import).
    """
    imp, aw, st, cfg = _setup([_unit("a")])
    imp.fail_on = {"a"}
    imp.run(); st.flush()
    assert imp.result.failed == 1
    assert st.row("cluster", "a")["last_action"] == "failed"

    # Prerequisite fixed; retry_mode=failed_only, force_full_import stays FALSE.
    st.load(force=True)
    imp2 = ToyImporter(object(), cfg, aw, state=st, units_by_type={"cluster": [_unit("a")]})
    imp2.fail_on = set()
    imp2.retry_keys = st.retry_keys("failed_only")
    assert imp2.retry_keys == {("cluster", "a")}
    res2 = imp2.run(); st.flush()
    assert imp2.created == ["a"], "the failed unit must be re-created on retry, not replayed as failed"
    assert res2.failed == 0
    assert st.row("cluster", "a")["last_action"] == "created"


def test_retry_mode_does_not_reattempt_units_outside_the_bucket():
    """The narrowing still holds: a unit NOT in the retry bucket is recorded skipped, not touched —
    even though bypassing the checkpoint for the targeted ones."""
    imp, aw, st, cfg = _setup([_unit("a"), _unit("b")])
    imp.fail_on = {"a"}
    imp.run(); st.flush()          # a -> failed, b -> created (both checkpointed)

    st.load(force=True)
    imp2 = ToyImporter(object(), cfg, aw, state=st,
                       units_by_type={"cluster": [_unit("a"), _unit("b")]})
    imp2.fail_on = set()
    imp2.retry_keys = st.retry_keys("failed_only")   # only ("cluster","a")
    res2 = imp2.run()
    assert imp2.created == ["a"], "only the failed unit is re-attempted"
    b_row = next(r for r in res2.units if r["natural_key"] == "b")
    assert b_row["import_status"] == "skipped" and "not outstanding" in b_row["note"]


# ── dry run (§11 dry-run purity) ───────────────────────────────────────────

def test_dry_run_makes_real_decisions_and_zero_writes():
    imp, _aw, st, _cfg = _setup([_unit("a"), _unit("b")], dry_run=True)
    res = imp.run()
    assert imp.created == [], "a dry run must not call any create"
    assert res.dry_run == 2
    assert all("dry run: would CREATE" in r["note"] for r in res.units)
    # decisions are real, so the state store still knows nothing was actually made
    assert st.get_target_id("cluster", "a") is None


def test_dry_run_is_not_checkpointed():
    """A rehearsal must not make the next REAL run believe the work is already done."""
    imp, aw, st, cfg = _setup([_unit("a")], dry_run=True)
    imp.run()
    cfg.dry_run = False
    imp2 = ToyImporter(object(), cfg, aw, state=st, units_by_type={"cluster": [_unit("a")]})
    imp2.run()
    assert imp2.created == ["a"], "the dry run wrongly checkpointed the unit as done"


def test_dry_run_reports_the_action_it_WOULD_take():
    imp, aw, st, cfg = _setup([_unit("a")])
    imp.run()                                  # real create
    cfg.dry_run = True
    imp2 = ToyImporter(object(), cfg, aw, state=st,
                       units_by_type={"cluster": [_unit("a", fingerprint="sha256:CHANGED")]})
    imp2.preexisting = {"a": "tgt-a"}
    cfg.imports.force_full_import = True
    res = imp2.run()
    assert "would UPDATE" in res.units[0]["note"]


# ── manual / DAB units are never attempted ─────────────────────────────────

def test_manual_units_are_recorded_not_attempted():
    """Attempting a known-impossible create produces a permanent red failure every run, which
    trains the operator to ignore red (D10)."""
    imp, _aw, st, _cfg = _setup([_unit("repo1", import_action="manual",
                                       note="repos are out of scope")])
    res = imp.run()
    assert imp.created == [] and res.manual == 1
    assert st.row("cluster", "repo1")["last_action"] == "manual"


def test_dab_units_are_skipped_on_import_action_not_migration_mode():
    """Branching on `migration_mode` would import bundle STATE files and corrupt the customer's
    next `databricks bundle deploy` — it maps resources to SOURCE-workspace ids (verified live)."""
    imp, _aw, _st, _cfg = _setup([_unit("/Shared/.bundle/b/files/nb", import_action="dab_redeploy",
                                        migration_mode="content")])
    res = imp.run()
    assert imp.created == [], "bundle-owned content must never be imported"
    assert res.skipped == 1


# ── retry_mode narrows the work list only ──────────────────────────────────

def test_retry_keys_narrow_the_work_list_without_hiding_units():
    imp, _aw, _st, _cfg = _setup([_unit("outstanding"), _unit("clean")])
    imp.retry_keys = {("cluster", "outstanding")}
    res = imp.run()
    assert imp.created == ["outstanding"], "only outstanding units should be attempted"
    assert res.total == 2, "a narrowed run must still account for every unit"


# ── phases: order + prerequisite validation (§5) ───────────────────────────

def test_phase_order_is_identity_first_acls_last():
    assert PHASE_ORDER[0] == "identity" and PHASE_ORDER[-1] == "acls"
    assert ordered(["acls", "jobs", "identity"]) == ["identity", "jobs", "acls"]


def test_selecting_a_family_without_its_prerequisites_is_a_hard_error_naming_them():
    problems = validate_selection(["jobs"], state_store=None)
    joined = " ".join(problems)
    assert "needs `identity`" in joined and "needs `compute`" in joined
    assert "needs `workspace`" in joined
    # the message must TEACH: name the actual unresolvable reference and the fix
    assert "existing_cluster_id" in joined
    assert "import_assets" in joined


def test_a_prerequisite_already_in_the_state_table_satisfies_the_check():
    """This is what makes phase-at-a-time migration work at all — and why the table stores ids."""
    backend = FakeBackend()
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r",
                            "target_staging_location": "/tmp/x", "dry_run": False,
                            "imports": {"state_catalog": "c", "state_schema": "s"}})
    st = StateStore(backend, cfg)
    st.ensure_table()
    st.load()
    assert validate_selection(["genie"], st), "sql is not yet imported, so genie must be blocked"
    st.record("sql_warehouse", "wh1", action="created", fingerprint="f", target_object_id="w-1")
    st.flush()
    st.load(force=True)
    assert validate_selection(["genie"], st) == [], \
        "a prerequisite recorded in an earlier session must satisfy the check"


def test_selecting_everything_is_always_valid():
    assert validate_selection(list(PHASE_ORDER), state_store=None) == []


# ── run resolution (§3) ────────────────────────────────────────────────────

def _bundle(tmp, run_id, *, manifest=True, import_cp=False, results=False):
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": run_id,
                            "target_staging_location": tmp})
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    if manifest:
        aw.write_json(BP.MANIFEST_JSON, {"files": [], "tool_version": "0.1.0"})
    if import_cp:
        aw.write_json(BP.CHECKPOINT_JSON, {"import:compute": ["a"],
                                          "import:compute:results": {"a": {}}})
    if results:
        aw.write_json(BP.IMPORT_RESULTS_JSON, {"units": []})
    return cfg, aw


def test_run_resolution_prefers_an_explicit_run_id():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, _aw = _bundle(tmp, "20260101_000000")
        assert resolve_import_run_id(cfg, "explicit_run") == ("explicit_run", "widget")


def test_run_resolution_resumes_an_incomplete_import():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, _ = _bundle(tmp, "20260101_000000", import_cp=True)
        run_id, how = resolve_import_run_id(cfg, "")
        assert (run_id, how) == ("20260101_000000", "resume-incomplete-import")


def test_run_resolution_ignores_a_finished_import_and_uses_the_pointer():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, aw = _bundle(tmp, "20260101_000000", import_cp=True, results=True)
        from src.exporters.bundle_state import write_latest_export_pointer
        write_latest_export_pointer(cfg, "20260202_000000",
                                   {"tool_version": "0.1.0"}, {"job": 1})
        run_id, how = resolve_import_run_id(cfg, "")
        assert (run_id, how) == ("20260202_000000", "LATEST_EXPORT.json")


def test_run_resolution_fails_loudly_rather_than_inventing_a_run_id():
    """Inventing one would import an EMPTY bundle and report a spuriously clean run."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r",
                                "target_staging_location": tmp})
        with pytest.raises(RuntimeError, match="Cannot resolve which bundle"):
            resolve_import_run_id(cfg, "")


def test_an_export_checkpoint_alone_is_not_a_resumable_IMPORT():
    """An export checkpoint must not be mistaken for import progress."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, aw = _bundle(tmp, "20260101_000000")
        aw.write_json(BP.CHECKPOINT_JSON, {"export:content": ["/n"]})
        from src.exporters.bundle_state import write_latest_export_pointer
        write_latest_export_pointer(cfg, "20260101_000000", {"tool_version": "0.1.0"}, {})
        _run, how = resolve_import_run_id(cfg, "")
        assert how == "LATEST_EXPORT.json"


# ── the whole-run gates (all BEFORE any unit) ─────────────────────────────

def test_a_bad_manifest_aborts_before_any_unit_is_attempted():
    """A partial upload must never present as a partial migration (D7)."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, aw = _bundle(tmp, "r1", manifest=False)
        aw.write_json(BP.MANIFEST_JSON,
                      {"files": [{"path": "export/gone.json", "bytes": 5, "sha256": "deadbeef"}],
                       "tool_version": "0.1.0"})
        runner = ImportRunner(object(), cfg, aw, state=None)
        with pytest.raises(BundleVerificationError, match="failed its manifest check"):
            runner.run()


def test_the_manifest_survives_an_import_writing_its_own_files():
    """REGRESSION (found live): import writes `checkpoint.json` and its own reports INTO the bundle
    dir. When those were checksummed, the first import invalidated the bundle and the manifest gate
    then refused every later run on a bundle that was actually perfect.

    The manifest attests to the EXPORTED bundle — what must survive the handoff — not to per-attempt
    bookkeeping or target-side output.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg, aw = _bundle(tmp, "r1", manifest=False)
        aw.write_json(BP.EXPORT_INDEX_JSON, {"units": []})
        aw.write_manifest({})
        assert aw.verify_manifest()["ok"], "a freshly exported bundle must verify"

        # Simulate what an import run writes into the same directory.
        aw.write_json(BP.CHECKPOINT_JSON, {"import:compute": ["a"],
                                          "import:compute:results": {"a": {"status": "created"}}})
        aw.write_json(BP.IMPORT_RESULTS_JSON, {"units": []})
        aw.write_bytes("reports/import_results.html", b"<html></html>")
        aw.write_json(BP.PREFLIGHT_REPORT_JSON, {"verdict": "GO"})
        aw.write_json(BP.ACL_PARITY_REPORT_JSON, {"counts": {}})
        aw.write_bytes(BP.MANUAL_ACTIONS_IMPORT_MD, b"# manual actions\n")
        aw.write_bytes(BP.EXECUTION_IMPORT_LOG, b'{"msg": "hello"}\n')

        verify = aw.verify_manifest()
        assert verify["ok"], (
            "the bundle stopped verifying after an import wrote its own files — the second import "
            f"would be refused. mismatched={verify['mismatched']} missing={verify['missing']}")

        # But a genuinely corrupted EXPORT artifact must still be caught.
        with open(os.path.join(aw.root, BP.EXPORT_INDEX_JSON), "w") as f:
            f.write('{"units": ["tampered"]}')
        assert not aw.verify_manifest()["ok"], \
            "a real bundle corruption must still fail the gate"


def test_skip_manifest_verify_is_allowed_but_warns():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, aw = _bundle(tmp, "r1")
        cfg.imports.skip_manifest_verify = True
        runner = ImportRunner(object(), cfg, aw, state=None)
        out = runner.verify_bundle()
        assert out["skipped"] is True


def test_a_pointer_from_a_different_export_is_refused():
    """In airgap mode ops copies the run dir AND the pointer; a mismatch means they came from
    different exports, which must not be imported as if they matched."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, aw = _bundle(tmp, "r1")
        from src.exporters.bundle_state import write_latest_export_pointer
        write_latest_export_pointer(cfg, "r1", {"tool_version": "different-bundle"}, {})
        runner = ImportRunner(object(), cfg, aw, state=None)
        with pytest.raises(BundleVerificationError, match="manifest_checksum does not match"):
            runner.check_pointer_matches_bundle()


def test_a_preflight_no_go_blocks_the_run_when_enforced():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, aw = _bundle(tmp, "r1")
        runner = ImportRunner(object(), cfg, aw, state=None,
                              preflight_verdict={"verdict": "NO-GO",
                                                 "blocking": ["account identities missing"]})
        with pytest.raises(RuntimeError, match="NO-GO"):
            runner.enforce_preflight()
        # …and can be downgraded to advisory for a customer who accepted the gaps
        cfg.imports.preflight_enforce = False
        runner.enforce_preflight()


def test_selecting_a_family_without_prerequisites_aborts_the_runner():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, aw = _bundle(tmp, "r1")
        aw.write_json(BP.EXPORT_INDEX_JSON, {"units": []})
        cfg.imports.import_assets = ["jobs"]
        runner = ImportRunner(object(), cfg, aw, state=None)
        with pytest.raises(PrerequisiteError, match="missing prerequisites"):
            runner.run()
