"""
InventoryRunner — orchestrates all source-side collectors for `01_Inventory`.

Keeps the notebook thin: build a client, run every in-scope collector (read-only), classify
identities, then emit inventory.json / .html / .xlsx + identity_classification.json +
config_resolved.json into the run's staging bundle dir. Nothing here mutates the workspace.
"""
from __future__ import annotations

from src.collectors.apps_collector import AppsCollector
from src.collectors.compute_collector import ComputeCollector
from src.collectors.dashboards_collector import DashboardsCollector
from src.collectors.dlt_collector import DltCollector
from src.collectors.genie_collector import GenieCollector
from src.collectors.identity_collector import IdentityCollector
from src.collectors.jobs_collector import JobsCollector
from src.collectors.lakebase_collector import LakebaseCollector
from src.collectors.misc_collector import MiscCollector
from src.collectors.secrets_collector import SecretsCollector
from src.collectors.serving_collector import ServingCollector
from src.collectors.sql_collector import SqlCollector
from src.collectors.workspace_collector import WorkspaceCollector
from src.exporters import bundle_paths as BP
from src.identity.classifier import (classify_all, classification_summary,
                                     needs_account_action)
from src.utils.helpers import now_iso
from src.utils.logger import get_logger

_LOG = get_logger("inventory")

# inventory.html is redundant with inventory.xlsx (PLAN 7 §B2). The generator code stays; only its
# invocation is gated behind this one switch, so re-enabling for a customer who asks is a one-liner.
WRITE_INVENTORY_HTML = False

# All in-scope collectors (inventory is always full-scope; toggles apply from Export on).
_COLLECTORS = [
    IdentityCollector, ComputeCollector, WorkspaceCollector, SecretsCollector,
    JobsCollector, SqlCollector, DltCollector, DashboardsCollector,
    GenieCollector, ServingCollector, MiscCollector,
    AppsCollector, LakebaseCollector,   # inventory-only (migration flagged manual, v1)
]


