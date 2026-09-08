"""
GenieCollector — Genie spaces (SOURCE workspace). AUTO-MIGRATABLE (verified live fvm1 2026-08-01).

The per-space GET with `include_serialized_space=true` returns the full `serialized_space` JSON
(+ title, description, warehouse_id) — so a Genie space IS exportable and recreatable via
`create_space`/`update_space` on the target (contrary to the old public-REST limitation the
plan originally recorded; the reference tool `client_shared_utils/workspace_asset_migration`
confirmed this approach). We fetch that payload per space here so Export can emit a create-ready
body. natural_key = title.

Caveat carried to the target side: `serialized_space` references data sources by fully-qualified
UC name (e.g. `catalog.schema.table`); UC is out of scope for this utility, so those tables must
pre-exist on the target — same dependency class as dashboards. The warehouse_id is remapped on
import.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import dab_path_info, folder_natural_key, safe_str


class GenieCollector(BaseCollector):
    object_type = "genie_space"

    def natural_key(self, obj: dict) -> str:
        # PLAN 11 Finding-9: full path (`<parent_path>/<title>`), not the bare title, so two
        # same-named spaces in different folders don't collapse onto one target object.
        return folder_natural_key(obj.get("parent_path"), obj.get("title"))

    def discover(self) -> list[dict]:
        raw = self.client.get_paginated(
            "api/2.0/genie/spaces", "spaces",
            token_key="next_page_token", params={"page_size": 100},
        )
        items = []
        for s in raw:
            sid = safe_str(s.get("space_id") or s.get("id"))
            detail = self._space_detail(sid)
            # Genie spaces expose no deployment field; the list carries only `parent_path`
            # (coarser than dashboards' `path`), so DAB detection keys off that `.bundle/` folder.
            dab = dab_path_info(detail.get("parent_path") or s.get("parent_path"),
                                getattr(self.config, "dab_bundle_roots", None))
            items.append({
                "space_id": sid,
                "title": safe_str(detail.get("title") or s.get("title")),
                "description": safe_str(detail.get("description") or s.get("description")),
                "warehouse_id": safe_str(detail.get("warehouse_id") or s.get("warehouse_id")),
                "parent_path": safe_str(detail.get("parent_path") or s.get("parent_path")),
                # The create-ready body: serialized_space JSON (verified exportable). Kept as a
                # STRING exactly as the API returns it (target passes it back verbatim).
                "serialized_space": detail.get("serialized_space"),
                "has_serialized_space": bool(detail.get("serialized_space")),
                "deployed_by_dab": dab["deployed_by_dab"],
                "dab_scope": dab["dab_scope"],
                "acl": self.fetch_acl("genie", sid),   # ACLs (Plan 1a §1)
                "_raw": s,
            })
        return items

    def _space_detail(self, space_id: str) -> dict:
        """GET the space with serialized_space (best-effort; a failure leaves it un-migratable).

        Verified live: GET /api/2.0/genie/spaces/{id}?include_serialized_space=true returns
        {space_id,title,description,warehouse_id,parent_path,serialized_space,etag}.
        """
        if not space_id:
            return {}
        try:
            data = self.client.get(f"api/2.0/genie/spaces/{space_id}",
                                   params={"include_serialized_space": "true"})
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001 — best-effort; never abort the collector
            self.log.warning("genie space detail failed", space_id=space_id, error=str(exc))
            return {}
