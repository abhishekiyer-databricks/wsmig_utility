"""PLAN 16.1 — step-by-step run-output logging + state-store hardening (offline tests, plan §7).

Part A (logging): two outputs — the notebook CELL (stdout, `log_level`, capped warning/error lines)
and the DRIVER LOG (`sys.__stderr__`, always DEBUG, everything) — no log files, a start + end line
for every object, progress every PROGRESS_EVERY, redaction on both outputs.

Part B (state): a state read never fails silently — count-checked loads raise `StateLoadError`,
`load()` creates the tables on a first run, MERGE never blanks a stored id/fingerprint, and a
failed save turns the run into `completed_state_not_saved`.

Every test captures BOTH outputs into StringIO streams via `configure_logging(cell_stream=…,
driver_stream=…)`, so nothing here depends on pytest's capture of the real stdout/stderr.
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import threading

import pytest

from src.config.config_manager import Config
from src.exporters import bundle_paths as BP
from src.exporters.artifact_writer import ArtifactWriter
from src.state.sql_backend import SparkSqlBackend, StatementApiBackend
from src.state.state_store import ACTION_CREATED, StateLoadError, StateStore
from src.utils import logger as L
from tests.fakes import FakeClient
from tests.test_state_store import FakeBackend

_LINE = re.compile(r"^\S+ \S+ #(?P<seq>\d+) (?P<level>[A-Z]+)\s+\[(?P<comp>[^\]]+)\] "
                   r"run=(?P<run>\S+) stage=(?P<stage>\S+) \| (?P<msg>.*)$")


# ── helpers ────────────────────────────────────────────────────────────────

@pytest.fixture
def streams():
    """Configure logging into two StringIO streams; restore the lazy default afterwards."""
    cell, driver = io.StringIO(), io.StringIO()
    L.configure_logging(run_id="r1", stage="import", level="INFO",
                        cell_stream=cell, driver_stream=driver)
    yield cell, driver
    L.configure_logging(run_id="-", stage="-", level="INFO",
                        cell_stream=io.StringIO(), driver_stream=io.StringIO())


def _records(stream) -> list[dict]:
    """Parse the FIRST line of every record (tracebacks are continuation lines)."""
    out = []
    for line in stream.getvalue().splitlines():
        m = _LINE.match(line)
        if m:
            out.append(m.groupdict())
    return out


def _msgs(stream, level=None) -> list[str]:
    return [r["msg"] for r in _records(stream) if level is None or r["level"] == level]


def _cfg(tmp, *, dry_run=False, state=True, **over):
    d = {"role": "target", "source_workspace_id": "111", "run_id": "r1",
         "target_staging_location": tmp, "dry_run": dry_run,
         "imports": ({"state_catalog": "cat", "state_schema": "sch"} if state else {})}
    d.update(over)
    return Config.from_dict(d)


# ═════════════════════════════ Part A — logger ═════════════════════════════

def test_line_format_carries_seq_level_component_run_and_stage(streams):
    cell, driver = streams
    L.get_logger("BaseImporter").info("hello", k=1)
    (rec,) = _records(driver)
    assert rec["level"] == "INFO" and rec["comp"] == "BaseImporter"
    assert rec["run"] == "r1" and rec["stage"] == "IMPORT"
    assert rec["msg"] == "hello  k=1"
    assert _records(cell)[0]["seq"] == rec["seq"], "same record → same #seq in both outputs"


def test_seq_increases_by_one_per_record(streams):
    _cell, driver = streams
    log = L.get_logger("t")
    for i in range(5):
        log.debug(f"d{i}")
        log.info(f"i{i}")
    seqs = [int(r["seq"]) for r in _records(driver)]
    assert seqs == list(range(seqs[0], seqs[0] + 10))


def test_cell_follows_log_level_and_driver_is_always_debug(streams):
    cell, driver = streams
    log = L.get_logger("t")
    log.debug("only-in-driver")
    log.error("in-both")
    assert "only-in-driver" in _msgs(driver) and "only-in-driver" not in _msgs(cell)
    assert "in-both" in _msgs(driver) and "in-both" in _msgs(cell)


def test_log_level_widget_sets_the_cell_only():
    cell, driver = io.StringIO(), io.StringIO()
    L.configure_logging(run_id="r", stage="export", level="WARNING",
                        cell_stream=cell, driver_stream=driver)
    log = L.get_logger("t")
    log.info("info-line")
    log.warning("warn-line")
    assert _msgs(cell) == ["warn-line"]
    assert _msgs(driver) == ["info-line", "warn-line"]
    # DEBUG on the widget shows DEBUG in the cell too; blank → INFO
    L.configure_logging(level="DEBUG", cell_stream=cell, driver_stream=driver)
    L.get_logger("t").debug("dbg")
    assert "dbg" in _msgs(cell)
    blank = io.StringIO()
    L.configure_logging(level="", cell_stream=blank, driver_stream=io.StringIO())
    L.get_logger("t").debug("hidden")
    L.get_logger("t").info("shown")
    assert _msgs(blank) == ["shown"]


def test_structured_logger_applies_no_threshold_of_its_own(streams):
    """Branch bug: StructuredLogger(level="INFO") dropped DEBUG before it reached any handler."""
    _cell, driver = streams
    L.get_logger("t", level="ERROR").debug("still-reaches-the-driver-log")
    assert "still-reaches-the-driver-log" in _msgs(driver)


def test_no_propagation_to_root_so_no_double_lines(streams):
    app = logging.getLogger(L.APP_LOGGER_NAME)
    assert app.propagate is False
    root_buf = io.StringIO()
    root_handler = logging.StreamHandler(root_buf)
    logging.getLogger().addHandler(root_handler)
    try:
        L.get_logger("t").warning("once")
    finally:
        logging.getLogger().removeHandler(root_handler)
    assert root_buf.getvalue() == ""


def test_configure_is_idempotent(streams):
    cell, driver = streams
    for _ in range(3):
        L.configure_logging(run_id="r1", stage="import", cell_stream=cell, driver_stream=driver)
    L.get_logger("t").info("x")
    assert _msgs(cell).count("x") == 1 and _msgs(driver).count("x") == 1


def test_secrets_are_redacted_in_both_outputs_including_tracebacks(streams):
    cell, driver = streams
    L.register_secret("s3cr3t-value-123")
    log = L.get_logger("t")
    log.info("token=s3cr3t-value-123")
    try:
        raise RuntimeError("upstream echoed s3cr3t-value-123")
    except RuntimeError:
        log.exception("boom")
    for out in (cell.getvalue(), driver.getvalue()):
        assert "s3cr3t-value-123" not in out
        assert "***REDACTED***" in out
    assert "Traceback" in driver.getvalue()


def test_short_values_are_not_registered_as_secrets(streams):
    _cell, driver = streams
    L.register_secret("ab")
    L.get_logger("t").info("about abc")
    assert "about abc" in _msgs(driver)


def test_no_file_is_ever_written():
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        L.configure_logging(run_id="r", stage="inventory", cell_stream=io.StringIO(),
                            driver_stream=io.StringIO())
        log = L.get_logger("t")
        for i in range(50):
            log.debug("d", i=i)
            log.warning("w", i=i)
        with L.live_run("INVENTORY"):
            log.info("inside")
    finally:
        os.chdir(cwd)
    assert os.listdir(tmp) == []
    assert not hasattr(L, "set_log_file") and not hasattr(L, "flush_log_file")


# ── live_run ───────────────────────────────────────────────────────────────

def test_live_run_logs_start_and_finish(streams):
    cell, _driver = streams
    with L.live_run("import"):
        L.get_logger("t").info("work")
    msgs = _msgs(cell)
    assert msgs[0] == "Stage IMPORT started"
    assert msgs[-1].startswith("Stage IMPORT finished (status=ok, ")


def test_live_run_logs_an_exception_with_traceback_and_reraises(streams):
    cell, driver = streams
    with pytest.raises(ValueError, match="kaboom"):
        with L.live_run("EXPORT"):
            raise ValueError("kaboom")
    errs = _msgs(cell, "ERROR")
    assert errs and errs[0].startswith("Stage EXPORT FAILED after") and "kaboom" in errs[0]
    assert "Traceback (most recent call last)" in driver.getvalue()


def test_live_run_starts_no_thread(streams):
    before = threading.active_count()
    with L.live_run("IMPORT"):
        assert threading.active_count() == before
    assert threading.active_count() == before


# ── cell cap ───────────────────────────────────────────────────────────────

def test_cell_cap_after_2000_failure_lines(streams):
    cell, driver = streams
    log = L.get_logger("t")
    n = L.CELL_FAILURE_CAP + 300
    for i in range(n):
        (log.error if i % 2 else log.warning)(f"fail {i}")
    log.info("progress line still shows")
    cell_fail = [m for m in _msgs(cell) if m.startswith("fail ")]
    assert len(cell_fail) == L.CELL_FAILURE_CAP
    notices = [m for m in _msgs(cell) if "further failures are in the driver log" in m]
    assert len(notices) == 1 and "import_status.xlsx" in notices[0]
    assert "progress line still shows" in _msgs(cell), "INFO keeps flowing after the cap"
    assert len([m for m in _msgs(driver) if m.startswith("fail ")]) == n, "driver gets all"


def test_live_run_resets_the_cell_cap(streams):
    cell, _driver = streams
    log = L.get_logger("t")
    for i in range(L.CELL_FAILURE_CAP + 5):
        log.error(f"a{i}")
    with L.live_run("IMPORT"):
        log.error("after-reset")
    assert "after-reset" in _msgs(cell)


# ── progress ───────────────────────────────────────────────────────────────

def test_progress_emits_every_500_and_at_the_last_item(streams):
    cell, _driver = streams
    log = L.get_logger("t")
    emitted = [i for i in range(1, 1201) if log.progress("import notebooks", i, 1200, failed=0)]
    assert emitted == [500, 1000, 1200]
    assert _msgs(cell)[0] == "import notebooks: 500/1,200 — failed 0"


def test_progress_small_family_gets_one_line_at_the_end(streams):
    log = L.get_logger("t")
    assert [i for i in range(1, 8) if log.progress("p", i, 7)] == [7]


def test_progress_unknown_total_emits_every_500_only(streams):
    cell, _driver = streams
    log = L.get_logger("t")
    assert [i for i in range(1, 1001) if log.progress("collect", i)] == [500, 1000]
    assert _msgs(cell)[0] == "collect: 500"


def test_fmt_elapsed():
    assert L.fmt_elapsed(42) == "42s"
    assert L.fmt_elapsed(190) == "3m10s"
    assert L.fmt_elapsed(3720) == "1h02m"


# ═══════════════════════ Part A — instrumented steps ═══════════════════════

def _toy_importer(units, *, fail_on=(), warn_on=(), dry_run=False):
    from tests.test_import_framework import ToyImporter
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp, dry_run=dry_run)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    st = StateStore(FakeBackend(), cfg)
    st.load()
    imp = ToyImporter(object(), cfg, aw, state=st, units_by_type={"cluster": units})
    imp.fail_on, imp.warn_on = set(fail_on), set(warn_on)
    return imp


def _unit(key, **over):
    u = {"asset_type": "cluster", "natural_key": key, "source_id": f"src-{key}",
         "fingerprint": "sha256:v1", "import_action": "create", "export_status": "success",
         "payload": {"cluster_name": key}}
    u.update(over)
    return u


_OUTCOME = re.compile(r"^cluster (?P<key>\S+) → ")


def test_importer_logs_phase_pair_and_one_start_and_one_outcome_per_object(streams):
    cell, driver = streams
    units = [_unit("ok1"), _unit("bad"), _unit("warn"), _unit("man", import_action="manual"),
             _unit("dab", import_action="dab_redeploy")]
    imp = _toy_importer(units, fail_on={"bad"}, warn_on={"warn"})
    imp.run()

    dmsgs = _msgs(driver)
    assert any(m.startswith("Phase: import compute — 5 units") for m in dmsgs)
    assert any(m.startswith("Phase complete: import compute — created 1") for m in dmsgs)
    assert any("existence check started" in m for m in dmsgs)
    assert any("existence check done" in m for m in dmsgs)
    for key in ("ok1", "bad", "warn", "man", "dab"):
        starts = [i for i, m in enumerate(dmsgs) if m == f"importing cluster {key}"]
        ends = [i for i, m in enumerate(dmsgs)
                if (mm := _OUTCOME.match(m)) and mm.group("key") == key]
        assert len(starts) == 1, f"{key}: expected one start line, got {starts}"
        assert len(ends) == 1, f"{key}: expected exactly one outcome line, got {ends}"
        assert starts[0] < ends[0]
    assert any(m.startswith("decide cluster ok1: state_row=no exists=no") and
               m.endswith("→ CREATE") for m in dmsgs)

    # the failed object's ERROR line names the object, the category and the raw server error
    err = [m for m in _msgs(driver, "ERROR") if m.startswith("cluster bad")]
    assert err and "FAILED api_error" in err[0] and "INVALID_PARAMETER_VALUE" in err[0]
    assert [m for m in _msgs(driver, "WARNING") if m.startswith("cluster warn")]


def test_importer_cell_shows_phases_progress_and_failures_only(streams):
    cell, _driver = streams
    imp = _toy_importer([_unit("ok1"), _unit("bad")], fail_on={"bad"})
    imp.run()
    cmsgs = _msgs(cell)
    assert not [m for m in cmsgs if m.startswith("importing ") or m.startswith("decide ")]
    assert not [m for m in cmsgs if m.startswith("cluster ok1")], "successes stay in the driver"
    assert [m for m in cmsgs if m.startswith("cluster bad → FAILED")]
    assert [m for m in cmsgs if m.startswith("import compute: 2/2 — created 1")], cmsgs
    assert [m for m in cmsgs if m.startswith("Phase: import compute")]
    assert [m for m in cmsgs if m.startswith("Phase complete: import compute")]


def test_importer_retry_out_of_scope_unit_still_gets_an_outcome_line(streams):
    _cell, driver = streams
    imp = _toy_importer([_unit("a"), _unit("b")])
    imp.retry_keys = {("cluster", "a")}
    imp.run()
    dmsgs = _msgs(driver)
    assert "importing cluster b" in dmsgs
    assert [m for m in dmsgs if m.startswith("cluster b → skipped")]


def test_importer_progress_every_500(streams):
    cell, _driver = streams
    imp = _toy_importer([_unit(f"c{i}") for i in range(1100)])
    imp.run()
    prog = [m for m in _msgs(cell) if re.match(r"^import compute: [\d,]+/", m)]
    assert [p.split(" — ")[0] for p in prog] == ["import compute: 500/1,100",
                                                 "import compute: 1,000/1,100",
                                                 "import compute: 1,100/1,100"]


def test_collector_logs_phase_pair_and_per_object_acl_lines(streams):
    from src.collectors.jobs_collector import JobsCollector
    _cell, driver = streams
    client = FakeClient(
        paginated_table={"api/2.1/jobs/list": [{"job_id": 7}, {"job_id": 8}]},
        get_table={"api/2.1/jobs/get": lambda p: {"job_id": p["job_id"],
                                                  "settings": {"name": f"job{p['job_id']}"}},
                   "api/2.0/permissions/jobs/7": {"access_control_list": [{"user_name": "a"}]},
                   "api/2.0/permissions/jobs/8": {"access_control_list": []}})
    tmp = tempfile.mkdtemp()
    JobsCollector(client, _cfg(tmp)).run()
    dmsgs = _msgs(driver)
    assert "Phase: collect job" in dmsgs
    assert any(m.startswith("Phase complete: collect job — 2 objects, 0 errors") for m in dmsgs)
    for jid, grants in (("7", 1), ("8", 0)):
        i_start = dmsgs.index(f"collecting job {jid}")
        i_acl0 = dmsgs.index(f"fetching ACL jobs {jid}")
        i_acl1 = dmsgs.index(f"ACL jobs {jid} → {grants} grants")
        i_end = next(i for i, m in enumerate(dmsgs) if m.startswith(f"collected job {jid} "))
        assert i_start < i_acl0 < i_acl1 < i_end


def test_collector_failure_is_logged_with_traceback_and_phase_still_completes(streams):
    from src.collectors.jobs_collector import JobsCollector

    class Boom(FakeClient):
        def get_paginated(self, *a, **k):
            raise RuntimeError("list exploded")
    _cell, driver = streams
    JobsCollector(Boom(), _cfg(tempfile.mkdtemp())).run()
    assert any("collector failed: job: list exploded" in m for m in _msgs(driver, "ERROR"))
    assert "Traceback" in driver.getvalue()
    assert any(m.startswith("Phase complete: collect job — 0 objects, 1 errors")
               for m in _msgs(driver))


def test_workspace_walk_logs_listing_and_listed_per_directory(streams):
    from src.collectors.workspace_collector import WorkspaceCollector
    _cell, driver = streams
    client = FakeClient(get_table={
        "api/2.0/workspace/list": lambda p: {"objects": (
            [{"path": "/Shared", "object_type": "DIRECTORY", "object_id": 1}]
            if p["path"] == "/" else [])}})
    WorkspaceCollector(client, _cfg(tempfile.mkdtemp())).run()
    dmsgs = _msgs(driver)
    assert dmsgs.index("listing /") < dmsgs.index("listed / (1 objects)") \
        < dmsgs.index("listing /Shared") < dmsgs.index("listed /Shared (0 objects)")


def test_export_content_pass_logs_fetching_and_one_outcome_per_object(streams):
    from src.exporters.export_runner import ExportRunner
    cell, driver = streams
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()

    def _export(params):
        if params["path"] == "/a/broken":
            raise RuntimeError("500 boom")
        return b"print(1)"
    client = FakeClient(download_table={"api/2.0/workspace/export": _export})
    units = {"notebook": [
        {"asset_type": "notebook", "natural_key": p, "export_status": "success",
         "payload": {"language": "PYTHON"}, "fingerprint": "f"} for p in ("/a/nb1", "/a/broken")]}
    ExportRunner(client, cfg, aw, content_fetch_workers=1)._fetch_content(units)
    dmsgs = _msgs(driver)
    assert any(m.startswith("Phase: export content — 2 units") for m in dmsgs)
    assert any(m.startswith("Phase complete: export content — fetched 1, oversize 0, failed 1")
               for m in dmsgs)
    assert dmsgs.index("fetching /a/nb1  kind=notebook") < dmsgs.index("fetched /a/nb1 (8 bytes)")
    assert "fetching /a/broken  kind=notebook" in dmsgs
    fails = [m for m in _msgs(cell, "WARNING") if m.startswith("notebook /a/broken → FAILED")]
    assert len(fails) == 1, "exactly one outcome line for the failed fetch, and it reaches the cell"


def test_api_client_logs_each_call_without_bodies_or_headers(streams, monkeypatch):
    from src.auth.token_manager import ApiClient, HTTPStatusError, StaticTokenProvider
    _cell, driver = streams

    class Resp:
        def __init__(self, status, doc):
            self.status_code, self._doc, self.headers = status, doc, {}
            self.text = str(doc)

        def json(self):
            return self._doc

    replies = iter([Resp(200, {"secret_body_field": "zzz"}),
                    Resp(400, {"error_code": "INVALID_PARAMETER_VALUE", "message": "bad name"})])
    client = ApiClient("https://host", StaticTokenProvider("dapi-TOKEN-0123456789"))
    monkeypatch.setattr(client._s, "request", lambda *a, **k: next(replies))
    client.get("api/2.0/workspace/list", params={"path": "/Shared"})
    with pytest.raises(HTTPStatusError):
        client.post("api/2.0/clusters/create", {"cluster_name": "x"})
    api = [r["msg"] for r in _records(driver) if r["comp"] == "api"]
    assert api[0].startswith("GET  api/2.0/workspace/list?path=/Shared → 200 (")
    assert api[1].startswith("POST api/2.0/clusters/create → 400 (")
    assert "INVALID_PARAMETER_VALUE bad name" in api[1]
    out = driver.getvalue()
    assert "zzz" not in out and "cluster_name" not in out, "no request/response bodies"
    assert "dapi-TOKEN-0123456789" not in out and "Authorization" not in out


def test_retry_logs_a_warning_before_each_sleep(streams):
    from src.utils.retry import RetryableHTTPError, with_retry
    cell, _driver = streams
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableHTTPError(429, "GET https://h/api/2.0/x -> 429")
        return "ok"
    assert with_retry(flaky, sleep_fn=lambda s: None) == "ok"
    warns = _msgs(cell, "WARNING")
    assert warns == ["retrying GET https://h/api/2.0/x after 429 in 1s (attempt 1/5)",
                     "retrying GET https://h/api/2.0/x after 429 in 2s (attempt 2/5)"]


def test_oauth_secret_and_minted_token_never_reach_a_log(streams, monkeypatch):
    from src.auth import token_manager as tm
    cell, driver = streams

    class R:
        status_code = 200
        headers = {}

        def json(self):
            return {"access_token": "eyJ-MINTED-TOKEN-abcdef", "expires_in": 3600}
    monkeypatch.setattr(tm.requests, "post", lambda *a, **k: R())
    prov = tm.OAuthM2MTokenProvider("https://src", "client-id", "SP-SECRET-xyz-987")
    tok = prov()
    L.get_logger("t").info(f"accidentally logging {tok} and SP-SECRET-xyz-987")
    out = cell.getvalue() + driver.getvalue()
    assert "eyJ-MINTED-TOKEN-abcdef" not in out and "SP-SECRET-xyz-987" not in out
    assert any(m.startswith("token minted (expires in ") for m in _msgs(driver))


def test_read_json_or_empty_warns_on_a_missing_bundle_file(streams):
    from src.exporters.artifact_writer import read_json_or_empty
    cell, _driver = streams
    aw = ArtifactWriter(_cfg(tempfile.mkdtemp()))
    aw.ensure_output_path()
    assert read_json_or_empty(aw, BP.EXPORT_INDEX_JSON, "no units") == {}
    assert any(f"{BP.EXPORT_INDEX_JSON} is absent from the bundle — continuing with no units" in m
               for m in _msgs(cell, "WARNING"))


def test_job_templates_and_installer_project_log_level():
    """QA-1 lesson: a widget the jobs don't pass is unreachable — every task declares log_level."""
    import glob
    from src.utils.job_templates import load_template, render_template
    for path in glob.glob(os.path.join(os.path.dirname(__file__), "..", "jobs", "*.job.json")):
        tpl = load_template(path)
        for task in tpl["tasks"]:
            assert "log_level" in task["notebook_task"]["base_parameters"], (path, task["task_key"])
        spec = render_template(tpl, tokens={"REPO_PATH": "/r", "RUN_AS_SP": "sp"},
                               params={"log_level": "DEBUG"})
        assert all(t["notebook_task"]["base_parameters"]["log_level"] == "DEBUG"
                   for t in spec["tasks"])
    nb_dir = os.path.join(os.path.dirname(__file__), "..", "notebooks")
    for nb in ("01_Inventory.py", "02_Export.py", "04_Import.py", "00_Install_Jobs.py"):
        src = open(os.path.join(nb_dir, nb), encoding="utf-8").read()
        assert 'dbutils.widgets.dropdown("log_level", "INFO"' in src, nb
        assert "set_log_file" not in src and "flush_log_file" not in src, nb
    for nb in ("01_Inventory.py", "02_Export.py", "04_Import.py"):
        src = open(os.path.join(nb_dir, nb), encoding="utf-8").read()
        assert "print(" not in src, f"{nb}: print() bypasses the driver log"
        assert "configure_logging(" in src and "live_run(" in src, nb


