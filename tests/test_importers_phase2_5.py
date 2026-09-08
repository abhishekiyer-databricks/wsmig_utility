"""Offline tests for phases 2–5: compute, workspace, secrets, jobs (Plan 3 §6, §6c, §7d).

Each test targets a specific trap the plan calls out — the ones where a naive implementation looks
correct, passes a smoke test, and then fails in the customer's workspace:

  compute   — ephemeral clusters excluded; STOPPED after create; re-pinned; pool/node-type conflict;
              pool+policy remapped by name, and a dangling reference dropped with a warning
  workspace — user home dirs can't be mkdir'd; notebooks land as NOTEBOOKS not files; .bundle/
              content never uploaded; missing bytes are manual, not an empty file
  secrets   — AKV needs an AAD token (distinguished from a vault refusal); MANAGE set at create;
              values always manual; no edit API
  jobs      — compute remapped in job_clusters AND tasks; schedule AND continuous paused; run_as
              remapped; an unresolvable notebook_path is created_with_warning, not a silent bomb
"""
from __future__ import annotations

import base64
import json
import os
import tempfile

from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.importers.compute_importer import ComputeImporter, is_ephemeral_cluster
from src.importers.jobs_importer import JobsImporter
from src.importers.secrets_importer import SecretsImporter
from src.importers.workspace_importer import WorkspaceImporter, is_user_home
from src.state.state_store import StateStore
from tests.test_state_store import FakeBackend


def _same_path(actual: str, wanted: str) -> bool:
    """Whether `actual` is the API path `wanted`, ignoring the `api/<version>/` prefix."""
    import re
    stripped = re.sub(r"^api/\d+\.\d+/", "", actual)
    return stripped == wanted.lstrip("/") or actual == wanted


class RecordingClient:
    """Records every call; answers GETs from a table; mints ids on POST."""

    def __init__(self, get_table=None, paginated=None, fail_paths=None, status_paths=None):
        self.get_table = get_table or {}
        self.paginated = paginated or {}
        self.fail_paths = set(fail_paths or ())
        self.status_paths = set(status_paths or ())   # workspace paths that "exist"
        self.calls: list[tuple] = []
        self._n = 0

    @property
    def base_url(self):
        return "https://target.example.net"

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path == "api/2.0/workspace/get-status":
            p = (params or {}).get("path")
            if p in self.status_paths:
                return {"path": p, "object_type": "DIRECTORY"}
            raise RuntimeError("RESOURCE_DOES_NOT_EXIST")
        # A cluster the test force-started reports RUNNING on the next poll, so the
        # start→poll→install→stop path can be exercised without real sleeps.
        if path == "api/2.0/clusters/get" and getattr(self, "_started_clusters", None):
            if (params or {}).get("cluster_id") in self._started_clusters:
                return {"state": "RUNNING"}
        entry = self.get_table.get(path, {})
        return entry(params) if callable(entry) else entry

    def get_paginated(self, path, result_key, token_key="next_page_token", params=None,
                      max_pages=100000):
        self.calls.append(("GET_PAGINATED", path, params))
        return self.paginated.get(path, [])

    def get_scim(self, resource, max_items=0, count=500):
        return self.get_table.get(f"scim:{resource}", [])

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path in self.fail_paths:
            raise RuntimeError("INVALID_PARAMETER_VALUE: rejected")
        # Track force-started clusters so a later clusters/get returns RUNNING (see get()).
        if path == "api/2.0/clusters/start":
            self._started_clusters = getattr(self, "_started_clusters", set())
            self._started_clusters.add(str(body.get("cluster_id") or ""))
        self._n += 1
        return {"instance_pool_id": f"pool-{self._n}", "policy_id": f"pol-{self._n}",
                "cluster_id": f"clu-{self._n}", "job_id": f"job-{self._n}", "id": f"id-{self._n}"}

    def put(self, path, body):
        self.calls.append(("PUT", path, body))
        if path in self.fail_paths:
            raise RuntimeError("INVALID_PARAMETER_VALUE: rejected")
        return {}

    def patch(self, path, body, params=None):
        # `params` carries query args (e.g. Alerts V2 `update_mask`); kept at index 3 so existing
        # assertions on the body (index 2) are unaffected.
        self.calls.append(("PATCH", path, body, params))
        if path in self.fail_paths:
            raise RuntimeError("INVALID_PARAMETER_VALUE: rejected")
        return {}

    def posts_to(self, path):
        """Calls to EXACTLY this path (modulo the api version prefix).

        Deliberately not a substring or suffix match: `clusters/create` also matches
        `policies/clusters/create`, which silently mixed cluster and policy bodies together and made
        a passing remap look like a failure.
        """
        return [c for c in self.calls if c[0] == "POST" and _same_path(c[1], path)]

    def bodies_to(self, path):
        return [c[2] for c in self.posts_to(path)]


def _make(importer_cls, units, client, dry_run=False, context=None, staging_files=None,
          identity_map=None, imports_extra=None):
    d = tempfile.mkdtemp()
    imports = {"state_catalog": "c", "state_schema": "s"}
    imports.update(imports_extra or {})
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "111", "run_id": "r1",
                            "target_staging_location": d, "dry_run": dry_run,
                            "imports": imports})
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    for rel, data in (staging_files or {}).items():
        aw.write_bytes(rel, data)
    st = StateStore(FakeBackend(), cfg)
    st.ensure_table()
    st.load()
    by_type: dict = {}
    for u in units:
        by_type.setdefault(u["asset_type"], []).append(u)
    imp = importer_cls(client, cfg, aw, state=st, units_by_type=by_type,
                       identity_map=identity_map,
                       context=context if context is not None else {})
    return imp, st


def _unit(asset_type, key, payload=None, **over):
    u = {"asset_type": asset_type, "natural_key": key, "source_id": f"src-{key}",
         "fingerprint": f"sha256:{key}", "import_action": "create", "export_status": "success",
         "payload": payload or {}, "note": ""}
    u.update(over)
    return u


