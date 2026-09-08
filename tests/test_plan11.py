"""PLAN 11 regression tests — one (or more) per finding, so each fix is locked against re-drift.

Findings covered here (others are locked in the phase tests they naturally belong to):
  BUG-1     — the create-race adopt path HEALS a stale object instead of stranding it
  Finding-9 — full-path natural keys (no same-name collapse) + legacy_query UPDATE via PATCH
  Finding-10— exact-or-fail-loud reference remap: jobs sql_task/pipeline_task/run_job_task; run_as
  Finding-8 — orphaned-owner divert to the backup root for folder-placed assets
  Finding-7 — SP OAuth-secret manual unit is KIND-scoped (locked in test_identity_importer.py too)
  Finding-2 — account/Entra group membership note names the delta + the account-managed clause
  Finding-4 — cumulative Outstanding sheet driven from the state table
  Finding-3 — deleted_in_source shown inline on each asset-type tab
  Finding-12— configurable DAB bundle roots (glob OR directory prefix), default byte-identical
"""
from __future__ import annotations

import json
import os
import tempfile

from src.config.config_manager import Config
from src.exporters import bundle_paths as BP
from src.exporters.artifact_writer import ArtifactWriter
from src.importers.base_importer import BaseImporter
from src.importers.identity_importer import IdentityImporter
from src.importers.jobs_importer import JobsImporter
from src.importers.sql_importer import SqlImporter
from src.state.state_store import StateStore
from src.utils.helpers import dab_path_info, folder_natural_key, is_bundle_root_path
from tests.test_importers_phase2_5 import RecordingClient, _make, _unit
from tests.test_state_store import FakeBackend


# ═══════════════════════════ Finding-9 — no same-name collapse ══════════════════════════════

def test_two_same_named_queries_in_different_folders_both_create():
    """Finding-9 Bug B: two DISTINCT queries both named "New query" in DIFFERENT folders must
    migrate as TWO target objects — never collapse onto one (N-1 silently lost)."""
    client = RecordingClient()
    imp, _st = _make(SqlImporter, [
        _unit("legacy_query", "/Users/a@x.com/New query",
              {"display_name": "New query", "query_text": "select 1"}),
        _unit("legacy_query", "/Users/b@x.com/New query",
              {"display_name": "New query", "query_text": "select 2"}),
    ], client)
    res = imp.run()
    creates = client.bodies_to("sql/queries")
    assert res.created == 2, "both same-named queries must create as distinct objects"
    assert len(creates) == 2
    # distinct state keys → distinct target ids
    tids = set((imp.context.get("legacy_query_target_ids") or {}).values())
    assert len(tids) == 2


def test_a_changed_query_updates_via_PATCH_on_the_right_target_id():
    """Finding-9 Bug A: legacy_query UPDATE is a PATCH on the modern Queries API (POST-with-id 404s
    ENDPOINT_NOT_FOUND), hitting the target id resolved from STATE (id-anchor), not a name map."""
    client = RecordingClient(paginated={
        "api/2.0/sql/queries": [{"display_name": "New query", "id": "Q1"}],
        "api/2.0/alerts": []})
    key = "/Users/a@x.com/New query"
    imp, st = _make(SqlImporter, [
        _unit("legacy_query", key, {"display_name": "New query", "query_text": "select 99"},
              fingerprint="sha256:new")], client)
    st.record("legacy_query", key, action="created", fingerprint="sha256:old",
              target_object_id="Q1")
    res = imp.run()
    patches = [c for c in client.calls if c[0] == "PATCH" and "sql/queries/Q1" in c[1]]
    assert patches, "a changed query must PATCH /api/2.0/sql/queries/{id}"
    assert "update_mask" in patches[0][2], "the modern Queries PATCH needs an update_mask"
    assert client.posts_to("sql/queries") == [], "an UPDATE must not POST a duplicate"
    assert res.updated == 1


