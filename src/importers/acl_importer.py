"""
AclImporter — phase 12 (LAST): object permissions + the deferred principal remap (Plan 3 §6b, §6b-i).

**Why ACLs are dead last and separate.** A grant names a *principal* AND an *object*, so it can only
be applied once BOTH id maps exist. Export deliberately put them in their own `acls.json` for exactly
this reason.

**What "drop" means, precisely.** `PUT permissions/{type}/{id}` is DECLARATIVE AND ABSOLUTE: the body
you send becomes the object's complete explicit ACL, so anything omitted is REMOVED. That makes the
body contents load-bearing, which is why each omission below needs a real justification — and why
"drop" only ever meant "don't include in the PUT body", never "delete from target" or "hide from the
report". Every grant stays visible in `acls.json` and in the parity report regardless.

  • `inherited: true` — NOT SENT. These aren't the object's own ACL; they're a computed echo of an
    ancestor's grant that the API returns read-only. The target recomputes them from its own tree, and
    the ancestor's explicit grant is itself migrated. Sending them fails, or creates a spurious
    explicit grant the source didn't have — i.e. sending them is what BREAKS parity.
  • the `admins` group — NOT SENT. Built-in, workspace-local, always exists on target with
    unconditional admin, and has no source→target id to remap. The API reports it and rejects it on
    write. Target admins already have this access by construction.
  • the `/Shared` root — NOT SENT. The API rejects writes to it outright.
  • a grant whose object this run did NOT create or adopt — NOT SENT, because there is nothing to
    attach it to. This is a RUNTIME predicate, not a `.bundle/` path check (D17): two of its seven
    cases are dynamic (a unit that failed earlier in THIS run; a family deferred by `import_assets`),
    so a path-based rule would have been a silent bug in each.

**Parity is PROVEN, not asserted.** The phase ends with a post-apply diff (`acl_parity_report`):
re-GET every touched object, normalise both sides (resolving principals through the identity map,
sorting, and dropping `inherited` on BOTH sides so like is compared with like), and diff against the
exported source ACL. That is the one report to read after an import.

**ACL rows are first-class state rows** (§6b-i, D23) — one per OBJECT, not per grant, because
`PUT permissions` is declarative over the whole object (a per-grant row would imply we can retry a
single grant, which the API cannot do). Without these rows a skipped grant would be INVISIBLE to
`retry_mode`, and skipped grants are the units most likely to need a second pass.
"""
from __future__ import annotations

from src.exporters import bundle_paths as BP
from src.importers.base_importer import BaseImporter, SkippedNoObject
from src.state.state_store import (ACTION_FAILED, ACTION_SKIPPED_NO_OBJECT, CAT_DAB_REDEPLOY,
                                   CAT_FAMILY_NOT_SELECTED, CAT_LEGACY_DASHBOARD, CAT_NOT_SUPPORTED,
                                   CAT_OVERSIZE, CAT_REPO_OUT_OF_SCOPE, CAT_UC_BACKED,
                                   CAT_UNIT_FAILED_EARLIER)
from src.transform.transforms import fingerprint
from src.utils.helpers import safe_str

# The built-in group that always exists on target with unconditional admin, has no id to remap, and
# is rejected on write. Omitting it PRESERVES parity rather than breaking it.
_BUILTIN_ADMINS = "admins"

# Objects whose ACL the API refuses to change.
_IMMUTABLE_PATHS = ("/Shared",)

# permissions-API object type → the asset_type whose target ids resolve it. `directories`/`notebooks`/
# `files` are resolved by PATH rather than by a stored id, so they map to their content asset_type.
_PERM_TYPE_TO_ASSET = {
    "clusters": "cluster",
    "instance-pools": "instance_pool",
    "cluster-policies": "cluster_policy",
    "jobs": "job",
    "pipelines": "dlt_pipeline",
    "notebooks": "notebook",
    "directories": "directory",
    "files": "workspace_file",
    "repos": "repo",
    "sql/warehouses": "sql_warehouse",
    "dashboards": "lakeview_dashboard",
    "queries": "legacy_query",
    "alerts": "legacy_alert",
    "alertsv2": "alert_v2",
    "genie": "genie_space",
    "serving-endpoints": "serving_endpoint",
    "secret-scope": "secret_scope",
}

