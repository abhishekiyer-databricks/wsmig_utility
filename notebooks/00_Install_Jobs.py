# Databricks notebook source
# MAGIC %md
# MAGIC # 00 — Install Jobs (run in the TARGET workspace)
# MAGIC Deploys the checked-in `jobs/*.job.json` definitions as real Databricks Jobs, PRE-FILLED with
# MAGIC the config you enter ONCE below — so the customer never re-types per-run parameters. The repo
# MAGIC is a Git folder (no DAB, no CLI), so this uses the Jobs API 2.2 directly and is **idempotent**:
# MAGIC re-running RESETs a job of the same name rather than creating a duplicate (keyed by name).
# MAGIC
# MAGIC **Two end-to-end jobs** are shipped that differ ONLY in the import task's baked `dry_run`
# MAGIC (`…_dry_run` vs `…_live`), so "rehearse first, run live later" is just "run the dry job, then
# MAGIC the live job" — no parameter to flip at Run-now. Single-task `inventory`/`export`/`import`
# MAGIC and an `airgap_source` (01→02) job are also selectable.
# MAGIC
# MAGIC **Secret handling:** prefer the `source_sp_secret_scope`+`source_sp_secret_key` pointer — the
# MAGIC secret is then NEVER written into `base_parameters` (job params are visible on the run page).
# MAGIC A raw `spn_secret_value` is baked into the jobs ONLY if you set
# MAGIC `allow_secret_in_job_params=true` (an explicit opt-in — the secret is then stored in cleartext
# MAGIC in job params + run history; use the scope path for anything but a throwaway/authorized run).
# MAGIC Supplying a raw secret WITHOUT that opt-in is an error, so a job is never created secret-less.

# COMMAND ----------

# MAGIC %md ## Widgets — fill the FULL config ONCE; it is projected into each selected job

# COMMAND ----------

# Which jobs to deploy (deploy one, some, or all — most customers pick the direct dry+live pair).
dbutils.widgets.multiselect(
    "deploy_jobs", "direct_end_to_end_dry_run",
    ["direct_end_to_end_dry_run", "direct_end_to_end_live", "inventory", "export", "import",
     "airgap_source"],
    "Jobs to deploy (create or reset by name)")

# The identity each created job runs as — a TARGET workspace-admin SP (applicationId).
dbutils.widgets.text("run_as_sp", "", "Run-as SP applicationId (target workspace admin)")

# Common config, projected onto every job (each job keeps only the keys its tasks declare).
dbutils.widgets.dropdown("connectivity_mode", "direct", ["airgap", "direct"], "Connectivity mode")
dbutils.widgets.text("source_workspace_id", "", "Source workspace id")
dbutils.widgets.text("staging_location", "", "Staging location (/Volumes/...)")

# direct-mode source connection. Secret via scope pointer (preferred) OR spn_secret_value.
dbutils.widgets.text("source_workspace_url", "", "[direct] Source workspace URL")
dbutils.widgets.text("source_sp_client_id", "", "[direct] Source SP applicationId (not a secret)")
dbutils.widgets.text("source_sp_secret_scope", "", "[direct] Secret scope for the SP secret")
dbutils.widgets.text("source_sp_secret_key", "", "[direct] Secret key within that scope")
dbutils.widgets.text("spn_secret_value", "", "[direct] SP secret (only baked in if opt-in below)")
# OPT-IN: bake spn_secret_value into the jobs' base_parameters. OFF by default because Job params
# are visible in cleartext on the run/job page and kept in run history — the scope pointer avoids
# that. Turn ON only for your own authorized run / a throwaway smoke test.
dbutils.widgets.dropdown("allow_secret_in_job_params", "false", ["true", "false"],
                         "[direct] Bake spn_secret_value into job params (VISIBLE in cleartext)")

# import-side config (only projected onto jobs that have an import task).
dbutils.widgets.text("state_catalog", "", "State catalog (shared, must exist)")
dbutils.widgets.text("state_schema", "", "State schema (shared, must exist)")
dbutils.widgets.text("account_id", "", "Account id (optional)")

# COMMAND ----------

# MAGIC %md ## Bootstrap `src/` onto sys.path + resolve the repo path

# COMMAND ----------

# MAGIC %pip install -q databricks-sdk requests

# COMMAND ----------

import os
import sys


