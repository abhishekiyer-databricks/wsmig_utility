"""Offline tests for PLAN 16.3.1 — hotfix 1.2.1.

Part A: an absent `/Users/<owner>` home is CREATED with `mkdirs` as the workspace-admin run-as
        (homes are no longer created on workspace load, ES-2364565); the platform's refusal becomes
        a precise, retryable prerequisite. Account SPs (identity `sp_mapping`) are no longer
        assumed to have a home (the latent D3 bug).
Part B: an identity that is only NESTED on the source is never directly assigned on the target
        (no permissionassignments PUT, no workspace-SCIM POST), is reached through its parent in
        PASS 3, and gets its entitlements; identities already on target are reconciled (adopt).
DB5:    every entitlement write is GET → PATCH missing → GET, and each identity row says what was
        copied / already present / failed.

The two "call sequence unchanged" goldens (tests 14 and 28) were captured by running the same
scenario against a read-only export of `main` @ 4724c59 (1.2.0) — see PLAN §9.
"""
from __future__ import annotations

import logging
import os
import tempfile

import pytest

import src.importers.identity_importer as ii
from src.importers.base_importer import HOME_PROVISIONED_NOTE
from src.importers.dashboards_importer import DashboardsImporter
from src.importers.genie_importer import GenieImporter
from src.importers.identity_importer import IdentityImporter
from src.importers.sql_importer import SqlImporter
from src.importers.workspace_importer import WorkspaceImporter
from src.state.state_store import UpsertAction
from tests.test_identity_importer import FakeScimClient, _importer
from tests.test_identity_importer import _unit as _iunit
from tests.test_importers_phase2_5 import (_DIR_PROTECTED, RecordingClient, _make,
                                           _write_roster)
from tests.test_importers_phase2_5 import _unit as _wunit

U = "/Users/u@x.com"
APP = "9e15fb97-bd21-4abc-9def-0123456789ab"
NEW_APP = "11112222-3333-4444-5555-666677778888"
_PERM_DENIED = ("403 Client Error: PERMISSION_DENIED: 49399e58 does not have View permissions on "
                "1234 (object id of /Users)")
_SCIM = "api/2.0/preview/scim/v2"
_ASSIGN = "api/2.0/preview/permissionassignments/principals/"


def _mkdirs(client):
    return [b["path"] for b in client.bodies_to("workspace/mkdirs")]


def _status_gets(client, path):
    return [c for c in client.calls if c[0] == "GET" and c[1] == "api/2.0/workspace/get-status"
            and (c[2] or {}).get("path") == path]


def _nb(path, ref="c/nb.py"):
    return _wunit("notebook", path, {"path": path, "language": "PYTHON"}, content_ref=ref)


# ═══════════════════════════════════ PART A ═══════════════════════════════════

def test_absent_user_home_is_created_with_mkdirs_then_content_imports():
    client = RecordingClient(mkdirs_creates=True)
    imp, _st = _make(WorkspaceImporter, [
        _wunit("directory", U, {"path": U}),
        _wunit("directory", f"{U}/a", {"path": f"{U}/a"}),
        _nb(f"{U}/a/nb"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}})
    res = imp.run()
    assert res.failed == 0 and res.created == 3
    assert _mkdirs(client)[0] == U, "the home root is created first"
    assert client.bodies_to("workspace/import")[0]["path"] == f"{U}/a/nb"


def test_home_root_unit_records_created_note_not_prerequisite():
    client = RecordingClient(mkdirs_creates=True)
    imp, st = _make(WorkspaceImporter, [_wunit("directory", U, {"path": U})], client)
    res = imp.run()
    row = res.units[0]
    assert row["import_status"] == "created" and row["failure_category"] == ""
    assert row["note"] == HOME_PROVISIONED_NOTE
    assert st.row("directory", U)["last_action"] == "created"
    assert imp.context["homes_provisioned"] == ["u@x.com"]


def test_home_created_once_per_owner_per_pass():
    """N content units under one absent home → exactly ONE root mkdirs and ONE read-back (plus the
    one cached presence probe) — never one per content unit (the 7-hour lesson)."""
    client = RecordingClient(mkdirs_creates=True)
    units = [_nb(f"{U}/a/nb{i}", ref=f"c/nb{i}.py") for i in range(6)]
    imp, _st = _make(WorkspaceImporter, units, client,
                     staging_files={f"c/nb{i}.py": b"x" for i in range(6)})
    res = imp.run()
    assert res.created == 6 and res.failed == 0
    assert _mkdirs(client).count(U) == 1
    assert len(_status_gets(client, U)) == 2, "one presence probe + one read-back"
    # …and a REFUSED home is attempted once too, not once per content unit (the provision cache).
    refused = RecordingClient(mkdirs_errors={U: _DIR_PROTECTED})
    imp2, _st2 = _make(WorkspaceImporter, [_nb(f"{U}/a/nb{i}", ref=f"c/nb{i}.py") for i in range(6)],
                       refused, staging_files={f"c/nb{i}.py": b"x" for i in range(6)})
    assert imp2.run().failed == 6
    assert _mkdirs(refused) == [U]