def test_folder_natural_key_helper():
    assert folder_natural_key("/Workspace/Users/a", "New query") == "/Users/a/New query"
    assert folder_natural_key("", "solo") == "solo"
    assert folder_natural_key(None, "solo") == "solo"


def test_lakeview_and_genie_export_units_use_full_path_natural_key():
    """Finding-9 (Run-2 regression): the EXPORT unit builders for lakeview dashboards and Genie
    spaces must stamp the FULL-PATH natural key, not the bare display_name/title. The collectors
    and the importer's `existing_keys` already key on the full path; when the export unit kept the
    bare name, two same-named dashboards in different folders collapsed onto ONE target on import
    (the 2nd silently UPDATED the 1st = data loss). Caught live in PLAN 11 Run 2."""
    from src.exporters.asset_export import _lakeview_units, _genie_units
    dash = _lakeview_units([
        {"display_name": "dupe", "dashboard_id": "d1",
         "parent_path": "/Users/a@x.com/folderA", "serialized_dashboard": "{}"},
        {"display_name": "dupe", "dashboard_id": "d2",
         "parent_path": "/Users/a@x.com/folderB", "serialized_dashboard": "{}"},
    ])
    keys = {u["natural_key"] for u in dash}
    assert keys == {"/Users/a@x.com/folderA/dupe", "/Users/a@x.com/folderB/dupe"}, \
        "same-named dashboards in different folders must get DISTINCT full-path keys"
    genie = _genie_units([
        {"title": "space", "space_id": "g1", "parent_path": "/Users/a@x.com/f1",
         "serialized_space": "{}"},
        {"title": "space", "space_id": "g2", "parent_path": "/Users/a@x.com/f2",
         "serialized_space": "{}"},
    ])
    gkeys = {u["natural_key"] for u in genie}
    assert gkeys == {"/Users/a@x.com/f1/space", "/Users/a@x.com/f2/space"}


def test_alert_v2_update_sends_update_mask_query_param():
    """Finding-9/BUG-1 (Run-2 regression): the Alerts V2 PATCH REQUIRES an `update_mask` query arg.
    A body-only PATCH 400s "update_mask is required", so a changed alert (BUG-1 correctly routed it
    to UPDATE) still failed and stayed stale. The mask must name the settable fields present in the
    body — and nothing read-only (id/create_time/owner/…), which would also 400."""
    client = RecordingClient(paginated={"api/2.0/alerts": [
        {"display_name": "wsmig_test_alert_v2", "id": "A1"}]})
    key = "/Users/a@x.com/wsmig_test_alert_v2"
    imp, st = _make(SqlImporter, [
        _unit("alert_v2", key,
              {"display_name": "wsmig_test_alert_v2", "warehouse_id": "SRC-WH",
               "evaluation": {"threshold": {"value": {"double_value": 5}}},
               "schedule": {"quartz_cron_schedule": "0 0 9 * * ?"},
               "id": "A1", "create_time": "2026-01-01", "owner_user_name": "a@x.com"},
              fingerprint="sha256:new")], client)
    st.record("alert_v2", key, action="created", fingerprint="sha256:old", target_object_id="A1")
    # the warehouse ref must resolve, or _remap_warehouse hard-fails before the PATCH
    imp.units_by_type["sql_warehouse"] = [
        _unit("sql_warehouse", "wh", {"name": "wh"}, source_id="SRC-WH")]
    imp.context["sql_warehouse_target_ids"] = {"wh": "TGT-WH"}
    res = imp.run()
    patches = [c for c in client.calls if c[0] == "PATCH" and "alerts/A1" in c[1]]
    assert patches, "a changed alert_v2 must PATCH /api/2.0/alerts/{id}"
    params = patches[0][3] or {}
    mask = params.get("update_mask", "")
    assert mask, "the Alerts V2 PATCH MUST carry a non-empty update_mask query arg"
    masked = set(mask.split(","))
    assert "evaluation" in masked and "warehouse_id" in masked
    assert not (masked & {"id", "create_time", "owner_user_name"}), \
        "read-only fields must never appear in the update_mask (they 400 the PATCH)"
    assert res.updated == 1


