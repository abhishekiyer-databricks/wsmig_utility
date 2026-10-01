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
            # B7: publish-state + schedules, so the target lands PUBLISHED when source is published
            # (consumers see data), with the same embed_credentials MODE + schedules. A draft-only
            # dashboard migrated draft-only on target is the pre-B7 bug (consumers saw nothing).
            pub = self._published_state(did)
            items.append({
                "dashboard_id": did,
                "display_name": safe_str(full.get("display_name") or d.get("display_name")),
                "warehouse_id": safe_str(full.get("warehouse_id")),
                "parent_path": safe_str(full.get("parent_path")),
                "path": safe_str(full.get("path")),
                "deployed_by_dab": dab["deployed_by_dab"],
                "dab_scope": dab["dab_scope"],
                "serialized_dashboard": full.get("serialized_dashboard"),
                # B7 publish facets:
                "is_published": pub["is_published"],
                "embed_credentials": pub["embed_credentials"],
                "published_warehouse_id": pub["warehouse_id"],
                "schedules": self._schedules(did),
                "acl": self.fetch_acl("dashboards", did),   # ACLs (Plan 1a §1)
                "_raw": d,
            })
        return items

    def _published_state(self, did: str) -> dict:
        """B7: the published snapshot via `GET .../published` (404 → draft-only). Returns
        `{is_published, embed_credentials, warehouse_id}`. `embed_credentials` is the data-permission
        MODE: True=Shared (embeds the publisher's credentials), False=Individual (each viewer uses
        their own). The API exposes NO embedded-principal — a Shared dashboard re-publishes as the
        run-as SP on target (documented limitation; see B7)."""
        try:
            pub = self.client.get(f"api/2.0/lakeview/dashboards/{did}/published") or {}
        except Exception:  # noqa: BLE001 — 404 (not published) or an unreadable publish state
            pub = {}
        is_published = bool(pub) and any(
            k in pub for k in ("revision_create_time", "embed_credentials", "warehouse_id"))
        return {
            "is_published": is_published,
            "embed_credentials": bool(pub.get("embed_credentials")),
            "warehouse_id": safe_str(pub.get("warehouse_id")),
        }

    def _schedules(self, did: str) -> list:
        """B7: the dashboard's schedules (+ each schedule's subscriptions), best-effort — a draft
        dashboard has none and a read failure must not fail inventory."""
        try:
            doc = self.client.get(f"api/2.0/lakeview/dashboards/{did}/schedules") or {}
        except Exception:  # noqa: BLE001
            return []
        out = []
        for sch in (doc.get("schedules") or []):
            if not isinstance(sch, dict):
                continue
            sid = safe_str(sch.get("schedule_id"))
            cron = sch.get("cron_schedule") or {}
            subs = []
            try:
                sdoc = self.client.get(
                    f"api/2.0/lakeview/dashboards/{did}/schedules/{sid}/subscriptions") or {}
                subs = [s for s in (sdoc.get("subscriptions") or []) if isinstance(s, dict)]
            except Exception:  # noqa: BLE001
                subs = []
            out.append({
                "display_name": safe_str(sch.get("display_name")),
                "cron_quartz": safe_str(cron.get("quartz_cron_expression")),
                "timezone_id": safe_str(cron.get("timezone_id")),
                "pause_status": safe_str(sch.get("pause_status")),
                "warehouse_id": safe_str(sch.get("warehouse_id")),
                "subscriptions": subs,
            })
        return out
