"""Offline tests for notebooks/tools/Sync_Group_SP_Entitlements.py (core logic, fake workspaces)."""
import itertools
import json
from pathlib import Path

import pytest

_NB = Path(__file__).resolve().parents[1] / "notebooks" / "tools" / "Sync_Group_SP_Entitlements.py"


def _load():
    """Exec the notebook's logic cells (everything before the 'Connect' cell) without dbutils."""
    src = _NB.read_text().split("# MAGIC %md ## Connect")[0]
    src = "\n".join(l for l in src.splitlines() if not l.startswith("dbutils."))
    ns = {}
    exec(compile(src, str(_NB), "exec"), ns)
    return ns


NB = _load()
G, SP = NB["GROUP"], NB["SP"]
_ids = itertools.count(1000)


class FakeWs:
    """groups: {name: (ents, members_by_ref)}; sps: {app_id: (name, ents)}. Failure injection by name/app_id."""

    def __init__(self, groups=None, sps=None, fail_patch=(), drop_on_patch=(), fail_get=()):
        self.objs = {}
        for name, spec in (groups or {}).items():
            ents, members = (spec, []) if isinstance(spec, set) else spec
            self.objs[(G, str(next(_ids)))] = {"name": name, "ents": set(ents), "members": members}
        for app_id, (name, ents) in (sps or {}).items():
            self.objs[(SP, str(next(_ids)))] = {"name": name, "app_id": app_id, "ents": set(ents)}
        self.fail_patch, self.drop_on_patch, self.fail_get = set(fail_patch), set(drop_on_patch), set(fail_get)
        self.patches = []

    def _key(self, o):
        return o.get("app_id") or o["name"]

    def direct_assignment_ids(self):
        return {i for (_, i) in self.objs}

    def list_groups(self):
        out = {}
        for (k, i), o in self.objs.items():
            if k == G:
                out.setdefault(o["name"], []).append({"id": i, "name": o["name"]})
        return out

    def list_service_principals(self):
        out = {}
        for (k, i), o in self.objs.items():
            if k == SP:
                out.setdefault(o["app_id"], []).append({"id": i, "name": o["name"], "app_id": o["app_id"]})
        return out

    def get_entitlements(self, kind, oid):
        o = self.objs[(kind, oid)]
        if self._key(o) in self.fail_get:
            raise NB["ApiError"]("target", "GET", f"/{kind}/{oid}", 403, json.dumps({"detail": "not allowed"}))
        return set(o["ents"])

    def add_entitlements(self, kind, oid, values):
        o = self.objs[(kind, oid)]
        self.patches.append((kind, self._key(o), set(values)))
        if self._key(o) in self.fail_patch:
            raise NB["ApiError"]("target", "PATCH", f"/{kind}/{oid}", 400,
                                 json.dumps({"error_code": "INVALID_PARAMETER_VALUE", "message": "bad"}))
        if self._key(o) not in self.drop_on_patch:
            o["ents"] |= set(values)

    def group_detail(self, gid):
        o = self.objs[(G, gid)]
        if o["name"] in self.fail_get:
            raise NB["ApiError"]("source", "GET", f"/Groups/{gid}", 500, "boom")
        counts = {"users": 0, "service_principals": 0, "groups": 0, "other": 0}
        for ref in o["members"]:
            counts[{"Users": "users", "ServicePrincipals": "service_principals", "Groups": "groups"}[ref]] += 1
        return sorted(o["ents"]), counts


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(NB["time"], "sleep", lambda *_: None)


QUIET = dict(log=lambda *_: None)


def _sync(src, tgt, kinds=(G, SP), name="", dry=False):
    _, rows = NB["run_sync"](src, tgt, list(kinds), name, dry, **QUIET)
    return {(r["type"], r["name"]): r for r in rows}


# ── groups ──────────────────────────────────────────────────────────────────

