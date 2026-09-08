"""
JobsImporter — phase 5: jobs, with the deepest remapping in the tool (Plan 3 §6, §7d).

A job settings blob references almost everything else, in several places, so this is where a naive
"POST the exported settings" does the most damage. Every one of the following is a real trap:

  • **Compute references appear in BOTH `job_clusters` AND every task** — `existing_cluster_id`,
    `new_cluster.policy_id`, `new_cluster.instance_pool_id`. Missing one leaves the job pointing at a
    SOURCE-workspace cluster id, which fails only when it runs.
  • **Schedules AND `continuous` must both be paused on import.** Pausing only `schedule` lets a
    `continuous` job start running against half-migrated data the moment it is created — the single
    most damaging default this tool could have.
  • **`run_as` pointing at a workspace-local SP must be remapped** through the SP map (the reference
    tool does NOT do this — our addition). Left alone it names an appId that doesn't exist on target.
  • **A job with no `IS_OWNER` ACL is malformed.** Reported rather than migrated, since the API will
    not accept it either.
  • **A `notebook_path` is NOT validated at create.** The Jobs API accepts a path that doesn't exist
    and the job then fails at FIRST RUN. That is why this importer statically pre-checks every task
    path and records `created_with_warning` — a create-failure check alone would never catch it, and
    the customer would discover it in production (D14).
"""
from __future__ import annotations

from src.importers.base_importer import BaseImporter, PrerequisiteMissing
from src.utils.helpers import safe_str


