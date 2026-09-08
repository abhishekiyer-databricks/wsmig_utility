"""Offline unit tests for the Export engine (Plan 2).

Run: python3 -m tests.test_export   (from the repo root; no Databricks/network needed).
"""
from __future__ import annotations

from src.config.config_manager import Config
from src.exporters import bundle_paths as BP
from tests.fakes import FakeClient


def _cfg(**over):
    d = {"role": "source", "source_workspace_id": "1", "run_id": "r",
         "source_staging_location": "/Volumes/a/b/c"}
    d.update(over)
    return Config.from_dict(d)


# ─────────────────────────── transforms ────────────────────────────────────

def test_strip_and_fingerprint_stable():
    from src.transform.transforms import strip_runtime, fingerprint, normalize
    cluster = {"cluster_name": "cl", "cluster_id": "c-123", "state": "RUNNING",
               "start_time": 111, "last_activity_time": 222, "spark_version": "13.x",
               "node_type_id": "Standard_DS3_v2", "autoscale": {"min_workers": 1, "max_workers": 4}}
    stripped = strip_runtime("cluster", cluster)
    # runtime fields gone; create-config kept.
    assert "cluster_id" not in stripped and "state" not in stripped
    assert "start_time" not in stripped and "last_activity_time" not in stripped
    assert stripped["cluster_name"] == "cl" and stripped["spark_version"] == "13.x"
    assert stripped["autoscale"] == {"min_workers": 1, "max_workers": 4}
    # fingerprint is deterministic + insensitive to the stripped runtime churn.
    churned = dict(cluster, state="TERMINATED", start_time=999, cluster_id="c-999")
    assert fingerprint(strip_runtime("cluster", cluster)) == fingerprint(strip_runtime("cluster", churned))
    # a real content change flips it.
    changed = dict(cluster, node_type_id="Standard_DS4_v2")
    assert fingerprint(strip_runtime("cluster", cluster)) != fingerprint(strip_runtime("cluster", changed))
    assert fingerprint({}).startswith("sha256:")
    assert normalize({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_strip_glob_patterns():
    from src.transform.transforms import strip_runtime
    c = {"cluster_name": "x", "terminated_time": 1, "start_time": 2, "creator_by_user": "u",
         "last_activity_by_user_name": "u2", "keep_me": True}
    s = strip_runtime("cluster", c)
    assert "terminated_time" not in s and "start_time" not in s
    assert "creator_by_user" not in s and "last_activity_by_user_name" not in s
    assert s["keep_me"] is True and s["cluster_name"] == "x"


def test_fingerprint_stable_under_list_reordering():
    """A re-export of an UNCHANGED asset must yield the SAME fingerprint even when the source
    API returns list members in a different order.

    Regression: SCIM returns group `members` in a random order on each GET (verified live on
    fvm1 — 3 GETs, same 9 members, 3 orders). Hashing raw order re-fingerprinted an unchanged
    group every run, so the target's cross-run UPSERT would have "updated" it forever.
    """
    from src.transform.transforms import fingerprint
    g1 = {"displayName": "users",
          "members": [{"value": "1", "display": "a"}, {"value": "2", "display": "b"},
                      {"value": "3", "display": "c"}],
          "entitlements": [{"value": "workspace-access"}, {"value": "allow-cluster-create"}]}
    g2 = {"displayName": "users",
          "members": [{"value": "3", "display": "c"}, {"value": "1", "display": "a"},
                      {"value": "2", "display": "b"}],
          "entitlements": [{"value": "allow-cluster-create"}, {"value": "workspace-access"}]}
    assert fingerprint(g1) == fingerprint(g2)
    # a REAL membership change must still change the fingerprint
    g3 = {**g1, "members": g1["members"][:2]}
    assert fingerprint(g3) != fingerprint(g1)
    # nested reordering (task lists inside a job payload) is handled too
    j1 = {"name": "j", "tasks": [{"task_key": "a"}, {"task_key": "b"}]}
    j2 = {"name": "j", "tasks": [{"task_key": "b"}, {"task_key": "a"}]}
    assert fingerprint(j1) == fingerprint(j2)


def test_dbfs_library_refs_flagged_manual():
    """A library whose artifact lives on DBFS exports as a DANGLING reference (DBFS content is
    out of scope), so it must be `manual` — never a `success` that silently breaks the target."""
    from src.exporters.asset_export import build_all
    misc = [
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:jar",
         "library": {"jar": "dbfs:/FileStore/x.jar"}},
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:whl",
         "library": {"whl": "/dbfs/FileStore/y.whl"}},
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:pypi",
         "library": {"pypi": {"package": "tabulate==0.9.0"}}},
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:maven",
         "library": {"maven": {"coordinates": "g:a:1"}}},
        # a UC Volumes / workspace-files artifact DOES migrate → must stay auto
        {"misc_type": "cluster_library", "cluster_id": "c1", "_natural_key": "c1:vol",
         "library": {"jar": "/Volumes/cat/sch/vol/z.jar"}},
    ]
    by = {m["natural_key"]: m for m in build_all({"misc": misc})["cluster_library"]}
    assert by["c1:jar"]["migration_mode"] == "manual" and "DBFS" in by["c1:jar"]["note"]
    assert by["c1:whl"]["migration_mode"] == "manual"
    assert by["c1:pypi"]["migration_mode"] == "auto"
    assert by["c1:maven"]["migration_mode"] == "auto"
    assert by["c1:vol"]["migration_mode"] == "auto"
    # a job task pulling a dbfs library still migrates, but must SAY so
    jobs = [{"name": "j", "job_id": "1", "deployed_by_dab": False,
             "settings": {"name": "j", "tasks": [
                 {"task_key": "t", "libraries": [{"jar": "dbfs:/FileStore/q.jar"}]}]}}]
    ju = build_all({"job": jobs})["job"][0]
    assert ju["migration_mode"] == "auto" and "DBFS" in ju["note"]


def test_dab_owned_pathless_assets_are_not_recreated():
    """DAB can own assets with no workspace path (cluster/pool/warehouse/scope/serving/genie).
    Each must export as `dab` with NO payload, so import can't duplicate a bundle-owned asset."""
    from src.exporters.asset_export import build_all
    units = build_all({
        "compute": [
            {"compute_type": "cluster", "_natural_key": "c", "cluster_id": "1",
             "deployed_by_dab": True, "_raw": {"cluster_name": "c"}},
            {"compute_type": "instance_pool", "_natural_key": "p", "instance_pool_id": "2",
             "deployed_by_dab": True, "_raw": {"instance_pool_name": "p"}},
        ],
        "sql": [{"sql_type": "warehouse", "_natural_key": "w", "id": "3",
                 "deployed_by_dab": True, "_raw": {"name": "w"}}],
        "secret_scope": [{"name": "s", "deployed_by_dab": True, "key_names": ["k"],
                          "backend_type": "DATABRICKS"}],
        "serving_endpoint": [{"name": "e", "deployed_by_dab": True, "migratable": True,
                              "config": {"served_entities": []}}],
        "genie_space": [{"title": "g", "space_id": "4", "deployed_by_dab": True,
                         "serialized_space": "{}", "has_serialized_space": True}],
    })
    for at in ("cluster", "instance_pool", "sql_warehouse", "secret_scope",
               "serving_endpoint", "genie_space"):
        u = units[at][0]
        assert u["export_status"] == "dab", f"{at} should be dab, got {u['export_status']}"
        assert u["payload"] == {}, f"{at} must carry no create payload"
        # `dab` on its own reads as "not exported" — the paired action is what tells the operator
        # the bundle redeploy recreates it and import deliberately skips it.
        assert u["import_action"] == "dab_redeploy", \
            f"{at} action should be dab_redeploy, got {u['import_action']}"
    # the scope's VALUES are still a manual action even though DAB redeploys the scope
    assert units["secret_value"][0]["migration_mode"] == "manual"