def test_group_adds_only_missing_and_verifies_and_never_touches_system_groups():
    src = FakeWs({"A": {"workspace-access", "databricks-sql-access"}, "users": set(), "admins": {"x"}})
    tgt = FakeWs({"A": {"databricks-sql-access", "allow-cluster-create"}, "users": set(), "admins": set()})
    r = _sync(src, tgt)
    assert list(r) == [(G, "A")]
    assert r[(G, "A")]["status"] == "ADDED" and r[(G, "A")]["extra_on_target"] == "allow-cluster-create"
    assert tgt.patches == [(G, "A", {"workspace-access"})]


def test_dry_run_writes_nothing():
    src = FakeWs({"A": {"workspace-access"}}, {"app1": ("sp1", {"workspace-access"})})
    tgt = FakeWs({"A": set()}, {"app1": ("sp1", set())})
    r = _sync(src, tgt, dry=True)
    assert {v["status"] for v in r.values()} == {"WOULD_ADD"} and tgt.patches == []


# ── service principals ──────────────────────────────────────────────────────

def test_sp_matched_by_application_id_not_name():
    src = FakeWs(sps={"app1": ("etl-sp", {"workspace-access", "allow-cluster-create"})})
    tgt = FakeWs(sps={"app1": ("etl-sp-RENAMED", {"workspace-access"})})
    r = _sync(src, tgt)
    row = r[(SP, "etl-sp (app1)")]
    assert row["status"] == "ADDED" and tgt.patches == [(SP, "app1", {"allow-cluster-create"})]


def test_sp_in_sync():
    src = FakeWs(sps={"app1": ("s", {"databricks-sql-access"})})
    tgt = FakeWs(sps={"app1": ("s", {"databricks-sql-access"})})
    assert _sync(src, tgt)[(SP, "s (app1)")]["status"] == "IN_SYNC"


def test_sp_silent_patch_caught_by_read_back():
    src = FakeWs(sps={"app1": ("s", {"workspace-access"})})
    tgt = FakeWs(sps={"app1": ("s", set())}, drop_on_patch={"app1"})
    row = _sync(src, tgt)[(SP, "s (app1)")]
    assert row["status"] == "VERIFY_FAILED" and "workspace-access" in row["detail"]


def test_errors_carry_server_message_and_run_continues():
    src = FakeWs({"A": {"workspace-access"}, "B": {"workspace-access"}}, {"app1": ("s", {"workspace-access"})})
    tgt = FakeWs({"A": set(), "B": set()}, {"app1": ("s", set())}, fail_patch={"A"}, fail_get={"app1"})
    r = _sync(src, tgt)
    assert r[(G, "A")]["status"] == "ERROR" and "HTTP 400" in r[(G, "A")]["detail"] \
        and "INVALID_PARAMETER_VALUE: bad" in r[(G, "A")]["detail"]
    assert r[(SP, "s (app1)")]["status"] == "ERROR" and "HTTP 403" in r[(SP, "s (app1)")]["detail"]
    assert r[(G, "B")]["status"] == "ADDED"
    _, problems = NB["summarize"](list(r.values()), **QUIET)
    assert len(problems) == 2


def test_duplicate_group_name_on_target_is_refused():
    src = FakeWs({"A": {"workspace-access"}})
    tgt = FakeWs({"A": set()})
    tgt.objs[(G, "dup")] = {"name": "A", "ents": set(), "members": []}
    assert _sync(src, tgt)[(G, "A")]["status"] == "ERROR" and tgt.patches == []


def test_principal_type_filter():
    src = FakeWs({"A": {"workspace-access"}}, {"app1": ("s", {"workspace-access"})})
    tgt = FakeWs({"A": set()}, {"app1": ("s", set())})
    assert [k for k, _ in _sync(src, tgt, kinds=[SP])] == [SP]
    assert [k for k, _ in _sync(src, tgt, kinds=[G])] == [G]


# ── single-item mode ────────────────────────────────────────────────────────

