"""Regression tests for the fingerprint gaps found in the Plan 3 §7c-audit.

These guard the assumption the ENTIRE upsert design rests on: *if a source-side change would
alter the target object, the fingerprint must move.* Every gap below was a silent failure — a
fully green re-run that left the target stale — so each has a test that fails on the pre-fix code.

  • GAP 1 (serious): notebook/workspace-file CONTENT was not fingerprinted at all. The payload is
    only {path, object_type, language}, so editing a notebook's code re-exported to the SAME
    fingerprint → the importer decided SKIP → the target kept the OLD code.
  • GAP 2: an SP's `has_secrets` was collected but never hashed, so adding an OAuth secret to an
    existing SPN never re-surfaced its manual action.
  • Plus the "mutate one field ⇒ fingerprint changes" sensitivity sweep (§11), which is the only
    check that catches an OVER-STRIP: a field wrongly listed in STRIP_FIELDS is invisible to the
    fingerprint forever, and the existing SDK-allowlist check only catches EXTRA fields, never a
    missing one.
"""
from __future__ import annotations

import json
import os
import tempfile

from src.exporters import bundle_paths as BP
from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.export_runner import ExportRunner
from tests.fakes import FakeClient


def _cfg(staging, **over):
    d = {"role": "source", "source_workspace_id": "111", "run_id": "r1",
         "source_staging_location": staging}
    d.update(over)
    return Config.from_dict(d)


def _inventory_with_notebook(code: bytes):
    """A one-notebook inventory plus the client that serves `code` as its bytes."""
    objects = {
        "workspace_object": [
            {"object_type": "NOTEBOOK", "path": "/Shared/nb", "object_id": "n1",
             "language": "PYTHON"},
        ],
    }
    client = FakeClient(download_table={"api/2.0/workspace/export": code})
    return objects, client


def _export_once(staging, objects, client, run_id="r1"):
    """Run only the unit-building + content pass, returning the export index by natural_key."""
    cfg = _cfg(staging, run_id=run_id)
    aw = ArtifactWriter(cfg)
    aw.ensure_output_path()
    aw.write_json(BP.INVENTORY_JSON, {"objects_by_type": objects})
    ExportRunner(client, cfg, aw, content_fetch_workers=2).run()
    index = aw.read_json(BP.EXPORT_INDEX_JSON) or {}
    return {(u["asset_type"], u["natural_key"]): u for u in index.get("units", [])}


# ── GAP 1 — notebook CONTENT must be fingerprinted ─────────────────────────

def test_notebook_content_change_moves_the_fingerprint():
    """THE regression test for GAP 1: same path, different bytes ⇒ different fingerprint.

    Pre-fix this asserted equal hashes, which is precisely why an edited notebook was SKIPped on
    import and the target silently kept the old code.
    """
    objects, client_v1 = _inventory_with_notebook(b"print('version one')")
    with tempfile.TemporaryDirectory() as d1:
        units_v1 = _export_once(d1, objects, client_v1)
    objects, client_v2 = _inventory_with_notebook(b"print('version two - edited')")
    with tempfile.TemporaryDirectory() as d2:
        units_v2 = _export_once(d2, objects, client_v2)

    fp1 = units_v1[("notebook", "/Shared/nb")]["fingerprint"]
    fp2 = units_v2[("notebook", "/Shared/nb")]["fingerprint"]
    assert fp1.startswith("sha256:") and fp2.startswith("sha256:")
    assert fp1 != fp2, ("editing a notebook's CONTENT did not move its fingerprint — the target's "
                        "upsert will SKIP it and keep the OLD code (GAP 1)")


def test_notebook_content_fingerprint_is_stable_when_unchanged():
    """The other half: identical bytes ⇒ identical fingerprint, so an unchanged notebook SKIPs
    rather than being pointlessly re-uploaded on every run."""
    code = b"print('stable')"
    objects, c1 = _inventory_with_notebook(code)
    with tempfile.TemporaryDirectory() as d1:
        units_a = _export_once(d1, objects, c1)
    objects, c2 = _inventory_with_notebook(code)
    with tempfile.TemporaryDirectory() as d2:
        units_b = _export_once(d2, objects, c2)
    assert (units_a[("notebook", "/Shared/nb")]["fingerprint"]
            == units_b[("notebook", "/Shared/nb")]["fingerprint"])


