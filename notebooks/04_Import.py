# Databricks notebook source
# MAGIC %md
# MAGIC # 04 — Import (TARGET side)
# MAGIC Creates assets on the TARGET workspace from a verified bundle, in dependency order:
# MAGIC identity → compute → workspace → secrets → jobs → SQL → DLT → dashboards → genie → serving
# MAGIC → misc → **ACLs last** (a grant needs both id maps, so it can only run at the end).
# MAGIC
# MAGIC **Dry run is the default.** `dry_run=true` is a full rehearsal: real reads, real bundle, real
# MAGIC create/update/skip decisions, and **zero writes** to the target. Run it that way first.
# MAGIC
# MAGIC **Every unit is fail-soft.** One asset's failure — for any reason — is recorded with its
# MAGIC reason and the run continues. Only four things stop the run, all *before* any unit is
# MAGIC attempted: a bad bundle manifest, a preflight NO-GO, an unreachable state schema when live,
# MAGIC and (in `direct` mode) a source client that cannot authenticate. Each is a case where
# MAGIC continuing would give a *wrong* target rather than an incomplete one.
# MAGIC
# MAGIC **Re-runs are expected and safe.** Every asset is UPSERTed against the Delta migration state
# MAGIC table, so a second run creates what's new, updates what changed on source, and skips what
# MAGIC didn't. See `plans/PLAN_3_import.md`.

# COMMAND ----------

# MAGIC %md ## Widgets
# MAGIC The role is DERIVED (import always runs in the target) — there is no `role` widget. In
# MAGIC `direct` mode (default) inventory/export also ran here; `airgap` expects an uploaded bundle.

# COMMAND ----------

dbutils.widgets.dropdown("connectivity_mode", "direct", ["airgap", "direct"],
                         "Connectivity mode")
dbutils.widgets.text("source_workspace_id", "", "Source workspace id (identifies the bundle)")
dbutils.widgets.text("staging_location", "", "Staging location (UC Volume /Volumes/...)")
dbutils.widgets.text("run_id", "", "Run id (blank = resume, else LATEST_EXPORT.json)")

# Dry run FIRST. Flip to false only once the rehearsal reads clean.
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"],
                         "Dry run (true = decide everything, write nothing)")

# The migration state table: ONE shared catalog+schema for every workspace pair, both assumed to
# already exist. The tool owns the table NAMES. Required when dry_run=false.
dbutils.widgets.text("state_catalog", "", "State catalog (shared, must exist)")
dbutils.widgets.text("state_schema", "", "State schema (shared, must exist)")

# This session's work list over what the bundle contains. `acls` is independently selectable
# because ACL replay is the pass most likely to need a second attempt.
dbutils.widgets.multiselect("import_assets", "all",
                            ["all", "identity", "compute", "workspace", "secrets", "jobs", "sql",
                             "dlt", "dashboards", "genie", "serving", "misc", "acls"],
                            "Asset families to import THIS run")

# Narrow the run to outstanding units after fixing a prerequisite. One dropdown, not booleans, so an
# invalid combination cannot be set.
dbutils.widgets.dropdown("retry_mode", "off",
                         ["off", "failed_only", "skipped_only", "failed_and_skipped"],
                         "Retry mode (narrows the work list only)")

dbutils.widgets.dropdown("preflight_enforce", "true", ["true", "false"],
                         "Fail the run on a preflight NO-GO")
dbutils.widgets.dropdown("force_full_import", "false", ["true", "false"],
                         "Ignore the checkpoint and re-evaluate every unit")
dbutils.widgets.dropdown("allow_deletes", "false", ["true", "false"],
                         "Allow deleting target objects removed from source (default: report only)")
dbutils.widgets.dropdown("library_force_start_clusters", "false", ["true", "false"],
                         "Start stopped clusters to install libraries (consumes DBUs)")
dbutils.widgets.text("account_id", "", "Account id (optional; enables account-level checks)")

