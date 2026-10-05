"""PLAN 16.2 — customer release fixes (offline tests, plan §1–§6).

  §1  logging truth: a resumed unit says so; the manifest phase has a `Phase complete:` line
  §2  `.db_internal` (platform-internal folders) skipped end to end — inventory, export, import,
      state; `main`-written bundles/state included
  §3  dashboard + Genie ACLs resolved by SOURCE ID (acls.json keys them by name, state by path)
  §4  dashboard publish state + schedules + subscriptions as two new unit types
  §5  a retry that heals an object also applies its ACL / its dashboard's publish+schedules;
      `skipped_no_object` is not "done" on a same-run_id resume
  §6  export hard-fails without an inventory (never re-runs it)

`FakeTarget` is a small STATEFUL target workspace (dashboards, publish, schedules, subscriptions,
permissions, workspace paths, SCIM users, notification destinations) whose behaviour follows the
facts verified live in plan §4.0 — e.g. publish mints a new revision each POST (F4), a draft's
`/published` and `/schedules` 404 (F1/F8), re-adding a subscriber is idempotent (F11).
"""
from __future__ import annotations

import io
import json
import os
import re
import tempfile

import pytest

from src.auth.token_manager import HTTPStatusError
from src.config.config_manager import Config
from src.exporters import bundle_paths as BP
from src.exporters.acl_writer import collect_acls
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.asset_export import build_all
from src.importers.acl_importer import AclImporter
from src.importers.dashboards_importer import DashboardsImporter
from src.importers.import_runner import ImportRunner
from src.importers.workspace_importer import WorkspaceImporter
from src.state.state_store import StateStore
from src.transform.transforms import fingerprint, strip_runtime
from src.utils import logger as L
from tests.fakes import FakeClient
from tests.test_state_store import FakeBackend

DASH_NK = "/Users/a@x.com/Sales"
PUB_NK = f"{DASH_NK}#published"


# ═══════════════════════════════ fakes + helpers ═══════════════════════════════

def _nf(path: str) -> HTTPStatusError:
    return HTTPStatusError(404, f"GET https://t/{path} -> 404: NOT_FOUND: Unable to find "
                                f"published dashboard")


