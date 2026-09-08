"""
AppsCollector — Databricks Apps (SOURCE workspace). INVENTORY-ONLY.

Apps are a workspace-level (non-UC) asset: code + resources + compute. Their migration is
NOT automated in v1 (app source, resource bindings and secrets need per-app handling), so
inventory LISTS them for visibility and flags migration as manual (master §6a). Cursor-
paginated (/api/2.0/apps, key 'apps'). natural_key = app name.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class AppsCollector(BaseCollector):
    object_type = "app"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("name"))

    def discover(self) -> list[dict]:
        try:
            raw = self.client.get_paginated(
                "api/2.0/apps", "apps", token_key="next_page_token",
                params={"page_size": 100},
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("apps list failed", error=str(exc))
            return []
        items = []
        for a in raw:
            name = safe_str(a.get("name"))
            items.append({
                "name": name,
                "description": safe_str(a.get("description")),
                "creator": safe_str(a.get("creator")),
                "url": safe_str(a.get("url")),
                "migratable": False,   # v1: manual (app source + resource bindings)
                "acl": self.fetch_acl("apps", name),   # apps support permissions (verified live)
                "_raw": a,
            })
        return items
