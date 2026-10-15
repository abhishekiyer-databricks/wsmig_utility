"""
DltImporter — phase 7: Lakeflow Declarative (DLT) pipelines (Plan 3 §6).

A pipeline spec references notebook paths, compute and — critically — Unity Catalog. UC is OUT OF
SCOPE for this utility, so a pipeline whose `catalog`/`target` points at a UC catalog that does not
exist on target WILL fail to create. That failure carries the UC reason EXPLICITLY rather than a raw
API error, because "the UC migration has not run yet" and "our payload is wrong" need very different
responses from the operator, and the raw message ("TABLE_OR_VIEW_NOT_FOUND", "catalog not found")
does not distinguish them.

**Run-as (PLAN 16.3 §5, CUST-1).** A UC pipeline runs as its OWNER unless the spec sets `run_as`, and
the tool never changes an owner (that needs a metastore admin), so a migrated pipeline is owned by the
migration identity. When the source pipeline ran as a SERVICE PRINCIPAL, the spec's `run_as` keeps it
running as that SP (remapped through `sp_mapping`); a user can't be set as run-as (the API 400s), so a
user-owned pipeline runs as the migration identity. A refused `run_as` degrades to a warning, never a
failure: the pipeline is created without it.
"""
from __future__ import annotations

from typing import Optional

from src.importers.base_importer import BaseImporter
from src.utils.helpers import looks_like_app_id, pipeline_not_creatable, safe_str

# The create/update was refused BECAUSE of `run_as`: the runner lacks the Service Principal User
# role on that SP. Verified live 2026-10-14 (create AND update, workspace-admin runner holding only
# servicePrincipal.manager): `403 PERMISSION_DENIED Cannot bind the service principal provided in
# 'run_as' field (<sp>) to the pipeline. The user creating or updating the pipeline must have
# 'servicePrincipal.user' role on the service principal.` ALL markers must match, so any other 403
# (e.g. the run-as SP lacks USE CATALOG — also PERMISSION_DENIED, verified the same day) propagates.
_RUN_AS_REFUSED_MARKERS = ("permission_denied", "'run_as'", "servicePrincipal.user")


