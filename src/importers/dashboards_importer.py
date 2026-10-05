"""
DashboardsImporter — phase 8: AI/BI (Lakeview) dashboards (Plan 3 §6) + their publish state and
schedules (PLAN 16.2 §4).

The dashboard's whole definition is the `serialized_dashboard` string, which is carried VERBATIM —
we never parse or rewrite it. Only two things need attention:

  • **`warehouse_id` must be remapped**, or every widget queries a warehouse that doesn't exist.
  • **`serialized_dashboard` references UC tables by fully-qualified name.** UC is out of scope, so a
    dashboard can import perfectly and still render empty because its tables aren't on target. That
    is the single most common cause of a "successful" import producing a broken dashboard, so it is
    stated in the unit's note rather than left for the customer to discover.

**Publish + schedules are their OWN units** (`lakeview_dashboard_publish`,
`lakeview_dashboard_schedule`), processed AFTER every dashboard (schedules need a published
dashboard, F8). So the dashboard unit and its fingerprint are byte-identical to `main`, and the
upsert machinery (state, retry, dry run, report rows, checkpoint) covers the new facets for free.
Facts verified live 2026-10-05 (plan §4.0, F1–F14) that shape the code:
  • publish is NOT idempotent (every POST = a new revision, F4) → only POST on a real difference,
    then READ BACK (a 2xx is not proof);
  • the target publish is done BY the migration SP — no API publishes "as" someone else (docs);
  • published content = the source DRAFT (no API returns the published revision's content, F1) →
    a WARNING when the source draft has unpublished changes;
  • schedule duplicates are allowed (F9) → map by stored id, else adopt an exact match, never
    blindly create; a `PUT` replaces the whole schedule and needs the current etag (F10);
  • re-adding a subscriber returns the same subscription (F11) → safe on every create/update/adopt;
  • viewer-credential ("Individual data permissions") dashboards only allow SELF-subscription (F12)
    → no subscriber POST at all; ONE note on the schedule row listing every subscriber.
Never unpublishes and never deletes a schedule — a source unpublish / schedule delete is a
`deleted_in_source` flag only (state_store.DELETED_IN_SOURCE_NOTE).
"""
from __future__ import annotations

from src.importers.base_importer import (BaseImporter, NotApplied, PrerequisiteMissing,
                                         SkippedNoObject)
from src.state.state_store import (ACTED_STATUSES, ACTION_CREATED_WITH_WARNING,
                                   ACTION_SKIPPED_NO_OBJECT, CAT_OBJECT_ABSENT,
                                   CAT_UNIT_FAILED_EARLIER, UpsertAction)
from src.utils.helpers import is_not_found, safe_str

DASHBOARD = "lakeview_dashboard"
PUBLISH = "lakeview_dashboard_publish"
SCHEDULE = "lakeview_dashboard_schedule"

_VIEWER_NOTE = ("published with Individual data permissions — subscribers must re-subscribe "
                "themselves (Databricks only allows self-subscription)")


def _mode(embed: bool) -> str:
    return "publisher credentials" if embed else "viewer credentials"