def test_legacy_sql_alert_is_manual_at_export_and_never_attempted():
    """IMP-5: legacy SQL alerts (like legacy dashboards) are MANUAL at export — the v1 create API
    is obsolete, so attempting it produced permanent red. It must carry NO payload, action=manual,
    and a note steering the operator to rebuild as Alerts V2. Alerts V2 still migrate as `auto`."""
    from src.exporters.asset_export import build_all
    units = build_all({"sql": [
        {"sql_type": "legacy_alert", "_natural_key": "old_alert", "id": "a1",
         "_raw": {"name": "old_alert", "condition": {"op": ">"}}},
        {"sql_type": "alert", "_natural_key": "v2_alert", "id": "a2",
         "_raw": {"display_name": "v2_alert"}},
    ]})
    la = units["legacy_alert"][0]
    assert la["migration_mode"] == "manual" and la["import_action"] == "manual"
    assert la["payload"] == {}, "a manual legacy alert must carry no create payload"
    assert "Alerts V2" in la["note"] and "obsolete" in la["note"].lower()
    # Alerts V2 are unaffected — still auto-migratable.
    assert units["alert_v2"][0]["migration_mode"] == "auto"


def test_identity_import_action_distinguishes_automatic_from_prerequisite():
    """`import_action` must say who does the work, and users/SPNs vs account GROUPS differ.

    `adopt_or_assign` = the utility handles it with no human step (the workspace SCIM POST creates
    at the account and assigns; an SPN POST carrying applicationId adopts the existing account SPN).
    `assign_on_target` = an account GROUP that must ALREADY exist in the target account, because the
    utility must not create it (that makes a workspace-local shadow which permanently blocks the
    real group). Collapsing both into one "must pre-exist" label sent operators chasing
    account-admin work for every user and SPN that is in fact automatic.
    """
    from src.exporters.asset_export import build_all
    ids = [
        {"identity_type": "user", "userName": "a@x.com", "id": "1",
         "kind": "account", "_raw": {"userName": "a@x.com"}},
        {"identity_type": "user", "userName": "b@x.com", "id": "2",
         "kind": "needs_review", "_raw": {"userName": "b@x.com"}},
        {"identity_type": "service_principal", "applicationId": "app1", "id": "3",
         "kind": "account", "_raw": {"applicationId": "app1"}},
        {"identity_type": "group", "displayName": "acct-grp", "id": "5",
         "kind": "account", "resource_type": "Group", "_raw": {"displayName": "acct-grp"}},
        {"identity_type": "group", "displayName": "ws-grp", "id": "6",
         "kind": "workspace_local", "resource_type": "WorkspaceGroup",
         "_raw": {"displayName": "ws-grp"}},
    ]
    u = build_all({"identity": ids})
    users = {x["natural_key"]: x for x in u["user"]}
    sps = {x["natural_key"]: x for x in u["service_principal"]}
    groups = {x["natural_key"]: x for x in u["group"]}
    # everything was exported fine...
    assert all(x["export_status"] == "success" for x in list(users.values()) + list(sps.values()))
    # ...but the target action differs, and AUTOMATIC must not read as a prerequisite
    assert users["a@x.com"]["import_action"] == "adopt_or_assign"
    assert users["b@x.com"]["import_action"] == "review_required"
    assert sps["app1"]["import_action"] == "adopt_or_assign"
    assert groups["ws-grp"]["import_action"] == "create"
    assert groups["acct-grp"]["import_action"] == "assign_on_target", \
        "an account GROUP is the ONE identity that can still need an account admin"


def test_every_unit_has_a_known_import_action():
    """The Import Action column is on EVERY sheet, so every unit must carry a valid action.

    Previously only identity units got one; every other tab left the operator inferring intent
    from the export status, which is how "DAB" got read as "not exported". A blank or unknown
    action would render "—" and put us back there.
    """
    from src.exporters.asset_export import IMPORT_ACTIONS, build_all
    units_by_type = build_all(_sample_inventory())
    assert units_by_type, "sample inventory produced no units"
    for asset_type, units in units_by_type.items():
        for u in units:
            act = u.get("import_action")
            assert act, f"{asset_type}/{u['natural_key']} has no import_action"
            assert act in IMPORT_ACTIONS, \
                f"{asset_type}/{u['natural_key']} has unknown action {act!r} (would render '—')"
    # spot-check the non-`create` verbs, which are the ones a refactor is likeliest to drop
    by = {at: units_by_type.get(at, []) for at in
          ("notebook", "workspace_file", "cluster_library", "workspace_conf", "secret_value")}
    assert all(u["import_action"] == "create_and_upload" for u in by["notebook"] + by["workspace_file"])
    assert all(u["import_action"] == "install" for u in by["cluster_library"])
    assert all(u["import_action"] == "set_conf" for u in by["workspace_conf"])
    assert all(u["import_action"] == "manual" for u in by["secret_value"])


def test_import_action_follows_a_late_status_change():
    """Toggles and the content pass change export_status AFTER build_all stamped the action.

    A toggled-off or oversize unit must not still advertise "CREATE on target" — the runner
    re-derives the action once statuses are final.
    """
    from src.exporters.asset_export import derive_import_action
    u = {"asset_type": "notebook", "natural_key": "/n", "migration_mode": "content",
         "export_status": "success"}
    assert derive_import_action(u) == "create_and_upload"
    # oversize: recorded, but the bytes never reached the bundle → a human must copy it
    u["export_status"] = "skipped_oversize"
    assert derive_import_action(u) == "manual"
    # toggled off / failed: there is nothing for import to do at all
    for st in ("skip", "failure"):
        u["export_status"] = st
        assert derive_import_action(u) == "none", f"status {st} should yield no action"


# ─────────────────────────── asset_export ──────────────────────────────────

