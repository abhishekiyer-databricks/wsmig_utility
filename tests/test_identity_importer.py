"""Offline tests for the identity importer — phase 1 (Plan 3 §6, §7.1).

Identity is the highest-risk phase, and its risks are specific:
  • creating an account-managed SP instead of assigning it mints a NEW applicationId and ORPHANS
    every ACL that referenced it — so create-vs-assign is tested per classification;
  • a recreated DB-managed SP's old→new appId map cannot be rebuilt from the target, so the map must
    be written per identity, including for identities we deliberately do NOT create;
  • groups must be two-pass, or nested membership resolves only by luck of ordering;
  • built-in group MEMBERSHIP must migrate even though the group itself must not be created —
    otherwise a source admin silently isn't an admin on target.
"""
from __future__ import annotations

import tempfile

from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.importers.identity_importer import IdentityImporter
from src.state.state_store import StateStore
from tests.test_state_store import FakeBackend


class FakeScimClient:
    """Records every SCIM call and mints ids, so ordering and payloads can be asserted."""

    def __init__(self, users=None, sps=None, groups=None):
        self.scim = {"Users": list(users or []), "ServicePrincipals": list(sps or []),
                     "Groups": list(groups or [])}
        self.calls: list[tuple] = []
        self._n = 0
        self.fail_paths: set = set()
        self.assignments: list = []

    def get_scim(self, resource, max_items=0, count=500):
        return list(self.scim.get(resource, []))

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if "permissionassignments" in path:
            return {"permission_assignments": list(self.assignments)}
        return {}

    def put(self, path, body):
        self.calls.append(("PUT", path, body))
        if path in self.fail_paths:
            raise RuntimeError("PERMISSION_DENIED: cannot assign")
        return dict(body)

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path in self.fail_paths:
            raise RuntimeError("INVALID_PARAMETER_VALUE: nope")
        if path.endswith("/ServicePrincipals"):
            # Mirrors the REAL API, verified live 2026-08-06 (Plan 6 F4): an explicit
            # applicationId ADOPTS the existing account SP and preserves it; omitting it mints a
            # brand-new one. A fake that always minted a new appId is what let the duplicate-SP
            # bug pass its tests.
            requested = body.get("applicationId")
            if requested:
                for sp in self.scim["ServicePrincipals"]:
                    if sp.get("applicationId") == requested:
                        raise RuntimeError(
                            f"Service principal with application ID {requested} already exists.")
                self._n += 1
                return {"id": f"acct-{requested}", "applicationId": requested}
            self._n += 1
            return {"id": f"scim-{self._n}", "applicationId": f"new-app-uuid-{self._n}"}
        if path.endswith("/Users"):
            # Dedupes by userName (F5): an existing account user is adopted, not duplicated.
            for u in self.scim["Users"]:
                if u.get("userName") == body.get("userName"):
                    return dict(u)
            self._n += 1
            return {"id": f"scim-{self._n}", "userName": body.get("userName")}
        if path.endswith("/Groups"):
            for g in self.scim["Groups"]:
                if g.get("displayName") == body.get("displayName"):
                    raise RuntimeError(
                        f"Group with name {body.get('displayName')} already exists.")
            self._n += 1
            return {"id": f"scim-{self._n}", "displayName": body.get("displayName"),
                    "meta": {"resourceType": "WorkspaceGroup"}}
        self._n += 1
        return {"id": f"scim-{self._n}"}

    def puts_to(self, needle):
        return [c for c in self.calls if c[0] == "PUT" and needle in c[1]]

    def patch(self, path, body):
        self.calls.append(("PATCH", path, body))
        if path in self.fail_paths:
            raise RuntimeError("PERMISSION_DENIED: cannot patch")
        return {}

    def patches_to(self, needle):
        return [c for c in self.calls if c[0] == "PATCH" and needle in c[1]]

    def posts_to(self, needle):
        return [c for c in self.calls if c[0] == "POST" and needle in c[1]]


def _unit(asset_type, key, payload, classification="", kind=None, **over):
    u = {"asset_type": asset_type, "natural_key": key, "source_id": f"src-{key}",
         "fingerprint": f"sha256:{key}", "import_action": "create", "export_status": "success",
         "classification": classification, "kind": kind if kind is not None else classification,
         "payload": payload, "note": ""}
    u.update(over)
    return u


def _importer(client, units, dry_run=False, state=True):
    d = tempfile.mkdtemp()
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r1",
                            "target_staging_location": d, "dry_run": dry_run,
                            "imports": ({"state_catalog": "c", "state_schema": "s"}
                                        if state else {})})
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    st = None
    if state:
        st = StateStore(FakeBackend(), cfg)
        st.ensure_table()
        st.load()
    by_type: dict = {}
    for u in units:
        by_type.setdefault(u["asset_type"], []).append(u)
    return IdentityImporter(client, cfg, aw, state=st, units_by_type=by_type), st


# ── create vs assign, per classification ───────────────────────────────────

