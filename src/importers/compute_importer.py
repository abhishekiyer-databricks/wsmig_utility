"""
ComputeImporter — phase 2: instance pools → cluster policies → clusters (Plan 3 §6).

The order is fixed because a cluster can reference BOTH a pool and a policy, so both must exist and
be mapped (source id → natural key → target id) before any cluster is created.

Same cloud, same region, so node types carry over verbatim — none of the reference tool's GCP node
mapping applies. What DOES apply, and is easy to get wrong:

  • **Ephemeral clusters must be excluded.** `job-*`, `dlt-execution-*`, `mlflow-model-*` are created
    by the platform for a single run and die with it. Recreating them litters the target with dead
    compute the real job/pipeline never uses.
  • **Stop the cluster right after create.** `clusters/create` STARTS the cluster. Migrating 30
    clusters would otherwise silently start 30 clusters and burn the customer's DBUs for nothing.
  • **Re-pin pinned clusters.** Pinning keeps a terminated cluster visible in the UI; a
    migrated-but-unpinned cluster effectively disappears once it is cleaned up.
  • **When a pool is set, strip the node types.** `node_type_id` / `driver_node_type_id` /
    `enable_elastic_disk` are rejected alongside `instance_pool_id` — the pool dictates them.
  • **The creator cannot be preserved** (the API attributes the cluster to the caller), so the source
    creator is recorded in an `OriginalCreator` tag rather than lost.
"""
from __future__ import annotations

import json

from src.importers.base_importer import BaseImporter
from src.utils.helpers import safe_str

# Clusters the platform creates for a run and destroys with it — never migrated.
_EPHEMERAL_PREFIXES = ("job-", "dlt-execution-", "mlflow-model-")

# Cluster-policy definition keys that pin an instance-pool id (a SOURCE id must be remapped, Bug 9).
_POLICY_POOL_KEYS = ("instance_pool_id", "driver_instance_pool_id")

# Fields the pool dictates; sending them alongside instance_pool_id is rejected.
_POOL_CONFLICTS = ("node_type_id", "driver_node_type_id", "enable_elastic_disk")


def is_ephemeral_cluster(name: str) -> bool:
    """Whether a cluster name is a platform-generated ephemeral one."""
    n = safe_str(name)
    return any(n.startswith(p) for p in _EPHEMERAL_PREFIXES)