# ═══════════════════════════ BUG-1 — create-race adopt heals ════════════════════════════════

class _RacyImporter(BaseImporter):
    """Toy importer: existence map MISSES the object, create raises RESOURCE_ALREADY_EXISTS."""
    component = "sql"
    asset_types = ("alert_v2",)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.updated_with = []

    def load(self):
        return self.units_for("alert_v2")

    def existing_keys(self):
        return {}                      # the map MISSES it — exactly the BUG-1 condition

    def create_one(self, unit):
        raise RuntimeError("RESOURCE_ALREADY_EXISTS: an alert with that name exists")

    def update_one(self, unit, target_id):
        self.updated_with.append((self.natural_key(unit), target_id))
        return {"target_id": target_id, "note": "updated in place"}


def test_create_race_adopt_heals_a_stale_object_instead_of_stranding_it():
    """BUG-1: the decision resolved to CREATE (existence map missed), the create raced
    RESOURCE_ALREADY_EXISTS, AND the stored fingerprint moved → the object must be UPDATED (healed
    against the state-stored target id), not recorded `adopted` with the edit silently dropped."""
    client = RecordingClient()
    key = "/Users/a@x.com/wsmig_test_alert_v2"
    imp, st = _make(_RacyImporter, [
        _unit("alert_v2", key, {"display_name": "wsmig_test_alert_v2"},
              fingerprint="sha256:NEW")], client)
    st.record("alert_v2", key, action="adopted", fingerprint="sha256:OLD",
              target_object_id="4308521176234470")
    res = imp.run()
    assert imp.updated_with == [(key, "4308521176234470")], \
        "the create-race adopt path must resolve the real target id and UPDATE it"
    assert res.updated == 1 and res.adopted == 0
    assert st.row("alert_v2", key)["last_action"] == "updated"
    assert st.row("alert_v2", key)["last_source_fingerprint"] == "sha256:NEW", \
        "the fingerprint may advance only AFTER the update lands"


class _FailingUpdateImporter(BaseImporter):
    """Toy importer whose object EXISTS (so the decision is UPDATE) but whose update always errors."""
    component = "sql"
    asset_types = ("alert_v2",)

    def load(self):
        return self.units_for("alert_v2")

    def existing_keys(self):
        return {self.natural_key(u): "T1" for u in self.load()}

    def create_one(self, unit):
        return {"target_id": "T1"}

    def update_one(self, unit, target_id):
        raise RuntimeError("400: INVALID_PARAMETER_VALUE update_mask is required")


def test_failed_update_does_not_advance_the_stored_fingerprint():
    """PLAN 11 Run-3 regression: a FAILED update must NOT advance last_source_fingerprint. The change
    never landed, so if the new fingerprint were stored, the next normal run would see 'unchanged'
    and SKIP/ADOPT — leaving the target permanently stale while state claims it is current. Instead
    the LAST SUCCESSFUL fingerprint must persist so the source still reads as moved and is retried."""
    client = RecordingClient()
    key = "/Users/a@x.com/wsmig_test_alert_v2"
    imp, st = _make(_FailingUpdateImporter, [
        _unit("alert_v2", key, {"display_name": "wsmig_test_alert_v2"},
              fingerprint="sha256:NEW")], client)
    st.record("alert_v2", key, action="created", fingerprint="sha256:OLD", target_object_id="T1")
    res = imp.run()
    assert res.failed == 1
    row = st.row("alert_v2", key)
    assert row["last_action"] == "failed"
    assert row["last_source_fingerprint"] == "sha256:OLD", \
        "a failed update must NOT advance the fingerprint (else the next run skips it as unchanged)"
    assert row["target_object_id"] == "T1", "a failed update must keep the target id"