def test_single_item_by_group_name_sp_name_and_app_id():
    def fresh():
        return (FakeWs({"A": {"workspace-access"}, "B": {"workspace-access"}},
                       {"app1": ("s1", {"workspace-access"}), "app2": ("s2", {"workspace-access"})}),
                FakeWs({"A": set(), "B": set()}, {"app1": ("s1", set()), "app2": ("s2", set())}))
    for name, expect in (("B", [(G, "B")]), ("s2", [(SP, "s2 (app2)")]), ("app1", [(SP, "s1 (app1)")])):
        src, tgt = fresh()
        assert list(_sync(src, tgt, name=name)) == expect
        assert len(tgt.patches) == 1


def test_single_item_not_on_target_is_reported_not_written():
    src, tgt = FakeWs({"A": {"workspace-access"}}), FakeWs({})
    row = _sync(src, tgt, name="A")[(G, "A")]
    assert row["status"] == "NOT_ON_TARGET" and tgt.patches == []


@pytest.mark.parametrize("name, msg", [("nope", "is not a group name"), ("users", "system group")])
def test_single_item_bad_name_fails_fast(name, msg):
    with pytest.raises(ValueError, match=msg):
        _sync(FakeWs({"A": set(), "users": set()}), FakeWs({"A": set()}), name=name)


def test_single_item_ambiguous_sp_name_asks_for_app_id():
    src = FakeWs(sps={"app1": ("same", set()), "app2": ("same", set())})
    with pytest.raises(ValueError, match="applicationId"):
        _sync(src, FakeWs(), name="same")


# ── difference report ───────────────────────────────────────────────────────

def test_differences_both_directions_with_member_counts_and_name_twins():
    src = FakeWs({"both": set(), "sp_only_grp": ({"workspace-access"}, ["ServicePrincipals", "ServicePrincipals"]),
                  "mixed": (set(), ["Users", "ServicePrincipals", "Groups"]), "users": set()},
                 {"app1": ("both-sp", set()), "app2": ("src-sp", {"workspace-access"}), "app3": ("twin", set())})
    tgt = FakeWs({"both": set(), "tgt_grp": set(), "admins": set()},
                 {"app1": ("both-sp", set()), "app9": ("tgt-sp", set()), "app4": ("twin", set())})
    inv = NB["read_inventory"](src, tgt, [G, SP], **QUIET)
    diffs, errors = NB["find_differences"](src, tgt, inv, **QUIET)
    got = {(d["side"], d["type"], d["name"]): d for d in diffs}
    assert set(got) == {("source_only", G, "sp_only_grp"), ("source_only", G, "mixed"),
                        ("target_only", G, "tgt_grp"),
                        ("source_only", SP, "src-sp"), ("source_only", SP, "twin"),
                        ("target_only", SP, "tgt-sp"), ("target_only", SP, "twin")}, "users/admins never listed"
    g = got[("source_only", G, "sp_only_grp")]
    assert (g["members_users"], g["members_sps"], g["members_groups"]) == (0, 2, 0)
    assert g["note"] == "only service principals as members" and g["entitlements"] == "workspace-access"
    assert got[("source_only", G, "mixed")]["note"] == ""
    assert "SAME NAME but applicationId app4" in got[("source_only", SP, "twin")]["note"]
    assert "SAME NAME but applicationId app3" in got[("target_only", SP, "twin")]["note"]
    assert got[("source_only", SP, "src-sp")]["entitlements"] == "workspace-access"
    assert errors == []


def test_difference_read_error_is_reported():
    src = FakeWs({"gone": set()}, fail_get={"gone"})
    inv = NB["read_inventory"](src, FakeWs(), [G], **QUIET)
    diffs, errors = NB["find_differences"](src, FakeWs(), inv, **QUIET)
    assert len(errors) == 1 and "HTTP 500" in errors[0]["note"]


