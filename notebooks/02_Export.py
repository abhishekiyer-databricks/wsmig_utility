# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Export (SOURCE side)
# MAGIC Turns the Plan 1 inventory into a portable, self-describing **bundle**: a create-ready
# MAGIC JSON payload per migratable unit, the actual notebook/file **bytes**, `export/acls.json`,
# MAGIC the `export_index.json` tie-back ledger, `export_status.xlsx`, and `manifest.json` (checksums).
# MAGIC
# MAGIC **Runs on the source-reading side** (air-gapped model — `plans/PLAN_0_master.md` §1,§3),
# MAGIC right after `01_Inventory`. Idempotent + checkpointed + fail-soft. Reads only the source;
# MAGIC writes only to `staging_location`. No target calls, no secrets.

# COMMAND ----------

# MAGIC %md ## Widgets
# MAGIC The role is DERIVED from the mode (PLAN 7 §C): `direct` (default) runs this in the TARGET,
# MAGIC reads the source over REST and writes the bundle to `staging_location`; `airgap` runs it
# MAGIC INSIDE the source and writes to `staging_location` for ops to move. No `role` widget.

# COMMAND ----------

# B12: a numeric "<N><letter>. " prefix is prepended to each widget's ORIGINAL display name so
# Databricks groups them in the bar (1=Source, 2=Output, 3=Bundle scope, 5=Run). Prefix only —
# the display text and the widget names/defaults are unchanged.
dbutils.widgets.dropdown("connectivity_mode", "direct", ["airgap", "direct"],
                         "1a. Connectivity mode")
dbutils.widgets.text("source_workspace_id", "", "1b. Source workspace id")
# direct-mode source connection. The secret is EITHER a scope pointer (preferred — a widget value is
# visible on the run page and kept in run history) OR spn_secret_value; scope+key wins when both set.
dbutils.widgets.text("source_workspace_url", "", "1c. [direct] Source workspace URL")
dbutils.widgets.text("source_sp_client_id", "", "1d. [direct] Source SP applicationId (not a secret)")
dbutils.widgets.text("source_sp_secret_scope", "", "1e. [direct] Secret scope for the SP secret")
dbutils.widgets.text("source_sp_secret_key", "", "1f. [direct] Secret key within that scope")
dbutils.widgets.text("spn_secret_value", "", "1g. [direct] SP secret (only if no scope/key; redacted)")
dbutils.widgets.text("staging_location", "", "2a. Staging location (/Volumes/...)")
# direct-mode ONLY: the shared control table lets export SKIP re-fetching workspace content whose
# source modified_at hasn't moved since it was migrated (incremental speed-up). Blank / airgap =
# full export as before. Same shared catalog+schema the import uses.
dbutils.widgets.text("state_catalog", "", "2b. State catalog (shared, must exist)")
dbutils.widgets.text("state_schema", "", "2c. State schema (shared, must exist)")
dbutils.widgets.text("max_scim", "0", "3a. Max SCIM per type (0 = all)")
dbutils.widgets.text("max_workspace_items", "0", "3b. Max workspace items (0 = all)")
dbutils.widgets.text("max_ws_api_calls", "0", "3c. Max workspace/list calls (0 = unlimited)")
dbutils.widgets.text("content_fetch_workers", "8", "3d. Parallel content-fetch workers")
dbutils.widgets.dropdown("force_full_export", "false", ["true", "false"],
                         "3e. Ignore checkpoint/resume — re-export everything")
# Per-asset toggles (all default true; set false to skip a family — still recorded as 'skip').
# These are BUNDLE scope and belong on the source side; import narrows with `import_assets` instead.
for _t in ["identity", "compute", "workspace", "secrets", "jobs", "sql", "dlt",
           "dashboards", "genie", "serving", "misc"]:
    dbutils.widgets.dropdown(f"migrate_{_t}", "true", ["true", "false"],
                             f"3z. Migrate {_t}")
dbutils.widgets.text("run_id", "", "5a. Run id (blank = use LATEST_INVENTORY / resume incomplete)")
dbutils.widgets.text("log_level", "DEBUG", "5b. Log level (DEBUG shows every step)")      # B5
dbutils.widgets.text("parallel_threads", "1",
                     "5c. Parallel enrichment threads (1 = serial)")                      # B6

# COMMAND ----------

# MAGIC %md ## Bootstrap `src/` onto sys.path + install requirements

# COMMAND ----------

# MAGIC %pip install -q databricks-sdk openpyxl requests

# COMMAND ----------

import os
import sys


