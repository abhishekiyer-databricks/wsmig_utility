"""
SqlCollector — SQL warehouses + legacy SQL (queries, alerts, dashboards) (SOURCE workspace).

Warehouses: /api/2.0/sql/warehouses (not paginated today — verify). Legacy queries/alerts/
dashboards: cursor-paginated /api/2.0/sql/{queries,alerts,dashboards}. Alerts V2 (the current
alert surface; legacy-alert creation is disabled): /api/2.0/alerts. natural_key = display_name
(query/alert) or name (warehouse/dashboard). Alerts carry sql_type legacy_alert vs alert.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import dab_path_info, folder_natural_key, safe_str


class SqlCollector(BaseCollector):
    object_type = "sql"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("_natural_key"))

    # endpoint segment -> (singular sql_type label, permissions-API object type)
    # permissions object types verified live: queries / alerts / dashboards (Plan 1a §1).
    _LEGACY = {"queries": ("legacy_query", "queries"),
               "alerts": ("legacy_alert", "alerts"),
               "dashboards": ("legacy_dashboard", "dashboards")}

    def discover(self) -> list[dict]:
        out: list[dict] = []
        out.extend(self._warehouses())
        for kind in self._LEGACY:
            out.extend(self._legacy(kind, "results"))
        out.extend(self._alerts_v2())
        return out

    def _warehouses(self) -> list[dict]:
        raw = self.client.get("api/2.0/sql/warehouses").get("warehouses", []) or []
        items = []
        for w in raw:
            items.append({
                "sql_type": "warehouse",
                "id": safe_str(w.get("id")),
                "name": safe_str(w.get("name")),
                "_natural_key": safe_str(w.get("name")),
                "warehouse_type": safe_str(w.get("warehouse_type")),
                "acl": self.fetch_acl("sql/warehouses", w.get("id")),
                "_raw": w,
            })
        return items

    def _legacy(self, kind: str, result_key: str) -> list[dict]:
        try:
            raw = self.client.get_paginated(
                f"api/2.0/sql/{kind}", result_key,
                token_key="next_page_token", params={"page_size": 100},
            )
        except Exception as exc:  # noqa: BLE001
            # A 404 means the legacy endpoint isn't present on this workspace (deprecated /
            # feature-off) — that's expected, not an error. Log quietly at INFO.
            if "404" in str(exc):
                self.log.info("legacy sql endpoint absent (skipping)", kind=kind)
            else:
                self.log.warning("legacy sql fetch failed", kind=kind, error=str(exc))
            return []
        sql_type, perm_type = self._LEGACY[kind]
        items = []
        for o in raw:
            # /api/2.0/sql/{queries,alerts} name the object `display_name`; only
            # /api/2.0/sql/dashboards uses `name`. Check display_name first so the
            # natural_key and the ACL-tab Object column are never blank.
            name = safe_str(o.get("display_name") or o.get("name") or o.get("title"))
            oid = safe_str(o.get("id"))
            if kind == "queries":
                # PLAN 8 Bug 7: the LIST omits `parent_path` (verified live 2026-08-18) — enrich via
                # GET-by-id so the query can be recreated in its SOURCE workspace folder rather than
                # silently at the workspace root.
                self._enrich_query_parent_path(o, oid)
            # PLAN 11 Finding-9: key by the FULL path (`<parent_path>/<name>`), not the bare name —
            # otherwise N distinct same-named queries (all "New query") collapse onto ONE target
            # object and N-1 are silently lost. Falls back to name/oid when parent_path is absent
            # (legacy alerts/dashboards from the LIST carry no path).
            parent = o.get("parent_path") or o.get("parent")
            item = {
                "sql_type": sql_type,   # legacy_query / legacy_alert / legacy_dashboard
                "id": oid,
                "name": name,
                "_natural_key": folder_natural_key(parent, name) or oid,
                "acl": self.fetch_acl(perm_type, oid),   # ACLs (Plan 1a §1)
                "_raw": o,
            }
            # Legacy alerts/queries expose no workspace path and aren't a DAB resource type
            # (only Alerts V2 is) → always Manual. Legacy dashboards can be DAB-deployed and
            # carry a `parent` path, so classify those.
            if kind == "dashboards":
                dab = dab_path_info(o.get("parent") or o.get("parent_path"),
                                    getattr(self.config, "dab_bundle_roots", None))
            else:
                dab = {"deployed_by_dab": False, "dab_scope": ""}
            item["deployed_by_dab"] = dab["deployed_by_dab"]
            item["dab_scope"] = dab["dab_scope"]
            items.append(item)
        return items

    def _enrich_query_parent_path(self, raw: dict, oid: str) -> None:
        """Best-effort: merge `parent_path` from GET-by-id into the (shallower) LIST object. The
        query still exports if this fails — it just lands at the root on import (Bug 7)."""
        if not oid:
            return
        try:
            full = self.client.get(f"api/2.0/sql/queries/{oid}") or {}
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
            self.log.warning("query parent_path enrichment failed", id=oid, error=str(exc)[:160])
            return
        if full.get("parent_path") is not None:
            raw["parent_path"] = full.get("parent_path")

    def _alerts_v2(self) -> list[dict]:
        """Alerts V2 (/api/2.0/alerts) — the current alert surface (legacy-alert creation
        is disabled). Distinct from legacy alerts; permissions object type is `alertsv2`."""
        try:
            # Live fvm1 (2026-07-31): GET /api/2.0/alerts returns the list under `alerts`
            # (NOT `results` as some docs state); pagination token is `next_page_token`.
            raw = self.client.get_paginated(
                "api/2.0/alerts", "alerts",
                token_key="next_page_token", params={"page_size": 100},
            )
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                self.log.info("alerts v2 endpoint absent (skipping)")
            else:
                self.log.warning("alerts v2 fetch failed", error=str(exc))
            return []
        items = []
        for o in raw:
            oid = safe_str(o.get("id"))
            # PLAN 8 Bug 10: the LIST is SHALLOW — it omits `evaluation` (with `source.name`) and
            # `schedule`, both REQUIRED by the create API (verified live 2026-08-18). Enrich via
            # GET-by-id so the exported payload can actually be recreated. Fall back to the list
            # object if the detail read fails.
            full = self._alert_v2_full(oid) or o
            name = safe_str(full.get("display_name") or o.get("display_name") or o.get("name"))
            # Alerts V2 IS a DAB resource type; a bundle-deployed one sits under `.bundle/`
            # (exposed via `parent_path`). Hand-made alerts return no parent_path → Manual.
            dab = dab_path_info(full.get("parent_path") or o.get("parent_path"),
                                getattr(self.config, "dab_bundle_roots", None))
            parent = full.get("parent_path") or o.get("parent_path")
            items.append({
                "sql_type": "alert",   # Alerts V2 (vs legacy_alert)
                "id": oid,
                "name": name,
                # Finding-9: full-path key so two same-named alerts in different folders stay
                # distinct (falls back to name when parent_path is absent).
                "_natural_key": folder_natural_key(parent, name) or oid,
                "parent_path": safe_str(full.get("parent_path") or o.get("parent_path")),
                "deployed_by_dab": dab["deployed_by_dab"],
                "dab_scope": dab["dab_scope"],
                "acl": self.fetch_acl("alertsv2", oid),
                "_raw": full,
            })
        return items

    def _alert_v2_full(self, oid: str) -> dict:
        """The full Alert V2 (evaluation + schedule + query_text + warehouse_id) via GET-by-id —
        the create-ready shape the LIST does not return (Bug 10). Best-effort."""
        if not oid:
            return {}
        try:
            return self.client.get(f"api/2.0/alerts/{oid}") or {}
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
            self.log.warning("alert_v2 enrichment failed", id=oid, error=str(exc)[:160])
            return {}