# ── real Api pagination / error text ────────────────────────────────────────

def test_list_pages_through_everything_and_keeps_duplicates():
    names = [f"g{i}" for i in range(250)] + ["g5"]
    api = NB["Api"]("target", "https://x", lambda: "t")
    starts = []

    def fake_call(method, path, params=None, body=None):
        start, count = params["startIndex"], min(params["count"], 7)
        starts.append(start)
        chunk = names[start - 1:start - 1 + count]
        return {"totalResults": len(names),
                "Resources": [{"id": str(start + i), "displayName": n, "applicationId": f"a-{n}"}
                              for i, n in enumerate(chunk)]}
    api.call = fake_call
    groups = api.list_groups()
    assert sum(map(len, groups.values())) == 251 and len(groups["g5"]) == 2 and starts[:3] == [1, 8, 15]
    sps = api.list_service_principals()
    assert len(sps["a-g5"]) == 2


def test_group_detail_counts_members_by_ref_type():
    api = NB["Api"]("source", "https://x", lambda: "t")
    api.call = lambda *a, **k: {"entitlements": [{"value": "workspace-access"}], "members": [
        {"$ref": "ServicePrincipals/1"}, {"$ref": "ServicePrincipals/2"}, {"$ref": "Users/3"}, {"$ref": "Groups/4"}]}
    ents, counts = api.group_detail("9")
    assert ents == ["workspace-access"] and counts == {"users": 1, "service_principals": 2, "groups": 1, "other": 0}


def test_api_error_text():
    err = NB["ApiError"]("source", "GET", "/x", 403, json.dumps({"detail": "This API is disabled"}))
    assert str(err) == "[source] GET /x → HTTP 403: This API is disabled"


def test_reader_sp_is_labelled_not_flagged_as_a_migration_gap():
    src = FakeWs(sps={"reader": ("tool-sp", set())})
    inv = NB["read_inventory"](src, FakeWs(), [SP], **QUIET)
    diffs, _ = NB["find_differences"](src, FakeWs(), inv, source_reader_app_id="reader", **QUIET)
    assert diffs[0]["note"] == "this is the source SP this notebook uses to read the source"


# ── admin access (direct membership of `admins`) ────────────────────────────

def _recording_api(responses):
    api = NB["Api"]("target", "https://x", lambda: "t")
    calls = []

    def fake_call(method, path, params=None, body=None):
        calls.append((method, path, params, body))
        return responses(method, path, params)
    api.call = fake_call
    return api, calls


def test_admin_access_is_read_from_direct_admins_membership_only():
    read = NB["Api"]._entitlements_of
    assert read({"entitlements": [{"value": "workspace-consume"}],
                 "groups": [{"display": "admins", "type": "direct"}]}) == {"workspace-consume", NB["ADMIN"]}
    assert read({"groups": [{"display": "admins", "type": "indirect"}]}) == set(), \
        "admin via a parent group is the parent's grant, synced on its own"
    assert read({"groups": [{"display": "other"}]}) == set()


def test_get_entitlements_asks_for_groups():
    api, calls = _recording_api(lambda m, p, q: {"entitlements": [], "groups": [{"display": "admins", "type": "direct"}]})
    assert api.get_entitlements(SP, "7") == {NB["ADMIN"]}
    assert "groups" in calls[0][2]["attributes"]


def test_admin_access_is_granted_by_adding_to_admins_group():
    def responses(m, p, q):
        if m == "GET":
            return {"Resources": [{"id": "999", "displayName": "admins"}]}
        return {}
    api, calls = _recording_api(responses)
    api.add_entitlements(G, "42", {NB["ADMIN"], "workspace-consume"})
    patches = [(p, b["Operations"][0]) for m, p, _, b in calls if m == "PATCH"]
    assert ("/api/2.0/preview/scim/v2/Groups/42",
            {"op": "add", "path": "entitlements", "value": [{"value": "workspace-consume"}]}) in patches
    assert ("/api/2.0/preview/scim/v2/Groups/999",
            {"op": "add", "path": "members", "value": [{"value": "42"}]}) in patches
    assert not any(NB["ADMIN"] in json.dumps(b) for m, p, _, b in calls if m == "PATCH" and p.endswith("/42"))