class InventoryRunner:
    def __init__(self, client, config, artifact_writer, dbutils=None) -> None:
        self.client = client
        self.config = config
        self.aw = artifact_writer
        self.dbutils = dbutils

    def run(self) -> dict:
        self.aw.ensure_output_path()
        objects_by_type: dict[str, list] = {}
        stats: list[dict] = []

        bundle_state_paths: set[str] = set()
        for cls in _COLLECTORS:
            coll = cls(self.client, self.config, self.dbutils)
            objs = coll.run()
            objects_by_type[coll.object_type] = objs
            stats.append(coll.stats())
            # The workspace walk is what discovers DAB bundle state files.
            bundle_state_paths |= getattr(coll, "bundle_state_paths", set()) or set()

        # Stamp DAB ownership on assets that have NO workspace path (clusters, pools, warehouses,
        # secret scopes, serving endpoints). Path-based detection already covered the workspace
        # tree; this closes the gap for everything else. Fail-soft: an empty registry just means
        # nothing is claimed, so assets export normally rather than being wrongly skipped.
        self._stamp_dab_ownership(objects_by_type, bundle_state_paths)

        # Classify identities (annotates in place).
        identities = objects_by_type.get("identity", [])
        classify_all(identities)
        id_summary = classification_summary(identities)

        counts = {t: len(o) for t, o in objects_by_type.items()}
        warnings = [e for s in stats for e in s.get("errors", [])]

        # ── artifacts ─────────────────────────────────────────────────────
        self.aw.write_json(BP.INVENTORY_JSON, {
            "generated_utc": now_iso(),
            "source_workspace_id": self.config.source_workspace_id,
            "counts": counts,
            "objects_by_type": objects_by_type,
            "collector_stats": stats,
        })
        # The account worklist: account GROUPS are the only identities that can still need a human
        # (users/SPs are assigned automatically by the workspace SCIM POST). Emitted at INVENTORY
        # time so the gap is known before export/import rather than surfacing as an import failure.
        account_actions = needs_account_action(identities)
        self.aw.write_json(BP.IDENTITY_CLASSIFICATION_JSON, {
            "summary": id_summary,
            "needs_account_action": account_actions,
            "identities": [{k: v for k, v in o.items() if k != "_raw"} for o in identities],
        })
        if account_actions:
            _LOG.info("account groups requiring provisioning in the TARGET account",
                      count=len(account_actions),
                      groups=[a["displayName"] for a in account_actions][:10])
        self.aw.write_json(BP.CONFIG_RESOLVED_JSON, self.config.redacted())

        # inventory.html generation is gated OFF by default (PLAN 7 §B2): inventory.xlsx carries the
        # same content, so the HTML is redundant. Flip WRITE_INVENTORY_HTML to re-enable in one line.
        if WRITE_INVENTORY_HTML:
            self._write_html(objects_by_type, counts, stats, id_summary, warnings)
        self._write_excel(objects_by_type, counts)

        # Drop the LATEST_INVENTORY.json pointer at the wsmig root so 02_Export — even when run
        # as a SEPARATE job/run — can resolve this run_id with a blank run_id widget (Plan 2 §2b).
        # Best-effort: a pointer hiccup must never fail the read-only inventory.
        try:
            from src.exporters.bundle_state import write_latest_pointer
            write_latest_pointer(self.config, self.config.run_id, counts)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("latest-inventory pointer not written", error=str(exc))

        _LOG.info("inventory complete", total=sum(counts.values()), warnings=len(warnings))
        return {"counts": counts, "identity_summary": id_summary,
                "warnings": warnings, "output_path": self.aw.root}

    # ── DAB ownership for pathless assets ─────────────────────────────────
    # (collector bucket, id field, asset_type as the bundle state names it)
    # For the mixed `sql` bucket: asset_type → the `sql_type` the collector stamps on records.
    _SQL_TYPE_FOR = {"sql_warehouse": "warehouse", "alert_v2": "alert"}
    _DAB_STAMP_TARGETS = (
        ("compute", "instance_pool_id", "instance_pool"),
        ("compute", "cluster_id", "cluster"),
        ("secret_scope", "name", "secret_scope"),
        ("serving_endpoint", "name", "serving_endpoint"),
        ("sql", "id", "sql_warehouse"),
        # Alerts V2 need the state file too, even though an alert HAS a workspace path:
        # `GET /api/2.0/alerts` (the LIST call) omits `parent_path` entirely — only GET-by-id
        # returns it — so sql_collector's path-based detection can never fire for them.
        # Verified live 2026-08-06; without this a DAB-deployed alert is classified Manual and
        # the importer would DUPLICATE an alert the customer's bundle redeploys.
        ("sql", "id", "alert_v2"),
    )

    def _stamp_dab_ownership(self, objects_by_type: dict, bundle_state_paths: set) -> None:
        if not bundle_state_paths:
            return
        from src.collectors.dab_registry import DabRegistry
        try:
            reg = DabRegistry.build(self.client, sorted(bundle_state_paths))
        except Exception as exc:  # noqa: BLE001 — never fail inventory over DAB detection
            _LOG.warning("DAB registry build failed", error=str(exc))
            return
        if not len(reg):
            return
        stamped = 0
        for bucket, id_field, asset_type in self._DAB_STAMP_TARGETS:
            for rec in objects_by_type.get(bucket, []) or []:
                # only consider records of the right kind within a mixed bucket
                if bucket == "compute" and not str(rec.get(id_field) or "").strip():
                    continue
                # The `sql` bucket is mixed (warehouses, queries, alerts, dashboards), so only
                # look at the sql_type this target is about.
                if bucket == "sql" and rec.get("sql_type") != self._SQL_TYPE_FOR[asset_type]:
                    continue
                if reg.owns(asset_type, rec.get(id_field)):
                    rec["deployed_by_dab"] = True
                    # Finding-12: scope from the bundle root (Shared → shared, else user), honouring
                    # a configurable directory root the same way `dab_path_info` does.
                    from src.utils.helpers import dab_path_info
                    root = reg.bundle_of(asset_type, rec.get(id_field))
                    _cfg = getattr(self, "config", None)
                    rec["dab_scope"] = dab_path_info(
                        root, getattr(_cfg, "dab_bundle_roots", None)).get("dab_scope") \
                        or ("shared" if "/Shared/" in safe_str(root) else "user")
                    stamped += 1
        _LOG.info("DAB ownership stamped", bundles=len(reg.bundles),
                  resources=len(reg), assets_flagged=stamped)

    def _write_html(self, objects_by_type, counts, stats, id_summary, warnings) -> None:
        from src.reports.html_generator import render_inventory
        html_doc = render_inventory(
            objects_by_type=objects_by_type, counts=counts, collector_stats=stats,
            identity_summary=id_summary, warnings=warnings,
            workspace_url=self.config.ctx.workspace_url, generated_at=now_iso(),
        )
        # HTML is plain text — safe to write directly to the Volume.
        self.aw.write_bytes(BP.INVENTORY_HTML, html_doc.encode("utf-8"))

    def _write_excel(self, objects_by_type, counts) -> None:
        try:
            from src.exporters.excel_generator import generate_excel
            self.aw.write_text_local_then_copy(
                BP.INVENTORY_XLSX,
                lambda local: generate_excel(objects_by_type, counts, local, self.config),
            )
        except Exception as exc:  # noqa: BLE001 — Excel is optional; never fail the run
            _LOG.warning("excel generation skipped", error=str(exc))