def test_an_sp_is_created_WITH_its_source_applicationId_so_the_appid_is_preserved():
    """THE regression guard for Plan 6 F4 — the highest-value assertion in this file.

    Verified live 2026-08-06: POSTing an SP WITH `applicationId` adopts the account SP and keeps the
    id; omitting it mints a new one. The old code omitted it, which silently orphaned every ACL,
    job run_as and secret grant referencing that SP and produced 13 duplicate appIds at account
    level. So the create body MUST carry applicationId, and the map must come out as identity.
    """
    client = FakeScimClient()
    imp, st = _importer(client, [
        _unit("service_principal", "app-uuid-1",
              {"displayName": "etl-sp", "entitlements": [{"value": "allow-cluster-create"}]},
              kind="account")])
    res = imp.run()
    assert res.created == 1
    posts = client.posts_to("/ServicePrincipals")
    assert posts, "the SP was never created"
    assert posts[0][2].get("applicationId") == "app-uuid-1", \
        "applicationId MUST be sent, or the target mints a new one and orphans every ACL"
    mapping = st.load_identity_map()["sp_mapping"]
    assert mapping["app-uuid-1"] == "app-uuid-1", \
        "the applicationId must be PRESERVED (identity map), not remapped to a new one"


def test_an_sp_that_already_exists_at_the_account_is_ADOPTED_not_failed():
    """`already exists` is the re-run / multi-workspace path, not an error.

    100+ workspace pairs share one account, so by the time pair N runs, pair 1 has usually already
    put this SP in the account. That must be an adopt with the SAME appId, never a failure and never
    a duplicate.
    """
    client = FakeScimClient(sps=[{"id": "sp-existing", "applicationId": "app-uuid-1"}])
    imp, st = _importer(client, [
        _unit("service_principal", "app-uuid-1", {"displayName": "etl-sp"}, kind="account")])
    res = imp.run()
    assert res.failed == 0, "an existing account SP must not be reported as a failure"
    assert st.load_identity_map()["sp_mapping"]["app-uuid-1"] == "app-uuid-1"


def test_an_appid_that_changes_despite_being_requested_is_a_LOUD_warning():
    """Should be impossible now, but if it ever happens every ACL is silently orphaned."""
    client = FakeScimClient()
    # Simulate a workspace that ignores the requested applicationId.
    client.post = lambda path, body, _o=client.post: (
        {"id": "x", "applicationId": "TOTALLY-DIFFERENT"} if path.endswith("/ServicePrincipals")
        else _o(path, body))
    imp, _st = _importer(client, [
        _unit("service_principal", "app-uuid-1", {"displayName": "sp"}, kind="account")])
    res = imp.run()
    row = res.units[0]
    assert "applicationId CHANGED" in (row.get("note") or ""), \
        "a changed appId must be reported loudly, not logged and forgotten"


def test_an_account_sp_already_assigned_is_adopted_and_its_target_id_recorded():
    """The appId is stable but the TARGET SCIM id differs — and ACL remap needs that target id, so
    an adopt MUST still write an identity-map row."""
    client = FakeScimClient(sps=[{"id": "scim-existing", "applicationId": "umi-app-uuid"}])
    imp, st = _importer(client, [
        _unit("service_principal", "umi-app-uuid", {"displayName": "umi"},
              kind="account")])
    res = imp.run()
    assert res.adopted == 1 and res.failed == 0
    m = st.load_identity_map()
    assert m["sp_mapping"]["umi-app-uuid"] == "umi-app-uuid", "appId must be unchanged"
    assert m["scim_ids"]["service_principal:umi-app-uuid"] == "scim-existing", \
        "the TARGET scim id must be recorded even though nothing was created"


def test_a_user_is_posted_and_needs_NO_account_admin_prerequisite():
    """Plan 6 F3/F5: the workspace SCIM POST creates the user at the ACCOUNT and assigns it, and
    dedupes by userName. The old code refused and demanded a manual account-admin assignment for
    every user, which the API makes unnecessary."""
    client = FakeScimClient()
    imp, st = _importer(client, [
        _unit("user", "someone@corp.com", {"userName": "someone@corp.com"}, kind="account")])
    res = imp.run()
    assert client.posts_to("/Users"), "the user must be POSTed (that IS the assignment)"
    assert res.failed == 0, "a user must never be a prerequisite failure any more"
    assert st.row("user", "someone@corp.com")["failure_category"] in ("", None)


def test_an_existing_entra_user_is_adopted_with_its_target_id():
    client = FakeScimClient(users=[{"id": "u-9", "userName": "someone@corp.com"}])
    imp, st = _importer(client, [
        _unit("user", "someone@corp.com", {"userName": "someone@corp.com"},
              kind="account")])
    res = imp.run()
    assert res.adopted == 1
    assert st.load_identity_map()["scim_ids"]["user:someone@corp.com"] == "u-9"


# ── groups: two-pass, name-based member resolution ─────────────────────────

