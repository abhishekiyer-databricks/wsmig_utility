"""PLAN 13 regression tests (B1–B13).

One module for the whole backlog's offline regressions, each test named for the item it pins so a
reintroduced bug fails loudly. Live verification is separate (see plans/qa-testing-agent.md)."""
from __future__ import annotations

import io
import logging

import pytest


# ─────────────────────────────── B5 — structured cell logging ──────────────────────────────

def _reset_wsmig_logger():
    app = logging.getLogger("wsmig")
    for h in list(app.handlers):
        app.removeHandler(h)
    return app


def test_b5_configure_logging_idempotent_and_no_double_print():
    """configure_logging is idempotent (replaces its own handlers, never duplicates) and sets
    propagate=False so a line is not re-emitted by the notebook root logger (no double print)."""
    from src.utils import logger as lg

    _reset_wsmig_logger()
    buf1 = io.StringIO()
    lg.configure_logging(run_id="r1", stage="IMPORT", level="DEBUG", capture=False, stream=buf1)
    app = logging.getLogger("wsmig")
    n_after_first = len(app.handlers)
    assert app.propagate is False

    # Re-configure: must NOT accumulate handlers (idempotent), and must retarget cleanly.
    buf2 = io.StringIO()
    lg.configure_logging(run_id="r1", stage="IMPORT", level="DEBUG", capture=False, stream=buf2)
    assert len(app.handlers) == n_after_first, "re-configure duplicated handlers"

    log = lg.get_log("mod.x")
    log.info("hello once")
    assert buf2.getvalue().count("hello once") == 1, "line printed more than once"
    assert buf1.getvalue().count("hello once") == 0, "old handler not removed on re-configure"


def test_b5_context_filter_stamps_run_id_and_stage():
    """Every line carries run_id + stage from the mutable context."""
    from src.utils import logger as lg

    _reset_wsmig_logger()
    buf = io.StringIO()
    lg.configure_logging(run_id="RUN42", stage="EXPORT", level="DEBUG", capture=False, stream=buf)
    lg.get_log("collectors.jobs").warning("listing jobs")
    out = buf.getvalue()
    assert "run=RUN42" in out and "stage=EXPORT" in out
    assert "[wsmig.collectors.jobs]" in out
    # set_context updates subsequent lines.
    lg.set_context(stage="IMPORT")
    lg.get_log("x").info("now importing")
    assert "stage=IMPORT" in buf.getvalue().splitlines()[-1]


def test_b5_redact_filter_scrubs_registered_secret():
    """A registered secret value never appears in an emitted line."""
    from src.utils import logger as lg

    _reset_wsmig_logger()
    buf = io.StringIO()
    lg.configure_logging(run_id="r", stage="s", level="DEBUG", capture=False, stream=buf)
    lg.register_secret("sup3r-s3cret-value")
    lg.get_log("auth").debug("using secret sup3r-s3cret-value for M2M")
    out = buf.getvalue()
    assert "sup3r-s3cret-value" not in out
    assert "***REDACTED***" in out


def test_b5_live_stdout_handler_resolves_current_stdout_per_record():
    """_LiveStdoutHandler.emit resolves sys.stdout at emit time (not construction) and flushes, so
    lines land in the CURRENTLY running cell — the crux of live cell visibility."""
    import sys
    from src.utils import logger as lg

    _reset_wsmig_logger()
    lg.configure_logging(run_id="r", stage="s", level="DEBUG", capture=False)  # live handler

    class _Spy(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flushed = False

        def flush(self):
            self.flushed = True

    cell_a, cell_b = _Spy(), _Spy()
    real = sys.stdout
    try:
        sys.stdout = cell_a
        lg.get_log("x").info("in cell A")
        sys.stdout = cell_b
        lg.get_log("x").info("in cell B")
    finally:
        sys.stdout = real
    assert "in cell A" in cell_a.getvalue() and "in cell A" not in cell_b.getvalue()
    assert "in cell B" in cell_b.getvalue() and "in cell B" not in cell_a.getvalue()
    assert cell_a.flushed and cell_b.flushed, "handler must flush per record (stuck-run visibility)"


def test_b5_pin_stdout_routes_worker_lines_to_pinned_stream():
    """pin_stdout pins the live handler to a captured stream so lines emitted while another
    sys.stdout is active (a worker thread's thread-local stdout) still reach the cell stream."""
    import sys
    from src.utils import logger as lg

    _reset_wsmig_logger()
    lg.configure_logging(run_id="r", stage="s", level="DEBUG", capture=False)
    cell = io.StringIO()
    worker_local = io.StringIO()
    real = sys.stdout
    try:
        sys.stdout = cell
        with lg.pin_stdout():            # captures `cell`
            sys.stdout = worker_local    # simulate a worker's thread-local stdout
            lg.get_log("worker").info("item-7 done")
    finally:
        sys.stdout = real
    assert "item-7 done" in cell.getvalue()
    assert "item-7 done" not in worker_local.getvalue()


def test_b5_capture_buffer_returns_full_run_log():
    """configure_logging(capture=True) returns a StringIO; get_captured_log() yields the text."""
    from src.utils import logger as lg

    _reset_wsmig_logger()
    buf = lg.configure_logging(run_id="r", stage="s", level="DEBUG", capture=True)
    assert isinstance(buf, io.StringIO)
    lg.get_log("x").info("captured line")
    assert "captured line" in lg.get_captured_log()
    assert lg.get_log_buffer() is buf


def test_b5_structured_logger_facade_still_streams_and_mirrors(tmp_path):
    """The kept get_logger(name).info(msg, **fields) façade routes to the cell AND the JSON file
    mirror (back-compat for the ~65 call sites + the file-mirror tests)."""
    import json
    import os
    from src.utils import logger as lg

    _reset_wsmig_logger()
    buf = io.StringIO()
    lg.configure_logging(run_id="r", stage="s", level="DEBUG", capture=False, stream=buf)
    dest = os.path.join(tmp_path, "execution.log")
    lg.set_log_file(dest)
    try:
        log = lg.get_logger("probe")
        log.info("did a thing", n=3)
        lg.flush_log_file()
    finally:
        lg.set_log_file(None)
    # cell stream got the greppable line with fields appended
    assert "did a thing" in buf.getvalue() and "n=3" in buf.getvalue()
    # file mirror got the JSON record
    rec = json.loads(open(dest, encoding="utf-8").read().strip())
    assert rec["msg"] == "did a thing" and rec["n"] == 3 and rec["level"] == "INFO"


# ─────────────────────────────── B13 — catalog rename helpers ──────────────────────────────

def test_b13_parse_catalog_mapping_flat_and_blank():
    from src.utils.helpers import parse_catalog_mapping
    assert parse_catalog_mapping("") == {}
    assert parse_catalog_mapping('{"src":"tgt"}') == {"src": "tgt"}
    # nested {"catalogs": {...}} is also accepted (forgiveness)
    assert parse_catalog_mapping('{"catalogs": {"a":"b"}}') == {"a": "b"}


def test_b13_parse_catalog_mapping_rejects_blank_and_collisions():
    from src.utils.helpers import parse_catalog_mapping
    with pytest.raises(ValueError):
        parse_catalog_mapping('{"src":""}')
    with pytest.raises(ValueError):
        parse_catalog_mapping('{"":"tgt"}')
    with pytest.raises(ValueError):
        parse_catalog_mapping('{"a":"z","b":"z"}')   # two sources → one target


def test_b13_remap_catalog_refs_token_boundary_safe():
    from src.utils.helpers import remap_catalog_refs
    m = {"src": "tgt"}
    # qualifier, backticked, bare standalone — all rewritten
    assert remap_catalog_refs("SELECT * FROM src.sch.tbl", m) == "SELECT * FROM tgt.sch.tbl"
    assert remap_catalog_refs("USE CATALOG `src`", m) == "USE CATALOG `tgt`"
    assert remap_catalog_refs("USE CATALOG src", m) == "USE CATALOG tgt"
    # a longer identifier merely CONTAINING src is NOT touched
    assert remap_catalog_refs("SELECT * FROM src_archive.s.t", m) == "SELECT * FROM src_archive.s.t"
    assert remap_catalog_refs("SELECT * FROM mysrc.s.t", m) == "SELECT * FROM mysrc.s.t"
    # blank mapping → byte-identical
    assert remap_catalog_refs("src.a.b", {}) == "src.a.b"


def test_b13_config_parses_catalog_mapping_widget():
    from src.config.config_manager import Config, STAGE_IMPORT

    class _W:
        def __init__(self, vals):
            self.vals = vals

        class widgets:  # noqa: N801
            pass

    vals = {
        "connectivity_mode": "airgap", "source_workspace_id": "123",
        "staging_location": "/Volumes/c/s/v", "dry_run": "true",
        "catalog_mapping_json": '{"old_cat":"new_cat"}',
    }

    class _DB:
        class widgets:  # noqa: N801
            @staticmethod
            def get(name):
                return vals.get(name, "")

    cfg = Config.from_dbutils(_DB(), None, stage=STAGE_IMPORT)
    assert cfg.catalog_mapping == {"old_cat": "new_cat"}
    assert cfg.log_level == "DEBUG" and cfg.parallel_threads == 1
    # the resolved mapping is auditable in config_resolved.json (redacted() keeps it)
    assert cfg.redacted()["catalog_mapping"] == {"old_cat": "new_cat"}


# ─────────────────────────── B4 — workspace-conf read-back ──────────────────────────────────

def _import_test_helpers():
    import importlib
    return importlib.import_module("tests.test_importers_phase2_5")


def test_b4_conf_key_honoured_is_created_and_verified():
    """A conf key the platform honours is Created with a 'verified by read-back' note."""
    h = _import_test_helpers()
    from src.importers.misc_importer import MiscImporter
    client = h.RecordingClient()
    imp, st = h._make(MiscImporter, [
        h._unit("workspace_conf", "enableWebTerminal",
                {"key": "enableWebTerminal", "value": "false"})], client)
    res = imp.run()
    assert res.created == 1 and res.failed == 0
    unit_row = next(u for u in res.units if u["asset_type"] == "workspace_conf")
    assert "verified by read-back" in unit_row["note"]


def test_b4_silently_dropped_conf_key_is_FAILED_and_fingerprint_not_advanced():
    """PLAN 13 B4: PATCH returns 200 but a read-back shows the value unchanged → FAILED (observed
    vs intended), category not_applied, and the state fingerprint is NOT advanced so a re-run
    re-attempts it (never self-conceals as 'done')."""
    h = _import_test_helpers()
    from src.importers.misc_importer import MiscImporter
    from src.state.state_store import CAT_NOT_APPLIED
    client = h.RecordingClient()
    client.drop_conf_keys = {"enableExportNotebook"}   # simulate the silent drop (200, no change)
    imp, st = h._make(MiscImporter, [
        h._unit("workspace_conf", "enableExportNotebook",
                {"key": "enableExportNotebook", "value": "false"})], client)
    res = imp.run()
    assert res.failed == 1 and res.created == 0
    row = st.row("workspace_conf", "enableExportNotebook")
    assert row["failure_category"] == CAT_NOT_APPLIED
    assert "read-back" in row["last_error"] and "did NOT honour" in row["last_error"]
    # fingerprint NOT advanced on a FAILED outcome → next run sees "moved" and retries
    assert row.get("last_source_fingerprint", "") in ("", None)


def test_b4_verify_applied_helper():
    from src.importers.base_importer import verify_applied
    ok, observed = verify_applied(lambda: "false", "false")
    assert ok and observed == "false"
    ok, observed = verify_applied(lambda: "true", "false")
    assert not ok and observed == "true"
    # a getter that raises is a verification failure, not a crash
    def _boom():
        raise RuntimeError("boom")
    ok, observed = verify_applied(_boom, "false")
    assert not ok and "read-back failed" in observed


# ─────────────────────────── B11 — loud report-write failure ────────────────────────────────

def _empty_runnable_bundle(tmp, run_id="r1"):
    from src.config.config_manager import Config
    from src.exporters import bundle_paths as BP
    from src.exporters.artifact_writer import ArtifactWriter
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": run_id,
                            "target_staging_location": tmp})
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    aw.write_json(BP.EXPORT_INDEX_JSON, {"units": []})
    aw.write_manifest({})
    return cfg, aw