def _sample_inventory():
    return {
        "identity": [
            {"identity_type": "user", "id": "u1", "userName": "alice@corp.com",
             "classification": "entra_user", "_raw": {"id": "u1", "userName": "alice@corp.com"}},
            # `_raw` must carry applicationId: the report adapter reads the SP display row from
            # `_raw`, and the workbook joins Export Status / Import Action on the natural key
            # (= applicationId). Without it both columns rendered "—" for this row.
            {"identity_type": "service_principal", "id": "s1", "applicationId": "app-dbx",
             "classification": "db_managed_sp", "has_secrets": True,
             "_raw": {"id": "s1", "applicationId": "app-dbx", "displayName": "sp-one"}},
            {"identity_type": "group", "id": "g1", "displayName": "eng",
             "classification": "db_managed_group", "_raw": {"id": "g1", "displayName": "eng"}},
            {"identity_type": "group", "id": "g2", "displayName": "admins",
             "classification": "builtin_group", "_raw": {"id": "g2", "displayName": "admins"}},
        ],
        "compute": [
            {"compute_type": "cluster", "cluster_id": "c1", "_natural_key": "cl",
             "acl": [], "_raw": {"cluster_name": "cl", "cluster_id": "c1", "state": "RUNNING",
                                 "node_type_id": "n1"}},
            {"compute_type": "cluster_policy", "policy_id": "p1", "_natural_key": "pol",
             "_raw": {"name": "pol", "policy_id": "p1", "definition": "{}"}},
        ],
        "workspace_object": [
            {"object_type": "DIRECTORY", "path": "/Shared", "object_id": "1"},
            {"object_type": "NOTEBOOK", "path": "/Users/alice@corp.com/nb", "object_id": "10",
             "language": "PYTHON"},
            {"object_type": "FILE", "path": "/Users/bob@corp.com/data.csv", "object_id": "12"},
            {"object_type": "REPO", "path": "/Repos/x/r", "repo_id": "20",
             "_raw": {"path": "/Repos/x/r", "url": "https://g/r", "provider": "gitHub",
                      "branch": "main", "head_commit_id": "abc"}},
        ],
        "secret_scope": [
            {"name": "kv", "backend_type": "AZURE_KEYVAULT",
             "keyvault_metadata": {"dns_name": "d"}, "key_names": ["k1", "k2"],
             "acls": [{"principal": "eng", "permission": "READ"}], "_raw": {"name": "kv"}},
        ],
        "job": [
            {"job_id": "j1", "name": "nightly", "deployed_by_dab": False,
             "settings": {"name": "nightly", "tasks": [{"task_key": "t"}]}, "acl": [], "_raw": {}},
            {"job_id": "j2", "name": "bundle-job", "deployed_by_dab": True,
             "settings": {"name": "bundle-job", "deployment": {"kind": "BUNDLE"}}, "_raw": {}},
        ],
        "sql": [
            {"sql_type": "warehouse", "id": "w1", "_natural_key": "wh",
             "_raw": {"name": "wh", "id": "w1", "state": "RUNNING", "warehouse_type": "PRO"}},
            {"sql_type": "legacy_query", "id": "q1", "_natural_key": "q",
             "_raw": {"display_name": "q", "id": "q1", "query": "select 1"}},
        ],
        "dlt_pipeline": [
            {"pipeline_id": "dp1", "name": "bronze", "deployed_by_dab": False,
             "spec": {"name": "bronze", "target": "db"}, "acl": []},
        ],
        "lakeview_dashboard": [
            {"dashboard_id": "d1", "display_name": "KPIs", "deployed_by_dab": False,
             "warehouse_id": "w1", "serialized_dashboard": "{}", "acl": [],
             "parent_path": "/Users/alice@corp.com"},
        ],
        "genie_space": [
            {"space_id": "gs1", "title": "Genie", "warehouse_id": "w1", "acl": [],
             "serialized_space": '{"version":2}', "parent_path": "/Users/alice@corp.com"},
            {"space_id": "gs2", "title": "NoSer", "warehouse_id": "w1", "acl": []},  # no payload
        ],
        "serving_endpoint": [
            {"name": "ext-ep", "migratable": True, "migration_note": "external",
             "config": {"served_entities": [{"external_model": {"name": "gpt"}}]}, "acl": []},
            {"name": "uc-ep", "migratable": False, "migration_note": "UC model",
             "config": {"served_entities": [{"entity_name": "cat.sch.model"}]}, "acl": []},
        ],
        "misc": [
            {"misc_type": "global_init_script", "script_id": "gi1", "_natural_key": "gis",
             "name": "gis", "position": 1, "enabled": True, "script_b64": "ZWNobw=="},
            {"misc_type": "workspace_conf", "key": "enableTokensConfig", "value": "true"},
        ],
        "app": [{"name": "my-app"}],
        "lakebase_project": [{"name": "lb1"}],
    }


def test_build_all_asset_types_and_modes():
    from src.exporters.asset_export import build_all
    ubt = build_all(_sample_inventory())
    # every fine-grained asset_type present.
    assert set(ubt) >= {"user", "service_principal", "group", "cluster", "cluster_policy",
                        "directory", "notebook", "workspace_file", "repo", "secret_scope",
                        "secret_value", "job", "sql_warehouse", "legacy_query", "dlt_pipeline",
                        "lakeview_dashboard", "genie_space", "serving_endpoint",
                        "global_init_script", "workspace_conf", "app", "lakebase_project"}
    # SP with secrets → note; db-managed group → auto.
    sp = ubt["service_principal"][0]
    assert sp["migration_mode"] == "auto" and "secret" in sp["note"].lower()
    groups = {g["natural_key"]: g for g in ubt["group"]}
    assert groups["eng"]["migration_mode"] == "auto"
    # A BUILT-IN group is never recreated (it already exists on target) → `covered`, but its
    # MEMBERSHIP is exported as its own unit so source admins actually become target admins.
    assert groups["admins"]["migration_mode"] == "covered"
    memberships = {m["natural_key"]: m for m in ubt["group_membership"]}
    assert memberships["admins"]["migration_mode"] == "auto"
    assert "members" in memberships["admins"]["payload"]
    # ...and only for built-ins: a db-managed group carries its members in the group unit itself.
    assert "eng" not in memberships
    # notebook/file are content mode with owner captured.
    nb = ubt["notebook"][0]
    assert nb["migration_mode"] == "content" and nb["content_ref"] is None
    assert nb["owner"] == "alice@corp.com"
    assert ubt["workspace_file"][0]["owner"] == "bob@corp.com"
    # DAB job → dab, no payload; normal job → auto with settings payload.
    jobs = {j["natural_key"]: j for j in ubt["job"]}
    assert jobs["bundle-job"]["migration_mode"] == "dab" and jobs["bundle-job"]["payload"] == {}
    assert jobs["nightly"]["migration_mode"] == "auto" and jobs["nightly"]["payload"]["tasks"]
    # secret_value units: one per key, manual.
    sv = ubt["secret_value"]
    assert {u["natural_key"] for u in sv} == {"kv/k1", "kv/k2"}
    assert all(u["migration_mode"] == "manual" and u["migratable"] is False for u in sv)
    # serving: external migratable auto, UC-backed manual.
    serv = {s["natural_key"]: s for s in ubt["serving_endpoint"]}
    assert serv["ext-ep"]["migration_mode"] == "auto" and serv["ext-ep"]["migratable"] is True
    assert serv["uc-ep"]["migration_mode"] == "manual" and serv["uc-ep"]["migratable"] is False
    # genie: auto when serialized_space present, manual when absent. PLAN 11 Finding-9: the natural
    # key is the FULL PATH (`<parent_path>/<title>`), so the space with a folder keys on it; a space
    # with NO parent_path falls back to the bare title.
    genie = {g["natural_key"]: g for g in ubt["genie_space"]}
    assert genie["/Users/alice@corp.com/Genie"]["migration_mode"] == "auto"
    assert genie["/Users/alice@corp.com/Genie"]["payload"]["serialized_space"] == '{"version":2}'
    # PLAN 8 Bug 7 (Lakeview/Genie siblings): the exported payload carries parent_path so the object
    # is recreated in its SOURCE folder, not the API default.
    assert ubt["lakeview_dashboard"][0]["payload"]["parent_path"] == "/Users/alice@corp.com"
    assert genie["/Users/alice@corp.com/Genie"]["payload"]["parent_path"] == "/Users/alice@corp.com"
    assert genie["NoSer"]["migration_mode"] == "manual" and genie["NoSer"]["migratable"] is False
    # apps/lakebase manual.
    assert ubt["app"][0]["migration_mode"] == "manual"
    # warehouse payload stripped of runtime state.
    assert "state" not in ubt["sql_warehouse"][0]["payload"]


# ─────────────────────────── acl_writer ────────────────────────────────────