def test_groups_are_created_empty_then_members_patched():
    """Two-pass, so nested/cross membership resolves regardless of order."""
    client = FakeScimClient(users=[{"id": "u-1", "userName": "a@b.com"}])
    imp, _st = _importer(client, [
        _unit("group", "finance", {"displayName": "finance",
                                   "members": [{"display": "a@b.com", "kind": "user",
                                                "value": "SOURCE-ID-IGNORED"}]},
              kind="workspace_local")])
    imp.run()
    create = client.posts_to("/Groups")
    assert len(create) == 1
    assert "members" not in create[0][2], "the group must be created EMPTY, members come after"
    patches = client.patches_to("/Groups/")
    member_patch = [p for p in patches if p[2]["Operations"][0]["path"] == "members"]
    assert member_patch, "members were never patched"
    # resolved BY NAME to the TARGET id, never the source id
    assert member_patch[0][2]["Operations"][0]["value"] == [{"value": "u-1"}]


def test_nested_group_membership_resolves_in_either_order():
    """A parent group listed BEFORE its child must STILL get the child as a member.

    This is the whole point of the second pass: membership is applied only after every group exists,
    so bundle ordering is irrelevant. Patching members during each group's own create looked
    equivalent but silently under-populated any group whose members came later in the bundle — the
    exact bug two-pass exists to prevent.
    """
    client = FakeScimClient()
    imp, _st = _importer(client, [
        # parent FIRST, child second — the ordering that a one-pass implementation gets wrong
        _unit("group", "parent", {"displayName": "parent",
                                  "members": [{"display": "child", "kind": "group"}]},
              kind="workspace_local"),
        _unit("group", "child", {"displayName": "child", "members": []},
              kind="workspace_local"),
    ])
    res = imp.run()
    assert res.created == 2
    parent_row = next(r for r in res.units if r["natural_key"] == "parent")
    assert parent_row["note"] == "1/1 members added", \
        "the forward-referenced child group was not added to its parent"
    assert parent_row["import_status"] == "created", "a fully-populated group is not degraded"
    # and the member really is the CHILD's target id, resolved by name
    child_id = imp._target_groups["child"]
    member_patch = [p for p in client.patches_to("/Groups/")
                    if p[2]["Operations"][0]["path"] == "members"]
    assert member_patch[0][2]["Operations"][0]["value"] == [{"value": child_id}]


def test_a_member_that_is_a_recreated_sp_resolves_through_the_id_map():
    """A DB-managed SP got a NEW appId, so the source appId matches nothing on target — the map is
    what closes the gap."""
    client = FakeScimClient(sps=[{"id": "sp-t", "applicationId": "new-app"}])
    imp, _st = _importer(client, [
        _unit("group", "g", {"displayName": "g",
                             "members": [{"display": "old-app", "kind": "service_principal"}]},
              kind="workspace_local")])
    imp.identity_map = {"sp_mapping": {"old-app": "new-app"}}
    imp.run()
    patch = [p for p in client.patches_to("/Groups/")
             if p[2]["Operations"][0]["path"] == "members"]
    assert patch and patch[0][2]["Operations"][0]["value"] == [{"value": "sp-t"}]


def test_an_unresolvable_member_does_not_lose_the_resolvable_ones():
    """Sending a dangling id would fail the whole PATCH; partial success + an explicit list is
    strictly better than all-or-nothing."""
    client = FakeScimClient(users=[{"id": "u-1", "userName": "here@b.com"}])
    imp, _st = _importer(client, [
        _unit("group", "g", {"displayName": "g", "members": [
            {"display": "here@b.com", "kind": "user"},
            {"display": "missing@b.com", "kind": "user"}]},
              kind="workspace_local")])
    res = imp.run()
    patch = [p for p in client.patches_to("/Groups/")
             if p[2]["Operations"][0]["path"] == "members"]
    assert patch[0][2]["Operations"][0]["value"] == [{"value": "u-1"}]
    assert "1/2 members added" in res.units[0]["note"]
    assert "missing@b.com" in res.units[0]["note"], "the unresolved member must be NAMED"


def test_an_account_group_is_ASSIGNED_never_posted():
    """Plan 6 F6 — POSTing an account group creates a workspace-local SHADOW that permanently
    blocks assigning the real one. So the importer must PUT permissionassignments instead."""
    client = FakeScimClient()
    imp, st = _importer(client, [
        _unit("group", "corp-analysts", {"displayName": "corp-analysts"}, kind="account")])
    imp.context["account_principal_ids"] = {"groups": {"corp-analysts": "acct-grp-77"}}
    res = imp.run()
    assert client.posts_to("/Groups") == [], "an account group must NEVER be POSTed"
    puts = client.puts_to("permissionassignments/principals/acct-grp-77")
    assert puts, "the account group was never assigned"
    assert res.failed == 0
    assert st.load_identity_map()["scim_ids"]["group:corp-analysts"] == "acct-grp-77", \
        "the ACCOUNT id must be preserved as the target id"


