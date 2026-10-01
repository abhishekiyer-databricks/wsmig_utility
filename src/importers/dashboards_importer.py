"""
DashboardsImporter — phase 8: AI/BI (Lakeview) dashboards (Plan 3 §6; PLAN 13 B7 + B13).

The dashboard's whole definition is the `serialized_dashboard` string, carried VERBATIM except for
two surgical edits:
  • **`warehouse_id` must be remapped**, or every widget queries a warehouse that doesn't exist.
  • **B13 catalog rename:** when `catalog_mapping` is set, catalog tokens inside `serialized_dashboard`
    (its dataset `queryLines` SQL) are rewritten source→target (token-boundary safe). Blank mapping →
    byte-identical to before.

**B7 publish-state parity.** A source *published* dashboard must land *published* on target (else
consumers open it and see nothing — only the editable draft came over). After the draft is created /
updated, a reconcile step:
  • publishes on target when source is published and the target differs (idempotently — publish is
    NOT idempotent, so it only (re)publishes on an actual diff, read-back verified);
  • preserves the `embed_credentials` MODE (Individual=per-viewer, Shared=embeds the run-as SP — the
    publish API embeds the CALLER and exposes no principal field, documented limitation);
  • recreates the dashboard's schedules (+ subscriptions, best-effort), warehouse remapped;
  • on a source *unpublish*, FLAGS the drift only — it NEVER unpublishes the target (user decision
    2026-10-01, like allow_deletes=false).
The row is ATOMIC: any required facet (draft OR publish OR schedule) failing → the whole row FAILED,
with a per-facet breakdown in the note.

`serialized_dashboard` references UC tables by fully-qualified name; UC is out of scope, so a
dashboard can import perfectly and still render empty until its tables exist on target — stated in
the note rather than left for the customer to discover.
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter, VerificationFailed
from src.utils.helpers import remap_catalog_refs, safe_str


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
        # B7: publish + schedule reconcile (atomic — a required facet failure raises → FAILED row).
        pub_note, pub_warn = self._reconcile_publish_and_schedules(unit, did)
        full_note = "; ".join(x for x in (note, pub_note) if x)
        # PLAN 11 Finding-8: an orphaned owner's dashboard is preserved under the backup root as
        # created_with_warning (parity with notebooks), never a hard prerequisite_missing failure.
        if res is not None and res.kind == "backup":
            return {"target_id": did, "warning": f"{res.note} {full_note}".strip()}
        if pub_warn:
            return {"target_id": did,
                    "warning": f"{full_note}; {pub_warn}" if full_note else pub_warn}
        return {"target_id": did, "note": full_note}

    def update_one(self, unit: dict, target_id: str) -> dict:
        body, note = self._body(unit)
        self.client.patch(f"api/2.0/lakeview/dashboards/{target_id}", body)
        pub_note, pub_warn = self._reconcile_publish_and_schedules(unit, target_id)
        full_note = "; ".join(x for x in (note, pub_note) if x)
        if pub_warn:
            return {"target_id": target_id,
                    "warning": f"{full_note}; {pub_warn}" if full_note else pub_warn}
        return {"target_id": target_id, "note": full_note}

    def _body(self, unit: dict) -> tuple[dict, str]:
        payload = dict(unit.get("payload") or {})
        body = {"display_name": safe_str(payload.get("display_name")) or self.natural_key(unit)}
        # Carried verbatim — the serialized definition is the dashboard — EXCEPT the B13 catalog
        # rename, a token-boundary-safe rewrite of catalog refs in the dataset queryLines (blank
        # mapping → unchanged, byte-identical to today).
        serialized = payload.get("serialized_dashboard")
        if serialized is not None:
            mapping = getattr(self.config, "catalog_mapping", None) or {}
            body["serialized_dashboard"] = remap_catalog_refs(serialized, mapping) if mapping \
                else serialized

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

    # ── B7 publish + schedule reconcile ───────────────────────────────────
    def _reconcile_publish_and_schedules(self, unit: dict, dashboard_id: str) -> tuple[str, str]:
        """Reconcile publish-state + schedules AFTER the draft exists. Returns `(note, warning)`;
        RAISES on a required-facet failure so the base class records the row FAILED (atomic)."""
        payload = unit.get("payload") or {}
        notes: list[str] = []

        src_published = bool(payload.get("is_published"))
        tgt = self._get_published(dashboard_id)
        if src_published:
            desired_embed = bool(payload.get("embed_credentials"))
            src_wh = safe_str(payload.get("published_warehouse_id") or payload.get("warehouse_id"))
            desired_wh = self.require_remap(
                "sql_warehouse", src_wh,
                referenced_by=f"published dashboard `{safe_str(payload.get('display_name'))}`"
            ) if src_wh else ""
            already = (tgt.get("is_published")
                       and bool(tgt.get("embed_credentials")) == desired_embed
                       and safe_str(tgt.get("warehouse_id")) == desired_wh)
            if already:
                # Publish is NOT idempotent (a re-publish mints a new revision) — so only (re)publish
                # on an ACTUAL diff. Nothing changed → leave it, mint no new revision.
                notes.append(f"published ({'Shared' if desired_embed else 'Individual'}): unchanged")
            else:
                self.client.post(f"api/2.0/lakeview/dashboards/{dashboard_id}/published",
                                 {"embed_credentials": desired_embed, "warehouse_id": desired_wh})
                after = self._get_published(dashboard_id)   # B4: read-back verify, don't trust 200
                ok = (after.get("is_published")
                      and bool(after.get("embed_credentials")) == desired_embed
                      and safe_str(after.get("warehouse_id")) == desired_wh)
                if not ok:
                    raise VerificationFailed(
                        f"publish did not apply: wanted embed_credentials={desired_embed}, "
                        f"warehouse={desired_wh!r}; read-back shows "
                        f"embed_credentials={after.get('embed_credentials')}, "
                        f"warehouse={after.get('warehouse_id')!r}")
                mode = "Shared (embeds the run-as SP)" if desired_embed else "Individual (per-viewer)"
                notes.append(f"published: {mode}")
        elif tgt.get("is_published"):
            # B7 user decision 2026-10-01: a source unpublish is FLAGGED ONLY — never unpublish the
            # target (non-destructive by default, like allow_deletes=false).
            return "; ".join(notes), (
                "source dashboard is now DRAFT-ONLY but the target is still PUBLISHED — the tool does "
                "NOT unpublish on target (flag-only, like allow_deletes=false). Unpublish it manually "
                "if the published view should be retired.")
        else:
            notes.append("draft-only")

        sched_note = self._reconcile_schedules(dashboard_id, payload.get("schedules") or [])
        if sched_note:
            notes.append(sched_note)
        return "; ".join(notes), ""

    def _get_published(self, dashboard_id: str) -> dict:
        """The target published snapshot, normalised to `{is_published, embed_credentials,
        warehouse_id}`. 404 (draft-only) → is_published False."""
        try:
            pub = self.client.get(f"api/2.0/lakeview/dashboards/{dashboard_id}/published") or {}
        except Exception:  # noqa: BLE001
            return {"is_published": False}
        is_pub = bool(pub) and any(
            k in pub for k in ("revision_create_time", "embed_credentials", "warehouse_id"))
        return {"is_published": is_pub, "embed_credentials": bool(pub.get("embed_credentials")),
                "warehouse_id": safe_str(pub.get("warehouse_id"))}

    def _reconcile_schedules(self, dashboard_id: str, source_schedules: list) -> str:
        """Recreate each source schedule not already on target (warehouse remapped, exact-or-fail);
        subscriptions are best-effort (recipient remapped). A schedule-create failure propagates (it
        fails the atomic row); a subscription failure is a warning, not a gate."""
        if not source_schedules:
            return ""
        try:
            doc = self.client.get(
                f"api/2.0/lakeview/dashboards/{dashboard_id}/schedules") or {}
            existing = {self._schedule_key(s) for s in (doc.get("schedules") or [])}
        except Exception:  # noqa: BLE001
            existing = set()
        created = 0
        for sch in source_schedules:
            key = safe_str(sch.get("display_name")) or safe_str(sch.get("cron_quartz"))
            if key in existing:
                continue
            body: dict = {"cron_schedule": {
                "quartz_cron_expression": safe_str(sch.get("cron_quartz")),
                "timezone_id": safe_str(sch.get("timezone_id")) or "UTC"}}
            if sch.get("display_name"):
                body["display_name"] = sch["display_name"]
            if sch.get("pause_status"):
                body["pause_status"] = sch["pause_status"]
            wh = safe_str(sch.get("warehouse_id"))
            if wh:
                body["warehouse_id"] = self.require_remap(
                    "sql_warehouse", wh, referenced_by="dashboard schedule")
            new = self.client.post(
                f"api/2.0/lakeview/dashboards/{dashboard_id}/schedules", body) or {}
            created += 1
            self._create_subscriptions(dashboard_id, safe_str(new.get("schedule_id")),
                                       sch.get("subscriptions") or [])
        return f"{created} schedule(s) created" if created else "schedules: unchanged"

    @staticmethod
    def _schedule_key(s: dict) -> str:
        return (safe_str(s.get("display_name"))
                or safe_str((s.get("cron_schedule") or {}).get("quartz_cron_expression")))

    def _create_subscriptions(self, dashboard_id: str, schedule_id: str, subs: list) -> None:
        """Best-effort subscription recreate (recipient principal remapped). A failure is a WARNING,
        never a gate — a schedule with no subscribers still runs."""
        if not schedule_id:
            return
        for sub in subs:
            if not isinstance(sub, dict):
                continue
            body = {"subscriber": self._remap_subscriber(sub.get("subscriber") or {})}
            try:
                self.client.post(
                    f"api/2.0/lakeview/dashboards/{dashboard_id}/schedules/{schedule_id}/"
                    f"subscriptions", body)
            except Exception as exc:  # noqa: BLE001 — subscriptions never gate the dashboard row
                self.result.warnings.append(
                    f"dashboard {dashboard_id} schedule {schedule_id}: a subscription could not be "
                    f"recreated ({str(exc)[:120]}) — re-add the recipient manually")

    def _remap_subscriber(self, subscriber: dict) -> dict:
        """Remap a subscriber's principal through the identity maps; a notification-destination
        subscriber (destination_id) passes through unchanged."""
        out = dict(subscriber)
        up = out.get("user_subscriber") or {}
        if isinstance(up, dict) and up.get("user_name"):
            out["user_subscriber"] = {**up, "user_name": self.resolve_principal(
                safe_str(up.get("user_name")), "user")}
        sp = out.get("service_principal_subscriber") or {}
        if isinstance(sp, dict) and sp.get("application_id"):
            out["service_principal_subscriber"] = {**sp, "application_id": self.resolve_principal(
                safe_str(sp.get("application_id")), "service_principal")}
        return out