def test_directory_protected_is_prerequisite_saying_owner_not_assigned():
    client = RecordingClient(mkdirs_errors={U: _DIR_PROTECTED})
    imp, st = _make(WorkspaceImporter, [_wunit("directory", f"{U}/a", {"path": f"{U}/a"})], client)
    res = imp.run()
    row = res.units[0]
    assert row["import_status"] == "failed" and row["failure_category"] == "prerequisite_missing"
    assert f"home `{U}` could not be created: the owner is not assigned to the target workspace" \
        in row["note"]
    assert "DIRECTORY_PROTECTED: Folder Users is protected" in row["note"]
    assert "Assign `u@x.com` to the target, then re-run with retry_mode=failed_only" in row["note"]
    assert "first login" not in row["note"] and "logs in" not in row["note"]


def test_other_mkdirs_error_is_prerequisite_with_server_message():
    client = RecordingClient(mkdirs_errors={U: _PERM_DENIED})
    imp, _st = _make(WorkspaceImporter, [_wunit("directory", U, {"path": U})], client)
    row = imp.run().units[0]
    assert row["failure_category"] == "prerequisite_missing"
    assert "PERMISSION_DENIED" in row["note"] and "does not have View permissions" in row["note"]
    assert "Confirm the run-as identity is a workspace admin" in row["note"]


def test_mkdirs_200_but_readback_absent_is_prerequisite():
    client = RecordingClient()            # mkdirs "succeeds" but the home never appears
    imp, _st = _make(WorkspaceImporter, [_wunit("directory", U, {"path": U})], client)
    row = imp.run().units[0]
    assert row["failure_category"] == "prerequisite_missing"
    assert "mkdirs returned 200 but the home is not present" in row["note"]


def test_dry_run_never_calls_mkdirs_on_a_home_root():
    client = RecordingClient(mkdirs_creates=True)
    imp, _st = _make(WorkspaceImporter, [
        _wunit("directory", U, {"path": U}), _nb(f"{U}/nb")], client, dry_run=True,
        staging_files={"c/nb.py": b"x"})
    res = imp.run()
    assert client.posts_to("workspace/mkdirs") == [] and client.posts_to("workspace/import") == []
    for row in res.units:
        assert f"home `{U}` does not exist yet; a live run creates it." in row["note"]
    # defence in depth: even a direct call never sends mkdirs in a dry run
    assert imp._provision_home("u@x.com", U) is False
    assert client.posts_to("workspace/mkdirs") == []


def test_existence_check_never_creates_a_home():
    client = RecordingClient(mkdirs_creates=True)
    imp, _st = _make(WorkspaceImporter, [_wunit("directory", U, {"path": U}), _nb(f"{U}/nb")],
                     client)
    assert imp.existing_keys() == {}
    assert [c for c in client.calls if c[0] != "GET"] == [], "the existence check only reads"


def test_owner_deleted_in_source_goes_to_backup_without_mkdirs():
    client = RecordingClient(mkdirs_creates=True)
    imp, _st = _make(WorkspaceImporter, [_wunit("directory", U, {"path": U}), _nb(f"{U}/nb")],
                     client, staging_files={"c/nb.py": b"x"})
    _write_roster(imp.staging, users=["someone.else@x.com"])     # u@x.com deleted in source
    res = imp.run()
    assert res.warned == 2 and res.failed == 0
    assert U not in _mkdirs(client), "never mkdirs the deleted owner's /Users home (D2)"
    assert client.bodies_to("workspace/import")[0]["path"] == "/Users_Backup/u@x.com/nb"


def test_account_sp_home_with_identity_mapping_is_checked_and_created():
    """D3 + the latent bug: an account SP keeps its appId, so sp_mapping is `appId → appId`. 1.2.0
    read that as "recreated SP, home auto-provisioned" and never checked or created it."""
    client = RecordingClient(mkdirs_creates=True)
    imp, _st = _make(WorkspaceImporter, [
        _wunit("directory", f"/Users/{APP}", {"path": f"/Users/{APP}"}),
        _nb(f"/Users/{APP}/jobs_code/nb")], client, staging_files={"c/nb.py": b"x"},
        identity_map={"sp_mapping": {APP: APP}})
    res = imp.run()
    assert res.failed == 0 and res.created == 2
    assert _mkdirs(client).count(f"/Users/{APP}") == 1
    assert res.units[0]["note"] == HOME_PROVISIONED_NOTE
    assert "auto-provisioned" not in res.units[0]["note"]
    assert client.bodies_to("workspace/import")[0]["path"] == f"/Users/{APP}/jobs_code/nb"


def test_recreated_sp_remaps_to_new_app_id_and_creates_its_absent_home():
    client = RecordingClient(mkdirs_creates=True)
    imp, _st = _make(WorkspaceImporter, [
        _wunit("directory", f"/Users/{APP}", {"path": f"/Users/{APP}"}),
        _nb(f"/Users/{APP}/proj/nb")], client, staging_files={"c/nb.py": b"x"},
        identity_map={"sp_mapping": {APP: NEW_APP}})
    res = imp.run()
    assert res.failed == 0
    root = res.units[0]
    assert root["target_id"] == f"/Users/{NEW_APP}"
    assert HOME_PROVISIONED_NOTE in root["note"] and "remapped" in root["note"]
    assert _mkdirs(client).count(f"/Users/{NEW_APP}") == 1 and f"/Users/{APP}" not in _mkdirs(client)
    assert client.bodies_to("workspace/import")[0]["path"] == f"/Users/{NEW_APP}/proj/nb"


