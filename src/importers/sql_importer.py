"""
SqlImporter — phase 6: warehouses → legacy queries → legacy alerts → alerts v2 (Plan 3 §6, §6d).

Warehouses go FIRST because everything downstream — queries, alerts, DLT, Lakeview dashboards and
Genie spaces — carries a `warehouse_id` that must be remapped to the target's.

**Legacy SQL dashboards are NEVER attempted** (D10/§6d). `POST /api/2.0/preview/sql/dashboards` — the
old Redash-style create — no longer works on modern workspaces (verified live while building the fvm1
fixtures, which is why no live fixture for one exists). Read/list still work, so they inventory and
export fine; only creation is gone. Attempting it would produce a permanent red failure on every run
forever, which trains the operator to ignore red — so each is recorded `manual` with enough detail to
rebuild it as an AI/BI dashboard. Their underlying `legacy_query` objects still migrate, so only the
visual layout is hand-rebuilt. Export already marks them `manual`, so the base class handles it and
this importer has no dashboard code path at all.
"""
from __future__ import annotations

from typing import Optional

from src.importers.base_importer import BaseImporter, HardRemapFailure, UnsupportedOperation
from src.utils.helpers import safe_str

# The v1 alert `op` vocabulary. The modern `condition.op` uses words; v1 wants symbols.
_V1_OPS = {"GREATER_THAN": ">", "GREATER_THAN_OR_EQUAL": ">=", "LESS_THAN": "<",
           "LESS_THAN_OR_EQUAL": "<=", "EQUAL": "==", "NOT_EQUAL": "!="}


def _legacy_alert_options(condition) -> Optional[dict]:
    """Translate a modern `condition` block into the v1 `options` a legacy create requires.

    Returns None when it does not map cleanly — the caller then reports a manual rebuild rather than
    guessing at an alert's trigger, which is the kind of thing that must not be approximated.
    """
    if not isinstance(condition, dict):
        return None
    op = _V1_OPS.get(safe_str(condition.get("op")))
    column = ((condition.get("operand") or {}).get("column") or {}).get("name")
    threshold = ((condition.get("threshold") or {}).get("value") or {})
    value = next((threshold[k] for k in ("string_value", "double_value", "bool_value")
                  if k in threshold), None)
    if not op or not column or value is None:
        return None
    return {"column": safe_str(column), "op": op, "value": safe_str(value)}