# ═══════════════════════════════ COMPUTE ═══════════════════════════════════

def test_ephemeral_clusters_are_never_migrated():
    """`job-*` / `dlt-execution-*` clusters die with their run; recreating them litters the target."""
    assert is_ephemeral_cluster("job-123-run-456")
    assert is_ephemeral_cluster("dlt-execution-abc")
    assert is_ephemeral_cluster("mlflow-model-x")
    assert not is_ephemeral_cluster("etl-shared-cluster")

    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("cluster", "job-999-run-1", {"cluster_name": "job-999-run-1"}),
        _unit("cluster", "real-etl", {"cluster_name": "real-etl", "spark_version": "14.3.x"}),
    ], client)
    res = imp.run()
    assert res.total == 1, "the ephemeral cluster must not even be reported as work"
    assert [b["cluster_name"] for b in client.bodies_to("clusters/create")] == ["real-etl"]


def test_a_new_cluster_is_stopped_immediately_after_create():
    """create STARTS the cluster — migrating 30 would otherwise burn the customer's DBUs."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("cluster", "etl", {"cluster_name": "etl", "spark_version": "14.3.x"})], client)
    res = imp.run()
    assert client.posts_to("clusters/delete"), "the cluster was left RUNNING after create"
    assert "stopped immediately" in res.units[0]["note"]


def test_a_pinned_cluster_is_repinned():
    """Unpinned, a terminated cluster disappears from the UI once it is cleaned up."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("cluster", "etl", {"cluster_name": "etl", "pinned": True})], client)
    imp.run()
    assert client.posts_to("clusters/pin"), "a pinned source cluster was not re-pinned"


def test_pool_and_policy_are_remapped_by_name_to_target_ids():
    """Source ids mean nothing on target; the natural key is the only stable link."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("instance_pool", "shared-pool", {"instance_pool_name": "shared-pool"},
              source_id="SRC-POOL"),
        _unit("cluster_policy", "std-policy", {"name": "std-policy", "definition": "{}"},
              source_id="SRC-POL"),
        _unit("cluster", "etl", {"cluster_name": "etl", "instance_pool_id": "SRC-POOL",
                                 "policy_id": "SRC-POL"}),
    ], client)
    res = imp.run()
    assert res.created == 3
    body = client.bodies_to("clusters/create")[0]
    assert body["instance_pool_id"] == "pool-1", "the pool id was not remapped to the target id"
    assert body["policy_id"] == "pol-2", "the policy id was not remapped to the target id"


def test_cluster_policy_definition_pool_id_is_remapped():
    """PLAN 8 Bug 9: a policy that FIXES instance_pool_id to a SOURCE pool id rejects every cluster
    under it, so the ids INSIDE the definition must be remapped through the pool map too — not just
    the ids in the cluster spec."""
    client = RecordingClient()
    definition = json.dumps({"instance_pool_id": {"type": "fixed", "value": "SRC-POOL"},
                             "spark_version": {"type": "fixed", "value": "13.3.x"}})
    imp, _st = _make(ComputeImporter, [
        _unit("instance_pool", "shared-pool", {"instance_pool_name": "shared-pool"},
              source_id="SRC-POOL"),
        _unit("cluster_policy", "std", {"name": "std", "definition": definition}, source_id="SRC-POL"),
    ], client)
    res = imp.run()
    assert res.created == 2
    got = json.loads(client.bodies_to("policies/clusters/create")[0]["definition"])
    assert got["instance_pool_id"]["value"] == "pool-1", "the PINNED pool id must be remapped"
    assert got["spark_version"]["value"] == "13.3.x", "non-id fields are untouched"


def test_cluster_policy_pinning_a_pool_not_in_the_bundle_fails_loud():
    """PLAN 11 Finding-10: a policy pinning a pool that is NOT in the bundle FAILS LOUD — never the
    old "keep the source pool id + warn" (which let the policy reject every cluster under it), and
    never a silent substitution. Lift-and-shift only remaps to the pool we recreated."""
    client = RecordingClient()
    definition = json.dumps({"driver_instance_pool_id": {"type": "fixed", "value": "GONE"}})
    imp, st = _make(ComputeImporter, [
        _unit("cluster_policy", "std", {"name": "std", "definition": definition})], client)
    res = imp.run()
    assert res.created == 0 and res.failed == 1
    row = st.row("cluster_policy", "std")
    assert row["failure_category"] == "dependency_unresolved"
    assert "not available on source" in row["last_error"]


def test_cluster_policy_pinning_an_in_bundle_pool_not_yet_created_is_retryable():
    """PLAN 11 Finding-10: a pinned pool that IS in the bundle but not yet on target is a RETRYABLE
    prerequisite (heals on retry_mode=failed_only), distinct from the hard not-in-bundle case."""
    client = RecordingClient()
    definition = json.dumps({"driver_instance_pool_id": {"type": "fixed", "value": "SRC-POOL"}})
    imp, st = _make(ComputeImporter, [
        _unit("instance_pool", "pool-a", {"instance_pool_name": "pool-a"}, source_id="SRC-POOL"),
        _unit("cluster_policy", "std", {"name": "std", "definition": definition})], client)
    # The pool IS in the bundle (so source_id → natural_key resolves) but is NOT created this run
    # (narrowed out of the work list), so it has no target id yet — the in-bundle-not-yet case.
    imp.retry_keys = {("cluster_policy", "std")}
    res = imp.run()
    row = st.row("cluster_policy", "std")
    assert row["failure_category"] == "prerequisite_missing"
    assert "retry_mode=failed_only" in row["last_error"]


def test_node_types_are_stripped_when_a_pool_is_set():
    """`node_type_id` alongside `instance_pool_id` is rejected — the pool dictates the node type."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("instance_pool", "p", {"instance_pool_name": "p"}, source_id="SP"),
        _unit("cluster", "etl", {"cluster_name": "etl", "instance_pool_id": "SP",
                                 "node_type_id": "Standard_DS3_v2",
                                 "driver_node_type_id": "Standard_DS3_v2",
                                 "enable_elastic_disk": True}),
    ], client)
    imp.run()
    body = client.bodies_to("clusters/create")[0]
    for field in ("node_type_id", "driver_node_type_id", "enable_elastic_disk"):
        assert field not in body, f"{field} must not be sent with instance_pool_id"