# ═══════════════════════ Part B — SQL backend fails loud ═══════════════════

class _FakeDF:
    def __init__(self, rows=None, err=None):
        self._rows, self._err = rows or [], err

    def collect(self):
        if self._err:
            raise self._err
        return self._rows


class _Row(dict):
    def asDict(self):
        return dict(self)


def test_spark_query_raises_on_a_failing_collect():
    class Spark:
        def sql(self, stmt):
            return _FakeDF(err=RuntimeError("PERMISSION_DENIED: SELECT on table"))
    with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
        SparkSqlBackend(Spark()).query("SELECT * FROM t")


def test_spark_query_returns_rows_as_dicts():
    class Spark:
        def sql(self, stmt):
            return _FakeDF(rows=[_Row(a=1), _Row(a=2)])
    assert SparkSqlBackend(Spark()).query("SELECT a FROM t") == [{"a": 1}, {"a": 2}]


def test_spark_execute_and_sql_alias_raise_on_ddl_error():
    class Spark:
        def sql(self, stmt):
            raise RuntimeError("[SCHEMA_NOT_FOUND]")
    be = SparkSqlBackend(Spark())
    with pytest.raises(RuntimeError, match="SCHEMA_NOT_FOUND"):
        be.execute("CREATE TABLE x (a INT)")
    with pytest.raises(RuntimeError, match="SCHEMA_NOT_FOUND"):
        be.sql("CREATE TABLE x (a INT)")