@pytest.mark.parametrize("kind", ["query", "dashboard", "genie"])
def test_query_dashboard_genie_parent_home_is_created(kind):
    """All three `remap_parent_path` callers create the absent parent home before their create."""
    from tests.test_plan16_3 import _bed, _unit as _bunit
    home = "/Users/alice@corp.com"
    env = _bed()
    if kind == "genie":
        cls, u = GenieImporter, _bunit("genie_space", f"{home}/G",
                                       {"title": "G", "serialized_space": "{}", "parent_path": home})
    elif kind == "dashboard":
        cls, u = DashboardsImporter, _bunit("lakeview_dashboard", f"{home}/D",
                                            {"display_name": "D", "serialized_dashboard": "{}",
                                             "parent_path": home})
    else:
        cls, u = SqlImporter, _bunit("legacy_query", f"{home}/Q",
                                     {"display_name": "Q", "query_text": "select 1",
                                      "parent_path": home})
    res, _imp = env.run(cls, [u])
    row = _rows_by_type(res)[u["asset_type"]]
    assert row["import_status"] in ("created", "created_with_warning"), row["note"]
    mk = [c[2]["path"] for c in env.client.calls
          if c[0] == "POST" and c[1] == "api/2.0/workspace/mkdirs"]
    assert mk == [home], "the parent home is created exactly once"
    assert home in env.client.paths


def _rows_by_type(res):
    return {r["asset_type"]: r for r in res.units}


def test_upgrade_failed_only_heals_1_2_0_home_prerequisite_rows():
    """§4/§8.3 upgrade path: 1.2.0 left every home row failed/prerequisite_missing; 1.2.1 with
    retry_mode=failed_only and the SAME run creates the home and imports the content."""
    client = RecordingClient(mkdirs_creates=True)
    units = [_wunit("directory", U, {"path": U}), _wunit("directory", f"{U}/a", {"path": f"{U}/a"}),
             _nb(f"{U}/a/nb")]
    imp, st = _make(WorkspaceImporter, units, client, staging_files={"c/nb.py": b"x"},
                    imports_extra={"retry_mode": "failed_only"})
    for u in units:      # the 1.2.0-shaped rows (failed rows keep no fingerprint)
        st.record(u["asset_type"], u["natural_key"], action="failed",
                  error="`/Users/u@x.com` is a USER HOME directory, which cannot be created — it "
                        "appears only once `u@x.com` is provisioned on this workspace.",
                  failure_category="prerequisite_missing")
    imp.retry_keys = {(u["asset_type"], u["natural_key"]) for u in units}
    res = imp.run()
    assert res.failed == 0 and res.created == 3
    assert [st.row(u["asset_type"], u["natural_key"])["last_action"] for u in units] == \
        ["created"] * 3
    assert res.units[0]["note"] == HOME_PROVISIONED_NOTE


# Captured from main @ 4724c59 (1.2.0) running this exact scenario (PLAN §9).
_GOLDEN_1_2_0_PRESENT_HOME = [
    ("GET", "api/2.0/workspace/get-status", {"path": U}),
    ("GET", "api/2.0/workspace/get-status", {"path": U}),
    ("GET", "api/2.0/workspace/get-status", {"path": f"{U}/a"}),
    ("GET", "api/2.0/workspace/get-status", {"path": f"{U}/a/nb"}),
    ("POST", "api/2.0/workspace/mkdirs", {"path": f"{U}/a"}),
    ("POST", "api/2.0/workspace/mkdirs", {"path": f"{U}/a"}),
    ("POST", "api/2.0/workspace/import", f"{U}/a/nb"),
]


def test_present_home_call_sequence_unchanged():
    client = RecordingClient(status_paths={U})
    imp, _st = _make(WorkspaceImporter, [
        _wunit("directory", U, {"path": U}), _wunit("directory", f"{U}/a", {"path": f"{U}/a"}),
        _nb(f"{U}/a/nb")], client, staging_files={"c/nb.py": b"print(1)"},
        identity_map={"sp_mapping": {}})
    imp.run()
    calls = [(c[0], c[1], c[2]["path"] if c[1] == "api/2.0/workspace/import" else c[2])
             for c in client.calls]
    assert calls == _GOLDEN_1_2_0_PRESENT_HOME


# ═══════════════════════════════════ PART B ═══════════════════════════════════

class NestedScim(FakeScimClient):
    """FakeScimClient + account-global nesting: assigning a parent (PUT) makes its children
    visible in target SCIM, after `lag_polls` further list calls of that resource (PB6)."""

    def __init__(self, reveal_on_put=None, lag_polls=0, **kw):
        super().__init__(**kw)
        self.reveal_on_put = reveal_on_put or {}       # principal id → [(resource, scim obj)]
        self.lag_polls = lag_polls
        self._pending: list = []
        self.scim_calls: list = []

    def put(self, path, body):
        out = super().put(path, body)
        self._pending.extend(self.reveal_on_put.get(path.rsplit("/", 1)[-1], []))
        return out

    def get_scim(self, resource, max_items=0, count=500):
        self.scim_calls.append(resource)
        if any(r == resource for r, _ in self._pending):
            if self.lag_polls > 0:
                self.lag_polls -= 1
            else:
                for r, obj in [p for p in self._pending if p[0] == resource]:
                    self.scim[r].append(obj)
                self._pending = [p for p in self._pending if p[0] != resource]
        return super().get_scim(resource, max_items, count)