def test_a_cluster_pool_not_in_the_bundle_fails_loud_not_dropped():
    """PLAN 11 Finding-10: a cluster whose pool is NOT in the bundle FAILS LOUD — the old behaviour
    silently DROPPED the pool and created a mis-configured cluster. Lift-and-shift never drops a
    reference; it recreates the object or fails."""
    client = RecordingClient()
    imp, st = _make(ComputeImporter, [
        _unit("cluster", "etl", {"cluster_name": "etl", "instance_pool_id": "GONE"})], client)
    res = imp.run()
    assert client.bodies_to("clusters/create") == [], "the cluster must NOT be created mis-configured"
    assert res.created == 0 and res.failed == 1
    row = st.row("cluster", "etl")
    assert row["failure_category"] == "dependency_unresolved"
    assert "not available on source" in row["last_error"]


def test_the_source_creator_is_preserved_as_a_tag():
    """The API attributes the cluster to the caller, so the creator can only survive as a tag."""
    client = RecordingClient()
    imp, _st = _make(ComputeImporter, [
        _unit("cluster", "etl", {"cluster_name": "etl",
                                 "creator_user_name": "someone@corp.com"})], client)
    imp.run()
    body = client.bodies_to("clusters/create")[0]
    assert body["custom_tags"]["OriginalCreator"] == "someone@corp.com"
    assert "creator_user_name" not in body, "creator_user_name is not a create field"


def test_instance_pool_update_sends_the_full_config_with_the_id():
    """instance-pools/edit is NOT a partial update — omitting a field resets it."""
    client = RecordingClient()
    imp, st = _make(ComputeImporter, [
        _unit("instance_pool", "p", {"instance_pool_name": "p", "min_idle_instances": 5},
              fingerprint="sha256:v2")], client)
    st.record("instance_pool", "p", action="created", fingerprint="sha256:v1",
              target_object_id="pool-existing")
    imp.existing_keys = lambda: {"p": "pool-existing"}
    imp.run()
    body = client.bodies_to("instance-pools/edit")[0]
    assert body["instance_pool_id"] == "pool-existing"
    assert body["min_idle_instances"] == 5, "the full config must be sent, not just the id"


# ═══════════════════════════════ WORKSPACE ══════════════════════════════════

def test_a_user_home_directory_is_reported_as_a_prerequisite_not_mkdird():
    """`/Users/<email>` appears only when the USER is provisioned — that's why identity is phase 1."""
    assert is_user_home("/Users/a@b.com")
    assert not is_user_home("/Users/a@b.com/sub")
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("directory", "/Users/missing@corp.com", {"path": "/Users/missing@corp.com"})], client)
    res = imp.run()
    assert client.posts_to("workspace/mkdirs") == [], "a home directory must never be mkdir'd"
    assert res.failed == 1
    row = st.row("directory", "/Users/missing@corp.com")
    assert row["failure_category"] == "prerequisite_missing"
    assert "provisioned" in row["last_error"]


_OLD_APP = "9e15fb97-bd21-4abc-9def-0123456789ab"
_NEW_APP = "11112222-3333-4444-5555-666677778888"


def test_sp_home_content_is_remapped_to_the_new_application_id():
    """IMP-6: a recreated SP gets a NEW applicationId, so its home `/Users/<oldAppId>/...` must be
    rewritten to `/Users/<newAppId>/...` — otherwise content lands in a directory that can never
    exist (the two failed dirs). The home ROOT is a skip (auto-provisioned at SP create); content
    beneath it is created/uploaded at the remapped path."""
    idmap = {"sp_mapping": {_OLD_APP: _NEW_APP}}
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("directory", f"/Users/{_OLD_APP}", {"path": f"/Users/{_OLD_APP}"}),
        _unit("directory", f"/Users/{_OLD_APP}/proj", {"path": f"/Users/{_OLD_APP}/proj"}),
        _unit("notebook", f"/Users/{_OLD_APP}/proj/nb",
              {"path": f"/Users/{_OLD_APP}/proj/nb", "language": "PYTHON"},
              content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map=idmap)
    res = imp.run()
    assert res.failed == 0, "nothing should fail once the SP home is remapped"
    # home root: skipped (auto-provisioned), not mkdir'd
    mkdirs = [b["path"] for b in client.bodies_to("workspace/mkdirs")]
    assert f"/Users/{_OLD_APP}" not in mkdirs, "the OLD-appId home root must never be mkdir'd"
    assert f"/Users/{_NEW_APP}/proj" in mkdirs, "content dir must be created under the NEW appId"
    # notebook uploaded to the remapped path
    nb = client.bodies_to("workspace/import")[0]
    assert nb["path"] == f"/Users/{_NEW_APP}/proj/nb"
    # and every OLD-appId path is gone from what we sent
    assert not any(_OLD_APP in b.get("path", "") for b in client.bodies_to("workspace/import"))


def test_an_unmigrated_sp_home_is_a_clear_prerequisite_not_a_bare_failure():
    """IMP-6: an SP home whose applicationId is NOT in the identity map (the SP was never migrated)
    can't be created — say so plainly, distinct from a user-home prerequisite."""
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("directory", f"/Users/{_OLD_APP}", {"path": f"/Users/{_OLD_APP}"})], client,
        identity_map={"sp_mapping": {}})
    res = imp.run()
    assert client.posts_to("workspace/mkdirs") == []
    assert res.failed == 1
    row = st.row("directory", f"/Users/{_OLD_APP}")
    assert row["failure_category"] == "prerequisite_missing"
    assert "SERVICE PRINCIPAL home" in row["last_error"]