def test_an_account_group_shadowed_by_a_workspace_group_on_target_is_BLOCKING():
    """The single most damaging state in the plan: a same-named workspace-local group on target
    makes the real account group unassignable forever, and nothing looks wrong in the report."""
    client = FakeScimClient(groups=[{"id": "ws-shadow", "displayName": "corp-analysts",
                                     "meta": {"resourceType": "WorkspaceGroup"}}])
    imp, st = _importer(client, [
        _unit("group", "corp-analysts", {"displayName": "corp-analysts"}, kind="account")])
    res = imp.run()
    assert client.posts_to("/Groups") == []
    assert res.failed == 1, "the shadow conflict must fail loudly, not silently adopt the shadow"
    row = st.row("group", "corp-analysts")
    assert row["failure_category"] == "prerequisite_missing"
    assert "shadow" in row["last_error"] and "Delete the workspace-local group" in row["last_error"]


def test_an_account_groups_members_are_NEVER_patched():
    """Multi-workspace safety: an account group's membership is account-GLOBAL, so patching it
    while migrating workspace N would change that group in every OTHER workspace — and Entra would
    revert it anyway. Assignment already brings members along (verified live)."""
    client = FakeScimClient(groups=[{"id": "acct-1", "displayName": "corp-analysts",
                                     "meta": {"resourceType": "Group"}}])
    imp, _st = _importer(client, [
        _unit("group", "corp-analysts",
              {"displayName": "corp-analysts",
               "members": [{"display": "a@b.com", "kind": "user"}]},
              kind="account", members_are_account_owned=True)])
    imp.run()
    member_patches = [p for p in client.patches_to("/Groups/")
                      if p[2]["Operations"][0]["path"] == "members"]
    assert member_patches == [], "an account group's members must never be patched"


def test_a_group_with_an_undetermined_kind_is_refused_rather_than_guessed():
    """No meta.resourceType in the bundle: guessing workspace-local risks the permanent shadow."""
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("group", "mystery", {"displayName": "mystery"}, kind="needs_review")])
    res = imp.run()
    assert client.posts_to("/Groups") == [], "an unclassified group must not be created"
    assert res.failed == 1


def test_account_admin_role_is_never_replayed():
    """Account roles are account-GLOBAL — replaying account_admin while migrating workspace N would
    escalate that identity across every workspace in the account."""
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("user", "boss@corp.com",
              {"userName": "boss@corp.com",
               "roles": [{"value": "account_admin"}, {"value": "workspace-access"}]},
              kind="account")])
    imp.run()
    role_patches = [p for p in client.patches_to("/Users/")
                    if p[2]["Operations"][0]["path"] == "roles"]
    sent = [v["value"] for p in role_patches for v in p[2]["Operations"][0]["value"]]
    assert "account_admin" not in sent, "account_admin must never be granted by this tool"
    assert "workspace-access" in sent, "workspace-scoped grants must still migrate"


def test_workspace_ADMIN_is_reproduced_on_target():
    """ADMIN vs USER lives ONLY in permissionassignments (F8) — without this a source admin
    silently lands on target as a plain USER."""
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("user", "boss@corp.com", {"userName": "boss@corp.com",
                                        "workspace_permissions": ["ADMIN"]}, kind="account")])
    imp.run()
    puts = client.puts_to("permissionassignments/principals/")
    assert puts, "no workspace permission was set"
    assert puts[0][2] == {"permissions": ["ADMIN"]}


# ── built-in groups: never created, but membership MUST migrate ─────────────

def test_builtin_group_membership_is_patched_onto_the_existing_group():
    """Without this a source workspace admin would silently NOT be an admin on target."""
    client = FakeScimClient(users=[{"id": "u-admin", "userName": "boss@corp.com"}],
                            groups=[{"id": "grp-admins", "displayName": "admins"}])
    imp, st = _importer(client, [
        _unit("group_membership", "admins",
              {"displayName": "admins", "members": [{"display": "boss@corp.com", "kind": "user"}]},
              kind="system", import_action="add_members")])
    res = imp.run()
    assert client.posts_to("/Groups") == [], "a built-in group must never be created"
    patch = [p for p in client.patches_to("/Groups/grp-admins")
             if p[2]["Operations"][0]["path"] == "members"]
    assert patch, "membership was not added to the existing built-in group"
    assert patch[0][2]["Operations"][0]["value"] == [{"value": "u-admin"}]
    assert res.created == 1
    # the built-in group still needs a map row, because ACL remap resolves `admins` through it
    assert st.load_identity_map()["group_map"]["admins"] == "grp-admins"


# ── entitlements + roles are SEPARATE patches after create ──────────────────