class DashboardsImporter(BaseImporter):
    component = "dashboards"
    asset_types = (DASHBOARD, PUBLISH, SCHEDULE)
    reconcile_on_adopt_types = (PUBLISH, SCHEDULE)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._acted: set = set()            # dashboard/publish keys acted on THIS run (§5a)
        self._target_published: dict = {}   # target dashboard id → its GET …/published doc
        self._target_schedules: dict = {}   # target dashboard id → [schedule, …]
        self._claimed_sids: set = set()     # target schedule ids already matched this phase
        self._probed_sid: dict = {}         # schedule unit key → the target schedule id it matched
        self._user_ids: dict = {}           # userName → target user id ("" = not on target)
        self._destinations = None           # target notification destinations (listed once)

    def load(self) -> list[dict]:
        # Dashboards first, then their publish state, then schedules (a schedule needs a published
        # dashboard, F8) — the order is load-bearing.
        return self.units_for(DASHBOARD) + self.units_for(PUBLISH) + self.units_for(SCHEDULE)

    def existing_keys(self) -> dict:
        """`{source full-path: dashboard_id}` — PAGINATED (lakeview is a cursor API).

        PLAN 11 Finding-9: the natural key is the full path (`<parent_path>/<display_name>`), but
        the LIST omits parent_path, so matching goes through `folder_existing_keys` (id-anchor via
        state + collapse-safe unique-name adoption) — two same-named dashboards in different folders
        no longer resolve to one target. Publish/schedule units are probed lazily, per unit
        (`probe_existing`): their dashboard may only be created later in this same phase.
        """
        dashboards = self.client.get_paginated("api/2.0/lakeview/dashboards", "dashboards",
                                               params={"page_size": 100})
        found = self.folder_existing_keys("lakeview_dashboard", dashboards, "display_name",
                                          "dashboard_id")
        self.context.setdefault("lakeview_dashboard_target_ids", {}).update(found)
        return found

    # ── the dashboard itself (unchanged) ──────────────────────────────────
    def create_one(self, unit: dict) -> dict:
        at = safe_str(unit.get("asset_type"))
        if at == PUBLISH:
            return self._publish(unit, self._require_parent(unit), why="create")
        if at == SCHEDULE:
            return self._create_schedule(unit, self._require_parent(unit))
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
        at = safe_str(unit.get("asset_type"))
        if at == PUBLISH:
            return self._publish(unit, self._require_parent(unit), why="update")
        if at == SCHEDULE:
            return self._update_schedule(unit, self._require_parent(unit),
                                         self._probed_sid.get(self.natural_key(unit)) or target_id)
        body, note = self._body(unit)
        self.client.patch(f"api/2.0/lakeview/dashboards/{target_id}", body)
        return {"target_id": target_id, "note": note}

    def adopt_one(self, unit: dict, target_id: str) -> dict:
        at = safe_str(unit.get("asset_type"))
        if at == PUBLISH:
            return self._adopt_publish(unit, self._require_parent(unit))
        return self._adopt_schedule(unit, self._require_parent(unit),
                                    self._probed_sid.get(self.natural_key(unit)) or target_id)

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

    # ── retry: a dashboard's publish/schedule units follow it (PLAN 16.2 §5a) ──
    def in_work_list(self, unit: dict) -> bool:
        if super().in_work_list(unit):
            return True
        at = safe_str(unit.get("asset_type"))
        parent = safe_str(unit.get("parent_natural_key"))
        if at == PUBLISH:
            return (DASHBOARD, parent) in self._acted
        if at == SCHEDULE:
            return ((DASHBOARD, parent) in self._acted
                    or (PUBLISH, f"{parent}#published") in self._acted)
        return False

    def _record(self, unit: dict, status: str, **kw) -> None:
        super()._record(unit, status, **kw)
        if status in ACTED_STATUSES:
            self._acted.add((safe_str(unit.get("asset_type")), self.natural_key(unit)))

    # ── lazy existence + decision hooks ───────────────────────────────────
    def probe_existing(self, unit: dict, existing: dict) -> None:
        """Publish: the target dashboard's `GET …/published` = 200. Schedule: a target schedule
        that is the stored one, else an exact `(cron, timezone, display_name)` match."""
        at = safe_str(unit.get("asset_type"))
        if at not in (PUBLISH, SCHEDULE):
            return
        key = self.natural_key(unit)
        did = self._parent_target_id(unit)
        if not did:
            return                       # parent not on target → CREATE → skipped_no_object
        if at == PUBLISH:
            if self._get_published(did):
                existing[key] = did
            return
        sid = self._match_schedule(unit, did)
        if sid:
            existing[key] = sid
            self._probed_sid[key] = sid

    def recheck_unchanged(self, unit: dict, row) -> bool:
        """An unchanged unit is still revisited when its last outcome left work open: a schedule
        whose subscribers waited on a target user / destination (or carries the viewer-credential
        note), and a child that had no dashboard to attach to (`skipped_no_object`)."""
        last = safe_str((row or {}).get("last_action"))
        if last == ACTION_SKIPPED_NO_OBJECT and safe_str(unit.get("asset_type")) in (PUBLISH,
                                                                                    SCHEDULE):
            return True
        return safe_str(unit.get("asset_type")) == SCHEDULE and last == ACTION_CREATED_WITH_WARNING

    def dry_run_intent(self, unit: dict, action: UpsertAction) -> str:
        at = safe_str(unit.get("asset_type"))
        if at == PUBLISH:
            mode = _mode(bool((unit.get("payload") or {}).get("embed_credentials")))
            return {UpsertAction.CREATE: f"would publish ({mode})",
                    UpsertAction.UPDATE: f"would republish ({mode})",
                    UpsertAction.ADOPT: "would compare the target publish state and republish "
                                        "only if it differs"}.get(action, "")
        if at == SCHEDULE:
            return {UpsertAction.CREATE: f"would create schedule ({self._pause_for(unit)})",
                    UpsertAction.UPDATE: "would update schedule",
                    UpsertAction.ADOPT: "would adopt the existing schedule + add its "
                                        "subscribers"}.get(action, "")
        return ""

    # ── parent resolution ─────────────────────────────────────────────────
    def _parent_target_id(self, unit: dict) -> str:
        parent = safe_str(unit.get("parent_natural_key"))
        if not parent:
            return ""
        live = (self.context.get("lakeview_dashboard_target_ids") or {}).get(parent)
        if live:
            return safe_str(live)
        return safe_str(self.state.get_target_id(DASHBOARD, parent) or "") if self.state else ""

    def _require_parent(self, unit: dict) -> str:
        did = self._parent_target_id(unit)
        if did:
            return did
        parent = safe_str(unit.get("parent_natural_key"))
        row = self.state.row(DASHBOARD, parent) if self.state is not None else None
        failed = safe_str((row or {}).get("last_action")) == "failed"
        raise SkippedNoObject(
            f"its dashboard `{parent}` is not on target"
            + (" (its import FAILED)" if failed else " (not imported yet)")
            + " — nothing to publish/schedule. Fix the dashboard; this unit follows it on the "
              "next run or retry.",
            category=CAT_UNIT_FAILED_EARLIER if failed else CAT_OBJECT_ABSENT)

    # ── publish ───────────────────────────────────────────────────────────
    def _get_published(self, did: str) -> dict:
        """The target's `GET …/published` doc, or {} when it is a draft (404) / unreadable."""
        try:
            doc = self.client.get(f"api/2.0/lakeview/dashboards/{did}/published") or {}
        except Exception as exc:  # noqa: BLE001 — a draft 404s; anything else is logged
            if not is_not_found(exc):
                self.log.debug(f"published state of {did} unreadable: {str(exc)[:200]}")
            doc = {}
        self._target_published[did] = doc
        return doc

    def _desired_publish(self, unit: dict) -> tuple:
        payload = unit.get("payload") or {}
        embed = bool(payload.get("embed_credentials"))
        src_wh = safe_str(payload.get("warehouse_id"))
        wh = self.require_remap("sql_warehouse", src_wh, referenced_by=(
            f"published dashboard `{safe_str(unit.get('dashboard_display_name'))}`")) if src_wh \
            else ""
        return embed, wh

    @staticmethod
    def _publish_matches(doc: dict, embed: bool, wh: str) -> bool:
        return (bool(doc) and bool(doc.get("embed_credentials")) == embed
                and (not wh or safe_str(doc.get("warehouse_id")) == wh))

    def _publish(self, unit: dict, did: str, why: str) -> dict:
        """POST publish with the source mode + warehouse, then READ BACK (F3, F4)."""
        embed, wh = self._desired_publish(unit)
        body = {"embed_credentials": embed}
        if wh:
            body["warehouse_id"] = wh
        self.client.post(f"api/2.0/lakeview/dashboards/{did}/published", body)
        after = self._get_published(did)
        if not self._publish_matches(after, embed, wh):
            raise NotApplied(
                f"publish returned success but the read-back does not match: wanted "
                f"embed_credentials={embed}" + (f", warehouse_id={wh}" if wh else "")
                + f"; target shows embed_credentials={after.get('embed_credentials')!r}, "
                  f"warehouse_id={after.get('warehouse_id')!r}")
        verb = {"create": "published", "update": "republished (the source republished, switched "
                "credentials mode or warehouse)", "adopt": "republished to match the source"}[why]
        note = f"{verb} ({_mode(embed)})"
        if unit.get("unpublished_changes"):
            return {"target_id": did, "warning": (
                f"{note}; source draft has unpublished changes — target published = current "
                f"draft")}
        return {"target_id": did, "note": note}

    def _adopt_publish(self, unit: dict, did: str) -> dict:
        """Target already published: same mode + warehouse → adopted, NO POST (publish is not
        idempotent, F4); different → republish with the source values → updated."""
        embed, wh = self._desired_publish(unit)
        doc = self._target_published.get(did)
        if doc is None:
            doc = self._get_published(did)
        if self._publish_matches(doc, embed, wh):
            return {"changed": False, "target_id": did,
                    "note": f"already published on target ({_mode(embed)}, same warehouse) — not "
                            f"republished"}
        out = self._publish(unit, did, why="adopt")
        out["changed"] = True
        return out

    # ── schedules ─────────────────────────────────────────────────────────
    def _schedules_of(self, did: str) -> list:
        if did not in self._target_schedules:
            try:
                self._target_schedules[did] = list(self.client.get_paginated(
                    f"api/2.0/lakeview/dashboards/{did}/schedules", "schedules") or [])
            except Exception as exc:  # noqa: BLE001 — a draft 404s; the create then says why
                self.log.debug(f"schedules of {did} unreadable: {str(exc)[:200]}")
                self._target_schedules[did] = []
        return self._target_schedules[did]

    @staticmethod
    def _schedule_sig(sch: dict) -> tuple:
        cron = sch.get("cron_schedule") or {}
        return (safe_str(cron.get("quartz_cron_expression")), safe_str(cron.get("timezone_id")),
                safe_str(sch.get("display_name")))

    def _match_schedule(self, unit: dict, did: str) -> str:
        """The target schedule this unit IS: the stored id if still there, else an exact
        `(cron, timezone, display_name)` match not owned by another unit (F9 — duplicates are
        allowed, so the tool must never create a second copy of one a human recreated)."""
        targets = self._schedules_of(did)
        present = {safe_str(t.get("schedule_id")) for t in targets}
        key = self.natural_key(unit)
        stored = safe_str(self.state.get_target_id(SCHEDULE, key) or "") if self.state else ""
        if stored and stored in present:
            self._claimed_sids.add(stored)
            return stored
        owned = set(self._claimed_sids)
        if self.state is not None:
            owned |= {tid for nk, tid in self.state.target_ids_for(SCHEDULE).items() if nk != key}
        want = self._schedule_sig(unit.get("payload") or {})
        for t in targets:
            sid = safe_str(t.get("schedule_id"))
            if sid and sid not in owned and self._schedule_sig(t) == want:
                self._claimed_sids.add(sid)
                return sid
        return ""

    def _pause_for(self, unit: dict) -> str:
        """`pause_job_schedules=true` (the default) → PAUSED, else the source value. Same rule as
        jobs: the source keeps running during a migration, so an UNPAUSED copy would refresh twice
        and email every subscriber twice."""
        if self.config.transform.pause_job_schedules:
            return "PAUSED"
        return safe_str((unit.get("payload") or {}).get("pause_status")) or "UNPAUSED"

    def _schedule_body(self, unit: dict) -> dict:
        payload = unit.get("payload") or {}
        cron = payload.get("cron_schedule") or {}
        body = {"cron_schedule": {"quartz_cron_expression":
                                  safe_str(cron.get("quartz_cron_expression")),
                                  "timezone_id": safe_str(cron.get("timezone_id")) or "UTC"},
                "display_name": safe_str(payload.get("display_name")),
                "pause_status": self._pause_for(unit)}
        if safe_str(payload.get("warehouse_id")):
            name = safe_str(unit.get("dashboard_display_name"))
            body["warehouse_id"] = self.require_remap(
                "sql_warehouse", payload["warehouse_id"],
                referenced_by=f"schedule of dashboard `{name}`")
        return body

    def _pause_note(self, unit: dict, body: dict) -> str:
        src = safe_str((unit.get("payload") or {}).get("pause_status")) or "UNPAUSED"
        if body["pause_status"] == "PAUSED" and src != "PAUSED":
            return f"PAUSED (pause_job_schedules=true; source is {src})"
        return body["pause_status"]

    def _not_published_prerequisite(self, exc: Exception, unit: dict) -> None:
        if is_not_found(exc):
            raise PrerequisiteMissing(
                f"dashboard `{safe_str(unit.get('parent_natural_key'))}` is not PUBLISHED on "
                f"target "
                f"(its publish unit did not apply), and a schedule needs a published dashboard — "
                f"fix the publish unit, then re-run with retry_mode=failed_only.") from exc

    def _create_schedule(self, unit: dict, did: str) -> dict:
        body = self._schedule_body(unit)
        try:
            created = self.client.post(f"api/2.0/lakeview/dashboards/{did}/schedules", body) or {}
        except Exception as exc:  # noqa: BLE001
            self._not_published_prerequisite(exc, unit)
            raise
        sid = safe_str(created.get("schedule_id"))
        self._schedules_of(did).append({**body, "schedule_id": sid})
        self._claimed_sids.add(sid)
        return self._with_subscribers(unit, did, sid,
                                      f"schedule created {self._pause_note(unit, body)}")

    def _update_schedule(self, unit: dict, did: str, sid: str) -> dict:
        """`PUT` the FULL body with the current etag (F10 — an omitted field is blanked)."""
        current = self.client.get(f"api/2.0/lakeview/dashboards/{did}/schedules/{sid}") or {}
        body = self._schedule_body(unit)
        if current.get("etag"):
            body["etag"] = current["etag"]
        self.client.put(f"api/2.0/lakeview/dashboards/{did}/schedules/{sid}", body)
        return self._with_subscribers(unit, did, sid,
                                      f"schedule updated in place {self._pause_note(unit, body)}")

    def _adopt_schedule(self, unit: dict, did: str, sid: str) -> dict:
        """An identical schedule already exists on target → adopt it (never a duplicate, F9) and
        make sure its subscribers are there (idempotent, F11). Its pause state is left alone."""
        out = self._with_subscribers(unit, did, sid, "existing identical schedule adopted")
        out["changed"] = False
        return out

    # ── subscriptions ─────────────────────────────────────────────────────
    def _with_subscribers(self, unit: dict, did: str, sid: str, note: str) -> dict:
        added, manual = self._apply_subscribers(unit, did, sid)
        if added:
            note += f"; {added} subscriber(s) subscribed"
        if manual:
            return {"target_id": sid, "warning": "; ".join([note] + manual)}
        return {"target_id": sid, "note": note}

    def _apply_subscribers(self, unit: dict, did: str, sid: str) -> tuple:
        """Add every source subscriber (idempotent, F11). Returns `(added, manual_lines)`. Extra
        target subscribers are never removed."""
        subs = (unit.get("payload") or {}).get("subscribers") or []
        if not subs:
            return 0, []
        if not unit.get("parent_embed_credentials"):
            # F12 — a platform rule: on a viewer-credential dashboard NOBODY can subscribe another
            # user (nor a destination). ONE note listing every subscriber, no POST at all.
            who = [safe_str(s.get("user_name") or s.get("user_id") or s.get("display_name")
                            or s.get("destination_id")) for s in subs]
            return 0, [f"{_VIEWER_NOTE}: {', '.join(w for w in who if w)}"]
        added, manual = 0, []
        path = f"api/2.0/lakeview/dashboards/{did}/schedules/{sid}/subscriptions"
        for s in subs:
            if s.get("kind") == "user":
                name = safe_str(s.get("user_name"))
                if not name:
                    manual.append(f"subscriber with source user id {safe_str(s.get('user_id'))} "
                                  f"is not in the source user roster — subscribe the right user "
                                  f"by hand")
                    continue
                uid = self._target_user_id(name)
                if not uid:
                    manual.append(f"user {name} is not on target — subscribe them by hand once "
                                  f"assigned (or re-run)")
                    continue
                body, who = {"subscriber": {"user_subscriber": {"user_id": uid}}}, name
            else:
                name = safe_str(s.get("display_name"))
                dest = self._target_destination_id(name, safe_str(s.get("destination_type")))
                if not dest:
                    label = name or safe_str(s.get("destination_id"))
                    manual.append(f"create notification destination {label} on target, then "
                                  f"re-run")
                    continue
                body = {"subscriber": {"destination_subscriber": {"destination_id": dest}}}
                who = name
            try:
                self.client.post(path, body)
                added += 1
            except Exception as exc:  # noqa: BLE001 — one subscriber must not lose the schedule
                manual.append(f"subscriber {who} could not be added ({str(exc)[:160]}) — add "
                              f"by hand")
        return added, manual

    def _target_user_id(self, user_name: str) -> str:
        """Target SCIM id of a user, by userName: the identity map, else a SCIM lookup (cached)."""
        if user_name not in self._user_ids:
            uid = safe_str((self.identity_map.get("scim_ids") or {}).get(f"user:{user_name}"))
            if not uid:
                try:
                    doc = self.client.get("api/2.0/preview/scim/v2/Users",
                                          params={"filter": f'userName eq "{user_name}"'}) or {}
                    res = doc.get("Resources") or []
                    uid = safe_str(res[0].get("id")) if res else ""
                except Exception as exc:  # noqa: BLE001 — degraded: reported as a manual line
                    self.log.debug(f"SCIM lookup of {user_name} failed: {str(exc)[:200]}")
                    uid = ""
            self._user_ids[user_name] = uid
        return self._user_ids[user_name]

    def _target_destination_id(self, name: str, dtype: str) -> str:
        """A target notification destination with the same name + type. Never created by the tool
        (out of scope) — adopted by name, else a manual line."""
        if not name:
            return ""
        if self._destinations is None:
            try:
                self._destinations = list(self.client.get_paginated(
                    "api/2.0/notification-destinations", "results") or [])
            except Exception as exc:  # noqa: BLE001 — degraded: every destination becomes manual
                self.log.warning(f"could not list notification destinations on target — "
                                 f"destination subscribers become manual steps: {exc}")
                self._destinations = []
        for d in self._destinations:
            if (safe_str(d.get("display_name")) == name
                    and (not dtype or safe_str(d.get("destination_type")) == dtype)):
                return safe_str(d.get("id"))
        return ""
