"""
SecretsImporter — phase 4: secret scopes (+ their MANAGE principal) (Plan 3 §6, §6c, D4).

**Secret VALUES never migrate, and that is physical rather than a limitation of this tool.** No
Databricks API returns a secret value — that is the entire point of a secret store. So every key gets
a `manual` unit telling the operator which value to re-populate, on EVERY run rather than only the
first (the scope's key-name list IS fingerprinted, so an added key is detected).

Two implementation traps, both verified live (memory `fvm1-test-fixtures-and-akv-state`):

1. **An Azure Key Vault-backed scope needs an AZURE AD token, not a Databricks token.** Only for the
   linking call (`POST secrets/scopes/create` with `scope_backend_type=AZURE_KEYVAULT`): Databricks
   must prove to *Azure* that the caller may read that vault, and a Databricks OAuth/context token
   carries no AAD identity — the call fails with `"must have userAADToken defined!"`. If the run-as
   identity is an Entra SP / managed identity it can mint that token itself, headlessly. Note this is
   SEPARATE from vault permissions: the AAD token is *who is asking*, the vault's access policy is
   *whether they're allowed* — and the failure note distinguishes them, because the customer fixes
   the two differently. On any failure that scope fails, is reported with its remediation, and the
   run CONTINUES (D4).

2. **`initial_manage_principal` must be set AT CREATE.** `users:MANAGE` cannot be patched afterwards —
   getting it wrong means deleting and recreating the scope, so it is resolved through the identity
   map BEFORE the create rather than fixed up later.

**Cross-region reality check, stated rather than buried:** carrying the vault over verbatim means a
region-2 workspace reads a region-1 vault. That works if the target identity is granted access and
the network path allows it, and it is the customer's stated preference — but it leaves a cross-region
dependency, so the note says so plainly (preflight WARNs on it too).
"""
from __future__ import annotations

from src.exporters import bundle_paths as BP
from src.importers.base_importer import BaseImporter, PrerequisiteMissing
from src.utils.helpers import safe_str

_AKV = "AZURE_KEYVAULT"