@pytest.fixture
def fast(monkeypatch):
    """No real waiting: record sleeps; one poll unless a test widens the budget."""
    sleeps: list = []
    monkeypatch.setattr(ii, "_sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(ii, "NESTED_WAIT_SEC", 0)
    return sleeps


def _grp(name, perms, members=(), ents=(), kind="account"):
    return _iunit("group", name, {"displayName": name,
                                  "members": [{"value": f"src-{m}", "display": m} for m in members],
                                  "entitlements": [{"value": e} for e in ents]},
                  kind=kind, workspace_permissions=perms)


def _sp(app, perms, ents=()):
    return _iunit("service_principal", app, {"displayName": f"sp-{app}", "applicationId": app,
                                             "entitlements": [{"value": e} for e in ents]},
                  kind="account", workspace_permissions=perms)


def _user(name, perms, ents=()):
    return _iunit("user", name, {"userName": name, "displayName": name,
                                 "entitlements": [{"value": e} for e in ents]},
                  kind="account", workspace_permissions=perms)


def _nested_bed(lag_polls=0):
    """P1 (direct) contains C1 (group), S1 (SP), nu@x.com (user) — all nested only on source."""
    units = [_user("nu@x.com", [], ents=("workspace-access",)),
             _sp("s1", [], ents=("workspace-access",)),
             _grp("P1", ["USER"], members=("C1", "s1", "nu@x.com"), ents=("workspace-access",)),
             _grp("C1", [], ents=("databricks-sql-access",))]
    reveal = {"src-P1": [
        ("Groups", {"id": "src-C1", "displayName": "C1", "meta": {"resourceType": "Group"}}),
        ("ServicePrincipals", {"id": "src-s1", "applicationId": "s1", "displayName": "sp-s1"}),
        ("Users", {"id": "src-nu", "userName": "nu@x.com", "displayName": "nu@x.com"})]}
    return NestedScim(reveal_on_put=reveal, lag_polls=lag_polls), units


def _next_run(imp, st, client, **kw):
    """The same importer setup on a NEW run (fresh checkpoint, same state table)."""
    import copy
    from src.exporters.artifact_writer import ArtifactWriter
    cfg = copy.deepcopy(imp.config)
    cfg.run_id = "r2"
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    return IdentityImporter(client, cfg, aw, state=st, units_by_type=imp.units_by_type, **kw)


def _puts(client, pid):
    return client.puts_to(f"{_ASSIGN}{pid}")


def test_nested_only_account_group_create_sends_no_permissionassignments_put(fast):
    client, units = _nested_bed()
    imp, _st = _importer(client, units)
    imp.run()
    assert _puts(client, "src-C1") == []
    assert len(_puts(client, "src-P1")) == 1, "the direct parent IS assigned"


def test_nested_only_sp_and_user_create_send_no_scim_post(fast):
    client, units = _nested_bed()
    imp, _st = _importer(client, units)
    imp.run()
    assert client.posts_to("/ServicePrincipals") == [] and client.posts_to("/Users") == [], \
        "a workspace-SCIM POST would directly assign them (PB1)"
    assert _puts(client, "src-s1") == [] and _puts(client, "src-nu") == []


def test_nested_pass_resolves_via_one_list_per_kind_and_applies_entitlements(fast):
    client, units = _nested_bed()
    imp, st = _importer(client, units)
    res = imp.run()
    rows = {r["natural_key"]: r for r in res.units}
    for key, tid, ent in (("C1", "src-C1", "databricks-sql-access"),
                          ("s1", "src-s1", "workspace-access"),
                          ("nu@x.com", "src-nu", "workspace-access")):
        assert rows[key]["import_status"] == "adopted" and rows[key]["target_id"] == tid
        assert rows[key]["entitlements_copied"] == ent
        assert "nested on source via `P1`" in rows[key]["note"]
        assert rows[key]["assignment"] == "via group (P1)"
        assert st.row(rows[key]["asset_type"], key)["last_action"] == "adopted"
    # existence check = 1 list per kind; pass 3 resolved everything on its FIRST poll = 1 per kind
    assert sorted(client.scim_calls) == sorted(["Users", "ServicePrincipals", "Groups"] * 2)


def test_nested_pass_polls_until_visible_within_budget(fast, monkeypatch):
    monkeypatch.setattr(ii, "NESTED_WAIT_SEC", 30)
    client, units = _nested_bed(lag_polls=1)          # Groups visible only on the 2nd poll
    imp, _st = _importer(client, units)
    res = imp.run()
    assert {r["natural_key"]: r["import_status"] for r in res.units}["C1"] == "adopted"
    assert fast == [ii.NESTED_POLL_SEC], "exactly one wait between the two polls"


def _shadow_bed():
    """P3 is an ACCOUNT group, but the target has a workspace-local shadow of it (Plan 6 F6), so
    P3 can never be assigned and C3 (nested in P3 only) never becomes visible — DB1."""
    units = [_grp("P3", ["USER"], members=("C3",)), _grp("C3", [])]
    client = NestedScim(groups=[{"id": "shadow-1", "displayName": "P3",
                                 "meta": {"resourceType": "WorkspaceGroup"}}],
                        reveal_on_put={"src-P3": [("Groups", {"id": "src-C3", "displayName": "C3",
                                                               "meta": {"resourceType": "Group"}})]})
    return client, units


def test_nested_pass_parent_missing_is_prerequisite_naming_parents_and_their_outcome(fast):
    client, units = _shadow_bed()
    imp, st = _importer(client, units)
    res = imp.run()
    row = {r["natural_key"]: r for r in res.units}["C3"]
    assert row["import_status"] == "failed" and row["failure_category"] == "prerequisite_missing"
    assert "nested on source via `P3`; none of them is on the target" in row["note"]
    assert "`P3`: failed" in row["note"]
    assert "retry_mode=failed_only" in row["note"]
    assert _puts(client, "src-C3") == [], "DB1: never a fallback to a direct assignment"
    assert st.row("group", "C3")["failure_category"] == "prerequisite_missing"


def test_failed_only_retries_a_nested_prerequisite_and_heals_it(fast):
    client, units = _shadow_bed()
    imp, st = _importer(client, units)
    imp.run()
    st.flush()
    # The operator deletes the shadow; retry_mode=failed_only re-runs the two failed rows.
    client.scim["Groups"] = []
    imp2 = IdentityImporter(client, imp.config, imp.staging, state=st,
                            units_by_type=imp.units_by_type,
                            retry_keys={("group", "P3"), ("group", "C3")})
    res = imp2.run()
    rows = {r["natural_key"]: r for r in res.units}
    assert rows["P3"]["import_status"] == "created"
    assert rows["C3"]["import_status"] == "adopted" and rows["C3"]["target_id"] == "src-C3"
    assert _puts(client, "src-C3") == []


def test_nested_only_update_path_never_reassigns(fast):
    """The re-run regression: 1.2.0 re-PUT USER for a nested group on every re-run (update AND the
    skip/adopt hook), undoing any manual cleanup."""
    c1 = _grp("C1", [], ents=("databricks-sql-access",))
    client = FakeScimClient(groups=[{"id": "src-C1", "displayName": "C1",
                                     "meta": {"resourceType": "Group"}}])
    imp, st = _importer(client, [c1])
    st.record("group", "C1", action="adopted", fingerprint="sha256:OLD", target_object_id="src-C1")
    res = imp.run()                                        # fingerprint moved → UPDATE
    assert res.units[0]["import_status"] == "updated", res.units[0]["note"]
    assert client.puts_to(_ASSIGN) == []
    st.flush()
    res2 = _next_run(imp, st, client).run()                # a LATER run, unchanged → SKIP
    assert res2.units[0]["import_status"] == "skipped"
    assert client.puts_to(_ASSIGN) == []


def test_adopt_nested_only_applies_entitlements_without_assignment(fast):
    c1 = _grp("C1", [], ents=("databricks-sql-access",))
    client = FakeScimClient(groups=[{"id": "src-C1", "displayName": "C1",
                                     "meta": {"resourceType": "Group"}}])
    imp, _st = _importer(client, [_grp("P1", ["USER"], members=("C1",)), c1])
    imp.units_by_type = {"group": [c1, _grp("P1", ["USER"], members=("C1",))]}
    res = imp.run()
    row = {r["natural_key"]: r for r in res.units}["C1"]
    assert row["import_status"] == "updated", "an entitlement was added → updated (§3B.4)"
    assert row["entitlements_copied"] == "databricks-sql-access"
    assert "reached through its group on the target, not directly assigned" in row["note"]
    assert row["assignment"] == "via group (P1)"
    assert _puts(client, "src-C1") == []


def test_adopt_direct_on_source_via_group_on_target_ensures_direct_assignment(fast):
    """The reverse case: direct on source, only via a group on the target at phase start."""
    sp = _sp("sph", ["USER"])
    client = FakeScimClient(sps=[{"id": "tgt-sph", "applicationId": "sph", "displayName": "sp"}])
    imp, _st = _importer(client, [sp])
    res = imp.run()
    assert res.units[0]["import_status"] == "adopted"
    assert _puts(client, "tgt-sph") == [("PUT", f"{_ASSIGN}tgt-sph", {"permissions": ["USER"]})]
    assert "direct workspace assignment applied (as on source)" in res.units[0]["note"]
    assert res.units[0]["assignment"] == "direct"


def test_adopt_preexisting_direct_on_1_2_0_target_left_unchanged_with_db3_note(fast):
    c1 = _grp("C1", [])
    client = FakeScimClient(groups=[{"id": "src-C1", "displayName": "C1",
                                     "meta": {"resourceType": "Group"}}])
    client.assignments = [{"principal": {"principal_id": "src-C1"}, "permissions": ["USER"]}]
    imp, _st = _importer(client, [_grp("P1", ["USER"], members=("C1",)), c1])
    res = imp.run()
    row = {r["natural_key"]: r for r in res.units}["C1"]
    assert row["import_status"] == "adopted"
    assert "directly assigned on target, nested only on source (via `P1`); left unchanged" \
        in row["note"]
    assert row["assignment"] == "direct on target (pre-existing); nested on source"
    assert [c for c in client.calls if c[0] in ("PUT", "DELETE") and "src-C1" in c[1]] == [], \
        "DB3: never removed, never re-PUT"


def test_unknown_permissions_none_keeps_user_fallback(fast):
    g = _grp("G", None)
    imp, _st = _importer(FakeScimClient(), [g])
    client = imp.client
    imp.run()
    assert _puts(client, "src-G") == [("PUT", f"{_ASSIGN}src-G", {"permissions": ["USER"]})]
    assert imp._is_nested_only(g) is False


def test_workspace_local_group_is_never_nested_only(fast):
    client = FakeScimClient()
    loc_user = _grp("L1", ["USER"], kind="workspace_local")
    loc_empty = _grp("L2", [], kind="workspace_local")
    imp, _st = _importer(client, [loc_user, loc_empty])
    assert not imp._is_nested_only(loc_user) and not imp._is_nested_only(loc_empty)
    res = imp.run()
    assert {r["import_status"] for r in res.units} == {"created"}
    assert len(client.posts_to("/Groups")) == 2


def test_system_groups_are_never_nested_only(fast):
    imp, _st = _importer(FakeScimClient(), [])
    for name in ("users", "admins"):
        for kind in ("system", "account"):
            assert not imp._is_nested_only(_grp(name, [], kind=kind))
    assert not imp._is_nested_only(_iunit("group_membership", "admins", {}, kind="system",
                                          workspace_permissions=[]))


# Captured from main @ 4724c59 (1.2.0) running this exact scenario (PLAN §9).
_GOLDEN_1_2_0_DIRECT = [
    ("GET", "api/2.0/preview/permissionassignments", None),
    ("POST", f"{_SCIM}/Users", {"userName": "boss@corp.com"}),
    ("PUT", f"{_ASSIGN}scim-1", {"permissions": ["ADMIN"]}),
    ("POST", f"{_SCIM}/Users", {"userName": "plain@corp.com"}),
    ("PUT", f"{_ASSIGN}scim-2", {"permissions": ["USER"]}),
    ("POST", f"{_SCIM}/ServicePrincipals", {"displayName": "sp1", "applicationId": "app-1"}),
    ("PUT", f"{_ASSIGN}acct-app-1", {"permissions": ["USER"]}),
    ("PUT", f"{_ASSIGN}src-g-user", {"permissions": ["USER"]}),
    ("PUT", f"{_ASSIGN}src-g-admin", {"permissions": ["ADMIN"]}),
]


def test_direct_identities_create_call_sequence_unchanged(fast):
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _iunit("user", "boss@corp.com", {"userName": "boss@corp.com"}, kind="account",
               workspace_permissions=["ADMIN"]),
        _iunit("user", "plain@corp.com", {"userName": "plain@corp.com"}, kind="account",
               workspace_permissions=["USER"]),
        _iunit("service_principal", "app-1", {"displayName": "sp1"}, kind="account",
               workspace_permissions=["USER"]),
        _iunit("group", "g-user", {"displayName": "g-user"}, kind="account",
               workspace_permissions=["USER"]),
        _iunit("group", "g-admin", {"displayName": "g-admin"}, kind="account",
               workspace_permissions=["ADMIN"]),
    ])
    imp.run()
    assert [tuple(c) for c in client.calls] == _GOLDEN_1_2_0_DIRECT