# PLAN 9: an orphaned home is content under `/Users/<owner>` whose owner was DELETED in source (so
# it is absent from the roster and never created on target). Rather than failing it as a
# prerequisite, divert it to a top-level backup folder, preserving the sub-tree, so no bytes are
# lost and an operator can reassign them. Flip to false to restore the prerequisite behaviour.
dbutils.widgets.dropdown("workspace_home_backup", "true", ["true", "false"],
                         "Back up orphaned (deleted-in-source) home content instead of failing it")
dbutils.widgets.text("workspace_home_backup_root", "/Users_Backup",
                     "Top-level folder for orphaned home backups")
# Cell output level (the driver log — Compute → Driver logs → Standard error — is ALWAYS DEBUG).
dbutils.widgets.dropdown("log_level", "INFO", ["DEBUG", "INFO", "WARNING", "ERROR"],
                         "Cell log level (driver log is always DEBUG)")

# `direct`-mode only — how to reach the SOURCE. The secret is EITHER a scope pointer (preferred:
# a widget value is visible on the run page and kept in run history) OR spn_secret_value.
dbutils.widgets.text("source_workspace_url", "", "[direct] Source workspace URL")
dbutils.widgets.text("source_sp_client_id", "", "[direct] Source SP applicationId (not a secret)")
dbutils.widgets.text("source_sp_secret_scope", "", "[direct] Secret scope holding the SP secret")
dbutils.widgets.text("source_sp_secret_key", "", "[direct] Secret key within that scope")
dbutils.widgets.text("spn_secret_value", "", "[direct] SP secret (only if no scope/key; redacted)")

# NOTE: no Azure Key Vault / AAD widgets. An AKV-backed secret scope cannot be created from this
# environment — it needs an Azure AD token that a Databricks SPN credential / managed-identity-backed
# SPN cannot provide from a private, notebook-only workspace (IMP-4, proven live). AKV-backed scopes
# are therefore always reported as a clean manual step; Databricks-backed scopes migrate normally.

# Transform options. NOTE: the per-asset `migrate_*` toggles are NOT on import — they are bundle
# scope, set on the SOURCE side (01/02). Here `import_assets` is the work-list selector instead.
dbutils.widgets.dropdown("pause_job_schedules", "true", ["true", "false"],
                         "Pause imported job schedules AND continuous triggers")
dbutils.widgets.text("user_domain_mapping", "", "old.com=new.com,...")
dbutils.widgets.text("user_id_mapping", "", "old@a.com=new@b.com,...")

# COMMAND ----------

# MAGIC %md ## Bootstrap `src/` onto sys.path + install requirements

# COMMAND ----------

# MAGIC %pip install -q databricks-sdk openpyxl requests

# COMMAND ----------

import os
import sys


def _add_repo_root_to_syspath() -> str:
    """Find the repo root (the dir containing `src/`) and prepend it to sys.path."""
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

from src.auth.token_manager import build_clients
from src.config.config_manager import ROLE_TARGET, STAGE_IMPORT, Config
from src.exporters import bundle_paths as BP
from src.exporters.artifact_writer import ArtifactWriter, read_json_or_empty
from src.importers.import_runner import (RUN_STATUS_STATE_NOT_SAVED, ImportRunner,
                                         resolve_import_run_id)
from src.state.sql_backend import build_sql_backend
from src.state.state_store import StateStore
from src.utils import logger as _logger

# Every line below goes to BOTH outputs: the cell (at `log_level`) and the driver log (always DEBUG).
_say = _logger.get_logger("notebook").info
_say(f"repo root on sys.path: {_REPO_ROOT}")

# COMMAND ----------

# MAGIC %md ## Config + which bundle to import
# MAGIC Precedence: explicit `run_id` → resume an incomplete import → `LATEST_EXPORT.json` → fail
# MAGIC loudly. A run_id is never invented: that would import an empty bundle and report a
# MAGIC spuriously clean run.

# COMMAND ----------

cfg = Config.from_dbutils(dbutils, spark, stage=STAGE_IMPORT)
assert cfg.role == ROLE_TARGET, f"04_Import must run with role=target (got {cfg.role!r})"