def test_statement_api_backend_gets_query_and_execute_from_sql():
    be = StatementApiBackend(client=None, warehouse_id="w")
    be.sql = lambda stmt: [{"n": "3"}]          # the Statement API returns strings
    assert be.query("SELECT count(*) AS n") == [{"n": "3"}]


# ═══════════════════════ Part B — count-checked load ═══════════════════════

class CountingBackend(FakeBackend):
    """FakeBackend whose `count(*)` / `SELECT *` answers can be overridden per table."""

    def __init__(self, counts=None, drop_rows=None, fail_query=False):
        super().__init__()
        self.counts = counts or {}          # table marker → forced count(*)
        self.drop_rows = drop_rows or {}    # table marker → number of rows to "lose"
        self.fail_query = fail_query

    @staticmethod
    def _marker(stmt):
        return "identity" if "wsmig_identity_map" in stmt else "state"

    def query(self, stmt):
        self.statements.append(stmt)
        if self.fail_query:
            raise RuntimeError("[INSUFFICIENT_PERMISSIONS] User does not have SELECT")
        m = self._marker(stmt)
        if "count(*)" in stmt.lower() and m in self.counts:
            return [{"n": self.counts[m]}]
        rows = FakeBackend.sql(self, stmt)
        self.statements.pop()               # FakeBackend.sql recorded it a second time
        if "count(*)" not in stmt.lower() and m in self.drop_rows:
            rows = rows[: max(len(rows) - self.drop_rows[m], 0)]
        return rows