# Why a grant's object might legitimately not be on target (§6b, D17) — recorded in
# `failure_category` so "which permissions are still outstanding, and why" is a SQL query.
_ABSENCE_REASON = {
    "dab_redeploy": (CAT_DAB_REDEPLOY,
                     "the object is bundle-owned (.bundle/), so the customer's "
                     "`databricks bundle deploy` recreates it — re-run import_assets=acls with "
                     "retry_mode=skipped_only after the redeploy lands"),
    "repo": (CAT_REPO_OUT_OF_SCOPE,
             "Git repos are out of scope for import, so the object was never created — the grants "
             "are listed here so whoever recreates the repo can reapply access"),
    "legacy_dashboard": (CAT_LEGACY_DASHBOARD,
                         "legacy SQL dashboards cannot be created via the API on modern workspaces, "
                         "so there is no object to grant on"),
    "failed": (CAT_UNIT_FAILED_EARLIER,
               "the object FAILED to import earlier in this run, so its grants have nothing to "
               "attach to — fix that failure and retry; the ACL follows"),
    "not_selected": (CAT_FAMILY_NOT_SELECTED,
                     "the object's family was not selected in this run (import_assets), so it does "
                     "not exist yet — import that family, then re-run import_assets=acls"),
    "oversize": (CAT_OVERSIZE,
                 "the object exceeded an API size cap and was never exported, so it was never "
                 "created on target"),
    "uc_backed": (CAT_UC_BACKED,
                  "the object is Unity Catalog-backed and UC is out of scope, so it could not be "
                  "recreated here"),
}