def test_b11_report_write_failure_marks_run_not_cleanly_complete(tmp_path):
    """A clean import whose report write fails must NOT present as success: run_status flips to
    completed_no_report and the reason is carried (ties B4)."""
    from src.importers.import_runner import ImportRunner
    cfg, aw = _empty_runnable_bundle(str(tmp_path))
    runner = ImportRunner(object(), cfg, aw, state=None)

    def _boom(summary):
        raise RuntimeError("volume flush stalled")
    runner._write_reports = _boom
    out = runner.run()
    assert out["run_status"] == "completed_no_report"
    assert "volume flush stalled" in out["report_write_error"]


def test_b11_report_verify_catches_missing_xlsx(tmp_path):
    """Even if _write_reports does not raise, a missing/empty xlsx is caught by the post-write
    verification and flips the run to completed_no_report."""
    from src.importers.import_runner import ImportRunner
    cfg, aw = _empty_runnable_bundle(str(tmp_path))
    runner = ImportRunner(object(), cfg, aw, state=None)

    def _claims_but_writes_nothing(summary):
        summary["reports"] = {"xlsx": "reports/does_not_exist.xlsx"}
    runner._write_reports = _claims_but_writes_nothing
    out = runner.run()
    assert out["run_status"] == "completed_no_report"


def test_b11_healthy_run_still_writes_report_and_stays_clean(tmp_path):
    """The success path is unchanged — a real report is written and the run is `completed`."""
    import os
    from src.importers.import_runner import ImportRunner
    from src.exporters import bundle_paths as BP
    cfg, aw = _empty_runnable_bundle(str(tmp_path))
    runner = ImportRunner(object(), cfg, aw, state=None)
    out = runner.run()
    assert out["run_status"] == "completed"
    xlsx = (out.get("reports") or {}).get("xlsx") or BP.IMPORT_STATUS_DRYRUN_XLSX
    assert os.path.isfile(os.path.join(aw.root, xlsx))


def test_b11_aborting_run_reraises_original_error_even_if_report_also_fails(tmp_path):
    """An already-aborting run must re-raise its ORIGINAL error; a report failure never masks it."""
    from src.importers.import_runner import ImportRunner, BundleVerificationError
    from src.exporters import bundle_paths as BP
    cfg, aw = _empty_runnable_bundle(str(tmp_path))
    # Corrupt the manifest so verify_bundle aborts (the original error).
    aw.write_json(BP.MANIFEST_JSON,
                  {"files": [{"path": "export/gone.json", "bytes": 5, "sha256": "deadbeef"}],
                   "tool_version": "0.1.0"})
    runner = ImportRunner(object(), cfg, aw, state=None)

    def _boom(summary):
        raise RuntimeError("report also failed")
    runner._write_reports = _boom
    with pytest.raises(BundleVerificationError, match="failed its manifest check"):
        runner.run()


# ─────────────────────────── B9 — run_id = job run id ───────────────────────────────────────

def test_b9_shipped_jobs_pin_run_id_to_job_run_id():
    """Every shipped job's notebook tasks carry run_id='{{job.run_id}}' so the bundle dir name
    equals the Jobs-UI run id (correlatable, repair-stable)."""
    import glob
    import json
    import os
    jobs_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "jobs")
    files = glob.glob(os.path.join(jobs_dir, "*.job.json"))
    assert files, "no job templates found"
    seen = 0
    for p in files:
        spec = json.load(open(p, encoding="utf-8"))
        for task in spec.get("tasks", []):
            bp = (task.get("notebook_task") or {}).get("base_parameters") or {}
            if "run_id" in bp:
                assert bp["run_id"] == "{{job.run_id}}", f"{os.path.basename(p)} run_id not pinned"
                seen += 1
    assert seen >= 6, "expected run_id pinned across all task definitions"


