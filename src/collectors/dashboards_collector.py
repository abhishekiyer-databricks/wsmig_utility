"""
DashboardsCollector — AI/BI (Lakeview) dashboards (SOURCE workspace).

List is cursor-paginated (/api/2.0/lakeview/dashboards); per-dashboard detail carries
`serialized_dashboard` + `warehouse_id` needed to recreate. natural_key = full path.

PLAN 16.2 §4 — publish state, schedules and subscriptions (verified live 2026-10-05, F1–F14):
  • `GET …/{id}/published`: 200 → published (`embed_credentials`, `warehouse_id`,
    `revision_create_time`); **404 only** → draft; any other error → `unknown` + WARNING, and no
    publish/schedule unit is exported for it (so nothing is changed on the target).
  • a published dashboard's schedules (`GET …/schedules`) and each schedule's subscriptions
    (`GET …/schedules/{sid}/subscriptions`). A draft has none (its schedule calls 404, F8).
  • a destination subscriber is resolved to its `display_name` + `destination_type`
    (`GET notification-destinations/{id}`, F13) — the target adopts destinations BY NAME.
  • a user subscriber carries only `user_id`; the inventory runner resolves it to `user_name` from
    the identity roster afterwards (`resolve_subscriber_users`).
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import dab_path_info, folder_natural_key, is_not_found, safe_str

PUBLISH_PUBLISHED = "published"
PUBLISH_DRAFT = "draft"
PUBLISH_UNKNOWN = "unknown"


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
        self._destinations: dict = {}
        items = []
        for d in raw:
            did = safe_str(d.get("dashboard_id"))
            self.log.debug(f"collecting dashboard {did}")
            full = {}
            try:
                full = self.client.get(f"api/2.0/lakeview/dashboards/{did}") or {}
            except Exception as exc:  # noqa: BLE001
                self.log.warning("dashboard detail failed", dashboard_id=did, error=str(exc))
            # AI/BI dashboards have NO deployment.kind field (unlike jobs/pipelines); the only
            # DAB signal is the workspace `path` sitting under a `.bundle/` folder (verified live).
            dab = dab_path_info(full.get("path") or full.get("parent_path"),
                                getattr(self.config, "dab_bundle_roots", None))
            state, published = self._published(did)
            schedules = self._schedules(did) if state == PUBLISH_PUBLISHED else []
            items.append({
                "dashboard_id": did,
                "display_name": safe_str(full.get("display_name") or d.get("display_name")),
                "warehouse_id": safe_str(full.get("warehouse_id")),
                "parent_path": safe_str(full.get("parent_path")),
                "path": safe_str(full.get("path")),
                "update_time": safe_str(full.get("update_time") or d.get("update_time")),
                "deployed_by_dab": dab["deployed_by_dab"],
                "dab_scope": dab["dab_scope"],
                "serialized_dashboard": full.get("serialized_dashboard"),
                "publish_state": state,
                "published": published,
                # None = the schedules could not be read (WARNING logged) → no schedule units.
                "schedules": schedules,
                "acl": self.fetch_acl("dashboards", did),   # ACLs (Plan 1a §1)
                "_raw": d,
            })
            self.log.debug(f"collected dashboard {did} ({items[-1]['display_name']}) — "
                           f"{state}, {len(schedules or [])} schedule(s)")
        return items

    # ── publish state (F1) ────────────────────────────────────────────────
    def _published(self, did: str) -> tuple:
        """`(publish_state, published)`: 200 → published + its fields; 404 → draft; else unknown."""
        if not did:
            return PUBLISH_UNKNOWN, {}
        try:
            doc = self.client.get(f"api/2.0/lakeview/dashboards/{did}/published") or {}
        except Exception as exc:  # noqa: BLE001 — classified below; never aborts the collector
            if is_not_found(exc):
                return PUBLISH_DRAFT, {}
            self.log.warning(f"dashboard {did}: publish state unreadable — treated as UNKNOWN, so "
                             f"its publish state and schedules are NOT migrated: {exc}")
            return PUBLISH_UNKNOWN, {}
        return PUBLISH_PUBLISHED, {
            "embed_credentials": bool(doc.get("embed_credentials")),
            "warehouse_id": safe_str(doc.get("warehouse_id")),
            "revision_create_time": safe_str(doc.get("revision_create_time")),
            "display_name": safe_str(doc.get("display_name")),
        }

    # ── schedules + subscriptions (F9, F11) ───────────────────────────────
    def _schedules(self, did: str):
        """A PUBLISHED dashboard's schedules, each with its subscribers. None on a read error."""
        try:
            raw = self.client.get_paginated(f"api/2.0/lakeview/dashboards/{did}/schedules",
                                            "schedules", token_key="next_page_token")
        except Exception as exc:  # noqa: BLE001 — degraded: no schedule units for this dashboard
            self.log.warning(f"dashboard {did}: schedules unreadable — its schedules are NOT "
                             f"migrated this run: {exc}")
            return None
        out = []
        for sch in raw or []:
            if not isinstance(sch, dict):
                continue
            sid = safe_str(sch.get("schedule_id"))
            cron = sch.get("cron_schedule") or {}
            out.append({
                "schedule_id": sid,
                "display_name": safe_str(sch.get("display_name")),
                "cron_schedule": {
                    "quartz_cron_expression": safe_str(cron.get("quartz_cron_expression")),
                    "timezone_id": safe_str(cron.get("timezone_id"))},
                "pause_status": safe_str(sch.get("pause_status")),
                "warehouse_id": safe_str(sch.get("warehouse_id")),
                "subscribers": self._subscribers(did, sid),
            })
        return out

    def _subscribers(self, did: str, sid: str) -> list:
        """`[{kind: user, user_id} | {kind: destination, destination_id, display_name,
        destination_type}]` for one schedule; [] (WARNING) when unreadable."""
        try:
            subs = self.client.get_paginated(
                f"api/2.0/lakeview/dashboards/{did}/schedules/{sid}/subscriptions",
                "subscriptions", token_key="next_page_token")
        except Exception as exc:  # noqa: BLE001 — degraded: the schedule migrates without them
            self.log.warning(f"dashboard {did} schedule {sid}: subscriptions unreadable — the "
                             f"schedule migrates WITHOUT its subscribers: {exc}")
            return []
        out = []
        for s in subs or []:
            subscriber = (s or {}).get("subscriber") or {}
            user = subscriber.get("user_subscriber") or {}
            dest = subscriber.get("destination_subscriber") or {}
            if user.get("user_id"):
                out.append({"kind": "user", "user_id": safe_str(user.get("user_id"))})
            elif dest.get("destination_id"):
                out.append({"kind": "destination", **self._destination(
                    safe_str(dest.get("destination_id")))})
        return out

    def _destination(self, dest_id: str) -> dict:
        """A notification destination's name + type (F13), cached; `unresolved` when unreadable."""
        if dest_id not in self._destinations:
            try:
                doc = self.client.get(f"api/2.0/notification-destinations/{dest_id}") or {}
                self._destinations[dest_id] = {
                    "destination_id": dest_id,
                    "display_name": safe_str(doc.get("display_name")),
                    "destination_type": safe_str(doc.get("destination_type"))}
            except Exception as exc:  # noqa: BLE001 — degraded: kept by id, manual at import
                self.log.warning(f"notification destination {dest_id} unreadable — its "
                                 f"subscriptions become a manual step: {exc}")
                self._destinations[dest_id] = {"destination_id": dest_id, "unresolved": True}
        return dict(self._destinations[dest_id])


def resolve_subscriber_users(objects_by_type: dict) -> int:
    """Resolve every dashboard user subscriber's `user_id` → `user_name` from the inventory's
    identity roster (the user list the identity collector read). In place; returns how many stayed
    unresolved — those are kept as `{kind: user, user_id, unresolved: true}` → manual at import."""
    names = {safe_str(i.get("id")): safe_str(i.get("userName"))
             for i in objects_by_type.get("identity", []) or []
             if i.get("identity_type") == "user" and i.get("id") and i.get("userName")}
    unresolved = 0
    for d in objects_by_type.get("lakeview_dashboard", []) or []:
        for sch in d.get("schedules") or []:
            for sub in sch.get("subscribers") or []:
                if sub.get("kind") != "user" or sub.get("user_name"):
                    continue
                name = names.get(safe_str(sub.get("user_id")))
                if name:
                    sub["user_name"] = name
                else:
                    sub["unresolved"] = True
                    unresolved += 1
    return unresolved