def test_content_under_an_absent_user_home_is_a_clean_prerequisite_not_a_raw_error():
    """PLAN 8 Bug 8/14: a subdir/notebook under a user home whose owner is ABSENT on target must be
    ONE clean prerequisite_missing per unit — not the raw DIRECTORY_PROTECTED / parent-missing
    api_error per descendant that swamped the RIL failure list (≈264 of 297 failures)."""
    client = RecordingClient()   # get-status raises RESOURCE_DOES_NOT_EXIST → home reads as absent
    imp, st = _make(WorkspaceImporter, [
        _unit("directory", "/Users/ghost@x.com/proj", {"path": "/Users/ghost@x.com/proj"}),
        _unit("notebook", "/Users/ghost@x.com/proj/nb",
              {"path": "/Users/ghost@x.com/proj/nb", "language": "PYTHON"}, content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}})
    res = imp.run()
    assert res.failed == 2
    assert client.posts_to("workspace/mkdirs") == [], "must NOT attempt mkdirs under an absent home"
    for row in res.units:
        assert row["failure_category"] == "prerequisite_missing"
        assert "owner is not present on target" in row["note"]


def _write_classification(aw, sp_app_ids):
    from src.exporters import bundle_paths as BP
    aw.write_json(BP.IDENTITY_CLASSIFICATION_JSON, {"identities": [
        {"identity_type": "service_principal", "applicationId": a} for a in sp_app_ids]})


def test_orphan_sp_home_message_distinguishes_not_migrated_from_deleted():
    """A2: the orphan-SP-home message must say WHY the appId is missing. If the appId IS in the
    source roster it was skipped/filtered this run; if it is ABSENT it was deleted in source — the
    operator's next step differs, so the two must read differently."""
    # (a) appId present in the source roster but NOT in sp_mapping → "present in source but not
    #     migrated this run".
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("directory", f"/Users/{_OLD_APP}", {"path": f"/Users/{_OLD_APP}"})], client,
        identity_map={"sp_mapping": {}})
    _write_classification(imp.staging, [_OLD_APP])
    imp.run()
    row = st.row("directory", f"/Users/{_OLD_APP}")
    assert "present in the source roster" in row["last_error"]
    assert "deleted in source" not in row["last_error"]

    # (b) appId ABSENT from the roster, with backup DISABLED → "deleted in source" prerequisite.
    #     (With backup enabled — the default — an absent owner is diverted to /Users_Backup instead;
    #     that path is covered by test_orphaned_sp_home_content_diverted_to_backup below.)
    client2 = RecordingClient()
    imp2, st2 = _make(WorkspaceImporter, [
        _unit("directory", f"/Users/{_OLD_APP}", {"path": f"/Users/{_OLD_APP}"})], client2,
        identity_map={"sp_mapping": {}}, imports_extra={"workspace_home_backup": False})
    _write_classification(imp2.staging, ["some-other-appid-1234-5678-9abc-def012345678"])
    imp2.run()
    row2 = st2.row("directory", f"/Users/{_OLD_APP}")
    assert "deleted in source" in row2["last_error"]


# ═══════════════════ PLAN 9 — orphaned-home backup (/Users_Backup) ═══════════════════

def _write_roster(aw, *, users=(), sps=()):
    """Write identity_classification.json with the given user home-owners (userName/email) and SP
    applicationIds — the SOURCE roster the divert decision reads."""
    from src.exporters import bundle_paths as BP
    idents = [{"identity_type": "user", "userName": u,
               "email": (u if "@" in u else f"{u}@corp.com")} for u in users]
    idents += [{"identity_type": "service_principal", "applicationId": a} for a in sps]
    aw.write_json(BP.IDENTITY_CLASSIFICATION_JSON, {"identities": idents})


def test_orphaned_user_home_content_diverted_to_backup():
    """PLAN 9: a user DELETED in source (absent from the roster) leaves orphaned home content — it
    cannot be recreated under /Users/, so it is diverted to /Users_Backup/<owner>/… and recorded
    created_with_warning, so no bytes are dropped."""
    client = RecordingClient()   # nothing exists on target; get-status raises
    imp, st = _make(WorkspaceImporter, [
        _unit("directory", "/Users/gone@x.com", {"path": "/Users/gone@x.com"}),
        _unit("directory", "/Users/gone@x.com/proj", {"path": "/Users/gone@x.com/proj"}),
        _unit("notebook", "/Users/gone@x.com/proj/nb",
              {"path": "/Users/gone@x.com/proj/nb", "language": "PYTHON"}, content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}})
    _write_roster(imp.staging, users=["someone.else@x.com"])   # gone@x.com is NOT in the roster
    res = imp.run()
    assert res.failed == 0, "orphaned content must be diverted, never failed"
    assert res.warned == 3, "root + subdir + notebook all diverted as created_with_warning"
    mkdirs = [b["path"] for b in client.bodies_to("workspace/mkdirs")]
    assert "/Users_Backup/gone@x.com" in mkdirs
    assert "/Users_Backup/gone@x.com/proj" in mkdirs
    assert imp.client.bodies_to("workspace/import")[0]["path"] == "/Users_Backup/gone@x.com/proj/nb"
    row = st.row("notebook", "/Users/gone@x.com/proj/nb")   # keyed by SOURCE path (idempotency)
    assert row["last_action"] == "created_with_warning"
    assert row["target_object_id"] == "/Users_Backup/gone@x.com/proj/nb"
    assert "deleted in source" in row["last_error"]


def test_orphaned_sp_home_content_diverted_to_backup():
    """An SP deleted in source (absent appId) has the SAME divert — /Users_Backup/<appId>/…."""
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("workspace_file", f"/Users/{_OLD_APP}/f",
              {"path": f"/Users/{_OLD_APP}/f"}, content_ref="c/f.txt"),
    ], client, staging_files={"c/f.txt": b"data"}, identity_map={"sp_mapping": {}})
    _write_roster(imp.staging, sps=["11110000-0000-0000-0000-000000000000"])   # not _OLD_APP
    res = imp.run()
    assert res.failed == 0 and res.warned == 1
    assert client.bodies_to("workspace/import")[0]["path"] == f"/Users_Backup/{_OLD_APP}/f"


