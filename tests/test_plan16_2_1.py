"""PLAN 16.2.1 — hotfix 1.1.1: cluster policies built on a POLICY FAMILY (offline tests, plan §4).

A family policy's GET/list carries `policy_family_id` + `policy_family_definition_overrides` AND the
resolved `definition`. 1.1.0 copied all three into the create body → 400 `policy_family_id and
definition cannot be used together`, and every job/cluster using the policy then failed
`prerequisite_missing`. The fix sends family_id + overrides XOR definition, on create AND edit.

`ChainTarget` enforces the API's rule (verified live 2026-10-01/09), so a body carrying both fields
fails here exactly as it does on a real workspace.
"""
from __future__ import annotations

import json

from src.auth.token_manager import HTTPStatusError
from src.exporters import bundle_paths as BP
from src.exporters.asset_export import build_all
from src.importers.compute_importer import ComputeImporter
from tests.test_plan16_2 import Env, FakeTarget, _rows, _runner

FAMILY = "wsmig_test_policy_family_job"
CUSTOM = "wsmig_test_policy"
SRC_POLICY_ID = "SRCPOL1"
OVERRIDES = json.dumps({"custom_tags.wsmig_policy": {"type": "fixed", "value": "family_job"}})
RESOLVED = json.dumps({"custom_tags.wsmig_policy": {"type": "fixed", "value": "family_job"},
                       "cluster_type": {"type": "fixed", "value": "job"}})
CUSTOM_DEF = json.dumps({"node_type_id": {"type": "fixed", "value": "Standard_DS3_v2"}})
_BOTH = "INVALID_PARAMETER_VALUE policy_family_id and definition cannot be used together"


class ChainTarget(FakeTarget):
    """FakeTarget + cluster policies + jobs, with the real create/edit validation rule."""

    def __init__(self):
        super().__init__()
        self.policies: dict = {}     # name → policy_id
        self.jobs: dict = {}         # name → job_id

    def get(self, path, params=None):
        if path == "api/2.0/policies/clusters/list":
            self.calls.append(("GET", path, params))
            return {"policies": [{"name": n, "policy_id": i} for n, i in self.policies.items()]}
        if path in ("api/2.0/instance-pools/list", "api/2.0/clusters/list"):
            self.calls.append(("GET", path, params))
            return {}
        return super().get(path, params)

    def get_paginated(self, path, result_key, token_key="next_page_token", params=None,
                      max_pages=100000):
        if path == "api/2.1/jobs/list":
            self.calls.append(("GET_PAGINATED", path, params))
            return [{"job_id": i, "settings": {"name": n}} for n, i in self.jobs.items()]
        return super().get_paginated(path, result_key, token_key, params, max_pages)

    def post(self, path, body):
        if path in ("api/2.0/policies/clusters/create", "api/2.0/policies/clusters/edit"):
            self.calls.append(("POST", path, body))
            if body.get("policy_family_id") and body.get("definition") is not None:
                raise HTTPStatusError(400, f"POST https://t/{path} -> 400: {_BOTH}")
            if path.endswith("/create"):
                pid = self._id("tpol")
                self.policies[body["name"]] = pid
                return {"policy_id": pid}
            return {}
        if path == "api/2.1/jobs/create":
            self.calls.append(("POST", path, body))
            jid = self._id("tjob")
            self.jobs[body["name"]] = jid
            return {"job_id": jid}
        return super().post(path, body)


def _family_payload(**extra):
    p = {"name": FAMILY, "policy_family_id": "job-cluster",
         "policy_family_definition_overrides": OVERRIDES, "definition": RESOLVED,
         "description": "job compute for team x"}
    p.update(extra)
    return p


def _policy_unit(payload, nk=None, source_id=SRC_POLICY_ID, action="create"):
    return {"asset_type": "cluster_policy", "natural_key": nk or payload["name"],
            "source_id": source_id, "fingerprint": "fp-" + (nk or payload["name"]),
            "import_action": action, "export_status": "success", "payload": payload}