def _add_repo_root_to_syspath() -> str:
    """Find the repo root (dir containing `src/`) and prepend it to sys.path (see 01_Inventory)."""
    candidates = []
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_path = ctx.notebookPath().get()
        repo_dir = os.path.dirname(os.path.dirname(nb_path))
        candidates += [repo_dir, "/Workspace" + repo_dir]
    except Exception:
        pass
    here = os.getcwd()
    candidates += [here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))]
    for cand in candidates:
        if cand and os.path.isdir(os.path.join(cand, "src")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            return cand
    raise RuntimeError("Could not locate the repo root (dir containing `src/`). Tried: "
                       + ", ".join(repr(c) for c in candidates))


_REPO_ROOT = _add_repo_root_to_syspath()
print(f"repo root on sys.path: {_REPO_ROOT}")

from src.config.config_manager import Config, STAGE_EXPORT
from src.auth.token_manager import build_clients
from src.exporters import bundle_paths as BP
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.bundle_state import resolve_export_run_id
from src.exporters.export_runner import ExportRunner
from src.state.sql_backend import build_sql_backend
from src.state.state_store import StateStore
from src.utils import logger as _logger

# COMMAND ----------

# MAGIC %md ## Build config + resolve which run to export

# COMMAND ----------

# Role is DERIVED from stage + mode (PLAN 7 §C): export reads the source, so role=source in airgap
# (runs inside the source) and role=target in direct (runs in the target, reads the source over
# REST). Config.validate() enforces the source connection widgets in direct mode.
cfg = Config.from_dbutils(dbutils, spark, stage=STAGE_EXPORT)

# Resolve the run_id (Plan 2 §2b): explicit widget → task-value (2-task job) → incomplete-bundle
# resume → LATEST_INVENTORY.json pointer → fail loudly. Never invent a run_id (empty bundle).
_widget_run_id = (dbutils.widgets.get("run_id") or "").strip()
if not _widget_run_id:
    try:
        _tv = dbutils.jobs.taskValues.get(taskKey="inventory", key="run_id", debugValue="")
        if _tv:
            _widget_run_id = str(_tv).strip()
            print(f"run_id taken from Inventory task values: {_widget_run_id}")
    except Exception as _exc:
        pass  # not a 2-task job / no task values — fall through to pointer/resume

_force_full = (dbutils.widgets.get("force_full_export") or "false").strip().lower() == "true"
_run_id, _how = resolve_export_run_id(cfg, _widget_run_id, _force_full)
cfg.run_id = _run_id

# (source_client, target_client) — the SAME local client in `airgap`, an M2M-bound client on
# source_workspace_url in `direct`. The exporter is unchanged either way; only which client it gets.
source_client, local_client = build_clients(cfg, dbutils=dbutils, spark=spark)
client = source_client
print(f"Source workspace : {client.base_url}"
      + (f"   (read over REST from {cfg.ctx.workspace_url})" if cfg.is_direct else ""))
print(f"Run id           : {cfg.run_id}  (resolved via: {_how})")
print(f"Bundle           : {cfg.output_path}")

# COMMAND ----------

# MAGIC %md ## Run export → bundle in staging

# COMMAND ----------

aw = ArtifactWriter(cfg, dbutils=dbutils, spark=spark)
_logger.set_log_file(os.path.join(aw.ensure_output_path(), BP.EXECUTION_EXPORT_LOG))
# B5: structured per-step logging live in THIS cell (run_id + stage on every line, DEBUG default).
_logger.configure_logging(run_id=cfg.run_id, stage="EXPORT", level=cfg.log_level, capture=False)

_workers = int((dbutils.widgets.get("content_fetch_workers") or "8") or 8)

# DIRECT mode only: build the shared control table so export can SKIP re-fetching unchanged
# workspace content (its fingerprint — the source modified_at — already matches the migrated row).
# airgap export runs source-side and must never touch the target table, so state stays None there.
# force_full_export disables the skip inside the runner. A first run (empty/absent table) just
# fetches everything, so this is safe before the first import has populated the table.
_export_state = None
if cfg.is_direct and cfg.state_enabled:
    _backend = build_sql_backend(cfg, spark=spark, client=local_client)
    _export_state = StateStore(_backend, cfg)
    _export_state.ensure_table()
    _export_state.load(force=True)
    print(f"Incremental state : {cfg.state_table_fqn} "
          f"({len(_export_state._cache)} rows — unchanged content will skip re-fetch)")

result = ExportRunner(client, cfg, aw, dbutils=dbutils,
                      content_fetch_workers=_workers,
                      force_full_export=_force_full, state=_export_state).run()

print("\n=== Export complete ===")
print(f"  total            {result['total']:>6}")
for k in ("success", "failure", "skipped_oversize", "manual", "dab", "skip"):
    print(f"  {k:<16} {result.get(k, 0):>6}")
# Export status says what we CAPTURED; import action says what the target side will DO with it.
# They're different questions — a "Skipped (DAB)" unit still lands on target, via the customer's
# bundle redeploy — so print both. Same two columns as every sheet in export_status.xlsx.
print("\n=== Import actions (what the TARGET side will do) ===")
for _act, _n in sorted((result.get("action_counts") or {}).items(), key=lambda kv: -kv[1]):
    print(f"  {_act or '(none)':<20} {_n:>6}")
print(f"\nBundle: {result['output_path']}")
print("  export/ + misc/export_index.json + export/acls.json + reports/export_status.xlsx + "
      "misc/manifest.json")

# Verify the manifest we just wrote checksums cleanly (handoff-integrity self-check).
_verify = aw.verify_manifest()
print(f"\nManifest self-check: {'OK' if _verify['ok'] else 'PROBLEM'}")
if not _verify["ok"]:
    print("  missing:", _verify["missing"][:10])
    print("  mismatched:", _verify["mismatched"][:10])

# Push the last log records to the Volume (the log is appended locally, then mirrored — append
# straight onto a UC Volume silently fails, which used to truncate the log to one line). Runs
# AFTER the manifest, which is why the manifest deliberately excludes execution_*.log.
_logger.flush_log_file()