# In a multi-task Job, `01_Inventory` publishes the run_id so every later task acts on one bundle.
_widget_run_id = (dbutils.widgets.get("run_id") or "").strip()
if not _widget_run_id:
    try:
        _tv = dbutils.jobs.taskValues.get(taskKey="inventory", key="run_id", debugValue="")
        if _tv:
            _widget_run_id = str(_tv).strip()
            _say(f"run_id taken from the inventory task's values: {_widget_run_id}")
    except Exception as _exc:  # expected: not a multi-task job → resume / the pointer
        _logger.get_logger("notebook").debug(f"no inventory task value for run_id: {_exc}")

_run_id, _how = resolve_import_run_id(cfg, _widget_run_id)
cfg.run_id = _run_id

source_client, client = build_clients(cfg, dbutils=dbutils, spark=spark)

_say(f"Target workspace : {cfg.ctx.workspace_url}")
_say(f"Source ws id     : {cfg.source_workspace_id}")
_say(f"Run id           : {cfg.run_id}   (resolved via: {_how})")
_say(f"Bundle           : {cfg.output_path}")
_say(f"Connectivity     : {cfg.connectivity_mode}")
_say(f"Mode             : {'DRY RUN — nothing will be written' if cfg.dry_run else 'LIVE'}")
_say(f"Families         : {', '.join(cfg.imports.selected_families)}")
_say(f"Retry mode       : {cfg.imports.retry_mode}")
_say(f"Home backup      : {'ON → ' + cfg.imports.workspace_home_backup_root if cfg.imports.workspace_home_backup else 'OFF (orphaned homes fail as prerequisite)'}")

# COMMAND ----------

# MAGIC %md ## Bundle summary (what is about to be imported)

# COMMAND ----------

aw = ArtifactWriter(cfg, dbutils=dbutils, spark=spark)
aw.ensure_output_path()
# PLAN 16.1: the log is the run output itself — the cell (log_level) + the driver log (always DEBUG).
# Configured HERE so the bundle summary, state, preflight and the run all log through it. No log file.
_logger.configure_logging(run_id=cfg.run_id, stage="IMPORT",
                          level=dbutils.widgets.get("log_level") or "INFO")

_index = read_json_or_empty(aw, BP.EXPORT_INDEX_JSON, "an empty unit summary")
_bundle_cfg = read_json_or_empty(aw, BP.CONFIG_RESOLVED_JSON, "unknown bundle provenance")
_say(f"Bundle produced in `{_bundle_cfg.get('connectivity_mode', '?')}` mode, "
     f"tool version {_index.get('tool_version', '?')}, at {_index.get('generated_utc', '?')}")
_say(f"{len(_index.get('units', []))} units in the bundle:")
for _at, _counts in sorted((_index.get("counts") or {}).items()):
    _say(f"  {_at:<24} {_counts}")

# COMMAND ----------

# MAGIC %md ## Migration state table
# MAGIC One shared catalog+schema across all workspace pairs, keyed by `source_workspace_id`. The
# MAGIC tool owns the table names; the catalog/schema must already exist.

# COMMAND ----------

state = None
if cfg.state_enabled:
    _backend = build_sql_backend(cfg, spark=spark, client=client)
    state = StateStore(_backend, cfg)
    state.ensure_table()
    state.load(force=True)
    _say(f"State table : {cfg.state_table_fqn}")
    _say(f"Identity map: {cfg.identity_map_table_fqn}")
    _say(f"Existing rows for this workspace pair: {len(state._cache)}")
    if state._cache:
        _say("Where this pair is up to:")
        for _action, _n in sorted(state.summary().items(), key=lambda kv: -kv[1]):
            _say(f"  {_action:<22} {_n}")
else:
    _say("State store DISABLED (dry run with no state_catalog) — a first-look rehearsal needs no "
         "UC setup. A LIVE import requires state_catalog + state_schema.")

# COMMAND ----------

# MAGIC %md ## Preflight gate (verify only — creates nothing)

# COMMAND ----------

from src.importers.preflight import Preflight

_pf = Preflight(client, cfg, aw, state=state, dbutils=dbutils,
                source_client=source_client if cfg.is_direct else None).run()