def _env():
    env = Env()
    env.client = ChainTarget()
    return env


def _policy_posts(env, endswith):
    return [c[2] for c in env.client.writes("POST", endswith=endswith)]


# ═══════════════════════════════ create / edit body shape ═══════════════════════════════

def test_family_policy_create_sends_family_id_and_overrides_never_definition():
    env = _env()
    res, _ = env.run(ComputeImporter, [_policy_unit(_family_payload())])
    assert _rows(res, "cluster_policy")[FAMILY]["import_status"] == "created"
    body = _policy_posts(env, "policies/clusters/create")[0]
    assert body == {"name": FAMILY, "policy_family_id": "job-cluster",
                    "policy_family_definition_overrides": OVERRIDES,
                    "description": "job compute for team x"}
    assert "definition" not in body


def test_custom_policy_create_body_is_byte_identical_to_1_1_0():
    payload = {"name": CUSTOM, "definition": CUSTOM_DEF, "description": "d",
               "max_clusters_per_user": 2}
    # 1.1.0's body: every present field of this list, in this order.
    old = {"name": CUSTOM}
    for f in ("definition", "description", "libraries", "max_clusters_per_user",
              "policy_family_id", "policy_family_definition_overrides"):
        if payload.get(f) is not None:
            old[f] = payload[f]
    env = _env()
    env.run(ComputeImporter, [_policy_unit(payload)])
    body = _policy_posts(env, "policies/clusters/create")[0]
    assert json.dumps(body) == json.dumps(old)


def test_family_only_policy_without_overrides_sends_family_id_only():
    env = _env()
    payload = {"name": FAMILY, "policy_family_id": "personal-vm", "definition": RESOLVED}
    env.run(ComputeImporter, [_policy_unit(payload)])
    assert _policy_posts(env, "policies/clusters/create")[0] == {
        "name": FAMILY, "policy_family_id": "personal-vm"}


def _changed_policy_run(payload):
    env = _env()
    env.client.policies[payload["name"]] = "TP9"
    env.seed("cluster_policy", payload["name"], action="created", fp="old-fp", target="TP9",
             source=SRC_POLICY_ID)
    env.new_run("r2")
    res, _ = env.run(ComputeImporter, [_policy_unit(payload)])
    return env, res


def test_family_policy_edit_sends_family_shape_never_definition():
    env, res = _changed_policy_run(_family_payload())
    assert _rows(res, "cluster_policy")[FAMILY]["import_status"] == "updated"
    assert _policy_posts(env, "policies/clusters/edit") == [{
        "policy_id": "TP9", "name": FAMILY, "policy_family_id": "job-cluster",
        "policy_family_definition_overrides": OVERRIDES}]


def test_custom_policy_edit_body_unchanged():
    env, res = _changed_policy_run({"name": CUSTOM, "definition": CUSTOM_DEF})
    assert _rows(res, "cluster_policy")[CUSTOM]["import_status"] == "updated"
    assert _policy_posts(env, "policies/clusters/edit") == [
        {"policy_id": "TP9", "name": CUSTOM, "definition": CUSTOM_DEF}]


def test_pool_id_pinned_in_family_overrides_is_remapped_to_the_target_pool():
    env = _env()
    env.seed("instance_pool", "pool_a", action="created", fp="x", target="TGTPOOL",
             source="SRCPOOL")
    env.new_run("r2")
    pool_unit = {"asset_type": "instance_pool", "natural_key": "pool_a", "source_id": "SRCPOOL",
                 "fingerprint": "x", "import_action": "create", "export_status": "success",
                 "payload": {"instance_pool_name": "pool_a"}}
    env.client.get = _with_pool(env.client.get)
    overrides = json.dumps({"instance_pool_id": {"type": "fixed", "value": "SRCPOOL"}})
    env.run(ComputeImporter, [pool_unit, _policy_unit(
        _family_payload(policy_family_definition_overrides=overrides))])
    body = _policy_posts(env, "policies/clusters/create")[0]
    assert json.loads(body["policy_family_definition_overrides"]) == {
        "instance_pool_id": {"type": "fixed", "value": "TGTPOOL"}}
    assert "definition" not in body


