"""
IdentityCollector — reads users, groups, service principals from THIS (source) workspace.

Reads WORKSPACE SCIM (not account). Captures, per identity, the fields the target-side
importer + classifier need:
  • users: userName, displayName, emails, active, externalId, entitlements, roles, groups
  • service_principals: applicationId, displayName, active, externalId, entitlements, roles
  • groups: displayName, externalId, entitlements, roles, members (with type + ref), nesting

`externalId` is preserved verbatim — it is the classifier's primary signal (present on
Entra/SCIM-provisioned identities, absent on Databricks-managed/workspace-local ones).
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str

# SCIM $ref substrings → member kind (API-reliable per the migrate review).
_MEMBER_KIND = (("ServicePrincipals/", "service_principal"),
                ("Groups/", "group"),
                ("Users/", "user"))


def _member_kind(member: dict) -> str:
    ref = member.get("$ref", "") or ""
    for needle, kind in _MEMBER_KIND:
        if needle in ref:
            return kind
    # Fallback: infer from the display value when $ref is absent.
    return "user" if "@" in safe_str(member.get("display")) else "unknown"


def _entitlements(obj: dict) -> list:
    return [e.get("value") for e in obj.get("entitlements", []) if e.get("value")]


def _roles(obj: dict) -> list:
    return [r.get("value") for r in obj.get("roles", []) if r.get("value")]


class IdentityCollector(BaseCollector):
    """Collects all three SCIM identity types. `object_type` is 'identity' (multi-type)."""

    object_type = "identity"

    def discover(self) -> list[dict]:
        max_scim = int(getattr(self.config, "max_scim", 0) or 0)
        # (scim_id, error) for every SP whose OAuth-secret check failed — collapsed into ONE
        # warning below, since the usual cause is a single missing privilege, not N problems.
        self._secret_check_failures: list[tuple] = []
        # Read assignments FIRST so every identity below can be stamped with ADMIN vs USER.
        self._assignments = self._permission_assignments()
        out: list[dict] = []
        out.extend(self._users(max_scim))
        out.extend(self._service_principals(max_scim))
        out.extend(self._groups(max_scim))
        self._warn_secret_check_failures()
        return out

    # ── workspace permission assignments (ADMIN vs USER) ──────────────────
    def _permission_assignments(self):
        """`{principal_id: ["ADMIN"|"USER"]}` for every principal assigned to this workspace.

        This is the ONLY place workspace ADMIN-vs-USER is visible — SCIM `entitlements` does not
        carry it (Plan 6 F8), so without this an admin-by-assignment silently migrates as a plain
        USER. `principal_id` equals the workspace SCIM `id` for users, SPs and groups alike (F6),
        so it joins onto the identity rows with no new key.

        Returns `None` (NOT `{}`) if the call fails. `{}` would read as "nobody has permissions"
        and would silently downgrade every admin on target; unknown must stay distinguishable from
        none — the same discipline as `_sp_has_secrets`.
        """
        try:
            data = self.client.get("api/2.0/preview/permissionassignments")
        except Exception as exc:  # noqa: BLE001 — never fail inventory over this
            self.log.warning(
                "could not read workspace permission assignments — ADMIN vs USER will be "
                "reported as 'Could not check' (NOT as 'USER')", error=str(exc)[:200])
            return None
        out: dict = {}
        for entry in (data or {}).get("permission_assignments", []) or []:
            principal = entry.get("principal") or {}
            pid = safe_str(principal.get("principal_id"))
            if pid:
                out[pid] = [p for p in (entry.get("permissions") or []) if p]
        return out

    def _permissions_for(self, scim_id: str):
        """This identity's workspace permissions: `["ADMIN"]`/`["USER"]`, `[]`, or `None`."""
        if self._assignments is None:
            return None                      # could not check — must not read as "no permissions"
        return self._assignments.get(safe_str(scim_id), [])

    def _warn_secret_check_failures(self) -> None:
        if not self._secret_check_failures:
            return
        n = len(self._secret_check_failures)
        _, first_error = self._secret_check_failures[0]
        denied = "PERMISSION_DENIED" in first_error or "403" in first_error
        self.log.warning(
            "could not check OAuth client secrets for service principals — "
            "reported as 'Could not check' (NOT as 'No')",
            service_principals_affected=n,
            likely_cause=("the running identity lacks account_admin; a workspace-admin SERVICE "
                          "PRINCIPAL cannot read another SP's credentials (a USER with "
                          "account_admin can). Grant account_admin to populate this flag."
                          if denied else "see error"),
            example_error=first_error[:200])

    # natural_key differs per identity type; override the base helper.
    def natural_key(self, obj: dict) -> str:
        t = obj.get("identity_type")
        if t == "user":
            return safe_str(obj.get("userName"))
        if t == "service_principal":
            return safe_str(obj.get("applicationId"))
        if t == "group":
            return safe_str(obj.get("displayName"))
        return safe_str(obj.get("id"))

    # ── per-type mappers ──────────────────────────────────────────────────
    def _users(self, max_scim: int) -> list[dict]:
        raw = self.client.get_scim("Users", max_items=max_scim)
        return [self._map_user(u) for u in raw]

    def _service_principals(self, max_scim: int) -> list[dict]:
        raw = self.client.get_scim("ServicePrincipals", max_items=max_scim)
        return [self._map_sp(s) for s in raw]

    def _sp_has_secrets(self, scim_id: str):
        """Whether an SP has OAuth client secrets — `True` / `False` / `None` for "couldn't check".

        `GET /api/2.0/accounts/servicePrincipals/{SCIM_ID}/credentials/secrets` returns secret
        metadata (id/hash/status), never the value. Client secrets CANNOT be migrated → this flags
        the SP for manual secret recreation on target (Plan 1a §6).

        **`None` is not the same as `False`.** Despite the `/accounts/` path this is an
        account-level resource behind a workspace proxy: a USER with account_admin can read it,
        but a plain workspace-admin **service principal** gets `403 PERMISSION_DENIED` (verified
        live 2026-08-06 in `direct` mode). Returning `False` there would be silently wrong — it
        reads as "this SP has no secrets", so an operator would skip recreating a secret that
        really does exist. Unknown must stay distinguishable from no.

        Never aborts the collector: any failure degrades to `None`.
        """
        if not scim_id:
            return False
        try:
            data = self.client.get(
                f"api/2.0/accounts/servicePrincipals/{scim_id}/credentials/secrets")
            return bool(data.get("secrets")) if isinstance(data, dict) else False
        except Exception as exc:  # noqa: BLE001
            # One systemic permission gap would otherwise log once per SP; record it and let the
            # collector emit a single summary warning at the end of the SP pass.
            self._secret_check_failures.append((scim_id, str(exc)))
            return None

    def _groups(self, max_scim: int) -> list[dict]:
        raw = self.client.get_scim("Groups", max_items=max_scim)
        return [self._map_group(g) for g in raw]

    def _map_user(self, u: dict) -> dict:
        emails = u.get("emails", []) or []
        primary = next((e.get("value") for e in emails if e.get("primary")),
                       emails[0].get("value") if emails else "")
        return {
            "identity_type": "user",
            "id": safe_str(u.get("id")),                 # source SCIM id (stripped on import)
            "userName": safe_str(u.get("userName")),
            "displayName": safe_str(u.get("displayName")),
            "email": safe_str(primary),
            "active": u.get("active", True),
            "externalId": safe_str(u.get("externalId")),  # reported only (entra_backed)
            "entitlements": _entitlements(u),
            "roles": _roles(u),
            # ADMIN vs USER — invisible in SCIM entitlements, so it must come from here (F8).
            "workspace_permissions": self._permissions_for(u.get("id")),
            "group_memberships": [g.get("display") for g in u.get("groups", [])],
            "_raw": u,
        }

    def _map_sp(self, s: dict) -> dict:
        scim_id = safe_str(s.get("id"))
        return {
            "identity_type": "service_principal",
            "id": scim_id,
            "applicationId": safe_str(s.get("applicationId")),
            "displayName": safe_str(s.get("displayName")),
            "active": s.get("active", True),
            "externalId": safe_str(s.get("externalId")),  # reported only (entra_backed)
            "entitlements": _entitlements(s),
            "roles": _roles(s),
            "workspace_permissions": self._permissions_for(scim_id),
            "has_secrets": self._sp_has_secrets(scim_id),  # OAuth secrets → manual on target
            "_raw": s,
        }

    def _map_group(self, g: dict) -> dict:
        members = []
        for m in g.get("members", []) or []:
            members.append({
                "value": safe_str(m.get("value")),        # source id (remapped on import)
                "display": safe_str(m.get("display")),    # name/email — the stable key
                "kind": _member_kind(m),
            })
        return {
            "identity_type": "group",
            "id": safe_str(g.get("id")),
            "displayName": safe_str(g.get("displayName")),
            "externalId": safe_str(g.get("externalId")),  # reported only (entra_backed)
            # THE workspace-vs-account signal (Plan 6 F1): "WorkspaceGroup" | "Group". Free — it is
            # already in the LIST response. Getting this wrong shadows an account group and blocks
            # it permanently (F6), so it is captured verbatim and never inferred.
            "resource_type": safe_str((g.get("meta") or {}).get("resourceType")),
            "entitlements": _entitlements(g),
            "roles": _roles(g),
            "workspace_permissions": self._permissions_for(g.get("id")),
            "members": members,
            "member_count": len(members),
            "has_nested_groups": any(m["kind"] == "group" for m in members),
            "_raw": g,
        }