def test_b9_installer_leaves_job_run_id_reference_intact():
    """render_template treats {{job.run_id}} as a pinned non-blank default (a platform dynamic
    reference, not an installer {{UPPER}} token) and leaves it intact — never overwritten by the
    installer's blank run_id, never flagged as an unfilled placeholder."""
    import os
    from src.utils.job_templates import load_template, render_template
    jobs_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "jobs")
    t = load_template(os.path.join(jobs_dir, "import.job.json"))
    spec = render_template(
        t, tokens={"REPO_PATH": "/Repos/me/wsmig", "RUN_AS_SP": "sp"},
        params={"connectivity_mode": "direct", "source_workspace_id": "9",
                "staging_location": "/Volumes/a/b/c", "run_id": "",
                "state_catalog": "c", "state_schema": "s"},
        run_as={"service_principal_name": "sp"})
    bp = spec["tasks"][0]["notebook_task"]["base_parameters"]
    assert bp["run_id"] == "{{job.run_id}}"


def test_b9_manifest_missing_names_latest_export(tmp_path):
    """Pointing import at a run dir whose export never completed (no manifest.json) yields an
    ACTIONABLE message naming the last COMPLETED export (LATEST_EXPORT), not a bare manifest error."""
    from src.config.config_manager import Config
    from src.exporters import bundle_paths as BP
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.bundle_state import write_latest_export_pointer
    from src.importers.import_runner import ImportRunner, BundleVerificationError

    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "incomplete",
                            "target_staging_location": str(tmp_path)})
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()           # a run dir with NO manifest.json
    # a DIFFERENT, completed export is recorded as the latest pointer
    write_latest_export_pointer(cfg, "20260101_good", {"tool_version": "0.1.0"}, {})
    runner = ImportRunner(object(), cfg, aw, state=None)
    with pytest.raises(BundleVerificationError, match="20260101_good"):
        runner.verify_bundle()


# ─────────────────────── B8 — home provisioning (defer + re-sweep) ──────────────────────────

def test_b8_home_content_that_appears_at_resweep_is_imported_no_mkdir_of_home():
    """A home-content unit whose home is ABSENT during the main loop is DEFERRED (not failed), then
    imported by the end-of-phase re-sweep once the home has lazily appeared — and the tool NEVER
    mkdirs the protected `/Users/<owner>` home."""
    h = _import_test_helpers()
    from src.importers.workspace_importer import WorkspaceImporter
    home = "/Users/late@x.com"

    class _LazyHomeClient(h.RecordingClient):
        """The home 'provisions' lazily: get-status on it is absent during the main loop and present
        only once the importer is in its re-sweep (checked via the importer's _in_resweep flag)."""
        def __init__(self, **kw):
            super().__init__(**kw)
            self.importer = None

        def get(self, path, params=None):
            if path == "api/2.0/workspace/get-status" and (params or {}).get("path") == home:
                self.calls.append(("GET", path, params))
                if self.importer is not None and self.importer._in_resweep:
                    return {"path": home, "object_type": "DIRECTORY", "object_id": "home-1"}
                raise RuntimeError("RESOURCE_DOES_NOT_EXIST")
            return super().get(path, params)

    client = _LazyHomeClient()
    imp, st = h._make(WorkspaceImporter, [
        h._unit("notebook", f"{home}/nb", {"path": f"{home}/nb", "language": "PYTHON"},
                content_ref="c/nb.py")], client, staging_files={"c/nb.py": b"print(1)"},
        identity_map={"sp_mapping": {}})
    # owner IS in the source roster → the resolver DEFERS rather than diverts to backup
    imp.staging.write_json(__import__("src.exporters.bundle_paths", fromlist=["x"])
                           .IDENTITY_CLASSIFICATION_JSON,
                           {"identities": [{"identity_type": "user", "userName": "late@x.com",
                                            "email": "late@x.com"}]})
    client.importer = imp
    res = imp.run()
    assert res.failed == 0, "the deferred unit must heal in the re-sweep once the home appears"
    assert (res.created + res.warned + res.updated + res.adopted) == 1
    imported = [b["path"] for b in client.bodies_to("workspace/import")]
    assert f"{home}/nb" in imported
    assert all(b.get("path") != home for b in client.bodies_to("workspace/mkdirs")), \
        "must NEVER mkdir a protected /Users/<owner> home"


def test_b8_home_still_absent_at_resweep_is_clean_prerequisite_not_mkdird():
    """A home that never appears → the deferred unit becomes ONE clean prerequisite_missing (healed
    later by failed_only), and the home is never mkdir'd."""
    h = _import_test_helpers()
    from src.importers.workspace_importer import WorkspaceImporter
    client = h.RecordingClient()   # get-status always raises → home never appears
    imp, st = h._make(WorkspaceImporter, [
        h._unit("notebook", "/Users/live@x.com/nb",
                {"path": "/Users/live@x.com/nb", "language": "PYTHON"}, content_ref="c/nb.py")],
        client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}})
    imp.staging.write_json(__import__("src.exporters.bundle_paths", fromlist=["x"])
                           .IDENTITY_CLASSIFICATION_JSON,
                           {"identities": [{"identity_type": "user", "userName": "live@x.com",
                                            "email": "live@x.com"}]})
    res = imp.run()
    assert res.failed == 1
    row = st.row("notebook", "/Users/live@x.com/nb")
    assert row["failure_category"] == "prerequisite_missing"
    assert client.posts_to("workspace/mkdirs") == []