def test_create_race_adopt_stays_adopt_when_fingerprint_unchanged():
    """The heal only fires on a MOVED fingerprint — an unchanged one is a plain adopt (no wasted
    update call)."""
    client = RecordingClient()
    key = "/Users/a@x.com/alert"
    imp, st = _make(_RacyImporter, [
        _unit("alert_v2", key, {"display_name": "alert"}, fingerprint="sha256:SAME")], client)
    st.record("alert_v2", key, action="created", fingerprint="sha256:SAME",
              target_object_id="T1")
    res = imp.run()
    assert imp.updated_with == [], "unchanged fingerprint → no update"
    assert res.adopted == 1


# ═══════════════════════════ Finding-10 — phase order: sql+dlt before jobs ══════════════════

def test_sql_and_dlt_run_before_jobs():
    """PLAN 11 Finding-10 follow-up: jobs DEPEND on sql/dlt (sql_task.warehouse_id,
    pipeline_task.pipeline_id) — never the reverse — so sql and dlt must be created BEFORE jobs, so
    those references resolve on the first pass instead of a retryable prerequisite."""
    from src.importers.phases import PHASE_ORDER
    assert PHASE_ORDER.index("sql") < PHASE_ORDER.index("jobs")
    assert PHASE_ORDER.index("dlt") < PHASE_ORDER.index("jobs")
    # dlt still needs sql (warehouse refs); identity first, acls last.
    assert PHASE_ORDER.index("sql") < PHASE_ORDER.index("dlt")
    assert PHASE_ORDER[0] == "identity" and PHASE_ORDER[-1] == "acls"


# ═══════════════════════════ Finding-10 — jobs task references ══════════════════════════════

def _job(tasks):
    return _unit("job", "etl-job", {"name": "etl-job", "tasks": tasks})


def test_job_sql_task_warehouse_AND_query_are_remapped_to_the_target():
    """Finding-10 + addendum: a sql_task remaps BOTH its `warehouse_id` AND its nested
    `query.query_id` (the query it runs is migrated too — leaving the source query id dangled the
    task, the exact gap the PLAN 11 Run 2 live test caught)."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [_job([
        {"task_key": "q", "sql_task": {"warehouse_id": "SRC-WH", "query": {"query_id": "SRC-Q"}}}])],
        client, context={"sql_warehouse_target_ids": {"wh": "TGT-WH"},
                         "legacy_query_target_ids": {"q1": "TGT-Q"}})
    imp.units_by_type["sql_warehouse"] = [_unit("sql_warehouse", "wh", {"name": "wh"},
                                                source_id="SRC-WH")]
    imp.units_by_type["legacy_query"] = [_unit("legacy_query", "q1", {"display_name": "q1"},
                                               source_id="SRC-Q")]
    imp.run()
    body = client.bodies_to("2.1/jobs/create") or client.bodies_to("jobs/create")
    task = body[0]["tasks"][0]
    assert task["sql_task"]["warehouse_id"] == "TGT-WH", "sql_task.warehouse_id must be remapped"
    assert task["sql_task"]["query"]["query_id"] == "TGT-Q", "sql_task.query.query_id must be remapped"


def test_job_sql_task_query_not_in_bundle_fails_loud():
    """Finding-10 addendum: a sql_task.query.query_id whose query is NOT in the bundle fails loud —
    never left dangling at the source id (same rule as warehouse/pipeline/job refs)."""
    client = RecordingClient()
    imp, st = _make(JobsImporter, [_job([
        {"task_key": "q", "sql_task": {"warehouse_id": "SRC-WH", "query": {"query_id": "GONE-Q"}}}])],
        client, context={"sql_warehouse_target_ids": {"wh": "TGT-WH"}})
    imp.units_by_type["sql_warehouse"] = [_unit("sql_warehouse", "wh", {"name": "wh"},
                                                source_id="SRC-WH")]
    res = imp.run()
    assert res.created == 0 and res.failed == 1
    assert "not available on source" in st.row("job", "etl-job")["last_error"]


def test_job_dbt_task_warehouse_is_remapped():
    """Finding-10 addendum: a dbt_task carries its OWN warehouse_id — remap it like sql_task's."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [_job([
        {"task_key": "d", "dbt_task": {"warehouse_id": "SRC-WH", "commands": ["dbt run"]}}])],
        client, context={"sql_warehouse_target_ids": {"wh": "TGT-WH"}})
    imp.units_by_type["sql_warehouse"] = [_unit("sql_warehouse", "wh", {"name": "wh"},
                                                source_id="SRC-WH")]
    imp.run()
    body = client.bodies_to("2.1/jobs/create") or client.bodies_to("jobs/create")
    assert body[0]["tasks"][0]["dbt_task"]["warehouse_id"] == "TGT-WH"