def test_home_owner_in_roster_but_import_failed_stays_prerequisite():
    """PLAN 9 §2: an owner PRESENT in the roster whose home is merely absent on target this run
    (identity import failed/pending) must NOT be diverted — it recovers into the REAL home on
    retry_mode=failed_only. Diverting it would scatter a live user's files."""
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("notebook", "/Users/live@x.com/nb",
              {"path": "/Users/live@x.com/nb", "language": "PYTHON"}, content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}})
    _write_roster(imp.staging, users=["live@x.com"])   # owner IS in the roster
    res = imp.run()
    assert res.failed == 1 and res.warned == 0
    assert client.posts_to("workspace/mkdirs") == [], "must not divert an in-roster owner"
    row = st.row("notebook", "/Users/live@x.com/nb")
    assert row["failure_category"] == "prerequisite_missing"
    assert "owner is not present on target" in row["last_error"]


def test_recreated_sp_home_still_remaps_to_new_appid_not_backup():
    """sp_mapping wins over backup: a recreated SP's home follows its NEW appId (IMP-6), never the
    backup root, even if the OLD appId is absent from the roster."""
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("notebook", f"/Users/{_OLD_APP}/proj/nb",
              {"path": f"/Users/{_OLD_APP}/proj/nb", "language": "PYTHON"}, content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"},
        identity_map={"sp_mapping": {_OLD_APP: _NEW_APP}})
    _write_roster(imp.staging, sps=[])   # OLD appId absent, but sp_mapping still wins
    res = imp.run()
    assert res.failed == 0 and res.warned == 0
    path = client.bodies_to("workspace/import")[0]["path"]
    assert path == f"/Users/{_NEW_APP}/proj/nb"
    assert "Users_Backup" not in path


def test_present_user_home_uses_real_home_not_backup():
    """When the owner's home IS provisioned on target, content lands there — never diverted, even
    if the owner is absent from the roster (present beats roster)."""
    client = RecordingClient(status_paths={"/Users/real@x.com"})   # the real home exists
    imp, _st = _make(WorkspaceImporter, [
        _unit("notebook", "/Users/real@x.com/nb",
              {"path": "/Users/real@x.com/nb", "language": "PYTHON"}, content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}})
    _write_roster(imp.staging, users=[])
    res = imp.run()
    assert res.failed == 0 and res.warned == 0
    assert client.bodies_to("workspace/import")[0]["path"] == "/Users/real@x.com/nb"


def test_workspace_home_backup_false_preserves_current_prerequisite_behaviour():
    """With the flag OFF, an orphaned home reverts to today's prerequisite_missing (no divert)."""
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("notebook", "/Users/gone@x.com/nb",
              {"path": "/Users/gone@x.com/nb", "language": "PYTHON"}, content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}},
        imports_extra={"workspace_home_backup": False})
    _write_roster(imp.staging, users=[])   # gone@x.com absent → would divert if flag were on
    res = imp.run()
    assert res.failed == 1 and res.warned == 0
    assert client.posts_to("workspace/mkdirs") == []
    assert st.row("notebook", "/Users/gone@x.com/nb")["failure_category"] == "prerequisite_missing"


def test_unknown_roster_does_not_silently_divert():
    """No/garbled classification file → roster 'unknown' → prerequisite, NEVER a silent backup: we
    must not scatter a possibly-live user's files on a missing roster."""
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("notebook", "/Users/ghost@x.com/nb",
              {"path": "/Users/ghost@x.com/nb", "language": "PYTHON"}, content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}})
    # deliberately DO NOT write identity_classification.json
    res = imp.run()
    assert res.failed == 1 and res.warned == 0
    assert client.posts_to("workspace/mkdirs") == []
    assert st.row("notebook", "/Users/ghost@x.com/nb")["failure_category"] == "prerequisite_missing"


def test_backup_root_is_configurable_and_normalised():
    """A custom backup root is honoured, and validate() normalises a trailing/leading slash."""
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("notebook", "/Users/gone@x.com/nb",
              {"path": "/Users/gone@x.com/nb", "language": "PYTHON"}, content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}},
        imports_extra={"workspace_home_backup_root": "/Shared/HomeBackups"})
    _write_roster(imp.staging, users=[])
    imp.run()
    assert client.bodies_to("workspace/import")[0]["path"] == "/Shared/HomeBackups/gone@x.com/nb"
    # normalisation happens in validate(): leading / added, trailing / stripped.
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "1",
                            "target_staging_location": "/Volumes/c/s/v", "dry_run": True,
                            "imports": {"workspace_home_backup_root": "Users_Backup/"}})
    cfg.validate()
    assert cfg.imports.workspace_home_backup_root == "/Users_Backup"


def test_backup_subtree_hierarchy_preserved():
    """Nested orphaned dirs land parents-first under the backup root (depth-sorted load)."""
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("directory", "/Users/gone@x.com/a/b", {"path": "/Users/gone@x.com/a/b"}),
        _unit("directory", "/Users/gone@x.com", {"path": "/Users/gone@x.com"}),
        _unit("directory", "/Users/gone@x.com/a", {"path": "/Users/gone@x.com/a"}),
    ], client, identity_map={"sp_mapping": {}})
    _write_roster(imp.staging, users=[])
    imp.run()
    mkdirs = [b["path"] for b in client.bodies_to("workspace/mkdirs")]
    assert mkdirs.index("/Users_Backup/gone@x.com") < mkdirs.index("/Users_Backup/gone@x.com/a")
    assert mkdirs.index("/Users_Backup/gone@x.com/a") < mkdirs.index("/Users_Backup/gone@x.com/a/b")


