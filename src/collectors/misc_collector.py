"""
MiscCollector — global init scripts, cluster libraries, workspace conf.

PATs/tokens are EXCLUDED (disabled in the customer workspace). IP access lists are EXCLUDED
too: in this customer's setup they are configured at the ACCOUNT level (account console /
account API), which a workspace-scoped tool cannot see or migrate — they are a customer /
account-admin manual task, not a workspace asset. Each sub-fetch is independent and
best-effort so one failing (e.g. a feature disabled) never drops the others.
natural_key varies by misc_type (script name / conf key / cluster+library).
"""
from __future__ import annotations

import json

from src.collectors.base_collector import BaseCollector
from src.utils.helpers import safe_str

# workspace-conf keys we read + carry (documented default set; operator can trim). master §11.
_WS_CONF_KEYS = [
    "enableTokensConfig", "maxTokenLifetimeDays", "enableIpAccessLists",
    "enableExportNotebook", "enableResultsDownloading", "enableWebTerminal",
    "enableDbfsFileBrowser", "enableUploadDataUis",
    "storeInteractiveNotebookResultsInCustomerAccount",
]


class MiscCollector(BaseCollector):
    object_type = "misc"

    def natural_key(self, obj: dict) -> str:
        return safe_str(obj.get("_natural_key"))

    def discover(self) -> list[dict]:
        out: list[dict] = []
        out.extend(self._global_init_scripts())
        out.extend(self._cluster_libraries())
        out.extend(self._workspace_conf())   # IP access lists dropped — account-level (see module docstring)
        return out

    def _global_init_scripts(self) -> list[dict]:
        try:
            raw = self.client.get("api/2.0/global-init-scripts").get("scripts", []) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning("global-init-scripts failed", error=str(exc))
            return []
        items = []
        for s in raw:
            sid = safe_str(s.get("script_id"))
            script = {}
            try:
                script = self.client.get(f"api/2.0/global-init-scripts/{sid}") or {}
            except Exception as exc:  # noqa: BLE001
                self.log.warning("gis detail failed", script_id=sid, error=str(exc))
            items.append({
                "misc_type": "global_init_script",
                "script_id": sid,
                "name": safe_str(s.get("name")),
                "_natural_key": safe_str(s.get("name")),
                "position": s.get("position"),
                "enabled": s.get("enabled"),
                "script_b64": script.get("script"),   # base64 body from detail
                "_raw": s,
            })
        return items

    def _all_purpose_cluster_ids(self):
        """The set of NON-ephemeral (all-purpose) cluster ids on source, or None if the list could
        not be read. Mirrors `compute_collector`'s ephemeral exclusion so cluster libraries match
        how the clusters themselves are inventoried (PLAN 11 Finding-11)."""
        from src.collectors.compute_collector import _EPHEMERAL_CLUSTER
        try:
            raw = self.client.get("api/2.0/clusters/list").get("clusters", []) or []
        except Exception as exc:  # noqa: BLE001 — unknown → don't filter (keep old behaviour)
            self.log.warning("clusters/list failed; cluster-library ephemeral filter skipped",
                             error=str(exc))
            return None
        out = set()
        for c in raw:
            name = safe_str(c.get("cluster_name"))
            src = c.get("cluster_source", "")
            if _EPHEMERAL_CLUSTER.match(name) or src in ("JOB", "PIPELINE", "MODELS"):
                continue
            cid = safe_str(c.get("cluster_id"))
            if cid:
                out.add(cid)
        return out

    def _cluster_libraries(self) -> list[dict]:
        try:
            raw = self.client.get("api/2.0/libraries/all-cluster-statuses").get("statuses", []) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning("all-cluster-statuses failed", error=str(exc))
            return []
        # PLAN 11 Finding-11: `all-cluster-statuses` returns libraries for EVERY cluster, including
        # the ephemeral job/DLT/model clusters that `compute_collector` deliberately excludes — which
        # produced ~49 clusters × 3 libs = 147 noisy `skipped_no_object` rows at customer scale (the
        # libraries live on ephemeral job clusters that are recreated with their own config every
        # run / DAB redeploy — never installable as standalone). Skip any library whose cluster is
        # not in the all-purpose set, matching how the clusters themselves are inventoried. None =
        # the cluster list was unreadable → don't filter (never silently drop everything).
        allpurpose = self._all_purpose_cluster_ids()
        items = []
        for st in raw:
            cid = safe_str(st.get("cluster_id"))
            if allpurpose is not None and cid not in allpurpose:
                continue
            for ls in st.get("library_statuses", []) or []:
                lib = ls.get("library", {}) or {}
                items.append({
                    "misc_type": "cluster_library",
                    "cluster_id": cid,
                    "library": lib,
                    "_natural_key": f"{cid}:{json.dumps(lib, sort_keys=True)}",
                    "status": safe_str(ls.get("status")),
                    "is_library_for_all_clusters": bool(ls.get("is_library_for_all_clusters", False)),
                })
        return items

    def _workspace_conf(self) -> list[dict]:
        items = []
        for key in _WS_CONF_KEYS:
            try:
                data = self.client.get("api/2.0/workspace-conf", params={"keys": key})
                val = data.get(key) if isinstance(data, dict) else None
            except Exception as exc:  # noqa: BLE001
                self.log.warning("workspace-conf failed", key=key, error=str(exc))
                continue
            if val is not None:
                items.append({
                    "misc_type": "workspace_conf",
                    "key": key,
                    "_natural_key": key,
                    "value": val,
                })
        return items