def test_b8_home_present_caches_within_a_pass_and_resweep_reset_re_detects():
    """_home_present caches the verdict (present AND absent) WITHIN a pass so a 10K-object content
    phase makes ~1 probe per owner, not one per unit (the probe explosion that caused a 7h phase).
    A home that provisions LATER is re-detected because the re-sweep RESETS `_home_present_cache` —
    not by re-probing on every call within the same pass."""
    h = _import_test_helpers()
    from src.importers.workspace_importer import WorkspaceImporter
    home = "/Users/appears@x.com"

    class _Flip(h.RecordingClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.present = False
            self.probes = 0

        def get(self, path, params=None):
            if path == "api/2.0/workspace/get-status" and (params or {}).get("path") == home:
                self.probes += 1
                if self.present:
                    return {"path": home, "object_id": "h1"}
                raise RuntimeError("RESOURCE_DOES_NOT_EXIST")
            return super().get(path, params)

    client = _Flip()
    imp, _st = h._make(WorkspaceImporter, [], client, identity_map={"sp_mapping": {}})
    assert imp._home_present(home) is False
    # Within the SAME pass the absent verdict is cached — repeated checks do NOT re-probe (this is
    # the fix: ~1 probe per owner, not per unit). Even if the home appears, the cached pass-verdict
    # stands until the pass ends; the deferred unit is healed by the re-sweep, not mid-pass.
    client.present = True
    for _ in range(5):
        assert imp._home_present(home) is False
    assert client.probes == 1, "absent must be cached within a pass (one probe, not one per call)"
    # The end-of-phase re-sweep resets the cache (see _resweep_deferred_homes) → fresh re-detect.
    imp._home_present_cache = {}
    assert imp._home_present(home) is True
    assert client.probes == 2


def test_b8_runner_resweeps_home_content_just_before_acls():
    """B8 (refined): the ImportRunner defers the home-content re-sweep to just BEFORE the ACL phase
    (max lazy-provisioning time) rather than at the end of the workspace phase — a home that appears
    by then is healed, and its content is created before ACLs so permissions still apply."""
    import os
    import tempfile
    from src.config.config_manager import Config
    from src.exporters import bundle_paths as BP
    from src.exporters.artifact_writer import ArtifactWriter
    from src.importers.import_runner import ImportRunner

    tmp = tempfile.mkdtemp()
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r1",
                            "target_staging_location": tmp, "dry_run": False,
                            "imports": {"import_assets": ["identity", "workspace", "acls"]}})
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    aw.write_manifest({})
    # a single home-content unit under a home that is absent during the workspace phase
    nb = {"asset_type": "notebook", "natural_key": "/Users/late@x.com/nb", "source_id": "n1",
          "fingerprint": "f1", "import_action": "create", "export_status": "success",
          "content_ref": "c/nb.py", "payload": {"path": "/Users/late@x.com/nb", "language": "PYTHON"}}
    aw.write_json(BP.EXPORT_INDEX_JSON, {"units": [dict(nb, **{"payload": None})]})
    from src.exporters import asset_export
    aw.write_json(asset_export.ARTIFACT_PATH["notebook"], {"units": [nb]})
    aw.write_bytes("c/nb.py", b"print(1)")
    aw.write_json(BP.IDENTITY_CLASSIFICATION_JSON, {"identities": [
        {"identity_type": "user", "userName": "late@x.com", "email": "late@x.com"}]})

    order = []

    class _Client:
        base_url = "https://t"

        def __init__(self):
            self.home_live = False

        def get(self, path, params=None):
            if path == "api/2.0/workspace/get-status":
                p = (params or {}).get("path")
                # Only the HOME ROOT lazily appears (home_live); the notebook itself never pre-exists,
                # so it is CREATED (not adopted) once its home is present.
                if p == "/Users/late@x.com" and self.home_live:
                    return {"path": p, "object_id": "home1"}
                raise RuntimeError("RESOURCE_DOES_NOT_EXIST")
            return {}

        def get_paginated(self, *a, **k):
            return []

        def get_scim(self, *a, **k):
            return []

        def post(self, path, body):
            order.append(("POST", path))
            if path == "api/2.0/workspace/mkdirs":
                # the home provisioned by the time the re-sweep mkdir's content's parent chain
                self.home_live = True
            return {}

        def put(self, path, body):
            order.append(("PUT", path))
            return {}

        def patch(self, path, body, params=None):
            return {}

    client = _Client()
    runner = ImportRunner(client, cfg, aw, state=None)
    # Deterministic lazy-provisioning: the home is ABSENT during the workspace phase (notebook is
    # deferred), and "appears" exactly when the pre-ACL re-sweep runs — hook the re-sweep to flip it.
    _real = runner._resweep_home_content

    def _hooked(ubt):
        order.append(("RESWEEP", "start"))
        client.home_live = True
        return _real(ubt)
    runner._resweep_home_content = _hooked
    runner.run()

    resweep_at = next(i for i, (v, p) in enumerate(order) if v == "RESWEEP")
    imports = [i for i, (verb, p) in enumerate(order) if p == "api/2.0/workspace/import"]
    assert imports, "the deferred home notebook must be imported by the pre-ACL re-sweep"
    # the notebook was NOT imported during the workspace phase (it was deferred) — only after the
    # re-sweep started.
    assert min(imports) > resweep_at, "home content must be deferred, not created in the workspace phase"


def test_b8_genuinely_absent_owner_backup_off_is_immediate_prerequisite_not_deferred():
    """An owner DELETED in source (absent from roster) with backup OFF is an IMMEDIATE prerequisite —
    not deferred/re-swept (its home will never appear)."""
    h = _import_test_helpers()
    from src.importers.workspace_importer import WorkspaceImporter
    from src.exporters import bundle_paths as BP
    client = h.RecordingClient()
    imp, st = h._make(WorkspaceImporter, [
        h._unit("directory", "/Users/ghost@x.com", {"path": "/Users/ghost@x.com"})], client,
        identity_map={"sp_mapping": {}}, imports_extra={"workspace_home_backup": False})
    imp.staging.write_json(BP.IDENTITY_CLASSIFICATION_JSON,
                           {"identities": [{"identity_type": "user", "userName": "someone@x.com",
                                            "email": "someone@x.com"}]})
    # resolver → immediate prerequisite (NOT defer): nothing is parked for the re-sweep
    res = imp.run()
    assert res.failed == 1 and imp._deferred_units == []
    assert st.row("directory", "/Users/ghost@x.com")["failure_category"] == "prerequisite_missing"


# ─────────────────────────── B10 — .db_internal ACL skip ────────────────────────────────────

def test_b10_db_internal_acl_is_skipped_not_a_permission_denied():
    """A `.db_internal` directory ACL is SKIPPED (not_supported), like /Shared — never a 403 that
    failed_only re-attempts forever; a NON-internal directory ACL still applies."""
    h = _import_test_helpers()
    from src.importers.acl_importer import AclImporter
    from src.state.state_store import CAT_NOT_SUPPORTED
    acls = [
        {"asset_type": "directory", "natural_key": "/Users/u@x.com/.db_internal",
         "perm_object_type": "directories", "source_id": "d1",
         "grants": [{"principal": "u@x.com", "principal_type": "user",
                     "permission_level": "CAN_MANAGE"}]},
        {"asset_type": "directory", "natural_key": "/Users/u@x.com/real",
         "perm_object_type": "directories", "source_id": "d2",
         "grants": [{"principal": "u@x.com", "principal_type": "user",
                     "permission_level": "CAN_MANAGE"}]},
    ]
    client = h.RecordingClient(status_paths={"/Users/u@x.com/.db_internal", "/Users/u@x.com/real"})
    imp, st = h._make(AclImporter, [], client,
                      staging_files={"export/acls.json": __import__("json").dumps(acls).encode()},
                      context={"workspace_path_remap": {}})
    res = imp.run()
    internal = st.row("acl", "directories:/Users/u@x.com/.db_internal")
    assert internal["last_action"] == "skipped_no_object"
    assert internal["failure_category"] == CAT_NOT_SUPPORTED
    # the real directory's ACL was still PUT
    puts = [c for c in client.calls if c[0] == "PUT" and "permissions/directories" in c[1]]
    assert puts, "a non-internal directory ACL must still be applied"


def test_b10_collector_does_not_fetch_acl_for_internal_paths():
    """The workspace collector does not fetch_acl for `.db_internal`/`.ide`/`.databricks` paths."""
    from src.collectors.workspace_collector import WorkspaceCollector
    calls = []

    class _C(WorkspaceCollector):
        def __init__(self):
            self.fetched = calls

        def fetch_acl(self, perm_type, object_id):  # noqa: D401
            calls.append((perm_type, object_id))
            return []
    c = _C()
    assert c._object_acl("DIRECTORY", "1", "/Users/u@x.com/.db_internal") is None
    assert c._object_acl("DIRECTORY", "2", "/Users/u@x.com/.ide/x") is None
    assert c._object_acl("DIRECTORY", "3", "/Users/u@x.com/real") == []
    assert ("directories", "3") in calls and ("directories", "1") not in calls


# ─────────────────────────── B3 — cluster-policy family shape ───────────────────────────────

def test_b3_family_policy_sends_family_id_not_definition():
    """A policy built on a policy FAMILY must send policy_family_id (+ overrides) and NOT the
    resolved definition (the two together → 400). A custom policy sends definition."""
    h = _import_test_helpers()
    from src.importers.compute_importer import ComputeImporter
    client = h.RecordingClient()
    family = h._unit("cluster_policy", "job-pol", {
        "name": "job-pol", "policy_family_id": "job-cluster",
        "policy_family_definition_overrides": {"autotermination_minutes": {"type": "fixed",
                                                                           "value": 30}},
        "definition": {"spark_conf.x": {"type": "fixed", "value": "y"}}})   # resolved — must drop
    custom = h._unit("cluster_policy", "custom-pol", {
        "name": "custom-pol", "definition": {"node_type_id": {"type": "fixed", "value": "x"}}})
    imp, _st = h._make(ComputeImporter, [family, custom], client)
    imp.run()
    bodies = {b.get("name"): b for b in client.bodies_to("policies/clusters/create")}
    assert "policy_family_id" in bodies["job-pol"] and "definition" not in bodies["job-pol"]
    assert bodies["job-pol"]["policy_family_id"] == "job-cluster"
    assert "policy_family_definition_overrides" in bodies["job-pol"]
    # custom policy: the reverse
    assert "definition" in bodies["custom-pol"] and "policy_family_id" not in bodies["custom-pol"]