def test_job_sql_task_legacy_alert_ref_is_warned_not_failed():
    """Finding-10 addendum: a sql_task.alert points at a LEGACY SQL alert (a manual item, never
    recreated) — so it must NOT hard-fail; the job is created_with_warning and the ref is named."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [_job([
        {"task_key": "a", "sql_task": {"warehouse_id": "SRC-WH",
                                       "alert": {"alert_id": "LEG-ALERT"}}}])],
        client, context={"sql_warehouse_target_ids": {"wh": "TGT-WH"}})
    imp.units_by_type["sql_warehouse"] = [_unit("sql_warehouse", "wh", {"name": "wh"},
                                                source_id="SRC-WH")]
    res = imp.run()
    assert res.failed == 0 and (res.created + res.warned) == 1, "manual-only ref must not hard-fail"
    body = client.bodies_to("2.1/jobs/create") or client.bodies_to("jobs/create")
    assert body, "job must still be created"


def test_job_pipeline_and_run_job_tasks_are_remapped():
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [_job([
        {"task_key": "p", "pipeline_task": {"pipeline_id": "SRC-PIPE"}},
        {"task_key": "j", "run_job_task": {"job_id": "SRC-JOB"}}])], client,
        context={"dlt_pipeline_target_ids": {"pipe": "TGT-PIPE"},
                 "job_target_ids": {"other": "TGT-JOB"}})
    imp.units_by_type["dlt_pipeline"] = [_unit("dlt_pipeline", "pipe", {"name": "pipe"},
                                               source_id="SRC-PIPE")]
    imp.units_by_type["job"] = list(imp.units_by_type.get("job", [])) + [
        _unit("job", "other", {"name": "other"}, source_id="SRC-JOB")]
    imp.run()
    body = client.bodies_to("2.1/jobs/create") or client.bodies_to("jobs/create")
    tasks = {t["task_key"]: t for t in body[0]["tasks"]}
    assert tasks["p"]["pipeline_task"]["pipeline_id"] == "TGT-PIPE"
    assert tasks["j"]["run_job_task"]["job_id"] == "TGT-JOB"


def test_job_task_reference_not_in_bundle_fails_loud():
    """Finding-10: a task warehouse that is NOT in the bundle is a HARD failure, never left as the
    source id."""
    client = RecordingClient()
    imp, st = _make(JobsImporter, [_job([
        {"task_key": "q", "sql_task": {"warehouse_id": "GONE"}}])], client)
    res = imp.run()
    assert res.created == 0 and res.failed == 1
    row = st.row("job", "etl-job")
    assert row["failure_category"] == "dependency_unresolved"
    assert "not available on source" in row["last_error"]


def test_run_as_account_sp_not_in_map_is_left_as_is_not_failed():
    """Finding-10: run_as is the ONE correct exception — an account SP's appId is stable, so an
    unmapped run_as is LEFT AS-IS (a warning), never a hard fail."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _unit("job", "j", {"name": "j", "tasks": [],
                           "run_as": {"service_principal_name": "acct-app-id"}})], client)
    res = imp.run()
    body = client.bodies_to("2.1/jobs/create") or client.bodies_to("jobs/create")
    assert body[0]["run_as"]["service_principal_name"] == "acct-app-id", "run_as left as-is"
    # created (possibly created_with_warning for the unmapped-run_as note) but NEVER a hard failure
    assert res.failed == 0 and (res.created + res.warned) == 1