class SqlImporter(BaseImporter):
    component = "sql"
    asset_types = ("sql_warehouse", "legacy_query", "legacy_alert", "alert_v2",
                   "legacy_dashboard")

    def load(self) -> list[dict]:
        """Warehouses → queries → alerts. Legacy dashboards ride along as `manual` units."""
        return self.units_for("sql_warehouse", "legacy_query", "legacy_alert", "alert_v2",
                              "legacy_dashboard")

    def existing_keys(self) -> dict:
        """`{name: id}` per SQL asset type, published for the phases that remap onto them."""
        out: dict = {}

        warehouses = (self.client.get("api/2.0/sql/warehouses") or {}).get("warehouses") or []
        found = {safe_str(w.get("name")): safe_str(w.get("id"))
                 for w in warehouses if w.get("name")}
        self.context.setdefault("sql_warehouse_target_ids", {}).update(found)
        out.update(found)

        # Queries and alerts are cursor APIs, and a truncated list here means a DUPLICATE query on
        # every re-run — so both go through the paginating helper rather than a bare get.
        # PLAN 11 Finding-9: these are keyed by the FULL PATH now, but the LIST omits parent_path,
        # so matching goes through `folder_existing_keys` (id-anchor via state + collapse-safe
        # unique-name adoption) rather than a `{display_name: id}` map that collapsed same-named
        # objects onto one target.
        queries = self.client.get_paginated("api/2.0/sql/queries", "results",
                                            params={"page_size": 100})
        q_list = [{"_name": safe_str(q.get("display_name") or q.get("name")),
                   "id": safe_str(q.get("id"))} for q in queries]
        q_found = self.folder_existing_keys("legacy_query", q_list, "_name", "id")
        self.context.setdefault("legacy_query_target_ids", {}).update(q_found)
        out.update(q_found)

        alerts = self.client.get_paginated("api/2.0/alerts", "results", params={"page_size": 100})
        a_list = [{"_name": safe_str(a.get("display_name") or a.get("name")),
                   "id": safe_str(a.get("id"))} for a in alerts]
        a_found = self.folder_existing_keys("alert_v2", a_list, "_name", "id")
        self.context.setdefault("alert_v2_target_ids", {}).update(a_found)
        out.update(a_found)

        return out

    # ── create ────────────────────────────────────────────────────────────
    def create_one(self, unit: dict) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type == "sql_warehouse":
            return self._create_warehouse(unit)
        if asset_type == "legacy_query":
            return self._create_query(unit)
        if asset_type == "legacy_alert":
            return self._create_legacy_alert(unit)
        if asset_type == "alert_v2":
            return self._create_alert_v2(unit)
        raise RuntimeError(f"sql importer got an unexpected asset_type {asset_type!r}")

    def update_one(self, unit: dict, target_id: str) -> dict:
        """The edit APIs — note each SQL asset uses a DIFFERENT shape."""
        asset_type = safe_str(unit.get("asset_type"))
        payload = dict(unit.get("payload") or {})
        if asset_type == "sql_warehouse":
            # Warehouses edit via `/{id}/edit`, not a PUT on the collection.
            self.client.post(f"api/2.0/sql/warehouses/{target_id}/edit",
                             self._warehouse_body(payload, unit))
            return {"target_id": target_id}
        if asset_type == "legacy_query":
            # PLAN 11 Finding-9 Bug A: UPDATE is `PATCH /api/2.0/sql/queries/{id}` with an
            # `update_mask` — the modern Queries API. `POST /api/2.0/sql/queries/{id}` (the old
            # Redash `/preview/sql/queries/{id}` convention) does NOT exist on modern workspaces and
            # 404s ENDPOINT_NOT_FOUND, so every legacy_query update silently failed. CREATE stays a
            # POST with no id (correct). The mask names exactly the fields the body carries.
            body, _res = self._query_body(payload)
            self.client.patch(f"api/2.0/sql/queries/{target_id}",
                              {"query": body, "update_mask": ",".join(sorted(body.keys()))})
            return {"target_id": target_id}
        if asset_type == "legacy_alert":
            self.client.put(f"api/2.0/sql/alerts/{target_id}", self._legacy_alert_body(payload))
            return {"target_id": target_id}
        if asset_type == "alert_v2":
            body, _res = self._alert_v2_body(payload)
            # Alerts V2 PATCH REQUIRES `update_mask` (a query arg) naming the fields to write —
            # a body-only PATCH 400s "update_mask is required" (caught live, PLAN 11 Run 2). Without
            # this the BUG-1 fix routed a changed alert to UPDATE correctly but the call itself
            # failed, so the alert stayed stale — the very failure mode BUG-1 set out to kill.
            self.client.patch(f"api/2.0/alerts/{target_id}", body,
                              params={"update_mask": self._alert_v2_update_mask(body)})
            return {"target_id": target_id}
        return {"target_id": target_id}

    # ── warehouses ────────────────────────────────────────────────────────
    def _create_warehouse(self, unit: dict) -> dict:
        body = self._warehouse_body(unit.get("payload") or {}, unit)
        created = self.client.post("api/2.0/sql/warehouses", body)
        wh_id = safe_str(created.get("id"))
        self.context.setdefault("sql_warehouse_target_ids", {})[self.natural_key(unit)] = wh_id
        return {"target_id": wh_id,
                "note": "same cloud/region, so warehouse_type and size are kept verbatim"}

    def _warehouse_body(self, payload: dict, unit: dict) -> dict:
        body = dict(payload)
        body["name"] = safe_str(body.get("name")) or self.natural_key(unit)
        # `creator_name` names a SOURCE identity: the target attributes the warehouse to the caller,
        # and an unknown name is rejected outright.
        body.pop("creator_name", None)
        return body

    # ── legacy queries ────────────────────────────────────────────────────
    def _create_query(self, unit: dict) -> dict:
        body, res = self._query_body(unit.get("payload") or {})
        try:
            created = self.client.post("api/2.0/sql/queries", {"query": body})
        except Exception as exc:  # noqa: BLE001
            self.missing_parent_prerequisite(exc, body.get("parent_path"), self.natural_key(unit))
            raise
        qid = safe_str(created.get("id"))
        self.context.setdefault("legacy_query_target_ids", {})[self.natural_key(unit)] = qid
        parent = safe_str(body.get("parent_path"))
        # PLAN 11 Finding-8: an orphaned owner's query is preserved under the backup root as
        # created_with_warning (parity with notebooks), never a hard prerequisite_missing failure.
        if res.kind == "backup":
            return {"target_id": qid, "warning": res.note}
        return {"target_id": qid, "note": f"created in {parent}" if parent else ""}

    def _query_body(self, payload: dict):
        body = dict(payload)
        # PLAN 8 Bug 7: PRESERVE + remap `parent_path` so the query lands in its SOURCE folder. It
        # used to be popped, which dropped every query at the workspace root (the object was
        # `Created` but never appeared in the user's/target directory tree).
        res = self.remap_parent_path(body)
        self._remap_warehouse(body, referenced_by=f"query `{safe_str(body.get('display_name'))}`")
        return body, res

    # ── alerts ────────────────────────────────────────────────────────────
    def _create_legacy_alert(self, unit: dict) -> dict:
        """Create a LEGACY (v1) SQL alert — which needs the legacy `options` shape.

        Verified live: `POST /api/2.0/sql/alerts` rejects the modern payload with "Missing alert
        definition". The v1 create wants a flat `options{column, op, value}`, but the current LIST/GET
        surface returns the NEW `condition{op, operand, threshold}` shape instead — so the exported
        payload cannot be posted back as-is. The condition is translated where it maps cleanly, and
        anything that doesn't is reported as a manual rebuild rather than an opaque 400.
        """
        payload = self._legacy_alert_body(unit.get("payload") or {})
        if "options" not in payload:
            options = _legacy_alert_options(payload.get("condition"))
            if options is None:
                raise UnsupportedOperation(
                    f"legacy alert `{self.natural_key(unit)}` cannot be recreated: the v1 create API "
                    f"requires the old flat `options{{column, op, value}}` shape, but the current "
                    f"read API only returns the newer `condition` shape, and this alert's condition "
                    f"does not translate cleanly. Rebuild it on target as an Alerts V2 alert (the "
                    f"underlying query HAS migrated), or recreate it by hand.")
            payload["options"] = options
            payload.pop("condition", None)
        created = self.client.post("api/2.0/sql/alerts", payload)
        return {"target_id": safe_str(created.get("id")),
                "note": "legacy (v1) alert — its `condition` was translated to the v1 `options` shape"}

    def _legacy_alert_body(self, payload: dict) -> dict:
        """Remap the alert's QUERY id — an alert holding a source query id is inert on target.

        Finding-10: exact-or-fail-loud. A source query in the bundle but not yet on target is a
        retryable prerequisite; one not in the bundle at all is a hard failure — never a silent
        keep-dangling."""
        body = dict(payload)
        src_query = safe_str(body.get("query_id"))
        if src_query:
            body["query_id"] = self.require_remap("legacy_query", src_query,
                                                  referenced_by=f"legacy alert `{safe_str(body.get('name'))}`")
        self._remap_warehouse(body, referenced_by=f"legacy alert `{safe_str(body.get('name'))}`")
        return body

    def _create_alert_v2(self, unit: dict) -> dict:
        # Verified against the SDK's `create_alert`: /api/2.0/alerts takes the AlertV2 body FLAT,
        # NOT wrapped in {"alert": ...} the way legacy queries are.
        body, res = self._alert_v2_body(unit.get("payload") or {})
        try:
            created = self.client.post("api/2.0/alerts", body)
        except Exception as exc:  # noqa: BLE001
            self.missing_parent_prerequisite(exc, body.get("parent_path"), self.natural_key(unit))
            raise
        aid = safe_str(created.get("id"))
        self.context.setdefault("alert_v2_target_ids", {})[self.natural_key(unit)] = aid
        if res.kind == "backup":
            return {"target_id": aid, "warning": res.note}   # Finding-8: orphan-owner divert
        return {"target_id": aid}

    def _alert_v2_body(self, payload: dict):
        body = dict(payload)
        # PLAN 8 Bug 7: keep + remap `parent_path` (was popped) so the alert lands in its folder.
        # PLAN 8 Bug 10: `evaluation` + `schedule` (both REQUIRED by create) now travel because the
        # collector enriches the shallow LIST via GET-by-id — nothing to add here beyond remaps.
        res = self.remap_parent_path(body)
        self._remap_warehouse(body, referenced_by=f"alert `{safe_str(body.get('display_name'))}`")
        return body, res

    # Fields Alerts V2 lets you PATCH. The mask must name only settable fields present in the body:
    # server-owned ones (id/create_time/lifecycle_state/owner/effective_run_as/state) are read-only
    # and `parent_path` doesn't move an existing alert — including any of them makes the PATCH 400.
    _ALERT_V2_UPDATABLE = (
        "display_name", "query_text", "warehouse_id", "evaluation", "schedule",
        "custom_summary", "custom_description", "seconds_to_retrigger", "run_as_user_name")

    def _alert_v2_update_mask(self, body: dict) -> str:
        """Comma-joined `update_mask` of the settable top-level fields actually present in `body`.
        Falls back to `evaluation` (the field a threshold change always touches) so a sparse body
        still produces a non-empty mask rather than the "must contain a subfield" rejection."""
        fields = [f for f in self._ALERT_V2_UPDATABLE if f in body]
        return ",".join(fields) if fields else "evaluation"

    # ── shared ────────────────────────────────────────────────────────────
    def _remap_warehouse(self, body: dict, referenced_by: str = "") -> None:
        """Remap `warehouse_id` (and the legacy `data_source_id` spelling) onto the target warehouse
        THIS TOOL created for the source one — exact or fail loud (PLAN 11 Finding-10).

        Lift-and-shift: the ONLY legitimate remap is source-warehouse → the warehouse we recreated
        for it. A source warehouse in the bundle but not yet on target is a retryable prerequisite;
        one NOT in the bundle is a hard failure. The old "point it at any existing warehouse to keep
        it runnable" substitution is GONE — it made an object look migrated while silently querying a
        DIFFERENT warehouse (exactly the row-6 alert case).
        """
        remapped_any = False
        for field in ("warehouse_id", "data_source_id"):
            if field not in body:
                continue
            src = safe_str(body.get(field))
            if not src:
                continue      # empty sibling field; the no-warehouse check below decides
            body[field] = self.require_remap("sql_warehouse", src, referenced_by=referenced_by)
            remapped_any = True
        # An object that CARRIES a warehouse field but it is blank has no warehouse configured on
        # source — a conscious hard failure (row-6), not a silent create-without-a-warehouse.
        if not remapped_any and any(f in body for f in ("warehouse_id", "data_source_id")):
            raise HardRemapFailure(
                f"{referenced_by or 'this object'} has an empty warehouse_id — no warehouse is "
                f"configured on source, so there is nothing to remap. Configure it on source and "
                f"re-export (lift-and-shift does not substitute a warehouse).")
