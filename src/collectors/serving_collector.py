"""
ServingCollector — model serving endpoints (SOURCE workspace). INVENTORY-ONLY / conditional.

Skips platform-managed `databricks-*` endpoints (not user-owned). Also skips **Agent Bricks
agent endpoints** (`task=agent/*`, e.g. Multi-Agent Supervisor `mas-*`): they are NOT
recreatable via workspace REST — a deployed agent is backed by a UC-registered MLflow
ResponsesAgent model plus UC volumes/tables/functions/indexes and UI-only orchestration
metadata, all outside this non-UC workspace utility's scope. Since the import side can't stand
one up on the target, we don't inventory them. natural_key = endpoint name.

DOWNGRADED to migration-manual (decided with customer): a serving endpoint is only a thin
wrapper that POINTS at a model. What it serves is usually a **UC-registered model**
(`entity_name = catalog.schema.model`), and UC is OUT of scope for this utility — the backing
model won't exist on the target, so the endpoint POST would fail. Only endpoints serving
foundation / external models (no UC dependency) are auto-migratable. So we still INVENTORY
every non-agent endpoint but flag `migratable` + a human `migration_note`, rather than
promising a create path.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class ServingCollector(BaseCollector):
    object_type = "serving_endpoint"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("name"))

    def discover(self) -> list[dict]:
        raw = self.client.get("api/2.0/serving-endpoints").get("endpoints", []) or []
        items = []
        for e in raw:
            name = safe_str(e.get("name"))
            if name.startswith("databricks-"):
                continue  # platform-managed, not user-owned
            if str(e.get("task") or "").startswith("agent/"):
                continue  # Agent Bricks agent — not recreatable via workspace REST (see docstring)
            config = e.get("config", {}) or {}
            migratable, note = self._classify(config)
            items.append({
                "name": name,
                "config": config,
                "tags": e.get("tags"),
                "migratable": migratable,
                "migration_note": note,
                "acl": self.fetch_acl("serving-endpoints", e.get("id") or name),
                "_raw": e,
            })
        return items

    @staticmethod
    def _classify(config: dict) -> tuple[bool, str]:
        """Decide if an endpoint is auto-migratable, and why not.

        UC-registered model → NOT migratable (UC out of scope; model absent on target).
        External model (OpenAI/Anthropic/etc.) → migratable (no UC dependency).
        Foundation-model / other → migratable but review (may still reference UC).
        """
        served = (config.get("served_entities")
                  or config.get("served_models") or [])
        # A served entity naming a 3-part `catalog.schema.model` is a UC model reference.
        for s in served:
            entity = safe_str(s.get("entity_name") or s.get("model_name"))
            if entity.count(".") >= 2:
                return (False,
                        "Serves UC-registered model "
                        f"'{entity}' — UC is out of scope; recreate the model on target first.")
        if any(s.get("external_model") for s in served):
            return (True, "External model endpoint — no UC dependency; auto-migratable.")
        if not served:
            return (False, "No served entities resolved — review before migrating.")
        return (True, "Non-UC served model — auto-migratable (verify model artifact on target).")