def test_content_hash_is_not_added_to_the_create_payload():
    """`_content_sha256` is a FINGERPRINT INPUT only. Leaking it into `payload` would put a
    non-create field into the body sent to `workspace/import`, which the API rejects."""
    objects, client = _inventory_with_notebook(b"x = 1")
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        aw = ArtifactWriter(cfg)
        aw.ensure_output_path()
        aw.write_json(BP.INVENTORY_JSON, {"objects_by_type": objects})
        ExportRunner(client, cfg, aw, content_fetch_workers=2).run()
        payloads = aw.read_json("export/workspace/objects.json") or {}
    for u in payloads.get("units", []):
        assert "_content_sha256" not in (u.get("payload") or {}), \
            "_content_sha256 leaked into the create payload"


def test_resumed_content_unit_keeps_its_content_fingerprint():
    """A RESUMED unit must restore the content hash from the checkpoint.

    Otherwise the second run re-fingerprints on metadata alone, the hash silently reverts to the
    content-blind value, and GAP 1 comes back for exactly the crash-recovery case §4 warns about.
    """
    code = b"print('resume me')"
    objects, client = _inventory_with_notebook(code)
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        aw = ArtifactWriter(cfg)
        aw.ensure_output_path()
        aw.write_json(BP.INVENTORY_JSON, {"objects_by_type": objects})

        first = ExportRunner(client, cfg, aw, content_fetch_workers=2).run()
        fp_first = {(u["asset_type"], u["natural_key"]): u["fingerprint"]
                    for u in (aw.read_json(BP.EXPORT_INDEX_JSON) or {}).get("units", [])}

        # Second run over the SAME dir: the checkpoint marks the notebook done, so it resumes
        # without re-fetching (a client that would raise proves no fetch happened).
        exploding = FakeClient(download_table={
            "api/2.0/workspace/export": AssertionError("must not re-fetch a resumed unit")})
        ExportRunner(exploding, cfg, aw, content_fetch_workers=2).run()
        fp_second = {(u["asset_type"], u["natural_key"]): u["fingerprint"]
                     for u in (aw.read_json(BP.EXPORT_INDEX_JSON) or {}).get("units", [])}

    key = ("notebook", "/Shared/nb")
    assert fp_second[key] == fp_first[key], \
        "a resumed content unit lost its content hash and re-fingerprinted on metadata only"
    assert first["total"] >= 1


# ── GAP 2 — SP has_secrets must be in the fingerprint input ────────────────

def _sp_unit(has_secrets: bool):
    from src.exporters.asset_export import build_all
    inv = {"identity": [{
        "identity_type": "service_principal", "id": "s1", "applicationId": "app-1",
        "displayName": "sp one", "has_secrets": has_secrets,
        "classification": "db_managed_sp",
        "_raw": {"id": "s1", "applicationId": "app-1", "displayName": "sp one",
                 "entitlements": [{"value": "allow-cluster-create"}]},
    }]}
    return build_all(inv)["service_principal"][0]


def test_sp_oauth_secret_moves_the_fingerprint():
    """GAP 2: creating an OAuth secret on an EXISTING SPN must move the hash, so the state store
    reports `updated` and the "recreate the secret manually" action re-surfaces that run."""
    without, with_ = _sp_unit(False), _sp_unit(True)
    assert without["fingerprint"] != with_["fingerprint"], \
        "adding an OAuth client secret to an SPN did not move its fingerprint (GAP 2)"
    assert "secret" in with_["note"].lower(), "the manual action note is missing"


def test_sp_has_secrets_is_not_a_create_field():
    """`_has_secrets` must not reach the SCIM create payload — it isn't a SCIM attribute."""
    for unit in (_sp_unit(False), _sp_unit(True)):
        assert "_has_secrets" not in unit["payload"]
        assert "has_secrets" not in unit["payload"]


# ── Repos are out of scope for import (D9/§6a) ─────────────────────────────

