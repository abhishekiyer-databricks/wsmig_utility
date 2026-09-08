"""
job_templates — load, fill and install the checked-in `jobs/*.job.json` templates (PLAN 7 §E).

The repo is a **Git folder** in the workspace (no DAB, no CLI/terminal), so jobs are packaged as
Jobs API 2.2 JSON definitions + an idempotent installer notebook (`00_Install_Jobs`). This module
is the pure, offline-testable core the notebook drives:

  • `load_template(path)`            — read one `*.job.json`
  • `declared_param_keys(template)`  — the `base_parameters` keys the template's tasks declare
  • `render_template(template, ctx)` — substitute `{{PLACEHOLDER}}` tokens and PROJECT the supplied
                                       config onto ONLY the params each task already declares
  • `create_or_reset(client, rendered)` — POST jobs/create, or jobs/reset when a job of that name
                                       already exists (idempotent, keyed by name)

Design rules baked in:
  • A template's tasks declare their `base_parameters`. The installer fills only the keys a task
    declared — an inventory job never grows a `state_catalog`, an import job never grows
    `content_fetch_workers` — so every job's param page shows exactly what it uses. A key whose
    template default is BLANK is filled from the config; a NON-BLANK default is a deliberate PIN
    (e.g. the airgap job's `connectivity_mode="airgap"`, or an end-to-end job's `dry_run`) and is
    left untouched, so the installer's common config can't silently un-pin it.
  • The source SP SECRET is NEVER written into `base_parameters` when a scope pointer is available
    (job params are visible on the run/job page). The caller passes `spn_secret_value` only if the
    customer insisted on the widget path, and the installer warns.
  • Substitution is whole-token `{{NAME}}` → value, applied to strings anywhere in the template
    (notebook paths, run_as). A leftover `{{...}}` after rendering is a bug and `render_template`
    raises, so a half-filled job can never be created.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

_TOKEN = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def load_template(path: str) -> dict:
    """Read one `*.job.json` template into a dict."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _iter_tasks(template: dict) -> list:
    return template.get("tasks") or []


def declared_param_keys(template: dict) -> set:
    """Every `base_parameters` key declared across the template's tasks.

    This is the projection surface: the installer fills only these keys, so a job's param page shows
    exactly what its tasks consume and nothing else.
    """
    keys: set = set()
    for task in _iter_tasks(template):
        bp = (task.get("notebook_task") or {}).get("base_parameters") or {}
        keys.update(bp.keys())
    return keys


def _substitute_tokens(obj: Any, mapping: dict) -> Any:
    """Recursively replace whole `{{NAME}}` tokens in every string with mapping[NAME]."""
    if isinstance(obj, str):
        def repl(m):
            name = m.group(1)
            if name not in mapping:
                return m.group(0)   # leave unknown tokens; render_template will catch them
            return str(mapping[name])
        return _TOKEN.sub(repl, obj)
    if isinstance(obj, list):
        return [_substitute_tokens(v, mapping) for v in obj]
    if isinstance(obj, dict):
        return {k: _substitute_tokens(v, mapping) for k, v in obj.items()}
    return obj


def _leftover_tokens(obj: Any) -> set:
    found: set = set()
    if isinstance(obj, str):
        found.update(_TOKEN.findall(obj))
    elif isinstance(obj, list):
        for v in obj:
            found |= _leftover_tokens(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= _leftover_tokens(v)
    return found


def render_template(template: dict, *, tokens: dict, params: dict,
                    run_as: dict = None) -> dict:
    """Return a create-ready job spec: tokens substituted + params projected onto declared keys.

    `tokens`  — whole-token replacements (`REPO_PATH`, `RUN_AS_SP`, …) applied to every string.
    `params`  — the full config set; each task keeps ONLY the keys it already declared. A declared
                key whose template default is BLANK is filled from `params`; a NON-BLANK default is a
                deliberate PIN (the airgap job's `connectivity_mode`, an end-to-end job's `dry_run`)
                and is left as-is, so the installer's common config cannot silently un-pin it. A key
                the task didn't declare is never added.
    `run_as`  — optional explicit `run_as` object to set on the job (else the template's own).

    Raises if any `{{...}}` token survives — a half-filled job must never be created.
    """
    spec = copy.deepcopy(template)
    spec = _substitute_tokens(spec, tokens or {})

    for task in _iter_tasks(spec):
        nb = task.get("notebook_task")
        if not nb:
            continue
        declared = dict(nb.get("base_parameters") or {})
        for key in list(declared.keys()):
            # Fill only BLANK defaults; a non-blank template value is a pin the installer respects.
            if str(declared.get(key, "")).strip() == "" and key in (params or {}):
                declared[key] = params[key]
        nb["base_parameters"] = declared

    if run_as is not None:
        spec["run_as"] = run_as

    leftover = _leftover_tokens(spec)
    if leftover:
        raise ValueError(
            f"job template `{spec.get('name')}` still has unfilled placeholders {sorted(leftover)} "
            f"after rendering — refusing to create a half-filled job. Provide them in `tokens`.")
    return spec


def find_job_id_by_name(client, name: str) -> str:
    """The job id of an existing job with this exact `name`, or "" if none. Paginates.

    Idempotency is keyed by name: re-installing resets the SAME job rather than minting a duplicate.
    """
    token = None
    while True:
        params = {"name": name, "limit": 25}
        if token:
            params["page_token"] = token
        doc = client.get("api/2.2/jobs/list", params=params) or {}
        for job in doc.get("jobs", []) or []:
            settings = job.get("settings") or {}
            if str(settings.get("name")) == name:
                return str(job.get("job_id"))
        token = doc.get("next_page_token")
        if not token:
            return ""


def create_or_reset(client, rendered: dict, dry_run: bool = False) -> dict:
    """Create the rendered job, or `jobs/reset` an existing one of the same name (idempotent).

    Returns `{"name", "job_id", "action"}` where action is create/reset/would-create/would-reset.
    """
    name = rendered.get("name")
    if not name:
        raise ValueError("a job template must have a `name`")
    existing_id = find_job_id_by_name(client, name)

    if dry_run:
        return {"name": name, "job_id": existing_id,
                "action": "would-reset" if existing_id else "would-create"}

    if existing_id:
        client.post("api/2.2/jobs/reset", {"job_id": int(existing_id), "new_settings": rendered})
        return {"name": name, "job_id": existing_id, "action": "reset"}
    out = client.post("api/2.2/jobs/create", rendered) or {}
    return {"name": name, "job_id": str(out.get("job_id", "")), "action": "create"}