def test_nested_identity_published_for_acl_run_as_and_secret_scope_phases(fast):
    client, units = _nested_bed()
    imp, _st = _importer(client, units)
    imp.context = ctx = {}
    imp.run()
    ti = ctx["target_identities"]
    assert ti["service_principals"]["s1"] == "src-s1"
    assert ti["groups"]["C1"] == "src-C1" and ti["users"]["nu@x.com"] == "src-nu"


def test_dry_run_nested_rows_are_would_adopt_and_never_poll(fast):
    client, units = _nested_bed()
    imp, _st = _importer(client, units, dry_run=True)
    res = imp.run()
    row = {r["natural_key"]: r for r in res.units}["C1"]
    assert row["dry_run"] and row["note"].startswith(
        "dry run: would ADOPT (nested on source via `P1`; no direct assignment)")
    assert row["entitlements_would_copy"] == "databricks-sql-access"
    assert client.scim_calls == ["Users", "ServicePrincipals", "Groups"], "existence check only"
    assert [c for c in client.calls if c[0] != "GET"] == []


def test_permissions_for_unit_raises_if_reached_for_nested_only(fast):
    imp, _st = _importer(FakeScimClient(), [])
    with pytest.raises(RuntimeError, match="nested-only identity reached the assignment path"):
        imp._permissions_for_unit(_grp("C1", []))
    assert imp._permissions_for_unit(_grp("G", ["ADMIN"])) == ["ADMIN"]