class SecretsImporter(BaseImporter):
    component = "secrets"
    asset_types = ("secret_scope", "secret_value")

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._manage_cache = None      # lazily read from export/acls.json

    def load(self) -> list[dict]:
        """Scopes first, then the per-key `secret_value` units (all `manual`, never attempted)."""
        return self.units_for("secret_scope", "secret_value")

    def existing_keys(self) -> dict:
        """`{scope_name: scope_name}` — a scope's NAME is its identifier.

        `secrets/scopes/list` exposes no pagination. A truncated list here would attempt to recreate
        an existing scope, which fails as RESOURCE_ALREADY_EXISTS and is adopted — so the downside is
        contained rather than becoming a duplicate.
        """
        doc = self.client.get("api/2.0/secrets/scopes/list") or {}
        found = {safe_str(s.get("name")): safe_str(s.get("name"))
                 for s in (doc.get("scopes") or []) if s.get("name")}
        self.context.setdefault("secret_scope_target_ids", {}).update(found)
        return found

    def create_one(self, unit: dict) -> dict:
        if safe_str(unit.get("asset_type")) != "secret_scope":
            # `secret_value` units are `manual` and never reach here — this is a guard, not a path.
            raise PrerequisiteMissing(
                "secret VALUES are never readable through any API, so they cannot be imported — "
                "re-populate this key by hand on the target (≤128 KB per value)")

        payload = unit.get("payload") or {}
        name = self.natural_key(unit)
        backend = safe_str(payload.get("backend_type")).upper() or "DATABRICKS"

        body: dict = {"scope": name}
        manage_principal, deferred_manage = self._manage_principal(name)
        # `users` is the only value the API accepts here (see _manage_principal); any NAMED
        # principal is granted MANAGE straight after create instead.
        body["initial_manage_principal"] = manage_principal

        if backend == _AKV:
            return self._create_akv_scope(unit, body, payload, deferred_manage)

        self.client.post("api/2.0/secrets/scopes/create", body)
        granted = self._grant_manage(name, deferred_manage)
        keys = payload.get("key_names") or []
        return {"target_id": name,
                "note": (f"Databricks-backed scope created (MANAGE={manage_principal}"
                         f"{', ' + ', '.join(granted) if granted else ''}). Its "
                         f"{len(keys)} secret VALUE(s) are NOT migratable — no API returns a value — "
                         f"so re-populate them on target; each key has its own manual row.")}

    def _grant_manage(self, scope: str, principals: list) -> list:
        """Grant MANAGE to named principals after the scope exists.

        `initial_manage_principal` only accepts `users`, so a source scope managed by a specific
        user/SP/group has to be reproduced through `secrets/acls/put`. Best-effort per principal: a
        scope that exists but is missing one MANAGE grant is far better than no scope at all, and the
        ACL phase (12) re-applies scope ACLs anyway — this is belt-and-braces for the MANAGE case.
        """
        granted = []
        for principal in principals:
            try:
                self.client.post("api/2.0/secrets/acls/put",
                                 {"scope": scope, "principal": principal, "permission": "MANAGE"})
                granted.append(principal)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("scope MANAGE grant failed", scope=scope, principal=principal,
                                 error=str(exc)[:200])
        return granted

    def _create_akv_scope(self, unit: dict, body: dict, payload: dict,
                          deferred_manage: list = ()) -> dict:
        """An Azure Key Vault-backed scope is ALWAYS a manual step here — never attempted (IMP-4).

        This is a hard Azure identity-model fact, proven live 2026-08-08, not a tool gap. Creating
        an AKV-backed scope needs an AZURE AD token for app 2ff814a6… (`"must have userAADToken
        defined!"`), but:
          • the only credential the customer can supply is a DATABRICKS SPN secret, which mints a
            DATABRICKS token (wrong issuer — `<workspace>/oidc`, not `login.microsoftonline.com`); and
          • that SPN is backed by a User-Assigned Managed Identity, which refuses secret-based Azure
            AD auth (`AADSTS7000232`) and can obtain a token only via Azure IMDS — reachable only
            from Azure compute the MI is attached to, which a front-end-private / VDI / notebook-only
            workspace does not provide.
        There is therefore no code path to the required token, so the scope is reported as a clean
        MANUAL step with the exact vault to recreate against. Databricks-backed scopes are unaffected.
        """
        name = self.natural_key(unit)
        meta = payload.get("keyvault_metadata") or {}
        dns_name = safe_str(meta.get("dns_name")) or "the source vault"
        raise PrerequisiteMissing(
            f"scope `{name}` is Azure Key Vault-backed — CREATE IT BY HAND on the target against "
            f"vault {dns_name}. This cannot be automated in this environment: the API needs an "
            f"Azure AD token for app 2ff814a6-3304-4ab8-85cb-cd0e6f879c1d (\"must have userAADToken "
            f"defined!\"), but a Databricks SPN credential only yields a Databricks token, and a "
            f"managed-identity-backed SPN can mint an Azure AD token only via Azure IMDS — which is "
            f"unreachable from a front-end-private / notebook-only workspace. Recreate the scope in "
            f"the UI/CLI (Create Scope → Azure Key Vault), then re-run with retry_mode=failed_only; "
            f"the tool will adopt it. (Databricks-backed scopes migrate automatically.)")

    def _source_manage_grants(self) -> dict:
        """`{scope_name: [principal, ...]}` holding MANAGE on source, read from `export/acls.json`.

        Read from the bundle rather than taken from cross-phase context, because the ACL phase runs
        LAST (phase 12) and this is needed at scope-create time in phase 4 — `users:MANAGE` cannot be
        patched afterwards, so waiting for the ACL phase would be too late by design.
        """
        if self._manage_cache is not None:
            return self._manage_cache
        cache: dict = {}
        for entry in (self.staging.read_json(BP.EXPORT_ACLS_JSON) or []):
            if not isinstance(entry, dict) or safe_str(entry.get("asset_type")) != "secret_scope":
                continue
            scope = safe_str(entry.get("natural_key"))
            for grant in entry.get("grants") or []:
                if safe_str((grant or {}).get("permission_level")).upper() == "MANAGE":
                    cache.setdefault(scope, []).append(safe_str(grant.get("principal")))
        self._manage_cache = cache
        return cache

    def _manage_principal(self, scope: str) -> tuple[str, list]:
        """`(initial_manage_principal, deferred_manage_principals)` for a new scope.

        **`users` is the ONLY value this API accepts.** Verified live 2026-08-06: passing any
        specific principal — including the CALLER's own username — fails with
        `400 BAD_REQUEST Cannot specify <principal> as initial_manage_principal`. So a source scope
        whose MANAGE holder is a named user/SP/group cannot express that at create time; sending it
        fails the whole scope (which is what happened: 3 of 4 scopes failed on the first live run).

        The named principals are therefore returned separately, to be granted via
        `secrets/acls/put` straight after the scope exists. That ordering is safe: unlike
        `users:MANAGE` — which genuinely cannot be patched later — an explicit MANAGE acl CAN be
        put afterwards, so nothing is lost by deferring it.
        """
        deferred = []
        for principal in self._source_manage_grants().get(scope, []):
            if principal == "users":
                continue          # already covered by the initial_manage_principal below
            mapped = self.resolve_principal(principal)
            if mapped:
                deferred.append(mapped)
        # `users` matches the API's own default, and is the only accepted literal.
        return "users", deferred

    def update_one(self, unit: dict, target_id: str) -> dict:
        """A secret scope has NO edit API, and recreating it would destroy its values."""
        return {"target_id": target_id or self.natural_key(unit),
                "note": ("the scope changed on source (a key added/removed, or the vault "
                         "re-pointed) but the Secrets API has no scope EDIT call. The scope was left "
                         "as it is, because recreating it would DELETE every value stored in it. "
                         "Reconcile by hand, or deliberately delete and re-import the scope."),
                "warning": ("scope definition changed on source but cannot be updated in place (no "
                            "edit API exists) — reconcile manually")}