def test_entitlements_and_roles_are_applied_as_separate_patches_after_create():
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("service_principal", "app-1",
              {"displayName": "sp", "entitlements": [{"value": "allow-cluster-create"}],
               "roles": [{"value": "some-role"}]},
              kind="account")])
    imp.run()
    create = client.posts_to("/ServicePrincipals")[0][2]
    assert "entitlements" not in create and "roles" not in create, \
        "several workspaces reject these inline — they must be separate PATCHes"
    paths = [p[2]["Operations"][0]["path"] for p in client.patches_to("/ServicePrincipals/")]
    assert "entitlements" in paths and "roles" in paths


def test_a_failed_entitlement_patch_does_not_fail_the_identity():
    """The identity already exists by then; losing it over an entitlement would be worse."""
    client = FakeScimClient()
    # The SP now ADOPTS its account id, so the PATCH target is acct-<appId>, not scim-N.
    client.fail_paths = {"api/2.0/preview/scim/v2/ServicePrincipals/acct-app-1"}
    imp, _st = _importer(client, [
        _unit("service_principal", "app-1",
              {"displayName": "sp", "entitlements": [{"value": "allow-cluster-create"}]},
              kind="account")])
    res = imp.run()
    assert res.created == 1 and res.failed == 0
    assert any("could not set entitlements" in w for w in res.warnings)


# ── an SP with OAuth secrets is created but flagged degraded ────────────────

def test_a_workspace_local_sp_with_oauth_secrets_yields_a_created_sp_plus_a_manual_secret_task():
    """PLAN 8 Bug 3 + PLAN 11 Finding-7: a WORKSPACE-LOCAL SP (recreated with a NEW applicationId)
    that had a secret is CREATED cleanly; the un-migratable secret is its OWN standing `manual`
    unit — kept out of failed_only (Bug 2) and re-emitted every run (Bug 5)."""
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("service_principal", "app-1", {"displayName": "sp"},
              kind="workspace_local",
              note="OAuth client secret(s) present — NOT exportable; recreate on target manually.")])
    res = imp.run()
    sp = next(r for r in res.units if r["asset_type"] == "service_principal")
    assert sp["import_status"] in ("created", "adopted"), \
        "the SP object itself is created cleanly — the secret is tracked separately"
    secret = next(r for r in res.units if r["asset_type"] == "service_principal_secret")
    assert secret["import_status"] == "manual", "the secret is a manual task, not a warning"
    assert "Create a new OAuth secret on target" in secret["note"]
    assert res.manual >= 1


def test_an_account_sp_with_oauth_secrets_emits_NO_manual_secret_task():
    """PLAN 11 Finding-7: an ACCOUNT-level SP keeps its applicationId and its account-level OAuth
    secret / UMI credential is intact — assigning it carries the identity, so there is NOTHING to
    re-issue. A `manual` step here is false work that would sit in the Outstanding sheet forever, so
    NO `service_principal_secret` unit is emitted (True or unknown has_secrets)."""
    for note in ("OAuth client secret(s) present — NOT exportable; recreate on target manually.",
                 "could not verify OAuth secrets (proxy unreachable)"):
        client = FakeScimClient()
        imp, _st = _importer(client, [
            _unit("service_principal", "acct-app", {"displayName": "sp"}, kind="account", note=note)])
        res = imp.run()
        assert not any(r["asset_type"] == "service_principal_secret" for r in res.units), \
            "an account SP must NOT get a manual OAuth-secret task"
        assert res.manual == 0


def test_oauth_secret_manual_task_is_emitted_every_run_independent_of_the_sp_outcome():
    """PLAN 8 Bug 3: the secret task (for a workspace-local SP) is driven by the SOURCE note, not by
    the SP's create/skip outcome, so it is re-emitted on EVERY run and never collapses to a generic
    'unchanged' row."""
    def build():
        return _importer(FakeScimClient(), [
            _unit("service_principal", "app-1", {"displayName": "sp"}, kind="workspace_local",
                  note="OAuth client secret(s) present — NOT exportable; recreate on target manually.")])
    imp, _ = build()
    secret_units = [u for u in imp.load() if u["asset_type"] == "service_principal_secret"]
    assert len(secret_units) == 1 and secret_units[0]["import_action"] == "manual"
    # a second, independent run still emits it (note-driven, not state-driven)
    imp2, _ = build()
    assert any(u["asset_type"] == "service_principal_secret" for u in imp2.load())


def test_identity_update_note_names_added_and_removed_entitlements():
    """PLAN 8 Bug 5: update_one names the ACTUAL change — an entitlement added, and one removed IN
    SOURCE that is NOT removed on target — instead of the old fixed 'entitlements/roles re-applied'."""
    import json
    client = FakeScimClient()
    imp, st = _importer(client, [_unit("service_principal", "app-1", {"displayName": "sp"},
                                       kind="account")])
    st.record("service_principal", "app-1", action="created", fingerprint="old",
              target_object_id="acct-app-1",
              source_detail=json.dumps({"entitlements": ["allow-cluster-create", "workspace-access"],
                                        "roles": []}, sort_keys=True))
    st.flush()
    unit = {"asset_type": "service_principal", "natural_key": "app-1",
            "payload": {"entitlements": [{"value": "databricks-sql-access"},
                                         {"value": "workspace-access"}]}}
    out = imp.update_one(unit, "acct-app-1")
    assert "entitlements added: ['databricks-sql-access']" in out["note"]
    assert "removed in source" in out["note"] and "allow-cluster-create" in out["note"]
    assert "databricks-sql-access" in out["source_detail"], "the new snapshot is returned for next run"