def test_rerun_adopts_backup_copy_not_duplicated():
    """existing_keys probes the RESOLVED (backup) path, so a re-run ADOPTS the backup copy rather
    than re-uploading it — idempotency with natural_key=source path, target_id=backup path."""
    client = RecordingClient(status_paths={"/Users_Backup/gone@x.com/nb"})
    imp, st = _make(WorkspaceImporter, [
        _unit("notebook", "/Users/gone@x.com/nb",
              {"path": "/Users/gone@x.com/nb", "language": "PYTHON"}, content_ref="c/nb.py"),
    ], client, staging_files={"c/nb.py": b"print(1)"}, identity_map={"sp_mapping": {}})
    _write_roster(imp.staging, users=[])
    res = imp.run()
    assert client.bodies_to("workspace/import") == [], "an existing backup copy must be adopted, not re-uploaded"
    assert res.adopted == 1
    assert st.row("notebook", "/Users/gone@x.com/nb")["target_object_id"] == "/Users_Backup/gone@x.com/nb"


def test_roster_status_indexes_users_by_username_and_email_and_sps_by_appid():
    """_roster_status resolves a home owner by SP applicationId AND by user userName/email."""
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [], client, identity_map={"sp_mapping": {}})
    from src.exporters import bundle_paths as BP
    imp.staging.write_json(BP.IDENTITY_CLASSIFICATION_JSON, {"identities": [
        {"identity_type": "user", "userName": "alice", "email": "alice@corp.com"},
        {"identity_type": "service_principal", "applicationId": _OLD_APP},
    ]})
    assert imp._roster_status("alice") == "in_roster"
    assert imp._roster_status("alice@corp.com") == "in_roster"
    assert imp._roster_status(_OLD_APP) == "in_roster"
    assert imp._roster_status("nobody@corp.com") == "absent"


def test_workspace_roots_are_skipped_not_created():
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("directory", "/Shared", {"path": "/Shared"}),
        _unit("directory", "/Repos", {"path": "/Repos"}),
    ], client)
    res = imp.run()
    assert client.posts_to("workspace/mkdirs") == []
    assert res.created == 2 and "exists by construction" in res.units[0]["note"]


def test_directories_are_created_top_down():
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("directory", "/Shared/a/b/c", {"path": "/Shared/a/b/c"}),
        _unit("directory", "/Shared/a", {"path": "/Shared/a"}),
        _unit("directory", "/Shared/a/b", {"path": "/Shared/a/b"}),
    ], client)
    paths = [safe_get(u) for u in imp.load()]
    assert paths == ["/Shared/a", "/Shared/a/b", "/Shared/a/b/c"]


def safe_get(unit):
    return unit["natural_key"]


def test_a_notebook_is_imported_as_a_NOTEBOOK_with_its_language():
    """Without format=SOURCE + language a `.py` lands as an opaque FILE and nothing runs."""
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("notebook", "/Shared/etl", {"path": "/Shared/etl", "language": "SQL"},
              content_ref="export/workspace/content/etl.sql")], client,
        staging_files={"export/workspace/content/etl.sql": b"SELECT 1"})
    res = imp.run()
    body = client.bodies_to("workspace/import")[0]
    assert body["format"] == "SOURCE"
    assert body["language"] == "SQL"
    assert body["object_type"] == "NOTEBOOK"
    assert base64.b64decode(body["content"]) == b"SELECT 1"
    assert res.created == 1


def test_a_workspace_file_is_imported_verbatim_as_AUTO():
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("workspace_file", "/Shared/data.csv", {"path": "/Shared/data.csv"},
              content_ref="export/workspace/content/data.bin")], client,
        staging_files={"export/workspace/content/data.bin": b"a,b\n1,2\n"})
    imp.run()
    body = client.bodies_to("workspace/import")[0]
    assert body["format"] == "AUTO", "a workspace file must not be interpreted as a notebook"
    assert "language" not in body


def test_bundle_content_is_never_uploaded():
    """Importing bundle STATE points the customer's next `bundle deploy` at SOURCE object ids."""
    client = RecordingClient()
    imp, _st = _make(WorkspaceImporter, [
        _unit("notebook", "/Shared/.bundle/b/files/nb", {"path": "/Shared/.bundle/b/files/nb"},
              import_action="dab_redeploy", migration_mode="content",
              content_ref="export/workspace/content/nb.py")], client,
        staging_files={"export/workspace/content/nb.py": b"x=1"})
    res = imp.run()
    assert client.posts_to("workspace/import") == [], "bundle content must never be uploaded"
    assert res.skipped == 1


def test_a_unit_with_no_exported_bytes_is_a_manual_copy_not_an_empty_file():
    """Uploading an empty file would look successful while losing the content."""
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("notebook", "/Shared/huge", {"path": "/Shared/huge"}, content_ref="")], client)
    res = imp.run()
    assert client.posts_to("workspace/import") == []
    assert res.failed == 1
    assert "not in the bundle" in st.row("notebook", "/Shared/huge")["last_error"]


def test_repos_are_recorded_manual_and_never_created():
    """D9: repos are out of scope for import, but must stay a countable, reconcilable line."""
    client = RecordingClient()
    imp, st = _make(WorkspaceImporter, [
        _unit("repo", "/Repos/me/app", {"url": "https://github.com/o/app", "branch": "main"},
              import_action="manual", note="git repos are OUT OF SCOPE for import")], client)
    res = imp.run()
    assert client.posts_to("api/2.0/repos") == []
    assert res.manual == 1
    assert st.row("repo", "/Repos/me/app")["last_action"] == "manual"


# ═══════════════════════════════ SECRETS ════════════════════════════════════

