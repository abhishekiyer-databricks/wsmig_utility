"""
LakebaseCollector — Lakebase instances / managed Postgres projects (SOURCE workspace).
INVENTORY-ONLY.

Lakebase (managed Postgres) is a workspace-level asset whose data + connection topology are
not automatable in v1 → inventory LISTS instances for visibility and flags migration manual
(master §6a). Cursor-paginated (/api/2.0/postgres/projects, key 'projects').
natural_key = project/instance name.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class LakebaseCollector(BaseCollector):
    object_type = "lakebase_project"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("name"))

    def discover(self) -> list[dict]:
        try:
            raw = self.client.get_paginated(
                "api/2.0/postgres/projects", "projects", token_key="next_page_token",
                params={"page_size": 100},
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("lakebase projects list failed", error=str(exc))
            return []
        items = []
        for p in raw:
            status = p.get("status", {}) or {}
            items.append({
                "name": safe_str(p.get("name")),
                "display_name": safe_str(status.get("display_name")),
                "pg_version": safe_str(status.get("pg_version")),
                "migratable": False,   # v1: manual (data + connection topology)
                "_raw": p,
            })
        return items