def test_group_member_removed_in_source_is_reported_not_applied():
    """PLAN 8 Bug 5: a ws-local group member removed on source is REPORTED (retained on target —
    review), never silently omitted the way the old additive-only note did."""
    import json
    client = FakeScimClient(users=[{"userName": "keep@x", "id": "u-keep", "displayName": "Keep"},
                                   {"userName": "drop@x", "id": "u-drop", "displayName": "Drop"}])
    imp, st = _importer(client, [_unit("group", "g", {"displayName": "g"}, kind="workspace_local")])
    imp.existing_keys()   # populate the display-name index so members resolve
    st.record("group", "g", action="created", fingerprint="old", target_object_id="g-1",
              source_detail=json.dumps({"entitlements": [], "roles": [],
                                        "members": ["Keep", "Drop"]}, sort_keys=True))
    st.flush()
    unit = {"asset_type": "group", "natural_key": "g", "kind": "workspace_local",
            "payload": {"displayName": "g", "members": [{"display": "Keep", "kind": "user"}]}}
    out = imp.update_one(unit, "g-1")
    assert "members removed in source (retained on target — review): ['Drop']" in out["note"]


def test_identity_diff_degrades_gracefully_when_there_is_no_prior_snapshot():
    """PLAN 8 Bug 5: the first run after the column was added has no prior snapshot — a neutral note,
    never a crash on the null."""
    client = FakeScimClient()
    imp, st = _importer(client, [_unit("user", "u@x", {"userName": "u@x"})])
    st.record("user", "u@x", action="created", fingerprint="old", target_object_id="u-1")  # no detail
    st.flush()
    unit = {"asset_type": "user", "natural_key": "u@x", "payload": {"entitlements": []}}
    out = imp.update_one(unit, "u-1")
    assert "prior source detail not recorded" in out["note"]


# ── ordering + dry run ─────────────────────────────────────────────────────

def test_load_order_is_users_then_sps_then_groups_then_membership():
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("group_membership", "admins", {"displayName": "admins", "members": []}),
        _unit("group", "g", {"displayName": "g"}, kind="workspace_local"),
        _unit("user", "u@b.com", {"userName": "u@b.com"}, classification="needs_review"),
        _unit("service_principal", "app", {"displayName": "sp"}, kind="account"),
    ])
    order = [u["asset_type"] for u in imp.load()]
    assert order == ["user", "service_principal", "group", "group_membership"]


def test_dry_run_creates_no_identity_and_writes_no_map():
    client = FakeScimClient()
    imp, st = _importer(client, [
        _unit("service_principal", "app-1", {"displayName": "sp"},
              kind="account")], dry_run=True)
    res = imp.run()
    assert client.posts_to("/ServicePrincipals") == [] and client.patches_to("/") == []
    assert res.dry_run == 1
    assert st.load_identity_map()["sp_mapping"] == {}, \
        "a rehearsal must not write identity mappings"


def test_existence_check_paginates_via_get_scim():
    """SCIM pagination is a known latent bug in the reference tool; a truncated list here would
    report an existing identity as absent and CREATE A DUPLICATE."""
    seen = {}

    class CountingClient(FakeScimClient):
        def get_scim(self, resource, max_items=0, count=500):
            seen[resource] = seen.get(resource, 0) + 1
            return super().get_scim(resource, max_items, count)

    client = CountingClient()
    imp, _st = _importer(client, [])
    imp.existing_keys()
    assert set(seen) == {"Users", "ServicePrincipals", "Groups"}, \
        "all three SCIM types must be listed through the paginating helper"


# ── bugs found while wiring the phases together (not by the unit tests above) ───

def test_kind_survives_the_bundle_round_trip_into_the_importer():
    """`import_runner` merges payload-file fields onto each unit, and originally copied only
    `classification` — so `kind` never reached the importer and EVERY group degraded to
    NEEDS_REVIEW (skipped). The carry-over list must include the fields the importer branches on.
    """
    from src.importers.import_runner import ImportRunner
    carried = ImportRunner._PAYLOAD_CARRY_FIELDS
    for field in ("kind", "members_are_account_owned", "workspace_permissions"):
        assert field in carried, f"`{field}` is not carried from the payload file onto the unit"


