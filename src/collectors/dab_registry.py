"""
dab_registry — authoritative "is this asset DAB-managed?" lookup, built from the bundle STATE
files that `databricks bundle deploy` leaves in the workspace.

WHY this exists
---------------
For workspace-tree assets (jobs, pipelines, dashboards, Genie spaces) DAB-ness can be inferred
from the object's path: a bundle-deployed asset lives under a `.bundle/` folder. But DAB can now
also manage assets that have NO workspace path at all — clusters, instance pools, SQL warehouses,
secret scopes, serving endpoints (CLI v1.10.0 exposes 32 resource types). For those, path
inference is impossible.

Tag-sniffing does NOT work. `default_tags` on this customer's workspaces carries
`DeployedBy=Terraform` / `ManagedBy=Terraform` on **every** cluster and pool — including
manually-created ones (verified live on fvm1) — because the tags come from an Azure/workspace
policy, not from DAB. Keying off them would wrongly skip hand-made assets, which is far worse
than missing a DAB one: a skipped asset silently never migrates.

The reliable signal is the bundle's own state file. Each deployed bundle writes
`<root_path>/state/resources.json` (CLI ≥1.x; older CLIs wrote `terraform.tfstate`) which maps
every resource it owns to the concrete workspace id it created:

    {"state": {"resources.clusters.my_cluster": {"__id__": "0803-023949-be43al8w", ...},
               "resources.sql_warehouses.my_wh":  {"__id__": "9d3e917f0d21df83", ...}}}

So we walk `.bundle/**/state/`, parse those files, and build `{(kind, id) -> bundle_root}`.
An asset whose id is in that set is provably owned by a bundle.

Fail-soft by design: if no state file is readable the registry is simply EMPTY, and every asset
falls back to path-based detection / plain export. Never let a state-read hiccup turn into a
skipped asset.
"""
from __future__ import annotations

import json
from typing import Any

from src.utils.logger import get_logger

_LOG = get_logger("dab_registry")

# bundle resource-collection name → the fine-grained asset_type we emit units for.
# (Only the types this utility exports; UC/postgres/vector-search resources are out of scope.)
_RESOURCE_KIND_TO_ASSET = {
    "jobs": "job",
    "pipelines": "dlt_pipeline",
    "dashboards": "lakeview_dashboard",
    "genie_spaces": "genie_space",
    "clusters": "cluster",
    "instance_pools": "instance_pool",
    "sql_warehouses": "sql_warehouse",
    "secret_scopes": "secret_scope",
    "model_serving_endpoints": "serving_endpoint",
    "alerts": "alert_v2",
    "apps": "app",
}

# Fine-grained asset_types a DAB bundle CAN own — the ones for which a "Deployed by DAB" column
# is meaningful (otherwise the cell is "NA", not "Manual"). Two sources of truth:
#   • pathless resources declared in the CLI bundle schema (verified v0.291.0): jobs, pipelines,
#     dashboards(=Lakeview), clusters, sql_warehouses, secret_scopes, model_serving_endpoints,
#     alerts(=Alerts V2). Genie spaces are bundle-ownable too (proven live: a DAB-deployed genie
#     space reports `resources.genie_spaces.*` in its state) even though the schema string search
#     misses the key.
#   • workspace-tree assets that live UNDER a bundle root and so are bundle-managed: notebooks,
#     workspace files.
# Deliberately EXCLUDED (no bundle resource type, verified live): instance_pool, cluster_policy,
# cluster_library, global_init_script, legacy sql_query/alert/dashboard, repo, workspace_conf.
DAB_CAPABLE_ASSET_TYPES = frozenset({
    "job", "dlt_pipeline", "lakeview_dashboard", "genie_space", "cluster",
    "sql_warehouse", "secret_scope", "serving_endpoint", "alert_v2",
    "notebook", "workspace_file",
})

_STATE_FILES = ("resources.json", "terraform.tfstate")


class DabRegistry:
    """`(asset_type, source_id)` → owning bundle root, for every DAB-managed asset found."""

    def __init__(self) -> None:
        self._owned: dict[tuple[str, str], str] = {}
        self.bundles: list[str] = []

    # ── build ──────────────────────────────────────────────────────────────
    @classmethod
    def build(cls, client, bundle_state_paths: list[str]) -> "DabRegistry":
        """Parse each `<bundle_root>/state/<state file>` reachable in the workspace."""
        reg = cls()
        for path in bundle_state_paths:
            try:
                # State files are plain workspace FILES; `direct_download` returns raw bytes
                # (the base64 `content` route would need a second decode step).
                raw = client.download_bytes("api/2.0/workspace/export",
                                            params={"path": path, "direct_download": "true"})
            except Exception as exc:  # noqa: BLE001 — absent/unreadable state = no claims
                _LOG.warning("bundle state unreadable", path=path, error=str(exc))
                continue
            try:
                doc = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("bundle state unparseable", path=path, error=str(exc))
                continue
            root = path.split("/state/")[0]
            n = reg._ingest(doc, root)
            if n:
                reg.bundles.append(root)
                _LOG.info("bundle state parsed", root=root, resources=n)
        return reg

    def _ingest(self, doc: dict, bundle_root: str) -> int:
        if not isinstance(doc, dict):
            return 0
        if isinstance(doc.get("state"), dict):
            return self._ingest_resources_json(doc["state"], bundle_root)
        if isinstance(doc.get("resources"), list):
            return self._ingest_tfstate(doc["resources"], bundle_root)
        return 0

    def _ingest_resources_json(self, state: dict, bundle_root: str) -> int:
        """CLI >=1.x `resources.json`: keys look like `resources.<kind>.<name>[.permissions]`."""
        count = 0
        for key, node in state.items():
            parts = str(key).split(".")
            # skip sub-resources like `resources.secret_scopes.x.permissions`
            if len(parts) != 3 or parts[0] != "resources":
                continue
            asset_type = _RESOURCE_KIND_TO_ASSET.get(parts[1])
            oid = str((node or {}).get("__id__") or "").strip()
            if asset_type and oid:
                self._owned[(asset_type, oid)] = bundle_root
                count += 1
        return count

    def _ingest_tfstate(self, resources: list, bundle_root: str) -> int:
        """Legacy `terraform.tfstate`: `type` is e.g. `databricks_job`, id in instance attrs."""
        tf_type_to_asset = {
            "databricks_job": "job", "databricks_pipeline": "dlt_pipeline",
            "databricks_dashboard": "lakeview_dashboard", "databricks_cluster": "cluster",
            "databricks_instance_pool": "instance_pool",
            "databricks_sql_endpoint": "sql_warehouse",
            "databricks_secret_scope": "secret_scope",
            "databricks_model_serving": "serving_endpoint",
        }
        count = 0
        for res in resources:
            if not isinstance(res, dict):
                continue
            asset_type = tf_type_to_asset.get(str(res.get("type")))
            if not asset_type:
                continue
            for inst in res.get("instances") or []:
                oid = str(((inst or {}).get("attributes") or {}).get("id") or "").strip()
                if oid:
                    self._owned[(asset_type, oid)] = bundle_root
                    count += 1
        return count

    # ── query ──────────────────────────────────────────────────────────────
    def owns(self, asset_type: str, source_id: Any) -> bool:
        oid = str(source_id or "").strip()
        return bool(oid) and (asset_type, oid) in self._owned

    def bundle_of(self, asset_type: str, source_id: Any) -> str:
        return self._owned.get((asset_type, str(source_id or "").strip()), "")

    def __len__(self) -> int:
        return len(self._owned)
