"""
MiscImporter — phase 11: global init scripts → cluster libraries → workspace conf (Plan 3 §6, D6).

Three unrelated assets share a phase because each is small and each depends on something earlier.

**Cluster libraries are DEFERRED by default, and that is a deliberate decision (D6).**
`libraries/install` needs a RUNNING cluster, but the compute phase deliberately STOPS every cluster
right after creating it so the migration doesn't burn the customer's DBUs. Force-starting clusters to
install libraries would silently spend their money, so instead each library is recorded `manual` with
its cluster and spec, and `library_force_start_clusters=true` opts into starting them. Silently
burning DBUs is not a default.

**Workspace conf is written key-by-key, from a documented key set only.** `PATCH workspace-conf`
accepts arbitrary keys, and blanket-writing whatever the source returned could flip a security
setting (token access, IP-list enforcement) on the target. So each key is applied individually — one
rejected key doesn't take the others down with it — and the note names what changed.

**IP access lists are NOT here**: they are account-level for this customer, so a workspace-scoped tool
cannot see or migrate them (master §6a) — a customer/account-admin task, out of scope by decision.
"""
from __future__ import annotations

import json

from src.importers.base_importer import BaseImporter, PrerequisiteMissing, SkippedNoObject
from src.state.state_store import CAT_DEPENDENCY_UNRESOLVED
from src.utils.helpers import safe_str

# Library statuses that mean "already registered on the cluster" — so an existing-check SKIP is
# safe (Bug 16). A FAILED / UNINSTALL_ON_RESTART library is NOT present, so it still re-installs.
_LIBRARY_PRESENT_STATES = frozenset({"INSTALLED", "PENDING", "RESOLVING", "INSTALLING",
                                     "RESTORING"})

# Workspace-conf keys this tool is willing to write. Anything else is reported rather than applied:
# a conf key can change the workspace's SECURITY posture, and blanket-writing an unknown key from a
# source workspace is not a risk worth taking silently.
KNOWN_CONF_KEYS = frozenset({
    "enableTokensConfig", "maxTokenLifetimeDays", "enableDeprecatedClusterNamedInitScripts",
    "enableDeprecatedGlobalInitScripts", "enableIpAccessLists", "enableProjectTypeInWorkspace",
    "enableWorkspaceFilesystem", "enableDbfsFileBrowser", "enableExportNotebook",
    "enableNotebookTableClipboard", "enableResultsDownloading", "enableUploadDataUis",
    "enableWebTerminal", "enforceUserIsolation", "storeInteractiveNotebookResultsInCustomerAccount",
    "enableVerboseAuditLogs",
})


