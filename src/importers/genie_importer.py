"""
GenieImporter — phase 9: Genie spaces (Plan 3 §6).

**Genie spaces ARE auto-migratable** — verified live on fvm1 2026-08-01. This supersedes the older
"serialized_space is an un-exportable protobuf" belief that earlier plans carried: the current API
returns the full `serialized_space` JSON on
`GET /api/2.0/genie/spaces/{id}?include_serialized_space=true`, and it round-trips through
`create_space` / `update_space`.

Same shape as Lakeview: the serialized payload is carried VERBATIM, only `warehouse_id` is remapped,
and the one real caveat is that `serialized_space` pins UC tables by fully-qualified name. UC is out
of scope, so those tables must pre-exist on target — a space can create successfully and still be
unusable until they do, which is why the note says so explicitly.
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter
from src.utils.helpers import safe_str


class GenieImporter(BaseImporter):
    component = "genie"
    asset_types = ("genie_space",)

    def load(self) -> list[dict]:
        return self.units_for("genie_space")

    def existing_keys(self) -> dict:
        """`{source full-path: space_id}` for spaces already on target.

        PLAN 11 Finding-9: keyed by full path (`<parent_path>/<title>`); the LIST omits
        parent_path, so matching goes through `folder_existing_keys` (id-anchor via state +
        collapse-safe unique-name adoption) — two same-named spaces in different folders no longer
        resolve to one target.
        """
        spaces = self.client.get_paginated("api/2.0/genie/spaces", "spaces",
                                           params={"page_size": 100})
        found = self.folder_existing_keys("genie_space", spaces, "title", "space_id")
        self.context.setdefault("genie_space_target_ids", {}).update(found)
        return found

    def create_one(self, unit: dict) -> dict:
        body, note = self._body(unit)
        # PLAN 8 Bug 7 (Genie sibling): recreate the space in its SOURCE folder, not the caller's
        # home. Verified live that Genie create HONORS parent_path. Only on CREATE — an update
        # doesn't move an existing space.
        parent = safe_str((unit.get("payload") or {}).get("parent_path"))
        res = None
        if parent:
            body["parent_path"] = parent
            res = self.remap_parent_path(body)
        try:
            created = self.client.post("api/2.0/genie/spaces", body)
        except Exception as exc:  # noqa: BLE001
            self.missing_parent_prerequisite(exc, body.get("parent_path"), self.natural_key(unit))
            raise
        sid = safe_str(created.get("space_id"))
        self.context.setdefault("genie_space_target_ids", {})[self.natural_key(unit)] = sid
        # PLAN 11 Finding-8: an orphaned owner's Genie space is preserved under the backup root as
        # created_with_warning (parity with notebooks), never a hard prerequisite_missing failure.
        if res is not None and res.kind == "backup":
            return {"target_id": sid, "warning": f"{res.note} {note}"}
        return {"target_id": sid, "note": note}

    def update_one(self, unit: dict, target_id: str) -> dict:
        """`update_space` — PATCH on the space, same body shape as create."""
        body, note = self._body(unit)
        self.client.patch(f"api/2.0/genie/spaces/{target_id}", body)
        return {"target_id": target_id, "note": note}

    def _body(self, unit: dict) -> tuple[dict, str]:
        payload = dict(unit.get("payload") or {})
        body = {"title": safe_str(payload.get("title")) or self.natural_key(unit)}
        if payload.get("description"):
            body["description"] = payload["description"]
        # Verbatim: the API round-trips exactly what it emitted, and rewriting it risks corrupting a
        # schema we don't own.
        if payload.get("serialized_space") is not None:
            body["serialized_space"] = payload["serialized_space"]

        notes = []
        src_wh = safe_str(payload.get("warehouse_id"))
        if src_wh:
            # PLAN 11 Finding-10: exact-or-fail-loud — no silent substitution to "any warehouse".
            body["warehouse_id"] = self.require_remap(
                "sql_warehouse", src_wh,
                referenced_by=f"genie space `{safe_str(payload.get('title'))}`")

        notes.append("serialized_space carried verbatim. NOTE it references Unity Catalog tables by "
                     "fully-qualified name, and UC is OUT OF SCOPE for this utility — the space "
                     "cannot answer questions until those tables exist on target.")
        return body, " ".join(notes)