def test_a_databricks_scope_is_created_with_manage_at_create_time():
    """`users:MANAGE` cannot be patched later — getting it wrong means delete + recreate."""
    client = RecordingClient()
    imp, _st = _make(SecretsImporter, [
        _unit("secret_scope", "app-secrets",
              {"name": "app-secrets", "backend_type": "DATABRICKS", "key_names": ["k1", "k2"]})],
        client, context={"secret_acls": {"app-secrets": [{"principal": "users",
                                                          "permission": "MANAGE"}]}})
    res = imp.run()
    body = client.bodies_to("secrets/scopes/create")[0]
    assert body["initial_manage_principal"] == "users"
    assert "2 secret VALUE(s) are NOT migratable" in res.units[0]["note"]


def test_a_named_manage_principal_is_deferred_to_an_acl_put_not_sent_at_create():
    """`initial_manage_principal` accepts ONLY `users` — a named principal must not be sent there.

    Regression (live 2026-08-06): the source scopes were MANAGEd by specific users, so the importer
    put those names in `initial_manage_principal` and the API rejected every one —
    `400 BAD_REQUEST Cannot specify <user> as initial_manage_principal` — failing 3 of 4 scopes.
    Verified against the real API that even the CALLER's own username is refused; `users` is the
    only accepted literal. The named grant is therefore applied afterwards via `secrets/acls/put`,
    which (unlike `users:MANAGE`) genuinely can be patched later.
    """
    # the MANAGE holder is read from the BUNDLE (export/acls.json), not from cross-phase context:
    # the ACL phase runs last, so the scope phase has to read it from the bundle directly.
    acls = json.dumps([{"asset_type": "secret_scope", "natural_key": "app-secrets",
                        "grants": [{"principal": "alice@corp.com", "permission_level": "MANAGE"}]}])
    client = RecordingClient()
    imp, _st = _make(SecretsImporter, [
        _unit("secret_scope", "app-secrets",
              {"name": "app-secrets", "backend_type": "DATABRICKS", "key_names": ["k1"]})],
        client, staging_files={"export/acls.json": acls.encode()})
    res = imp.run()

    body = client.bodies_to("secrets/scopes/create")[0]
    assert body["initial_manage_principal"] == "users", \
        f"only `users` may be sent at create, got {body['initial_manage_principal']!r}"
    assert "alice@corp.com" not in str(body), "a named principal must never reach the create body"

    # …and it must still END UP with MANAGE, via the ACL API
    acl_bodies = client.bodies_to("secrets/acls/put")
    assert any(b.get("principal") == "alice@corp.com" and b.get("permission") == "MANAGE"
               and b.get("scope") == "app-secrets" for b in acl_bodies), \
        f"expected a MANAGE acls/put for alice@corp.com, got {acl_bodies}"
    assert res.units[0]["target_id"] == "app-secrets"


def test_an_akv_scope_is_always_a_manual_step_never_attempted(monkeypatch):
    """IMP-4 (reversed 2026-08-08): an AKV-backed scope CANNOT be created from this environment and
    is not automatable — proven live. Creating it needs an Azure AD token that neither a Databricks
    SPN credential (mints a Databricks token, wrong issuer) nor a managed-identity-backed SPN (can
    only use IMDS, unreachable from a private/notebook-only workspace) can produce. So it must NEVER
    be attempted: no scopes/create call, no Azure AD/IMDS call, a clean manual remediation naming the
    vault. This guards against any regression that re-introduces a token-mint attempt."""
    import requests
    # any network attempt would be a bug — make them explode so the test catches it
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no HTTP POST may happen for an AKV scope")))
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no IMDS/HTTP GET may happen for an AKV scope")))

    client = RecordingClient()
    imp, st = _make(SecretsImporter, [
        _unit("secret_scope", "kv-scope",
              {"name": "kv-scope", "backend_type": "AZURE_KEYVAULT",
               "keyvault_metadata": {"dns_name": "https://v.vault.azure.net/",
                                     "resource_id": "/subscriptions/x/v"}})], client)
    res = imp.run()
    assert client.posts_to("secrets/scopes/create") == [], "an AKV scope must never be created here"
    assert res.failed == 1
    err = st.row("secret_scope", "kv-scope")["last_error"]
    assert st.row("secret_scope", "kv-scope")["failure_category"] == "prerequisite_missing"
    # message must be deterministic + actionable: name the vault, say create-by-hand, no false
    # promise of an aad_tenant_id knob (which we proved cannot help).
    assert "CREATE IT BY HAND" in err and "https://v.vault.azure.net/" in err
    assert "userAADToken" in err
    assert "aad_tenant_id" not in err, "must NOT suggest a tenant-id knob — it cannot help"


def test_that_mint_aad_token_no_longer_exists():
    """IMP-4: the AAD token-minting entrypoint was removed — there is no code path to an Azure AD
    token in this deployment, so the function must not exist to be accidentally wired up again."""
    import src.auth.token_manager as tm
    assert not hasattr(tm, "mint_aad_token"), "mint_aad_token must be gone (AKV is manual-only)"


def test_a_secret_scope_has_no_edit_api_so_a_change_is_reported_not_applied():
    """Recreating a scope would DELETE every value in it — worse than an honest report."""
    client = RecordingClient()
    imp, st = _make(SecretsImporter, [
        _unit("secret_scope", "s", {"name": "s", "backend_type": "DATABRICKS",
                                    "key_names": ["a", "b"]}, fingerprint="sha256:v2")], client)
    st.record("secret_scope", "s", action="created", fingerprint="sha256:v1", target_object_id="s")
    imp.existing_keys = lambda: {"s": "s"}
    res = imp.run()
    assert client.posts_to("secrets/scopes/create") == []
    assert res.warned == 1
    assert "no edit API exists" in res.units[0]["note"]


# ═══════════════════════════════ JOBS ═══════════════════════════════════════

def _job_unit(name, settings, **over):
    return _unit("job", name, settings, **over)


