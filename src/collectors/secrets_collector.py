"""
SecretsCollector — secret scopes + ACLs (SOURCE workspace).

Captures scope NAMES, ACLs, AND `backend_type` (DATABRICKS vs AZURE_KEYVAULT) plus
`keyvault_metadata` (dns_name, resource_id) — required so the import side builds the right
create payload (AKV-backed scopes need `backend_azure_keyvault`; master §10a).

Secret VALUES are NEVER exported by the API → flagged manual (never spin a cluster to read
them). We also record each scope's secret KEY NAMES (metadata only, no values) so the target
report can tell the operator exactly which values to re-populate.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class SecretsCollector(BaseCollector):
    object_type = "secret_scope"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("name"))

    def discover(self) -> list[dict]:
        raw = self.client.get("api/2.0/secrets/scopes/list").get("scopes", []) or []
        items = []
        for s in raw:
            name = safe_str(s.get("name"))
            backend = safe_str(s.get("backend_type")) or "DATABRICKS"
            items.append({
                "name": name,
                "backend_type": backend,                        # DATABRICKS | AZURE_KEYVAULT
                "keyvault_metadata": s.get("keyvault_metadata"),  # dns_name, resource_id (AKV)
                "acls": self._acls(name),
                "key_names": self._key_names(name),             # names only — NO values
                "values_migratable": False,                     # always manual
                "_raw": s,
            })
        return items

    def _acls(self, scope: str) -> list:
        try:
            data = self.client.get("api/2.0/secrets/acls/list", params={"scope": scope})
            return data.get("items", []) if isinstance(data, dict) else []
        except Exception as exc:  # noqa: BLE001
            self.log.warning("secret ACL fetch failed", scope=scope, error=str(exc))
            return []

    def _key_names(self, scope: str) -> list:
        """List secret KEY names in a scope (metadata only; the API never returns values)."""
        try:
            data = self.client.get("api/2.0/secrets/list", params={"scope": scope})
            return [safe_str(k.get("key")) for k in data.get("secrets", [])] if isinstance(data, dict) else []
        except Exception as exc:  # noqa: BLE001
            self.log.warning("secret key list failed", scope=scope, error=str(exc))
            return []
