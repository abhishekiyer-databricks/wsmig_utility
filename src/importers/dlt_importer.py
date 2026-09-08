"""
DltImporter — phase 7: Lakeflow Declarative (DLT) pipelines (Plan 3 §6).

A pipeline spec references notebook paths, compute and — critically — Unity Catalog. UC is OUT OF
SCOPE for this utility, so a pipeline whose `catalog`/`target` points at a UC catalog that does not
exist on target WILL fail to create. That failure carries the UC reason EXPLICITLY rather than a raw
API error, because "the UC migration has not run yet" and "our payload is wrong" need very different
responses from the operator, and the raw message ("TABLE_OR_VIEW_NOT_FOUND", "catalog not found")
does not distinguish them.
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter
from src.utils.helpers import safe_str


class DltImporter(BaseImporter):
    component = "dlt"
    asset_types = ("dlt_pipeline",)

    def load(self) -> list[dict]:
        return self.units_for("dlt_pipeline")

    def existing_keys(self) -> dict:
        """`{pipeline_name: pipeline_id}` — PAGINATED (pipelines is a cursor API)."""
        pipelines = self.client.get_paginated("api/2.0/pipelines", "statuses",
                                              params={"max_results": 100})
        found = {safe_str(p.get("name")): safe_str(p.get("pipeline_id"))
                 for p in pipelines if p.get("name")}
        self.context.setdefault("dlt_pipeline_target_ids", {}).update(found)
        return found

    def create_one(self, unit: dict) -> dict:
        body, warning = self._spec(unit)
        created = self.client.post("api/2.0/pipelines", body)
        pid = safe_str(created.get("pipeline_id"))
        self.context.setdefault("dlt_pipeline_target_ids", {})[self.natural_key(unit)] = pid
        return {"target_id": pid, "warning": warning,
                "note": "created in DEVELOPMENT-safe form: not started by import"}

    def update_one(self, unit: dict, target_id: str) -> dict:
        """`PUT pipelines/{id}` replaces the spec; it requires the id INSIDE the body too."""
        body, warning = self._spec(unit)
        body["id"] = target_id
        self.client.put(f"api/2.0/pipelines/{target_id}", body)
        return {"target_id": target_id, "warning": warning}

    # ── spec assembly ─────────────────────────────────────────────────────
    def _spec(self, unit: dict) -> tuple[dict, str]:
        spec = dict(unit.get("payload") or {})
        warnings: list[str] = []
        spec["name"] = safe_str(spec.get("name")) or self.natural_key(unit)

        self._remap_libraries(spec, warnings)
        self._remap_clusters(spec, warnings)
        self._remap_run_as(spec, warnings)
        self._warn_on_uc(spec, warnings)

        # Server-derived fields that a create rejects (the pipeline id is nested as `id` in a spec).
        for field in ("id", "pipeline_id", "state", "cluster_id", "creator_user_name",
                      "run_as_user_name", "last_modified", "latest_updates", "pipeline_type",
                      "health"):
            spec.pop(field, None)

        warning = " ".join(warnings)
        if warning:
            self.result.warnings.append(f"dlt_pipeline/{self.natural_key(unit)}: {warning}")
        return spec, warning

    def _remap_libraries(self, spec: dict, warnings: list) -> None:
        """A pipeline's source notebooks/files must exist on target or its first update fails."""
        known = self.context.get("workspace_paths") or set()
        for lib in spec.get("libraries") or []:
            if not isinstance(lib, dict):
                continue
            for kind, key in (("notebook", "path"), ("file", "path")):
                ref = lib.get(kind)
                if not isinstance(ref, dict):
                    continue
                path = safe_str(ref.get(key))
                if not path or path in known:
                    continue
                if not self._exists_on_target(path):
                    warnings.append(
                        f"pipeline source `{path}` does not exist on target — the pipeline was "
                        f"created but its first update will FAIL. This is usually a notebook inside "
                        f"a Git folder (out of scope for import — recreate it by hand), or the "
                        f"workspace family has not been imported yet.")

    def _remap_clusters(self, spec: dict, warnings: list) -> None:
        """Remap policy/pool ids in each `clusters` entry (exact-or-fail-loud); drop node types when
        pooled.

        PLAN 11 Finding-10: the old "drop the reference to let the pipeline be created" silently
        changed the pipeline's compute; now an unresolved pool/policy is a retryable prerequisite
        (in bundle, not yet on target) or a hard failure (not in bundle), never a silent drop.
        """
        for cluster in spec.get("clusters") or []:
            if not isinstance(cluster, dict):
                continue
            for field, ref_type in (("policy_id", "cluster_policy"),
                                    ("instance_pool_id", "instance_pool"),
                                    ("driver_instance_pool_id", "instance_pool")):
                src = safe_str(cluster.get(field))
                if src:
                    cluster[field] = self.require_remap(
                        ref_type, src, referenced_by=f"pipeline `{safe_str(spec.get('name'))}` cluster")
            if cluster.get("instance_pool_id"):
                for field in ("node_type_id", "driver_node_type_id", "enable_elastic_disk"):
                    cluster.pop(field, None)

    def _remap_run_as(self, spec: dict, warnings: list) -> None:
        run_as = spec.get("run_as")
        if not isinstance(run_as, dict):
            return
        app_id = safe_str(run_as.get("service_principal_name"))
        if not app_id:
            return
        mapped = (self.identity_map.get("sp_mapping") or {}).get(app_id, "")
        if mapped and mapped != app_id:
            run_as["service_principal_name"] = mapped
        elif not mapped:
            warnings.append(
                f"run_as service principal {app_id!r} is not in the identity map, so it was left "
                f"as-is — a workspace-local SP has a different applicationId on target.")

    def _warn_on_uc(self, spec: dict, warnings: list) -> None:
        """Say the UC dependency out loud BEFORE the API's opaque error does.

        UC is out of scope for this utility, so a pipeline writing to a UC catalog cannot work until
        the UC migration has created it. Naming that up front is the difference between a 5-second
        diagnosis and an afternoon.
        """
        catalog = safe_str(spec.get("catalog"))
        schema = safe_str(spec.get("schema") or spec.get("target"))
        if catalog:
            warnings.append(
                f"this pipeline writes to Unity Catalog (`{catalog}"
                + (f".{schema}" if schema else "") +
                "`), which is OUT OF SCOPE for this utility. If that catalog/schema does not already "
                "exist on the target, the create will fail or the first update will — it must be "
                "created by the UC migration, not by this tool.")

    def _exists_on_target(self, path: str) -> bool:
        try:
            return bool(self.client.get("api/2.0/workspace/get-status", params={"path": path}))
        except Exception:  # noqa: BLE001 — absent 404s, which is the answer
            return False