def test_b3_family_policy_edit_also_drops_definition():
    """update_one mirrors the create shape — a family policy edit never sends definition."""
    h = _import_test_helpers()
    from src.importers.compute_importer import ComputeImporter
    client = h.RecordingClient()
    imp, _st = h._make(ComputeImporter, [], client)
    unit = h._unit("cluster_policy", "job-pol", {
        "name": "job-pol", "policy_family_id": "job-cluster",
        "policy_family_definition_overrides": {"a": {"type": "fixed", "value": 1}},
        "definition": {"b": {"type": "fixed", "value": 2}}})
    imp.update_one(unit, "pol-99")
    edit = client.bodies_to_edit = [c[2] for c in client.calls
                                    if c[0] == "POST" and c[1].endswith("policies/clusters/edit")]
    assert edit and "policy_family_id" in edit[0] and "definition" not in edit[0]


# ─────────────────────────────── B2 — owner preservation ───────────────────────────────────

class _QueryOwnerClient:
    """A minimal client modelling the Queries owner mechanism: create attributes to the run-as SP;
    a PATCH owner_user_name succeeds only if the principal exists on target (else rejected); a GET
    reflects the stored owner (so the B2/B4 read-back works)."""
    base_url = "https://target.example.net"

    def __init__(self, known_users):
        self.known_users = set(known_users)
        self._owner = {}
        self.calls = []
        self._n = 0

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path.startswith("api/2.0/sql/queries/"):
            qid = path.rsplit("/", 1)[-1]
            return {"id": qid, "owner_user_name": self._owner.get(qid, "run-as-sp")}
        return {}

    def get_paginated(self, path, result_key, token_key="next_page_token", params=None,
                      max_pages=100000):
        return []

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        self._n += 1
        qid = f"q-{self._n}"
        if path == "api/2.0/sql/queries":
            self._owner[qid] = "run-as-sp"
        return {"id": qid}

    def patch(self, path, body, params=None):
        self.calls.append(("PATCH", path, body, params))
        if path.startswith("api/2.0/sql/queries/"):
            qid = path.rsplit("/", 1)[-1]
            owner = (body.get("query") or {}).get("owner_user_name")
            if owner is not None:
                if owner not in self.known_users:
                    raise RuntimeError(f"owner {owner} does not exist on target")
                self._owner[qid] = owner
        return {}


def test_b2_query_owner_is_set_to_remapped_source_owner_and_verified():
    """An in-roster source owner is transferred to the target query (verified by read-back)."""
    h = _import_test_helpers()
    from src.importers.sql_importer import SqlImporter
    client = _QueryOwnerClient(known_users={"alice@x.com"})
    unit = h._unit("legacy_query", "/Users/alice@x.com/q1",
                   {"display_name": "q1", "query_text": "select 1"}, source_owner="alice@x.com")
    imp, _st = h._make(SqlImporter, [unit], client)
    res = imp.run()
    assert res.created == 1 and res.failed == 0 and res.warned == 0
    patches = [c for c in client.calls if c[0] == "PATCH" and "sql/queries/" in c[1]]
    assert patches and (patches[0][2].get("query") or {}).get("owner_user_name") == "alice@x.com"


def test_b2_orphaned_owner_falls_back_to_run_as_sp_not_a_failure():
    """A source owner ABSENT from the target roster (left the org) → the query is left owned by the
    run-as SP with a WARNING — never a failure, never a non-existent principal."""
    h = _import_test_helpers()
    from src.importers.sql_importer import SqlImporter
    client = _QueryOwnerClient(known_users=set())    # the owner does NOT exist on target
    unit = h._unit("legacy_query", "/Users/gone@x.com/q2",
                   {"display_name": "q2", "query_text": "select 1"}, source_owner="gone@x.com")
    imp, _st = h._make(SqlImporter, [unit], client)
    res = imp.run()
    assert res.failed == 0, "an orphaned owner must never fail the query"
    assert res.warned == 1, "it is created_with_warning (left owned by the run-as SP)"
    unit_row = next(u for u in res.units if u["asset_type"] == "legacy_query")
    assert "run-as SP" in unit_row["note"] or "run-as SP" in str(unit_row.get("note"))


def test_b2_jobs_keep_owner_via_is_owner_grant_in_acl_body():
    """jobs/pipelines/warehouses keep their source owner via the ACL phase's IS_OWNER grant — the
    grant travels verbatim into the PUT permissions body (principal remapped)."""
    import json
    h = _import_test_helpers()
    from src.importers.acl_importer import AclImporter
    acls = [{"asset_type": "job", "natural_key": "etl", "perm_object_type": "jobs", "source_id": "j1",
             "grants": [{"principal": "owner@x.com", "principal_type": "user",
                         "permission_level": "IS_OWNER"},
                        {"principal": "team@x.com", "principal_type": "user",
                         "permission_level": "CAN_MANAGE"}]}]
    client = h.RecordingClient()
    imp, _st = h._make(AclImporter, [], client,
                       staging_files={"export/acls.json": json.dumps(acls).encode()},
                       context={"job_target_ids": {"etl": "job-99"}})
    imp.run()
    puts = [c[2] for c in client.calls if c[0] == "PUT" and "permissions/jobs/job-99" in c[1]]
    assert puts, "the job ACL must be PUT"
    levels = {(g.get("user_name"), g["permission_level"]) for g in puts[0]["access_control_list"]}
    assert ("owner@x.com", "IS_OWNER") in levels, "IS_OWNER must travel verbatim to preserve owner"


# ─────────────────────────── B7 — dashboard publish parity ──────────────────────────────────

class _LakeviewClient:
    """Models the Lakeview publish + schedule surface for B7 reconcile tests."""
    base_url = "https://t"

    def __init__(self, published=None, publish_noop=False):
        self._pub = {k: dict(v) for k, v in (published or {}).items()}
        self.publish_noop = publish_noop   # POST /published returns 200 but doesn't change (silent)
        self.calls = []
        self._n = 0
        self._rev = 0

    def _did(self, path):
        return path.split("/")[4]

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path.endswith("/published"):
            p = self._pub.get(self._did(path))
            if not p:
                raise RuntimeError("404 RESOURCE_DOES_NOT_EXIST")
            return {"embed_credentials": p["embed_credentials"], "warehouse_id": p["warehouse_id"],
                    "revision_create_time": p.get("rev", "r1")}
        if path.endswith("/schedules"):
            return {"schedules": []}
        return {}

    def get_paginated(self, path, result_key, token_key=None, params=None, max_pages=100000):
        return []

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        self._n += 1
        if path == "api/2.0/lakeview/dashboards":
            return {"dashboard_id": f"D-{self._n}"}
        if path.endswith("/published"):
            if not self.publish_noop:
                self._rev += 1
                self._pub[self._did(path)] = {"embed_credentials": body["embed_credentials"],
                                              "warehouse_id": body["warehouse_id"],
                                              "rev": f"r{self._rev}"}
            return {}
        if path.endswith("/schedules"):
            return {"schedule_id": f"S-{self._n}"}
        return {"id": f"id-{self._n}"}

    def patch(self, path, body, params=None):
        self.calls.append(("PATCH", path, body, params))
        return {}

    def delete(self, path, body=None):
        self.calls.append(("DELETE", path, body))
        return {}


def _dash_imp(client, extra_units=None):
    h = _import_test_helpers()
    from src.importers.dashboards_importer import DashboardsImporter
    wh = h._unit("sql_warehouse", "wh", {}, source_id="w1")
    units = [wh] + list(extra_units or [])
    imp, st = h._make(DashboardsImporter, units, client,
                      context={"sql_warehouse_target_ids": {"wh": "W-TGT"}})
    return imp


def _dash_unit(**facets):
    h = _import_test_helpers()
    payload = {"display_name": "D", "serialized_dashboard": "{}", "warehouse_id": "w1",
               "published_warehouse_id": "w1"}
    payload.update(facets)
    return h._unit("lakeview_dashboard", "/Users/a@x.com/D", payload)