def _store_with(backend, ws="111", ensure=True):
    cfg = _cfg(tempfile.mkdtemp(), source_workspace_id=ws)
    st = StateStore(backend, cfg)
    if ensure:
        st.ensure_table()
    return st


def test_load_refuses_when_count_says_21276_but_zero_rows_loaded():
    st = _store_with(CountingBackend(counts={"state": 21276}))
    with pytest.raises(StateLoadError) as ei:
        st.load()
    assert "21276" in str(ei.value) and "only 0 were loaded" in str(ei.value)
    assert st._cache == {} and st._loaded is False


def test_load_refuses_on_a_partial_load():
    st = _store_with(CountingBackend(counts={"state": 21276, "identity": 0}))
    for i in range(16355):
        st.backend.state[("111", "job", f"j{i}")] = {"source_workspace_id": "111",
                                                    "asset_type": "job", "natural_key": f"j{i}"}
    with pytest.raises(StateLoadError, match="has 21276 rows .* only 16355 were loaded"):
        st.load()


def test_load_refuses_on_an_identity_table_mismatch():
    st = _store_with(CountingBackend(counts={"identity": 42}))
    with pytest.raises(StateLoadError, match="wsmig_identity_map has 42 rows"):
        st.load()


def test_load_first_run_zero_of_zero_continues(streams):
    _cell, driver = streams
    st = _store_with(CountingBackend())
    assert st.load() == {}
    loaded = [m for m in _msgs(driver) if m.startswith("state loaded")]
    assert len(loaded) == 2 and all("rows=0 expected=0" in m for m in loaded)