def _with_pool(get):
    def wrapped(path, params=None):
        if path == "api/2.0/instance-pools/list":
            return {"instance_pools": [{"instance_pool_name": "pool_a",
                                        "instance_pool_id": "TGTPOOL"}]}
        return get(path, params)
    return wrapped


def test_1_1_0_body_would_have_been_rejected_by_the_fake():
    """Guards the fake itself: the exact 1.1.0 body (family + definition) gets the live 400."""
    env = _env()
    try:
        env.client.post("api/2.0/policies/clusters/create",
                        {"name": FAMILY, **_family_payload()})
    except HTTPStatusError as exc:
        assert "cannot be used together" in str(exc)
    else:
        raise AssertionError("ChainTarget must reject family_id + definition")


# ═══════════════════════════════ the customer chain (§4 test 6) ═══════════════════════════════

def _job_unit(name, shape):
    cluster = {"spark_version": "16.4.x-scala2.12", "node_type_id": "Standard_DS3_v2",
               "num_workers": 1, "policy_id": SRC_POLICY_ID,
               "apply_policy_default_values": True}
    task = {"task_key": "t1", "notebook_task": {"notebook_path": "/Shared/wsmig_test/py_nb"}}
    if shape == "job_cluster":
        settings = {"name": name, "job_clusters": [{"job_cluster_key": "pf",
                                                    "new_cluster": dict(cluster)}],
                    "tasks": [{**task, "job_cluster_key": "pf"}]}
    else:
        settings = {"name": name, "tasks": [{**task, "new_cluster": dict(cluster)}]}
    return {"asset_type": "job", "natural_key": name, "source_id": f"src-{name}",
            "fingerprint": f"fp-{name}", "import_action": "create", "export_status": "success",
            "payload": settings}


def _grant(principal, ptype, level):
    return {"principal": principal, "principal_type": ptype, "permission_level": level,
            "inherited": False}


def _customer_env():
    """1.1.0 left: the family policy FAILED (the 400), its two jobs FAILED (prerequisite_missing),
    their ACL rows `skipped_no_object`; plus an unrelated failed job that must stay untouched by
    the policy fix (it fails again for its own reason)."""
    env = _env()
    err = f"POST …/policies/clusters/create -> 400: {_BOTH}"
    env.state.record("cluster_policy", FAMILY, action="failed", fingerprint="",
                     source_object_id=SRC_POLICY_ID, error=err)
    for j in ("job_a", "job_b"):
        env.state.record("job", j, action="failed", fingerprint="", source_object_id=f"src-{j}",
                         error="`a job cluster` references cluster_policy … not yet created")
    env.state.record("job", "job_unrelated_ok", action="created", fingerprint="fp-job_unrelated_ok",
                     target_object_id="T-OK", source_object_id="src-job_unrelated_ok")
    for k in (f"cluster-policies:{FAMILY}", "jobs:job_a", "jobs:job_b",
              "jobs:job_unrelated_ok"):
        env.state.record("acl", k, action="skipped_no_object", fingerprint="x")
    env.state.flush()
    env.client.jobs["job_unrelated_ok"] = "T-OK"
    env.client.paths["/Shared/wsmig_test/py_nb"] = "o-nb"     # the task notebook exists on target
    env.new_run("r2", retry_mode="failed_only")
    env.aw.write_json(BP.EXPORT_ACLS_JSON, [
        {"asset_type": "cluster_policy", "natural_key": FAMILY, "source_id": SRC_POLICY_ID,
         "perm_object_type": "cluster-policies",
         "grants": [_grant("ai27_account_grp_1", "group", "CAN_USE")]},
        {"asset_type": "job", "natural_key": "job_a", "source_id": "src-job_a",
         "perm_object_type": "jobs", "grants": [_grant("bob@x.com", "user", "CAN_VIEW")]},
        {"asset_type": "job", "natural_key": "job_b", "source_id": "src-job_b",
         "perm_object_type": "jobs", "grants": [_grant("bob@x.com", "user", "CAN_MANAGE_RUN")]},
        {"asset_type": "job", "natural_key": "job_unrelated_ok",
         "source_id": "src-job_unrelated_ok", "perm_object_type": "jobs",
         "grants": [_grant("bob@x.com", "user", "CAN_VIEW")]}])
    units = {"cluster_policy": [_policy_unit(_family_payload())],
             "job": [_job_unit("job_a", "job_cluster"), _job_unit("job_b", "new_cluster"),
                     _job_unit("job_unrelated_ok", "job_cluster")]}
    runner = _runner(env)
    for family in ("compute", "jobs", "acls"):
        runner._run_phase(family, units)
    return env, runner