def test_compute_is_remapped_in_BOTH_job_clusters_and_tasks():
    """Missing either leaves the job pointing at a SOURCE cluster id, failing only at run time."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("etl", {
            "name": "etl",
            "job_clusters": [{"job_cluster_key": "jc",
                              "new_cluster": {"policy_id": "SRC-POL", "spark_version": "14.3.x"}}],
            "tasks": [{"task_key": "t1", "existing_cluster_id": "SRC-CLU"},
                      {"task_key": "t2",
                       "new_cluster": {"instance_pool_id": "SRC-POOL",
                                       "node_type_id": "Standard_DS3_v2"}}],
        })], client, context={
            "cluster_target_ids": {"etl-cluster": "TGT-CLU"},
            "cluster_policy_target_ids": {"std": "TGT-POL"},
            "instance_pool_target_ids": {"pool": "TGT-POOL"}})
    # source_id → natural_key comes from the bundle's own units
    imp.units_by_type["cluster"] = [_unit("cluster", "etl-cluster", source_id="SRC-CLU")]
    imp.units_by_type["cluster_policy"] = [_unit("cluster_policy", "std", source_id="SRC-POL")]
    imp.units_by_type["instance_pool"] = [_unit("instance_pool", "pool", source_id="SRC-POOL")]

    imp.run()
    body = client.bodies_to("jobs/create")[0]
    assert body["tasks"][0]["existing_cluster_id"] == "TGT-CLU", "task cluster not remapped"
    assert body["job_clusters"][0]["new_cluster"]["policy_id"] == "TGT-POL", \
        "job_clusters policy not remapped"
    assert body["tasks"][1]["new_cluster"]["instance_pool_id"] == "TGT-POOL"
    assert "node_type_id" not in body["tasks"][1]["new_cluster"], \
        "node type must be dropped when a pool is set"


def test_schedule_AND_continuous_are_both_paused():
    """Pausing only `schedule` lets a CONTINUOUS job run against half-migrated data immediately."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j",
                        "schedule": {"quartz_cron_expression": "0 0 1 * * ?",
                                     "pause_status": "UNPAUSED"},
                        "continuous": {"pause_status": "UNPAUSED"},
                        "trigger": {"pause_status": "UNPAUSED"}})], client)
    imp.run()
    body = client.bodies_to("jobs/create")[0]
    assert body["schedule"]["pause_status"] == "PAUSED"
    assert body["continuous"]["pause_status"] == "PAUSED", "a continuous job was left RUNNING"
    assert body["trigger"]["pause_status"] == "PAUSED"


def test_run_as_service_principal_is_remapped_through_the_sp_map():
    """The reference tool does NOT do this; an unmapped run_as names an appId absent on target."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "run_as": {"service_principal_name": "old-app"}})], client)
    imp.identity_map = {"sp_mapping": {"old-app": "new-app"}}
    imp.run()
    body = client.bodies_to("jobs/create")[0]
    assert body["run_as"]["service_principal_name"] == "new-app"


def test_an_unmapped_run_as_is_warned_about_rather_than_silently_left():
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "run_as": {"service_principal_name": "unknown-app"}})], client)
    res = imp.run()
    assert res.warned == 1
    assert "not in the identity map" in res.units[0]["note"]


def test_an_unresolvable_notebook_path_is_created_with_a_warning():
    """THE time-bomb check (D14): the Jobs API accepts a bad path and the job fails at FIRST RUN, so
    a create-failure check alone would never catch it."""
    client = RecordingClient()
    imp, st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "tasks": [
            {"task_key": "t1",
             "notebook_task": {"notebook_path": "/Repos/me/gitfolder/nb"}}]})], client)
    res = imp.run()
    assert client.posts_to("jobs/create"), "the job must still be created — the path may be intended"
    assert res.warned == 1
    note = res.units[0]["note"]
    assert "FAIL AT FIRST RUN" in note and "Git folder" in note
    assert st.row("job", "j")["last_action"] == "created_with_warning"


def test_a_resolvable_notebook_path_produces_no_warning():
    client = RecordingClient(status_paths={"/Shared/etl"})
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "tasks": [
            {"task_key": "t1", "notebook_task": {"notebook_path": "/Shared/etl"}}]})], client,
        context={"workspace_paths": {"/Shared/etl"}})
    res = imp.run()
    assert res.created == 1 and res.warned == 0


def test_external_storage_paths_are_not_flagged():
    """A dbfs:/ or /Volumes/ path is not workspace content, so it must not be reported missing."""
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "tasks": [
            {"task_key": "t", "spark_python_task": {"python_file": "dbfs:/scripts/x.py"}}]})],
        client)
    res = imp.run()
    assert res.created == 1 and res.warned == 0


def test_jobs_existence_check_is_paginated_and_expands_tasks():
    """A truncated jobs list duplicates every job past the first page; without expand_tasks the
    response drops `tasks` entirely."""
    client = RecordingClient(paginated={"api/2.1/jobs/list": [
        {"job_id": "1", "settings": {"name": "existing-job"}}]})
    imp, _st = _make(JobsImporter, [], client)
    found = imp.existing_keys()
    assert found == {"existing-job": "1"}
    call = [c for c in client.calls if c[0] == "GET_PAGINATED"][0]
    assert call[2]["expand_tasks"] == "true"


def test_job_update_uses_reset_which_replaces_settings_wholesale():
    client = RecordingClient()
    imp, st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "max_concurrent_runs": 4}, fingerprint="sha256:v2")], client)
    st.record("job", "j", action="created", fingerprint="sha256:v1", target_object_id="job-existing")
    imp.existing_keys = lambda: {"j": "job-existing"}
    imp.run()
    body = client.bodies_to("jobs/reset")[0]
    assert body["job_id"] == "job-existing"
    assert body["new_settings"]["max_concurrent_runs"] == 4


def test_server_side_echoes_are_stripped_from_the_job_body():
    client = RecordingClient()
    imp, _st = _make(JobsImporter, [
        _job_unit("j", {"name": "j", "created_time": 123, "creator_user_name": "a@b.com",
                        "job_id": "SOURCE-ID"})], client)
    imp.run()
    body = client.bodies_to("jobs/create")[0]
    for field in ("created_time", "creator_user_name", "job_id"):
        assert field not in body, f"{field} is not a create field"