def test_workspace_other_object_types_dedup_and_not_dropped():
    """The walk returns DASHBOARD/ALERT/MLFLOW_EXPERIMENT objects. Dashboards/alerts that match a
    NATIVE unit (by ASSET ID) are `covered` (counted once, not re-exported); unmatched ones stay
    manual; MLflow is out of scope. Nothing is silently dropped (1:1 reconciliation).

    Matching is by id, not path, and this fixture reflects the live API shapes that forced it:
      * the ALERT twin's native record has `parent_path: None` (fvm1: null even on a detail GET),
        so a path-keyed match left it `manual` — a real dashboard/alert reported as needing manual
        work when it was in fact fully exported;
      * `/a/orphan.lvdash.json` sits in the SAME DIRECTORY as the matched dashboard. The old code
        also keyed on the native record's `parent_path` (a directory), so this unrelated dashboard
        was wrongly marked `covered` — the dangerous direction, since `covered` asserts "already
        exported" and would hide a genuine export gap.
    """
    from src.exporters.asset_export import build_all
    obt = {
        "workspace_object": [
            {"object_type": "NOTEBOOK", "path": "/a/nb", "object_id": "1", "language": "PYTHON"},
            # a DASHBOARD's resource_id IS the Lakeview dashboard_id
            {"object_type": "DASHBOARD", "path": "/a/kpis.lvdash.json", "object_id": "2",
             "resource_id": "d1"},                                                        # twin
            {"object_type": "DASHBOARD", "path": "/a/orphan.lvdash.json", "object_id": "5",
             "resource_id": "d-unknown"},                                     # no native match
            # an ALERT's object_id is the Alerts V2 id (its resource_id is an unrelated uuid)
            {"object_type": "ALERT", "path": "/a/al.dbalert.json", "object_id": "a1",
             "resource_id": "3247336e-uuid-not-the-alert-id"},                            # twin
            {"object_type": "MLFLOW_EXPERIMENT", "path": "/a/exp", "object_id": "4"},
        ],
        "lakeview_dashboard": [
            {"dashboard_id": "d1", "display_name": "KPIs", "path": "/a/kpis.lvdash.json",
             "parent_path": "/a", "warehouse_id": "w", "serialized_dashboard": "{}",
             "deployed_by_dab": False}],
        # live shape: Alerts V2 exposes NO workspace path at all
        "sql": [{"sql_type": "alert", "id": "a1", "_natural_key": "al", "parent_path": None,
                 "_raw": {"parent_path": None}, "deployed_by_dab": False}],
    }
    ubt = build_all(obt)
    covered = {u["natural_key"]: u for u in ubt["lakeview_dashboard_file"]}
    assert covered["/a/kpis.lvdash.json"]["export_status"] == "covered"
    # same folder as the matched dashboard, but a different asset → must NOT be claimed covered
    assert covered["/a/orphan.lvdash.json"]["export_status"] == "manual"
    # pathless alert still dedupes, via object_id
    assert ubt["alert_v2_file"][0]["export_status"] == "covered"
    # MLflow out of scope, never fetched.
    assert ubt["mlflow_experiment"][0]["migratable"] is False
    assert ubt["mlflow_experiment"][0]["content_ref"] is None
    # nothing dropped: 1 nb + 2 dashboard-file + 1 alert-file + 1 mlflow + native(1 dash +1 alert)
    total = sum(len(v) for v in ubt.values())
    assert total == 7


