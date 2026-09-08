"""
ComputeCollector — instance pools, cluster policies, all-purpose clusters (SOURCE workspace).

Inventory-time (read-only). Records natural keys (names) + ACLs. Flags ephemeral clusters
(job-*/dlt-execution-*/mlflow-model-*) as `ephemeral=True` so Export/Import can exclude them
(migrate review, master §10a) — inventory still lists them for visibility.
"""
from __future__ import annotations

import re

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str

_EPHEMERAL_CLUSTER = re.compile(r"^(job-\d+-run-\d+|dlt-execution-.+|mlflow-model-.+)")


class ComputeCollector(BaseCollector):
    object_type = "compute"

    def natural_key(self, obj: dict) -> str:
        # name for all three; clusters use cluster_name, pools instance_pool_name, policies name
        return safe_str(obj.get("_natural_key"))

    def discover(self) -> list[dict]:
        out: list[dict] = []
        out.extend(self._pools())
        out.extend(self._policies())
        out.extend(self._clusters())
        return out

    def _pools(self) -> list[dict]:
        raw = self.client.get("api/2.0/instance-pools/list").get("instance_pools", []) or []
        items = []
        for p in raw:
            items.append({
                "compute_type": "instance_pool",
                "instance_pool_id": safe_str(p.get("instance_pool_id")),
                "instance_pool_name": safe_str(p.get("instance_pool_name")),
                "_natural_key": safe_str(p.get("instance_pool_name")),
                "acl": self.fetch_acl("instance-pools", p.get("instance_pool_id")),
                "_raw": p,
            })
        return items

    def _policies(self) -> list[dict]:
        raw = self.client.get("api/2.0/policies/clusters/list").get("policies", []) or []
        items = []
        for p in raw:
            items.append({
                "compute_type": "cluster_policy",
                "policy_id": safe_str(p.get("policy_id")),
                "name": safe_str(p.get("name")),
                "_natural_key": safe_str(p.get("name")),
                "acl": self.fetch_acl("cluster-policies", p.get("policy_id")),
                "_raw": p,
            })
        return items

    def _clusters(self) -> list[dict]:
        raw = self.client.get("api/2.0/clusters/list").get("clusters", []) or []
        items = []
        for c in raw:
            name = safe_str(c.get("cluster_name"))
            src = c.get("cluster_source", "")
            ephemeral = bool(_EPHEMERAL_CLUSTER.match(name)) or src in ("JOB", "PIPELINE", "MODELS")
            if ephemeral:
                # Job/DLT/model clusters are ephemeral and never migrated — omit entirely from
                # inventory (only all-purpose clusters are relevant). (Plan 1a §8.)
                continue
            items.append({
                "compute_type": "cluster",
                "cluster_id": safe_str(c.get("cluster_id")),
                "cluster_name": name,
                "cluster_source": safe_str(src),
                "pinned": bool(c.get("pinned_by_user_name")),
                "_natural_key": name,
                "acl": self.fetch_acl("clusters", c.get("cluster_id")),
                "_raw": c,
            })
        return items