_say(f"=== PREFLIGHT: {_pf['verdict']} ===")
for _grade, _key in (("BLOCKING", "blocking"), ("DEGRADING", "degrading"),
                     ("COSMETIC", "cosmetic")):
    # One line per finding — capped, because a missing-identity check lists one item per identity.
    _items = _pf.get(_key) or []
    for _item in _items[:200]:
        _say(f"  [{_grade}] {_item}")
    if len(_items) > 200:
        _say(f"  … {len(_items) - 200} more {_grade} items in misc/preflight_report.json")
_say(f"Graded verdict printed above; misc/preflight_report.json written to the bundle. "
     f"{'Import will NOT run.' if _pf['verdict'] == 'NO-GO' and cfg.imports.preflight_enforce else ''}")

# COMMAND ----------

# MAGIC %md ## Run the import

# COMMAND ----------

runner = ImportRunner(client, cfg, aw, state=state, dbutils=dbutils, preflight_verdict=_pf)
with _logger.live_run("IMPORT"):
    result = runner.run()

_say(f"=== IMPORT {result['run_status'].upper()} "
     f"({'DRY RUN' if cfg.dry_run else 'LIVE'}) in {result['elapsed_sec']}s ===")
_totals = result.get("totals", {})
for _k in ("total", "created", "updated", "adopted", "skipped", "created_with_warning",
           "manual", "not_selected", "skipped_no_object", "failed"):
    _say(f"  {_k:<22} {_totals.get(_k, 0):>6}")

_say("Per phase:")
for _phase in result.get("per_phase", []):
    _say(f"  {_phase['component']:<12} total={_phase['total']:<5} created={_phase['created']:<5} "
         f"updated={_phase['updated']:<4} skipped={_phase['skipped']:<5} "
         f"failed={_phase['failed']:<4} manual={_phase['manual']:<4} "
         f"({_phase['elapsed_sec']}s)")

# COMMAND ----------

# MAGIC %md ## Failures, warnings and manual actions
# MAGIC A failure here is a per-unit outcome, not a broken run. Fix the cause, then re-run this
# MAGIC notebook with `retry_mode=failed_only` to attempt exactly those units.

# COMMAND ----------

# Read back the results file THIS run wrote — a live retry tags it `_retry_<ts>`, a dry run keeps
# the canonical name; the runner records the actual path under summary["reports"].
_results_rel = (result.get("reports") or {}).get("json") or BP.IMPORT_RESULTS_JSON
_results = read_json_or_empty(aw, _results_rel, "no per-unit rows to summarise")
_units = _results.get("units", [])

_failed = [u for u in _units if u.get("import_status") == "failed"]
_warned = [u for u in _units if u.get("import_status") == "created_with_warning"]
_manual = [u for u in _units if u.get("import_status") == "manual"]
_no_obj = [u for u in _units if u.get("import_status") == "skipped_no_object"]

_say(f"=== FAILURES ({len(_failed)}) — fix, then retry_mode=failed_only ===")
for _u in _failed[:40]:
    _say(f"  [{_u.get('failure_category')}] {_u['asset_type']}/{_u['natural_key']}")
    _say(f"      {_u.get('note')}")

_say(f"=== CREATED BUT DEGRADED ({len(_warned)}) — they exist, but verify before use ===")
for _u in _warned[:25]:
    _say(f"  {_u['asset_type']}/{_u['natural_key']}: {str(_u.get('note'))[:180]}")

# PLAN 9: orphaned-home diversions are created_with_warning rows whose note names the backup path.
_home_backups = [u for u in _warned if "deleted in source" in str(u.get("note", ""))]
if _home_backups:
    _say(f"=== HOME BACKUPS ({len(_home_backups)}) — orphaned (deleted-in-source) content "
         f"preserved under {cfg.imports.workspace_home_backup_root} ===")
    for _u in _home_backups[:25]:
        _say(f"  {_u['natural_key']}  →  {_u.get('target_id')}")

_say(f"=== MANUAL STEPS ({len(_manual)}) ===")
for _u in _manual[:25]:
    _say(f"  {_u['asset_type']}/{_u['natural_key']}: {str(_u.get('note'))[:160]}")