def test_collect_acls_keys_and_counts():
    from src.exporters.acl_writer import collect_acls, acl_counts
    obt = {
        "compute": [{"compute_type": "cluster", "cluster_id": "c1", "_natural_key": "cl",
                     "acl": [{"user_name": "a@x.com",
                              "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
                             {"group_name": "eng",
                              "all_permissions": [{"permission_level": "CAN_ATTACH_TO",
                                                   "inherited": True}]}]}],
        "secret_scope": [{"name": "kv", "acls": [{"principal": "eng", "permission": "READ"}]}],
        "job": [{"name": "j", "job_id": "j1", "acl": []}],   # empty → not emitted
    }
    acls = collect_acls(obt)
    keys = {(e["asset_type"], e["natural_key"]) for e in acls}
    assert ("cluster", "cl") in keys and ("secret_scope", "kv") in keys
    assert ("job", "j") not in keys   # empty ACL not emitted
    cl = next(e for e in acls if e["asset_type"] == "cluster")
    assert cl["perm_object_type"] == "clusters"
    principals = {(g["principal"], g["principal_type"]) for g in cl["grants"]}
    assert ("a@x.com", "user") in principals and ("eng", "group") in principals
    counts = acl_counts(acls)
    assert counts[("cluster", "cl")] == 2 and counts[("secret_scope", "kv")] == 1


# ─────────────────────────── parallel ──────────────────────────────────────

def test_parallel_map_failsoft_and_complete():
    from src.exporters.parallel import parallel_map, Locked
    def fn(x):
        if x == 3:
            raise ValueError("boom")
        return x * 2
    results = {item: (res, err) for item, res, err in parallel_map(range(6), fn, max_workers=4)}
    assert results[2] == (4, None)
    assert results[3][0] is None and isinstance(results[3][1], ValueError)
    assert len(results) == 6
    # Locked guards shared state.
    box = Locked({"n": 0})
    for item, _r, _e in parallel_map(range(10), lambda i: i, max_workers=1):
        with box as s:
            s["n"] += 1
    assert box.value["n"] == 10


def test_parallel_map_streams_incrementally():
    """parallel_map must YIELD as results complete, not return one list at the end — the content
    pass relies on that to checkpoint mid-flight. Guards against a revert to an eager list."""
    import threading
    from src.exporters.parallel import parallel_map

    # A generator does no work until first iteration.
    started = threading.Event()
    gen = parallel_map(range(4), lambda i: started.set() or i, max_workers=2)
    assert not started.is_set(), "parallel_map ran work at call time — it must be lazy"

    # Consuming ONE item must not require all items to have finished. Item 0 blocks until
    # released, so if we can pull a result before releasing it, results are genuinely streamed.
    release = threading.Event()

    def fn(i):
        if i == 0:
            release.wait(timeout=5)
        return i

    got = []
    for item, res, err in parallel_map(range(6), fn, max_workers=4):
        got.append(res)
        if len(got) == 1:
            # We have a result while item 0 is still blocked → streaming, not batched.
            assert not release.is_set()
            release.set()
    assert sorted(got) == list(range(6))


def test_content_pass_checkpoints_in_batches(monkeypatch=None):
    """A crash mid-content-pass must not lose ALL download progress: the checkpoint is rewritten
    every CHECKPOINT_BATCH items, and every fetched key lands in it exactly once."""
    import json
    import os
    import tempfile
    from src.exporters import export_runner as er

    tmp = tempfile.mkdtemp()

    class FakeAW:
        def __init__(self):
            self.root = tmp
            self.writes = 0
            self.cp = {}

        def is_done(self, component, key):
            return key in self.cp.get(component, [])

        def get_results(self, component):
            return self.cp.get(f"{component}:results", {})

        def mark_done_bulk(self, component, keys, results=None):
            keys = list(keys)
            if not keys and not results:
                return
            self.writes += 1
            self.cp.setdefault(component, []).extend(keys)
            if results:
                self.cp.setdefault(f"{component}:results", {}).update(results)

        def read_json(self, rel):
            return None

        def write_json(self, rel, data):
            pass

    n = 450                      # > 2 batches at CHECKPOINT_BATCH=200
    units = {"notebook": [{"asset_type": "notebook", "natural_key": f"/nb/{i}",
                           "export_status": "success", "payload": {"language": "PYTHON"}}
                          for i in range(n)]}

    class FakeFetch:
        def __init__(self, *a, **k):
            pass

        def fetch(self, unit):
            from src.exporters.content_fetcher import FetchResult
            return FetchResult(status="success", content_ref="x", content_route="direct_download")

    orig = er.ContentFetcher
    er.ContentFetcher = FakeFetch
    try:
        aw = FakeAW()
        runner = er.ExportRunner(client=None, config=_FakeCfg(), artifact_writer=aw,
                                 content_fetch_workers=4)
        runner._fetch_content(units)
    finally:
        er.ContentFetcher = orig

    done = aw.cp["export:content"]
    assert len(done) == n, f"expected {n} checkpointed keys, got {len(done)}"
    assert len(set(done)) == n, "a key was checkpointed twice"
    # 450 items → flushes at 200 and 400, plus the final remainder flush = 3.
    expected = n // er.CHECKPOINT_BATCH + 1
    assert aw.writes == expected, f"expected {expected} checkpoint writes, got {aw.writes}"
    # And crucially NOT one write per item (the O(n²)-bytes failure mode).
    assert aw.writes < n


class _FakeCfg:
    """Minimal config for the content-pass test (toggles/ids unused by _fetch_content)."""
    class _T:
        def __getattr__(self, name):
            return True
    toggles = _T()
    run_id = "t"
    source_workspace_id = "1"
    output_path = ""


def test_dab_bundle_content_is_never_imported():
    """Workspace content under a `.bundle/` root is exported but NEVER imported.

    Importing it is not merely redundant: `state/terraform.tfstate` maps bundle resources to
    SOURCE-workspace object ids, so landing it on the target makes the customer's next
    `bundle deploy` update/delete the wrong objects. Files, notebooks AND the bundle's directories
    must all read `dab_redeploy`, same as the bundle-owned jobs/pipelines.
    """
    from src.exporters.asset_export import (
        dab_bundle_root, derive_import_action, is_dab_content_path,
    )

    # Path detection: only real bundle roots, and only plain workspace content.
    assert is_dab_content_path("workspace_file", "/Shared/.bundle/b/files/databricks.yml")
    assert is_dab_content_path("notebook", "/Users/a@x.com/.bundle/b/files/nb")
    assert is_dab_content_path("directory", "/Shared/.bundle/b/state")
    # The `.bundle` CONTAINER dir itself, not just paths under it. Matching only the "/.bundle/"
    # segment let this one directory fall through to `create` while every file inside it read
    # `dab_redeploy` — the one row in the tree that claimed the importer would recreate it.
    assert is_dab_content_path("directory", "/Shared/.bundle")
    assert is_dab_content_path("directory", "/Users/a@x.com/.bundle")
    assert not is_dab_content_path("workspace_file", "/Shared/wsmig/config.json")
    # A job under a bundle path is DAB-owned via dab_registry (mode="dab"), not via this check.
    assert not is_dab_content_path("job", "/Shared/.bundle/b/files/x")
    # Must not match a folder that merely LOOKS similar.
    assert not is_dab_content_path("notebook", "/Shared/my.bundle.stuff/nb")
    assert not is_dab_content_path("workspace_file", "/Shared/.bundlex/b/f")

    # Bundle root extraction → one manual-action row per bundle, not per file.
    assert dab_bundle_root("/Shared/.bundle/b1/files/x") == "/Shared/.bundle/b1"
    assert dab_bundle_root("/Users/a@x.com/.bundle/u1/state/y") == "/Users/a@x.com/.bundle/u1"
    assert dab_bundle_root("/Shared/nope") == ""

    # The action itself: content/auto must NOT win and advertise CREATE + UPLOAD.
    def act(asset_type, key, mode, status="success"):
        return derive_import_action({"asset_type": asset_type, "natural_key": key,
                                     "migration_mode": mode, "export_status": status})
    assert act("workspace_file", "/Shared/.bundle/b/files/databricks.yml", "content") == \
        "dab_redeploy"
    assert act("notebook", "/Shared/.bundle/b/files/nb", "content") == "dab_redeploy"
    assert act("directory", "/Shared/.bundle/b/state", "auto") == "dab_redeploy"
    assert act("directory", "/Shared/.bundle", "auto") == "dab_redeploy"
    # Non-bundle content is unaffected.
    assert act("notebook", "/Shared/wsmig/nb", "content") == "create_and_upload"
    assert act("directory", "/Shared/wsmig", "auto") == "create"
    # A terminal status still wins over the bundle rule (toggled off / oversize).
    assert act("workspace_file", "/Shared/.bundle/b/f", "content", "skip") == "none"
    assert act("workspace_file", "/Shared/.bundle/b/f", "content", "skipped_oversize") == "manual"

    # migration_mode must stay auto/content: changing it would drop these units out of the payload
    # files and strand their ACL grants. The importer branches on import_action instead.
    from src.exporters.asset_export import build_all
    units = build_all({"workspace_object": [
        {"object_type": "FILE", "path": "/Shared/.bundle/b/state/terraform.tfstate",
         "object_id": "1"},
        {"object_type": "DIRECTORY", "path": "/Shared/.bundle/b/state", "object_id": "2"},
    ]})
    wf = units["workspace_file"][0]
    assert wf["import_action"] == "dab_redeploy" and wf["migration_mode"] == "content"
    d = units["directory"][0]
    assert d["import_action"] == "dab_redeploy" and d["migration_mode"] == "auto"


def test_acl_sheet_rows_on_bundle_content_read_dab_redeploy():
    """The Object-Permissions sheet joins nothing from the index — it hardcoded
    ("success", "apply_acl") for EVERY grant, including grants on workspace content inside a
    bundle root. The importer replays ACLs only for objects it created and it creates nothing
    under a bundle root, so those rows promised an action import will never take.

    Status stays `success` (acls.json really did capture the grant); only the ACTION changes.
    """
    from src.exporters.export_excel import _resolve_status

    def act(object_type, object_key):
        return _resolve_status("object_permissions",
                               {"object_type": object_type, "object_key": object_key},
                               {}, [], {})

    # Bundle content — every workspace object type the ACL sheet emits, incl. the `.bundle` root.
    for otype, key in (("directory", "/Shared/.bundle"),
                       ("directory", "/Shared/.bundle/b/dev"),
                       ("notebook", "/Shared/.bundle/b/dev/files/nb"),
                       ("file", "/Shared/.bundle/b/dev/state/resources.json")):
        status, note, action = act(otype, key)
        assert (status, action) == ("success", "dab_redeploy"), f"{otype} {key} → {action}"
        assert note, "the DAB row must carry the explanatory note the workbook renders"

    # Non-bundle workspace content is untouched.
    assert act("directory", "/Shared/analytics")[2] == "apply_acl"
    assert act("notebook", "/Users/a@x.com/nb")[2] == "apply_acl"
    # Pathless assets keep the uniform action: a bundle-owned job/warehouse/scope is flagged on
    # its OWN sheet (via dab_registry), and its object_key is a name, never a path.
    for otype in ("job", "cluster", "sql_warehouse", "secret_scope", "dlt_pipeline"):
        assert act(otype, "nightly")[2] == "apply_acl", otype
    # A name that merely looks bundle-ish must not be misread as a path.
    assert act("secret_scope", "/Shared/.bundle/b")[2] == "apply_acl"


def test_dab_flag_column_on_every_pathless_dab_capable_asset_tab():
    """`_COLUMNS` is the single registry all three renderers read (inventory HTML, inventory
    Excel, export workbook), so the DAB column must be declared there AND populated by `adapt()`
    — but ONLY for asset types a bundle can actually own (`DAB_CAPABLE_ASSET_TYPES`).

    Regression: warehouses/clusters/scopes/serving endpoints CAN be bundle-owned (stamped from the
    bundle state files by `_stamp_dab_ownership`) but had no column saying so. INV-2 refinement:
    instance pools and legacy Redash dashboards are NOT bundle resources, so the column is dropped
    from those tabs rather than showing a misleading bare "Manual".
    """
    from src.reports.inventory_view import _COLUMNS, _resolve_items, adapt

    dab = {"deployed_by_dab": True, "dab_scope": "shared"}
    data = adapt({
        "sql": [dict(dab, sql_type="warehouse", id="w1", _raw={"name": "wh"}, name="wh"),
                dict(sql_type="legacy_dashboard", id="d1", name="dash")],
        "compute": [dict(dab, compute_type="cluster", cluster_id="c1", cluster_name="cl"),
                    dict(compute_type="instance_pool", instance_pool_id="p1",
                         instance_pool_name="pool")],
        "secret_scope": [dict(dab, name="kv", backend_type="DATABRICKS", key_names=["k"])],
        "serving_endpoint": [dict(dab, name="ep", migratable=True)],
    })
    # DAB-capable pathless tabs: column present AND populated.
    for card in ("sql_warehouses", "clusters", "secret_scopes", "serving_endpoints"):
        labels = [lbl for (_k, lbl, _f) in _COLUMNS[card]]
        assert "Deployed by DAB" in labels, f"{card} has no DAB column"
        for row in _resolve_items(data, card):
            assert row.get("_dab"), f"{card} row has an empty _dab cell: {row!r}"
    # Non-DAB-capable tabs: NO column at all (INV-2).
    for card in ("instance_pools", "sql_dashboards"):
        labels = [lbl for (_k, lbl, _f) in _COLUMNS[card]]
        assert "Deployed by DAB" not in labels, f"{card} should NOT have a DAB column"

    # The label vocabulary matches the jobs tab (Manual / DAB (Shared) / DAB (User)).
    assert _resolve_items(data, "sql_warehouses")[0]["_dab"] == "DAB (Shared)"


def test_dab_content_keeps_action_and_note_through_the_content_pass():
    """The content pass sets export_status/note late; the bundle action must survive it, and the
    explanatory note must end up on the unit (it's what the workbook shows the operator)."""
    import tempfile
    from src.exporters import export_runner as er
    from src.exporters.asset_export import DAB_CONTENT_NOTE, build_all
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.content_fetcher import FetchResult

    tmp = tempfile.mkdtemp()

    class Cfg(_FakeCfg):
        output_path = tmp
    aw = ArtifactWriter(Cfg())
    aw.ensure_output_path()

    units = build_all({"workspace_object": [
        {"object_type": "FILE", "path": "/Shared/.bundle/b/files/databricks.yml", "object_id": "1"},
        {"object_type": "NOTEBOOK", "path": "/Shared/wsmig/nb", "object_id": "2",
         "language": "PYTHON"},
    ]})

    class Ok:
        def __init__(self, *a, **k):
            pass

        def fetch(self, unit):
            return FetchResult(status="success", content_ref="c", content_route="direct_download",
                              note="fetched")

    orig = er.ContentFetcher
    er.ContentFetcher = Ok
    try:
        r = er.ExportRunner(None, Cfg(), aw, content_fetch_workers=2)
        r._fetch_content(units)
        r._refresh_import_actions(units)
    finally:
        er.ContentFetcher = orig

    bundle = units["workspace_file"][0]
    assert bundle["import_action"] == "dab_redeploy", "content pass clobbered the bundle action"
    assert bundle["note"] == DAB_CONTENT_NOTE, "operator-facing reason missing"
    plain = units["notebook"][0]
    assert plain["import_action"] == "create_and_upload" and plain["note"] == "fetched"


def test_content_pass_resumes_after_a_crash():
    """THE regression test for mid-run failure.

    A crash mid-content-pass must leave a checkpoint that a re-run can actually use. Batching the
    checkpoint is not enough on its own: resume also needs each item's OUTCOME, and the
    export_index.json that used to supply it is written only after the pass — so after a crash it
    is absent and every file was re-fetched regardless of the checkpoint. The outcome now rides in
    the checkpoint itself. Asserts the re-fetch is bounded AND that resumed units keep their data.
    """
    import json
    import os
    import tempfile
    from src.exporters import export_runner as er
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.content_fetcher import FetchResult

    tmp = tempfile.mkdtemp()

    class Cfg(_FakeCfg):
        output_path = tmp
    aw = ArtifactWriter(Cfg())
    aw.ensure_output_path()

    n, crash_at = 450, 320

    def mkunits():
        return {"notebook": [{"asset_type": "notebook", "natural_key": f"/nb/{i}",
                              "export_status": "success", "payload": {"language": "PYTHON"}}
                             for i in range(n)]}

    class Crashy:
        def __init__(self, *a, **k):
            self.n = 0

        def fetch(self, unit):
            self.n += 1
            if self.n > crash_at:
                raise KeyboardInterrupt("cluster died")
            return FetchResult(status="success", content_ref="orig",
                               content_route="direct_download")

    orig = er.ContentFetcher
    er.ContentFetcher = Crashy
    try:
        try:
            # workers=1 keeps the crash point deterministic.
            er.ExportRunner(None, Cfg(), aw, content_fetch_workers=1)._fetch_content(mkunits())
            raise AssertionError("the fake fetcher should have crashed the pass")
        except KeyboardInterrupt:
            pass
    finally:
        er.ContentFetcher = orig

    cp = json.load(open(os.path.join(tmp, "misc", "checkpoint.json")))
    batch = er.CHECKPOINT_BATCH
    assert len(cp["export:content"]) == batch, "checkpoint did not survive the crash"
    assert len(cp["export:content:results"]) == batch, "outcomes not persisted → cannot resume"

    refetched = []

    class Counting:
        def __init__(self, *a, **k):
            pass

        def fetch(self, unit):
            refetched.append(unit["natural_key"])
            return FetchResult(status="success", content_ref="new",
                               content_route="direct_download")

    er.ContentFetcher = Counting
    try:
        units = mkunits()
        er.ExportRunner(None, Cfg(), aw, content_fetch_workers=4)._fetch_content(units)
    finally:
        er.ContentFetcher = orig

    assert len(refetched) == n - batch, \
        f"re-run re-fetched {len(refetched)}, expected {n - batch} (resume not working)"
    # Resumed units must carry the ORIGINAL fetch's data, not a blank/overwritten row.
    resumed = [u for u in units["notebook"] if u["natural_key"] in set(cp["export:content"])]
    assert len(resumed) == batch
    assert all(u["export_status"] == "success" and u["content_ref"] == "orig" for u in resumed), \
        "resumed units lost their content_ref"
    cp2 = json.load(open(os.path.join(tmp, "misc", "checkpoint.json")))
    assert len(cp2["export:content"]) == len(set(cp2["export:content"])) == n

    # force_full_export must ignore the checkpoint entirely.
    refetched.clear()
    er.ContentFetcher = Counting
    try:
        er.ExportRunner(None, Cfg(), aw, content_fetch_workers=4,
                        force_full_export=True)._fetch_content(mkunits())
    finally:
        er.ContentFetcher = orig
    assert len(refetched) == n, "force_full_export must re-fetch everything"


def test_checkpoint_results_roundtrip_and_backcompat():
    """mark_done_bulk stores outcomes atomically with the keys; an older checkpoint (keys only,
    no results) must not crash get_results."""
    import json
    import os
    import tempfile
    from src.exporters.artifact_writer import ArtifactWriter

    tmp = tempfile.mkdtemp()

    class Cfg(_FakeCfg):
        output_path = tmp
    aw = ArtifactWriter(Cfg())
    aw.ensure_output_path()

    aw.mark_done_bulk("c", ["a", "b"], {"a": {"export_status": "success"}})
    assert aw.is_done("c", "a") and aw.is_done("c", "b")
    assert aw.get_results("c")["a"]["export_status"] == "success"
    # Second batch merges rather than replacing.
    aw.mark_done_bulk("c", ["d"], {"d": {"export_status": "skipped_oversize"}})
    assert sorted(aw.get_results("c")) == ["a", "d"]
    assert len(json.load(open(os.path.join(tmp, "misc", "checkpoint.json")))["c"]) == 3
    # Re-marking an existing key must not duplicate it.
    aw.mark_done_bulk("c", ["a"], None)
    assert json.load(open(os.path.join(tmp, "misc", "checkpoint.json")))["c"].count("a") == 1
    # Old-format checkpoint: keys but no ":results" → empty dict, no exception.
    aw.write_json(BP.CHECKPOINT_JSON, {"c": ["a", "b"]})
    assert aw.get_results("c") == {}
    assert aw.is_done("c", "a")


def test_manifest_excludes_execution_log():
    """The log is flushed AFTER the manifest, so checksumming it would fail verify on every run."""
    from src.exporters.artifact_writer import _excluded_from_manifest
    assert _excluded_from_manifest("manifest.json")
    assert _excluded_from_manifest("execution_export.log")
    assert _excluded_from_manifest("execution_inventory.log")
    assert not _excluded_from_manifest("export_index.json")
    assert not _excluded_from_manifest("inventory.json")
    # Not over-broad: a real artifact that merely contains "log" stays in the manifest.
    assert not _excluded_from_manifest("changelog.json")


def test_logger_survives_a_filesystem_that_rejects_append():
    """THE regression test for the truncated log.

    A UC Volume rejects `open(path, "a")` once the file exists. The logger swallowed that (so
    logging could never break the pipeline), which meant a whole live export produced a ONE-LINE
    execution log. Simulate that filesystem exactly and require every record to survive.
    Verified against the pre-fix logger: it writes 1 line here.
    """
    import builtins
    import json as _j
    import os
    import tempfile
    from src.utils import logger as lg

    real_open = builtins.open
    dest = os.path.join(tempfile.mkdtemp(), "execution_export.log")

    def hostile_open(file, mode="r", *a, **k):
        if str(file) == dest and "a" in mode and os.path.exists(str(file)):
            raise OSError(95, "Operation not supported")   # what the Volume does
        return real_open(file, mode, *a, **k)

    builtins.open = hostile_open
    try:
        lg.set_log_file(dest)
        log = lg.get_logger("probe")
        for i in range(120):        # spans several mirror intervals
            log.info("record", i=i)
        log.warning("a warning")
        lg.flush_log_file()
    finally:
        builtins.open = real_open
        lg.set_log_file(None)

    lines = [_j.loads(x) for x in real_open(dest, encoding="utf-8").read().strip().split("\n")]
    assert len(lines) == 121, f"expected 121 records, got {len(lines)} (append-truncation bug)"
    assert [r["i"] for r in lines if "i" in r] == list(range(120)), "records lost or reordered"
    assert lines[-1]["level"] == "WARNING"


def test_logger_survives_append_hostile_dest_and_verifies_manifest():
    """End-to-end: many log records + a manifest build, then verify_manifest must pass.

    Reproduces the live fvm1 bug two ways — (a) a log truncated to one line, (b) a manifest that
    mismatches because the log grew after being checksummed.
    """
    import tempfile
    import os
    from src.utils import logger as lg
    from src.exporters.artifact_writer import ArtifactWriter

    tmp = tempfile.mkdtemp()

    class Cfg:
        run_id = "r"
        source_workspace_id = "ws"
        output_path = tmp
    aw = ArtifactWriter(Cfg())
    aw.ensure_output_path()

    dest = os.path.join(tmp, "execution_export.log")
    lg.set_log_file(dest)
    log = lg.get_logger("t")
    try:
        for i in range(60):          # > _MIRROR_EVERY, so several mirrors happen mid-run
            log.info("record", i=i)
        aw.write_json("export_index.json", {"units": []})
        aw.write_manifest({"notebook": 0})
        log.info("after manifest")   # the real post-manifest flush case
        log.warning("and a warning")
        lg.flush_log_file()

        import json as _j
        lines = [_j.loads(x) for x in open(dest, encoding="utf-8").read().strip().split("\n")]
        # 60 numbered records + write_manifest's own "manifest written" + 2 post-manifest records.
        assert len(lines) == 63, f"expected 63 records, got {len(lines)} (append-truncation bug)"
        assert [r.get("i") for r in lines if "i" in r] == list(range(60)), "records lost/reordered"
        assert lines[-1]["level"] == "WARNING" and lines[-2]["msg"] == "after manifest"

        v = aw.verify_manifest()
        assert v["ok"], f"manifest must verify despite the log growing after it: {v}"
        assert not any("execution" in p for p in
                       [f["path"] for f in v["manifest"]["files"]])
    finally:
        lg.set_log_file(None)


# ─────────────────────────── content_fetcher ───────────────────────────────

def test_content_fetcher_success(tmp_path=None):
    import tempfile, os
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.content_fetcher import ContentFetcher

    tmp = tempfile.mkdtemp()
    cfg = _cfg(source_staging_location=tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()

    nb_py = b"# Databricks notebook source\nprint(1)\n"
    nb_sql = b"-- Databricks notebook source\nSELECT 1\n"
    csv = b"col1,col2\n1,2\n"
    dl = {"api/2.0/workspace/export": lambda p: (
        nb_py if p.get("path", "").endswith("py_nb") else
        nb_sql if p.get("path", "").endswith("sql_nb") else csv)}
    fetcher = ContentFetcher(FakeClient(download_table=dl), aw)

    # notebook (PYTHON) → SOURCE .py, direct_download route
    r1 = fetcher.fetch({"asset_type": "notebook", "natural_key": "/Users/a/py_nb",
                        "payload": {"language": "PYTHON"}})
    assert r1.status == "success" and r1.content_route == "direct_download"
    assert r1.content_ref.endswith(".py")
    assert open(os.path.join(aw.root, r1.content_ref), "rb").read() == nb_py

    # notebook (SQL) → .sql
    r2 = fetcher.fetch({"asset_type": "notebook", "natural_key": "/Users/a/sql_nb",
                        "payload": {"language": "SQL"}})
    assert r2.status == "success" and r2.content_ref.endswith(".sql")

    # workspace_file → verbatim .bin
    rf = fetcher.fetch({"asset_type": "workspace_file", "natural_key": "/Users/a/x.csv",
                        "payload": {}})
    assert rf.status == "success" and rf.content_ref.endswith(".bin")
    assert open(os.path.join(aw.root, rf.content_ref), "rb").read() == csv


def test_content_fetcher_big_notebook_skipped_no_bytes():
    """A >10MB notebook (MAX_NOTEBOOK_SIZE_EXCEEDED) → skipped_oversize, and NO bytes written
    (decision: it can't be recreated as a notebook via any API)."""
    import tempfile, os
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.content_fetcher import ContentFetcher
    from src.auth.token_manager import DownloadHTTPError

    tmp = tempfile.mkdtemp()
    cfg = _cfg(source_staging_location=tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()

    dl = {"api/2.0/workspace/export": DownloadHTTPError(400, "MAX_NOTEBOOK_SIZE_EXCEEDED")}
    r = ContentFetcher(FakeClient(download_table=dl), aw).fetch(
        {"asset_type": "notebook", "natural_key": "/Users/a/huge", "payload": {"language": "PYTHON"}})
    assert r.status == "skipped_oversize" and r.content_ref is None
    assert r.oversize.get("type") == "notebook" and "10 MB" in r.oversize["reason"]
    # nothing was written into content/
    content_dir = os.path.join(aw.root, "export/workspace/content")
    assert not os.path.isdir(content_dir) or not os.listdir(content_dir)


def test_content_fetcher_big_file_oversize_and_failure():
    import tempfile
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.content_fetcher import ContentFetcher, FILE_CAP
    from src.auth.token_manager import DownloadHTTPError, OversizeError

    tmp = tempfile.mkdtemp()
    cfg = _cfg(source_staging_location=tmp)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()

    # file over 500MB → OversizeError from download_bytes cap → skipped_oversize
    dl = {"api/2.0/workspace/export": OversizeError(FILE_CAP + 1, "too big")}
    r = ContentFetcher(FakeClient(download_table=dl), aw).fetch(
        {"asset_type": "workspace_file", "natural_key": "/Users/a/giant.parquet", "payload": {}})
    assert r.status == "skipped_oversize" and r.oversize["type"] == "file"

    # a genuine non-size error → failure (files don't get the notebook-oversize benefit of doubt)
    dl3 = {"api/2.0/workspace/export": DownloadHTTPError(403, "forbidden")}
    r3 = ContentFetcher(FakeClient(download_table=dl3), aw).fetch(
        {"asset_type": "workspace_file", "natural_key": "/Users/a/forbidden", "payload": {}})
    assert r3.status == "failure" and "forbidden" in r3.note


def test_mangle_path_collision():
    from src.exporters.content_fetcher import mangle_path
    taken = set()
    a = mangle_path("/Users/a/NB", "notebook", "PYTHON", taken)
    b = mangle_path("/Users/a/nb", "notebook", "PYTHON", taken)   # case-insensitive collision
    assert a != b and a.lower() != b.lower()
    f = mangle_path("/Shared/data.csv", "file", "", taken)
    assert f.endswith(".bin") and f.startswith("Shared__data.csv")


# ─────────────────────────── bundle_state ──────────────────────────────────

def test_bundle_state_resolution():
    import tempfile, os, json
    from src.exporters import bundle_state as bs

    tmp = tempfile.mkdtemp()
    cfg = _cfg(source_staging_location=tmp, run_id="20260101_000000")
    root = bs.wsmig_root(cfg)
    os.makedirs(root)

    # no pointer, no bundle → export resolution fails loudly.
    try:
        bs.resolve_export_run_id(cfg, "", False)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass

    # explicit widget always wins.
    assert bs.resolve_export_run_id(cfg, "R99", False) == ("R99", "widget")

    # pointer resolves.
    bs.write_latest_pointer(cfg, "20260101_120000", {"job": 3})
    assert bs.resolve_export_run_id(cfg, "", False) == ("20260101_120000", "pointer")

    # an incomplete bundle (checkpoint, no manifest) is resumed ahead of the pointer. The
    # bookkeeping files live under misc/ now (PLAN 7 §D), which is where bundle_state probes them.
    from src.exporters import bundle_paths as bp
    def _write(rundir, rel, data):
        p = os.path.join(rundir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f)
    inc = os.path.join(root, "20260102_000000")
    os.makedirs(inc)
    _write(inc, bp.CHECKPOINT_JSON, {})
    assert bs.resolve_export_run_id(cfg, "", False) == ("20260102_000000", "resume")
    # force_full skips resume → falls back to pointer.
    assert bs.resolve_export_run_id(cfg, "", True)[1] == "pointer"

    # a complete bundle (manifest present) is NOT resumed.
    _write(inc, bp.MANIFEST_JSON, {})
    assert bs.find_latest_incomplete_run(cfg) is None

    # inventory resolution: blank + incomplete → resume; else fresh.
    inc2 = os.path.join(root, "20260103_000000")
    os.makedirs(inc2)
    _write(inc2, bp.CHECKPOINT_JSON, {})
    assert bs.resolve_inventory_run_id(cfg, "", False) == ("20260103_000000", "resume")
    assert bs.resolve_inventory_run_id(cfg, "", True) == (cfg.run_id, "fresh")
    assert bs.resolve_inventory_run_id(cfg, "X", False) == ("X", "widget")


def test_dab_alert_v2_is_stamped_from_bundle_state_not_path():
    """A DAB-deployed Alerts V2 must be detected from the bundle STATE file.

    Regression (found live 2026-08-06): `GET /api/2.0/alerts` — the LIST call the collector
    uses — omits `parent_path` entirely; only GET-by-id returns it. So sql_collector's
    path-based detection can never fire for alerts, and a bundle-owned alert was classified
    Manual → import_action=create, which would DUPLICATE an alert the customer's bundle
    redeploys on every release.
    """
    import json as _json

    from src.collectors.inventory_runner import InventoryRunner

    state_path = "/Shared/.bundle/b1/state/resources.json"
    state_doc = {"state": {
        # the shape a real CLI 1.5.0 resources.json uses
        "resources.alerts.dab_alert": {"__id__": "999", "state": {"display_name": "dab_alert"}},
        "resources.alerts.dab_alert.permissions": {"__id__": "/alertsv2/999", "state": {}},
    }}
    client = FakeClient(download_table={
        "api/2.0/workspace/export": lambda p: (
            _json.dumps(state_doc).encode() if p.get("path") == state_path else b"")})

    # exactly what sql_collector emits: sql_type="alert", no parent_path from the list call
    objects = {"sql": [
        {"sql_type": "alert", "id": "999", "name": "dab_alert", "_natural_key": "dab_alert",
         "deployed_by_dab": False, "dab_scope": ""},
        {"sql_type": "alert", "id": "111", "name": "hand_made", "_natural_key": "hand_made",
         "deployed_by_dab": False, "dab_scope": ""},
        # a warehouse in the same mixed bucket must not be disturbed
        {"sql_type": "warehouse", "id": "999", "name": "wh", "_natural_key": "wh",
         "deployed_by_dab": False, "dab_scope": ""},
    ]}
    runner = InventoryRunner.__new__(InventoryRunner)
    runner.client = client
    runner._stamp_dab_ownership(objects, {state_path})

    by_name = {r["name"]: r for r in objects["sql"]}
    assert by_name["dab_alert"]["deployed_by_dab"] is True, \
        "bundle-owned alert must be stamped from the state file"
    assert by_name["dab_alert"]["dab_scope"] == "shared"
    assert by_name["hand_made"]["deployed_by_dab"] is False, "hand-made alert must stay Manual"
    # id 999 collides with the alert's id on purpose: the mixed `sql` bucket must be filtered by
    # sql_type, or a warehouse would inherit an alert's DAB claim.
    assert by_name["wh"]["deployed_by_dab"] is False, \
        "warehouse must not inherit the alert's claim on the same id"

    # and the exported unit must then carry the skip-on-import action
    from src.exporters.asset_export import build_all
    unit = build_all({"sql": [by_name["dab_alert"] | {"_raw": {"display_name": "dab_alert"}}]})
    au = unit["alert_v2"][0]
    assert au["export_status"] == "dab" and au["import_action"] == "dab_redeploy"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


def test_managed_by_labels_cover_every_kind_in_BOTH_html_and_excel():
    """The "Managed By" column must never show a raw internal value like "account".

    HTML and Excel each used to keep their OWN copy of this label map, and both knew only the
    pre-Plan-6 vocabulary — so a new `kind` fell through to `str(value)` and rendered as the raw
    string. They now share one map; this pins that every kind has a human label in both.
    """
    from src.reports.inventory_view import MANAGED_BY_LABEL, managed_by_label
    from src.identity.classifier import IdentityKind
    for kind in IdentityKind:
        assert kind.value in MANAGED_BY_LABEL, f"`{kind.value}` has no Managed By label"
        assert managed_by_label(kind.value) != kind.value, \
            f"`{kind.value}` renders as its raw internal value"
    # the legacy vocabulary must still render, so old reports/bundles stay readable
    for legacy in ("entra_user", "umi_or_entra_sp", "db_managed_sp", "account_group",
                   "db_managed_group", "builtin_group"):
        assert managed_by_label(legacy) != legacy

    # and the two renderers must agree, character for character
    from src.reports.html_generator import _cell_html as html_cell
    from src.exporters.excel_generator import _cell_text as excel_cell
    for kind in IdentityKind:
        excel = str(excel_cell(kind.value, "cls_managed"))
        assert excel in html_cell(kind.value, "cls_managed"), \
            f"HTML and Excel disagree on the label for `{kind.value}`"


def test_import_action_labels_cover_the_closed_vocabulary():
    """Every action in IMPORT_ACTIONS needs an Excel label, or it silently renders as "—"."""
    from src.exporters.asset_export import IMPORT_ACTIONS
    from src.exporters.export_excel import _IMPORT_ACTION_LABEL, _IMPORT_ACTION_FILL
    for action in IMPORT_ACTIONS:
        assert action in _IMPORT_ACTION_LABEL, f"`{action}` has no Excel label"
        assert action in _IMPORT_ACTION_FILL, f"`{action}` has no Excel fill colour"
    # the automatic path must NOT be coloured/worded as a human prerequisite
    assert "must pre-exist" not in _IMPORT_ACTION_LABEL["adopt_or_assign"]
    assert _IMPORT_ACTION_FILL["adopt_or_assign"] == _IMPORT_ACTION_FILL["create"], \
        "adopt_or_assign is fully automatic, so it must be green like create — not prerequisite blue"
    assert "pre-exist" in _IMPORT_ACTION_LABEL["assign_on_target"]


def test_payload_files_carry_the_fields_import_branches_on():
    """Export writes TWO places — the index (ledger) and the per-asset payload files — and the
    importer merges them. `_artifact_unit`'s allowlist decides what survives into the payload file.

    `kind` was missing from it, so import got the field only from the index. Anything reading the
    payload file (and the runner's carry-over list does) would have seen no `kind` and degraded every
    group to NEEDS_REVIEW. Both allowlists must agree on the identity fields.
    """
    from src.exporters.export_runner import ExportRunner
    from src.importers.import_runner import ImportRunner
    unit = {"asset_type": "group", "natural_key": "g", "payload": {}, "kind": "account",
            "entra_backed": True, "members_are_account_owned": True,
            "workspace_permissions": ["ADMIN"], "externalId": "oid", "classification": "account"}
    written = ExportRunner._artifact_unit(unit)
    for field in ("kind", "entra_backed", "members_are_account_owned", "workspace_permissions"):
        assert field in written, f"`{field}` is dropped when writing the payload file"
        # ...and the importer must actually pick it up off that file
        assert field in ImportRunner._PAYLOAD_CARRY_FIELDS, \
            f"`{field}` is written but never merged back onto the unit"