class JobsImporter(BaseImporter):
    component = "jobs"
    asset_types = ("job",)

    def load(self) -> list[dict]:
        return self.units_for("job")

    def existing_keys(self) -> dict:
        """`{job_name: job_id}` for jobs already on target — PAGINATED.

        `jobs/list` 2.1 pages with `next_page_token`, and `expand_tasks=true` is required or the
        response drops `tasks` entirely. A truncated list here would duplicate every job past the
        first page, which is why this one uses the cursor helper rather than a bare get.
        """
        jobs = self.client.get_paginated("api/2.1/jobs/list", "jobs",
                                         params={"limit": 100, "expand_tasks": "true"})
        found: dict = {}
        for job in jobs:
            name = safe_str((job.get("settings") or {}).get("name"))
            if name:
                # A duplicate job NAME is legal on Databricks. Keeping the first keeps the mapping
                # deterministic, and the create path's own duplicate handling covers the rest.
                found.setdefault(name, safe_str(job.get("job_id")))
        self.context.setdefault("job_target_ids", {}).update(found)
        return found

    def create_one(self, unit: dict) -> dict:
        body, warning = self._settings(unit)
        created = self.client.post("api/2.1/jobs/create", body)
        job_id = safe_str(created.get("job_id"))
        self.context.setdefault("job_target_ids", {})[self.natural_key(unit)] = job_id
        return {"target_id": job_id, "note": "schedule/continuous paused on import",
                "warning": warning}

    def update_one(self, unit: dict, target_id: str) -> dict:
        """`jobs/reset` replaces the settings WHOLESALE — which is what we want for an upsert."""
        body, warning = self._settings(unit)
        self.client.post("api/2.1/jobs/reset", {"job_id": target_id,
                                                "new_settings": body.get("settings", body)})
        return {"target_id": target_id, "warning": warning,
                "note": "settings replaced wholesale via jobs/reset"}

    # ── settings assembly ─────────────────────────────────────────────────
    def _settings(self, unit: dict) -> tuple[dict, str]:
        """`(create_body, warning)` — the fully remapped, paused job settings."""
        settings = dict(unit.get("payload") or {})
        warnings: list[str] = []

        settings["name"] = safe_str(settings.get("name")) or self.natural_key(unit)

        self._require_owner(unit, settings)
        self._remap_compute(settings, warnings)
        self._remap_run_as(settings, warnings)
        self._pause_triggers(settings)
        warnings.extend(self._check_notebook_paths(settings))

        # Server-side echoes that are not create fields.
        for field in ("created_time", "creator_user_name", "job_id", "run_as_user_name",
                      "effective_budget_policy_id"):
            settings.pop(field, None)

        warning = " ".join(warnings)
        if warning:
            self.result.warnings.append(f"job/{self.natural_key(unit)}: {warning}")
        return settings, warning

    def _require_owner(self, unit: dict, settings: dict) -> None:
        """A job with no IS_OWNER grant is malformed; the API rejects it too, so say so clearly."""
        acl = unit.get("acl") or (unit.get("payload") or {}).get("access_control_list") or []
        if not acl:
            return    # no ACL captured at all is normal — the ACL phase applies grants separately
        has_owner = any(safe_str(p.get("permission_level")) == "IS_OWNER"
                        for entry in acl if isinstance(entry, dict)
                        for p in (entry.get("all_permissions") or []))
        if not has_owner:
            self.result.warnings.append(
                f"job/{self.natural_key(unit)}: the source job has an ACL but no IS_OWNER grant, "
                f"which is malformed — the target may assign ownership to the run-as identity")

    def _remap_compute(self, settings: dict, warnings: list) -> None:
        """Remap compute ids in `job_clusters` AND every task — both, or the job breaks at run.

        PLAN 11 Finding-10: every reference is exact-or-fail-loud. The old behaviour (drop a job
        cluster's pool/policy; keep a dangling source `existing_cluster_id`) silently mis-pointed the
        job; now an unresolved reference is a retryable prerequisite (in bundle, not yet on target)
        or a hard failure (not in bundle at all), so the job is NEVER created silently mis-pointed.
        """
        for job_cluster in settings.get("job_clusters") or []:
            if isinstance(job_cluster, dict):
                self._remap_new_cluster(job_cluster.get("new_cluster"))

        for task in settings.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            tk = safe_str(task.get("task_key"))
            self._remap_new_cluster(task.get("new_cluster"))
            src_id = safe_str(task.get("existing_cluster_id"))
            if src_id:
                task["existing_cluster_id"] = self.require_remap(
                    "cluster", src_id, referenced_by=f"job task {tk!r}")
            self._remap_task_references(task, tk, warnings)

    def _remap_task_references(self, task: dict, task_key: str, warnings: list) -> None:
        """Remap the task references the tool previously MISSED ENTIRELY (Finding-10 GAP): a
        `sql_task.warehouse_id`, a `pipeline_task.pipeline_id`, and a `run_job_task.job_id` all
        carried the SOURCE id straight through, so the task pointed at a non-existent/other object
        and failed at run. Each is now remapped through its target-id map under the same
        exact-or-fail-loud rule (a pipeline/job created LATER in dependency order resolves on
        retry_mode=failed_only).

        Finding-10 addendum (found by the PLAN 11 Run 2 live test): a `sql_task` ALSO carries a
        nested `query.query_id` (a warehouse ref was remapped but the query it runs was NOT — so the
        task ran a source query id that does not exist on target), and a `dbt_task` carries its own
        `warehouse_id`. Both are now remapped like the rest. A `sql_task.alert`/`sql_task.dashboard`
        points at a LEGACY SQL alert/dashboard, which is a MANUAL migration item (never recreated by
        this tool), so there is nothing to remap to — the reference is left as-is and NAMED as a
        manual re-point, rather than hard-failing on an object that is intentionally out of scope."""
        # direct id fields on a task spec (each references an object THIS TOOL recreates)
        for spec_key, field, ref_type in (("sql_task", "warehouse_id", "sql_warehouse"),
                                          ("pipeline_task", "pipeline_id", "dlt_pipeline"),
                                          ("run_job_task", "job_id", "job"),
                                          ("dbt_task", "warehouse_id", "sql_warehouse")):
            spec = task.get(spec_key)
            if not isinstance(spec, dict):
                continue
            src = safe_str(spec.get(field))
            if src:
                spec[field] = self.require_remap(
                    ref_type, src, referenced_by=f"job task {task_key!r} {spec_key}.{field}")

        # nested sql_task sub-objects: a QUERY is migrated (remap exact-or-fail-loud); a legacy
        # ALERT/DASHBOARD is a manual item (warn + leave the id for a hand re-point).
        sql_task = task.get("sql_task")
        if isinstance(sql_task, dict):
            query = sql_task.get("query")
            if isinstance(query, dict) and safe_str(query.get("query_id")):
                query["query_id"] = self.require_remap(
                    "legacy_query", safe_str(query.get("query_id")),
                    referenced_by=f"job task {task_key!r} sql_task.query.query_id")
            for sub, id_field, human in (("alert", "alert_id", "a legacy SQL alert"),
                                         ("dashboard", "dashboard_id", "a legacy SQL dashboard")):
                block = sql_task.get(sub)
                if isinstance(block, dict) and safe_str(block.get(id_field)):
                    warnings.append(
                        f"task {task_key!r} sql_task.{sub} references {human} "
                        f"(id `{safe_str(block.get(id_field))}`) — legacy SQL {sub}s are a MANUAL "
                        f"migration item (not recreated by this tool), so the reference is left "
                        f"as-is and the task must be re-pointed by hand on target.")

    def _remap_new_cluster(self, new_cluster) -> None:
        """Remap a `new_cluster` spec's policy + pool ids (exact-or-fail-loud), drop node types when
        pooled."""
        if not isinstance(new_cluster, dict):
            return
        for field, ref_type in (("policy_id", "cluster_policy"),
                                ("instance_pool_id", "instance_pool"),
                                ("driver_instance_pool_id", "instance_pool")):
            src_id = safe_str(new_cluster.get(field))
            if src_id:
                new_cluster[field] = self.require_remap(ref_type, src_id,
                                                        referenced_by="a job cluster")
        if new_cluster.get("instance_pool_id"):
            for field in ("node_type_id", "driver_node_type_id", "enable_elastic_disk"):
                new_cluster.pop(field, None)

    def _remap_run_as(self, settings: dict, warnings: list) -> None:
        """Remap `run_as` through the SP map — the reference tool does NOT do this.

        A workspace-local SP has a NEW applicationId on target, so an unmapped `run_as` names an
        identity that doesn't exist and the job cannot run as intended.
        """
        run_as = settings.get("run_as")
        if not isinstance(run_as, dict):
            return
        app_id = safe_str(run_as.get("service_principal_name"))
        if app_id:
            mapped = (self.identity_map.get("sp_mapping") or {}).get(app_id, "")
            if mapped and mapped != app_id:
                run_as["service_principal_name"] = mapped
            elif not mapped:
                warnings.append(
                    f"run_as service principal {app_id!r} is not in the identity map, so it was "
                    f"left as-is — if it was a workspace-local SP its applicationId differs on "
                    f"target and the job will not run as intended.")
            return
        user = safe_str(run_as.get("user_name"))
        if user:
            mapped_user = (self.identity_map.get("user_map") or {}).get(user, user)
            run_as["user_name"] = mapped_user

    def _pause_triggers(self, settings: dict) -> None:
        """Pause `schedule`, `continuous` AND `trigger` when `pause_job_schedules` (the default).

        Pausing only `schedule` would let a CONTINUOUS job start the instant it is created and run
        against half-migrated data — so all three trigger shapes are paused, not just the obvious one.
        """
        if not self.config.transform.pause_job_schedules:
            return
        for key in ("schedule", "continuous", "trigger"):
            block = settings.get(key)
            if isinstance(block, dict):
                block["pause_status"] = "PAUSED"

    def _check_notebook_paths(self, settings: dict) -> list[str]:
        """Statically resolve every task path against target content (D14 — the time-bomb check).

        The Jobs API does NOT validate `notebook_path` at create: the job is created happily and then
        fails at FIRST RUN. So relying on create failures would let a broken job through silently.
        The unit is still created (the path may be intentional, e.g. content imported later), but it
        is recorded `created_with_warning` and NAMED in the report.
        """
        warnings: list[str] = []
        known = self.context.get("workspace_paths") or set()
        for task in settings.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for spec_key, path_key in (("notebook_task", "notebook_path"),
                                       ("spark_python_task", "python_file"),
                                       ("sql_task", "file")):
                spec = task.get(spec_key)
                if not isinstance(spec, dict):
                    continue
                path = safe_str(spec.get(path_key))
                if not path or path.startswith(("dbfs:/", "s3://", "abfss://", "/Volumes/")):
                    continue
                if path in known or self._exists_on_target(path):
                    continue
                warnings.append(
                    f"task {safe_str(task.get('task_key'))!r} references `{path}`, which does NOT "
                    f"exist on target. The job was still created (the Jobs API does not validate "
                    f"paths) but it will FAIL AT FIRST RUN — this is usually a notebook inside a Git "
                    f"folder, which is out of scope for import and must be recreated by hand.")
        return warnings

    def _exists_on_target(self, path: str) -> bool:
        try:
            got = self.client.get("api/2.0/workspace/get-status", params={"path": path})
            return bool(got)
        except Exception:  # noqa: BLE001 — absent 404s, which is the answer we want
            return False