def test_admin_only_does_not_send_an_empty_entitlements_patch():
    api, calls = _recording_api(lambda m, p, q: {"Resources": [{"id": "999", "displayName": "admins"}]} if m == "GET" else {})
    api.add_entitlements(SP, "5", {NB["ADMIN"]})
    assert [p for m, p, _, _ in calls if m == "PATCH"] == ["/api/2.0/preview/scim/v2/Groups/999"]


def test_admin_access_flows_through_sync_one():
    src = FakeWs({"C": {"workspace-consume", NB["ADMIN"]}})
    tgt = FakeWs({"C": {"workspace-consume"}})
    row = _sync(src, tgt)[(G, "C")]
    assert row["status"] == "ADDED" and row["added"] == NB["ADMIN"]


# ── assign_missing: bring source-only groups / SPs to the target ────────────

_TYPE = {"group": "Groups", "sp": "ServicePrincipals", "user": "Users"}


class World:
    """An ACCOUNT: principals with ids shared by every workspace."""

    def __init__(self):
        self.p = {}

    def add(self, pid, kind, name, app_id="", members=()):
        self.p[pid] = {"kind": kind, "name": name, "app_id": app_id, "members": set(members)}
        return pid


class Ws:
    """A workspace in `world`: which account principals are assigned, plus its own workspace-local groups."""

    def __init__(self, world, assigned=(), local_groups=None, ents=None, visible_after=0,
                 fail_assign=(), drop_members=False, via_group=()):
        self.w, self.assigned, self.pending = world, set(assigned), {}
        self.via_group = set(via_group)          # listed by SCIM (member of an assigned group) but NOT assigned
        self.hidden_direct = set()               # directly assigned, but missing from the bulk list (guard test)
        self.local = {gid: {"name": n, "members": set(m)} for gid, (n, m) in (local_groups or {}).items()}
        self.ents = {k: set(v) for k, v in (ents or {}).items()}
        self.visible_after, self.fail_assign, self.drop_members = visible_after, set(fail_assign), drop_members
        self.writes = []
        self._new = itertools.count(9000)

    def _tick(self):
        for pid in list(self.pending):
            self.pending[pid] -= 1
            if self.pending[pid] < 0:
                self.assigned.add(pid)
                del self.pending[pid]

    def list_groups(self):
        self._tick()
        out = {}
        for pid in self.assigned:
            if self.w.p[pid]["kind"] == "group":
                out.setdefault(self.w.p[pid]["name"], []).append({"id": pid, "name": self.w.p[pid]["name"], "local": False})
        for gid, g in self.local.items():
            out.setdefault(g["name"], []).append({"id": gid, "name": g["name"], "local": True})
        return out

    def direct_assignment_ids(self):
        self._tick()
        return set(self.assigned)

    def list_service_principals(self):
        self._tick()
        out = {}
        for pid in self.assigned | self.via_group:
            p = self.w.p[pid]
            if p["kind"] == "sp":
                out.setdefault(p["app_id"], []).append({"id": pid, "name": p["name"], "app_id": p["app_id"]})
        return out

    def _members(self, gid):
        return self.local[gid]["members"] if gid in self.local else self.w.p[gid]["members"]

    def group_members(self, gid):
        out = []
        for m in sorted(self._members(gid)):
            if m in self.local:
                out.append(("Groups", m, self.local[m]["name"]))
            else:
                out.append((_TYPE[self.w.p[m]["kind"]], m, self.w.p[m]["name"]))
        return out

    def group_detail(self, gid):
        counts = {"users": 0, "service_principals": 0, "groups": 0, "other": 0}
        for t, _, _ in self.group_members(gid):
            counts[{"Users": "users", "ServicePrincipals": "service_principals", "Groups": "groups"}[t]] += 1
        return sorted(self.ents.get(gid, set())), counts

    def get_entitlements(self, kind, oid):
        return set(self.ents.get(oid, set()))

    def add_entitlements(self, kind, oid, values):
        self.writes.append(("ents", oid, set(values)))
        self.ents.setdefault(oid, set()).update(values)

    def user_name(self, uid):
        return self.w.p[uid]["name"]

    def find_user_id(self, name):
        hits = [pid for pid in self.assigned if self.w.p[pid]["kind"] == "user" and self.w.p[pid]["name"] == name]
        return hits[0] if hits else ""

    def is_directly_assigned(self, pid):
        return pid in self.assigned or pid in self.hidden_direct

    def assign_to_workspace(self, pid):
        self.writes.append(("assign", pid))
        if pid in self.fail_assign:
            raise NB["ApiError"]("target", "PUT", f"/permissionassignments/principals/{pid}", 403,
                                 json.dumps({"message": "not allowed"}))
        self.pending[pid] = self.visible_after