def test_failed_only_heals_policy_then_its_jobs_then_their_acls_in_one_run():
    env, runner = _customer_env()
    compute, jobs_res, acls = (_rows(r) for r in runner.results)
    assert compute[FAMILY]["import_status"] == "created"
    tpol = env.client.policies[FAMILY]
    assert "definition" not in _policy_posts(env, "policies/clusters/create")[0]

    assert jobs_res["job_a"]["import_status"] == "created"
    assert jobs_res["job_b"]["import_status"] == "created"
    created = {c[2]["name"]: c[2] for c in env.client.writes("POST", endswith="jobs/create")}
    assert set(created) == {"job_a", "job_b"}                     # the unrelated job not touched
    assert created["job_a"]["job_clusters"][0]["new_cluster"]["policy_id"] == tpol
    assert created["job_b"]["tasks"][0]["new_cluster"]["policy_id"] == tpol

    for k in (f"cluster-policies:{FAMILY}", "jobs:job_a", "jobs:job_b"):
        assert acls[k]["import_status"] == "created", k
    assert acls["jobs:job_unrelated_ok"].get("retry_out_of_scope") is True
    puts = {c[1] for c in env.client.writes("PUT", contains="permissions/")}
    assert puts == {f"api/2.0/permissions/cluster-policies/{tpol}",
                    f"api/2.0/permissions/jobs/{env.client.jobs['job_a']}",
                    f"api/2.0/permissions/jobs/{env.client.jobs['job_b']}"}


def test_failed_only_state_rows_end_created_with_target_ids():
    env, _ = _customer_env()
    env.state.load(force=True)
    pol = env.state.row("cluster_policy", FAMILY)
    assert pol["last_action"] == "created" and pol["target_object_id"]
    for j in ("job_a", "job_b"):
        assert env.state.row("job", j)["last_action"] == "created"


# ═══════════════════════════════ export side untouched ═══════════════════════════════

def test_family_policy_export_unit_and_fingerprint_unchanged():
    """The hotfix is import-only: the exported unit (payload + fingerprint) is what 1.1.0 wrote, so
    the customer's existing bundle is the input and no policy is re-touched by an fp change."""
    raw = {"policy_id": SRC_POLICY_ID, **_family_payload()}
    record = {"compute_type": "cluster_policy", "policy_id": SRC_POLICY_ID, "_natural_key": FAMILY,
              "_raw": raw}
    unit = build_all({"compute": [record]})["cluster_policy"][0]
    assert unit["payload"]["policy_family_id"] == "job-cluster"
    assert unit["payload"]["definition"] == RESOLVED       # still carried; import drops it
    from src.transform.transforms import fingerprint, strip_runtime
    assert unit["fingerprint"] == fingerprint(strip_runtime("cluster_policy", raw))