_say(f"=== PERMISSIONS PENDING AN OBJECT ({len(_no_obj)}) — normal for DAB/repos ===")
for _u in _no_obj[:15]:
    _say(f"  [{_u.get('failure_category')}] {_u['natural_key']}")

# COMMAND ----------

# MAGIC %md ## ACL parity — the report to read after an import
# MAGIC Source-vs-target permission diff, proven by re-reading every object we touched rather than
# MAGIC assumed. `inherited` grants are dropped on BOTH sides so like is compared with like. The
# MAGIC full table is folded into the **ACL Parity** sheet of the import workbook (PLAN 7 §B2/D-1);
# MAGIC this is the inline summary.

# COMMAND ----------

_parity = runner.context.get("acl_parity") or {}
if _parity:
    _say(f"objects re-read and diffed: {_parity.get('objects_checked', 0)}")
    for _verdict, _n in sorted((_parity.get("counts") or {}).items(), key=lambda kv: -kv[1]):
        _say(f"  {_verdict:<20} {_n}")
    _bad = [o for o in _parity.get("objects", []) if o.get("verdict") != "match"]
    for _o in _bad[:25]:
        _say(f"  {_o.get('verdict')}: {_o.get('perm_object_type')}/{_o.get('object')} "
             f"missing={_o.get('missing_on_target')} extra={_o.get('extra_on_target')}")
    _say(f"NOTE: {_parity.get('known_limitation', '')}")
    _say("Full parity table: the 'ACL Parity' sheet of import_status.xlsx.")
else:
    _say("no ACL parity (the acls family was not selected this run)")

# COMMAND ----------

# MAGIC %md ## Artifacts + what to do next

# COMMAND ----------

# List the files THIS run actually wrote (dry → *_dry_run; live retry → *_retry_<ts>; else canonical).
_reports = result.get("reports") or {}
_status_xlsx = _reports.get("xlsx") or (BP.IMPORT_STATUS_DRYRUN_XLSX if cfg.dry_run
                                        else BP.IMPORT_STATUS_XLSX)
_say(f"Bundle: {aw.root}\n")
for _name in (_reports.get("json") or BP.IMPORT_RESULTS_JSON, _status_xlsx,
              _reports.get("manual_actions") or BP.MANUAL_ACTIONS_IMPORT_MD,
              BP.PREFLIGHT_REPORT_JSON):
    _p = os.path.join(aw.root, _name)
    _say(f"  {'✓' if os.path.exists(_p) else '·'} {_name}")

_say("Next steps:")
if cfg.dry_run:
    _say(f"  1. Read {_status_xlsx} — every unit's intended action, failures first.")
    _say("  2. Fix anything BLOCKING in the preflight verdict above.")
    _say("  3. Re-run with dry_run=false (state_catalog + state_schema are then required).")
else:
    _say(f"  1. Read {_status_xlsx} — the 'ACL Parity' sheet is the source-vs-target diff.")
    _say("  2. Work through manual_actions_import.md (secret values, Git repos, legacy dashboards).")
    _say("  3. Re-run with retry_mode=failed_only once prerequisites are fixed.")
    _say("  4. Re-run with import_assets=acls + retry_mode=skipped_only after a DAB redeploy.")
_say(_logger.FULL_LOG_HINT)

# COMMAND ----------

# MAGIC %md ## Fail the job if the migration state was not saved
# MAGIC Every phase ran, but the final MERGE into the state table failed: the target WAS changed and
# MAGIC the source→target id map was not persisted. The job must go red; the next run's recovery
# MAGIC replay re-merges the rows from the checkpoint once the state table is fixed.

# COMMAND ----------

if result.get("run_status") == RUN_STATUS_STATE_NOT_SAVED:
    raise RuntimeError(
        "Import finished but the migration STATE WAS NOT SAVED (run_status="
        f"{RUN_STATUS_STATE_NOT_SAVED}). The target was changed; the source→target id map is in the "
        "checkpoint only. Fix the state table (permissions / cluster), then re-run — the recovery "
        "replay re-merges the rows. See the driver log (stderr) for the flush error.")