# ═══════════════════════════════ DB5 entitlements ═══════════════════════════════

def test_entitlements_get_patch_get_reports_copied_and_present(fast):
    path = f"{_SCIM}/Users/tgt-a"
    client = FakeScimClient(users=[{"id": "tgt-a", "userName": "a@x.com"}])
    client.attrs[path] = {"entitlements": {"workspace-access"}}
    imp, _st = _importer(client, [_user("a@x.com", ["USER"],
                                        ents=("workspace-access", "databricks-sql-access"))])
    res = imp.run()
    row = res.units[0]
    assert row["entitlements_copied"] == "databricks-sql-access"
    assert row["entitlements_present"] == "workspace-access"
    assert row["entitlements_failed"] == ""
    seq = [(c[0], c[1]) for c in client.calls if c[1] == path]
    assert seq == [("GET", path), ("PATCH", path), ("GET", path)]
    patched = client.patches_to(path)[0][2]["Operations"][0]["value"]
    assert patched == [{"value": "databricks-sql-access"}], "only the missing value is sent"
    assert "entitlements: copied databricks-sql-access; already present workspace-access" \
        in row["note"]


def test_entitlement_patch_error_fails_row_not_applied_with_server_message(fast):
    client = FakeScimClient()
    client.fail_paths = {f"{_SCIM}/Users/scim-1"}
    imp, st = _importer(client, [_user("a@x.com", ["USER"], ents=("workspace-access",))])
    row = imp.run().units[0]
    assert row["import_status"] == "failed" and row["failure_category"] == "not_applied"
    assert row["target_id"] == "scim-1"
    assert "PERMISSION_DENIED: cannot patch" in row["error_raw"]
    assert "exists on target, but entitlements could not be applied: workspace-access" in row["note"]
    assert "Re-run with retry_mode=failed_only" in row["note"]
    assert _puts(client, "scim-1"), "the assignment is still applied before the row fails"
    assert st.row("user", "a@x.com")["last_action"] == "failed"


