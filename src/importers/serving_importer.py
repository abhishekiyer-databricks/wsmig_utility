"""
ServingImporter — phase 10: model serving endpoints, EXTERNAL-MODEL ones only (Plan 3 §6).

An endpoint only ever *points at* a model, so what it points at decides whether it can be migrated:

  • **external-model endpoints** (OpenAI, Anthropic, Azure OpenAI, …) are self-contained apart from
    their API key, so they are auto-migratable — the key lives in a secret scope, which migrates as a
    scope with its value re-populated by hand.
  • **UC-registered-model endpoints CANNOT be recreated.** The model lives in Unity Catalog, which is
    out of scope for this utility, so the endpoint has nothing to serve on target. Export already
    marks these `manual` with the reason, and the base class records them without attempting — the
    right behaviour, because attempting would fail on every run forever.
  • **`databricks-*` endpoints are platform-managed** (foundation-model APIs). They exist on the
    target already by construction and must never be created.
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter, UnsupportedOperation
from src.utils.helpers import safe_str

# Platform-managed endpoint prefix — these exist by construction, never migrated.
_MANAGED_PREFIX = "databricks-"


class ServingImporter(BaseImporter):
    component = "serving"
    asset_types = ("serving_endpoint",)

    def load(self) -> list[dict]:
        """Skip platform-managed endpoints entirely — they aren't the customer's to migrate."""
        return [u for u in self.units_for("serving_endpoint")
                if not safe_str(u.get("natural_key")).startswith(_MANAGED_PREFIX)]

    def existing_keys(self) -> dict:
        """`{name: name}` — a serving endpoint's NAME is its identifier in the API path."""
        doc = self.client.get("api/2.0/serving-endpoints") or {}
        found = {safe_str(e.get("name")): safe_str(e.get("name"))
                 for e in (doc.get("endpoints") or []) if e.get("name")}
        self.context.setdefault("serving_endpoint_target_ids", {}).update(found)
        return found

    def create_one(self, unit: dict) -> dict:
        name = self.natural_key(unit)
        config = dict(unit.get("payload") or {})
        self._reject_uc_backed(name, config)
        self._require_provider_credentials(name, config)
        body = {"name": name, "config": self._config_body(config)}
        created = self.client.post("api/2.0/serving-endpoints", body)
        return {"target_id": safe_str(created.get("name")) or name,
                "note": ("external-model endpoint created. Its provider API key is NOT migratable — "
                         "re-populate the referenced secret scope value on target, or the endpoint "
                         "will fail on first request.")}

    def update_one(self, unit: dict, target_id: str) -> dict:
        """`PUT serving-endpoints/{name}/config` — the config, not the endpoint, is what changes."""
        name = target_id or self.natural_key(unit)
        config = dict(unit.get("payload") or {})
        self._reject_uc_backed(name, config)
        self.client.put(f"api/2.0/serving-endpoints/{name}/config", self._config_body(config))
        return {"target_id": name}

    def _reject_uc_backed(self, name: str, config: dict) -> None:
        """A UC-model-backed endpoint has nothing to serve on target — say so, don't just fail.

        Export normally marks these `manual` so they never reach here; this is the safety net for an
        endpoint whose UC backing wasn't detectable at export time.
        """
        for entity in (config.get("served_entities") or config.get("served_models") or []):
            if not isinstance(entity, dict):
                continue
            model = safe_str(entity.get("entity_name") or entity.get("model_name"))
            is_external = bool(entity.get("external_model"))
            if model and not is_external and model.count(".") >= 2:
                # A three-part name is a UC model (catalog.schema.model).
                raise UnsupportedOperation(
                    f"serving endpoint `{name}` serves the Unity Catalog model `{model}`. UC is OUT "
                    f"OF SCOPE for this utility, so that model does not exist on the target and the "
                    f"endpoint cannot be recreated here. Migrate the model with the UC tooling, then "
                    f"recreate this endpoint (only external-model endpoints are auto-migratable).")

    def _require_provider_credentials(self, name: str, config: dict) -> None:
        """An external-model endpoint cannot be created without its provider CREDENTIAL block.

        Verified live: the export payload carries `external_model{provider, name, task}` but NOT the
        credential (`openai_api_key` / `anthropic_api_key` / …), because that field is **write-only —
        the API never returns it**, exactly like a secret value. Posting the config without it fails
        with the unhelpful "Empty or wrong type of config provided for openai".

        So this is a MANUAL step by nature, not a tool defect: the endpoint definition migrated, but a
        human must supply the API key (usually as a secret-scope reference). Reported with that
        remediation rather than as a raw API rejection, so the operator knows it is a credential, not
        a malformed payload.
        """
        missing = []
        for entity in (config.get("served_entities") or config.get("served_models") or []):
            if not isinstance(entity, dict):
                continue
            external = entity.get("external_model")
            if not isinstance(external, dict):
                continue
            provider = safe_str(external.get("provider"))
            # Any `*_config` block is the credential holder (openai_config, anthropic_config, …).
            has_credentials = any(k.endswith("_config") and external.get(k) for k in external)
            if not has_credentials:
                missing.append(f"{safe_str(entity.get('name')) or provider} ({provider})")
        if missing:
            raise UnsupportedOperation(
                f"serving endpoint `{name}` serves external model(s) {', '.join(missing)}, but the "
                f"provider API KEY is write-only — no Databricks API ever returns it, so it is not in "
                f"the bundle (the same reason secret values are not). The endpoint definition "
                f"migrated, but it cannot be created without the credential. Recreate it on target "
                f"supplying the provider key (ideally as a secret-scope reference), then re-run with "
                f"retry_mode=failed_only for anything that depends on it.")

    @staticmethod
    def _config_body(config: dict) -> dict:
        """The create config, minus the server-derived fields a create rejects."""
        body = dict(config)
        for field in ("config_version", "state", "creation_timestamp", "last_updated_timestamp",
                      "creator", "id", "endpoint_url"):
            body.pop(field, None)
        # PLAN 8 Bug 11: the API rejects a config carrying BOTH `served_models` (deprecated) and
        # `served_entities` ("Both served_models and served_entities cannot be provided"). Send ONLY
        # served_entities: when both are present, drop the deprecated served_models; when only
        # served_models exists, promote it (renaming model_name/model_version → entity_*).
        if body.get("served_entities"):
            body.pop("served_models", None)
        elif body.get("served_models"):
            body["served_entities"] = [ServingImporter._model_to_entity(m)
                                       for m in body.pop("served_models")]
        return body

    @staticmethod
    def _model_to_entity(model) -> dict:
        """Map a deprecated `served_models` entry to the current `served_entities` shape."""
        if not isinstance(model, dict):
            return model
        entity = dict(model)
        if "model_name" in entity:
            entity["entity_name"] = entity.pop("model_name")
        if "model_version" in entity:
            entity["entity_version"] = entity.pop("model_version")
        return entity