def _bed(**tgt_kw):
    """Source has: account group G1 (2 SP members), account SP S1, reader SP R, twin SP T (target has another
    SP with the same name), local group L (members: SP S1, user U1, user U2, group G1). Target has: SP S2
    (shared), the other twin T2, user U1 only."""
    w = World()
    s1, s2 = w.add("101", "sp", "s1", "app-s1"), w.add("102", "sp", "s2", "app-s2")
    r, t1, t2 = w.add("103", "sp", "reader", "app-r"), w.add("104", "sp", "twin", "app-t1"), w.add("105", "sp", "twin", "app-t2")
    u1, u2 = w.add("201", "user", "u1@x.com"), w.add("202", "user", "u2@x.com")
    g1 = w.add("301", "group", "acct_grp", members={s1, s2})
    src = Ws(w, assigned={s1, s2, r, t1, u1, u2, g1}, local_groups={"401": ("local_grp", {s1, u1, u2, g1})},
             ents={g1: {"workspace-access"}, s1: {"databricks-sql-access"}, "401": {"workspace-consume"}})
    tgt = Ws(w, assigned={s2, t2, u1}, **tgt_kw)
    return src, tgt


def _plan(src, tgt):
    inv = NB["read_inventory"](src, tgt, [G, SP], **QUIET)
    items = [(k, key) for k in (G, SP) for key in sorted(inv[k][0])]
    return inv, NB["plan_missing"](inv, items, reader_app_id="app-r")


def test_plan_assigns_account_skips_local_reader_and_twin():
    src, tgt = _bed()
    _, plan = _plan(src, tgt)
    got = {(p["kind"], p["key"]): p["action"] for p in plan}
    assert got == {(G, "acct_grp"): "assign", (G, "local_grp"): "skip", (SP, "app-s1"): "assign",
                   (SP, "app-r"): "skip", (SP, "app-t1"): "skip"}, "app-s2 is on both, so not planned"
    twin = next(p for p in plan if p["key"] == "app-t1")
    assert "app-t2" in twin["detail"]
    local = next(p for p in plan if p["key"] == "local_grp")
    assert "recreate it manually" in local["detail"]


def test_bring_missing_dry_run_writes_nothing():
    src, tgt = _bed()
    _, plan = _plan(src, tgt)
    rows = NB["bring_missing"](plan, src, tgt, dry_run=True, **QUIET)
    assert {r["action"] for r in rows} == {"WOULD_ASSIGN", "SKIPPED"}
    assert tgt.writes == []


