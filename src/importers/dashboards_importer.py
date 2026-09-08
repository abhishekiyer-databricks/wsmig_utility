"""
DashboardsImporter — phase 8: AI/BI (Lakeview) dashboards (Plan 3 §6).

The dashboard's whole definition is the `serialized_dashboard` string, which is carried VERBATIM —
we never parse or rewrite it. Only two things need attention:

  • **`warehouse_id` must be remapped**, or every widget queries a warehouse that doesn't exist.
  • **`serialized_dashboard` references UC tables by fully-qualified name.** UC is out of scope, so a
    dashboard can import perfectly and still render empty because its tables aren't on target. That
    is the single most common cause of a "successful" import producing a broken dashboard, so it is
    stated in the unit's note rather than left for the customer to discover.
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter
from src.utils.helpers import safe_str


class DashboardsImporter(BaseImporter):
    component = "dashboards"
    asset_types = ("lakeview_dashboard",)

    def load(self) -> list[dict]:
        return self.units_for("lakeview_dashboard")

    def existing_keys(self) -> dict:
        """`{source full-path: dashboard_id}` — PAGINATED (lakeview is a cursor API).

        PLAN 11 Finding-9: the natural key is the full path (`<parent_path>/<display_name>`), but
        the LIST omits parent_path, so matching goes through `folder_existing_keys` (id-anchor via
        state + collapse-safe unique-name adoption) — two same-named dashboards in different folders
        no longer resolve to one target.
        """
        dashboards = self.client.get_paginated("api/2.0/lakeview/dashboards", "dashboards",
                                               params={"page_size": 100})
        found = self.folder_existing_keys("lakeview_dashboard", dashboards, "display_name",
                                          "dashboard_id")
        self.context.setdefault("lakeview_dashboard_target_ids", {}).update(found)
        return found

    def create_one(self, unit: dict) -> dict:
        body, note = self._body(unit)
        # PLAN 8 Bug 7 (Lakeview sibling): recreate the dashboard's `.lvdash.json` in its SOURCE
        # folder (a user-created dashboard belongs back in the user's directory), not the API
        # default. Only on CREATE — an update doesn't move an existing dashboard.
        parent = safe_str((unit.get("payload") or {}).get("parent_path"))
        res = None
        if parent:
            body["parent_path"] = parent
            res = self.remap_parent_path(body)
        try:
            created = self.client.post("api/2.0/lakeview/dashboards", body)
        except Exception as exc:  # noqa: BLE001
            self.missing_parent_prerequisite(exc, body.get("parent_path"), self.natural_key(unit))
            raise
        did = safe_str(created.get("dashboard_id"))
        self.context.setdefault("lakeview_dashboard_target_ids", {})[self.natural_key(unit)] = did
        # PLAN 11 Finding-8: an orphaned owner's dashboard is preserved under the backup root as
        # created_with_warning (parity with notebooks), never a hard prerequisite_missing failure.
        if res is not None and res.kind == "backup":
            return {"target_id": did, "warning": f"{res.note} {note}"}
        return {"target_id": did, "note": note}

    def update_one(self, unit: dict, target_id: str) -> dict:
        body, note = self._body(unit)
        self.client.patch(f"api/2.0/lakeview/dashboards/{target_id}", body)
        return {"target_id": target_id, "note": note}

    def _body(self, unit: dict) -> tuple[dict, str]:
        payload = dict(unit.get("payload") or {})
        body = {"display_name": safe_str(payload.get("display_name")) or self.natural_key(unit)}
        # Carried verbatim — the serialized definition is the dashboard, and rewriting any of it
        # risks corrupting layouts we don't own the schema for.
        if payload.get("serialized_dashboard") is not None:
            body["serialized_dashboard"] = payload["serialized_dashboard"]

        notes = []
        src_wh = safe_str(payload.get("warehouse_id"))
        if src_wh:
            # PLAN 11 Finding-10: exact-or-fail-loud. The old "attach it to any existing warehouse"
            # substitution made a dashboard look migrated while every widget silently queried a
            # DIFFERENT warehouse. Now it resolves to the warehouse we recreated for the source one,
            # or fails loud (retryable if the warehouse is in-bundle-not-yet, hard if not in bundle).
            body["warehouse_id"] = self.require_remap(
                "sql_warehouse", src_wh,
                referenced_by=f"dashboard `{safe_str(payload.get('display_name'))}`")

        notes.append("serialized_dashboard carried verbatim. NOTE its datasets reference Unity "
                     "Catalog tables by fully-qualified name, and UC is OUT OF SCOPE for this "
                     "utility — if those tables are not on target the dashboard imports fine but "
                     "renders empty until the UC migration creates them.")
        note = " ".join(notes)
        return body, note
