# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — Inventory (SOURCE side)
# MAGIC Read-only enumeration + identity classification of the **source** workspace. Writes
# MAGIC `inventory.{json,html,xlsx}` + `identity_classification.json` + `config_resolved.json`
# MAGIC into the run's staging bundle. Does **not** mutate anything and does **not** talk to
# MAGIC the target workspace.
# MAGIC
# MAGIC **Runs on the `source` side** (air-gapped model — see `plans/PLAN_0_master.md`).
# MAGIC Run as a Job whose **run-as identity is a workspace-admin SP** on the source workspace.
# MAGIC All config is via widgets (or job params).

# COMMAND ----------

# MAGIC %md ## Widgets
# MAGIC The role is DERIVED from the mode (PLAN 7 §C): `direct` (default) runs this in the TARGET
# MAGIC and reads the source over REST via OAuth M2M; `airgap` runs it INSIDE the source. Same
# MAGIC collectors, different client (master §1a) — no `role` widget.

# COMMAND ----------

dbutils.widgets.dropdown("connectivity_mode", "direct", ["airgap", "direct"], "Connectivity mode")
dbutils.widgets.text("source_workspace_id", "", "Source workspace id")
dbutils.widgets.text("staging_location", "", "Staging location (/Volumes/...)")
# direct-mode source connection. Secret = scope+key (preferred) OR spn_secret_value; scope wins.
dbutils.widgets.text("source_workspace_url", "", "[direct] Source workspace URL")
dbutils.widgets.text("source_sp_client_id", "", "[direct] Source SP applicationId (not a secret)")
dbutils.widgets.text("source_sp_secret_scope", "", "[direct] Secret scope for the SP secret")
dbutils.widgets.text("source_sp_secret_key", "", "[direct] Secret key within that scope")
dbutils.widgets.text("spn_secret_value", "", "[direct] SP secret (only if no scope/key; redacted)")
dbutils.widgets.text("run_id", "", "Run id (blank = auto YYYYMMDD_HHMMSS)")
dbutils.widgets.text("max_scim", "0", "Max SCIM per type (0 = all)")
dbutils.widgets.text("max_workspace_items", "0", "Max workspace items (0 = all)")
dbutils.widgets.text("max_ws_api_calls", "0", "Max workspace/list calls (0 = unlimited)")
dbutils.widgets.dropdown("force_full", "false", ["true", "false"],
                         "Force a fresh snapshot (ignore an incomplete bundle to resume)")

# COMMAND ----------

# MAGIC %md ## Bootstrap `src/` onto sys.path + install requirements

# COMMAND ----------

# MAGIC %pip install -q databricks-sdk openpyxl requests

# COMMAND ----------

import os
import sys


def _add_repo_root_to_syspath() -> str:
    """Find the repo root (the dir containing `src/`) and prepend it to sys.path.

    Tries several strategies because Databricks' cwd varies by runtime/compute:
      1. the notebook's own path from the notebook context (most reliable),
      2. the current working directory and its parents,
      3. common Git-folder mount prefixes.
    """
    candidates = []

    # 1. Derive from this notebook's workspace path via the notebook context.
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_path = ctx.notebookPath().get()  # e.g. /Repos/<user>/<repo>/notebooks/01_Inventory
        repo_dir = os.path.dirname(os.path.dirname(nb_path))  # .../<repo>
        candidates += [repo_dir, "/Workspace" + repo_dir]
    except Exception:
        pass

    # 2. cwd and its parents.
    here = os.getcwd()
    candidates += [here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))]

    for cand in candidates:
        if cand and os.path.isdir(os.path.join(cand, "src")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            return cand
    raise RuntimeError(
        "Could not locate the repo root (dir containing `src/`). Tried: "
        + ", ".join(repr(c) for c in candidates)
        + ". Ensure the whole Git folder (notebooks/ + src/) is connected."
    )


_REPO_ROOT = _add_repo_root_to_syspath()
print(f"repo root on sys.path: {_REPO_ROOT}")

from src.config.config_manager import Config, STAGE_INVENTORY
from src.auth.token_manager import build_clients
from src.exporters import bundle_paths as BP
from src.exporters.artifact_writer import ArtifactWriter
from src.collectors.inventory_runner import InventoryRunner
from src.utils import logger as _logger

# COMMAND ----------

# MAGIC %md ## Build config + client (this workspace only)

# COMMAND ----------

# Role is DERIVED from the stage + mode (PLAN 7 §C): inventory reads the source, so it is
# role=source in airgap (runs inside the source) and role=target in direct (runs in the target and
# reads the source over REST). No `role` widget.
cfg = Config.from_dbutils(dbutils, spark, stage=STAGE_INVENTORY)

# Resume model (Plan 2 §7a): with a blank run_id widget, reuse the newest INCOMPLETE bundle's
# run_id (a whole-job re-run then continues that attempt) rather than minting a new snapshot.
# An explicit run_id widget, or force_full=true, always wins → a fresh snapshot.
from src.exporters.bundle_state import resolve_inventory_run_id
_raw_run_id = (dbutils.widgets.get("run_id") or "").strip()
_force_full = (dbutils.widgets.get("force_full") or "false").strip().lower() == "true"
_resolved_run_id, _how = resolve_inventory_run_id(cfg, _raw_run_id, _force_full)
cfg.run_id = _resolved_run_id

# `build_clients` returns (source_client, target_client). In `airgap` mode both are the same local
# context-token client — "this workspace" IS the source. In `direct` mode the source client is bound
# to source_workspace_url with an OAuth M2M token, and collectors get that one. The collectors
# themselves are unchanged either way: they just take a `client`.
source_client, local_client = build_clients(cfg, dbutils=dbutils, spark=spark)
client = source_client
print(f"Source workspace : {client.base_url}"
      + (f"   (read over REST from {cfg.ctx.workspace_url})" if cfg.is_direct else ""))
print(f"Run id           : {cfg.run_id}  (resolved via: {_how})")
print(f"Staging          : {cfg.output_path}")

# Publish the run_id to a 2-task job's Export task (harmless no-op outside a job). Wrapped so an
# unusual runtime that doesn't expose jobs.taskValues can never break read-only inventory (§2b).
try:
    dbutils.jobs.taskValues.set(key="run_id", value=cfg.run_id)
except Exception as _exc:  # noqa: BLE001
    print(f"(taskValues.set skipped: {_exc})")

# COMMAND ----------

# MAGIC %md ## Run inventory (read-only) → write reports to staging

# COMMAND ----------

aw = ArtifactWriter(cfg, dbutils=dbutils, spark=spark)
# Inventory gets its OWN log file (under misc/, PLAN 7 §D) — it used to share
# `execution_export.log` with 02_Export, so the two runs' records landed in one file.
_logger.set_log_file(os.path.join(aw.ensure_output_path(), BP.EXECUTION_INVENTORY_LOG))

result = InventoryRunner(client, cfg, aw, dbutils=dbutils).run()

print("\n=== Inventory complete ===")
for k, v in sorted(result["counts"].items()):
    print(f"  {k:<22} {v:>6}")
print("\nIdentity classification:", result["identity_summary"])
if result["warnings"]:
    print("\nWarnings:")
    for w in result["warnings"]:
        print("  -", w)
print(f"\nArtifacts: {result['output_path']}")
print("  reports/inventory.xlsx  ·  misc/inventory.json  ·  misc/identity_classification.json")

# Push the last log records to the Volume (the log is appended locally, then mirrored — append
# straight onto a UC Volume silently fails, which used to truncate the log to one line).
_logger.flush_log_file()