def _resolve_repo_context():
    """Return (repo_root_on_syspath, repo_workspace_path). The workspace path is what a Job's
    `notebook_task.notebook_path` must point at; the syspath root is where `src/` lives locally."""
    nb_path = None
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_path = ctx.notebookPath().get()   # e.g. /Repos/<user>/<repo>/notebooks/00_Install_Jobs
    except Exception:
        pass

    candidates = []
    repo_ws_path = ""
    if nb_path:
        repo_ws_path = os.path.dirname(os.path.dirname(nb_path))    # .../<repo>
        candidates += [repo_ws_path, "/Workspace" + repo_ws_path]
    here = os.getcwd()
    candidates += [here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))]
    root = None
    for cand in candidates:
        if cand and os.path.isdir(os.path.join(cand, "src")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            root = cand
            break
    if root is None:
        raise RuntimeError("Could not locate the repo root (dir containing `src/`). Tried: "
                           + ", ".join(repr(c) for c in candidates))
    # The Job needs the WORKSPACE path (…/notebooks/xx), not the local fs path. Prefer the notebook
    # context; fall back to the syspath root with the /Workspace prefix stripped for display.
    if not repo_ws_path:
        repo_ws_path = root
    return root, repo_ws_path


_REPO_ROOT, _REPO_WS_PATH = _resolve_repo_context()
print(f"repo root on sys.path : {_REPO_ROOT}")
print(f"repo workspace path   : {_REPO_WS_PATH}  (job notebook_path prefix)")

from src.utils.job_templates import create_or_reset, load_template, render_template

# COMMAND ----------

# MAGIC %md ## Collect config, build the target client

# COMMAND ----------

_w = lambda n, d="": (dbutils.widgets.get(n) or d)   # noqa: E731

_selected = [s.strip() for s in (_w("deploy_jobs") or "").split(",") if s.strip()]
if not _selected:
    raise ValueError("Select at least one job in `deploy_jobs`.")

_run_as_sp = _w("run_as_sp").strip()
if not _run_as_sp:
    raise ValueError("`run_as_sp` is required — the target workspace-admin SP each job runs as.")

# The full config set the installer knows how to project. Each template keeps ONLY the keys its
# tasks declare, so an inventory job never gets state_catalog and an import job never gets
# content_fetch_workers. Blank values are fine — the customer can still edit per job later.
_params = {
    "connectivity_mode": _w("connectivity_mode", "direct"),
    "source_workspace_id": _w("source_workspace_id"),
    "staging_location": _w("staging_location"),
    "run_id": "",
    "source_workspace_url": _w("source_workspace_url"),
    "source_sp_client_id": _w("source_sp_client_id"),
    "source_sp_secret_scope": _w("source_sp_secret_scope"),
    "source_sp_secret_key": _w("source_sp_secret_key"),
    "state_catalog": _w("state_catalog"),
    "state_schema": _w("state_schema"),
    "account_id": _w("account_id"),
}

# Secret handling. The scope pointer is preferred and never puts the secret in a Job param. A raw
# spn_secret_value is baked into base_parameters ONLY when `allow_secret_in_job_params=true` — an
# explicit opt-in, because a Job stores its params in cleartext on the run/job page and run history.
_raw_secret = _w("spn_secret_value").strip()
_have_scope = bool(_w("source_sp_secret_scope") and _w("source_sp_secret_key"))
_allow_secret = _w("allow_secret_in_job_params", "false").strip().lower() == "true"

if _have_scope:
    if _raw_secret:
        print("NOTE: both a scope pointer and spn_secret_value were given — the jobs use the scope "
              "pointer (preferred); spn_secret_value is not baked in.")
elif _raw_secret and _allow_secret:
    # Opt-in: project the raw secret into the jobs. Loud, so it's never a surprise it's in cleartext.
    _params["spn_secret_value"] = _raw_secret
    print("WARNING: baking spn_secret_value into job base_parameters (allow_secret_in_job_params="
          "true). It is stored IN CLEARTEXT on the job/run page and kept in run history. Prefer a "
          "secret scope (source_sp_secret_scope + source_sp_secret_key) for anything but a "
          "throwaway/authorized run.")
elif _raw_secret and not _allow_secret:
    raise ValueError(
        "spn_secret_value was supplied but allow_secret_in_job_params=false, so the installer will "
        "NOT bake a cleartext secret into the jobs (they would be created WITHOUT a usable source "
        "secret and every task would fail validation). Either: (a) put the secret in a Databricks "
        "secret scope and set source_sp_secret_scope + source_sp_secret_key (recommended), or "
        "(b) set allow_secret_in_job_params=true to accept the secret being visible in job params.")
elif not _raw_secret:
    print("NOTE: no source SP secret provided (no scope pointer, no spn_secret_value). This is fine "
          "for AIRGAP jobs; a DIRECT job will fail at run time until you add one — set a scope "
          "pointer, or spn_secret_value + allow_secret_in_job_params=true.")

_tokens = {"REPO_PATH": _REPO_WS_PATH, "RUN_AS_SP": _run_as_sp}
_run_as = {"service_principal_name": _run_as_sp}

# COMMAND ----------

# MAGIC %md ## Deploy the selected jobs (create or reset by name — idempotent)

# COMMAND ----------

# The installer runs entirely in-workspace against the run-as SP's context token. It only needs to
# CREATE jobs, so it uses the ambient TARGET client directly rather than a full Config (which in
# direct mode would demand the source-connection widgets the installer merely writes into the jobs).
from src.auth.token_manager import ApiClient, StaticTokenProvider, resolve_context
_ctx = resolve_context(dbutils=dbutils, spark=spark)
client = ApiClient(_ctx.workspace_url, StaticTokenProvider(_ctx.token))

_results = []
for _name in _selected:
    _path = os.path.join(_REPO_ROOT, "jobs", f"{_name}.job.json")
    _template = load_template(_path)
    _rendered = render_template(_template, tokens=_tokens, params=_params, run_as=_run_as)
    _out = create_or_reset(client, _rendered)
    _results.append(_out)
    print(f"  {_out['action']:<8} {_out['name']:<44} job_id={_out['job_id'] or '(new)'}")

print("\nDone. Open Workflows → Jobs to run them.")
print("Recommended first run: `wsmig - direct end-to-end (DRY RUN)` → read "
      "reports/import_status_dry_run.xlsx → then `wsmig - direct end-to-end (LIVE)`.")