def _publish_posts(client):
    return [c for c in client.calls if c[0] == "POST" and c[1].endswith("/published")]


def test_b7_source_published_individual_publishes_target_embed_false():
    client = _LakeviewClient()
    imp = _dash_imp(client)
    note, warn = imp._reconcile_publish_and_schedules(
        _dash_unit(is_published=True, embed_credentials=False), "D1")
    posts = _publish_posts(client)
    assert posts and posts[0][2] == {"embed_credentials": False, "warehouse_id": "W-TGT"}
    assert "Individual" in note and not warn


def test_b7_source_published_shared_embeds_run_as_sp():
    client = _LakeviewClient()
    imp = _dash_imp(client)
    note, warn = imp._reconcile_publish_and_schedules(
        _dash_unit(is_published=True, embed_credentials=True), "D1")
    posts = _publish_posts(client)
    assert posts and posts[0][2]["embed_credentials"] is True
    assert "Shared" in note and "run-as SP" in note


def test_b7_publish_is_idempotent_diff_guard_no_republish():
    """Publish is NOT idempotent (a re-publish mints a new revision), so the reconcile must publish
    ONLY on an actual diff — target already matching → zero publish POSTs."""
    client = _LakeviewClient(published={"D1": {"embed_credentials": True, "warehouse_id": "W-TGT"}})
    imp = _dash_imp(client)
    note, warn = imp._reconcile_publish_and_schedules(
        _dash_unit(is_published=True, embed_credentials=True), "D1")
    assert _publish_posts(client) == [], "must not re-publish when nothing changed"
    assert "unchanged" in note


def test_b7_source_unpublish_is_flag_only_never_unpublishes_target():
    """Source draft-only but target still published → FLAG the drift; NEVER call DELETE /published."""
    client = _LakeviewClient(published={"D1": {"embed_credentials": False, "warehouse_id": "W-TGT"}})
    imp = _dash_imp(client)
    note, warn = imp._reconcile_publish_and_schedules(_dash_unit(is_published=False), "D1")
    assert warn and "does NOT unpublish" in warn
    assert not any(c[0] == "DELETE" for c in client.calls)
    assert _publish_posts(client) == []


def test_b7_publish_readback_mismatch_fails_the_row():
    """A publish that returns 200 but a read-back shows it did NOT take → VerificationFailed (atomic
    FAILED row), fingerprint not advanced (B4 ties)."""
    from src.importers.base_importer import VerificationFailed
    client = _LakeviewClient(publish_noop=True)   # POST 200 but state never changes
    imp = _dash_imp(client)
    with pytest.raises(VerificationFailed):
        imp._reconcile_publish_and_schedules(
            _dash_unit(is_published=True, embed_credentials=True), "D1")


def test_b7_schedule_recreated_with_remapped_warehouse():
    client = _LakeviewClient()
    imp = _dash_imp(client)
    unit = _dash_unit(is_published=False, schedules=[
        {"display_name": "daily", "cron_quartz": "0 0 9 * * ?", "timezone_id": "UTC",
         "pause_status": "UNPAUSED", "warehouse_id": "w1", "subscriptions": []}])
    note, warn = imp._reconcile_publish_and_schedules(unit, "D1")
    sched_posts = [c for c in client.calls if c[0] == "POST" and c[1].endswith("/schedules")]
    assert sched_posts and sched_posts[0][2]["warehouse_id"] == "W-TGT"
    assert sched_posts[0][2]["cron_schedule"]["quartz_cron_expression"] == "0 0 9 * * ?"
    assert "schedule(s) created" in note


def test_b7_full_create_publishes_and_is_created_status():
    client = _LakeviewClient()
    h = _import_test_helpers()
    imp = _dash_imp(client, extra_units=[
        _dash_unit(is_published=True, embed_credentials=True)])
    res = imp.run()
    assert res.created == 1 and res.failed == 0
    assert _publish_posts(client), "a published source must publish on target"


def test_b7_export_folds_publish_facets_into_fingerprint():
    """A publish-state change moves the dashboard fingerprint (so incremental runs detect it)."""
    from src.exporters.asset_export import _sql_units  # noqa: F401 (ensure module import)
    from src.exporters import asset_export
    base = {"dashboard_id": "d1", "display_name": "D", "warehouse_id": "w1",
            "serialized_dashboard": "{}", "parent_path": "/a", "deployed_by_dab": False,
            "is_published": False, "embed_credentials": False, "published_warehouse_id": "",
            "schedules": []}
    draft = asset_export._lakeview_units([dict(base)])[0]["fingerprint"]
    published = asset_export._lakeview_units([
        {**base, "is_published": True, "embed_credentials": True,
         "published_warehouse_id": "w1"}])[0]["fingerprint"]
    assert draft != published, "publishing a dashboard must move its fingerprint"


# ─────────────────────────── B13 — catalog remap in the 3 importers ─────────────────────────

def test_b13_dashboard_importer_remaps_catalog_in_serialized():
    client = _LakeviewClient()
    h = _import_test_helpers()
    from src.importers.dashboards_importer import DashboardsImporter
    wh = h._unit("sql_warehouse", "wh", {}, source_id="w1")
    unit = h._unit("lakeview_dashboard", "/a/D", {
        "display_name": "D", "warehouse_id": "w1",
        "serialized_dashboard": '{"datasets":[{"queryLines":["SELECT * FROM old_cat.s.t"]}]}'})
    imp, _st = h._make(DashboardsImporter, [wh, unit], client,
                       context={"sql_warehouse_target_ids": {"wh": "W-TGT"}},
                       imports_extra={})
    imp.config.catalog_mapping = {"old_cat": "new_cat"}
    body, _note = imp._body(unit)
    assert "new_cat.s.t" in body["serialized_dashboard"]
    assert "old_cat" not in body["serialized_dashboard"]


def test_b13_dashboard_blank_mapping_is_byte_identical():
    client = _LakeviewClient()
    h = _import_test_helpers()
    from src.importers.dashboards_importer import DashboardsImporter
    serialized = '{"datasets":[{"queryLines":["SELECT * FROM old_cat.s.t"]}]}'
    unit = h._unit("lakeview_dashboard", "/a/D",
                   {"display_name": "D", "serialized_dashboard": serialized})
    imp, _st = h._make(DashboardsImporter, [unit], client)
    body, _ = imp._body(unit)
    assert body["serialized_dashboard"] == serialized


def test_b13_genie_importer_remaps_catalog_in_serialized_space():
    h = _import_test_helpers()
    from src.importers.genie_importer import GenieImporter
    unit = h._unit("genie_space", "/a/G", {
        "title": "G", "serialized_space": '{"tables":["old_cat.s.t"]}'})
    imp, _st = h._make(GenieImporter, [unit], h.RecordingClient())
    imp.config.catalog_mapping = {"old_cat": "new_cat"}
    body, _ = imp._body(unit)
    assert "new_cat.s.t" in body["serialized_space"] and "old_cat" not in body["serialized_space"]


def test_b13_dlt_importer_remaps_structured_catalog_field():
    h = _import_test_helpers()
    from src.importers.dlt_importer import DltImporter
    unit = h._unit("dlt_pipeline", "bronze", {"name": "bronze", "catalog": "old_cat",
                                              "target": "sch"})
    imp, _st = h._make(DltImporter, [unit], h.RecordingClient())
    imp.config.catalog_mapping = {"old_cat": "new_cat"}
    spec, _warn = imp._spec(unit)
    assert spec["catalog"] == "new_cat" and spec.get("target") == "sch"


def test_b13_dlt_blank_mapping_leaves_catalog_unchanged():
    h = _import_test_helpers()
    from src.importers.dlt_importer import DltImporter
    unit = h._unit("dlt_pipeline", "bronze", {"name": "bronze", "catalog": "old_cat"})
    imp, _st = h._make(DltImporter, [unit], h.RecordingClient())
    spec, _ = imp._spec(unit)
    assert spec["catalog"] == "old_cat"