def test_repo_units_are_manual_and_keep_their_metadata():
    """Repos: never imported (so `manual`, not `auto`), but the metadata IS the manual runbook,
    so the payload must survive."""
    from src.exporters.asset_export import build_all
    inv = {"workspace_object": [{
        "object_type": "REPO", "path": "/Repos/me@co.com/my-repo", "repo_id": "r9",
        "_raw": {"path": "/Repos/me@co.com/my-repo", "url": "https://github.com/org/my-repo",
                 "provider": "gitHub", "branch": "main"},
    }]}
    unit = build_all(inv)["repo"][0]
    assert unit["migration_mode"] == "manual"
    assert unit["import_action"] == "manual"
    assert unit["migratable"] is False
    # the runbook data must still be there
    assert unit["payload"]["url"] == "https://github.com/org/my-repo"
    assert unit["payload"]["provider"] == "gitHub"
    assert "out of scope" in unit["note"].lower()


# ── Fingerprint SENSITIVITY sweep (§11) — catches an OVER-STRIP ────────────

# (asset_type, base payload, field to mutate, mutated value). Each field is one a customer can
# genuinely change on source and expect to see migrate. If a field here is wrongly listed in
# STRIP_FIELDS it becomes invisible to change detection FOREVER — and no allowlist check catches
# that, because an over-strip removes a field rather than adding one.
_SENSITIVITY_CASES = [
    ("cluster_policy", {"name": "p1", "definition": '{"spark_version":{"type":"fixed"}}'},
     "definition", '{"spark_version":{"type":"allowlist"}}'),
    ("cluster_policy", {"name": "p1", "definition": "{}"}, "name", "p1-renamed"),
    ("instance_pool", {"instance_pool_name": "pool", "node_type_id": "Standard_DS3_v2",
                       "min_idle_instances": 1}, "min_idle_instances", 5),
    ("cluster", {"cluster_name": "c1", "spark_version": "14.3.x", "num_workers": 2,
                 "node_type_id": "Standard_DS3_v2"}, "num_workers", 8),
    ("cluster", {"cluster_name": "c1", "spark_version": "14.3.x",
                 "spark_conf": {"a": "1"}}, "spark_conf", {"a": "2"}),
    ("job", {"name": "j1", "tasks": [{"task_key": "t1", "notebook_task": {"notebook_path": "/a"}}]},
     "tasks", [{"task_key": "t1", "notebook_task": {"notebook_path": "/b"}}]),
    ("job", {"name": "j1", "schedule": {"quartz_cron_expression": "0 0 1 * * ?"}},
     "schedule", {"quartz_cron_expression": "0 0 2 * * ?"}),
    ("sql_warehouse", {"name": "wh", "cluster_size": "Small"}, "cluster_size", "Large"),
    ("legacy_query", {"display_name": "q", "query_text": "select 1"},
     "query_text", "select 2"),
    ("alert_v2", {"display_name": "a", "query_text": "select 1"}, "query_text", "select 42"),
    ("dlt_pipeline", {"name": "p", "libraries": [{"notebook": {"path": "/a"}}]},
     "libraries", [{"notebook": {"path": "/b"}}]),
    ("lakeview_dashboard", {"display_name": "d", "serialized_dashboard": '{"pages":[]}'},
     "serialized_dashboard", '{"pages":[{"name":"p"}]}'),
    ("genie_space", {"title": "g", "serialized_space": '{"tables":["a"]}'},
     "serialized_space", '{"tables":["a","b"]}'),
    ("serving_endpoint", {"served_entities": [{"name": "m1"}]},
     "served_entities", [{"name": "m2"}]),
    ("global_init_script", {"name": "gis", "position": 0, "enabled": True,
                            "script_b64": "ZWNobyAx"}, "script_b64", "ZWNobyAy"),
    ("global_init_script", {"name": "gis", "position": 0, "enabled": True,
                            "script_b64": "ZWNobyAx"}, "enabled", False),
    ("workspace_conf", {"key": "enableTokensConfig", "value": "true"}, "value", "false"),
    ("secret_scope", {"name": "s", "backend_type": "DATABRICKS", "key_names": ["k1"]},
     "key_names", ["k1", "k2"]),
    ("secret_scope", {"name": "s", "backend_type": "AZURE_KEYVAULT",
                      "keyvault_metadata": {"dns_name": "https://v1.vault.azure.net/",
                                            "resource_id": "/subscriptions/x/v1"}},
     "keyvault_metadata", {"dns_name": "https://v2.vault.azure.net/",
                           "resource_id": "/subscriptions/x/v2"}),
    ("user", {"userName": "a@b.com", "entitlements": [{"value": "workspace-access"}]},
     "entitlements", [{"value": "workspace-access"}, {"value": "databricks-sql-access"}]),
    ("group", {"displayName": "g1", "members": [{"display": "a@b.com", "value": "1"}]},
     "members", [{"display": "a@b.com", "value": "1"}, {"display": "c@d.com", "value": "2"}]),
    ("group_membership", {"displayName": "admins", "members": [{"display": "a@b.com"}]},
     "members", [{"display": "a@b.com"}, {"display": "b@c.com"}]),
]