def test_a_legacy_bundle_without_kind_still_imports_correctly():
    """A bundle exported before Plan 6 has no `kind`, only the old `classification` vocabulary.

    Without a forward-map every group would classify NEEDS_REVIEW and be skipped, and — worse — an
    old `db_managed_sp` would take the create path. `db_managed_sp` must map to `account` so the SP
    is still adopted by applicationId.
    """
    from src.importers.identity_importer import _kind_of, KIND_ACCOUNT, KIND_WORKSPACE_LOCAL
    assert _kind_of({"classification": "db_managed_group"}) == KIND_WORKSPACE_LOCAL
    assert _kind_of({"classification": "account_group"}) == KIND_ACCOUNT
    assert _kind_of({"classification": "db_managed_sp"}) == KIND_ACCOUNT, \
        "an SP is always an account principal — the legacy name must not route it to create"
    # an explicit new-style kind always wins over the legacy alias
    assert _kind_of({"kind": "account", "classification": "db_managed_group"}) == KIND_ACCOUNT


def test_a_legacy_account_group_is_still_assigned_not_shadowed():
    """End-to-end proof of the legacy map: an old bundle's account group must NOT be POSTed."""
    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("group", "corp-analysts", {"displayName": "corp-analysts"},
              classification="account_group", kind="")])
    imp.context["account_principal_ids"] = {"groups": {"corp-analysts": "acct-99"}}
    res = imp.run()
    assert client.posts_to("/Groups") == [], "a legacy account group must never be POSTed"
    assert client.puts_to("permissionassignments/principals/acct-99"), "it was never assigned"
    assert res.failed == 0


def test_an_account_group_resolves_via_its_source_id_with_no_account_credentials():
    """Found by the live run: with workspace-admin only, resolution had NO path and every account
    group failed as "does not exist in the TARGET account" — even though it did exist.

    An account group's WORKSPACE SCIM id IS its ACCOUNT id (verified live: wsmig_acc_mixed_grp is
    152592557989155 in both), so when source and target share an account the exported `source_id` is
    already the account id, and permissionassignments accepts it workspace-side.
    """
    client = FakeScimClient()
    imp, st = _importer(client, [
        _unit("group", "corp-analysts", {"displayName": "corp-analysts"}, kind="account",
              source_id="152592557989155")])
    res = imp.run()
    assert res.failed == 0, "an account group must not fail merely because we lack account creds"
    assert client.puts_to("permissionassignments/principals/152592557989155"), \
        "the source_id was not used as the account principal id"
    assert st.load_identity_map()["scim_ids"]["group:corp-analysts"] == "152592557989155"


def test_a_failed_assignment_PUT_becomes_an_actionable_prerequisite():
    """A wrong/absent account id surfaces at the PUT. That must read as the same actionable
    prerequisite as an up-front miss, not as a raw API error."""
    client = FakeScimClient()
    client.fail_paths = {"api/2.0/preview/permissionassignments/principals/bogus-id"}
    imp, st = _importer(client, [
        _unit("group", "entra-grp", {"displayName": "entra-grp"}, kind="account",
              source_id="bogus-id", entra_backed=True)])
    res = imp.run()
    assert res.failed == 1
    row = st.row("group", "entra-grp")
    assert row["failure_category"] == "prerequisite_missing"
    assert "Entra SCIM" in row["last_error"], "an Entra group must name Entra SCIM as the fix"
    assert "retry_mode=failed_only" in row["last_error"]


def test_an_ADOPTED_identity_still_gets_its_workspace_permission():
    """Found by the live run: 3 users already present on target were ADOPTED, so `_ensure_assignment`
    (which the create paths call) never ran — a source ADMIN silently stayed a plain USER.

    Adoption is the COMMON case on a re-run and in multi-workspace accounts, so the permission must
    be applied on the adopt path too, not just on create.
    """
    client = FakeScimClient(users=[{"id": "u-existing", "userName": "boss@corp.com"}])
    imp, _st = _importer(client, [
        _unit("user", "boss@corp.com",
              {"userName": "boss@corp.com", "workspace_permissions": ["ADMIN"]},
              kind="account")])
    res = imp.run()
    assert res.adopted == 1, "the user already exists, so this is an adopt"
    puts = client.puts_to("permissionassignments/principals/u-existing")
    assert puts, "an adopted identity must STILL have its workspace permission applied"
    assert puts[0][2] == {"permissions": ["ADMIN"]}


def test_an_already_correct_permission_is_not_re_PUT():
    """Idempotency: re-running against a target that already matches must issue no writes."""
    client = FakeScimClient(users=[{"id": "u-existing", "userName": "boss@corp.com"}])
    client.assignments = [{"principal": {"principal_id": "u-existing",
                                         "user_name": "boss@corp.com"},
                           "permissions": ["ADMIN"]}]
    imp, _st = _importer(client, [
        _unit("user", "boss@corp.com",
              {"userName": "boss@corp.com", "workspace_permissions": ["ADMIN"]},
              kind="account")])
    imp.run()
    assert client.puts_to("permissionassignments") == [], \
        "an unchanged permission must not be re-PUT"