def test_load_n_of_n_loads(streams):
    backend = CountingBackend()
    st = _store_with(backend)
    for i in range(25):
        st.record("job", f"j{i}", action=ACTION_CREATED, fingerprint="f", target_object_id=str(i))
    st.flush()
    fresh = StateStore(backend, st.config)
    fresh.load()
    assert len(fresh._cache) == 25
    assert any("rows=25 expected=25" in m for m in _msgs(streams[1]))


def test_a_raising_query_is_a_state_load_error_never_an_empty_cache():
    st = _store_with(CountingBackend(fail_query=True))
    with pytest.raises(StateLoadError, match="INSUFFICIENT_PERMISSIONS"):
        st.load()
    assert st._loaded is False


def test_count_check_drops_rows_between_count_and_select():
    """The 'fake that drops rows' hook from the live fail-loud drill (§8.7)."""
    backend = CountingBackend(drop_rows={"state": 1})
    st = _store_with(backend)
    st.record("job", "a", action=ACTION_CREATED, fingerprint="f", target_object_id="1")
    st.record("job", "b", action=ACTION_CREATED, fingerprint="f", target_object_id="2")
    st.flush()
    with pytest.raises(StateLoadError, match="has 2 rows .* only 1 were loaded"):
        StateStore(backend, st.config).load()