def test_entitlement_patch_200_but_missing_after_get_fails_row(fast):
    client = FakeScimClient()
    client.lose_patches = True
    imp, _st = _importer(client, [_user("a@x.com", ["USER"], ents=("workspace-access",))])
    row = imp.run().units[0]
    assert row["import_status"] == "failed" and row["failure_category"] == "not_applied"
    assert "workspace-access (sent, but not present after a fresh GET)" in row["entitlements_failed"]
    gets = [c for c in client.calls if c[0] == "GET" and c[1] == f"{_SCIM}/Users/scim-1"]
    assert len(gets) == 1 + ii.ENTITLEMENT_READ_TRIES
    assert fast == [ii.ENTITLEMENT_READ_SEC] * (ii.ENTITLEMENT_READ_TRIES - 1)


def test_identity_without_entitlements_makes_no_entitlement_calls(fast):
    client = FakeScimClient()
    imp, _st = _importer(client, [_user("a@x.com", ["USER"])])
    row = imp.run().units[0]
    assert [c for c in client.calls if "/Users/" in c[1]] == []
    assert row["entitlements_copied"] == row["entitlements_present"] == ""


def test_failed_only_retries_entitlement_failure_via_adopt_and_heals(fast):
    """A DB5 failure is retried by failed_only; the identity exists, so the retry reconciles it
    (the failed row keeps no fingerprint, so the decision is UPDATE — same re-apply + re-verify
    as the adopt path) and the row heals."""
    client = FakeScimClient()
    client.lose_patches = True
    unit = _user("a@x.com", ["USER"], ents=("workspace-access",))
    imp, st = _importer(client, [unit])
    assert imp.run().units[0]["import_status"] == "failed"
    st.flush()
    client.lose_patches = False
    client.scim["Users"] = [{"id": "scim-1", "userName": "a@x.com"}]
    imp2 = IdentityImporter(client, imp.config, imp.staging, state=st,
                            units_by_type=imp.units_by_type, retry_keys={("user", "a@x.com")})
    row = imp2.run().units[0]
    assert row["import_status"] in ("updated", "adopted") and row["failure_category"] == ""
    assert row["entitlements_copied"] == "workspace-access"
    assert st.row("user", "a@x.com")["last_action"] == row["import_status"]


def test_account_level_role_still_dropped_and_logged(fast, caplog):
    client = FakeScimClient()
    imp, _st = _importer(client, [_iunit(
        "service_principal", "app-r", {"displayName": "r", "applicationId": "app-r",
                                       "roles": [{"value": "account_admin"}, {"value": "arn:x"}]},
        kind="account", workspace_permissions=["USER"])])
    with caplog.at_level(logging.INFO):
        row = imp.run().units[0]
    sent = [op["value"] for c in client.patches_to("/ServicePrincipals/")
            for op in c[2]["Operations"] if op["path"] == "roles"]
    assert sent == [[{"value": "arn:x"}]]
    assert row["entitlements_copied"] == "role:arn:x"
    assert "account_admin" not in row["entitlements_copied"] + row["entitlements_failed"]
    assert any("skipped account-level role" in r.getMessage() for r in caplog.records)


def _render(rows):
    import openpyxl
    from src.config.config_manager import Config
    from src.reports.import_report import _render_xlsx
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r1",
                            "target_staging_location": "/tmp"})
    summary = {"run_id": "r1", "source_workspace_id": "111", "connectivity_mode": "direct",
               "dry_run": False, "run_status": "completed", "deleted_in_source": {}}
    path = os.path.join(tempfile.mkdtemp(), "s.xlsx")
    _render_xlsx(path, cfg, summary, rows, None, None, None)
    return openpyxl.load_workbook(path)


