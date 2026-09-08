"""LIVE harness: run the real Inventory + Export against the fvm1 workspace (read-only source side).

NOT a unit test — needs the `fvm1` Databricks CLI profile. Builds an ApiClient from the profile's
OAuth token + host (same surface the run-as SP sees), runs InventoryRunner then ExportRunner into a
local temp staging dir, and prints a thorough audit of the bundle so every exported element can be
inspected. Writes NOTHING to the workspace.

Run: python3 -m tests.live_fvm1_export
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from src.auth.token_manager import ApiClient, StaticTokenProvider
from src.config.config_manager import Config
from src.collectors.inventory_runner import InventoryRunner
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.export_runner import ExportRunner

PROFILE = os.environ.get("WSMIG_LIVE_PROFILE", "fvm1")


def _profile() -> dict:
    import configparser
    c = configparser.ConfigParser()
    c.read(os.path.expanduser("~/.databrickscfg"))
    return dict(c[PROFILE])


def _token() -> str:
    out = subprocess.check_output(["databricks", "auth", "token", "-p", PROFILE], text=True)
    return json.loads(out)["access_token"]


def main() -> int:
    prof = _profile()
    host = prof["host"].rstrip("/")
    ws_id = prof.get("workspace_id", "live")
    staging = tempfile.mkdtemp(prefix="wsmig_live_")
    cfg = Config.from_dict({"role": "source", "source_workspace_id": ws_id, "run_id": "live1",
                            "source_staging_location": staging,
                            # keep the live run bounded so it finishes quickly.
                            "max_scim": 0, "max_workspace_items": 400, "max_ws_api_calls": 300})
    cfg.ctx.workspace_url = host
    cfg.ctx.token = _token()
    # The CLI OAuth token can expire during a long run → 403s. Use a refreshing provider (cached
    # ~10 min) so the LIVE HARNESS stays reliable. (Production uses the runtime's managed
    # notebook-context token via StaticTokenProvider — this refresh is a test-harness concern only.)
    import time
    _cache = {"tok": cfg.ctx.token, "ts": time.time()}

    def _fresh_token():
        if time.time() - _cache["ts"] > 600:
            _cache["tok"] = _token()
            _cache["ts"] = time.time()
        return _cache["tok"]

    client = ApiClient(host, _fresh_token)

    aw = ArtifactWriter(cfg)
    print(f"== staging: {staging}")
    print("== running inventory (read-only) ...")
    inv_res = InventoryRunner(client, cfg, aw).run()
    print("   inventory counts:", inv_res["counts"])

    print("== running export ...")
    result = ExportRunner(client, cfg, aw, content_fetch_workers=8).run()
    root = result["output_path"]
    print("   export summary:", {k: result.get(k) for k in
                                 ("total", "success", "failure", "skipped_oversize", "manual", "dab", "skip")})

    _audit(root)
    print("\n== bundle at:", root)
    return 0


def _audit(root: str) -> None:
    index = json.load(open(f"{root}/misc/export_index.json"))
    units = index["units"]
    print(f"\n=== EXPORT INDEX: {len(units)} units ===")
    print("per-asset_type counts (status breakdown):")
    for at in sorted(index["counts"]):
        print(f"  {at:<22} {index['counts'][at]}")

    # reconcile 1:1 against inventory (unit key coverage).
    inv = json.load(open(f"{root}/misc/inventory.json"))
    print("\n=== RECONCILE vs inventory.json ===")
    print("  inventory coarse counts:", inv["counts"])

    # verify every payload file parses + payloads are runtime-stripped where expected.
    print("\n=== PAYLOAD FILE AUDIT ===")
    export_dir = os.path.join(root, "export")
    for dirpath, _d, names in os.walk(export_dir):
        for n in sorted(names):
            if not n.endswith(".json"):
                continue
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root)
            try:
                doc = json.load(open(p))
            except Exception as exc:  # noqa: BLE001
                print(f"  !! {rel}: PARSE FAILED {exc}")
                continue
            if isinstance(doc, dict) and "units" in doc:
                print(f"  {rel}: {len(doc['units'])} units")
            elif isinstance(doc, list):
                print(f"  {rel}: {len(doc)} entries")
            else:
                print(f"  {rel}: (dict)")

    # spot-check runtime stripping on a few asset types.
    print("\n=== RUNTIME-STRIP SPOT CHECKS ===")
    _spot(root, "export/compute/clusters.json", ["cluster_id", "state", "start_time"])
    _spot(root, "export/compute/instance_pools.json", ["instance_pool_id", "stats", "status"])
    _spot(root, "export/sql/warehouses.json", ["id", "state", "num_clusters"])
    _spot(root, "export/jobs.json", None, present=["tasks"])
    _spot(root, "export/dlt/pipelines.json", ["pipeline_id", "state"])
    _strict_create_fields(root)

    # content bytes.
    content_dir = os.path.join(root, "export/workspace/content")
    if os.path.isdir(content_dir):
        files = os.listdir(content_dir)
        print(f"\n=== CONTENT BYTES: {len(files)} files ===")
        for f in files[:15]:
            fp = os.path.join(content_dir, f)
            head = open(fp, "rb").read(80)
            print(f"  {f}  ({os.path.getsize(fp)} B)  head={head[:60]!r}")

    # acls.
    acls = json.load(open(f"{root}/export/acls.json"))
    print(f"\n=== ACLs: {len(acls)} objects with grants ===")
    total_grants = sum(len(e["grants"]) for e in acls)
    print(f"   total grants: {total_grants}")
    for e in acls[:8]:
        print(f"  {e['asset_type']}/{e['natural_key']}: {len(e['grants'])} grants "
              f"(perm_type={e['perm_object_type']})")
        for g in e["grants"][:3]:
            print(f"      {g['principal_type']}:{g['principal']} = {g['permission_level']}"
                  + (" [inherited]" if g["inherited"] else ""))

    # Genie: now auto-migratable with serialized_space.
    genie = [u for u in units if u["asset_type"] == "genie_space"]
    print(f"\n=== GENIE SPACES: {len(genie)} ===")
    gpath = os.path.join(root, "export/genie/spaces.json")
    gpayloads = {}
    if os.path.isfile(gpath):
        gpayloads = {u["natural_key"]: u for u in json.load(open(gpath)).get("units", [])}
    for u in genie:
        pl = gpayloads.get(u["natural_key"], {}).get("payload", {})
        ss = pl.get("serialized_space")
        print(f"  {u['natural_key']}: status={u['export_status']} "
              f"serialized_space={'YES ('+str(len(ss))+' chars)' if ss else 'no'} "
              f"warehouse_id={pl.get('warehouse_id','')}")

    # Dashboard/alert dedup: covered vs native.
    covered = [u for u in units if u["export_status"] == "covered"]
    print(f"\n=== COVERED (native-exported twins, deduped): {len(covered)} ===")
    for u in covered[:10]:
        print(f"  {u['asset_type']}/{u['natural_key']}: {u['note']}")

    # failures + oversize + manual.
    fails = [u for u in units if u["export_status"] == "failure"]
    over = [u for u in units if u["export_status"] == "skipped_oversize"]
    manual = [u for u in units if u["export_status"] == "manual"]
    print(f"\n=== FAILURES: {len(fails)} ===")
    for u in fails[:20]:
        print(f"  {u['asset_type']}/{u['natural_key']}: {u['note']}")
    print(f"=== OVERSIZE: {len(over)} ===")
    for u in over[:20]:
        print(f"  {u['asset_type']}/{u['natural_key']}: {u['note']}")
    print(f"=== MANUAL: {len(manual)} ===  (asset types: "
          f"{sorted({u['asset_type'] for u in manual})})")

    # fingerprint sanity: all present, all sha256.
    bad_fp = [u for u in units if not u["fingerprint"].startswith("sha256:")]
    print(f"\n=== FINGERPRINTS: {len(units)} units, {len(bad_fp)} malformed ===")

    # manifest verify.
    mani = json.load(open(f"{root}/misc/manifest.json"))
    print(f"\n=== MANIFEST: {len(mani['files'])} files, checksummed ===")


# ── strict create-field allowlists ─────────────────────────────────────────
# Every top-level payload key must be a field the TARGET create API actually accepts. These
# lists come from the SDK create signatures (databricks-sdk), not from guesswork — a denylist
# of "known runtime junk" can't catch a field nobody thought to list, which is exactly how
# creator_user_name / driver_healthy / pipeline `id` slipped through before.
CREATE_FIELDS = {
    "cluster": {
        "apply_policy_default_values", "autoscale", "autotermination_minutes", "aws_attributes",
        "azure_attributes", "clone_from", "cluster_log_conf", "cluster_name", "custom_tags",
        "data_security_mode", "docker_image", "driver_instance_pool_id",
        "driver_node_type_flexibility", "driver_node_type_id", "enable_elastic_disk",
        "enable_local_disk_encryption", "gcp_attributes", "init_scripts", "instance_pool_id",
        "is_single_node", "kind", "node_type_id", "num_workers", "policy_id",
        "remote_disk_throughput", "runtime_engine", "single_user_name", "spark_conf",
        "spark_env_vars", "spark_version", "ssh_public_keys", "total_initial_remote_disk_size",
        "use_ml_runtime", "worker_node_type_flexibility", "workload_type",
    },
    "instance_pool": {
        "aws_attributes", "azure_attributes", "custom_tags", "disk_spec", "enable_elastic_disk",
        "gcp_attributes", "idle_instance_autotermination_minutes", "instance_pool_name",
        "max_capacity", "min_idle_instances", "node_type_flexibility", "node_type_id",
        "preloaded_docker_images", "preloaded_spark_versions", "remote_disk_throughput",
        "total_initial_remote_disk_size",
    },
    "cluster_policy": {
        "definition", "description", "libraries", "max_clusters_per_user", "name",
        "policy_family_definition_overrides", "policy_family_id",
    },
    "sql_warehouse": {
        "auto_stop_mins", "channel", "cluster_size", "creator_name", "enable_photon",
        "enable_serverless_compute", "instance_profile_arn", "max_num_clusters",
        "min_num_clusters", "name", "spot_instance_policy", "tags", "warehouse_type",
        "auto_resume",
    },
    "dlt_pipeline": {
        "allow_duplicate_names", "budget_policy_id", "catalog", "channel", "clusters",
        "configuration", "continuous", "deployment", "development", "dry_run", "edition",
        "environment", "event_log", "filters", "gateway_definition", "ingestion_definition",
        "libraries", "name", "notifications", "photon", "restart_window", "root_path", "run_as",
        "schema", "serverless", "storage", "tags", "target", "trigger", "usage_policy_id",
    },
    "legacy_query": {
        "apply_auto_limit", "catalog", "description", "display_name", "parameters",
        "parent_path", "query_text", "run_as_mode", "schema", "tags", "warehouse_id",
    },
    "alert_v2": {
        "custom_description", "custom_summary", "display_name", "evaluation", "parent_path",
        "query_text", "run_as", "schedule", "warehouse_id",
    },
    "lakeview_dashboard": {
        "display_name", "parent_path", "serialized_dashboard", "warehouse_id",
    },
    "repo": {"path", "provider", "sparse_checkout", "url", "branch"},
    "global_init_script": {"enabled", "name", "position", "script", "script_b64"},
}


def _strict_create_fields(root):
    """Assert no payload carries a key the target create API would reject."""
    print("\n=== STRICT CREATE-FIELD CHECK (all units, all payload keys) ===")
    from src.exporters.asset_export import ARTIFACT_PATH
    bad = 0
    for asset_type, allowed in sorted(CREATE_FIELDS.items()):
        rel = ARTIFACT_PATH.get(asset_type)
        p = os.path.join(root, rel) if rel else None
        if not p or not os.path.isfile(p):
            print(f"  {asset_type:<20} (no artifact file)")
            continue
        units = (json.load(open(p)) or {}).get("units", [])
        offenders = {}
        for u in units:
            if u.get("asset_type") != asset_type:
                continue
            for k in (u.get("payload") or {}):
                if k not in allowed:
                    offenders.setdefault(k, []).append(u["natural_key"])
        if offenders:
            bad += len(offenders)
            print(f"  {asset_type:<20} ✗ NON-CREATE FIELDS: "
                  f"{ {k: v[:2] for k, v in offenders.items()} }")
        else:
            print(f"  {asset_type:<20} ✓ clean ({len(units)} units)")
    print(f"  → {'PASS' if not bad else f'FAIL ({bad} leaked field kinds)'}")
    return bad == 0


def _spot(root, rel, absent, present=None):
    p = os.path.join(root, rel)
    if not os.path.isfile(p):
        print(f"  {rel}: (absent)")
        return
    doc = json.load(open(p))
    units = doc.get("units", []) if isinstance(doc, dict) else []
    if not units:
        print(f"  {rel}: 0 units")
        return
    payload = units[0].get("payload", {})
    issues = []
    for k in absent or []:
        if k in payload:
            issues.append(f"LEAKED {k}")
    for k in present or []:
        if k not in payload:
            issues.append(f"MISSING {k}")
    verdict = "OK" if not issues else " ".join(issues)
    print(f"  {rel}: {len(units)} units, first payload keys={sorted(payload)[:8]} -> {verdict}")


if __name__ == "__main__":
    sys.exit(main())