def test_fingerprint_is_sensitive_to_every_meaningful_field():
    """Mutate ONE meaningful field per asset_type ⇒ the fingerprint MUST change.

    This is the only check that catches an over-strip. A failure here names an asset_type whose
    field is being silently discarded before hashing — meaning that change would never migrate on
    a re-run, on a green report.
    """
    from src.transform.transforms import fingerprint, strip_runtime
    stale = []
    for asset_type, base, field, mutated in _SENSITIVITY_CASES:
        before = fingerprint(strip_runtime(asset_type, base))
        after = fingerprint(strip_runtime(asset_type, {**base, field: mutated}))
        if before == after:
            stale.append(f"{asset_type}.{field}")
    assert not stale, ("these fields do NOT move the fingerprint, so a source change to them "
                       f"would never migrate on a re-run (over-strip): {stale}")


def test_runtime_fields_do_not_move_the_fingerprint():
    """The converse, which keeps the sweep honest: RUNTIME state must NOT move the hash, or every
    run reports phantom updates (the reason STRIP_FIELDS exists at all)."""
    from src.transform.transforms import fingerprint, strip_runtime
    noisy = [
        ("cluster", {"cluster_name": "c", "spark_version": "14.3.x"},
         {"cluster_id": "0101-x", "state": "TERMINATED", "start_time": 123,
          "creator_user_name": "a@b.com", "last_restarted_time": 9}),
        ("instance_pool", {"instance_pool_name": "p", "node_type_id": "n"},
         {"instance_pool_id": "ip1", "stats": {"idle_count": 3}, "state": "ACTIVE"}),
        ("sql_warehouse", {"name": "w", "cluster_size": "Small"},
         {"id": "w1", "state": "RUNNING", "num_active_sessions": 4, "health": {"status": "OK"}}),
        ("dlt_pipeline", {"name": "p"},
         {"pipeline_id": "p1", "state": "IDLE", "cluster_id": "c1", "last_modified": 5}),
        ("cluster_library", {"cluster_id": "c1", "library": {"pypi": {"package": "requests"}}},
         {"status": "INSTALLED", "is_library_for_all_clusters": False}),
    ]
    churning = []
    for asset_type, base, runtime in noisy:
        clean = fingerprint(strip_runtime(asset_type, base))
        with_runtime = fingerprint(strip_runtime(asset_type, {**base, **runtime}))
        if clean != with_runtime:
            churning.append(asset_type)
    assert not churning, (f"runtime fields leak into the fingerprint for {churning} — every run "
                          "would report a phantom UPDATE")


# ── LATEST_EXPORT.json (§3) ────────────────────────────────────────────────

def test_latest_export_pointer_written_after_manifest_and_ties_to_it():
    """The pointer must exist after a completed export and its `manifest_checksum` must match the
    bundle's manifest — that's how import detects a pointer left over from a DIFFERENT upload."""
    from src.exporters.bundle_state import (manifest_checksum, read_latest_export_pointer,
                                            write_latest_export_pointer)
    objects, client = _inventory_with_notebook(b"print(1)")
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        aw = ArtifactWriter(cfg)
        aw.ensure_output_path()
        aw.write_json(BP.INVENTORY_JSON, {"objects_by_type": objects})
        ExportRunner(client, cfg, aw, content_fetch_workers=2).run()

        pointer = read_latest_export_pointer(cfg)
        assert pointer is not None, "LATEST_EXPORT.json was not written"
        assert pointer["run_id"] == "r1"
        assert pointer["source_workspace_id"] == "111"
        manifest = aw.read_json(BP.MANIFEST_JSON)
        assert pointer["manifest_checksum"] == manifest_checksum(manifest), \
            "pointer checksum does not tie to this bundle's manifest"
        # A pointer for a DIFFERENT bundle must be detectable.
        other = dict(manifest)
        other["run_id"] = "someone_elses_run"
        assert pointer["manifest_checksum"] != manifest_checksum(other)