def test_count_from_the_statement_api_is_a_string_and_still_compares():
    class StrCount(CountingBackend):
        def query(self, stmt):
            rows = super().query(stmt)
            return [{"n": str(r["n"])} for r in rows] if "count(*)" in stmt.lower() else rows
    st = _store_with(StrCount())
    assert st.load() == {}


def test_every_load_query_is_filtered_by_source_workspace_id():
    backend = CountingBackend()
    st = _store_with(backend, ws="AAA")
    st.load(force=True)
    selects = [s for s in backend.statements if s.strip().startswith("SELECT")]
    assert len(selects) == 4    # count + select, × 2 tables
    assert all("WHERE source_workspace_id = 'AAA'" in s for s in selects)


# ── first run: load() creates the tables itself ────────────────────────────

class NoTablesBackend(CountingBackend):
    """Tables don't exist until `CREATE TABLE IF NOT EXISTS` runs (a fresh state_schema)."""

    def __init__(self):
        super().__init__()
        self.tables: set = set()

    def sql(self, stmt):
        s = stmt.strip()
        if s.startswith("CREATE TABLE IF NOT EXISTS"):
            self.tables.add(s.split()[5])
        return super().sql(stmt)

    def query(self, stmt):
        table = stmt.split(" FROM ", 1)[1].split()[0]
        if table not in self.tables:
            raise RuntimeError(f"[TABLE_OR_VIEW_NOT_FOUND] {table}")
        return super().query(stmt)