class MiscImporter(BaseImporter):
    component = "misc"
    asset_types = ("global_init_script", "cluster_library", "workspace_conf")

    # Workspace conf is DECLARATIVE against a workspace that always exists, so an "adopt" must still
    # write the value — otherwise a conf key would never actually be applied.
    declarative_asset_types = ("workspace_conf",)

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # Clusters WE force-started this run, to STOP ONCE at the end of the phase (Bug 1). Batching
        # the stop to the end — rather than per library in a `finally` — is what fixes the
        # start/stop RACE: the first library's stop used to put the cluster into Terminating while
        # the second library's start was still trying, so only the first library on a shared cluster
        # installed. Now the cluster is started once, ALL its libraries install, then it is stopped
        # once. We only ever stop clusters in this set, so a cluster the customer already had running
        # is never stopped out from under them.
        self._force_started_clusters: set = set()

    def load(self) -> list[dict]:
        """Init scripts → libraries (need clusters) → conf."""
        return self.units_for("global_init_script", "cluster_library", "workspace_conf")

    def existing_keys(self) -> dict:
        out: dict = {}
        scripts = (self.client.get("api/2.0/global-init-scripts") or {}).get("scripts") or []
        found = {safe_str(s.get("name")): safe_str(s.get("script_id"))
                 for s in scripts if s.get("name")}
        self.context.setdefault("global_init_script_target_ids", {}).update(found)
        out.update(found)

        # Conf keys already SET on target. Only the documented keys are probed — asking for an
        # unknown key errors on some workspaces.
        try:
            conf = self.client.get("api/2.0/workspace-conf",
                                   params={"keys": ",".join(sorted(KNOWN_CONF_KEYS))}) or {}
            for key, value in (conf.items() if isinstance(conf, dict) else []):
                out[key] = safe_str(value)
        except Exception as exc:  # noqa: BLE001 — a conf read failure must not stop the phase
            self.log.warning("workspace-conf read failed", error=str(exc)[:200])

        # Bug 16: cluster libraries had NO existence check, so `key in existing` was ALWAYS False,
        # driving decide() to CREATE on every run — which re-attempts `libraries/install` (needs a
        # RUNNING cluster) and spuriously FAILS on the normally-stopped cluster, even for a library
        # that is already installed. Index the libraries ALREADY registered on their target cluster
        # so decide() can SKIP/ADOPT them.
        out.update(self._installed_cluster_library_keys())
        return out

    def _installed_cluster_library_keys(self) -> dict:
        """`{cluster_library natural_key: "<target_cluster>:<label>"}` for libraries already present
        on their target cluster. Queries `libraries/cluster-status` ONCE per target cluster."""
        out: dict = {}
        units = self.units_by_type.get("cluster_library", []) or []
        labels_by_cluster: dict = {}
        for unit in units:
            payload = unit.get("payload") or {}
            target_cluster, _key = self.remap_id("cluster", safe_str(payload.get("cluster_id")))
            if not target_cluster:
                continue      # no target cluster → Bug 13 records it skipped_no_object at create
            if target_cluster not in labels_by_cluster:
                labels_by_cluster[target_cluster] = self._installed_library_labels(target_cluster)
            if _library_label(payload.get("library")) in labels_by_cluster[target_cluster]:
                out[self.natural_key(unit)] = f"{target_cluster}:{_library_label(payload.get('library'))}"
        return out

    def _installed_library_labels(self, cluster_id: str) -> set:
        """The set of library labels registered on a target cluster (any non-failed state)."""
        try:
            doc = self.client.get("api/2.0/libraries/cluster-status",
                                  params={"cluster_id": cluster_id}) or {}
        except Exception as exc:  # noqa: BLE001 — treat "cannot read" as "none present" (safe: the
            self.log.warning("cluster-status read failed", cluster_id=cluster_id,  # install adopts
                             error=str(exc)[:200])                                 # on ALREADY_EXISTS
            return set()
        labels = set()
        for st in doc.get("library_statuses") or []:
            if safe_str(st.get("status")).upper() in _LIBRARY_PRESENT_STATES:
                labels.add(_library_label(st.get("library")))
        return labels

    def create_one(self, unit: dict) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type == "global_init_script":
            return self._create_init_script(unit)
        if asset_type == "cluster_library":
            return self._install_library(unit)
        if asset_type == "workspace_conf":
            return self._set_conf(unit)
        raise RuntimeError(f"misc importer got an unexpected asset_type {asset_type!r}")

    def update_one(self, unit: dict, target_id: str) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type == "global_init_script":
            payload = unit.get("payload") or {}
            self.client.patch(f"api/2.0/global-init-scripts/{target_id}",
                              self._script_body(unit, payload))
            return {"target_id": target_id}
        if asset_type == "workspace_conf":
            return self._set_conf(unit)
        if asset_type == "cluster_library":
            # A changed library spec is a DIFFERENT unit (the spec IS the natural key), so this path
            # only fires for a re-attempt of the same spec — and install is idempotent.
            return self._install_library(unit)
        return {"target_id": target_id}

    # ── global init scripts ───────────────────────────────────────────────
    def _create_init_script(self, unit: dict) -> dict:
        payload = unit.get("payload") or {}
        created = self.client.post("api/2.0/global-init-scripts", self._script_body(unit, payload))
        sid = safe_str(created.get("script_id"))
        self.context.setdefault("global_init_script_target_ids", {})[self.natural_key(unit)] = sid
        enabled = bool(payload.get("enabled"))
        return {"target_id": sid,
                "note": (f"position {payload.get('position')}, "
                         f"{'ENABLED as on source' if enabled else 'disabled as on source'}")}

    def _script_body(self, unit: dict, payload: dict) -> dict:
        """The create body. `script` carries the base64 body exactly as exported."""
        body = {"name": safe_str(payload.get("name")) or self.natural_key(unit),
                "script": payload.get("script_b64") or payload.get("script") or "",
                # The source's enabled state is preserved rather than forced: an init script runs on
                # EVERY cluster launch, so silently enabling one would change cluster behaviour
                # workspace-wide, and silently disabling one would break it.
                "enabled": bool(payload.get("enabled"))}
        if payload.get("position") is not None:
            body["position"] = payload["position"]
        return body

    def run(self):
        """Run the phase, then STOP every cluster WE force-started — once each, after all its
        libraries installed (Bug 1). Stopping here rather than per library is what removes the
        start/stop race that let only the first library on a shared cluster install."""
        result = super().run()
        self._stop_force_started_clusters()
        return result

    def _stop_force_started_clusters(self) -> None:
        for cluster_id in sorted(self._force_started_clusters):
            self._stop_cluster(cluster_id)
        self._force_started_clusters.clear()

    # ── cluster libraries (deferred by default — D6) ──────────────────────
    def _install_library(self, unit: dict) -> dict:
        payload = unit.get("payload") or {}
        library = payload.get("library")
        src_cluster = safe_str(payload.get("cluster_id"))
        target_cluster, key = self.remap_id("cluster", src_cluster)
        if not target_cluster:
            # Bug 13 (LOCKED 2026-08-14): DOWNGRADE from FAILED to skipped_no_object. The library's
            # source cluster is not in the migrated set — an ephemeral/job cluster
            # (`compute_collector` deliberately skips those) or a cluster deleted on source. The unit
            # stays VISIBLE (inventory is the base — don't silently drop it), but a red FAILED is
            # wrong: there is simply no target cluster to install onto.
            raise SkippedNoObject(
                f"source cluster {src_cluster!r}" + (f" ({key!r})" if key else "")
                + " is not in the migrated cluster set (ephemeral/deleted) — the library was not "
                  "installed. It stays listed so the source library is not silently dropped.",
                category=CAT_DEPENDENCY_UNRESOLVED)

        # Bug 1: start the cluster AT MOST ONCE per run and DEFER the stop to the end of the phase
        # (run()), so multiple libraries on the SAME cluster no longer race each other's start/stop.
        state = self._cluster_state(target_cluster)
        if state != "RUNNING" and target_cluster not in self._force_started_clusters:
            if not self.config.imports.library_force_start_clusters:
                # Starting the cluster would spend the customer's money without being asked. So this
                # is recorded as outstanding work with everything needed to finish it, not attempted.
                raise PrerequisiteMissing(
                    f"library `{_library_label(library)}` could not be installed because target "
                    f"cluster `{key or target_cluster}` is {state or 'not running'} — "
                    f"`libraries/install` needs a RUNNING cluster, and the migration deliberately "
                    f"stops clusters after creating them so it does not consume DBUs. Either start "
                    f"the cluster and re-run with retry_mode=failed_only, or set "
                    f"library_force_start_clusters=true to let the tool start it.")
            # Flag is set: start the cluster ONCE. It is stopped ONCE at the end of the phase
            # (`_stop_force_started_clusters`), never per library — that is what fixes the race. We
            # remember WE started it, so a cluster the customer already had running is never stopped.
            self._start_cluster_and_wait(target_cluster, key)
            self._force_started_clusters.add(target_cluster)

        # No per-library stop: a single library's install failure is recorded per-unit by the base
        # class and must not abort the others on this cluster, nor skip the final stop.
        self.client.post("api/2.0/libraries/install",
                         {"cluster_id": target_cluster, "libraries": [library]})

        note = f"installed on target cluster {key or target_cluster}"
        if target_cluster in self._force_started_clusters:
            note += (" (cluster force-started for its whole library batch, then stopped once after "
                     "all of them — no idle DBUs)")
        return {"target_id": f"{target_cluster}:{_library_label(library)}", "note": note}

    # How long to wait for a force-started cluster to reach RUNNING before giving up. A cold start
    # is typically 3–7 min; 15 min covers a slow pool-less start without hanging the phase forever.
    _START_TIMEOUT_S = 900
    _START_POLL_S = 15

    def _start_cluster_and_wait(self, cluster_id: str, label: str) -> None:
        """Force-start a cluster and block until RUNNING (library_force_start_clusters=true, D6).

        Idempotent: `clusters/start` on an already-RUNNING/PENDING cluster is tolerated (a
        "already ... " rejection is swallowed) and we simply poll. Raises PrerequisiteMissing on a
        terminal state or timeout, so the failure is actionable rather than an opaque install error.
        """
        import time
        try:
            self.client.post("api/2.0/clusters/start", {"cluster_id": cluster_id})
        except Exception as exc:  # noqa: BLE001 — an already-starting cluster is fine; just poll.
            if "already" not in str(exc).lower():
                raise PrerequisiteMissing(
                    f"could not start target cluster `{label or cluster_id}` to install libraries: "
                    f"{exc}")
        waited = 0
        while waited < self._START_TIMEOUT_S:
            state = self._cluster_state(cluster_id)
            if state == "RUNNING":
                return
            if state in ("TERMINATED", "ERROR", "UNKNOWN", ""):
                # A cluster that goes back to TERMINATED/ERROR while we wait won't self-heal.
                if waited:  # give the very first poll a chance (state can lag the start call)
                    raise PrerequisiteMissing(
                        f"target cluster `{label or cluster_id}` did not start (state={state or '?'}) "
                        f"— check its config/quota, then re-run with retry_mode=failed_only.")
            time.sleep(self._START_POLL_S)
            waited += self._START_POLL_S
        raise PrerequisiteMissing(
            f"target cluster `{label or cluster_id}` did not reach RUNNING within "
            f"{self._START_TIMEOUT_S // 60} min of a force-start — re-run with "
            f"retry_mode=failed_only once it is up.")

    def _stop_cluster(self, cluster_id: str) -> None:
        """Best-effort stop of a cluster WE force-started — never fail the unit on the stop."""
        try:
            self.client.post("api/2.0/clusters/delete", {"cluster_id": cluster_id})
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not stop force-started cluster", cluster_id=cluster_id,
                             error=str(exc))

    def _cluster_state(self, cluster_id: str) -> str:
        try:
            got = self.client.get("api/2.0/clusters/get", params={"cluster_id": cluster_id}) or {}
            return safe_str(got.get("state"))
        except Exception:  # noqa: BLE001
            return ""

    # ── workspace conf ────────────────────────────────────────────────────
    def _set_conf(self, unit: dict) -> dict:
        """Apply ONE conf key. Unknown keys are reported, never blanket-written.

        A conf key can change the workspace's security posture (token access, IP-list enforcement),
        so writing whatever the source happened to return would be an unreviewed security change.
        """
        payload = unit.get("payload") or {}
        key = safe_str(payload.get("key")) or self.natural_key(unit)
        value = payload.get("value")
        if key not in KNOWN_CONF_KEYS:
            raise PrerequisiteMissing(
                f"workspace conf key `{key}` is not in this tool's documented key set, so it was NOT "
                f"written — a conf key can change the workspace's security posture, and applying an "
                f"unrecognised one from the source without review is not something the tool will do. "
                f"Set it by hand if it is wanted: value on source was {value!r}.")
        # One key per call: a single rejected key must not take the others down with it.
        self.client.patch("api/2.0/workspace-conf", {key: safe_str(value)})
        return {"target_id": key, "note": f"{key} set to {safe_str(value)!r}"}


def _library_label(library) -> str:
    """A short human label for a library spec, for notes and target ids."""
    if not isinstance(library, dict):
        return safe_str(library)
    for kind in ("pypi", "maven", "cran"):
        spec = library.get(kind)
        if isinstance(spec, dict):
            return f"{kind}:{safe_str(spec.get('package') or spec.get('coordinates'))}"
    for kind in ("jar", "whl", "egg", "requirements"):
        if library.get(kind):
            return f"{kind}:{safe_str(library[kind])}"
    return json.dumps(library, sort_keys=True)[:80]