def test_report_identity_sheets_have_entitlement_and_assignment_columns(fast):
    from src.reports.inventory_view import _LABELS
    client, units = _nested_bed()
    imp, _st = _importer(client, units)
    rows = list(imp.run().units) + [{"asset_type": "job", "natural_key": "j", "import_status":
                                     "created", "note": "", "target_id": "1"}]
    wb = _render(rows)
    new_cols = ["Entitlements copied", "Already present", "Entitlements failed", "Assignment"]
    for card in ("users", "service_principals", "groups"):
        ws = wb[_LABELS.get(card, card)[:31]]
        header = [c.value for c in ws[1]]
        i = header.index("Note / reason")
        assert header[i + 1:i + 5] == new_cols, (card, header)
    groups = wb[_LABELS.get("groups", "groups")[:31]]
    header = [c.value for c in groups[1]]
    by_key = {r[header.index("Natural Key")].value: r for r in groups.iter_rows(min_row=2)}
    c1 = by_key["C1"]
    assert c1[header.index("Entitlements copied")].value == "databricks-sql-access"
    assert c1[header.index("Assignment")].value == "via group (P1)"
    jobs = wb[_LABELS.get("jobs", "jobs")[:31]]
    assert "Entitlements copied" not in [c.value for c in jobs[1]]


def test_non_identity_rows_have_no_entitlement_keys():
    client = RecordingClient(status_paths={U})
    imp, _st = _make(WorkspaceImporter, [_wunit("directory", f"{U}/a", {"path": f"{U}/a"})], client)
    row = imp.run().units[0]
    assert set(row) == {"asset_type", "natural_key", "family", "source_id", "target_id",
                        "import_status", "action_taken", "fingerprint", "note", "error_raw",
                        "failure_category", "dry_run"}


def test_dry_run_intent_is_only_for_nested_creates():
    imp, _st = _importer(FakeScimClient(), [])
    assert imp.dry_run_intent(_grp("C1", []), UpsertAction.ADOPT) == ""
    assert imp.dry_run_intent(_grp("G", ["USER"]), UpsertAction.CREATE) == ""


# ── group membership: id-first matching + nested members held until PASS 3 (2026-11-25 live) ──
# Live Run 1 (full bed): `users` reported "160/174 members added; could not resolve 14 … stays
# under-populated" while the target group matched the source BY ID (174/174). 13 were account users
# whose display name differs between the two workspaces; 1 was a nested-only user that PASS 3 only
# finds after membership was applied.

def _users_membership(members):
    return _iunit("group_membership", "users", {"displayName": "users", "members": members},
                  kind="system", import_action="add_members")


def _member_patches(client, gid):
    return [v["value"] for p in client.patches_to(f"/Groups/{gid}")
            if p[2]["Operations"][0]["path"] == "members" for v in p[2]["Operations"][0]["value"]]


def test_member_whose_display_name_differs_on_target_resolves_by_source_id(fast):
    client = FakeScimClient(
        users=[{"id": "acct-1", "userName": "julia.wu@x.com", "displayName": "Julia Wu"}],
        groups=[{"id": "grp-users", "displayName": "users"}])
    imp, _st = _importer(client, [
        _user("julia.wu@x.com", ["USER"]),
        _users_membership([{"value": "acct-1", "display": "julia.wu Wu", "kind": "user"}])])
    res = imp.run()
    row = next(r for r in res.units if r["asset_type"] == "group_membership")
    assert _member_patches(client, "grp-users") == ["acct-1"]
    assert "could not resolve" not in row["note"] and row["import_status"] != "created_with_warning"
    assert "1/1 members added" in row["note"]


def test_name_fallback_still_used_when_source_id_is_not_on_target(fast):
    """A recreated workspace-local identity has a NEW id — the name path must still work."""
    client = FakeScimClient(users=[{"id": "tgt-9", "userName": "a@x.com", "displayName": "A"}],
                            groups=[{"id": "grp-users", "displayName": "users"}])
    imp, _st = _importer(client, [
        _users_membership([{"value": "src-old-id", "display": "A", "kind": "user"}])])
    imp.run()
    assert _member_patches(client, "grp-users") == ["tgt-9"]


def test_nested_only_member_is_held_then_added_after_pass_3_without_a_warning(fast):
    client, units = _nested_bed()
    client.scim["Groups"].append({"id": "grp-users", "displayName": "users"})
    units.append(_users_membership([{"value": "src-nu", "display": "nu@x.com", "kind": "user"}]))
    imp, st = _importer(client, units)
    res = imp.run()
    row = next(r for r in res.units if r["asset_type"] == "group_membership")
    assert _member_patches(client, "grp-users") == ["src-nu"], "added once PASS 3 found it"
    assert row["import_status"] != "created_with_warning", row
    assert "could not resolve" not in row["note"]
    assert "1/1 members added (1 after the nested-identity pass)" in row["note"]
    assert st.row("group_membership", "users")["last_action"] != "created_with_warning"


def test_nested_only_member_still_invisible_after_pass_3_is_a_real_warning(fast):
    client, units = _nested_bed()
    client.reveal_on_put = {}                      # parent assignment never reveals the children
    client.scim["Groups"].append({"id": "grp-users", "displayName": "users"})
    units.append(_users_membership([{"value": "src-nu", "display": "nu@x.com", "kind": "user"}]))
    imp, _st = _importer(client, units)
    res = imp.run()
    row = next(r for r in res.units if r["asset_type"] == "group_membership")
    assert row["import_status"] == "created_with_warning"
    assert "0/1 members added" in row["note"] and "could not resolve 1 member(s)" in row["note"]
    assert client.patches_to("/Groups/grp-users") == [], "nothing resolvable → no empty PATCH"
