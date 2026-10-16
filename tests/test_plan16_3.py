"""PLAN 16.3 — customer fixes, wave 2 / release 1.2.0 (offline tests, plan §1–§8).

  §1  B9       each notebook ends with the run's folder; a blank-run_id import uses LATEST_EXPORT.json;
               inventory/export resume only the NEWEST folder
  §2  B4       workspace settings are read back after the write; a target that differs is re-applied
  §3  B11      a report that does not land turns the run `completed_no_report` (task red)
  §4  B2       a migrated SQL query gets its SOURCE owner (verified by a GET)
  §5  CUST-1   pipeline ACLs keep the CURRENT owner (source owner → CAN_MANAGE); an SP-owned source
               keeps running as that SP via spec `run_as`
  §6  CUST-2   a pipeline with no source code (a synced table's) is `manual`, never attempted
  §7  QA16.2-1 "folder does not exist" / "not published" only after a live check returns a real 404
  §8  QA16.2.1-1  Alert V2 `evaluation.state`/`last_evaluated_at` stay out of the fingerprint

`Bed` extends the 16.2 `FakeTarget` with the APIs this wave touches, following the facts verified
live in plan §0 (V3 workspace-conf strings/null, V4 query owner PATCH, V5 pipeline PUT needs exactly
one IS_OWNER, V6 run_as rules, V7 the pipelines LIST carries run_as_user_name).
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile

import pytest

from src.auth.token_manager import ApiClient, HTTPStatusError
from src.exporters import bundle_paths as BP
from src.exporters import bundle_state as bs
from src.exporters.asset_export import build_all
from src.importers.acl_importer import AclImporter
from src.importers.base_importer import BaseImporter
from src.importers.dashboards_importer import DashboardsImporter
from src.importers.dlt_importer import DltImporter
from src.importers.genie_importer import GenieImporter
from src.importers.import_runner import (RUN_STATUS_NO_REPORT, RUN_STATUS_STATE_NOT_SAVED,
                                         ImportRunner, resolve_import_run_id)
from src.importers.misc_importer import MiscImporter
from src.importers.sql_importer import SqlImporter
from src.transform.transforms import STRIP_FIELDS, strip_runtime
from src.utils.helpers import pipeline_not_creatable
from tests.test_plan16_2 import Env, FakeTarget, _rows, _runner

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = "runner-sp-0000-0000-0000-000000000000"     # the migration identity (an SP)
SP_SRC = "11111111-2222-3333-4444-555555555555"      # an account SP (same appId on target)
HOME = "/Users/alice@corp.com"


# ═══════════════════════════════ the fake target ═══════════════════════════════


# The live 403s (2026-10-14, plan16_3/api_probes/2026-10-14_runas_refusal.json), as ApiClient folds them.
REFUSED_RUN_AS = ("POST https://t/api/2.0/pipelines -> 403: PERMISSION_DENIED Cannot bind the "
                  "service principal provided in 'run_as' field ({}) to the pipeline. The user "
                  "creating or updating the pipeline must have 'servicePrincipal.user' role on the "
                  "service principal.")
NO_CATALOG = ("POST https://t/api/2.0/pipelines -> 403: PERMISSION_DENIED Principal '{}' does not "
              "have USE CATALOG on Catalog 'main'.")

class Bed(FakeTarget):
    """FakeTarget + workspace-conf, SQL queries/alerts, pipelines (+ their owner rules), Genie, SCIM Me."""

    def __init__(self):
        super().__init__()
        self.conf: dict = {}                 # workspace-conf key → string value (V3)
        self.ignored_conf: set = set()       # keys the workspace silently ignores
        self.queries: dict = {}              # qid → {display_name, owner_user_name}
        self.owner_patch_error = None        # an exception the owner PATCH raises
        self.owner_ignored = False           # the owner PATCH returns 200 but changes nothing
        self.alerts: dict = {}
        self.pipelines: dict = {}            # pid → {name, run_as_user_name}
        self.refuse_run_as: set = set()      # SPs the runner may not set as run_as
        self.no_catalog_run_as: set = set()  # SPs that lack USE CATALOG (a different 403)
        self.perm_get_error = None
        self.genie_error = None              # an exception the Genie create raises
        self.status_errors: dict = {}        # workspace path → exception from get-status

    # ── GET ──
    def get(self, path, params=None):
        if path == "api/2.0/workspace-conf":
            self.calls.append(("GET", path, params))
            keys = [k for k in (params or {}).get("keys", "").split(",") if k]
            return {k: self.conf.get(k) for k in keys}
        if path == "api/2.0/workspace/get-status":
            p = (params or {}).get("path")
            if p in self.status_errors:
                self.calls.append(("GET", path, params))
                raise self.status_errors[p]
        m = re.match(r"api/2.0/sql/queries/([^/]+)$", path)
        if m:
            self.calls.append(("GET", path, params))
            return {"id": m.group(1), **self.queries[m.group(1)]}
        if path == "api/2.0/preview/scim/v2/Me":
            self.calls.append(("GET", path, params))
            return {"userName": RUNNER}
        m = re.match(r"api/2.0/permissions/pipelines/([^/]+)$", path)
        if m and self.perm_get_error is not None:
            self.calls.append(("GET", path, params))
            raise self.perm_get_error
        return super().get(path, params)

    def get_paginated(self, path, result_key, token_key="next_page_token", params=None,
                      max_pages=100000):
        if path == "api/2.0/sql/queries":
            self.calls.append(("GET_PAGINATED", path, params))
            return [{"id": k, **v} for k, v in self.queries.items()]
        if path == "api/2.0/alerts":
            self.calls.append(("GET_PAGINATED", path, params))
            return [{"id": k, **v} for k, v in self.alerts.items()]
        if path == "api/2.0/pipelines":
            self.calls.append(("GET_PAGINATED", path, params))
            return [{"pipeline_id": k, **v} for k, v in self.pipelines.items()]
        return super().get_paginated(path, result_key, token_key, params, max_pages)

    # ── writes ──
    def post(self, path, body):
        if path == "api/2.0/sql/queries":
            self.calls.append(("POST", path, body))
            qid = self._id("q")
            self.queries[qid] = {"display_name": body["query"].get("display_name"),
                                 "owner_user_name": RUNNER}
            return {"id": qid, "owner_user_name": RUNNER}
        if path == "api/2.0/alerts":
            self.calls.append(("POST", path, body))
            aid = self._id("a")
            self.alerts[aid] = {"display_name": body.get("display_name")}
            return {"id": aid}
        if path == "api/2.0/pipelines":
            self.calls.append(("POST", path, copy.deepcopy(body)))
            if not any(body.get("libraries") or []) and not body.get("ingestion_definition"):
                raise HTTPStatusError(400, "INVALID_PARAMETER_VALUE: libraries must contain at "
                                           "least one element")
            self._check_run_as(body)
            pid = self._id("p")
            self.pipelines[pid] = {"name": body["name"], "run_as_user_name": self._run_as(body)}
            self.perms[f"pipelines/{pid}"] = [_owner_entry(RUNNER)]
            return {"pipeline_id": pid}
        if path == "api/2.0/genie/spaces":
            self.calls.append(("POST", path, body))
            if self.genie_error is not None:
                raise self.genie_error
            return {"space_id": self._id("g")}
        return super().post(path, body)

    def put(self, path, body):
        m = re.match(r"api/2.0/pipelines/([^/]+)$", path)
        if m:
            self.calls.append(("PUT", path, copy.deepcopy(body)))
            self._check_run_as(body)
            self.pipelines[m.group(1)]["run_as_user_name"] = self._run_as(body)
            return {}
        m = re.match(r"api/2.0/permissions/pipelines/([^/]+)$", path)
        if m:
            owners = [e for e in body["access_control_list"] if e["permission_level"] == "IS_OWNER"]
            if len(owners) != 1:                                                  # V5
                self.calls.append(("PUT", path, body))
                raise HTTPStatusError(400, "INVALID_PARAMETER_VALUE: The pipeline must have "
                                           "exactly one owner.")
            current = _owner_of(self.perms.get(f"pipelines/{m.group(1)}", []))
            new = next(v for k, v in owners[0].items() if k != "permission_level")
            if current and new != current:                                        # CUST-1
                self.calls.append(("PUT", path, body))
                raise HTTPStatusError(403, "PERMISSION_DENIED: Only metastore admins can change "
                                           "pipeline owner")
        return super().put(path, body)

    def patch(self, path, body, params=None):
        if path == "api/2.0/workspace-conf":
            self.calls.append(("PATCH", path, body))
            for k, v in body.items():
                if k not in self.ignored_conf:
                    self.conf[k] = v
            return {}
        m = re.match(r"api/2.0/sql/queries/([^/]+)$", path)
        if m and body.get("update_mask") == "owner_user_name":
            self.calls.append(("PATCH", path, body))
            if self.owner_patch_error is not None:
                raise self.owner_patch_error
            if not self.owner_ignored:
                self.queries[m.group(1)]["owner_user_name"] = body["query"]["owner_user_name"]
            return {}
        return super().patch(path, body, params)

    # ── helpers ──
    def _run_as(self, body):
        ra = body.get("run_as") or {}
        return ra.get("service_principal_name") or ra.get("user_name") or RUNNER

    def _check_run_as(self, body):
        ra = body.get("run_as") or {}
        if ra.get("user_name") and ra["user_name"] != RUNNER:                     # V6
            raise HTTPStatusError(400, "INVALID_PARAMETER_VALUE: You can only set run_as to "
                                       "yourself, a service principal, or a group")
        if ra.get("service_principal_name") in self.refuse_run_as:
            raise HTTPStatusError(403, REFUSED_RUN_AS.format(ra["service_principal_name"]))
        if ra.get("service_principal_name") in self.no_catalog_run_as:
            raise HTTPStatusError(403, NO_CATALOG.format(ra["service_principal_name"]))

    def owner_patches(self):
        return [c for c in self.writes("PATCH", "sql/queries/")
                if c[2].get("update_mask") == "owner_user_name"]


def _owner_entry(principal, field="service_principal_name"):
    return {field: principal, "all_permissions": [{"permission_level": "IS_OWNER",
                                                    "inherited": False}]}


def _owner_of(acl):
    for e in acl:
        if any(p["permission_level"] == "IS_OWNER" and not p.get("inherited")
               for p in e.get("all_permissions", [])):
            return next(v for k, v in e.items() if k != "all_permissions")
    return ""


def _bed(**kw) -> Env:
    env = Env(**kw)
    env.client = Bed()
    return env


def _unit(asset_type, nk, payload, *, source_id="s1", fp=None, action="create", **extra):
    return {"asset_type": asset_type, "natural_key": nk, "source_id": source_id,
            "fingerprint": fp or f"fp-{nk}", "import_action": action,
            "export_status": "success", "payload": payload, **extra}


# ═══════════════════════════════ §8 Alert V2 evaluation runtime ═══════════════════════════════

def _alert_record(**evaluation):
    ev = {"source": {"name": "c", "aggregation": "MAX"}, "comparison_operator": "GREATER_THAN",
          "threshold": {"value": {"double_value": 5}}, "notification": {"notify_on_ok": False},
          "state": "OK", "last_evaluated_at": "2026-10-12T09:00:00Z"}
    ev.update(evaluation)
    return {"sql_type": "alert", "id": "a1", "_natural_key": f"{HOME}/al",
            "_raw": {"id": "a1", "display_name": "al", "query_text": "select 1",
                     "parent_path": HOME, "evaluation": ev,
                     "schedule": {"quartz_cron_schedule": "0 0 9 * * ?", "timezone_id": "UTC"}}}


def _alert_unit(record):
    (u,) = build_all({"sql": [record]})["alert_v2"]
    return u


def test_alert_v2_two_exports_differing_only_in_evaluation_runtime_have_equal_fingerprints():
    a = _alert_unit(_alert_record())
    b = _alert_unit(_alert_record(state="TRIGGERED", last_evaluated_at="2026-10-13T09:00:00Z"))
    assert a["fingerprint"] == b["fingerprint"]


def test_alert_v2_evaluation_only_change_is_skipped_with_zero_patch():
    env = _bed()
    env.seed("sql_warehouse", "wh", target="TWH", source="w1")
    first = _alert_unit(_alert_record())
    env.client.alerts["TA1"] = {"display_name": "al"}
    env.seed("alert_v2", first["natural_key"], fp=first["fingerprint"], target="TA1")
    later = _alert_unit(_alert_record(state="TRIGGERED", last_evaluated_at="2026-10-14T09:00:00Z"))
    res, _ = env.run(SqlImporter, [later])
    assert _rows(res)[later["natural_key"]]["import_status"] == "skipped"
    assert not env.client.writes("PATCH", "api/2.0/alerts/")


class LiveAlertsPage(Bed):
    """`GET /api/2.0/alerts` returns `{"alerts": [...]}` (verified live 2026-10-15), paged by the
    REAL `ApiClient.get_paginated` — the plain fakes ignore `result_key`, which hid QA16.3-2."""
    warnings: list = []

    def get(self, path, params=None):
        if path == "api/2.0/alerts":
            return {"alerts": [{"id": k, **v} for k, v in self.alerts.items()]}
        return super().get(path, params)

    def get_paginated(self, path, result_key, token_key="next_page_token", params=None,
                      max_pages=100000):
        if path == "api/2.0/alerts":
            self.calls.append(("GET_PAGINATED", path, params))
            return ApiClient.get_paginated(self, path, result_key, token_key=token_key,
                                           params=params, max_pages=max_pages)
        return super().get_paginated(path, result_key, token_key, params, max_pages)


def test_alert_v2_already_on_target_is_found_by_the_live_list_shape_and_skipped():
    """QA16.3-2: the existence check read the list under `results`, so every alert looked absent →
    a create POST every run → 409 → `adopted`. It must be found and skipped with no write."""
    env = _bed()
    env.client = LiveAlertsPage()
    env.seed("sql_warehouse", "wh", target="TWH", source="w1")
    unit = _alert_unit(_alert_record())
    env.client.alerts["TA1"] = {"display_name": "al"}
    env.seed("alert_v2", unit["natural_key"], fp=unit["fingerprint"], target="TA1")
    res, _ = env.run(SqlImporter, [unit])
    assert _rows(res)[unit["natural_key"]]["import_status"] == "skipped"
    assert not env.client.writes("POST", "api/2.0/alerts")
    assert not env.client.writes("PATCH", "api/2.0/alerts/")


@pytest.mark.parametrize("edit", [
    {"threshold": {"value": {"double_value": 9}}},
    {"comparison_operator": "LESS_THAN"},
    {"source": {"name": "other"}},
])
def test_alert_v2_real_evaluation_edit_moves_the_fingerprint(edit):
    assert _alert_unit(_alert_record(**edit))["fingerprint"] != \
        _alert_unit(_alert_record())["fingerprint"]


def test_alert_v2_schedule_or_query_edit_moves_the_fingerprint():
    base = _alert_unit(_alert_record())["fingerprint"]
    r = _alert_record()
    r["_raw"]["schedule"] = {"quartz_cron_schedule": "0 0 10 * * ?", "timezone_id": "UTC"}
    assert _alert_unit(r)["fingerprint"] != base
    r = _alert_record()
    r["_raw"]["query_text"] = "select 2"
    assert _alert_unit(r)["fingerprint"] != base


def test_alert_v2_stripped_payload_keeps_every_create_field_of_evaluation():
    ev = _alert_unit(_alert_record())["payload"]["evaluation"]
    assert set(ev) == {"source", "comparison_operator", "threshold", "notification"}


def test_alert_v2_real_edit_is_an_update():
    env = _bed()
    env.seed("sql_warehouse", "wh", target="TWH", source="w1")
    old = _alert_unit(_alert_record())
    env.client.alerts["TA1"] = {"display_name": "al"}
    env.seed("alert_v2", old["natural_key"], fp=old["fingerprint"], target="TA1")
    new = _alert_unit(_alert_record(threshold={"value": {"double_value": 9}}))
    res, _ = env.run(SqlImporter, [new])
    assert _rows(res)[new["natural_key"]]["import_status"] == "updated"
    (patch,) = env.client.writes("PATCH", "api/2.0/alerts/TA1")
    assert patch[2]["evaluation"]["threshold"] == {"value": {"double_value": 9}}


def test_alert_v2_1x_payload_with_runtime_fields_is_sent_without_them():
    env = _bed()
    env.seed("sql_warehouse", "wh", target="TWH", source="w1")
    payload = copy.deepcopy(_alert_record()["_raw"])      # 1.1.1 kept evaluation.* in the payload
    for f in ("id",):
        payload.pop(f)
    res, _ = env.run(SqlImporter, [_unit("alert_v2", f"{HOME}/al", payload, source_id="a1")])
    (post,) = env.client.writes("POST", endswith="api/2.0/alerts")
    assert "state" not in post[2]["evaluation"] and "last_evaluated_at" not in post[2]["evaluation"]
    assert post[2]["evaluation"]["comparison_operator"] == "GREATER_THAN"
    assert payload["evaluation"]["state"] == "OK", "the bundle unit must not be mutated"


def test_strip_runtime_never_mutates_the_record():
    raw = _alert_record()["_raw"]
    before = copy.deepcopy(raw)
    out = strip_runtime("alert_v2", raw)
    assert raw == before
    assert "state" not in out["evaluation"] and out["evaluation"] is not raw["evaluation"]


def test_only_alert_v2_uses_nested_strip_paths():
    nested = {at: [p for p in pats if "." in p] for at, pats in STRIP_FIELDS.items()}
    assert {at: p for at, p in nested.items() if p} == {
        "alert_v2": ["evaluation.state", "evaluation.last_evaluated_at"]}


# Per type with GET-by-id enrichment: a server-side refresh of every volatile field it returns
# (timestamps, state, etag, evaluation runtime) must not move the fingerprint (§8 audit guard).
_VOLATILE = {
    "alert_v2": lambda: [_alert_record(),
                         _alert_record(state="TRIGGERED", last_evaluated_at="2027-01-01T00:00:00Z")],
    "legacy_query": lambda: [
        {"sql_type": "legacy_query", "id": "q1", "_natural_key": f"{HOME}/q",
         "_raw": {"display_name": "q", "id": "q1", "query_text": "select 1",
                  "update_time": t, "create_time": t, "lifecycle_state": "ACTIVE",
                  "last_modifier_user_name": who, "owner_user_name": "alice@corp.com"}}
        for t, who in (("2026-01-01", "a@x"), ("2026-02-02", "b@x"))],
    "lakeview_dashboard": lambda: [
        {"dashboard_id": "d1", "display_name": "KPIs", "warehouse_id": "w1",
         "serialized_dashboard": "{}", "parent_path": HOME, "update_time": t, "etag": e,
         "lifecycle_state": "ACTIVE"} for t, e in (("2026-01-01", "1"), ("2026-02-02", "2"))],
    "genie_space": lambda: [
        {"space_id": "g1", "title": "G", "warehouse_id": "w1", "serialized_space": "{}",
         "parent_path": HOME, "update_time": t} for t in ("2026-01-01", "2026-02-02")],
    "job": lambda: [
        {"job_id": "j1", "name": "nightly", "created_time": t, "creator_user_name": "a@x",
         "settings": {"name": "nightly", "tasks": [{"task_key": "t"}]}}
        for t in (1, 2)],
    "dlt_pipeline": lambda: [
        {"pipeline_id": "p1", "name": "bronze", "state": s, "latest_updates": [{"u": s}],
         "spec": {"id": "p1", "name": "bronze", "libraries": [{"notebook": {"path": "/n"}}]},
         "_raw": {"run_as_user_name": SP_SRC, "state": s}} for s in ("IDLE", "RUNNING")],
}
_BUCKET = {"alert_v2": "sql", "legacy_query": "sql", "lakeview_dashboard": "lakeview_dashboard",
           "genie_space": "genie_space", "job": "job", "dlt_pipeline": "dlt_pipeline"}


@pytest.mark.parametrize("asset_type", sorted(_VOLATILE))
def test_server_refresh_of_volatile_fields_does_not_move_the_fingerprint(asset_type):
    a, b = _VOLATILE[asset_type]()
    fa = build_all({_BUCKET[asset_type]: [a]})[asset_type][0]["fingerprint"]
    fb = build_all({_BUCKET[asset_type]: [b]})[asset_type][0]["fingerprint"]
    assert fa == fb


# ═══════════════════════════════ fingerprint stability (all types) ═══════════════════════════════

# The same inventory run through 1.1.1's `build_all` (main @ 51e3a08). Only alert_v2 WITH evaluation
# runtime fields may differ (D4); everything else must be byte-identical, or the first upgraded run
# would mass-UPDATE.
GOLDEN_INVENTORY = {
    "identity": [
        {"identity_type": "user", "id": "u1", "userName": "alice@corp.com",
         "classification": "entra_user", "_raw": {"id": "u1", "userName": "alice@corp.com",
                                                  "entitlements": [{"value": "workspace-access"}]}},
        {"identity_type": "service_principal", "id": "s1", "applicationId": "app-dbx",
         "classification": "db_managed_sp", "has_secrets": True,
         "_raw": {"id": "s1", "applicationId": "app-dbx", "displayName": "sp-one"}},
        {"identity_type": "group", "id": "g1", "displayName": "eng",
         "classification": "db_managed_group",
         "_raw": {"id": "g1", "displayName": "eng", "members": [{"value": "u1"}]}},
    ],
    "compute": [
        {"compute_type": "cluster", "cluster_id": "c1", "_natural_key": "cl", "acl": [],
         "_raw": {"cluster_name": "cl", "cluster_id": "c1", "state": "RUNNING",
                  "node_type_id": "n1", "spark_version": "15.4.x-scala2.12"}},
        {"compute_type": "cluster_policy", "policy_id": "p1", "_natural_key": "pol",
         "_raw": {"name": "pol", "policy_id": "p1", "definition": "{}"}},
        {"compute_type": "instance_pool", "instance_pool_id": "ip1", "_natural_key": "pool",
         "_raw": {"instance_pool_name": "pool", "instance_pool_id": "ip1",
                  "node_type_id": "n1", "state": "ACTIVE"}},
    ],
    "workspace_object": [
        {"object_type": "DIRECTORY", "path": "/Users/alice@corp.com/d", "object_id": "1"},
        {"object_type": "NOTEBOOK", "path": "/Users/alice@corp.com/nb", "object_id": "10",
         "language": "PYTHON"},
        {"object_type": "FILE", "path": "/Users/alice@corp.com/data.csv", "object_id": "12"},
    ],
    "secret_scope": [
        {"name": "dbx", "backend_type": "DATABRICKS", "key_names": ["k1"],
         "acls": [{"principal": "eng", "permission": "READ"}], "_raw": {"name": "dbx"}},
    ],
    "job": [
        {"job_id": "j1", "name": "nightly", "deployed_by_dab": False,
         "settings": {"name": "nightly", "tasks": [{"task_key": "t"}],
                      "schedule": {"quartz_cron_expression": "0 0 1 * * ?",
                                   "timezone_id": "UTC"}}, "acl": [], "_raw": {}},
    ],
    "sql": [
        {"sql_type": "warehouse", "id": "w1", "_natural_key": "wh",
         "_raw": {"name": "wh", "id": "w1", "state": "RUNNING", "warehouse_type": "PRO",
                  "cluster_size": "Small"}},
        {"sql_type": "legacy_query", "id": "q1", "_natural_key": "/Users/alice@corp.com/q",
         "_raw": {"display_name": "q", "id": "q1", "query_text": "select 1",
                  "warehouse_id": "w1", "parent_path": "/Users/alice@corp.com",
                  "owner_user_name": "alice@corp.com", "update_time": "2026-10-01T00:00:00Z",
                  "lifecycle_state": "ACTIVE"}},
        {"sql_type": "alert", "id": "a1", "_natural_key": "/Users/alice@corp.com/al",
         "_raw": {"id": "a1", "display_name": "al", "query_text": "select 1",
                  "warehouse_id": "w1", "parent_path": "/Users/alice@corp.com",
                  "evaluation": {"source": {"name": "c", "aggregation": "MAX"},
                                 "comparison_operator": "GREATER_THAN",
                                 "threshold": {"value": {"double_value": 5}},
                                 "notification": {"notify_on_ok": False},
                                 "state": "OK",
                                 "last_evaluated_at": "2026-10-12T09:00:00Z"},
                  "schedule": {"quartz_cron_schedule": "0 0 9 * * ?", "timezone_id": "UTC"}}},
        {"sql_type": "alert", "id": "a2", "_natural_key": "/Users/alice@corp.com/al_never",
         "_raw": {"id": "a2", "display_name": "al_never", "query_text": "select 2",
                  "warehouse_id": "w1", "parent_path": "/Users/alice@corp.com",
                  "evaluation": {"source": {"name": "c"},
                                 "comparison_operator": "LESS_THAN",
                                 "threshold": {"value": {"double_value": 1}}},
                  "schedule": {"quartz_cron_schedule": "0 0 9 * * ?", "timezone_id": "UTC"}}},
    ],
    "dlt_pipeline": [
        {"pipeline_id": "dp1", "name": "bronze", "deployed_by_dab": False,
         "spec": {"id": "dp1", "name": "bronze", "catalog": "cat", "schema": "s",
                  "libraries": [{"notebook": {"path": "/Users/alice@corp.com/nb"}}]},
         "_raw": {"pipeline_id": "dp1", "name": "bronze",
                  "run_as_user_name": "11111111-2222-3333-4444-555555555555"}, "acl": []},
        {"pipeline_id": "dp2", "name": "Synced table: cat.s.t", "deployed_by_dab": False,
         "spec": {"id": "dp2", "name": "Synced table: cat.s.t", "catalog": "cat",
                  "libraries": []},
         "_raw": {"pipeline_id": "dp2", "run_as_user_name": "alice@corp.com"}, "acl": []},
    ],
    "lakeview_dashboard": [
        {"dashboard_id": "d1", "display_name": "KPIs", "deployed_by_dab": False,
         "warehouse_id": "w1", "serialized_dashboard": "{}", "acl": [],
         "parent_path": "/Users/alice@corp.com", "publish_state": "published",
         "published": {"embed_credentials": True, "warehouse_id": "w1",
                       "revision_create_time": "2026-10-01T10:00:00Z"},
         "update_time": "2026-10-01T09:00:00Z",
         "schedules": [{"schedule_id": "S1", "display_name": "Daily",
                        "cron_schedule": {"quartz_cron_expression": "0 0 8 * * ?",
                                          "timezone_id": "UTC"},
                        "pause_status": "UNPAUSED", "warehouse_id": "", "subscribers": []}]},
    ],
    "genie_space": [
        {"space_id": "gs1", "title": "Genie", "warehouse_id": "w1", "acl": [],
         "description": "d", "serialized_space": '{"version":2}',
         "parent_path": "/Users/alice@corp.com"},
    ],
    "serving_endpoint": [
        {"name": "ext-ep", "migratable": True, "migration_note": "external",
         "config": {"served_entities": [{"external_model": {"name": "gpt"}}]}, "acl": []},
    ],
    "misc": [
        {"misc_type": "global_init_script", "script_id": "gi1", "_natural_key": "gis",
         "name": "gis", "position": 1, "enabled": True, "script_b64": "ZWNobw=="},
        {"misc_type": "workspace_conf", "key": "enableTokensConfig", "value": "true"},
    ],
}

GOLDEN_1_1_1 = {
    "alert_v2|/Users/alice@corp.com/al": "sha256:eb764eb2bb05603214ba088f01a091ce0b6ae10246331e75486171414f2ebe2b",
    "alert_v2|/Users/alice@corp.com/al_never": "sha256:8334fea5e012ab33155189915374dac09d949abc7e19263ed0b8c40ff39dd411",
    "cluster|cl": "sha256:6d6b9ffefef36c13a4fd574253cd3094b57a90a24ddc9d55a19543a47428f757",
    "cluster_policy|pol": "sha256:60adfe127ab47df573797b46675372884caa215a38d4888f38e2155b15bd238d",
    "directory|/Users/alice@corp.com/d": "sha256:bd982362005eac21578c2893a4944aa6b0bfa344dba7c9e0988f17ca85e33a5f",
    "dlt_pipeline|bronze": "sha256:71ca0c9a18eb5e934cfb79b4acbbb3d75846fae25ad2602c4c871fe753c96679",
    "dlt_pipeline|Synced table: cat.s.t": "sha256:ea323625345ddda7b530f25963d313862e285c8b190ec6b5c72814072a9718c5",
    "genie_space|/Users/alice@corp.com/Genie": "sha256:2e4b3f7ad218ac003e26512d1bf48f345869113e387d5a5e0d71b94066312d42",
    "global_init_script|gis": "sha256:86b3e14fa92b573d9065eac54e6d8865bb6fbad1642db48f84c907c9c0ee8ff8",
    "group|eng": "sha256:aad5371ffa8d11866e4ec022d58f3c978e597e62c2d7b9040c17422b6864798a",
    "instance_pool|pool": "sha256:91b0eb1b684272c3028103afa6aa99319c81d6f911ab5fbf3ef823707340480d",
    "job|nightly": "sha256:567d2bf7e362de9820b452770db34b93b93a76a91b6832801462f8a03ae77c17",
    "lakeview_dashboard|/Users/alice@corp.com/KPIs": "sha256:3b0734e1ab7790d6554f9d8a297d0c2b1e823f873c18baa050007adcf3cb885f",
    "lakeview_dashboard_publish|/Users/alice@corp.com/KPIs#published": "sha256:45034b345ce2048d33882cd1148954e0b819798b37c65f2cfad964e4d39778bd",
    "lakeview_dashboard_schedule|/Users/alice@corp.com/KPIs#schedule:S1": "sha256:baf9aa13bee427b3e91dea42a572b8807569371af5c8eded6cc2be857daf6841",
    "legacy_query|/Users/alice@corp.com/q": "sha256:7f7d0ea19a2a5144959d5e774637d7655a37e91bc3815334a82dc36637d6c2ff",
    "notebook|/Users/alice@corp.com/nb": "sha256:4b82dbf7f43ffef7f2c784f594edb74a14165f808ec1199484a23ac3cbb6cbdd",
    "secret_scope|dbx": "sha256:c609b8599313c7df373f67a982cde8e6a15ae2f0781dce0ff6017fe7d0053573",
    "secret_value|dbx/k1": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "service_principal|app-dbx": "sha256:2d0f2efdcfb314128dd42b3fd51574a01348de3041f58b80ba1f612cf06c236c",
    "serving_endpoint|ext-ep": "sha256:a357d237de852f4dd35dd265f85dba42c08db5bdba8115d33977b0a7cf4bc866",
    "sql_warehouse|wh": "sha256:811a6940c82d5719c4a04bd0fddbd6dac717f48200bd34577a83bc763baa2128",
    "user|alice@corp.com": "sha256:9333dbe42655cf9fb385c389d66e0e0e62bbe65ef5696d7489ab50b3e6f51054",
    "workspace_conf|enableTokensConfig": "sha256:62797d56d8e75cb0314c97e7f29cdba4e1cee0a81aecbb2a3bd3131be15f0cf7",
    "workspace_file|/Users/alice@corp.com/data.csv": "sha256:c9029398f0efed7c8830f1bc94324367a34911085649e1509924db979c23f5bf",
}


def _fingerprints(inventory) -> dict:
    return {f"{at}|{u['natural_key']}": u["fingerprint"]
            for at, units in build_all(copy.deepcopy(inventory)).items() for u in units}


def test_fingerprint_stability_every_type_but_alert_v2_matches_1_1_1():
    now = _fingerprints(GOLDEN_INVENTORY)
    assert set(now) == set(GOLDEN_1_1_1)
    moved = {k for k in now if now[k] != GOLDEN_1_1_1[k]}
    assert moved == {"alert_v2|/Users/alice@corp.com/al"}, \
        "only the Alert V2 that carries evaluation runtime fields may move (D4)"


# ═══════════════════════════════ §7 confirm-then-label ═══════════════════════════════

_TABLE_ERR = "INVALID_PARAMETER_VALUE: Table 'cat.schema.tbl' does not exist"
_FOLDER_MSG = ("`{nk}` targets workspace folder `{parent}`, which does not exist on target yet — "
               "provision/assign its owner (a user home is created on first login) or import the "
               "folder's content family first, then re-run with retry_mode=failed_only.")


TEAM = f"{HOME}/team"     # a sub-folder, so the probe of the EXACT parent is told apart from the
                          # (pre-existing, cached) home-root probe of `remap_parent_path`


def _folder_case(kind, err, *, parent=TEAM):
    """(env, importer class, units) for one folder-placed family whose create raises `err`."""
    env = _bed()
    env.client.paths[HOME] = "o0"
    env.client.paths[parent] = "o1"
    if kind == "genie":
        env.client.genie_error = err
        u = _unit("genie_space", f"{parent}/G", {"title": "G", "serialized_space": "{}",
                                                 "parent_path": parent})
        return env, GenieImporter, [u]
    path = {"dashboard": "api/2.0/lakeview/dashboards", "query": "api/2.0/sql/queries",
            "alert": "api/2.0/alerts"}[kind]
    orig = env.client.post

    def post(p, body, _orig=orig, _path=path):
        if p == _path:
            env.client.calls.append(("POST", p, body))
            raise err
        return _orig(p, body)
    env.client.post = post
    payload = {"dashboard": {"display_name": "D", "serialized_dashboard": "{}",
                             "parent_path": parent},
               "query": {"display_name": "Q", "query_text": "select 1", "parent_path": parent},
               "alert": {"display_name": "A", "query_text": "select 1", "parent_path": parent}}[kind]
    at = {"dashboard": "lakeview_dashboard", "query": "legacy_query", "alert": "alert_v2"}[kind]
    cls = DashboardsImporter if kind == "dashboard" else SqlImporter
    return env, cls, [_unit(at, f"{parent}/X", payload)]


def _status_probes(env, parent):
    return [c for c in env.client.calls
            if c[0] == "GET" and c[1] == "api/2.0/workspace/get-status"
            and (c[2] or {}).get("path") == parent]


@pytest.mark.parametrize("kind", ["genie", "dashboard", "query", "alert"])
def test_server_error_is_kept_when_the_folder_exists(kind):
    env, cls, units = _folder_case(kind, HTTPStatusError(400, _TABLE_ERR))
    res, _ = env.run(cls, units)
    row = res.units[-1]
    assert row["import_status"] == "failed"
    assert "cat.schema.tbl" in row["note"] and "targets workspace folder" not in row["note"]
    assert "cat.schema.tbl" in row["error_raw"]
    assert _status_probes(env, TEAM), "the exact folder was checked live"


@pytest.mark.parametrize("kind", ["genie", "dashboard", "query", "alert"])
def test_folder_message_is_1_1_1_text_when_the_folder_check_404s(kind):
    env, cls, units = _folder_case(kind, HTTPStatusError(400, "RESOURCE_DOES_NOT_EXIST: "
                                                               "Parent does not exist"))
    del env.client.paths[TEAM]
    res, _ = env.run(cls, units)
    row = res.units[-1]
    assert row["import_status"] == "failed" and row["failure_category"] == "prerequisite_missing"
    assert row["note"] == _FOLDER_MSG.format(nk=units[0]["natural_key"], parent=TEAM)


@pytest.mark.parametrize("probe_status", [403, 500])
@pytest.mark.parametrize("kind", ["genie", "query"])
def test_folder_probe_error_keeps_the_original_error(kind, probe_status):
    env, cls, units = _folder_case(kind, HTTPStatusError(400, _TABLE_ERR))
    env.client.status_errors[TEAM] = HTTPStatusError(probe_status, "PERMISSION_DENIED: nope"
                                                     if probe_status == 403 else "INTERNAL_ERROR")
    res, _ = env.run(cls, units)
    row = res.units[-1]
    assert "cat.schema.tbl" in row["note"] and "targets workspace folder" not in row["note"]


@pytest.mark.parametrize("kind", ["genie", "query"])
def test_unrelated_error_and_success_make_no_folder_check(kind):
    env, cls, units = _folder_case(kind, HTTPStatusError(400, "INVALID_PARAMETER_VALUE: bad"))
    env.run(cls, units)
    assert not _status_probes(env, TEAM)
    env2 = _bed()
    env2.client.paths.update({HOME: "o0", TEAM: "o1"})
    res, _ = env2.run(GenieImporter if kind == "genie" else SqlImporter, [units[0]])
    assert res.units[0]["import_status"] in ("created", "created_with_warning")
    assert not _status_probes(env2, TEAM)


def test_missing_subfolder_under_an_existing_home_is_our_message():
    sub = f"{HOME}/team/reports"
    env, cls, units = _folder_case("genie", HTTPStatusError(400, "Parent tree node does not exist"),
                                   parent=sub)
    del env.client.paths[sub]
    env.client.paths[HOME] = "o0"
    res, _ = env.run(cls, units)
    assert res.units[-1]["note"] == _FOLDER_MSG.format(nk=units[0]["natural_key"], parent=sub)


def _sched_importer(published):
    """A DashboardsImporter over a Bed whose `GET …/published` answers `published` (200 / 404 / 500)."""
    env = _bed()
    orig = env.client.get

    def get(p, params=None):
        if p.endswith("/published"):
            env.client.calls.append(("GET", p, params))
            if published == 200:
                return {"embed_credentials": True}
            raise HTTPStatusError(published, "GET x -> 404: NOT_FOUND" if published == 404
                                  else "INTERNAL_ERROR")
        return orig(p, params)
    env.client.get = get
    sched = {"asset_type": "lakeview_dashboard_schedule", "natural_key": "D#schedule:S1",
             "parent_natural_key": "D", "payload": {}}
    return env.importer(DashboardsImporter, []), sched


_SCHED_404 = HTTPStatusError(404, "POST x -> 404: NOT_FOUND: no such schedule parent")


def test_schedule_404_and_published_404_is_the_not_published_message():
    from src.importers.base_importer import PrerequisiteMissing
    imp, sched = _sched_importer(404)
    with pytest.raises(PrerequisiteMissing, match="is not PUBLISHED on target"):
        imp._not_published_prerequisite(_SCHED_404, sched, "TD1")


@pytest.mark.parametrize("published", [200, 500])
def test_schedule_404_with_published_200_or_500_keeps_the_server_error(published):
    imp, sched = _sched_importer(published)
    assert imp._not_published_prerequisite(_SCHED_404, sched, "TD1") is None   # caller re-raises
    assert any(c[1].endswith("TD1/published") for c in imp.client.calls)


def test_schedule_error_that_is_not_a_404_makes_no_published_check():
    imp, sched = _sched_importer(404)
    imp._not_published_prerequisite(HTTPStatusError(400, "INVALID_PARAMETER_VALUE"), sched, "TD1")
    assert not imp.client.calls


# ═══════════════════════════════ §3 report write ═══════════════════════════════

def _report_env():
    env = Env()
    env.aw.write_json("export/x.json", {"a": 1})
    env.aw.write_manifest({"x": 1})
    return env


def test_xlsx_render_failure_is_completed_no_report_with_an_error_line(monkeypatch, caplog):
    import src.reports.import_report as R
    env = _report_env()
    monkeypatch.setattr(R, "_render_xlsx", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("openpyxl exploded")))
    summary = _runner(env).run()
    assert summary["run_status"] == RUN_STATUS_NO_REPORT
    errs = summary["reports"]["errors"]
    assert [e["artifact"] for e in errs] == ["xlsx"] and "openpyxl exploded" in errs[0]["error"]
    assert any(r.levelname == "ERROR" and "import_status.xlsx" in r.getMessage()
               for r in caplog.records)
    for rel in (BP.IMPORT_RESULTS_JSON, BP.MANUAL_ACTIONS_IMPORT_MD):
        assert os.path.getsize(os.path.join(env.aw.root, rel)) > 0, "json + md still written"


def test_zero_byte_report_is_completed_no_report(monkeypatch):
    env = _report_env()
    real = env.aw.write_bytes

    def write_bytes(rel, data):
        real(rel, b"" if rel.endswith(".md") else data)
    monkeypatch.setattr(env.aw, "write_bytes", write_bytes)
    summary = _runner(env).run()
    assert summary["run_status"] == RUN_STATUS_NO_REPORT
    assert summary["reports"]["errors"][0]["error"] == "0 bytes after the write"


def test_clean_run_is_completed_with_an_unchanged_reports_dict():
    summary = _runner(_report_env()).run()
    assert summary["run_status"] == "completed"
    assert set(summary["reports"]) == {"json", "xlsx", "manual_actions"}


def test_abort_plus_report_failure_reraises_the_original_exception(monkeypatch):
    import src.reports.import_report as R
    env = _report_env()
    monkeypatch.setattr(R, "write_import_reports", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("report boom")))
    runner = _runner(env)
    monkeypatch.setattr(runner, "_units_from_bundle", lambda: (_ for _ in ()).throw(
        ValueError("the real abort")))
    with pytest.raises(ValueError, match="the real abort"):
        runner.run()
    assert runner.run_status == "aborted"


def test_state_not_saved_keeps_priority_over_a_report_failure(monkeypatch):
    import src.reports.import_report as R
    env = _report_env()
    monkeypatch.setattr(R, "_render_xlsx", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    runner = _runner(env)
    monkeypatch.setattr(env.state, "flush", lambda *a, **k: setattr(env.state, "flush_failed",
                                                                    True) or 0)
    assert runner.run()["run_status"] == RUN_STATUS_STATE_NOT_SAVED


def test_import_notebook_raises_on_completed_no_report():
    src = open(os.path.join(REPO, "notebooks", "04_Import.py"), encoding="utf-8").read()
    assert re.search(r'if result\.get\("run_status"\) == RUN_STATUS_NO_REPORT:\s+.*\n\s+raise '
                     r'RuntimeError', src)
    assert "re-run the import with run_id=" in src


# ═══════════════════════════════ §2 workspace settings ═══════════════════════════════

def _conf_unit(key, value, fp=None):
    return _unit("workspace_conf", key, {"key": key, "value": value}, fp=fp)


def test_conf_read_back_match_is_created_with_the_verified_note():
    env = _bed()
    res, _ = env.run(MiscImporter, [_conf_unit("enableExportNotebook", "false")])
    row = res.units[0]
    assert row["import_status"] == "created"
    assert row["note"] == "enableExportNotebook set to 'false' (verified by read-back)"


def test_conf_read_back_mismatch_is_failed_not_applied_with_no_fingerprint():
    env = _bed()
    env.client.conf["enableExportNotebook"] = "true"
    env.client.ignored_conf.add("enableExportNotebook")
    res, _ = env.run(MiscImporter, [_conf_unit("enableExportNotebook", "false")])
    row = res.units[0]
    assert row["import_status"] == "failed" and row["failure_category"] == "not_applied"
    assert "still reads 'true'" in row["note"] and "admin console" in row["note"]
    st = env.state.row("workspace_conf", "enableExportNotebook")
    assert not st.get("last_source_fingerprint"), "a FAILED row stores no fingerprint"


def test_1x_false_created_row_whose_target_differs_is_patched_and_read_back():
    env = _bed()
    env.client.conf["enableResultsDownloading"] = "true"
    env.seed("workspace_conf", "enableResultsDownloading", fp="fp1", target="enableResultsDownloading")
    res, _ = env.run(MiscImporter, [_conf_unit("enableResultsDownloading", "false", fp="fp1")])
    row = res.units[0]
    assert row["import_status"] == "created" and "verified by read-back" in row["note"]
    assert env.client.writes("PATCH", "workspace-conf")
    assert env.client.conf["enableResultsDownloading"] == "false"


def test_1x_row_whose_target_matches_makes_zero_patch_calls():
    env = _bed()
    env.client.conf["enableResultsDownloading"] = "false"
    env.seed("workspace_conf", "enableResultsDownloading", fp="fp1")
    res, _ = env.run(MiscImporter, [_conf_unit("enableResultsDownloading", "false", fp="fp1")])
    assert res.units[0]["import_status"] == "skipped"
    assert not env.client.writes("PATCH")


@pytest.mark.parametrize("target, source", [("False", "false"), (None, ""), ("30 ", "30")])
def test_conf_values_are_normalised_before_comparing(target, source):
    env = _bed()
    env.client.conf["maxTokenLifetimeDays"] = target
    env.seed("workspace_conf", "maxTokenLifetimeDays", fp="fp1")
    res, _ = env.run(MiscImporter, [_conf_unit("maxTokenLifetimeDays", source, fp="fp1")])
    assert res.units[0]["import_status"] == "skipped" and not env.client.writes("PATCH")


def test_unknown_conf_key_is_still_a_prerequisite_with_no_write():
    env = _bed()
    res, _ = env.run(MiscImporter, [_conf_unit("enableSomethingNew", "true")])
    assert res.units[0]["failure_category"] == "prerequisite_missing"
    assert not env.client.writes("PATCH")


def test_conf_dry_run_names_the_target_value_and_makes_no_patch():
    env = _bed(dry_run=True)
    env.client.conf["enableResultsDownloading"] = "true"
    env.seed("workspace_conf", "enableResultsDownloading", fp="fp1")
    res, _ = env.run(MiscImporter, [_conf_unit("enableResultsDownloading", "false", fp="fp1")])
    assert res.units[0]["note"] == ("dry run: would set `enableResultsDownloading` to 'false' "
                                    "(target currently 'true')")
    assert not env.client.writes("PATCH")


# ═══════════════════════════════ §4 query owner ═══════════════════════════════

def _query_unit(owner="alice@corp.com", fp="fpq", parent=HOME):
    u = _unit("legacy_query", f"{parent}/q", {"display_name": "q", "query_text": "select 1",
                                              "parent_path": parent},
              source_id="q1", fp=fp)
    if owner is not None:
        u["source_owner"] = owner
    return u


def _q_env(**kw):
    env = _bed(**kw)
    env.seed("sql_warehouse", "wh", target="TWH", source="w1")
    env.client.paths[HOME] = "o1"
    return env


def _write_roster(env, *users):
    env.aw.write_json(BP.IDENTITY_CLASSIFICATION_JSON, {"identities": [
        {"identity_type": "user", "userName": u} for u in users]})


def test_query_create_sets_the_remapped_source_owner_and_verifies_it():
    env = _q_env()
    res, _ = env.run(SqlImporter, [_query_unit("alice@corp.com")],
                     identity_map={"user_map": {"alice@corp.com": "alice@new.com"}})
    row = res.units[0]
    (patch,) = env.client.owner_patches()
    assert patch[2] == {"query": {"owner_user_name": "alice@new.com"},
                        "update_mask": "owner_user_name"}
    assert any(c[0] == "GET" and c[1] == f"api/2.0/sql/queries/{row['target_id']}"
               for c in env.client.calls)
    assert row["import_status"] == "created" and "owner set to alice@new.com (verified)" in row["note"]


@pytest.mark.parametrize("failure", ["mismatch", "4xx"])
def test_query_owner_mismatch_or_error_is_a_warning_not_a_failure(failure):
    env = _q_env()
    if failure == "mismatch":
        env.client.owner_ignored = True
    else:
        env.client.owner_patch_error = HTTPStatusError(403, "PERMISSION_DENIED: no")
    res, _ = env.run(SqlImporter, [_query_unit()])
    row = res.units[0]
    assert row["import_status"] == "created_with_warning"
    assert "query left owned by the migration SP" in row["note"]


def test_query_owner_deleted_in_source_gets_no_patch():
    env = _q_env()
    _write_roster(env, "bob@corp.com")             # alice is absent from the source roster
    res, _ = env.run(SqlImporter, [_query_unit("alice@corp.com")])
    assert not env.client.owner_patches()
    assert "deleted in source" in res.units[0]["note"]


def test_query_without_source_owner_makes_zero_extra_calls():
    env = _q_env()
    env.run(SqlImporter, [_query_unit(owner=None)])
    assert not env.client.owner_patches()
    assert not [c for c in env.client.calls if re.match(r"api/2.0/sql/queries/.", c[1])]


def test_unchanged_query_whose_owner_differs_gets_owner_only():
    env = _q_env()
    env.client.queries["TQ1"] = {"display_name": "q", "owner_user_name": RUNNER}
    unit = _query_unit()
    env.seed("legacy_query", unit["natural_key"], fp=unit["fingerprint"], target="TQ1")
    res, _ = env.run(SqlImporter, [unit])
    row = res.units[0]
    assert row["import_status"] == "updated" and "(verified)" in row["note"]
    content = [c for c in env.client.writes("PATCH", "sql/queries/")
               if c[2].get("update_mask") != "owner_user_name"]
    assert not content and len(env.client.owner_patches()) == 1


def test_unchanged_query_whose_owner_matches_makes_zero_writes():
    env = _q_env()
    env.client.queries["TQ1"] = {"display_name": "q", "owner_user_name": "alice@corp.com"}
    unit = _query_unit()
    env.seed("legacy_query", unit["natural_key"], fp=unit["fingerprint"], target="TQ1")
    res, _ = env.run(SqlImporter, [unit])
    assert res.units[0]["import_status"] == "skipped"
    assert not env.client.writes()


def test_query_adopt_with_a_moved_fingerprint_updates_content_and_owner():
    env = _q_env()
    env.client.queries["TQ1"] = {"display_name": "q", "owner_user_name": RUNNER}
    unit = _query_unit(fp="fp-new")
    env.seed("legacy_query", unit["natural_key"], fp="fp-old", target="TQ1")
    res, _ = env.run(SqlImporter, [unit])
    assert res.units[0]["import_status"] == "updated"
    masks = [c[2]["update_mask"] for c in env.client.writes("PATCH", "sql/queries/TQ1")]
    assert "owner_user_name" in masks and any(m != "owner_user_name" for m in masks)


def test_legacy_query_fingerprint_and_payload_ignore_the_owner():
    rec = GOLDEN_INVENTORY["sql"][1]
    (u,) = build_all({"sql": [copy.deepcopy(rec)]})["legacy_query"]
    assert u["source_owner"] == "alice@corp.com" and "owner_user_name" not in u["payload"]
    assert u["fingerprint"] == GOLDEN_1_1_1[f"legacy_query|{u['natural_key']}"]


# ═══════════════════════════════ §5 CUST-1 pipeline ACLs ═══════════════════════════════

PIPE = "pipe1"


def _pipe_acl(*grants):
    return {"asset_type": "dlt_pipeline", "natural_key": PIPE, "source_id": "SP1",
            "perm_object_type": "pipelines",
            "grants": [{"principal": p, "principal_type": t, "permission_level": lvl,
                        "inherited": False} for p, t, lvl in grants]}


_SRC_GRANTS = (("qa@corp.com", "user", "IS_OWNER"), ("analysts", "group", "CAN_RUN"),
               ("viewer@corp.com", "user", "CAN_VIEW"))


def _acl_env(current_owner=RUNNER, owner_field="service_principal_name", grants=_SRC_GRANTS):
    env = _bed()
    env.seed("dlt_pipeline", PIPE, target="TP1")
    env.client.perms["pipelines/TP1"] = [_owner_entry(current_owner, owner_field)]
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [_pipe_acl(*grants)])
    return env


def _pipe_put(env):
    (put,) = [c for c in env.client.writes("PUT", "permissions/pipelines/TP1")]
    return put[2]["access_control_list"]


def test_created_pipeline_keeps_the_runner_as_owner_and_gives_the_source_owner_can_manage():
    env = _acl_env()
    res, _ = env.run(AclImporter, [])
    acl = _pipe_put(env)
    assert acl[0] == {"service_principal_name": RUNNER, "permission_level": "IS_OWNER"}
    assert {"user_name": "qa@corp.com", "permission_level": "CAN_MANAGE"} in acl
    assert {"group_name": "analysts", "permission_level": "CAN_RUN"} in acl
    assert {"user_name": "viewer@corp.com", "permission_level": "CAN_VIEW"} in acl
    assert len(acl) == 4
    row = res.units[0]
    assert row["import_status"] == "created"
    assert f"owner kept as `{RUNNER}`" in row["note"] and "source owner `qa@corp.com` given " \
        "CAN_MANAGE" in row["note"]


def test_current_owner_is_the_source_owner_so_the_body_is_the_source_acl():
    env = _acl_env(current_owner="qa@corp.com", owner_field="user_name")
    res, _ = env.run(AclImporter, [])
    acl = _pipe_put(env)
    assert sorted(acl, key=json.dumps) == sorted([
        {"user_name": "qa@corp.com", "permission_level": "IS_OWNER"},
        {"group_name": "analysts", "permission_level": "CAN_RUN"},
        {"user_name": "viewer@corp.com", "permission_level": "CAN_VIEW"}], key=json.dumps)
    assert "owner kept" not in res.units[0]["note"]


def test_source_owner_already_can_manage_is_not_duplicated():
    env = _acl_env(grants=_SRC_GRANTS + (("qa@corp.com", "user", "CAN_MANAGE"),))
    env.run(AclImporter, [])
    acl = _pipe_put(env)
    assert acl.count({"user_name": "qa@corp.com", "permission_level": "CAN_MANAGE"}) == 1


def test_pipeline_owner_unreadable_is_failed_with_no_put():
    env = _acl_env()
    env.client.perm_get_error = HTTPStatusError(500, "INTERNAL_ERROR")
    res, _ = env.run(AclImporter, [])
    row = res.units[0]
    assert row["import_status"] == "failed" and "never guesses" in row["note"]
    assert not env.client.writes("PUT", "permissions/pipelines/")


def test_pipeline_with_no_owner_on_target_is_failed_with_no_put():
    env = _acl_env()
    env.client.perms["pipelines/TP1"] = []
    res, _ = env.run(AclImporter, [])
    assert res.units[0]["import_status"] == "failed"
    assert not env.client.writes("PUT", "permissions/pipelines/")


def test_other_object_types_keep_the_1_1_1_body():
    env = _bed()
    env.seed("job", "j", target="TJ1")
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [{
        "asset_type": "job", "natural_key": "j", "source_id": "J1", "perm_object_type": "jobs",
        "grants": [{"principal": "qa@corp.com", "principal_type": "user",
                    "permission_level": "IS_OWNER", "inherited": False},
                   {"principal": "analysts", "principal_type": "group",
                    "permission_level": "CAN_MANAGE_RUN", "inherited": False}]}])
    env.run(AclImporter, [])
    (put,) = env.client.writes("PUT", "permissions/jobs/TJ1")
    assert put[2] == {"access_control_list": [
        {"user_name": "qa@corp.com", "permission_level": "IS_OWNER"},
        {"group_name": "analysts", "permission_level": "CAN_MANAGE_RUN"}]}
    # no owner probe for non-pipeline types (the only GET is the parity re-read, after the PUT)
    i_put = env.client.calls.index(put)
    assert not [c for c in env.client.calls[:i_put] if c[0] == "GET" and "permissions/" in c[1]]


def test_pipeline_parity_is_match_with_a_known_gap():
    env = _acl_env()
    env.run(AclImporter, [])
    (obj,) = env.context["acl_parity"]["objects"]
    assert obj["verdict"] == "match"
    assert obj["known_gap"] == "owner kept on target (needs a metastore admin)"
    assert obj["detail"] == obj["known_gap"]


def _query_parity_env(target_acl, owner="qa@corp.com"):
    """A query ACL already applied (state row, same fingerprint → SKIP) whose target list is
    `target_acl`; the source grants are analysts CAN_RUN only (a creator-owner is not listed)."""
    from src.transform.transforms import fingerprint
    env = _bed()
    entry = {"asset_type": "legacy_query", "natural_key": "/Shared/q1", "source_id": "SQ1",
             "perm_object_type": "queries",
             "grants": [{"principal": "analysts", "principal_type": "group",
                         "permission_level": "CAN_RUN", "inherited": False}]}
    fp = fingerprint(AclImporter._normalise_grants(None, entry["grants"], remap=False))
    env.seed("acl", "queries:/Shared/q1", fp=fp, target="TQ1")
    env.new_run("r2")
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [entry])
    env.client.queries["TQ1"] = {"display_name": "q1", "owner_user_name": owner}
    env.client.perms["queries/TQ1"] = target_acl
    return env


def _grant_entry(field, principal, level):
    return {field: principal, "all_permissions": [{"permission_level": level, "inherited": False}]}


def test_query_owner_can_manage_added_by_an_owner_change_is_not_an_extra():
    """QA16.3-1 (live 2026-10-15): an owner change adds an explicit CAN_MANAGE for the new owner;
    the source creator-owner has none → still `match`, and nothing extra is written to the report."""
    env = _query_parity_env([_grant_entry("group_name", "analysts", "CAN_RUN"),
                             _grant_entry("user_name", "qa@corp.com", "CAN_MANAGE")])
    env.run(AclImporter, [])
    (obj,) = env.context["acl_parity"]["objects"]
    assert obj["verdict"] == "match"
    assert "known_gap" not in obj and "detail" not in obj
    assert obj["extra_on_target"] == []


def test_query_extra_can_manage_for_a_non_owner_is_still_reported():
    env = _query_parity_env([_grant_entry("group_name", "analysts", "CAN_RUN"),
                             _grant_entry("user_name", "qa@corp.com", "CAN_MANAGE"),
                             _grant_entry("user_name", "intruder@corp.com", "CAN_MANAGE")])
    env.run(AclImporter, [])
    (obj,) = env.context["acl_parity"]["objects"]
    assert obj["verdict"] == "extra_on_target"
    assert obj["extra_on_target"] == [["intruder@corp.com", "CAN_MANAGE"]]


def test_1x_failed_pipeline_acl_row_is_healed_by_failed_only():
    env = _acl_env()
    entry = _pipe_acl(*_SRC_GRANTS)
    from src.transform.transforms import fingerprint
    fp = fingerprint(AclImporter._normalise_grants(None, entry["grants"], remap=False))
    env.seed("acl", f"pipelines:{PIPE}", action="failed", fp=fp)
    env.new_run("r2", retry_mode="failed_only")
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [entry])
    res, _ = env.run(AclImporter, [], retry_keys=env.state.retry_keys("failed_only"))
    assert res.units[0]["import_status"] == "created"
    assert _pipe_put(env)[0]["permission_level"] == "IS_OWNER"


# ═══════════════════════════════ §5 CUST-1 pipeline run-as ═══════════════════════════════

def _pipe_unit(*, run_as=None, source_run_as=None, fp="fpp", libraries=True, name="bronze"):
    payload = {"name": name, "catalog": "cat", "schema": "s"}
    if libraries:
        payload["libraries"] = [{"notebook": {"path": "/Shared/nb"}}]
    if run_as is not None:
        payload["run_as"] = run_as
    u = _unit("dlt_pipeline", name, payload, source_id="SP1", fp=fp)
    if source_run_as is not None:
        u["source_run_as"] = source_run_as
    return u


def _dlt_env():
    env = _bed()
    env.client.paths["/Shared/nb"] = "n1"
    return env


def _pipe_bodies(env, verb="POST"):
    return [c[2] for c in env.client.calls if c[0] == verb and
            re.match(r"api/2.0/pipelines(/[^/]+)?$", c[1])]


def test_sp_owned_source_pipeline_is_created_with_the_remapped_run_as():
    env = _dlt_env()
    env.run(DltImporter, [_pipe_unit(source_run_as=SP_SRC)],
            identity_map={"sp_mapping": {SP_SRC: "new-sp-0000-0000-0000-000000000000"}})
    (body,) = _pipe_bodies(env)
    assert body["run_as"] == {"service_principal_name": "new-sp-0000-0000-0000-000000000000"}


def test_user_owned_source_pipeline_has_no_run_as():
    env = _dlt_env()
    env.run(DltImporter, [_pipe_unit(source_run_as="qa@corp.com")])
    (body,) = _pipe_bodies(env)
    assert "run_as" not in body


def test_explicit_sp_run_as_is_kept_as_today():
    env = _dlt_env()
    env.run(DltImporter, [_pipe_unit(run_as={"service_principal_name": SP_SRC},
                                     source_run_as=SP_SRC)])
    (body,) = _pipe_bodies(env)
    assert body["run_as"] == {"service_principal_name": SP_SRC}


def test_explicit_other_user_run_as_is_dropped_with_a_note():
    env = _dlt_env()
    res, _ = env.run(DltImporter, [_pipe_unit(run_as={"user_name": "qa@corp.com"},
                                              source_run_as="qa@corp.com")])
    (body,) = _pipe_bodies(env)
    assert "run_as" not in body
    assert "source run_as user `qa@corp.com` was dropped" in res.units[0]["note"]


def test_refused_run_as_is_retried_without_it_as_a_warning():
    env = _dlt_env()
    env.client.refuse_run_as.add(SP_SRC)
    res, _ = env.run(DltImporter, [_pipe_unit(source_run_as=SP_SRC)])
    first, second = _pipe_bodies(env)
    assert "run_as" in first and "run_as" not in second
    row = res.units[0]
    assert row["import_status"] == "created_with_warning"
    assert f"source ran as SP `{SP_SRC}` — grant the runner the Service Principal User role" \
        in row["note"]


def test_other_permission_denied_with_run_as_is_not_retried_and_fails():
    """A 403 that is NOT the run_as bind refusal (live: the run-as SP lacks USE CATALOG) must not
    be papered over by dropping run_as — the pipeline would silently run as the migration identity."""
    env = _dlt_env()
    env.client.no_catalog_run_as.add(SP_SRC)
    res, _ = env.run(DltImporter, [_pipe_unit(source_run_as=SP_SRC)])
    assert len(_pipe_bodies(env)) == 1                       # one attempt, no retry without run_as
    row = res.units[0]
    assert row["import_status"] == "failed"
    assert "USE CATALOG" in row["error_raw"]


def test_1x_target_pipeline_running_as_the_runner_is_rechecked_and_updated_with_run_as():
    env = _dlt_env()
    env.client.pipelines["TP1"] = {"name": "bronze", "run_as_user_name": RUNNER}
    unit = _pipe_unit(source_run_as=SP_SRC)
    env.seed("dlt_pipeline", "bronze", fp=unit["fingerprint"], target="TP1")
    res, _ = env.run(DltImporter, [unit])
    (put,) = _pipe_bodies(env, "PUT")
    assert put["run_as"] == {"service_principal_name": SP_SRC} and put["id"] == "TP1"
    assert res.units[0]["import_status"] in ("updated", "created_with_warning")
    # the next run sees the SP run-as on target → plain skip, zero writes
    env.new_run("r2")
    env.client.calls.clear()
    res2, _ = env.run(DltImporter, [unit])
    assert res2.units[0]["import_status"] == "skipped" and not env.client.writes()


def test_user_owned_pipeline_is_never_rechecked():
    env = _dlt_env()
    env.client.pipelines["TP1"] = {"name": "bronze", "run_as_user_name": RUNNER}
    unit = _pipe_unit(source_run_as="qa@corp.com")
    env.seed("dlt_pipeline", "bronze", fp=unit["fingerprint"], target="TP1")
    res, _ = env.run(DltImporter, [unit])
    assert res.units[0]["import_status"] == "skipped" and not env.client.writes()


def test_adopted_pipeline_with_moved_fingerprint_is_updated():
    env = _dlt_env()
    env.client.pipelines["TP1"] = {"name": "bronze", "run_as_user_name": RUNNER}
    unit = _pipe_unit(source_run_as="qa@corp.com", fp="fp-new")
    env.seed("dlt_pipeline", "bronze", fp="fp-old", target="TP1")
    res, _ = env.run(DltImporter, [unit])
    assert _pipe_bodies(env, "PUT")
    assert res.units[0]["import_status"] in ("updated", "created_with_warning")


def test_dlt_pipeline_fingerprint_ignores_the_run_as():
    rec = copy.deepcopy(GOLDEN_INVENTORY["dlt_pipeline"][0])
    (u, _synced) = build_all({"dlt_pipeline": [rec, GOLDEN_INVENTORY["dlt_pipeline"][1]]})[
        "dlt_pipeline"]
    assert u["source_run_as"] == SP_SRC and "run_as" not in u["payload"]
    assert u["fingerprint"] == GOLDEN_1_1_1["dlt_pipeline|bronze"]


# ═══════════════════════════════ §6 CUST-2 synced-table pipelines ═══════════════════════════════

def _pipe_record(name, **spec):
    return {"pipeline_id": "p", "name": name, "deployed_by_dab": False,
            "spec": {"name": name, **spec}, "_raw": {}}


def test_export_synced_table_pipeline_is_manual_naming_the_table():
    (u,) = build_all({"dlt_pipeline": [_pipe_record("Synced table: cat.s.t", libraries=[])]})[
        "dlt_pipeline"]
    assert u["migration_mode"] == "manual" and u["import_action"] == "manual"
    assert "belongs to the synced table `cat.s.t`" in u["note"]


def test_synced_table_note_drops_the_platforms_random_name_suffix():
    """The live name carries a suffix after the table (2026-10-16); the note names the table only."""
    spec = {"name": "Synced table: wsmig_lb_cat.public.zones_synced Vl02hH", "catalog": "wsmig_lb_cat",
            "managed_definition": {}, "pipeline_type": "MANAGED"}
    note = pipeline_not_creatable(spec)
    assert "belongs to the synced table `wsmig_lb_cat.public.zones_synced` (" in note
    assert "Vl02hH" not in note


def test_export_empty_libraries_pipeline_gets_the_generic_note():
    (u,) = build_all({"dlt_pipeline": [_pipe_record("odd")]})["dlt_pipeline"]
    assert u["import_action"] == "manual" and "no source code (`libraries` is empty)" in u["note"]


def test_export_ingestion_pipeline_is_auto():
    (u,) = build_all({"dlt_pipeline": [_pipe_record("ingest", ingestion_definition={"x": 1})]})[
        "dlt_pipeline"]
    assert u["migration_mode"] == "auto"


def test_export_normal_pipeline_is_auto_with_the_1_1_1_fingerprint():
    units = build_all({"dlt_pipeline": copy.deepcopy(GOLDEN_INVENTORY["dlt_pipeline"])})[
        "dlt_pipeline"]
    by = {u["natural_key"]: u for u in units}
    assert by["bronze"]["migration_mode"] == "auto"
    assert by["Synced table: cat.s.t"]["migration_mode"] == "manual"
    for nk, u in by.items():
        assert u["fingerprint"] == GOLDEN_1_1_1[f"dlt_pipeline|{nk}"]


def test_import_1x_create_unit_with_no_libraries_is_manual_with_zero_posts():
    env = _dlt_env()
    unit = _pipe_unit(name="Synced table: cat.s.t", libraries=False)
    res, _ = env.run(DltImporter, [unit])
    row = res.units[0]
    assert row["import_status"] == "manual" and "cat.s.t" in row["note"]
    assert not _pipe_bodies(env)


def test_pipeline_not_creatable_helper():
    assert pipeline_not_creatable({"name": "x", "libraries": [{"notebook": {}}]}) == ""
    assert pipeline_not_creatable({}) == ""
    assert pipeline_not_creatable({"name": "x", "libraries": [{}]}) != ""


def test_lakeflow_connect_gateway_is_manual_with_a_uc_connection_note():
    """A gateway has no `libraries` by design; the note must name the UC connection, not claim
    "no source code" (review 2026-10-14)."""
    spec = {"name": "sf_gateway", "gateway_definition": {
        "connection_name": "sqlserver_conn", "gateway_storage_catalog": "c",
        "gateway_storage_schema": "s"}}
    note = pipeline_not_creatable(spec)
    assert "Lakeflow Connect ingestion gateway" in note and "`sqlserver_conn`" in note
    assert "no source code" not in note
    # an ingestion pipeline (ingestion_definition) is still attempted, as in 1.1.1
    assert pipeline_not_creatable({"name": "ing", "ingestion_definition": {
        "connection_name": "sf_conn", "objects": []}}) == ""


# ═══════════════════════════════ §1 run folder ═══════════════════════════════

@pytest.mark.parametrize("nb", ["01_Inventory.py", "02_Export.py", "04_Import.py"])
def test_each_notebook_prints_run_id_and_folder_at_the_end(nb):
    src = open(os.path.join(REPO, "notebooks", nb), encoding="utf-8").read()
    i_run = src.rindex('_say(f"Run id : {cfg.run_id}")')
    i_folder = src.rindex('_say(f"Folder : {cfg.output_path')
    assert i_folder > i_run
    tail = src[i_folder:]
    # nothing but the red-task raises may follow the folder line
    assert "_say(" not in tail.split("\n", 1)[1]


def _run_dir(cfg, rid, *, manifest=False, checkpoint=None):
    d = bs.run_dir(cfg, rid)
    os.makedirs(os.path.join(d, "misc"), exist_ok=True)
    if manifest:
        with open(os.path.join(d, BP.MANIFEST_JSON), "w") as f:
            json.dump({"files": []}, f)
    if checkpoint is not None:
        with open(os.path.join(d, BP.CHECKPOINT_JSON), "w") as f:
            json.dump(checkpoint, f)
    return d


def _cfg(tmp):
    from src.config.config_manager import Config
    return Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "fresh",
                             "target_staging_location": tmp})


def test_standalone_import_uses_latest_export_even_with_an_older_unfinished_import():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        _run_dir(cfg, "20261012_000000", manifest=True, checkpoint={"import:compute": ["a"]})
        _run_dir(cfg, "20261013_000000", manifest=True)
        bs.write_latest_export_pointer(cfg, "20261013_000000", {"tool_version": "1"}, {})
        assert resolve_import_run_id(cfg, "") == ("20261013_000000", "LATEST_EXPORT.json")


def test_unfinished_import_in_the_latest_export_folder_is_continued_from_its_checkpoint():
    env = Env()
    env.aw.write_json(BP.MANIFEST_JSON, {"files": []})
    bs.write_latest_export_pointer(env.cfg, env.cfg.run_id, {"tool_version": "1"}, {})
    assert resolve_import_run_id(env.cfg, "")[0] == env.cfg.run_id
    unit = _unit("directory", "/Shared/x", {"path": "/Shared/x"})
    env.aw.mark_done_bulk("import:workspace", ["/Shared/x"],
                          {"/Shared/x": {"import_status": "created", "target_id": "o9",
                                         "asset_type": "directory"}})
    from src.importers.workspace_importer import WorkspaceImporter
    res, _ = env.run(WorkspaceImporter, [unit])
    assert res.units[0]["import_status"] == "created" and res.units[0]["target_id"] == "o9"
    assert res.units[0]["note"] == "already done in this run (resumed from checkpoint)"
    assert not env.client.writes()


def test_inventory_and_export_ignore_an_older_abandoned_folder():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        _run_dir(cfg, "20261001_000000", checkpoint={})              # abandoned days ago
        _run_dir(cfg, "20261013_000000", manifest=True, checkpoint={})   # newest, complete
        assert bs.find_latest_incomplete_run(cfg) is None
        assert bs.resolve_inventory_run_id(cfg, "", False) == ("fresh", "fresh")
        bs.write_latest_pointer(cfg, "20261013_000000", {"job": 1})
        assert bs.resolve_export_run_id(cfg, "", False) == ("20261013_000000", "pointer")


def test_newest_unfinished_folder_is_still_continued():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        _run_dir(cfg, "20261012_000000", manifest=True, checkpoint={})
        _run_dir(cfg, "20261013_000000", checkpoint={})
        assert bs.resolve_inventory_run_id(cfg, "", False) == ("20261013_000000", "resume")
        assert bs.resolve_export_run_id(cfg, "", False) == ("20261013_000000", "resume")


# ═══════════════════════════════ backward compatibility (1.1.1 state + bundle) ═══════════════════

def test_1_1_1_state_and_bundle_produce_only_the_declared_changes():
    """A 1.1.1-shaped bundle (no source_owner / source_run_as, a synced pipeline marked `create`)
    over 1.1.1-written state: only the declared changes happen — the synced pipeline turns manual,
    everything else SKIPs with zero writes."""
    env = _q_env()
    env.client.paths["/Shared/nb"] = "n1"
    env.client.queries["TQ1"] = {"display_name": "q", "owner_user_name": RUNNER}
    env.client.pipelines["TP1"] = {"name": "bronze", "run_as_user_name": RUNNER}
    query, pipe = _query_unit(owner=None), _pipe_unit()
    synced = _pipe_unit(name="Synced table: cat.s.t", libraries=False, fp="fps")
    env.seed("legacy_query", query["natural_key"], fp=query["fingerprint"], target="TQ1")
    env.seed("dlt_pipeline", "bronze", fp=pipe["fingerprint"], target="TP1")
    env.seed("dlt_pipeline", synced["natural_key"], action="failed")
    rq, _ = env.run(SqlImporter, [query])
    rp, _ = env.run(DltImporter, [pipe, synced])
    assert rq.units[0]["import_status"] == "skipped"
    by = _rows(rp)
    assert by["bronze"]["import_status"] == "skipped"
    assert by[synced["natural_key"]]["import_status"] == "manual"
    assert not env.client.writes()