class DltImporter(BaseImporter):
    component = "dlt"
    asset_types = ("dlt_pipeline",)
    # §5: an adopted pipeline's run-as is reconciled (`adopt_one`) — a plain ADOPT only records it.
    reconcile_on_adopt_types = ("dlt_pipeline",)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # `{target pipeline_id: run_as_user_name}` from the target LIST (`existing_keys`, V7) — what
        # the run-as recheck compares against, at no extra call.
        self._target_run_as: dict = {}
        self._me: Optional[str] = None

    def load(self) -> list[dict]:
        """Pipelines — a 1.x bundle's `create` for a pipeline with no source code (a synced table's)
        becomes `manual` here, with no API call (PLAN 16.3 §6; 1.2.0 export already marks it)."""
        out: list[dict] = []
        for unit in self.units_for("dlt_pipeline"):
            if safe_str(unit.get("import_action")) == "create":
                note = pipeline_not_creatable(unit.get("payload"))
                if note:
                    unit = {**unit, "import_action": "manual", "note": note}
            out.append(unit)
        return out

    def existing_keys(self) -> dict:
        """`{pipeline_name: pipeline_id}` — PAGINATED (pipelines is a cursor API)."""
        pipelines = self.client.get_paginated("api/2.0/pipelines", "statuses",
                                              params={"max_results": 100})
        found = {safe_str(p.get("name")): safe_str(p.get("pipeline_id"))
                 for p in pipelines if p.get("name")}
        self._target_run_as.update({safe_str(p.get("pipeline_id")): safe_str(p.get("run_as_user_name"))
                                    for p in pipelines if p.get("pipeline_id")})
        self.context.setdefault("dlt_pipeline_target_ids", {}).update(found)
        return found

    def create_one(self, unit: dict) -> dict:
        body, warning = self._spec(unit)
        created, warning = self._send(lambda b: self.client.post("api/2.0/pipelines", b),
                                      body, warning)
        pid = safe_str(created.get("pipeline_id"))
        self.context.setdefault("dlt_pipeline_target_ids", {})[self.natural_key(unit)] = pid
        return {"target_id": pid, "warning": warning,
                "note": "created in DEVELOPMENT-safe form: not started by import"}

    def update_one(self, unit: dict, target_id: str) -> dict:
        """`PUT pipelines/{id}` replaces the spec; it requires the id INSIDE the body too."""
        body, warning = self._spec(unit)
        body["id"] = target_id
        _out, warning = self._send(
            lambda b: self.client.put(f"api/2.0/pipelines/{target_id}", b), body, warning)
        if isinstance(body.get("run_as"), dict):
            self._target_run_as[target_id] = safe_str(body["run_as"].get("service_principal_name"))
        return {"target_id": target_id, "warning": warning}

    def _send(self, call, body: dict, warning: str) -> tuple:
        """Send the create/update; if it is refused BECAUSE of `run_as`, retry ONCE without it
        (§5) — the pipeline then runs as the migration identity, reported as a warning. Any other
        error propagates unchanged. Returns `(response, warning)`."""
        try:
            return (call(body) or {}), warning
        except Exception as exc:  # noqa: BLE001 — only a run_as refusal is handled here
            run_as = body.get("run_as")
            text = str(exc).lower()
            if not (isinstance(run_as, dict)
                    and all(m.lower() in text for m in _RUN_AS_REFUSED_MARKERS)):
                raise
            sp = safe_str(run_as.get("service_principal_name")) or safe_str(run_as.get("user_name"))
            self.log.debug(f"pipeline `{safe_str(body.get('name'))}`: run_as {sp} refused "
                           f"({str(exc)[:200]}) — retrying without it")
            body.pop("run_as", None)
            resp = call(body) or {}
            note = (f"runs as the migration identity; source ran as SP `{sp}` — grant the runner "
                    f"the Service Principal User role on `{sp}`, then re-run.")
            return resp, " ".join(x for x in (warning, note) if x)

    # ── run-as reconcile (§5) ─────────────────────────────────────────────
    def _desired_run_as(self, unit: dict) -> str:
        """The SP application id this pipeline should run as on target, or "" (user-owned / none).
        An explicit spec `run_as` SP wins; otherwise the source's run-as when it is an SP."""
        sp_map = self.identity_map.get("sp_mapping") or {}
        explicit = (unit.get("payload") or {}).get("run_as")
        if isinstance(explicit, dict):
            app_id = safe_str(explicit.get("service_principal_name"))
            return sp_map.get(app_id, app_id) if app_id else ""
        src = safe_str(unit.get("source_run_as"))
        return sp_map.get(src, src) if looks_like_app_id(src) else ""

    def _run_as_differs(self, unit: dict, target_id: str) -> bool:
        """Whether the target pipeline (per the LIST, V7) runs as someone other than the desired
        SP. Unknown target run-as is not "different": nothing is guessed."""
        desired = self._desired_run_as(unit)
        current = safe_str(self._target_run_as.get(target_id))
        return bool(desired and current and current.lower() != desired.lower())

    def recheck_unchanged(self, unit: dict, row) -> bool:
        """An unchanged pipeline whose target run-as is not its source SP is revisited — a 1.x
        target runs every pipeline as the migration SP. User-owned pipelines never recheck."""
        key = self.natural_key(unit)
        tid = (safe_str((self.context.get("dlt_pipeline_target_ids") or {}).get(key))
               or safe_str((row or {}).get("target_object_id")))
        return bool(tid) and self._run_as_differs(unit, tid)

    def adopt_one(self, unit: dict, target_id: str) -> dict:
        """ADOPT of a pipeline: a moved fingerprint is a full update (1.1.1's stale-update rule); a
        run-as that differs is a full spec PUT, now with `run_as`; otherwise plain adopted."""
        row = (self.state.row("dlt_pipeline", self.natural_key(unit))
               if self.state is not None else None)
        stored_fp = safe_str((row or {}).get("last_source_fingerprint"))
        if stored_fp not in ("", safe_str(unit.get("fingerprint"))) \
                or self._run_as_differs(unit, target_id):
            return {**self.update_one(unit, target_id), "changed": True}
        return {"changed": False, "target_id": target_id}

    def _migration_identity(self) -> str:
        """The runner's own userName (SCIM Me), read once — "" when unreadable."""
        if self._me is None:
            try:
                self._me = safe_str((self.client.get("api/2.0/preview/scim/v2/Me") or {})
                                    .get("userName"))
            except Exception as exc:  # noqa: BLE001 — degraded: a user run_as is then dropped
                self.log.warning(f"could not read the migration identity (SCIM Me) — a source "
                                 f"run_as USER is dropped: {str(exc)[:160]}")
                self._me = ""
        return self._me

    # ── spec assembly ─────────────────────────────────────────────────────
    def _spec(self, unit: dict) -> tuple[dict, str]:
        spec = dict(unit.get("payload") or {})
        warnings: list[str] = []
        spec["name"] = safe_str(spec.get("name")) or self.natural_key(unit)

        self._remap_libraries(spec, warnings)
        self._remap_clusters(spec, warnings)
        self._remap_run_as(spec, warnings)
        self._apply_run_as_rule(spec, unit, warnings)
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

    def _apply_run_as_rule(self, spec: dict, unit: dict, warnings: list) -> None:
        """§5: a source that RAN AS an SP (no explicit spec run_as) keeps that SP via spec
        `run_as`; an explicit run_as naming a USER other than the migration identity is dropped,
        because the target refuses it (V6) and the pipeline runs as the migration identity."""
        run_as = spec.get("run_as")
        if isinstance(run_as, dict):
            user = safe_str(run_as.get("user_name"))
            if user and user.lower() != self._migration_identity().lower():
                spec.pop("run_as", None)
                warnings.append(
                    f"source run_as user `{user}` was dropped — a pipeline can only run as the "
                    f"caller, a service principal or a group, so it runs as the migration identity.")
            return
        src = safe_str(unit.get("source_run_as"))
        if looks_like_app_id(src):
            spec["run_as"] = {"service_principal_name": self._desired_run_as(unit)}

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
        except Exception as exc:  # noqa: BLE001 — expected: absent 404s, which is the answer
            self.log.debug(f"get-status {path} → absent ({str(exc)[:160]})")
            return False