def test_first_run_load_creates_both_tables_then_counts_zero():
    backend = NoTablesBackend()
    st = _store_with(backend, ensure=False)          # load() WITHOUT a prior ensure_table()
    assert st.load() == {}
    assert backend.tables == {st.table_fqn, st.identity_table_fqn}
    ddl_before = [s for s in backend.statements if not s.strip().startswith("SELECT")]
    st.load(force=True)
    ddl_after = [s for s in backend.statements if not s.strip().startswith("SELECT")]
    assert ddl_after == ddl_before, "a second load() must issue no further DDL"


def test_state_disabled_issues_no_ddl_no_count_and_loads_empty():
    cfg = _cfg(tempfile.mkdtemp(), dry_run=True, state=False)
    backend = FakeBackend()
    st = StateStore(None, cfg)
    assert st.enabled is False
    assert st.load() == {} and st.flush() == 0
    assert backend.statements == []


def test_preflight_records_a_state_load_error_as_blocking():
    from src.importers.preflight import Preflight
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    st = StateStore(CountingBackend(counts={"state": 5}), cfg)
    pf = Preflight(object(), cfg, aw, state=st)
    pf.check_state_schema()
    (finding,) = [f for f in pf.findings if f["check"] == "migration state table"]
    assert finding["ok"] is False and finding["grade"] == "BLOCKING"
    assert "has 5 rows" in finding["detail"]


# ═══════════════════════ Part B — non-destructive MERGE ════════════════════

class MergeSemanticsBackend(FakeBackend):
    """Applies the MERGE's per-column SET semantics, including COALESCE(NULLIF(s.c,''), t.c)."""

    def sql(self, statement):
        s = statement.strip()
        if not s.startswith("MERGE"):
            return super().sql(statement)
        self.statements.append(statement)
        rows = self._parse_merge_source(statement)
        identity = "wsmig_identity_map" in statement
        target = self.identity if identity else self.state
        keycols = (("source_workspace_id", "entity_type", "source_key") if identity
                   else ("source_workspace_id", "asset_type", "natural_key"))
        keep = set(re.findall(r"t\.(\w+) = COALESCE\(NULLIF\(s\.\1, ''\), t\.\1\)", statement))
        for r in rows:
            k = tuple(r.get(c) for c in keycols)
            if k in target:
                old = target[k]
                target[k] = {c: (old.get(c) if c in keep and not r.get(c) else r.get(c))
                             for c in r}
            else:
                target[k] = r
        return []


def test_merge_sql_coalesces_the_four_state_columns_and_two_identity_columns():
    backend = FakeBackend()
    st = _store_with(backend)
    st.record("job", "j", action=ACTION_CREATED, target_object_id="9")
    st.record_identity("group", "g", target_id="77")
    st.flush()
    merges = [s for s in backend.statements if s.strip().startswith("MERGE")]
    state_merge = next(m for m in merges if "wsmig_migration_state" in m)
    ident_merge = next(m for m in merges if "wsmig_identity_map" in m)
    for c in ("target_object_id", "source_object_id", "last_source_fingerprint", "first_seen"):
        assert f"t.{c} = COALESCE(NULLIF(s.{c}, ''), t.{c})" in state_merge
    for c in ("target_id", "source_id"):
        assert f"t.{c} = COALESCE(NULLIF(s.{c}, ''), t.{c})" in ident_merge
    assert "t.last_action = s.last_action" in state_merge, "other columns overwrite as before"
    assert "t.target_key = s.target_key" in ident_merge


def test_a_blank_target_id_in_a_batch_never_blanks_the_stored_value():
    backend = MergeSemanticsBackend()
    st = _store_with(backend)
    st.record("job", "j", action=ACTION_CREATED, fingerprint="fp1", source_object_id="s1",
              target_object_id="9")
    st.flush()
    # A second store with a WRONG (empty) cache — exactly the case the guard exists for.
    wrong = StateStore(backend, st.config)
    wrong._loaded, wrong._ensured = True, True
    wrong.record("job", "j", action="failed", error="boom")
    wrong.flush()
    row = backend.state[("111", "job", "j")]
    assert row["target_object_id"] == "9"
    assert row["source_object_id"] == "s1"
    assert row["last_source_fingerprint"] == "fp1"
    assert row["last_action"] == "failed", "non-protected columns still update"