def test_members_resolve_by_DISPLAY_NAME_not_just_userName():
    """Found by the live run: `admins` and `users` imported with 0/5 and 1/13 members.

    A SCIM group member is identified by `display`, which for a USER is their display name
    ("Aman Bansal") — NOT their userName. The per-kind maps are keyed by userName/applicationId, so
    every human member of the built-in groups failed to resolve and those groups imported nearly
    EMPTY: a source workspace admin silently was not an admin on target, which is the exact failure
    the group_membership unit exists to prevent.
    """
    client = FakeScimClient(
        users=[{"id": "u-1", "userName": "aman.bansal@corp.com", "displayName": "Aman Bansal"}],
        sps=[{"id": "sp-1", "applicationId": "app-uuid", "displayName": "etl-sp"}],
        groups=[{"id": "grp-admins", "displayName": "admins"}])
    imp, _st = _importer(client, [
        _unit("group_membership", "admins",
              {"displayName": "admins",
               # exactly the shape the export writes: display names, no `kind`
               "members": [{"display": "Aman Bansal", "value": "8094393104170381",
                            "$ref": "Users/8094393104170381"},
                           {"display": "etl-sp", "value": "144500571493713",
                            "$ref": "ServicePrincipals/144500571493713"}]},
              kind="system")])
    res = imp.run()
    patch = [p for p in client.patches_to("/Groups/")
             if p[2]["Operations"][0]["path"] == "members"]
    assert patch, "members were never patched onto the built-in group"
    values = {v["value"] for v in patch[0][2]["Operations"][0]["value"]}
    assert values == {"u-1", "sp-1"}, f"display-name members did not resolve: {values}"
    assert "2/2 members added" in str(res.units[0].get("note"))


def test_members_created_in_THIS_run_resolve_by_display_name():
    """IMP-7b: the live bug — `wsmig_test_child_grp` reported "could not resolve Sanket Kelkar /
    Aman Bansal" even though both were CREATED in the same run. The display-name index was only
    populated from PRE-EXISTING target identities, never from ones we create in PASS 1. Now a group
    whose members are freshly-created users/SPs resolves them in PASS 2."""
    client = FakeScimClient()   # target starts EMPTY — everything is created this run
    imp, _st = _importer(client, [
        _unit("user", "sanket.kelkar@corp.com", {"displayName": "Sanket Kelkar"}, kind="account"),
        _unit("user", "aman.bansal@corp.com", {"displayName": "Aman Bansal"}, kind="account"),
        _unit("service_principal", "db-sp-app", {"displayName": "wsmig_test_db_sp"},
              kind="account"),
        _unit("group", "wsmig_test_child_grp",
              {"displayName": "wsmig_test_child_grp",
               "members": [{"display": "Sanket Kelkar"}, {"display": "Aman Bansal"},
                           {"display": "wsmig_test_db_sp"}]},
              kind="workspace_local"),
    ])
    res = imp.run()
    grp = next(u for u in res.units if u["asset_type"] == "group"
               and u["natural_key"] == "wsmig_test_child_grp")
    # all three members (2 freshly-created users + 1 freshly-created SP) must resolve
    assert "3/3 members added" in safe_str_note(grp), \
        f"created-in-run members did not resolve: {grp.get('note')}"


def test_a_databricks_generated_users_clone_group_is_skipped_not_recreated():
    """IMP-7a: `users-clone-<UTC> (created by Databricks)` is a platform artifact, not a customer
    group. It must be SKIPPED, never POSTed as a workspace-local group on target."""
    from src.identity.classifier import classify_group, IdentityKind, is_system_generated_group
    assert is_system_generated_group(
        {"displayName": "users-clone-2026-08-06-2010-UTC (created by Databricks)"})
    assert classify_group(
        {"displayName": "users-clone-2026-08-06-2010-UTC (created by Databricks)"}
    ) == IdentityKind.SYSTEM_GENERATED
    # a normal group is unaffected
    assert not is_system_generated_group({"displayName": "wsmig_test_child_grp"})

    # Export gives it a clean skip action (not review_required / manual — nothing for a human to do).
    from src.exporters.asset_export import derive_import_action
    assert derive_import_action(
        {"asset_type": "group", "classification": "system_generated", "kind": "system_generated",
         "migration_mode": "auto", "export_status": "success",
         "natural_key": "users-clone-2026-08-06-2010-UTC (created by Databricks)"}
    ) == "skip_generated"

    client = FakeScimClient()
    imp, _st = _importer(client, [
        _unit("group", "users-clone-2026-08-06-2010-UTC (created by Databricks)",
              {"displayName": "users-clone-2026-08-06-2010-UTC (created by Databricks)"},
              kind="system_generated", import_action="skip_generated")])
    res = imp.run()
    assert client.posts_to("/Groups") == [], "a Databricks-generated group must never be created"
    assert res.units[0]["import_status"] == "skipped"
    assert "Databricks-generated" in res.units[0]["note"]


def safe_str_note(unit) -> str:
    return str(unit.get("note") or "")