class ComputeImporter(BaseImporter):
    component = "compute"
    asset_types = ("instance_pool", "cluster_policy", "cluster")

    def load(self) -> list[dict]:
        """Pools → policies → clusters, with ephemeral clusters dropped.

        Ephemeral units are dropped SILENTLY rather than recorded as work: they were never migratable
        assets, and reporting dozens of `job-...` clusters as skipped rows would bury the real ones.
        """
        units = self.units_for("instance_pool", "cluster_policy", "cluster")
        return [u for u in units
                if not (safe_str(u.get("asset_type")) == "cluster"
                        and is_ephemeral_cluster(u.get("natural_key")))]

    # ── existence (by NAME — ids differ across workspaces) ────────────────
    def existing_keys(self) -> dict:
        """`{name: target_id}` for pools, policies and clusters already on target.

        These three list APIs return complete lists today, which master §6b flags as an UNTESTED
        assumption — so each response is checked for a continuation token and warns rather than
        silently truncating, because a truncated existence check here means a DUPLICATE create.
        """
        out: dict = {}
        for path, key_field, id_field, result_key, at in (
                ("api/2.0/instance-pools/list", "instance_pool_name", "instance_pool_id",
                 "instance_pools", "instance_pool"),
                ("api/2.0/policies/clusters/list", "name", "policy_id", "policies",
                 "cluster_policy"),
                ("api/2.0/clusters/list", "cluster_name", "cluster_id", "clusters", "cluster")):
            doc = self.client.get(path) or {}
            if isinstance(doc, dict) and doc.get("next_page_token"):
                self.result.warnings.append(
                    f"{path} returned a next_page_token, so this existence check may be INCOMPLETE "
                    f"— which risks duplicate creates. The tool needs pagination added for it.")
            found: dict = {}
            for item in (doc.get(result_key) or []):
                name = safe_str(item.get(key_field))
                if not name or (at == "cluster" and is_ephemeral_cluster(name)):
                    continue
                found[name] = safe_str(item.get(id_field))
            out.update(found)
            # Published per asset_type so later phases (jobs, DLT, libraries) can remap onto them.
            self.context.setdefault(f"{at}_target_ids", {}).update(found)
        return out

    # ── create ────────────────────────────────────────────────────────────
    def create_one(self, unit: dict) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type == "instance_pool":
            return self._create_pool(unit)
        if asset_type == "cluster_policy":
            return self._create_policy(unit)
        if asset_type == "cluster":
            return self._create_cluster(unit)
        raise RuntimeError(f"compute importer got an unexpected asset_type {asset_type!r}")

    def update_one(self, unit: dict, target_id: str) -> dict:
        """The edit APIs — each differs from its create in a way worth spelling out."""
        asset_type = safe_str(unit.get("asset_type"))
        payload = dict(unit.get("payload") or {})
        if asset_type == "instance_pool":
            # instance-pools/edit takes the id AND THE FULL CONFIG — it is not a partial update, so
            # omitting a field RESETS it. Hence the whole payload rather than a diff.
            self.client.post("api/2.0/instance-pools/edit",
                             {**payload, "instance_pool_id": target_id})
            return {"target_id": target_id}
        if asset_type == "cluster_policy":
            body = {"policy_id": target_id, "name": safe_str(payload.get("name"))}
            if payload.get("definition") is not None:
                body["definition"] = payload["definition"]
            warns = self._remap_policy_body_ids(body)   # Bug 9: remap pinned pool ids on update too
            self.client.post("api/2.0/policies/clusters/edit", body)
            return {"target_id": target_id, "warning": "; ".join(warns) if warns else ""}
        if asset_type == "cluster":
            body = self._cluster_body(unit)
            body["cluster_id"] = target_id
            self.client.post("api/2.0/clusters/edit", body)
            # An edit can restart the cluster, so re-apply the "leave it stopped" policy.
            return {"target_id": target_id, "note": self._stop_cluster(target_id)}
        return {"target_id": target_id}

    # ── pools ─────────────────────────────────────────────────────────────
    def _create_pool(self, unit: dict) -> dict:
        payload = dict(unit.get("payload") or {})
        payload.setdefault("instance_pool_name", self.natural_key(unit))
        created = self.client.post("api/2.0/instance-pools/create", payload)
        pool_id = safe_str(created.get("instance_pool_id"))
        self.context.setdefault("instance_pool_target_ids", {})[self.natural_key(unit)] = pool_id
        return {"target_id": pool_id}

    # ── policies ──────────────────────────────────────────────────────────
    def _create_policy(self, unit: dict) -> dict:
        """Send only what create accepts (a policy-FAMILY policy is a different shape)."""
        payload = dict(unit.get("payload") or {})
        body = {"name": safe_str(payload.get("name")) or self.natural_key(unit)}
        for field in ("definition", "description", "libraries", "max_clusters_per_user",
                      "policy_family_id", "policy_family_definition_overrides"):
            if payload.get(field) is not None:
                body[field] = payload[field]
        warns = self._remap_policy_body_ids(body)
        created = self.client.post("api/2.0/policies/clusters/create", body)
        policy_id = safe_str(created.get("policy_id"))
        self.context.setdefault("cluster_policy_target_ids", {})[self.natural_key(unit)] = policy_id
        return {"target_id": policy_id, "warning": "; ".join(warns) if warns else ""}

    def _remap_policy_body_ids(self, body: dict) -> list[str]:
        """Remap SOURCE object ids pinned inside the policy `definition`/overrides (Bug 9).

        A policy that FIXES `instance_pool_id` to a SOURCE pool id rejects every (correctly remapped)
        cluster or job under it — `INVALID_PARAMETER_VALUE: the value must be <target-pool> (is
        "<source-pool>")`. So the ids INSIDE the definition must be remapped through the same pool map
        the cluster/job importers use, not just the ids in the cluster spec."""
        warns: list[str] = []
        for field in ("definition", "policy_family_definition_overrides"):
            if body.get(field) is not None:
                body[field], w = self._remap_policy_definition(body[field])
                warns.extend(w)
        return warns

    def _remap_policy_definition(self, definition):
        """Remap pool ids inside a policy definition (a JSON string OR a dict). Returns
        `(remapped_definition_same_type, warnings)`; leaves an unparseable definition verbatim."""
        as_string = isinstance(definition, str)
        try:
            doc = json.loads(definition) if as_string else definition
        except (TypeError, ValueError):
            return definition, []
        if not isinstance(doc, dict):
            return definition, []
        doc = dict(doc)
        warns: list[str] = []
        for key in _POLICY_POOL_KEYS:
            constraint = doc.get(key)
            if not isinstance(constraint, dict):
                continue
            constraint = dict(constraint)
            # PLAN 11 Finding-10: exact-or-fail-loud. A pinned pool id must resolve to the pool we
            # recreated, or the policy fails (retryable if in-bundle-not-yet, hard if not in bundle)
            # — never keep the SOURCE pool id (which rejected every cluster/job under the policy).
            for sub in ("value", "defaultValue"):
                src = safe_str(constraint.get(sub))
                if src:
                    constraint[sub] = self.require_remap(
                        "instance_pool", src, referenced_by=f"cluster policy pin {key}.{sub}")
            vals = constraint.get("values")
            if isinstance(vals, list):
                constraint["values"] = [
                    self.require_remap("instance_pool", safe_str(v),
                                       referenced_by=f"cluster policy pin {key}.values")
                    if safe_str(v) else v
                    for v in vals]
            doc[key] = constraint
        return (json.dumps(doc) if as_string else doc), warns

    # ── clusters ──────────────────────────────────────────────────────────
    def _create_cluster(self, unit: dict) -> dict:
        body, remap_warning = self._cluster_body_and_warning(unit)
        body.setdefault("cluster_name", self.natural_key(unit))
        created = self.client.post("api/2.0/clusters/create", body)
        cluster_id = safe_str(created.get("cluster_id"))
        self.context.setdefault("cluster_target_ids", {})[self.natural_key(unit)] = cluster_id

        notes = [self._stop_cluster(cluster_id)]
        if self._was_pinned(unit):
            notes.append(self._pin_cluster(cluster_id))
        note = "; ".join(n for n in notes if n)
        # A dropped pool/policy reference means the cluster exists but is NOT configured as on
        # source, so it is reported degraded rather than clean.
        return {"target_id": cluster_id, "note": note,
                "warning": f"{note}. {remap_warning}" if remap_warning else ""}

    @staticmethod
    def _was_pinned(unit: dict) -> bool:
        payload = unit.get("payload") or {}
        return bool(unit.get("pinned") or payload.get("pinned"))

    def _cluster_body(self, unit: dict) -> dict:
        return self._cluster_body_and_warning(unit)[0]

    def _cluster_body_and_warning(self, unit: dict) -> tuple[dict, str]:
        """`(body, warning)` — remap pool + policy ids, then resolve pool/node-type conflicts."""
        payload = unit.get("payload") or {}
        body = dict(payload)
        warnings: list[str] = []

        # Remap through `source id → natural key → target id`. PLAN 11 Finding-10: exact-or-fail-loud
        # — the old "drop the reference to let the cluster be created" silently changed the cluster's
        # config. Now an unresolved pool/policy is a retryable prerequisite (in bundle, not yet on
        # target) or a hard failure (not in bundle), never a silent drop.
        for field, ref_type in (("instance_pool_id", "instance_pool"),
                                ("driver_instance_pool_id", "instance_pool"),
                                ("policy_id", "cluster_policy")):
            src_id = safe_str(body.get(field))
            if not src_id:
                continue
            body[field] = self.require_remap(ref_type, src_id,
                                             referenced_by=f"cluster `{self.natural_key(unit)}`")

        # A pool dictates the node types, so sending them alongside it is rejected outright.
        if body.get("instance_pool_id"):
            for field in _POOL_CONFLICTS:
                body.pop(field, None)

        # The API attributes the cluster to the CALLER, so the source creator can't be preserved —
        # recorded as a tag rather than lost.
        creator = safe_str(payload.get("creator_user_name"))
        if creator:
            tags = dict(body.get("custom_tags") or {})
            tags.setdefault("OriginalCreator", creator)
            body["custom_tags"] = tags
        for field in ("creator_user_name", "pinned"):
            body.pop(field, None)

        warning = " ".join(warnings)
        if warning:
            self.result.warnings.append(f"cluster/{self.natural_key(unit)}: {warning}")
        return body, warning

    def _stop_cluster(self, cluster_id: str) -> str:
        """Terminate a just-created cluster. Best-effort — a running cluster isn't a failed import."""
        try:
            self.client.post("api/2.0/clusters/delete", {"cluster_id": cluster_id})
            return "stopped immediately after create (so it consumes no DBUs)"
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not stop the new cluster", cluster_id=cluster_id,
                             error=str(exc)[:200])
            return (f"created but could NOT be stopped ({str(exc)[:120]}) — terminate it manually "
                    f"to avoid consuming DBUs")

    def _pin_cluster(self, cluster_id: str) -> str:
        """Re-pin a pinned cluster: unpinned, a terminated cluster vanishes from the UI on cleanup."""
        try:
            self.client.post("api/2.0/clusters/pin", {"cluster_id": cluster_id})
            return "re-pinned (as on source)"
        except Exception as exc:  # noqa: BLE001
            return f"could not re-pin ({str(exc)[:100]})"