class AclImporter(BaseImporter):
    component = "acls"
    asset_types = ("acl",)
    # An ACL is declarative against an object that already exists, so "the object is on target" does
    # NOT mean the grants were applied — an adopt must still perform the PUT.
    declarative_asset_types = ("acl",)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._applied: list[dict] = []       # (entry, target_id) pairs for the parity pass
        self._source_acls: list[dict] = []

    # ── load: acls.json → one unit per OBJECT ─────────────────────────────
    def load(self) -> list[dict]:
        """Turn `export/acls.json` into one unit per object (not per grant).

        The object is the unit of work because `PUT permissions` is declarative over the whole
        object. The unit's fingerprint is the hash of its NORMALISED GRANT SET, so an ACL changed on
        source moves it and a re-run replays only genuinely-changed ACLs.
        """
        self._source_acls = self.staging.read_json(BP.EXPORT_ACLS_JSON) or []
        units: list[dict] = []
        for entry in self._source_acls:
            if not isinstance(entry, dict):
                continue
            asset_type = safe_str(entry.get("asset_type"))
            natural_key = safe_str(entry.get("natural_key"))
            perm_type = safe_str(entry.get("perm_object_type"))
            grants = entry.get("grants") or []
            units.append({
                "asset_type": "acl",
                # `<perm_object_type>:<object natural_key>` — scoped by type so a cluster and a job
                # with the same name can't collide.
                "natural_key": f"{perm_type}:{natural_key}",
                "source_id": safe_str(entry.get("source_id")),
                "fingerprint": fingerprint(self._normalise_grants(grants, remap=False)),
                "import_action": "apply_acl",
                "export_status": "success",
                "payload": {"perm_object_type": perm_type, "object_asset_type": asset_type,
                            "object_natural_key": natural_key, "grants": grants},
                "note": "",
            })
        return units

    def existing_keys(self) -> dict:
        """`{acl natural_key: target_object_id}` for ACLs this tool has ALREADY applied.

        An ACL has no existence of its own, so "already there" means "we applied it, and the grant
        set has not changed on source since". Reporting those lets the fingerprint decide SKIP —
        which §6b-i asks for: *re-runs replay only genuinely-changed ACLs*. Without this, every run
        re-PUT every object's permissions (correct, since the call is declarative and idempotent, but
        one API call per object per run — on a 5,000-object workspace that is the slowest thing in
        the tool for no benefit).

        The object's real existence is still checked per unit inside `create_one`, so a target object
        that has since been deleted is still caught.
        """
        if self.state is None:
            return {}
        return {nk: tid for nk, tid in self.state.target_ids_for("acl").items() if tid}

    # ── apply ─────────────────────────────────────────────────────────────
    def create_one(self, unit: dict) -> dict:
        payload = unit.get("payload") or {}
        perm_type = safe_str(payload.get("perm_object_type"))
        object_key = safe_str(payload.get("object_natural_key"))
        object_asset_type = safe_str(payload.get("object_asset_type"))
        grants = payload.get("grants") or []

        if self._is_immutable(perm_type, object_key):
            raise SkippedNoObject(
                f"`{object_key}` is a workspace root whose ACL the API refuses to change, so it was "
                f"not attempted. Its access is fixed by the platform on BOTH sides, so this is not a "
                f"parity gap.", category=CAT_NOT_SUPPORTED)

        target_id = self._resolve_target_object(perm_type, object_asset_type, object_key)
        if not target_id:
            category, guidance = _ABSENCE_REASON[self._absence_reason(object_asset_type, object_key)]
            raise SkippedNoObject(
                f"the target object `{object_key}` ({object_asset_type}) does not exist, so its "
                f"{len(grants)} grant(s) could not be applied: {guidance}", category=category)

        # A secret scope's ACL is NOT the permissions API. It has its own endpoint, its own verb
        # (one call PER PRINCIPAL rather than one declarative body), and its own permission
        # vocabulary (READ/WRITE/MANAGE). Sending it to `PUT permissions/...` would 404.
        if perm_type == "secret-scope":
            return self._apply_secret_acls(object_key, grants)

        body = self._acl_body(grants)
        if not body["access_control_list"]:
            return {"target_id": target_id,
                    "note": ("every grant on this object was an inherited echo or the built-in "
                             "`admins` grant, so there is no explicit ACL to apply — target access "
                             "already matches by construction")}

        self.client.put(f"api/2.0/permissions/{perm_type}/{target_id}", body)
        self._applied.append({"perm_type": perm_type, "target_id": target_id,
                              "object_natural_key": object_key, "grants": grants})
        return {"target_id": target_id,
                "note": f"{len(body['access_control_list'])} explicit grant(s) applied"}

    def update_one(self, unit: dict, target_id: str) -> dict:
        """`PUT permissions` is already declarative — an update IS the same call."""
        return self.create_one(unit)

    # ── the PUT body (the load-bearing part) ──────────────────────────────
    def _acl_body(self, grants: list) -> dict:
        """Build the declarative body: every explicit grant, principals remapped.

        Omissions, each with its reason, are documented at the top of this module. Everything else
        travels VERBATIM — that is the apple-to-apple content.
        """
        by_principal: dict[tuple, set] = {}
        for grant in grants or []:
            if not isinstance(grant, dict):
                continue
            if grant.get("inherited"):
                continue                       # a computed echo, not this object's own ACL
            principal = safe_str(grant.get("principal"))
            ptype = safe_str(grant.get("principal_type"))
            level = safe_str(grant.get("permission_level"))
            if not principal or not level:
                continue
            if ptype == "group" and principal == _BUILTIN_ADMINS:
                continue                       # always exists on target; rejected on write
            target_principal = self._remap_principal(principal, ptype)
            if not target_principal:
                continue
            by_principal.setdefault((target_principal, ptype), set()).add(level)

        acl: list[dict] = []
        for (principal, ptype), levels in by_principal.items():
            field = {"user": "user_name", "group": "group_name",
                     "service_principal": "service_principal_name"}.get(
                         ptype, self._guess_principal_field(principal))
            for level in sorted(levels):
                acl.append({field: principal, "permission_level": level})
        return {"access_control_list": acl}

    def _apply_secret_acls(self, scope: str, grants: list) -> dict:
        """Apply a secret scope's ACLs via `secrets/acls/put` — one call PER PRINCIPAL.

        Different from every other ACL in three ways, all of which a shared code path would get
        wrong: a different endpoint, a per-principal call rather than one declarative body, and its
        own permission vocabulary (READ/WRITE/MANAGE). Because it is per-principal it is also NOT
        absolute — an existing grant this bundle doesn't mention survives, which is why the parity
        report treats scopes as additive rather than reporting them as `extra_on_target`.

        `users:MANAGE` is skipped: it was set at scope-create via `initial_manage_principal` and
        cannot be patched, so re-putting it is at best a no-op and at worst an error.
        """
        # Deliberately NOT added to `self._applied`: the parity diff assumes a declarative ACL, and a
        # scope's grants are per-principal and ADDITIVE, so a grant on target that this bundle doesn't
        # mention is not a divergence — diffing it would report false positives.
        applied, failed = 0, []
        for grant in grants or []:
            if not isinstance(grant, dict) or grant.get("inherited"):
                continue
            principal = safe_str(grant.get("principal"))
            level = safe_str(grant.get("permission_level")).upper()
            if not principal or not level:
                continue
            target_principal = self._remap_principal(principal,
                                                     safe_str(grant.get("principal_type")))
            if not target_principal:
                failed.append(f"{principal} (unresolvable principal)")
                continue
            if target_principal == "users" and level == "MANAGE":
                continue     # set at create via initial_manage_principal; cannot be patched
            try:
                self.client.post("api/2.0/secrets/acls/put",
                                 {"scope": scope, "principal": target_principal,
                                  "permission": level})
                applied += 1
            except Exception as exc:  # noqa: BLE001 — one bad grant must not lose the others
                failed.append(f"{target_principal}={level} ({str(exc)[:90]})")

        note = f"{applied} secret-scope ACL(s) applied via secrets/acls/put"
        if failed:
            note += f"; {len(failed)} could not be applied: {', '.join(failed[:4])}"
        return {"target_id": scope, "note": note, "warning": note if failed else ""}

    def _remap_principal(self, principal: str, ptype: str) -> str:
        """Resolve a SOURCE principal to its TARGET equivalent, by NAME never by source id.

        A recreated Databricks-managed SP has a NEW applicationId, so its grants must go through the
        SP map or they would name an identity that doesn't exist. An unresolvable principal is
        reported and DROPPED from the body rather than failing the whole object's PUT — one unknown
        principal must not cost the object every other grant it has.
        """
        sp_map = self.identity_map.get("sp_mapping") or {}
        user_map = self.identity_map.get("user_map") or {}
        if ptype == "service_principal" or principal in sp_map:
            mapped = sp_map.get(principal, "")
            if mapped:
                return mapped
            self.result.warnings.append(
                f"ACL principal (service principal) {principal!r} is not in the identity map, so "
                f"that grant was omitted — if it was a workspace-local SP it has a different "
                f"applicationId on target; import identity first, then re-run import_assets=acls")
            return ""
        if ptype == "user":
            return user_map.get(principal, principal)
        return principal      # groups keep their displayName; built-ins already exist on target

    @staticmethod
    def _guess_principal_field(principal: str) -> str:
        """The secret-scope ACL API doesn't type its principals, so infer from the value's shape."""
        return "user_name" if "@" in principal else "group_name"

    # ── object resolution ─────────────────────────────────────────────────
    def _resolve_target_object(self, perm_type: str, object_asset_type: str,
                               object_key: str) -> str:
        """The TARGET object id this grant applies to, or "" if it isn't on target.

        Workspace content is resolved by PATH (`workspace/get-status` returns the target's own
        object_id — the source id is meaningless), everything else through the state table's stored
        target id, which is what lets an object imported in an EARLIER session still get its ACL.
        """
        if perm_type in ("directories", "notebooks", "files"):
            # Workspace content may have been diverted from its source path — a recreated SP's home
            # (IMP-6) or an orphaned owner's home backed up to /Users_Backup (PLAN 9 §4.5). The
            # workspace importer published `workspace_path_remap` for exactly these, so the ACL
            # attaches to the object's ACTUAL target path rather than 404ing on the source path.
            remap = self.context.get("workspace_path_remap") or {}
            resolved = remap.get(object_key, object_key)
            status = self._get_status(resolved)
            return safe_str(status.get("object_id")) if status else ""
        if perm_type == "secret-scope":
            # A scope's NAME is its identifier, so "resolving" it means confirming the scope EXISTS
            # on target — otherwise `secrets/acls/put` would fail with an opaque error instead of the
            # honest "the secrets family has not been imported yet".
            if object_key in (self.target_id_map("secret_scope")
                              or self.context.get("secret_scope_target_ids") or {}):
                return object_key
            return ""
        asset_type = _PERM_TYPE_TO_ASSET.get(perm_type, object_asset_type)
        return safe_str(self.target_id_map(asset_type).get(object_key, ""))

    def _get_status(self, path: str) -> dict:
        try:
            return self.client.get("api/2.0/workspace/get-status", params={"path": path}) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _absence_reason(self, object_asset_type: str, object_key: str) -> str:
        """WHICH of the seven cases applies — the runtime predicate, not a path check (D17)."""
        # PLAN 11 Finding-12: honour configurable bundle roots — a Team-B bundle rooted at a plain
        # directory (no `.bundle` segment) is still recognised as DAB-owned, so its ACL reads
        # `dab_redeploy` (re-run after the redeploy) rather than the generic not_selected.
        from src.utils.helpers import dab_path_info
        if ("/.bundle/" in object_key or object_key.endswith("/.bundle")
                or dab_path_info(object_key,
                                 getattr(self.config, "dab_bundle_roots", None))["deployed_by_dab"]):
            return "dab_redeploy"
        if object_asset_type == "repo":
            return "repo"
        if object_asset_type == "legacy_dashboard":
            return "legacy_dashboard"
        # Dynamic cases — these are why the rule cannot be path-based: the same object is creatable
        # on one run and absent on another.
        if self.state is not None:
            row = self.state.row(object_asset_type, object_key)
            action = safe_str((row or {}).get("last_action"))
            if action == "failed":
                return "failed"
            if action == "not_selected":
                return "not_selected"
        from src.importers.phases import family_of
        family = family_of(object_asset_type)
        if family and not self.config.imports.selects(family):
            return "not_selected"
        return "not_selected"

    @staticmethod
    def _is_immutable(perm_type: str, object_key: str) -> bool:
        return perm_type == "directories" and safe_str(object_key).rstrip("/") in _IMMUTABLE_PATHS

    # ── run: apply, then PROVE parity ─────────────────────────────────────
    def run(self):
        result = super().run()
        try:
            self.write_parity_report()
        except Exception as exc:  # noqa: BLE001 — verification must not fail the run
            self.log.warning("acl parity report failed", error=str(exc)[:200])
            result.warnings.append(f"could not produce the ACL parity report: {exc}")
        try:
            self.context["acl_grants"] = self._build_grant_detail()
        except Exception as exc:  # noqa: BLE001 — report enrichment must not fail the run
            self.log.warning("acl grant detail failed", error=str(exc)[:200])
        return result

    # ── per-grant report detail (Bug 15) ──────────────────────────────────
    def _build_grant_detail(self) -> list[dict]:
        """Expand `acls.json` back to ONE ROW PER object×principal×permission, each stamped with the
        precise import outcome — so the import report can MIRROR the inventory "Object Permissions
        (ACLs)" sheet (Object Type · Object · Principal · Permission · Inherited) plus a per-grant
        Import Status, instead of collapsing each object to one `acl` row + a count (Bug 15).

        The object-level outcome is read from the ACL UNIT rows (keyed `<perm_type>:<object_nk>`);
        the per-grant applied/dropped/skipped decision reuses the SAME predicates the PUT body is
        built from (`_acl_body`), so the report can never disagree with what was actually sent. The
        base is `acls.json` (every grant, inherited ones included) so the sheet has the same row
        fidelity as inventory.
        """
        unit_by_key = {safe_str(u.get("natural_key")): u for u in self.result.units}
        rows: list[dict] = []
        for entry in self._source_acls:
            if not isinstance(entry, dict):
                continue
            perm_type = safe_str(entry.get("perm_object_type"))
            object_nk = safe_str(entry.get("natural_key"))
            source_id = safe_str(entry.get("source_id"))
            unit = unit_by_key.get(f"{perm_type}:{object_nk}") or {}
            unit_status = safe_str(unit.get("import_status"))
            target_id = safe_str(unit.get("target_id"))
            for grant in entry.get("grants") or []:
                if not isinstance(grant, dict):
                    continue
                principal = safe_str(grant.get("principal"))
                level = safe_str(grant.get("permission_level"))
                if not principal or not level:
                    continue
                rows.append({
                    "perm_object_type": perm_type, "object": object_nk,
                    "source_id": source_id, "target_id": target_id,
                    "principal": principal, "principal_type": safe_str(grant.get("principal_type")),
                    "permission": level, "inherited": bool(grant.get("inherited")),
                    "import_status": self._grant_status(perm_type, unit_status, grant),
                })
        return rows

    def _grant_status(self, perm_type: str, unit_status: str, grant: dict) -> str:
        """The per-grant Import Status shown in the report, derived from the object outcome + the
        exact skip/drop rules `_acl_body` applies. See the module docstring for why each grant is or
        isn't sent."""
        if unit_status == ACTION_FAILED:
            return "failed"
        if unit_status == ACTION_SKIPPED_NO_OBJECT or not unit_status:
            return "skipped — no target object"
        principal = safe_str(grant.get("principal"))
        ptype = safe_str(grant.get("principal_type"))
        level = safe_str(grant.get("permission_level"))
        if grant.get("inherited"):
            return "skipped — inherited/built-in"
        if ptype == "group" and principal == _BUILTIN_ADMINS:
            return "skipped — inherited/built-in"
        if perm_type == "secret-scope" and principal == "users" and level.upper() == "MANAGE":
            return "skipped — inherited/built-in"    # set at scope-create; cannot be patched
        if not self._principal_resolvable(principal, ptype):
            return "dropped — principal not on target"
        return "applied"

    def _principal_resolvable(self, principal: str, ptype: str) -> bool:
        """Whether `_remap_principal` would yield a target principal (a pure check — no warning side
        effect, unlike `_remap_principal`)."""
        sp_map = self.identity_map.get("sp_mapping") or {}
        if ptype == "service_principal" or principal in sp_map:
            return bool(sp_map.get(principal))
        return True     # users map to themselves; groups keep their displayName / are built-in

    # ── the parity report (§6b) ───────────────────────────────────────────
    def _objects_to_verify(self) -> list[dict]:
        """Every object whose ACL SHOULD now be right on target — applied this run OR previously.

        Verifying only what this run applied looked reasonable but made the report useless exactly
        when it matters most: on a re-run, unchanged ACLs correctly SKIP, so `_applied` is empty and
        the report came back "0 objects checked" — i.e. the parity evidence vanished on every run
        after the first. Parity is a claim about the TARGET's current state, not about this run's
        activity, so anything with a recorded ACL state row is re-read and diffed.
        """
        seen = {(a["perm_type"], a["target_id"]) for a in self._applied}
        out = list(self._applied)
        if self.state is None:
            return out
        # Re-derive the grant set from the bundle for objects we skipped this run.
        by_key = {}
        for entry in self._source_acls:
            if not isinstance(entry, dict):
                continue
            key = f"{safe_str(entry.get('perm_object_type'))}:{safe_str(entry.get('natural_key'))}"
            by_key[key] = entry
        for natural_key, target_id in self.state.target_ids_for("acl").items():
            entry = by_key.get(natural_key)
            if not entry or not target_id:
                continue
            perm_type = safe_str(entry.get("perm_object_type"))
            if perm_type == "secret-scope":
                # A scope's ACL is per-principal and additive, not declarative, so an "extra" grant
                # on target is not a divergence — the diff would report false positives.
                continue
            if (perm_type, target_id) in seen:
                continue
            out.append({"perm_type": perm_type, "target_id": target_id,
                        "object_natural_key": safe_str(entry.get("natural_key")),
                        "grants": entry.get("grants") or []})
        return out

    def write_parity_report(self) -> dict:
        """Re-GET every touched object and DIFF against source — turning belief into evidence.

        Both sides are normalised the same way: principals resolved through the identity map, levels
        sorted, and `inherited` dropped on BOTH sides so like is compared with like (the target
        recomputes inheritance from its own tree, so comparing raw GETs would report differences that
        aren't real).
        """
        objects: list[dict] = []
        counts = {"match": 0, "extra_on_target": 0, "missing_on_target": 0, "both": 0}

        for applied in self._objects_to_verify():
            perm_type = applied["perm_type"]
            target_id = applied["target_id"]
            expected = self._normalise_grants(applied["grants"], remap=True)
            try:
                doc = self.client.get(f"api/2.0/permissions/{perm_type}/{target_id}") or {}
            except Exception as exc:  # noqa: BLE001
                objects.append({"perm_object_type": perm_type, "target_id": target_id,
                                "object": applied["object_natural_key"], "verdict": "unverified",
                                "detail": f"could not re-read the ACL: {str(exc)[:160]}"})
                continue
            actual = self._normalise_from_api(doc.get("access_control_list") or [])

            expected_set, actual_set = set(expected), set(actual)
            missing = sorted(expected_set - actual_set)
            extra = sorted(actual_set - expected_set)
            present = sorted(expected_set & actual_set)   # verified on target (Bug 15)
            if not missing and not extra:
                verdict = "match"
            elif missing and extra:
                verdict = "both"
            elif missing:
                verdict = "missing_on_target"
            else:
                verdict = "extra_on_target"
            counts[verdict] = counts.get(verdict, 0) + 1
            objects.append({
                "perm_object_type": perm_type, "target_id": target_id,
                "object": applied["object_natural_key"], "verdict": verdict,
                "missing_on_target": [list(m) for m in missing],
                "extra_on_target": [list(e) for e in extra],
                "present": [list(p) for p in present],
            })

        report = {
            "run_id": self.config.run_id,
            "source_workspace_id": self.config.source_workspace_id,
            "objects_checked": len(objects),
            "counts": counts,
            "known_limitation": (
                "A role granted BOTH directly and via a group is indistinguishable through the "
                "Databricks API, so only the group grant migrates. Such a case appears below as "
                "`missing_on_target` — it is a platform limitation, not a tool failure."),
            "objects": objects,
        }
        # PLAN 7 §B2 / D-1: parity is NO LONGER a standalone acl_parity_report.{json,html}. It is
        # handed to the import report via the shared cross-phase context and rendered as the
        # "ACL Parity" sheet of import_status.xlsx — one lightweight workbook, still independent
        # proof (this re-reads every touched target object and diffs it against source).
        self.context["acl_parity"] = report
        self.log.info("acl parity report", **counts)
        return report

    # ── normalisation (both sides, identically) ───────────────────────────
    def _normalise_grants(self, grants: list, remap: bool) -> list:
        """Source grants → a sorted list of `(principal, level)`, dropping inherited + admins."""
        out = set()
        for grant in grants or []:
            if not isinstance(grant, dict) or grant.get("inherited"):
                continue
            principal = safe_str(grant.get("principal"))
            ptype = safe_str(grant.get("principal_type"))
            level = safe_str(grant.get("permission_level"))
            if not principal or not level or (ptype == "group" and principal == _BUILTIN_ADMINS):
                continue
            if remap:
                principal = self._remap_principal(principal, ptype) or principal
            out.add((principal, level))
        return sorted(out)

    def _normalise_from_api(self, acl: list) -> list:
        """A target `access_control_list` → the SAME shape, so the diff compares like with like."""
        out = set()
        for entry in acl or []:
            if not isinstance(entry, dict):
                continue
            principal = safe_str(entry.get("user_name") or entry.get("group_name")
                                 or entry.get("service_principal_name"))
            if not principal or principal == _BUILTIN_ADMINS:
                continue
            for perm in entry.get("all_permissions") or []:
                if not isinstance(perm, dict) or perm.get("inherited"):
                    continue          # dropped on BOTH sides — the target recomputes inheritance
                level = safe_str(perm.get("permission_level"))
                if level:
                    out.add((principal, level))
        return sorted(out)