# ═══════════════════════════ Finding-8 — orphan-owner divert ════════════════════════════════

def _roster_file(usernames):
    return {BP.IDENTITY_CLASSIFICATION_JSON: json.dumps(
        {"identities": [{"identity_type": "user", "userName": u} for u in usernames]}
    ).encode("utf-8")}


def test_orphaned_owner_query_is_diverted_to_backup_as_created_with_warning():
    """Finding-8: a query owned by a user ABSENT from the roster (deleted in source) is PRESERVED
    under the backup root as created_with_warning — parity with notebooks, not a hard failure."""
    client = RecordingClient()   # get-status 404s → the home is absent on target
    imp, _st = _make(SqlImporter, [
        _unit("legacy_query", "/Users/gone@x.com/Drafts/New query",
              {"display_name": "New query", "parent_path": "/Users/gone@x.com/Drafts"})],
        client, staging_files=_roster_file(["present@x.com"]))
    res = imp.run()
    assert res.warned == 1 and res.failed == 0
    body = client.bodies_to("sql/queries")[0]["query"]
    assert body["parent_path"] == "/Users_Backup/gone@x.com/Drafts", \
        "the query must land under the backup root"
    row = next(r for r in res.units if r["asset_type"] == "legacy_query")
    assert "deleted in source" in row["note"] and "preserved" in row["note"]
    # the backup folder is provisioned (create APIs do not auto-mkdir a parent)
    assert any(c[0] == "POST" and "workspace/mkdirs" in c[1]
               and c[2].get("path") == "/Users_Backup/gone@x.com/Drafts" for c in client.calls)


def test_in_roster_owner_is_not_diverted_to_backup():
    """Finding-8: an owner still IN the roster (home just not present yet) must NOT be silently
    diverted — the source path is kept so it recovers into the real home on retry."""
    client = RecordingClient()
    imp, _st = _make(SqlImporter, [
        _unit("legacy_query", "/Users/present@x.com/Drafts/q",
              {"display_name": "q", "parent_path": "/Users/present@x.com/Drafts"})],
        client, staging_files=_roster_file(["present@x.com"]))
    imp.run()
    body = client.bodies_to("sql/queries")[0]["query"]
    assert body["parent_path"] == "/Users/present@x.com/Drafts", \
        "an in-roster owner must not be diverted to /Users_Backup"


# ═══════════════════════════ Finding-2 — account-group membership note ══════════════════════

def test_account_group_membership_change_names_the_delta_and_account_managed_clause():
    """Finding-2: a membership-only change on an ACCOUNT group is `updated` (fingerprint moved) and
    the note NAMES the member delta + the account-managed clause — never the false 'no source-side
    change detected'."""
    client = RecordingClient()
    imp, st = _make(IdentityImporter, [], client)
    st.record("group", "acct-grp", action="adopted", fingerprint="old", target_object_id="G1",
              source_detail=json.dumps({"entitlements": [], "roles": [],
                                        "members": ["Alice"]}, sort_keys=True))
    st.flush()
    unit = {"asset_type": "group", "natural_key": "acct-grp", "kind": "account",
            "payload": {"displayName": "acct-grp",
                        "members": [{"display": "Alice"}, {"display": "Bob"}]}}
    out = imp.update_one(unit, "G1")
    assert "membership changed in source" in out["note"]
    assert "Bob" in out["note"] and "account-managed" in out["note"]
    assert "no source-side change detected" not in out["note"]


# ═══════════════════════════ Finding-4 / Finding-3 — report sheets ══════════════════════════

