"""
DashboardsCollector — AI/BI (Lakeview) dashboards (SOURCE workspace).

List is cursor-paginated (/api/2.0/lakeview/dashboards); per-dashboard detail carries
`serialized_dashboard` + `warehouse_id` needed to recreate. natural_key = display_name.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import dab_path_info, folder_natural_key, safe_str


class DashboardsCollector(BaseCollector):
    object_type = "lakeview_dashboard"

    def natural_key(self, obj: dict) -> str:
        # PLAN 11 Finding-9: full path (`<parent_path>/<display_name>`), not the bare display_name,
        # so two same-named dashboards in different folders don't collapse onto one target object.
        return folder_natural_key(obj.get("parent_path"), obj.get("display_name"))

    def discover(self) -> list[dict]:
        raw = self.client.get_paginated(
            "api/2.0/lakeview/dashboards", "dashboards",
            token_key="next_page_token", params={"page_size": 100},
        )
        items = []
        for d in raw:
            did = safe_str(d.get("dashboard_id"))
            full = {}
            try:
                full = self.client.get(f"api/2.0/lakeview/dashboards/{did}") or {}
            except Exception as exc:  # noqa: BLE001
                self.log.warning("dashboard detail failed", dashboard_id=did, error=str(exc))
            # AI/BI dashboards have NO deployment.kind field (unlike jobs/pipelines); the only
            # DAB signal is the workspace `path` sitting under a `.bundle/` folder (verified live).
            dab = dab_path_info(full.get("path") or full.get("parent_path"),
                                getattr(self.config, "dab_bundle_roots", None))
            items.append({
                "dashboard_id": did,
                "display_name": safe_str(full.get("display_name") or d.get("display_name")),
                "warehouse_id": safe_str(full.get("warehouse_id")),
                "parent_path": safe_str(full.get("parent_path")),
                "path": safe_str(full.get("path")),
                "deployed_by_dab": dab["deployed_by_dab"],
                "dab_scope": dab["dab_scope"],
                "serialized_dashboard": full.get("serialized_dashboard"),
                "acl": self.fetch_acl("dashboards", did),   # ACLs (Plan 1a §1)
                "_raw": d,
            })
        return items
