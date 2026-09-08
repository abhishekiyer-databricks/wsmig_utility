"""
WorkspaceCollector — directories, notebooks, files, repos + object ACLs (SOURCE workspace).

Recursive walk of /api/2.0/workspace/list with the special-path rules from the migrate review
(master §10a): `/Repos` handled separately (repo pointers, not notebooks); Trash skipped;
user-home roots noted (can't be mkdir'd on target — user must exist first). Inventory records
paths, object types, language, and ACLs; the notebook/file BYTES are pulled in Export (Plan 2),
not here. `max_ws_api_calls` caps directory traversal on very deep trees (safety, like the
inventory script). natural_key = workspace path.
"""
from __future__ import annotations

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import dab_path_info, is_bundle_root_path, safe_str

# /Projects is not inventoried as workspace content. /Repos IS descended (to discover git
# folders) but its container dirs are not emitted as content — see _walk.
_SKIP_TOP = ("/Projects",)


class WorkspaceCollector(BaseCollector):
    object_type = "workspace_object"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("path"))

    def discover(self) -> list[dict]:
        self._api_calls = 0
        self._max_calls = int(getattr(self.config, "max_ws_api_calls", 0) or 0)
        self._max_items = int(getattr(self.config, "max_workspace_items", 0) or 0)
        self._repo_ids: set[str] = set()   # git folders found during the walk (by object_id)
        # DAB bundle state files seen during the walk → feed DabRegistry (pathless-asset DAB
        # detection). Public so the runner can read it after the collector runs.
        self.bundle_state_paths: set[str] = set()
        objs: list[dict] = []
        self._walk("/", objs)
        objs.extend(self._repos())
        return objs

    def _budget_left(self) -> bool:
        return self._max_calls == 0 or self._api_calls < self._max_calls

    @staticmethod
    def _is_trash(path: str) -> bool:
        return path.startswith("/Users/") and "/Trash" in path

    @staticmethod
    def _is_user_root(path: str) -> bool:
        # /Users/<email> — home root; target creates it when the user exists.
        parts = [p for p in path.split("/") if p]
        return len(parts) == 2 and parts[0] == "Users"

    def _walk(self, path: str, out: list[dict]) -> None:
        if not self._budget_left():
            self.client.warnings.append(
                f"workspace/list: hit max_ws_api_calls={self._max_calls} — tree traversal truncated")
            return
        if self._max_items and len(out) >= self._max_items:
            return
        self._api_calls += 1
        try:
            data = self.client.get("api/2.0/workspace/list", params={"path": path})
        except Exception as exc:  # noqa: BLE001
            self.log.warning("workspace/list failed", path=path, error=str(exc))
            return
        for obj in data.get("objects", []) or []:
            p = safe_str(obj.get("path"))
            otype = safe_str(obj.get("object_type"))  # DIRECTORY | NOTEBOOK | FILE | REPO | LIBRARY
            if self._is_trash(p):
                continue
            # A git folder is a DIRECTORY with directory_info.is_git_folder=true, OR an object
            # typed REPO. It can live under /Repos OR inside a user folder — the ONLY reliable
            # signal across both is is_git_folder from workspace/list (the /repos list API can
            # return empty even when git folders exist). Record its id; detail is fetched in
            # _repos(). Do NOT descend into it (its contents are the cloned repo, not ours).
            is_git = (otype == "REPO") or bool(
                (obj.get("directory_info") or {}).get("is_git_folder"))
            if is_git:
                rid = safe_str(obj.get("object_id"))
                if rid:
                    self._repo_ids.add(rid)
                continue  # never descend into a git folder (its contents are the cloned repo)
            # /Projects is not inventoried as content.
            if any(p == t or p.startswith(t + "/") for t in _SKIP_TOP):
                continue
            # /Repos and /Repos/<user> are pure containers for git folders: descend to discover
            # the git folders inside them, but don't emit the container dirs as workspace content.
            if p == "/Repos" or p.startswith("/Repos/"):
                if otype == "DIRECTORY":
                    self._walk(p, out)
                continue
            # DAB classification (customer: code is redeployed to the target via an Azure
            # DevOps script that runs each bundle, NOT migrated file-by-file by this tool). So
            # each item records whether it lives under a `.bundle/` folder and, if so, whether
            # the bundle is shared (/Shared/.bundle — current staging + all prod) or user-scoped
            # (/Users/<email>/.bundle or /Workspace/<uuid>/.bundle — legacy staging pattern).
            dab = dab_path_info(p, getattr(self.config, "dab_bundle_roots", None))
            record = {
                "path": p,
                "object_type": otype,
                "language": safe_str(obj.get("language")),
                "object_id": safe_str(obj.get("object_id")),
                # DASHBOARD/ALERT entries carry the owning asset's id, which is how their
                # on-disk twin is matched to its native unit (paths are unreliable: an Alerts V2
                # record has parent_path=null even on a detail GET). For a DASHBOARD,
                # `resource_id` IS the Lakeview dashboard_id; for an ALERT, `object_id` is the
                # alert id (`resource_id` there is an unrelated uuid). Verified live on fvm1.
                "resource_id": safe_str(obj.get("resource_id")),
                "is_user_root": self._is_user_root(p),
                "deployed_by_dab": dab["deployed_by_dab"],
                "dab_scope": dab["dab_scope"],
                "acl": self._object_acl(otype, obj.get("object_id")),
            }
            # Remember bundle STATE files (`<root>/state/resources.json` or the legacy
            # `terraform.tfstate`). They map each bundle-owned resource to the concrete workspace
            # id it created, which is the only reliable way to tell that a PATHLESS asset (a
            # cluster, warehouse, pool, secret scope, serving endpoint) is DAB-managed. See
            # src/collectors/dab_registry.py for why tag-sniffing can't be used.
            # PLAN 11 Finding-12: honour configurable bundle roots so a Team-B directory-root bundle
            # (no `.bundle` segment) still has its `<root>/**/state/resources.json` discovered → the
            # registry claims that bundle's pathless clusters/warehouses/pools/scopes too.
            if otype == "FILE" and "/state/" in p \
                    and is_bundle_root_path(p, getattr(self.config, "dab_bundle_roots", None)) \
                    and p.rsplit("/", 1)[-1] in ("resources.json", "terraform.tfstate"):
                self.bundle_state_paths.add(p)

            out.append(record)
            if otype == "DIRECTORY":
                self._walk(p, out)

    def _object_acl(self, otype: str, object_id) -> list | None:
        # /Shared ACL is immutable on target (handled at import); still inventory others.
        # FILE objects DO have permissions (/api/2.0/permissions/files/{id}) — verified live.
        perm_type = {"NOTEBOOK": "notebooks", "DIRECTORY": "directories",
                     "FILE": "files"}.get(otype)
        if not perm_type:
            return None
        return self.fetch_acl(perm_type, object_id)

    def _repos(self) -> list[dict]:
        """Build repo records for every git folder found during the walk.

        Git folders live under /Repos OR inside a user folder. The /repos list API is
        unreliable (observed returning empty even when git folders exist), so the source of
        truth is `directory_info.is_git_folder` seen during the walk (self._repo_ids). We fetch
        full detail per id via GET /api/2.0/repos/{id} (works even when the list API is empty).
        The list API is still unioned in as a fallback, deduped by id, so nothing is missed.
        """
        repos, seen = [], set()

        def _add(rid: str, detail: dict) -> None:
            if not rid or rid in seen:
                return
            seen.add(rid)
            repos.append({
                "path": safe_str(detail.get("path")),
                "object_type": "REPO",
                "repo_id": rid,
                "url": safe_str(detail.get("url")),
                "provider": safe_str(detail.get("provider")),
                "branch": safe_str(detail.get("branch")),
                "head_commit_id": safe_str(detail.get("head_commit_id")),
                "acl": self.fetch_acl("repos", rid),
                "_raw": detail,
            })

        # Primary: per-id detail for git folders discovered during the walk.
        for rid in sorted(self._repo_ids):
            try:
                detail = self.client.get(f"api/2.0/repos/{rid}") or {}
            except Exception as exc:  # noqa: BLE001
                self.log.warning("repo detail failed", repo_id=rid, error=str(exc))
                detail = {}
            _add(rid, detail if isinstance(detail, dict) else {})

        # Fallback/union: the list API (in case it returns repos the walk didn't reach).
        for extra in ({}, {"path_prefix": "/Workspace"}):
            try:
                raw = self.client.get_paginated("api/2.0/repos", "repos",
                                                token_key="next_page_token", params=extra)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("repos list failed", extra=str(extra), error=str(exc))
                continue
            for r in raw:
                _add(safe_str(r.get("id")), r)
        return repos