# ─────────────────────────── B6 — bounded enrichment parallelism (Scope 1) ──────────────────

def _ws_collector_fixture(threads):
    from tests.fakes import FakeClient
    from src.collectors.workspace_collector import WorkspaceCollector
    from src.config.config_manager import Config

    def ws_list(params):
        p = params.get("path")
        if p == "/":
            return {"objects": [{"path": "/Shared", "object_type": "DIRECTORY", "object_id": "1"}]}
        if p == "/Shared":
            return {"objects": [
                {"path": f"/Shared/nb{i}", "object_type": "NOTEBOOK", "language": "PYTHON",
                 "object_id": str(100 + i)} for i in range(12)]}
        return {"objects": []}

    gt = {"api/2.0/workspace/list": ws_list,
          "api/2.0/permissions/directories/1": {"access_control_list": []}}
    for i in range(12):
        gt[f"api/2.0/permissions/notebooks/{100 + i}"] = {
            "access_control_list": [{"group_name": f"g{i}",
                                     "all_permissions": [{"permission_level": "CAN_READ"}]}]}
    cfg = Config.from_dict({"role": "source", "source_workspace_id": "1",
                            "source_staging_location": "/tmp/x", "parallel_threads": threads})
    return WorkspaceCollector(FakeClient(get_table=gt), cfg)


def test_b6_parallel_acl_enrichment_is_byte_identical_to_serial():
    """parallel_threads>1 produces the SAME per-path ACLs as serial (threads=1): worker completion
    order is irrelevant because each worker fills its own record in place."""
    serial = {o["path"]: o["acl"] for o in _ws_collector_fixture(1).run()}
    parallel = {o["path"]: o["acl"] for o in _ws_collector_fixture(4).run()}
    assert serial == parallel and len(serial) == 13   # 1 dir + 12 notebooks
    # the notebook ACLs were actually fetched (not left None)
    assert serial["/Shared/nb0"][0]["group_name"] == "g0"


def test_b6_map_parallel_is_fail_soft():
    """A per-item enrichment failure is logged, never raised, and never aborts the pass."""
    from src.collectors.base_collector import BaseCollector

    class _C(BaseCollector):
        object_type = "x"

        def discover(self):
            return []
    c = _C(client=None, config=type("Cfg", (), {"parallel_threads": 4})())
    items = [{"n": i} for i in range(10)]

    def _fn(it):
        if it["n"] == 3:
            raise RuntimeError("boom")
        it["done"] = True
        return it
    c.map_parallel(items, _fn)   # must not raise
    assert sum(1 for it in items if it.get("done")) == 9


def test_b6_parallel_threads_defaults_to_serial_one():
    from src.config.config_manager import Config
    cfg = Config.from_dict({"role": "source", "source_workspace_id": "1",
                            "source_staging_location": "/tmp/x"})
    assert cfg.parallel_threads == 1    # safe default / kill-switch (byte-identical to today)


# ─────────────────────────── B1 — airgap source run_as SP ───────────────────────────────────

def test_b1_source_run_as_spn_only_on_airgap_source_job():
    from src.utils.job_templates import run_as_for_job
    # airgap + the airgap_source job → the SOURCE SP
    assert run_as_for_job("airgap_source", "airgap", "tgt-sp", "src-sp") == {
        "service_principal_name": "src-sp"}
    # direct mode → ignored (01/02 run in the target, read source over OAuth M2M)
    assert run_as_for_job("airgap_source", "direct", "tgt-sp", "src-sp") == {
        "service_principal_name": "tgt-sp"}
    # any OTHER job → always the target SP, even in airgap
    assert run_as_for_job("import", "airgap", "tgt-sp", "src-sp") == {
        "service_principal_name": "tgt-sp"}
    assert run_as_for_job("direct_end_to_end_live", "airgap", "tgt-sp", "src-sp") == {
        "service_principal_name": "tgt-sp"}
    # blank source SP → falls back to the target SP (never a blank run_as)
    assert run_as_for_job("airgap_source", "airgap", "tgt-sp", "") == {
        "service_principal_name": "tgt-sp"}


# ─────────────────────────── B6 Scope 2 — import sub-level parallelism ───────────────────────

def _toy_importer_cls():
    import threading as _th
    from src.importers.base_importer import BaseImporter
    from src.utils.helpers import safe_str

    class _ToyImporter(BaseImporter):
        component = "toy"
        asset_types = ("a", "b")

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.order = []          # (asset_type) in the order create_one ran
            self.order_lock = _th.Lock()
            self.saw_all_a_when_b = []   # per-b: how many 'a' were recorded in state at the time

        def load(self):
            return self.units_for("a", "b")

        def existing_keys(self):
            return {}

        def create_one(self, unit):
            at = safe_str(unit.get("asset_type"))
            with self.order_lock:
                self.order.append(at)
            if at == "b":
                # barrier check: by the time any 'b' runs, EVERY 'a' must be flushed into state.
                self.saw_all_a_when_b.append(len(self.state.target_ids_for("a")))
            return {"target_id": f"t-{self.natural_key(unit)}"}

    return _ToyImporter


def _toy_units(n_a, n_b):
    units = []
    for i in range(n_a):
        units.append({"asset_type": "a", "natural_key": f"a{i}", "fingerprint": f"fa{i}",
                      "import_action": "create", "export_status": "success", "payload": {},
                      "source_id": f"sa{i}"})
    for i in range(n_b):
        units.append({"asset_type": "b", "natural_key": f"b{i}", "fingerprint": f"fb{i}",
                      "import_action": "create", "export_status": "success", "payload": {},
                      "source_id": f"sb{i}"})
    return units


def test_b6_parallel_import_no_lost_or_duplicated_rows():
    """Forced-parallel (threads=4) over many small units: every unit is recorded exactly once, no
    counter is lost (the recording lock), no row duplicated."""
    h = _import_test_helpers()
    units = _toy_units(40, 20)
    imp, st = h._make(_toy_importer_cls(), units, h.RecordingClient())
    imp.config.parallel_threads = 4
    res = imp.run()
    assert res.created == 60
    keys = [u["natural_key"] for u in res.units]
    assert len(keys) == 60 and len(set(keys)) == 60    # none lost, none duplicated
    assert set(keys) == {f"a{i}" for i in range(40)} | {f"b{i}" for i in range(20)}


def test_b6_sublevel_barrier_all_a_before_any_b():
    """The barrier is REQUIRED by the dependency graph: every 'a' (an earlier asset_type) completes
    AND is flushed to state before any 'b' (a later asset_type) runs."""
    h = _import_test_helpers()
    units = _toy_units(30, 15)
    imp, st = h._make(_toy_importer_cls(), units, h.RecordingClient())
    imp.config.parallel_threads = 4
    imp.run()
    # all 'a' precede all 'b' in create order
    last_a = max(i for i, at in enumerate(imp.order) if at == "a")
    first_b = min(i for i, at in enumerate(imp.order) if at == "b")
    assert last_a < first_b, "a sub-level must fully complete before the b sub-level starts"
    # and every 'b' saw ALL 30 'a' target ids already in state (barrier flushed them)
    assert imp.saw_all_a_when_b and all(c == 30 for c in imp.saw_all_a_when_b)


def test_b6_threads_1_equals_threads_4_outcomes():
    h = _import_test_helpers()
    units = _toy_units(25, 25)
    imp1, _ = h._make(_toy_importer_cls(), list(units), h.RecordingClient())
    imp1.config.parallel_threads = 1
    r1 = imp1.run()
    imp4, _ = h._make(_toy_importer_cls(), list(units), h.RecordingClient())
    imp4.config.parallel_threads = 4
    r4 = imp4.run()
    assert {(u["natural_key"], u["import_status"]) for u in r1.units} == \
           {(u["natural_key"], u["import_status"]) for u in r4.units}
    assert r1.created == r4.created == 50


