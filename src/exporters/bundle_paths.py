"""
bundle_paths — the ONE place the staging-bundle layout lives (PLAN 7 §D).

Every collector / exporter / importer / report writes and reads bundle files through the constants
here, never through a hardcoded string. The layout is therefore a one-line change: moving a file to
a new home is editing its constant, not hunting every call site.

Layout under `<staging>/wsmig/<source_workspace_id>/<run_id>/`:

    export/            exported asset payloads + content bytes + acls.json  (UNCHANGED — the handoff)
      identity/ compute/ workspace/ secrets/ dashboards/ misc/ ...
    reports/           human-facing outputs (xlsx + the import runbook)
    misc/              machine / bookkeeping JSON + the execution logs

Two facts callers rely on:
  • Manifest paths are RELATIVE to the run root, so a file moving into `reports/`/`misc/` simply
    gains that prefix — `verify_manifest` still resolves it.
  • `_excluded_from_manifest` matches on the BASENAME, so the exclusion rules are unaffected by the
    subdir a file now lives in.

The pair-level pointers (`LATEST_INVENTORY.json`, `LATEST_EXPORT.json`) live ABOVE the run dir at
the wsmig root and are owned by `bundle_state.py`, not here.
"""
from __future__ import annotations

# ── reports/ — human-facing ──────────────────────────────────────────────────
INVENTORY_XLSX = "reports/inventory.xlsx"
INVENTORY_HTML = "reports/inventory.html"          # generation gated off by default (PLAN 7 §B2)
EXPORT_STATUS_XLSX = "reports/export_status.xlsx"
IMPORT_STATUS_XLSX = "reports/import_status.xlsx"
IMPORT_STATUS_DRYRUN_XLSX = "reports/import_status_dry_run.xlsx"   # A1 — never clobbers the live one
MANUAL_ACTIONS_IMPORT_MD = "reports/manual_actions_import.md"

# ── misc/ — machine / bookkeeping ────────────────────────────────────────────
INVENTORY_JSON = "misc/inventory.json"
IDENTITY_CLASSIFICATION_JSON = "misc/identity_classification.json"
EXPORT_INDEX_JSON = "misc/export_index.json"
CONFIG_RESOLVED_JSON = "misc/config_resolved.json"
MANIFEST_JSON = "misc/manifest.json"
CHECKPOINT_JSON = "misc/checkpoint.json"
IMPORT_RESULTS_JSON = "misc/import_results.json"
PREFLIGHT_REPORT_JSON = "misc/preflight_report.json"
ACL_PARITY_REPORT_JSON = "misc/acl_parity_report.json"   # only written when D-1 = keep-standalone

EXECUTION_INVENTORY_LOG = "misc/execution_inventory.log"
EXECUTION_EXPORT_LOG = "misc/execution_export.log"
EXECUTION_IMPORT_LOG = "misc/execution_import.log"

# ── export/ — the exported bundle (UNCHANGED; the only thing the air-gap moves) ──
EXPORT_DIR = "export"
EXPORT_ACLS_JSON = "export/acls.json"
EXPORT_OVERSIZE_JSON = "export/oversize_artifacts.json"
EXPORT_MANUAL_ACTIONS_MD = "export/manual/manual_actions.md"
EXPORT_CONTENT_DIR = "export/workspace/content"

# Subdirectories created up-front by ArtifactWriter.ensure_output_path (alongside export/*).
# The `misc/` and `reports/` roots plus the export subtree the collectors/exporter write into.
TOP_LEVEL_SUBDIRS = ("reports", "misc")
EXPORT_SUBDIRS = ("export", "export/identity", "export/compute", "export/workspace",
                  "export/secrets", "export/dashboards", "export/misc")


def with_variant(rel_path: str, variant: str) -> str:
    """Insert a `_<variant>` tag before the file extension. Blank variant → path unchanged.

    Used so a re-run against the SAME run dir can preserve the prior report instead of clobbering it
    (PLAN 7 A1 = the `dry_run` variant; a `retry_<timestamp>` variant keeps each retry's report set):
      with_variant("reports/import_status.xlsx", "retry_20260811_101530")
        -> "reports/import_status_retry_20260811_101530.xlsx"
    """
    if not variant:
        return rel_path
    import os as _os
    root, ext = _os.path.splitext(rel_path)
    return f"{root}_{variant}{ext}"