def test_a_blank_identity_target_id_never_blanks_the_stored_value():
    backend = MergeSemanticsBackend()
    st = _store_with(backend)
    st.record_identity("service_principal", "old-app", target_id="scim-1", source_id="src-1",
                       target_key="new-app")
    st.flush()
    wrong = StateStore(backend, st.config)
    wrong._loaded, wrong._ensured = True, True
    wrong.record_identity("service_principal", "old-app", target_id="")
    wrong.flush()
    row = backend.identity[("111", "service_principal", "old-app")]
    assert row["target_id"] == "scim-1" and row["source_id"] == "src-1"


# ═══════════════════════ Part B — visible flush failures ═══════════════════

class FailingMergeBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.fail_merges = True

    def sql(self, statement):
        if statement.strip().startswith("MERGE") and self.fail_merges:
            self.statements.append(statement)
            raise RuntimeError("DELTA_CONCURRENT_APPEND")
        return super().sql(statement)


def test_flush_failure_sets_flush_failed_and_a_later_success_clears_it(streams):
    cell, _driver = streams
    backend = FailingMergeBackend()
    st = _store_with(backend)
    st.record("job", "j", action=ACTION_CREATED, target_object_id="1")
    assert st.flush() == 0
    assert st.flush_failed is True and len(st._pending) == 1
    assert any(m.startswith("state flush FAILED") for m in _msgs(cell, "ERROR"))
    backend.fail_merges = False
    assert st.flush() == 1
    assert st.flush_failed is False and st._pending == []


def test_flush_failure_turns_the_run_red_as_completed_state_not_saved(streams):
    from src.importers.import_runner import RUN_STATUS_STATE_NOT_SAVED, ImportRunner
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    aw.write_json(BP.MANIFEST_JSON, {"files": [], "tool_version": "t"})
    aw.write_json(BP.EXPORT_INDEX_JSON, {"units": []})
    backend = FailingMergeBackend()
    st = StateStore(backend, cfg)
    runner = ImportRunner(object(), cfg, aw, state=st)
    # Every phase "imports" one object; the MERGE that would persist it fails.
    runner._run_phase = lambda family, ubt: st.record(
        "cluster", f"c-{family}", action=ACTION_CREATED, target_object_id="t")
    summary = runner.run()
    assert summary["run_status"] == RUN_STATUS_STATE_NOT_SAVED
    assert any("final state save FAILED" in m for m in _msgs(streams[0], "ERROR"))


def test_a_successful_run_stays_completed():
    from src.importers.import_runner import ImportRunner
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    aw.write_json(BP.MANIFEST_JSON, {"files": [], "tool_version": "t"})
    aw.write_json(BP.EXPORT_INDEX_JSON, {"units": []})
    st = StateStore(FakeBackend(), cfg)
    runner = ImportRunner(object(), cfg, aw, state=st)
    runner._run_phase = lambda family, ubt: st.record(
        "cluster", f"c-{family}", action=ACTION_CREATED, target_object_id="t")
    assert runner.run()["run_status"] == "completed"


def test_import_runner_surfaces_a_state_load_error_as_a_hard_stop():
    from src.importers.import_runner import ImportRunner
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    aw.write_json(BP.MANIFEST_JSON, {"files": [], "tool_version": "t"})
    st = StateStore(CountingBackend(counts={"state": 21276}), cfg)
    runner = ImportRunner(object(), cfg, aw, state=st)
    phases = []
    runner._run_phase = lambda family, ubt: phases.append(family)
    with pytest.raises(StateLoadError):
        runner.run()
    assert phases == [], "no phase may run after an incomplete state load"
    assert runner.run_status == "aborted"


# ── _cache snapshot helpers (prepares 16.5) ────────────────────────────────

def test_cache_helpers_survive_concurrent_records():
    """The `_cache` helpers iterate a SNAPSHOT (`list(...)`), so a writer thread recording rows
    while they run (16.5 parallelism) can never raise "dictionary changed size during iteration".
    A stress test: without the snapshot this fails within a few hundred iterations."""
    st = _store_with(FakeBackend())
    st.load()
    for i in range(2000):
        st._cache[("job", f"seed{i}")] = {"last_action": "failed", "target_object_id": str(i)}
    stop = threading.Event()

    def writer():
        # Insert + delete a bounded key set: the dict SIZE keeps changing (what breaks a live
        # iterator) without growing, so every snapshot stays cheap.
        i = 0
        while not stop.is_set():
            key = ("job", f"w{i % 50}")
            if key in st._cache:
                st._cache.pop(key, None)
            else:
                st._cache[key] = {"last_action": "failed", "target_object_id": "x"}
            i += 1
    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(100):
            st.retry_keys("failed_only")
            st.target_ids_for("job")
            st.has_family(("job",))
            st.outstanding_rows()
            st.summary()
            st.load_identity_map()
    finally:
        stop.set()
        t.join()


# ── the synthetic 25K state-row generator for live QA ──────────────────────

def test_gen_state_rows_produces_unique_merge_ready_rows():
    from tests.gen_state_rows import generate_rows
    rows = generate_rows(25_000, source_workspace_id="999")
    assert len(rows) == 25_000
    keys = {(r["asset_type"], r["natural_key"]) for r in rows}
    assert len(keys) == 25_000
    assert set(rows[0]) == set(StateStore._STATE_COLS)
    assert all(r["source_workspace_id"] == "999" for r in rows)