def test_not_visible_after_waiting_is_a_failure():
    src, tgt = _bed(visible_after=10 ** 6)
    _, plan = _plan(src, tgt)
    rows = NB["bring_missing"](plan, src, tgt, dry_run=False, **QUIET)
    bad = [r for r in rows if r["action"] == "NOT_VISIBLE"]
    assert {r["name"] for r in bad} == {"acct_grp", "s1 (app-s1)"} and "NOT_VISIBLE" in NB["BAD_STATUSES"]


def test_assign_error_carries_server_message_and_others_continue():
    src, tgt = _bed(fail_assign={"301"})
    _, plan = _plan(src, tgt)
    rows = {r["name"]: r for r in NB["bring_missing"](plan, src, tgt, dry_run=False, **QUIET)}
    assert rows["acct_grp"]["action"] == "ERROR" and "HTTP 403" in rows["acct_grp"]["detail"] \
        and "not allowed" in rows["acct_grp"]["detail"]
    assert rows["s1 (app-s1)"]["action"] == "ASSIGNED"


def test_after_bring_over_the_entitlement_sync_covers_the_new_items():
    src, tgt = _bed()
    _, plan = _plan(src, tgt)
    NB["bring_missing"](plan, src, tgt, dry_run=False, **QUIET)
    _, rows = NB["run_sync"](src, tgt, [G, SP], "", False, **QUIET)
    got = {r["name"]: r for r in rows}
    assert got["acct_grp"]["status"] == "ADDED" and got["acct_grp"]["added"] == "workspace-access"
    assert got["s1 (app-s1)"]["status"] == "ADDED"
    assert "local_grp" not in got, "workspace-local groups are not brought over"


def test_dry_run_sync_shows_pending_items_as_would_add_once():
    src, tgt = _bed()
    inv, plan = _plan(src, tgt)
    pending = frozenset((p["kind"], p["key"]) for p in plan if p["action"] in ("assign", "create"))
    _, rows = NB["run_sync"](src, tgt, [G, SP], "acct_grp", True, inv=inv, pending=pending & {(G, "acct_grp")}, **QUIET)
    assert len(rows) == 1 and rows[0]["status"] == "WOULD_ADD" and rows[0]["added"] == "workspace-access"
    assert tgt.writes == []


def test_difference_report_marks_planned_items():
    src, tgt = _bed()
    inv, plan = _plan(src, tgt)
    planned = {(p["kind"], p["key"]): "WOULD BE ASSIGNED" for p in plan if p["action"] == "assign"}
    diffs, _ = NB["find_differences"](src, tgt, inv, planned=planned, **QUIET)
    g = next(d for d in diffs if d["name"] == "acct_grp" and d["side"] == "source_only")
    assert g["note"].startswith("WOULD BE ASSIGNED")


# ── direct vs via-group presence (live finding 2026-11-18) ──────────────────

def _via_group_bed(**tgt_kw):
    """SP v is DIRECT on the source but on the target only via an assigned group (like ai27_entsync_sp_srconly).
    SP m is only via a group on the SOURCE and absent from the target."""
    w = World()
    v, m = w.add("501", "sp", "v", "app-v"), w.add("502", "sp", "m", "app-m")
    g = w.add("601", "group", "grp", members={v})
    src = Ws(w, assigned={v, g}, via_group={m}, ents={v: {"workspace-access"}})
    tgt = Ws(w, assigned={g}, via_group={v}, **tgt_kw)
    return src, tgt


def test_inventory_marks_direct_vs_via_group():
    src, tgt = _via_group_bed()
    inv = NB["read_inventory"](src, tgt, [SP], **QUIET)
    s, t = inv[SP]
    assert s["app-v"][0]["direct"] is True and t["app-v"][0]["direct"] is False
    assert s["app-m"][0]["direct"] is False