def test_b6_fault_in_one_unit_fails_only_that_unit_under_parallel():
    """A deterministic error on one unit under parallelism fails ONLY that unit (fail-soft), is
    recorded, and does not advance its fingerprint; the rest succeed."""
    h = _import_test_helpers()
    from src.importers.base_importer import BaseImporter

    class _FlakyToy(BaseImporter):
        component = "toy"
        asset_types = ("a",)

        def load(self):
            return self.units_for("a")

        def existing_keys(self):
            return {}

        def create_one(self, unit):
            if self.natural_key(unit) == "a7":
                raise RuntimeError("INVALID_PARAMETER_VALUE: boom")
            return {"target_id": f"t-{self.natural_key(unit)}"}

    units = _toy_units(20, 0)
    imp, st = h._make(_FlakyToy, units, h.RecordingClient())
    imp.config.parallel_threads = 4
    res = imp.run()
    assert res.created == 19 and res.failed == 1
    row = st.row("a", "a7")
    assert row["last_action"] == "failed"
    assert row.get("last_source_fingerprint", "") in ("", None)   # not advanced → retry re-attempts


def test_b6_identity_and_misc_stay_serial():
    """IDENTITY (two-pass membership) and MISC (cluster-lib start/stop race) opt OUT of parallelism
    even when parallel_threads>1."""
    from src.importers.identity_importer import IdentityImporter
    from src.importers.misc_importer import MiscImporter
    assert IdentityImporter.parallel_safe is False
    assert MiscImporter.parallel_safe is False


# ─────────────────────────── B12 — widget grouping (labels only) ────────────────────────────

def _widget_defs(notebook_path):
    """Parse `dbutils.widgets.<type>("name", default, "label"...)` from a notebook into
    {name: (default, label)} — tolerant of multi-line calls."""
    import re
    text = open(notebook_path, encoding="utf-8").read()
    # Join continuation lines so a multi-line widget call is one string.
    flat = re.sub(r"\n\s+", " ", text)
    out = {}
    for m in re.finditer(
            r'dbutils\.widgets\.(?:text|dropdown|multiselect|combobox)\(\s*'
            r'(f?"[^"]*"|f?\'[^\']*\')\s*,\s*(f?"[^"]*"|f?\'[^\']*\')'
            r'(?:\s*,\s*\[[^\]]*\])?\s*,\s*(f?"[^"]*"|f?\'[^\']*\')', flat):
        name, default, label = (x.strip() for x in m.groups())
        out[name] = (default, label)
    return out


def test_b12_every_widget_label_keeps_original_text_with_only_a_numeric_prefix():
    """Widgets carry ONLY a `<N><letter>. ` grouping prefix — the ORIGINAL display text is kept
    verbatim (no injected `Group ·` tag, which caused confusion). The numeric prefix groups them in
    the Databricks widget bar."""
    import os
    import re
    nb_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "notebooks")
    pat = re.compile(r'^f?["\']\d+[a-z]\. \S')      # prefix then the original text (no "Group ·")
    for nb in ("00_Install_Jobs.py", "01_Inventory.py", "02_Export.py", "04_Import.py"):
        defs = _widget_defs(os.path.join(nb_dir, nb))
        assert defs, f"no widgets parsed from {nb}"
        for name, (_default, label) in defs.items():
            assert pat.match(label), f"{nb}:{name} label not prefixed: {label}"
            assert " · " not in label, f"{nb}:{name} still has an injected group tag: {label}"


def test_b12_prefix_text_matches_the_original_display_name():
    """The text AFTER the `<N><letter>. ` prefix equals the ORIGINAL widget label (prefix-only
    change) — proven against the PRE-B12 baseline for a representative sample.

    Pinned to the initial commit (pre-B12), not HEAD: once the B12 branch is committed, HEAD
    already carries the numeric prefixes, so HEAD is no longer the unprefixed "original" to diff
    against. `924d4f8` is the root commit that still has the bare labels."""
    import os
    import re
    import subprocess
    nb_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "notebooks")
    cur = _widget_defs(os.path.join(nb_dir, "01_Inventory.py"))
    head = subprocess.run(["git", "show", "924d4f8:notebooks/01_Inventory.py"],
                          capture_output=True, text=True, cwd=os.path.dirname(nb_dir)).stdout
    orig = {}
    import re as _re
    flat = _re.sub(r"\n\s+", " ", head)
    for m in _re.finditer(
            r'dbutils\.widgets\.(?:text|dropdown|multiselect)\(\s*"([^"]+)"\s*,\s*"[^"]*"'
            r'(?:\s*,\s*\[[^\]]*\])?\s*,\s*"([^"]*)"', flat):
        orig[m.group(1)] = m.group(2)
    for name in ("connectivity_mode", "max_scim", "force_full"):
        label = cur[f'"{name}"'][1].strip('"')
        assert re.match(r'^\d+[a-z]\. ', label)
        assert label.split(". ", 1)[1] == orig[name], f"{name}: text changed vs original"


def test_b12_shared_widgets_carry_the_same_label_across_notebooks():
    import os
    nb_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "notebooks")
    inv = _widget_defs(os.path.join(nb_dir, "01_Inventory.py"))
    exp = _widget_defs(os.path.join(nb_dir, "02_Export.py"))
    for shared in ("connectivity_mode", "source_workspace_id", "staging_location", "log_level",
                   "parallel_threads"):
        assert f'"{shared}"' in inv and f'"{shared}"' in exp
        assert inv[f'"{shared}"'][1] == exp[f'"{shared}"'][1], \
            f"{shared} label differs between 01 and 02"
    # staging + state + catalog_mapping all share the Output group number (2x) in 04.
    imp = _widget_defs(os.path.join(nb_dir, "04_Import.py"))
    for out_widget in ("staging_location", "state_catalog", "state_schema", "catalog_mapping_json"):
        assert imp[f'"{out_widget}"'][1][1:3].startswith("2"), f"{out_widget} not in Output group 2"


def test_b12_widget_names_and_defaults_unchanged_only_labels():
    """The regroup is label-ONLY: known widget names keep their expected defaults (no name/default
    drift)."""
    import os
    nb_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "notebooks")
    imp = _widget_defs(os.path.join(nb_dir, "04_Import.py"))
    assert imp['"connectivity_mode"'][0] == '"direct"'
    assert imp['"dry_run"'][0] == '"true"'
    assert imp['"retry_mode"'][0] == '"off"'
    assert imp['"catalog_mapping_json"'][0] == '""'
    inv = _widget_defs(os.path.join(nb_dir, "01_Inventory.py"))
    assert inv['"parallel_threads"'][0] == '"1"' and inv['"log_level"'][0] == '"DEBUG"'


def test_qa1_installer_and_job_templates_carry_parallel_threads_and_log_level():
    """QA-1: `parallel_threads` + `log_level` must be exposed by the installer AND present in every
    job template's task base_parameters — otherwise the deployed jobs always run serial at the
    notebook defaults and B6 parallelism can never be enabled through the shipped jobs."""
    import os
    import json
    import glob
    root = os.path.dirname(os.path.dirname(__file__))
    inst = _widget_defs(os.path.join(root, "notebooks", "00_Install_Jobs.py"))
    assert inst['"parallel_threads"'][0] == '"1"'
    assert inst['"log_level"'][0] == '"DEBUG"'
    installer_src = open(os.path.join(root, "notebooks", "00_Install_Jobs.py")).read()
    # projected into the config the installer writes into each job
    assert '"parallel_threads": _w("parallel_threads"' in installer_src
    assert '"log_level": _w("log_level"' in installer_src
    # every notebook task in every job template declares both keys so the installer fills them
    for f in glob.glob(os.path.join(root, "jobs", "*.job.json")):
        d = json.load(open(f))
        for t in d.get("tasks", []):
            nt = t.get("notebook_task")
            if nt is None:
                continue
            bp = nt.get("base_parameters", {})
            assert "parallel_threads" in bp, f"{os.path.basename(f)}/{t['task_key']} missing parallel_threads"
            assert "log_level" in bp, f"{os.path.basename(f)}/{t['task_key']} missing log_level"