def _render(rows, outstanding=None, deleted=None, run_id="RUN-2"):
    from src.reports.import_report import _render_xlsx
    import openpyxl
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": run_id,
                            "target_staging_location": "/tmp"})
    summary = {"run_id": run_id, "source_workspace_id": "111", "connectivity_mode": "direct",
               "dry_run": False, "run_status": "completed",
               "deleted_in_source": deleted or {}}
    path = os.path.join(tempfile.mkdtemp(), "s.xlsx")
    _render_xlsx(path, cfg, summary, rows, None, None, outstanding)
    return openpyxl.load_workbook(path)


def test_outstanding_sheet_is_driven_from_state_with_origin_column():
    """Finding-4: a cumulative Outstanding sheet, sourced from the state table (not this run's
    units), with an Origin column (new this run vs carried over) and the totals banner. Scoped to
    FAILURES ONLY — customer 2026-09-04 ("should only have failures")."""
    outstanding = [
        {"asset_type": "job", "natural_key": "j1", "last_action": "failed",
         "failure_category": "api_error", "last_error": "boom", "last_run_id": "RUN-2",
         "first_seen": "t0", "last_seen": "t2"},
        {"asset_type": "job", "natural_key": "j2", "last_action": "failed",
         "failure_category": "api_error", "last_error": "boom2", "last_run_id": "RUN-1",
         "first_seen": "t0", "last_seen": "t1"},
    ]
    wb = _render([], outstanding=outstanding)
    assert "Outstanding" in wb.sheetnames
    ws = wb["Outstanding"]
    text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)
    assert "2 outstanding" in text and "failures only" in text
    # nothing but failures in the banner/legend framing
    banner = str(ws["A1"].value)
    assert "created-with-warning" not in banner and "manual" not in banner
    assert "new this run" in text and "carried over" in text
    assert "j1" in text and "j2" in text


def test_outstanding_state_query_is_failures_only():
    """Finding-4 (customer 2026-09-04): the state-table Outstanding query returns ONLY `failed`.
    created_with_warning, manual, skipped_no_object, not_selected, skipped are NOT outstanding here
    (created_with_warning stays on its per-asset-type tab; the rest are by-design/elsewhere)."""
    import tempfile
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r",
                            "target_staging_location": tempfile.mkdtemp(), "dry_run": False,
                            "imports": {"state_catalog": "c", "state_schema": "s"}})
    st = StateStore(FakeBackend(), cfg)
    st.ensure_table(); st.load()
    st.record("job", "j1", action="failed", fingerprint="f")
    st.record("dlt_pipeline", "p1", action="created_with_warning", fingerprint="f")
    st.record("secret_value", "s1", action="manual", fingerprint="f")
    st.record("acl", "clusters:etl", action="skipped_no_object", fingerprint="f")
    st.record("cluster", "c1", action="skipped", fingerprint="f")
    st.flush(); st.load(force=True)
    keys = {(r["asset_type"], r["natural_key"]) for r in st.outstanding_rows()}
    assert keys == {("job", "j1")}, "only failed rows are outstanding (failures only)"


def test_deleted_in_source_shows_inline_on_the_asset_type_tab():
    """Finding-3: a `deleted_in_source` item appears on its asset-type tab (the Jobs tab), not only
    on the Summary sheet."""
    rows = [{"asset_type": "job", "natural_key": "live-job", "import_status": "created",
             "action_taken": "Created on target", "target_id": "T1", "source_id": "S1",
             "note": "", "failure_category": "", "error_raw": ""}]
    wb = _render(rows, deleted={"job": ["old-job"]})
    # the Jobs tab (card label "Jobs")
    jobs_sheet = next(wb[n] for n in wb.sheetnames if n.lower().startswith("job"))
    text = "\n".join(str(c.value) for row in jobs_sheet.iter_rows() for c in row if c.value)
    assert "old-job" in text, "the deleted-in-source job must appear on the Jobs tab"
    assert "Deleted in source" in text


# ═══════════════════════════ Finding-12 — configurable DAB roots ════════════════════════════