def test_direct_on_source_but_via_group_on_target_is_assigned_directly():
    src, tgt = _via_group_bed()
    inv = NB["read_inventory"](src, tgt, [G, SP], **QUIET)
    plan = NB["plan_missing"](inv, [(SP, "app-v"), (SP, "app-m"), (G, "grp")])
    assert [(p["key"], p["action"]) for p in plan] == [("app-v", "assign")], \
        "grp is direct on both; m is only via a group on the source, so it comes with its group"
    assert "only via group membership" in plan[0]["detail"]
    rows = NB["bring_missing"](plan, src, tgt, dry_run=False, **QUIET)
    assert rows[0]["action"] == "ASSIGNED" and "501" in tgt.assigned


def test_via_group_assignment_that_never_becomes_direct_is_not_visible():
    src, tgt = _via_group_bed(visible_after=10 ** 6)
    inv = NB["read_inventory"](src, tgt, [SP], **QUIET)
    plan = NB["plan_missing"](inv, [(SP, "app-v")])
    rows = NB["bring_missing"](plan, src, tgt, dry_run=False, **QUIET)
    assert rows[0]["action"] == "NOT_VISIBLE", "listed via the group is NOT enough: it must be directly assigned"


def test_difference_report_compares_direct_assignments_and_says_via_group():
    src, tgt = _via_group_bed()
    inv = NB["read_inventory"](src, tgt, [G, SP], **QUIET)
    diffs, _ = NB["find_differences"](src, tgt, inv, **QUIET)
    got = {(d["side"], d["name"]): d for d in diffs}
    assert set(got) == {("source_only", "v")}, "m is via-group only on the source, so it is not a migration gap"
    assert "on the target only via group membership" in got[("source_only", "v")]["note"]



def test_bring_missing_live_assigns_and_waits_for_direct_assignment():
    src, tgt = _bed(visible_after=2)
    _, plan = _plan(src, tgt)
    rows = {r["name"]: r for r in NB["bring_missing"](plan, src, tgt, dry_run=False, **QUIET)}
    assert rows["acct_grp"]["action"] == "ASSIGNED" and rows["s1 (app-s1)"]["action"] == "ASSIGNED"
    assert rows["local_grp"]["action"] == "SKIPPED"
    assert sorted(w for w in tgt.writes if w[0] == "assign") == [("assign", "101"), ("assign", "301")], \
        "never the reader (103), the twin (104) or anything for the workspace-local group"


def test_guard_never_overwrites_an_existing_direct_assignment():
    """If the bulk direct-assignment list ever missed a principal, the per-principal check must stop the PUT
    (a PUT USER could otherwise downgrade an ADMIN assignment)."""
    src, tgt = _bed()
    _, plan = _plan(src, tgt)
    tgt.hidden_direct.add("101")
    for dry in (True, False):
        rows = {r["name"]: r for r in NB["bring_missing"](plan, src, tgt, dry_run=dry, **QUIET)}
        assert rows["s1 (app-s1)"]["action"] == "SKIPPED" and "left unchanged" in rows["s1 (app-s1)"]["detail"]
    assert ("assign", "101") not in tgt.writes


def test_is_directly_assigned_maps_200_and_404_and_raises_other_errors():
    api = NB["Api"]("target", "https://x", lambda: "t")

    def answer(status):
        def call(*a, **k):
            if status == 200:
                return {"name": "workspace-assignments/1"}
            raise NB["ApiError"]("target", "GET", "/x", status, json.dumps({"error_code": "E", "message": "m"}))
        return call
    api.call = answer(200)
    assert api.is_directly_assigned("1") is True
    api.call = answer(404)
    assert api.is_directly_assigned("1") is False
    api.call = answer(403)
    with pytest.raises(NB["ApiError"]):
        api.is_directly_assigned("1")



def test_via_group_sp_is_not_reported_as_its_own_twin():
    src, tgt = _via_group_bed()
    inv = NB["read_inventory"](src, tgt, [SP], **QUIET)
    diffs, _ = NB["find_differences"](src, tgt, inv, **QUIET)
    note = next(d for d in diffs if d["name"] == "v")["note"]
    assert "SAME NAME" not in note and "only via group membership" in note
