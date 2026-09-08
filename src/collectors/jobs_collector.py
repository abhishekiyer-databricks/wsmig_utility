"""
JobsCollector — jobs / workflows (SOURCE workspace).

Uses API 2.1 with `expand_tasks=true` so MULTI_TASK jobs include their `tasks` (a 2.0-only
list drops them — migrate review, master §10a). Cursor-paginated. Captures ACLs and whether
an IS_OWNER grant exists (jobs without one are malformed — flagged for the import side).
natural_key = job name (duplicate names are possible; Export/Import handle the `name:::id`
suffix trick, so inventory records both name and id).
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str


class JobsCollector(BaseCollector):
    object_type = "job"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("name"))

    def discover(self) -> list[dict]:
        raw = self.client.get_paginated(
            "api/2.1/jobs/list", "jobs",
            token_key="next_page_token",
            params={"limit": 25, "expand_tasks": "true"},
        )
        items = []
        for j in raw:
            job_id = safe_str(j.get("job_id"))
            # `jobs/list` OMITS run-as entirely (verified live: both `settings.run_as` and the
            # top-level `run_as_user_name` come back null on the list surface, exactly like the
            # alerts LIST drops `parent_path`). Only `jobs/get` returns it, so enrich per-id.
            full = self._get_job(job_id) or j
            settings = full.get("settings", {}) or {}
            acl = self.fetch_acl("jobs", job_id)
            # The API's `format` is unreliable — modern jobs report MULTI_TASK even with a single
            # task. The true signal is the task count, so derive an honest type from it (point 1).
            task_count = len(settings.get("tasks", []) or [])
            api_format = safe_str(settings.get("format"))
            derived = "SINGLE_TASK" if task_count == 1 else (
                "MULTI_TASK" if task_count > 1 else api_format)
            # Normalise run-as into a TYPED dict and stamp it into `settings` so it (a) renders in
            # the report and (b) is preserved by the importer, which remaps a db-managed SP's
            # applicationId through the SP map. See `_normalise_run_as`.
            run_as = self._normalise_run_as(full, settings)
            if run_as:
                settings["run_as"] = run_as
            items.append({
                "job_id": job_id,
                "name": safe_str(settings.get("name")),
                "format": api_format,                 # raw API value (kept for fidelity)
                "task_count": task_count,
                "job_type": derived,                  # honest type from task count
                "creator_user_name": safe_str(full.get("creator_user_name")),
                "run_as": run_as,                     # typed dict {user_name|service_principal_name}
                "has_owner_acl": self._has_owner(acl),
                # DAB-deployed jobs carry settings.deployment.kind == "BUNDLE" → should be
                # redeployed to the target via their bundle, not migrated by this tool (Plan 1a §4).
                "deployed_by_dab": self._is_dab(settings),
                "acl": acl,
                "settings": settings,   # full spec (tasks, job_clusters, schedule, continuous)
                "_raw": full,
            })
        return items

    def _get_job(self, job_id: str) -> dict:
        """`jobs/get` for the authoritative spec (run-as, full settings). Best-effort."""
        if not job_id:
            return {}
        try:
            return self.client.get("api/2.1/jobs/get", params={"job_id": job_id}) or {}
        except Exception as exc:  # noqa: BLE001 — never let one job abort discovery
            self._errors.append(f"jobs/get {job_id}: {exc}")
            return {}

    @staticmethod
    def _normalise_run_as(job: dict, settings: dict) -> dict:
        """Return run-as as a TYPED dict `{service_principal_name}` or `{user_name}`, or `{}`.

        Two shapes exist and only `jobs/get` returns either:
          • EXPLICIT — `settings.run_as` is already `{service_principal_name}` or `{user_name}`.
          • IMPLICIT — no explicit run-as, so the effective identity is the read-only top-level
            `run_as_user_name` (a resolved string). It is an SP applicationId (a bare UUID) or a
            user email; classify by the `@` — usernames are emails, SP appIds are not.
        Normalising both into the typed dict lets the importer's SP remap fire either way, so a
        db-managed SP run-as gets its NEW applicationId on target.
        """
        explicit = settings.get("run_as")
        if isinstance(explicit, dict) and (explicit.get("service_principal_name")
                                           or explicit.get("user_name")):
            return {k: v for k, v in explicit.items() if k in
                    ("service_principal_name", "user_name") and v}
        resolved = safe_str(job.get("run_as_user_name"))
        if not resolved:
            return {}
        return {"user_name": resolved} if "@" in resolved else {
            "service_principal_name": resolved}

    @staticmethod
    def _is_dab(settings: dict) -> bool:
        dep = settings.get("deployment") or {}
        return safe_str(dep.get("kind")) == "BUNDLE"

    @staticmethod
    def _has_owner(acl) -> bool:
        for entry in acl or []:
            for p in entry.get("all_permissions", []):
                if p.get("permission_level") == "IS_OWNER":
                    return True
        return False