def test_dab_path_info_default_is_unchanged():
    assert dab_path_info("/Shared/.bundle/b/x") == {
        "deployed_by_dab": True, "dab_scope": "shared", "bundle_root": "/Shared/.bundle"}
    assert dab_path_info("/Users/u@x/.bundle/b/x")["dab_scope"] == "user"
    assert dab_path_info("/Workspace/Shared/.bundle/b/x")["dab_scope"] == "shared"
    assert dab_path_info("/Shared/regular/nb")["deployed_by_dab"] is False
    # the .bundle folder ITSELF (last segment) is not "content under a root"
    assert dab_path_info("/Shared/.bundle")["deployed_by_dab"] is False


def test_dab_path_info_directory_prefix_root():
    roots = ["/Users/dab@corp.com/prod"]
    assert dab_path_info("/Users/dab@corp.com/prod/dash", roots)["deployed_by_dab"] is True
    assert dab_path_info("/Users/dab@corp.com/prod/dash", roots)["dab_scope"] == "user"
    assert dab_path_info("/Users/dab@corp.com/other/dash", roots)["deployed_by_dab"] is False
    # with ONLY a directory root, .bundle no longer matches
    assert dab_path_info("/Shared/.bundle/b/x", roots)["deployed_by_dab"] is False


def test_dab_path_info_glob_root_and_mixed():
    assert dab_path_info("/Shared/myteam.bundle/b/x", ["*.bundle"])["deployed_by_dab"] is True
    # a mixed workspace hosting both conventions
    both = [".bundle", "/Users/dab@corp.com"]
    assert dab_path_info("/Shared/.bundle/b/x", both)["deployed_by_dab"] is True
    assert dab_path_info("/Users/dab@corp.com/anything", both)["deployed_by_dab"] is True


def test_is_bundle_root_path_for_state_file_discovery():
    roots = ["/Users/dab@corp.com/prod"]
    assert is_bundle_root_path("/Users/dab@corp.com/prod/state/resources.json", roots) is True
    assert is_bundle_root_path("/Shared/.bundle/b/state/resources.json") is True


def test_derive_import_action_content_honors_configured_bundle_root():
    """Finding-12 (Run 3 live gap): CONTENT (notebook/file/dir) under a configured non-`.bundle`
    root must be `dab_redeploy` (skipped on import), not `create` — else the tool duplicates what the
    team's bundle redeploys. `derive_import_action` must thread the configured roots, not only match
    the hard-coded `.bundle` fast path."""
    from src.exporters.asset_export import derive_import_action
    roots = [".bundle", "/Shared/DAB_Deployments"]
    for atype, key in (("notebook", "/Shared/DAB_Deployments/wsmig_teamb/files/teamb_nb"),
                       ("workspace_file", "/Shared/DAB_Deployments/wsmig_teamb/files/databricks.yml"),
                       ("directory", "/Shared/DAB_Deployments/wsmig_teamb/state")):
        u = {"asset_type": atype, "natural_key": key, "migration_mode": "content",
             "export_status": "success"}
        assert derive_import_action(u, roots) == "dab_redeploy", f"{key} must be dab_redeploy"
        # without the configured root it (correctly) has no reason to skip → create path
        assert derive_import_action(u) != "dab_redeploy"
    # a normal (non-bundle) notebook is still created
    normal = {"asset_type": "notebook", "natural_key": "/Shared/wsmig_test/py_nb",
              "migration_mode": "content", "export_status": "success"}
    assert derive_import_action(normal, roots) != "dab_redeploy"


def test_derive_import_action_content_honors_recorded_dab_flag():
    """Finding-12 belt-and-suspenders: the collector already stamps `deployed_by_dab` (roots-aware),
    so content carrying that flag is `dab_redeploy` even if roots aren't threaded to this call."""
    from src.exporters.asset_export import derive_import_action
    u = {"asset_type": "notebook", "natural_key": "/Shared/DAB_Deployments/wsmig_teamb/files/nb",
         "migration_mode": "content", "export_status": "success", "deployed_by_dab": True}
    assert derive_import_action(u) == "dab_redeploy"
