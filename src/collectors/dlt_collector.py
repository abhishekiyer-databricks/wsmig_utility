"""
DltCollector — Delta Live Tables pipelines (SOURCE workspace).

List is cursor-paginated (/api/2.0/pipelines, key 'statuses'); the full spec comes from the
per-pipeline detail (/api/2.0/pipelines/{id}). natural_key = pipeline name.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class DltCollector(BaseCollector):
    object_type = "dlt_pipeline"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("name"))

    def discover(self) -> list[dict]:
        raw = self.client.get_paginated(
            "api/2.0/pipelines", "statuses",
            token_key="next_page_token", params={"max_results": 100},
        )
        items = []
        for p in raw:
            pid = safe_str(p.get("pipeline_id"))
            spec = {}
            try:
                detail = self.client.get(f"api/2.0/pipelines/{pid}")
                spec = detail.get("spec", {}) if isinstance(detail, dict) else {}
            except Exception as exc:  # noqa: BLE001
                self.log.warning("pipeline detail failed", pipeline_id=pid, error=str(exc))
            items.append({
                "pipeline_id": pid,
                "name": safe_str(p.get("name")),
                "acl": self.fetch_acl("pipelines", pid),
                # DAB-deployed pipelines carry spec.deployment.kind == "BUNDLE" (Plan 1a §4).
                "deployed_by_dab": safe_str((spec.get("deployment") or {}).get("kind")) == "BUNDLE",
                "spec": spec,
                "_raw": p,
            })
        return items