class FakeTarget:
    """A stateful target workspace (see module docstring). Every call is recorded."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.dashboards: dict = {}      # id → {display_name, warehouse_id}
        self.published: dict = {}       # id → {embed_credentials, warehouse_id, revision_…}
        self.schedules: dict = {}       # dashboard id → [schedule]
        self.subs: dict = {}            # (dashboard id, schedule id) → {json(subscriber): sub id}
        self.perms: dict = {}           # "dashboards/<id>" → access_control_list (GET shape)
        self.paths: dict = {}           # workspace path → object_id
        self.users: dict = {}           # userName → SCIM id
        self.destinations: list = []
        self.fail_posts: set = set()    # exact POST paths that fail
        self.warnings: list = []
        self._n = 0

    base_url = "https://target.example.net"

    def _id(self, p):
        self._n += 1
        return f"{p}{self._n}"

    def writes(self, verb=None, contains="", endswith=""):
        return [c for c in self.calls if c[0] in ("POST", "PUT", "PATCH", "DELETE")
                and (verb is None or c[0] == verb) and contains in c[1]
                and c[1].endswith(endswith)]

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path == "api/2.0/workspace/get-status":
            p = (params or {}).get("path")
            if p in self.paths:
                return {"path": p, "object_id": self.paths[p], "object_type": "DIRECTORY"}
            raise HTTPStatusError(404, "RESOURCE_DOES_NOT_EXIST")
        m = re.match(r"api/2.0/lakeview/dashboards/([^/]+)/published$", path)
        if m:
            if m.group(1) in self.published:
                return dict(self.published[m.group(1)])
            raise _nf(path)
        m = re.match(r"api/2.0/lakeview/dashboards/([^/]+)/schedules/([^/]+)$", path)
        if m:
            for s in self.schedules.get(m.group(1), []):
                if s["schedule_id"] == m.group(2):
                    return dict(s)
            raise _nf(path)
        if path.startswith("api/2.0/permissions/"):
            return {"access_control_list": self.perms.get(path[len("api/2.0/permissions/"):], [])}
        if path == "api/2.0/preview/scim/v2/Users":
            name = re.search(r'"(.*)"', (params or {}).get("filter", "")).group(1)
            return {"Resources": [{"id": self.users[name]}] if name in self.users else []}
        return {}

    def get_paginated(self, path, result_key, token_key="next_page_token", params=None,
                      max_pages=100000):
        self.calls.append(("GET_PAGINATED", path, params))
        if path == "api/2.0/lakeview/dashboards":
            return [{"dashboard_id": k, **v} for k, v in self.dashboards.items()]
        m = re.match(r"api/2.0/lakeview/dashboards/([^/]+)/schedules$", path)
        if m:
            if m.group(1) not in self.published:
                raise _nf(path)
            return [dict(s) for s in self.schedules.get(m.group(1), [])]
        if path == "api/2.0/notification-destinations":
            return list(self.destinations)
        return []

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path in self.fail_posts:
            raise HTTPStatusError(400, "INVALID_PARAMETER_VALUE: rejected")
        if path == "api/2.0/lakeview/dashboards":
            did = self._id("d")
            self.dashboards[did] = {"display_name": body.get("display_name"),
                                    "warehouse_id": body.get("warehouse_id", "")}
            return {"dashboard_id": did}
        m = re.match(r"api/2.0/lakeview/dashboards/([^/]+)/published$", path)
        if m:
            did = m.group(1)
            self.published[did] = {
                "embed_credentials": bool(body.get("embed_credentials")),
                "warehouse_id": body.get("warehouse_id")
                or self.dashboards.get(did, {}).get("warehouse_id", ""),
                "revision_create_time": self._id("rev")}       # F4: a new revision per POST
            return dict(self.published[did])
        m = re.match(r"api/2.0/lakeview/dashboards/([^/]+)/schedules$", path)
        if m:
            if m.group(1) not in self.published:
                raise _nf(path)                                   # F8
            sch = {**body, "schedule_id": self._id("s"), "etag": "e1"}
            self.schedules.setdefault(m.group(1), []).append(sch)
            return dict(sch)
        m = re.match(r"api/2.0/lakeview/dashboards/([^/]+)/schedules/([^/]+)/subscriptions$",
                     path)
        if m:
            bucket = self.subs.setdefault((m.group(1), m.group(2)), {})
            k = json.dumps(body["subscriber"], sort_keys=True)
            bucket.setdefault(k, self._id("sub"))                 # F11: idempotent
            return {"subscription_id": bucket[k]}
        if path == "api/2.0/workspace/mkdirs":
            self.paths.setdefault(body["path"], self._id("o"))
            return {}
        return {}

    def put(self, path, body):
        self.calls.append(("PUT", path, body))
        if path.startswith("api/2.0/permissions/"):
            acl = []
            for e in body.get("access_control_list", []):
                who = {k: v for k, v in e.items() if k != "permission_level"}
                acl.append({**who, "all_permissions": [
                    {"permission_level": e["permission_level"], "inherited": False}]})
            self.perms[path[len("api/2.0/permissions/"):]] = acl
            return {}
        m = re.match(r"api/2.0/lakeview/dashboards/([^/]+)/schedules/([^/]+)$", path)
        if m:
            scheds = self.schedules.get(m.group(1), [])
            for i, s in enumerate(scheds):
                if s["schedule_id"] == m.group(2):
                    scheds[i] = {**body, "schedule_id": m.group(2), "etag": "e2"}   # F10
            return {}
        return {}

    def patch(self, path, body, params=None):
        self.calls.append(("PATCH", path, body))
        return {}

    def delete(self, path, params=None):
        self.calls.append(("DELETE", path, params))
        return {}


def _cfg(tmp, *, dry_run=False, retry_mode="off", pause=True, import_assets=None, **imports):
    imp = {"state_catalog": "c", "state_schema": "s", "retry_mode": retry_mode}
    if import_assets is not None:
        imp["import_assets"] = import_assets
    imp.update(imports)
    return Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r1",
                             "target_staging_location": tmp, "dry_run": dry_run,
                             "imports": imp, "transform": {"pause_job_schedules": pause}})


class Env:
    """One target + one state table + one bundle dir, reused across "runs" in a test."""

    def __init__(self, **cfg_kw):
        self.tmp = tempfile.mkdtemp()
        self.client = FakeTarget()
        self.backend = FakeBackend()
        self.cfg_kw = cfg_kw
        self.new_run(**cfg_kw)

    def new_run(self, run_id="r1", **cfg_kw):
        self.cfg = _cfg(self.tmp, **({**self.cfg_kw, **cfg_kw}))
        self.cfg.run_id = run_id
        self.aw = ArtifactWriter(self.cfg)
        self.aw.ensure_output_path()
        self.state = StateStore(self.backend, self.cfg)
        self.state.ensure_table()
        self.state.load(force=True)
        self.context: dict = {}
        return self

    def importer(self, cls, units, retry_keys=None, identity_map=None):
        by_type: dict = {}
        for u in units:
            by_type.setdefault(u["asset_type"], []).append(u)
        return cls(self.client, self.cfg, self.aw, state=self.state, units_by_type=by_type,
                   identity_map=identity_map or {}, context=self.context, retry_keys=retry_keys)

    def run(self, cls, units, **kw):
        imp = self.importer(cls, units, **kw)
        res = imp.run()
        self.state.flush()
        return res, imp

    def seed(self, asset_type, key, *, action="created", fp="", target="", source=""):
        self.state.record(asset_type, key, action=action, fingerprint=fp,
                          target_object_id=target, source_object_id=source)
        self.state.flush()


def _rows(res, asset_type=None):
    return {r["natural_key"]: r for r in res.units
            if asset_type is None or r["asset_type"] == asset_type}


@pytest.fixture
def driver():
    cell, drv = io.StringIO(), io.StringIO()
    L.configure_logging(run_id="r1", stage="import", level="INFO", cell_stream=cell,
                        driver_stream=drv)
    yield drv
    L.configure_logging(run_id="-", stage="-", level="INFO", cell_stream=io.StringIO(),
                        driver_stream=io.StringIO())


# A source inventory slice: one published + scheduled dashboard (publisher credentials), one draft.
def _dash_record(did="D1", *, state="published", embed=True, schedules=None, update_time="",
                 rev="2026-10-01T10:00:00Z", dab=False, name="Sales"):
    return {"dashboard_id": did, "display_name": name, "warehouse_id": "WH1",
            "parent_path": "/Users/a@x.com", "path": f"/Users/a@x.com/{name}.lvdash.json",
            "update_time": update_time, "deployed_by_dab": dab, "dab_scope": "",
            "serialized_dashboard": "{}", "publish_state": state,
            "published": ({"embed_credentials": embed, "warehouse_id": "WH1",
                           "revision_create_time": rev} if state == "published" else {}),
            "schedules": schedules if schedules is not None else [],
            "acl": [{"user_name": "bob@x.com",
                     "all_permissions": [{"permission_level": "CAN_RUN", "inherited": False}]}]}


def _sched(sid="S1", subs=None, pause="UNPAUSED", name="Daily", cron="0 0 8 * * ?"):
    return {"schedule_id": sid, "display_name": name,
            "cron_schedule": {"quartz_cron_expression": cron, "timezone_id": "UTC"},
            "pause_status": pause, "warehouse_id": "",
            "subscribers": subs if subs is not None else []}


def _inventory(*dashboards):
    return {"lakeview_dashboard": list(dashboards),
            "sql": [{"sql_type": "warehouse", "id": "WH1", "name": "wh", "_natural_key": "wh"}]}


def _bundle_units(objects_by_type):
    """export build_all → the flat unit list import sees (payload kept, as `_units_from_bundle`)."""
    return [u for units in build_all(objects_by_type).values() for u in units]


def _wh_unit():
    return {"asset_type": "sql_warehouse", "natural_key": "wh", "source_id": "WH1",
            "fingerprint": "f", "import_action": "create", "export_status": "success",
            "payload": {}}


def _env_with_wh(**kw):
    env = Env(**kw)
    env.seed("sql_warehouse", "wh", target="TWH")
    return env


# ═══════════════════════════════ §1 logging truth ═══════════════════════════════

def test_resumed_unit_logs_resumed_from_checkpoint_with_no_decide_and_no_call(driver):
    env = Env()
    unit = {"asset_type": "directory", "natural_key": "/Shared/x", "source_id": "1",
            "fingerprint": "f", "import_action": "create", "export_status": "success",
            "payload": {"path": "/Shared/x"}}
    env.aw.mark_done_bulk("import:workspace", ["/Shared/x"], {"/Shared/x": {
        "import_status": "created", "target_id": "/Shared/x", "note": "made",
        "asset_type": "directory"}})
    imp = env.importer(WorkspaceImporter, [unit])
    imp.existing_keys = lambda: {}
    res = imp.run()
    assert res.units[0]["import_status"] == "created"
    log = driver.getvalue()
    assert "directory /Shared/x → created (resumed from checkpoint — no API call this run)" in log
    assert "decide directory /Shared/x" not in log
    assert not env.client.writes()


def test_resumed_failure_is_still_an_error_line(driver):
    env = Env()
    unit = {"asset_type": "directory", "natural_key": "/Shared/y", "source_id": "1",
            "fingerprint": "f", "import_action": "create", "export_status": "success",
            "payload": {}}
    env.aw.mark_done_bulk("import:workspace", ["/Shared/y"], {"/Shared/y": {
        "import_status": "failed", "note": "boom", "asset_type": "directory"}})
    imp = env.importer(WorkspaceImporter, [unit])
    imp.existing_keys = lambda: {}
    imp.run()
    assert re.search(r"ERROR .*directory /Shared/y → failed \(resumed from checkpoint",
                     driver.getvalue())


def _runner(env):
    return ImportRunner(env.client, env.cfg, env.aw, state=env.state)


def test_verify_bundle_logs_phase_complete_on_success(driver):
    env = Env()
    env.aw.write_json("export/x.json", {"a": 1})
    env.aw.write_manifest({"x": 1})
    _runner(env).verify_bundle()
    assert re.search(r"Phase complete: verify bundle manifest — \d+ files, 0 missing, "
                     r"0 mismatched \(", driver.getvalue())


def test_verify_bundle_logs_phase_complete_skipped(driver):
    env = Env(skip_manifest_verify=True)
    _runner(env).verify_bundle()
    assert "Phase complete: verify bundle manifest — SKIPPED" in driver.getvalue()


# ═══════════════════════════════ §2 .db_internal ═══════════════════════════════

def test_collector_records_db_internal_without_descending_or_reading_its_acl():
    from src.collectors.workspace_collector import WorkspaceCollector
    tbl = {"api/2.0/workspace/list": lambda p: {
        "/": {"objects": [{"path": "/Users", "object_type": "DIRECTORY", "object_id": "1"}]},
        "/Users": {"objects": [{"path": "/Users/a@x.com", "object_type": "DIRECTORY",
                                "object_id": "2"}]},
        "/Users/a@x.com": {"objects": [
            {"path": "/Users/a@x.com/.db_internal", "object_type": "DIRECTORY", "object_id": "3"},
            {"path": "/Users/a@x.com/nb", "object_type": "NOTEBOOK", "object_id": "4"}]},
    }.get(p["path"], {"objects": [{"path": p["path"] + "/child", "object_type": "FILE",
                                   "object_id": "9"}]})}
    seen = []
    client = FakeClient(get_table=tbl)
    orig = client.get

    def spy(path, params=None):
        seen.append((path, (params or {}).get("path")))
        return orig(path, params)
    client.get = spy
    cfg = Config.from_dict({"role": "source", "source_workspace_id": "1"})
    objs = WorkspaceCollector(client, cfg).discover()
    by_path = {o["path"]: o for o in objs}
    internal = by_path["/Users/a@x.com/.db_internal"]
    assert internal["migration_note"] == "platform-internal (Databricks-owned) — not migrated"
    assert internal["acl"] is None and internal["platform_internal"] is True
    assert ("api/2.0/permissions/directories/3", None) not in seen
    assert ("api/2.0/workspace/list", "/Users/a@x.com/.db_internal") not in seen
    assert not any(p.startswith("/Users/a@x.com/.db_internal/") for p in by_path)
    assert ("api/2.0/permissions/notebooks/4", None) in seen      # normal objects unchanged


def test_export_marks_internal_dir_skip_internal_with_no_acl_entry_and_same_fingerprint():
    path = "/Users/a@x.com/.db_internal"
    acl = [{"user_name": "a@x.com", "all_permissions": [{"permission_level": "CAN_MANAGE"}]}]
    obt = {"workspace_object": [
        {"path": path, "object_type": "DIRECTORY", "object_id": "3", "acl": acl},
        {"path": "/Users/a@x.com/keep", "object_type": "DIRECTORY", "object_id": "5", "acl": acl}]}
    units = {u["natural_key"]: u for u in build_all(obt)["directory"]}
    assert units[path]["import_action"] == "skip_internal"
    assert units["/Users/a@x.com/keep"]["import_action"] == "create"
    # fingerprint exactly as before 16.2 (payload {"path"}) → no mass UPDATE for main rows
    assert units[path]["fingerprint"] == fingerprint(strip_runtime("directory", {"path": path}))
    keys = {e["natural_key"] for e in collect_acls(obt)}
    assert path not in keys and "/Users/a@x.com/keep" in keys


def test_export_excel_has_a_label_for_skip_internal():
    from src.exporters.export_excel import _IMPORT_ACTION_LABEL
    assert "platform-internal" in _IMPORT_ACTION_LABEL["skip_internal"]


def _dir(path, action="create"):
    return {"asset_type": "directory", "natural_key": path, "source_id": "3",
            "fingerprint": fingerprint({"path": path}), "import_action": action,
            "export_status": "success", "payload": {"path": path}}


def test_import_skips_internal_dir_with_zero_calls_and_labels_it():
    env = Env()
    res, _ = env.run(WorkspaceImporter, [_dir("/Users/a@x.com/.db_internal", "skip_internal"),
                                         _dir("/Users/a@x.com/.ide", "create")])  # main bundle
    for row in res.units:
        assert row["import_status"] == "skipped"
        assert row["action_taken"] == "Skipped — platform-internal"
        assert row["skip_reason"] == "platform_internal"
    assert env.client.calls == [], "no existence probe and no write for platform-internal paths"
    assert env.state.row("directory", "/Users/a@x.com/.db_internal")["last_action"] == "skipped"


def test_main_created_internal_dir_row_is_re_recorded_skipped():
    env = Env()
    env.seed("directory", "/Users/a@x.com/.db_internal", action="created",
             fp=fingerprint({"path": "/Users/a@x.com/.db_internal"}))
    env.run(WorkspaceImporter, [_dir("/Users/a@x.com/.db_internal", "skip_internal")])
    assert env.state.row("directory", "/Users/a@x.com/.db_internal")["last_action"] == "skipped"


def test_main_internal_acl_row_is_not_marked_deleted_in_source():
    env = Env()
    env.seed("acl", "directories:/Users/a@x.com/.db_internal", action="skipped_no_object")
    env.seed("acl", "jobs:gone-job", action="created", target="j1")
    gone = env.state.mark_missing_in_source("acl", {"jobs:other"})
    assert gone == ["jobs:gone-job"]
    assert env.state.row("acl", "directories:/Users/a@x.com/.db_internal")["last_action"] == \
        "skipped_no_object"


def test_main_bundle_internal_acl_unit_is_skipped_without_a_call():
    env = Env()
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [{
        "asset_type": "directory", "natural_key": "/Users/a@x.com/.db_internal",
        "source_id": "3", "perm_object_type": "directories",
        "grants": [{"principal": "a@x.com", "principal_type": "user",
                    "permission_level": "CAN_MANAGE", "inherited": False}]}])
    res, _ = env.run(AclImporter, [])
    (row,) = res.units
    assert row["import_status"] == "skipped" and row["skip_reason"] == "platform_internal"
    assert not [c for c in env.client.calls if c[0] != "GET" or "permissions" in c[1]
                or "get-status" in c[1]]


def test_import_report_status_cell_reads_skipped_platform_internal():
    from src.reports.import_report import _status_label
    assert _status_label({"import_status": "skipped", "skip_reason": "platform_internal"})[0] == \
        "Skipped — platform-internal"
    assert _status_label({"import_status": "skipped"})[0] == "Skipped (unchanged)"


# ═══════════════════════════════ §3 dashboard + genie ACLs ═══════════════════════════════

def _acl_entry(asset_type, name, source_id, perm, principal="bob@x.com", level="CAN_RUN"):
    return {"asset_type": asset_type, "natural_key": name, "source_id": source_id,
            "perm_object_type": perm,
            "grants": [{"principal": principal, "principal_type": "user",
                        "permission_level": level, "inherited": False}]}


def _main_shaped_dash_and_genie_state(env):
    # state rows keyed by PATH (as main wrote them), source ids = dashboard_id / space_id
    env.seed("lakeview_dashboard", DASH_NK, target="TD1", source="D1")
    env.seed("genie_space", "/Users/a@x.com/Ask", target="TG1", source="G1")


def test_name_keyed_acls_resolve_to_path_keyed_state_by_source_id():
    env = Env()
    _main_shaped_dash_and_genie_state(env)
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [
        _acl_entry("lakeview_dashboard", "Sales", "D1", "dashboards"),
        _acl_entry("genie_space", "Ask", "G1", "genie", level="CAN_EDIT")])
    res, _ = env.run(AclImporter, [])
    assert {r["import_status"] for r in res.units} == {"created"}
    puts = {c[1]: c[2] for c in env.client.writes("PUT")}
    assert puts["api/2.0/permissions/dashboards/TD1"]["access_control_list"] == [
        {"user_name": "bob@x.com", "permission_level": "CAN_RUN"}]
    assert "api/2.0/permissions/genie/TG1" in puts


def test_dashboard_created_this_run_is_resolved_for_its_acl():
    env = _env_with_wh()
    units = _bundle_units(_inventory(_dash_record(state="draft")))
    env.run(DashboardsImporter, units + [_wh_unit()])
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [_acl_entry("lakeview_dashboard", "Sales", "D1",
                                                       "dashboards")])
    res, _ = env.run(AclImporter, units)
    (row,) = res.units
    assert row["import_status"] == "created"
    tid = env.state.get_target_id("lakeview_dashboard", DASH_NK)
    assert env.client.writes("PUT", f"permissions/dashboards/{tid}")


def test_main_skipped_no_object_dashboard_acl_row_is_reattempted_and_applied():
    env = Env()
    _main_shaped_dash_and_genie_state(env)
    entry = _acl_entry("lakeview_dashboard", "Sales", "D1", "dashboards")
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [entry])
    fp = fingerprint(AclImporter._normalise_grants(None, entry["grants"], remap=False))
    env.seed("acl", "dashboards:Sales", action="skipped_no_object", fp=fp)
    res, _ = env.run(AclImporter, [])
    assert res.units[0]["import_status"] == "created"
    assert env.client.writes("PUT", "permissions/dashboards/TD1")


def test_parity_sheet_lists_dashboards_and_genie():
    env = Env()
    _main_shaped_dash_and_genie_state(env)
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [
        _acl_entry("lakeview_dashboard", "Sales", "D1", "dashboards"),
        _acl_entry("genie_space", "Ask", "G1", "genie")])
    env.run(AclImporter, [])
    parity = env.context["acl_parity"]
    objs = {(o["perm_object_type"], o["target_id"]): o["verdict"] for o in parity["objects"]}
    assert objs == {("dashboards", "TD1"): "match", ("genie", "TG1"): "match"}


def test_parity_includes_dashboard_whose_acl_row_was_not_applied():
    """An object that IS on target but whose ACL row never got a target id still gets verified."""
    env = Env(import_assets=["identity", "sql", "dashboards", "acls"])
    _main_shaped_dash_and_genie_state(env)
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [_acl_entry("lakeview_dashboard", "Sales", "D1",
                                                       "dashboards")])
    imp = env.importer(AclImporter, [])
    imp._source_acls = imp.staging.read_json(BP.EXPORT_ACLS_JSON)
    imp.write_parity_report()
    (obj,) = env.context["acl_parity"]["objects"]
    assert obj["target_id"] == "TD1" and obj["verdict"] == "missing_on_target"


def test_absence_reason_is_family_not_selected_only_when_it_really_is():
    entry = _acl_entry("lakeview_dashboard", "Sales", "D1", "dashboards")
    for assets, expected in ((["identity", "acls"], "family_not_selected"),
                             (["all"], "object_absent")):
        env = Env(import_assets=assets)
        env.aw.write_json(BP.EXPORT_ACLS_JSON, [entry])
        res, _ = env.run(AclImporter, [])
        (row,) = res.units
        assert row["import_status"] == "skipped_no_object"
        assert row["failure_category"] == expected, assets


def test_dab_dashboard_acl_is_reported_dab_redeploy():
    env = Env()
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [_acl_entry("lakeview_dashboard", "Sales", "D1",
                                                       "dashboards")])
    units = _bundle_units(_inventory(_dash_record(dab=True)))
    res, _ = env.run(AclImporter, units)
    assert res.units[0]["failure_category"] == "dab_redeploy"


def test_object_ref_maps_each_perm_type_to_its_object_unit():
    env = Env()
    imp = env.importer(AclImporter, _bundle_units(_inventory(_dash_record())))

    def ref(perm, at, key, src=""):
        return imp.object_ref({"source_id": src, "payload": {
            "perm_object_type": perm, "object_asset_type": at, "object_natural_key": key}})
    assert ref("dashboards", "lakeview_dashboard", "Sales", "D1") == ("lakeview_dashboard",
                                                                     DASH_NK)
    assert ref("genie", "genie_space", "Ask", "G-unknown") == ("genie_space", "Ask")
    assert ref("notebooks", "notebook", "/Users/a/nb") == ("notebook", "/Users/a/nb")
    assert ref("directories", "directory", "/Shared/d") == ("directory", "/Shared/d")
    assert ref("files", "workspace_file", "/Shared/f.py") == ("workspace_file", "/Shared/f.py")
    assert ref("jobs", "job", "etl") == ("job", "etl")
    assert ref("clusters", "cluster", "c1") == ("cluster", "c1")
    assert ref("alertsv2", "alert_v2", "a1") == ("alert_v2", "a1")
    assert ref("secret-scope", "secret_scope", "sc") == ("secret_scope", "sc")
    assert ref("sql/warehouses", "", "wh") == ("sql_warehouse", "wh")


# ═══════════════════════════════ §4 publish + schedules ═══════════════════════════════

def test_collector_draft_404_makes_no_schedule_calls():
    from src.collectors.dashboards_collector import DashboardsCollector

    class C(FakeClient):
        def get(self, path, params=None):
            self.seen = getattr(self, "seen", []) + [path]
            if path.endswith("/published"):
                raise _nf(path)
            return super().get(path, params)

        def get_paginated(self, path, *a, **k):
            self.seen = getattr(self, "seen", []) + [path]
            return super().get_paginated(path, *a, **k)
    client = C(paginated_table={"api/2.0/lakeview/dashboards": [{"dashboard_id": "D1"}]})
    (d,) = DashboardsCollector(client, Config.from_dict({})).discover()
    assert d["publish_state"] == "draft" and d["schedules"] == []
    assert not [p for p in client.seen if "schedules" in p]
    assert _bundle_units(_inventory(d)) and all(
        u["asset_type"] == "lakeview_dashboard" for u in _bundle_units({"lakeview_dashboard": [d]}))


def test_collector_published_collects_schedules_and_resolves_subscribers():
    from src.collectors.dashboards_collector import DashboardsCollector, resolve_subscriber_users
    client = FakeClient(
        get_table={
            "api/2.0/lakeview/dashboards/D1": {"display_name": "Sales", "parent_path": "/Users/a",
                                               "update_time": "2026-10-02T00:00:00Z"},
            "api/2.0/lakeview/dashboards/D1/published": {
                "embed_credentials": True, "warehouse_id": "WH1",
                "revision_create_time": "2026-10-01T00:00:00Z"},
            "api/2.0/notification-destinations/N1": {"display_name": "ops-mail",
                                                     "destination_type": "EMAIL"}},
        paginated_table={
            "api/2.0/lakeview/dashboards": [{"dashboard_id": "D1"}],
            "api/2.0/lakeview/dashboards/D1/schedules": [{
                "schedule_id": "S1", "display_name": "Daily", "pause_status": "UNPAUSED",
                "cron_schedule": {"quartz_cron_expression": "0 0 8 * * ?", "timezone_id": "UTC"}}],
            "api/2.0/lakeview/dashboards/D1/schedules/S1/subscriptions": [
                {"subscriber": {"user_subscriber": {"user_id": "U1"}}},
                {"subscriber": {"user_subscriber": {"user_id": "U-gone"}}},
                {"subscriber": {"destination_subscriber": {"destination_id": "N1"}}}]})
    (d,) = DashboardsCollector(client, Config.from_dict({})).discover()
    assert d["publish_state"] == "published" and d["published"]["embed_credentials"] is True
    obt = {"lakeview_dashboard": [d],
           "identity": [{"identity_type": "user", "id": "U1", "userName": "tanveer@x.com"}]}
    assert resolve_subscriber_users(obt) == 1
    subs = d["schedules"][0]["subscribers"]
    assert {"kind": "user", "user_id": "U1", "user_name": "tanveer@x.com"} in subs
    assert {"kind": "user", "user_id": "U-gone", "unresolved": True} in subs
    assert {"kind": "destination", "destination_id": "N1", "display_name": "ops-mail",
            "destination_type": "EMAIL"} in subs
    sched = [u for u in _bundle_units(obt) if u["asset_type"] == "lakeview_dashboard_schedule"]
    assert sched[0]["payload"]["subscribers"] == sorted([
        {"kind": "destination", "display_name": "ops-mail", "destination_type": "EMAIL"},
        {"kind": "user", "user_name": "tanveer@x.com"},
        {"kind": "user", "user_id": "U-gone", "unresolved": True}],
        key=lambda x: json.dumps(x, sort_keys=True))


def test_collector_other_publish_error_is_unknown_warning_and_no_units(driver):
    from src.collectors.dashboards_collector import DashboardsCollector

    class C(FakeClient):
        def get(self, path, params=None):
            if path.endswith("/published"):
                raise HTTPStatusError(403, "PERMISSION_DENIED")
            return super().get(path, params)
    client = C(paginated_table={"api/2.0/lakeview/dashboards": [{"dashboard_id": "D1"}]})
    (d,) = DashboardsCollector(client, Config.from_dict({})).discover()
    assert d["publish_state"] == "unknown"
    assert "publish state unreadable" in driver.getvalue()
    assert [u["asset_type"] for u in _bundle_units({"lakeview_dashboard": [d]})] == [
        "lakeview_dashboard"]


def test_lakeview_dashboard_fingerprint_is_identical_to_main():
    """`main` built the dashboard unit from exactly these four fields — 16.2's new record fields
    (publish state, schedules, update_time) must not move it, or every upgraded run mass-UPDATEs."""
    rec = _dash_record(schedules=[_sched()], update_time="2026-10-09T00:00:00Z")
    unit = [u for u in _bundle_units(_inventory(rec)) if u["asset_type"] == "lakeview_dashboard"][0]
    main_payload = {"display_name": "Sales", "warehouse_id": "WH1", "serialized_dashboard": "{}",
                    "parent_path": "/Users/a@x.com"}
    assert unit["fingerprint"] == fingerprint(strip_runtime("lakeview_dashboard", main_payload))
    assert unit["natural_key"] == DASH_NK


def _published_env(embed=True, schedules=None, **kw):
    env = _env_with_wh(**kw)
    units = _bundle_units(_inventory(_dash_record(embed=embed, schedules=schedules)))
    return env, units + [_wh_unit()]


def test_publish_create_posts_once_and_reads_back():
    env, units = _published_env()
    res, _ = env.run(DashboardsImporter, units)
    row = _rows(res)[PUB_NK]
    assert row["import_status"] == "created"
    assert row["note"] == "published (publisher credentials)"
    did = env.state.get_target_id("lakeview_dashboard", DASH_NK)
    assert len(env.client.writes("POST", "/published")) == 1
    assert env.client.published[did]["embed_credentials"] is True
    assert env.client.published[did]["warehouse_id"] == "TWH"
    gets = [c for c in env.client.calls if c[0] == "GET" and c[1].endswith(f"{did}/published")]
    assert gets, "the publish was not read back"


def test_publish_viewer_credentials_mode_is_preserved():
    env, units = _published_env(embed=False)
    res, _ = env.run(DashboardsImporter, units)
    assert _rows(res)[PUB_NK]["note"] == "published (viewer credentials)"
    did = env.state.get_target_id("lakeview_dashboard", DASH_NK)
    assert env.client.published[did]["embed_credentials"] is False


def test_publish_rerun_unchanged_is_skip_with_zero_writes():
    env, units = _published_env()
    env.run(DashboardsImporter, units)
    before = len(env.client.writes())
    env.new_run("r2")
    res, _ = env.run(DashboardsImporter, units)
    assert _rows(res)[PUB_NK]["import_status"] == "skipped"
    assert len(env.client.writes()) == before


def test_publish_adopt_same_posts_nothing_and_adopt_diff_posts_once():
    for target_embed, expect_status, posts in ((True, "adopted", 0), (False, "updated", 1)):
        env, units = _published_env()
        env.client.dashboards["TD"] = {"display_name": "Sales", "warehouse_id": "TWH"}
        env.client.published["TD"] = {"embed_credentials": target_embed, "warehouse_id": "TWH",
                                      "revision_create_time": "x"}
        env.seed("lakeview_dashboard", DASH_NK, target="TD", source="D1",
                 fp=[u for u in units if u["asset_type"] == "lakeview_dashboard"][0]["fingerprint"])
        res, _ = env.run(DashboardsImporter, units)
        assert _rows(res)[PUB_NK]["import_status"] == expect_status
        assert len(env.client.writes("POST", "/published")) == posts
        assert env.client.published["TD"]["embed_credentials"] is True


def test_publish_fingerprint_moved_republishes_once():
    env, units = _published_env()
    env.run(DashboardsImporter, units)
    env.new_run("r2")
    moved = _bundle_units(_inventory(_dash_record(rev="2026-10-03T00:00:00Z"))) + [_wh_unit()]
    res, _ = env.run(DashboardsImporter, moved)
    assert _rows(res)[PUB_NK]["import_status"] == "updated"
    assert len(env.client.writes("POST", "/published")) == 2


def test_publish_parent_failed_is_skipped_no_object():
    env, units = _published_env()
    env.client.fail_posts.add("api/2.0/lakeview/dashboards")
    res, _ = env.run(DashboardsImporter, units)
    rows = _rows(res)
    assert rows[DASH_NK]["import_status"] == "failed"
    assert rows[PUB_NK]["import_status"] == "skipped_no_object"
    assert rows[PUB_NK]["failure_category"] == "unit_failed_earlier"
    assert not env.client.writes("POST", "/published")


def test_source_unpublished_is_flagged_never_deleted_even_with_allow_deletes():
    env, units = _published_env(allow_deletes=True)
    env.run(DashboardsImporter, units)
    draft_units = _bundle_units(_inventory(_dash_record(state="draft"))) + [_wh_unit()]
    env.new_run("r2")
    runner = _runner(env)
    runner._report_deleted_in_source({t: [u for u in draft_units if u["asset_type"] == t]
                                      for t in {u["asset_type"] for u in draft_units}},
                                     ["dashboards"])
    row = env.state.row("lakeview_dashboard_publish", PUB_NK)
    assert row["last_action"] == "deleted_in_source"
    assert "unpublish by hand" in row["last_error"]
    assert not env.client.writes("DELETE")


def test_unpublished_changes_warning_and_none_for_embed_alone():
    env = _env_with_wh()
    units = _bundle_units(_inventory(_dash_record(update_time="2026-10-02T00:00:00Z",
                                                  rev="2026-10-01T00:00:00Z"))) + [_wh_unit()]
    res, _ = env.run(DashboardsImporter, units)
    row = _rows(res)[PUB_NK]
    assert row["import_status"] == "created_with_warning"
    assert "source draft has unpublished changes — target published = current draft" in row["note"]
    env2, units2 = _published_env(embed=True)
    res2, _ = env2.run(DashboardsImporter, units2)
    assert _rows(res2)[PUB_NK]["import_status"] == "created"


SCHED_NK = f"{DASH_NK}#schedule:S1"


def test_schedule_create_paused_when_pause_job_schedules_true_else_source_value():
    for pause, expected in ((True, "PAUSED"), (False, "UNPAUSED")):
        env, units = _published_env(schedules=[_sched()], pause=pause)
        res, _ = env.run(DashboardsImporter, units)
        row = _rows(res)[SCHED_NK]
        assert row["import_status"] == "created"
        did = env.state.get_target_id("lakeview_dashboard", DASH_NK)
        (sch,) = env.client.schedules[did]
        assert sch["pause_status"] == expected
        assert sch["cron_schedule"] == {"quartz_cron_expression": "0 0 8 * * ?",
                                        "timezone_id": "UTC"}
        if pause:
            assert "PAUSED (pause_job_schedules=true; source is UNPAUSED)" in row["note"]


def test_identical_existing_schedule_is_adopted_not_duplicated():
    env, units = _published_env(schedules=[_sched()])
    env.client.dashboards["TD"] = {"display_name": "Sales", "warehouse_id": "TWH"}
    env.client.published["TD"] = {"embed_credentials": True, "warehouse_id": "TWH",
                                  "revision_create_time": "x"}
    env.client.schedules["TD"] = [{"schedule_id": "HUMAN", "display_name": "Daily", "etag": "e",
                                   "pause_status": "UNPAUSED", "cron_schedule": {
                                       "quartz_cron_expression": "0 0 8 * * ?",
                                       "timezone_id": "UTC"}}]
    env.seed("lakeview_dashboard", DASH_NK, target="TD", source="D1")
    res, _ = env.run(DashboardsImporter, units)
    row = _rows(res)[SCHED_NK]
    assert row["import_status"] == "adopted" and row["target_id"] == "HUMAN"
    assert not env.client.writes("POST", "/schedules")
    assert len(env.client.schedules["TD"]) == 1


def test_schedule_update_uses_etag_and_full_body():
    env, units = _published_env(schedules=[_sched()])
    env.run(DashboardsImporter, units)
    env.new_run("r2")
    moved = _bundle_units(_inventory(_dash_record(
        schedules=[_sched(cron="0 0 9 * * ?")]))) + [_wh_unit()]
    res, _ = env.run(DashboardsImporter, moved)
    assert _rows(res)[SCHED_NK]["import_status"] == "updated"
    (put,) = env.client.writes("PUT", "/schedules/")
    body = put[2]
    assert body["etag"] == "e1"
    assert body["display_name"] == "Daily" and body["pause_status"] == "PAUSED"
    assert body["cron_schedule"]["quartz_cron_expression"] == "0 0 9 * * ?"
    assert len(env.client.writes("POST", endswith="/schedules")) == 1, "only the first run's create"


def _subs():
    return [{"kind": "user", "user_id": "U1", "user_name": "tanveer@x.com"},
            {"kind": "user", "user_id": "U2", "user_name": "nobody@x.com"},
            {"kind": "destination", "destination_id": "N1", "display_name": "wsmig_test_dest",
             "destination_type": "EMAIL"},
            {"kind": "destination", "destination_id": "N2", "display_name": "missing_dest",
             "destination_type": "EMAIL"}]


def test_subscriptions_users_by_name_destinations_by_name_and_manual_lines():
    env, units = _published_env(schedules=[_sched(subs=_subs())])
    env.client.users["tanveer@x.com"] = "TU1"
    env.client.destinations = [{"id": "TN1", "display_name": "wsmig_test_dest",
                                "destination_type": "EMAIL"}]
    res, _ = env.run(DashboardsImporter, units)
    row = _rows(res)[SCHED_NK]
    assert row["import_status"] == "created_with_warning"
    posted = [c[2]["subscriber"] for c in env.client.writes("POST", "/subscriptions")]
    assert sorted(posted, key=json.dumps) == sorted(
        [{"user_subscriber": {"user_id": "TU1"}},
         {"destination_subscriber": {"destination_id": "TN1"}}], key=json.dumps)
    assert "user nobody@x.com is not on target" in row["note"]
    assert "create notification destination missing_dest on target, then re-run" in row["note"]


def test_subscriber_user_id_comes_from_the_identity_map_first():
    env, units = _published_env(schedules=[_sched(subs=[_subs()[0]])])
    imp = env.importer(DashboardsImporter, units,
                       identity_map={"scim_ids": {"user:tanveer@x.com": "MAPPED"}})
    imp.run()
    assert [c[2]["subscriber"] for c in env.client.writes("POST", "/subscriptions")] == [
        {"user_subscriber": {"user_id": "MAPPED"}}]
    assert not [c for c in env.client.calls if "scim" in c[1]]


def test_viewer_credentials_no_subscriber_post_and_one_comma_separated_note():
    from src.reports.import_report import _render_manual_actions
    subs = [{"kind": "user", "user_name": "alice@x.com"},
            {"kind": "user", "user_name": "bob@x.com"}]
    env, units = _published_env(embed=False, schedules=[_sched(subs=subs)])
    env.client.users.update({"alice@x.com": "A", "bob@x.com": "B"})
    res, _ = env.run(DashboardsImporter, units)
    row = _rows(res)[SCHED_NK]
    assert row["import_status"] == "created_with_warning"
    assert not env.client.writes("POST", "/subscriptions")
    expected = ("published with Individual data permissions — subscribers must re-subscribe "
                "themselves (Databricks only allows self-subscription): alice@x.com, bob@x.com")
    assert expected in row["note"]
    md = _render_manual_actions({"run_id": "r1"}, res.units)
    assert md.count(expected) == 1
    assert "Dashboard schedule subscribers — recreate by hand" in md


def test_rerun_adds_no_new_subscriptions():
    env, units = _published_env(schedules=[_sched(subs=[_subs()[0]])])
    env.client.users["tanveer@x.com"] = "TU1"
    env.run(DashboardsImporter, units)
    env.new_run("r2")
    res, _ = env.run(DashboardsImporter, units)
    assert _rows(res)[SCHED_NK]["import_status"] == "skipped"
    assert len(env.client.writes("POST", "/subscriptions")) == 1
    assert sum(len(v) for v in env.client.subs.values()) == 1


def test_pending_subscriber_is_rechecked_and_healed_on_rerun():
    env, units = _published_env(schedules=[_sched(subs=[_subs()[2]])])
    res, _ = env.run(DashboardsImporter, units)
    assert _rows(res)[SCHED_NK]["import_status"] == "created_with_warning"
    env.client.destinations = [{"id": "TN1", "display_name": "wsmig_test_dest",
                                "destination_type": "EMAIL"}]
    env.new_run("r2")
    res, _ = env.run(DashboardsImporter, units)
    row = _rows(res)[SCHED_NK]
    assert row["import_status"] == "adopted" and "1 subscriber(s) subscribed" in row["note"]
    assert len(env.client.writes("POST", endswith="/schedules")) == 1, \
        "the schedule itself is never re-created"


def test_main_state_dashboard_rows_only_then_publish_and_schedule_create():
    env, units = _published_env(schedules=[_sched()])
    dash = [u for u in units if u["asset_type"] == "lakeview_dashboard"][0]
    env.client.dashboards["TD"] = {"display_name": "Sales", "warehouse_id": "TWH"}
    env.seed("lakeview_dashboard", DASH_NK, target="TD", source="D1", fp=dash["fingerprint"])
    res, _ = env.run(DashboardsImporter, units)
    rows = _rows(res)
    assert rows[DASH_NK]["import_status"] == "skipped"
    assert rows[PUB_NK]["import_status"] == "created"
    assert rows[SCHED_NK]["import_status"] == "created"
    assert not env.client.writes("PATCH")


def test_dry_run_says_would_publish_and_would_create_schedule_with_no_post():
    env, units = _published_env(schedules=[_sched()], dry_run=True)
    res, _ = env.run(DashboardsImporter, units)
    rows = _rows(res)
    assert rows[PUB_NK]["note"] == "dry run: would publish (publisher credentials)"
    assert rows[SCHED_NK]["note"].startswith("dry run: would create schedule (PAUSED)")
    assert not env.client.writes()


def test_dab_dashboard_children_are_dab_redeploy_and_skipped():
    env = _env_with_wh()
    units = _bundle_units(_inventory(_dash_record(dab=True, schedules=[_sched()]))) + [_wh_unit()]
    kids = [u for u in units if u["asset_type"].startswith("lakeview_dashboard_")]
    assert {u["import_action"] for u in kids} == {"dab_redeploy"} and len(kids) == 2
    res, _ = env.run(DashboardsImporter, units)
    assert {r["import_status"] for r in res.units if r["asset_type"] != "sql_warehouse"} == {
        "skipped"}
    assert not env.client.writes()


def test_schedule_on_unpublished_parent_is_a_retryable_prerequisite():
    env, units = _published_env(schedules=[_sched()])
    did_path = "api/2.0/lakeview/dashboards/d1/published"
    env.client.fail_posts.add(did_path)
    res, _ = env.run(DashboardsImporter, units)
    rows = _rows(res)
    assert rows[PUB_NK]["import_status"] == "failed"
    assert rows[SCHED_NK]["import_status"] == "failed"
    assert rows[SCHED_NK]["failure_category"] == "prerequisite_missing"


def test_publish_read_back_mismatch_is_failed_not_applied():
    env, units = _published_env()
    orig = env.client.post

    def lying_post(path, body):
        out = orig(path, body)
        if path.endswith("/published"):
            env.client.published[path.split("/")[-2]]["embed_credentials"] = False
        return out
    env.client.post = lying_post
    res, _ = env.run(DashboardsImporter, units)
    row = _rows(res)[PUB_NK]
    assert row["import_status"] == "failed" and row["failure_category"] == "not_applied"


def test_new_types_are_in_the_dashboards_family_and_get_their_own_report_sheets():
    from src.importers.phases import FAMILY_ASSET_TYPES
    from src.reports.import_report import _card_for_asset_type
    assert FAMILY_ASSET_TYPES["dashboards"] == ("lakeview_dashboard",
                                                "lakeview_dashboard_publish",
                                                "lakeview_dashboard_schedule")
    assert _card_for_asset_type("lakeview_dashboard_publish") == "dashboard_publish"
    assert _card_for_asset_type("lakeview_dashboard_schedule") == "dashboard_schedules"


def test_main_checkpoint_rows_without_asset_type_still_replay_as_dashboards():
    env = Env()
    env.aw.mark_done_bulk("import:dashboards", [DASH_NK], {DASH_NK: {
        "import_status": "created", "target_id": "TD"}})
    out = _runner(env)._all_checkpoint_outcomes()
    assert f"lakeview_dashboard|{DASH_NK}" in out


def test_inventory_sheet_has_publish_columns():
    from src.reports.inventory_view import _dashboard_publish_cols
    rec = _dash_record(embed=False, schedules=[_sched(subs=[{"kind": "user",
                                                               "user_name": "a@x.com"}])])
    assert _dashboard_publish_cols(rec) == {"_published": "Yes", "_credentials": "viewer",
                                            "_schedules": 1, "_subscribers": "a@x.com"}
    assert _dashboard_publish_cols(_dash_record(state="draft"))["_published"] == "No (draft)"


# ═══════════════════════════════ §5 retry follows the object ═══════════════════════════════

def _retry_env(fail_dir_again=False):
    env = Env()
    path = "/Shared/team"
    env.seed("directory", path, action="failed", fp="")
    env.seed("acl", f"directories:{path}", action="skipped_no_object", fp="x")
    env.seed("acl", "directories:/Shared/other", action="skipped_no_object", fp="x")
    env.seed("directory", "/Shared/other", action="failed", fp="")
    env.new_run("r2", retry_mode="failed_only")
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [
        {"asset_type": "directory", "natural_key": path, "source_id": "7",
         "perm_object_type": "directories",
         "grants": [{"principal": "bob@x.com", "principal_type": "user",
                     "permission_level": "CAN_EDIT", "inherited": False}]},
        {"asset_type": "directory", "natural_key": "/Shared/other", "source_id": "8",
         "perm_object_type": "directories",
         "grants": [{"principal": "bob@x.com", "principal_type": "user",
                     "permission_level": "CAN_READ", "inherited": False}]}])
    if fail_dir_again:
        env.client.fail_posts.add("api/2.0/workspace/mkdirs")
    units = {"directory": [_dir(path)]}
    runner = _runner(env)
    runner._run_phase("workspace", units)
    runner._run_phase("acls", units)
    return env, runner


def test_failed_only_retry_applies_the_acl_of_the_healed_object():
    env, runner = _retry_env()
    acl_rows = {r["natural_key"]: r for r in runner.results[-1].units}
    assert acl_rows["directories:/Shared/team"]["import_status"] == "created"
    assert env.client.writes("PUT", "permissions/directories/")
    # an ACL whose object was NOT acted on stays out of the work list
    assert acl_rows["directories:/Shared/other"].get("retry_out_of_scope") is True


def test_failed_only_retry_does_not_attempt_the_acl_when_the_object_fails_again():
    env, runner = _retry_env(fail_dir_again=True)
    acl_rows = {r["natural_key"]: r for r in runner.results[-1].units}
    assert acl_rows["directories:/Shared/team"].get("retry_out_of_scope") is True
    assert not env.client.writes("PUT")


def test_retried_dashboard_comes_back_published_and_scheduled():
    env, units = _published_env(schedules=[_sched()])
    env.client.fail_posts.add("api/2.0/lakeview/dashboards")
    env.run(DashboardsImporter, units)
    env.client.fail_posts.clear()
    env.new_run("r2", retry_mode="failed_only")
    res, _ = env.run(DashboardsImporter, units,
                     retry_keys=env.state.retry_keys("failed_only"))
    rows = _rows(res)
    assert rows[DASH_NK]["import_status"] == "created"
    assert rows[PUB_NK]["import_status"] == "created"
    assert rows[SCHED_NK]["import_status"] == "created"


def test_same_run_rerun_redecides_a_skipped_no_object_checkpoint_entry():
    env = Env()
    _main_shaped_dash_and_genie_state(env)
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [_acl_entry("lakeview_dashboard", "Sales", "D1",
                                                       "dashboards")])
    env.aw.mark_done_bulk("import:acls", ["dashboards:Sales"], {"dashboards:Sales": {
        "import_status": "skipped_no_object", "asset_type": "acl", "note": "absent"}})
    res, _ = env.run(AclImporter, [])
    assert res.units[0]["import_status"] == "created"
    assert env.client.writes("PUT", "permissions/dashboards/TD1")


# ═══════════════════════════════ §6 export needs an inventory ═══════════════════════════════

def _export_env():
    tmp = tempfile.mkdtemp()
    cfg = Config.from_dict({"role": "source", "source_workspace_id": "111", "run_id": "R9",
                            "source_staging_location": tmp, "target_staging_location": tmp})
    return cfg, ArtifactWriter(cfg)


@pytest.mark.parametrize("content,why", [(None, "is ABSENT"), ("{not json", "is unreadable"),
                                         (json.dumps({"counts": {}}), "has no `objects_by_type`")])
def test_export_hard_fails_without_a_usable_inventory(content, why, monkeypatch):
    from src.collectors import inventory_runner
    from src.exporters.export_runner import ExportRunner, InventoryMissingError

    def boom(*a, **k):
        raise AssertionError("export must never construct InventoryRunner")
    monkeypatch.setattr(inventory_runner.InventoryRunner, "__init__", boom)
    cfg, aw = _export_env()
    if content is not None:
        os.makedirs(os.path.dirname(os.path.join(aw.root, BP.INVENTORY_JSON)), exist_ok=True)
        with open(os.path.join(aw.root, BP.INVENTORY_JSON), "w") as f:
            f.write(content)
    before = {os.path.join(dp, n) for dp, _d, ns in os.walk(aw.root) for n in ns}
    with pytest.raises(InventoryMissingError) as ei:
        ExportRunner(FakeClient(), cfg, aw).run()
    msg = str(ei.value)
    assert os.path.join(aw.root, BP.INVENTORY_JSON) in msg and "'R9'" in msg
    assert "run 01_Inventory first" in msg and why in msg
    after = {os.path.join(dp, n) for dp, _d, ns in os.walk(aw.root) for n in ns}
    assert after == before, "nothing may be written to the bundle"


def test_export_with_a_valid_inventory_still_runs():
    from src.exporters.export_runner import ExportRunner
    cfg, aw = _export_env()
    aw.ensure_output_path()
    aw.write_json(BP.INVENTORY_JSON, {"objects_by_type": {}})
    out = ExportRunner(FakeClient(), cfg, aw).run()
    assert out["total"] == 0 and os.path.isfile(os.path.join(aw.root, BP.MANIFEST_JSON))
