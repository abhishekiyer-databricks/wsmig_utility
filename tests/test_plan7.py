"""Offline tests for PLAN 7 — widget slimming (C), staging layout (D), and job packaging (E).

The behavioural fixes A1/A2 and the output-slimming B2 are covered in test_preflight_and_reports.py
and test_importers_phase2_5.py alongside the code they touch; this file covers the config-build /
role-derivation / staging-fallback surface and the job-template rendering + install flow.
"""
from __future__ import annotations

import glob
import json
import os

import pytest

from src.config.config_manager import (MODE_AIRGAP, MODE_DIRECT, ROLE_SOURCE, ROLE_TARGET,
                                       STAGE_EXPORT, STAGE_IMPORT, STAGE_INVENTORY, Config,
                                       role_for_stage)
from src.utils.job_templates import (create_or_reset, declared_param_keys, find_job_id_by_name,
                                     load_template, render_template)

_JOBS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "jobs")


# ── C: role derivation (no `role` widget any more) ──────────────────────────

def test_role_is_derived_from_stage_and_mode():
    # import is always target
    assert role_for_stage(STAGE_IMPORT, MODE_DIRECT) == ROLE_TARGET
    assert role_for_stage(STAGE_IMPORT, MODE_AIRGAP) == ROLE_TARGET
    # source-reading stages: source in airgap (run inside source), target in direct (run in target)
    for stage in (STAGE_INVENTORY, STAGE_EXPORT):
        assert role_for_stage(stage, MODE_AIRGAP) == ROLE_SOURCE
        assert role_for_stage(stage, MODE_DIRECT) == ROLE_TARGET
    with pytest.raises(ValueError):
        role_for_stage("nonsense", MODE_DIRECT)


class _FakeWidgets:
    def __init__(self, values):
        self._v = dict(values)

    def get(self, name):
        if name not in self._v:
            raise Exception(f"no widget {name}")
        return self._v[name]


class _FakeDbutils:
    def __init__(self, values):
        self.widgets = _FakeWidgets(values)


def test_from_dbutils_derives_role_per_stage_with_no_role_widget():
    """A stage-scoped build must NOT read a `role` widget and must derive the right role."""
    # airgap inventory → role=source, no `role` widget present at all
    cfg = Config.from_dbutils(_FakeDbutils({
        "connectivity_mode": "airgap", "source_workspace_id": "1",
        "staging_location": "/Volumes/a/b/c"}), spark=None, stage=STAGE_INVENTORY)
    assert cfg.role == ROLE_SOURCE and cfg.staging_location == "/Volumes/a/b/c"

    # direct import → role=target (needs the source connection widgets)
    cfg2 = Config.from_dbutils(_FakeDbutils({
        "connectivity_mode": "direct", "source_workspace_id": "1",
        "staging_location": "/Volumes/a/b/c", "source_workspace_url": "https://src",
        "source_sp_client_id": "cid", "spn_secret_value": "s"}),
        spark=None, stage=STAGE_IMPORT)
    assert cfg2.role == ROLE_TARGET


def test_default_connectivity_mode_is_direct():
    """D-2: the widget default is `direct`."""
    cfg = Config.from_dbutils(_FakeDbutils({
        "source_workspace_id": "1", "staging_location": "/Volumes/a/b/c",
        "source_workspace_url": "https://src", "source_sp_client_id": "cid",
        "spn_secret_value": "s"}), spark=None, stage=STAGE_IMPORT)
    assert cfg.connectivity_mode == MODE_DIRECT


# ── C: single staging_location + the old→new upgrade fallback ───────────────

def test_single_staging_location_widget_is_used():
    cfg = Config.from_dbutils(_FakeDbutils({
        "connectivity_mode": "airgap", "source_workspace_id": "1", "run_id": "r",
        "staging_location": "/Volumes/one/loc/here"}), spark=None, stage=STAGE_INVENTORY)
    assert cfg.staging_location == "/Volumes/one/loc/here"


def test_old_staging_widgets_still_resolve_as_an_upgrade_fallback():
    """An in-flight job-param JSON that still carries the OLD two widgets must not break."""
    # airgap source: falls back to source_staging_location
    src = Config.from_dbutils(_FakeDbutils({
        "connectivity_mode": "airgap", "source_workspace_id": "1", "run_id": "r",
        "source_staging_location": "/Volumes/old/src/loc"}), spark=None, stage=STAGE_INVENTORY)
    assert src.staging_location == "/Volumes/old/src/loc"
    # direct: falls back to target_staging_location
    tgt = Config.from_dbutils(_FakeDbutils({
        "connectivity_mode": "direct", "source_workspace_id": "1", "run_id": "r",
        "target_staging_location": "/Volumes/old/tgt/loc", "source_workspace_url": "https://src",
        "source_sp_client_id": "cid", "spn_secret_value": "s"}), spark=None, stage=STAGE_IMPORT)
    assert tgt.staging_location == "/Volumes/old/tgt/loc"


def test_new_staging_location_wins_over_the_old_fallback():
    cfg = Config.from_dbutils(_FakeDbutils({
        "connectivity_mode": "airgap", "source_workspace_id": "1", "run_id": "r",
        "staging_location": "/Volumes/new/loc",
        "source_staging_location": "/Volumes/old/loc"}), spark=None, stage=STAGE_INVENTORY)
    assert cfg.staging_location == "/Volumes/new/loc"


def test_missing_staging_location_fails_fast():
    with pytest.raises(ValueError, match="staging_location"):
        Config.from_dbutils(_FakeDbutils({
            "connectivity_mode": "airgap", "source_workspace_id": "1"}),
            spark=None, stage=STAGE_INVENTORY)


def test_from_dict_accepts_staging_location():
    cfg = Config.from_dict({"role": "target", "source_workspace_id": "1", "run_id": "r",
                            "staging_location": "/Volumes/x/y/z"})
    assert cfg.staging_location == "/Volumes/x/y/z"


# ── E: the job templates are valid and render cleanly ───────────────────────

_EXPECTED_TEMPLATES = {
    "inventory", "export", "import", "airgap_source",
    "direct_end_to_end_dry_run", "direct_end_to_end_live",
}


def test_every_expected_template_exists_and_is_valid_json():
    found = {os.path.basename(p)[:-len(".job.json")] for p in glob.glob(f"{_JOBS_DIR}/*.job.json")}
    assert _EXPECTED_TEMPLATES <= found, f"missing templates: {_EXPECTED_TEMPLATES - found}"
    for name in _EXPECTED_TEMPLATES:
        t = load_template(f"{_JOBS_DIR}/{name}.job.json")
        assert t["name"].startswith("wsmig") and t["tasks"], f"{name} malformed"
        # each task points at a repo-relative notebook and declares base_parameters
        for task in t["tasks"]:
            nb = task["notebook_task"]
            assert nb["notebook_path"].startswith("{{REPO_PATH}}/notebooks/")
            assert isinstance(nb["base_parameters"], dict)


def _render(name, **over):
    t = load_template(f"{_JOBS_DIR}/{name}.job.json")
    params = {"connectivity_mode": "direct", "source_workspace_id": "999",
              "staging_location": "/Volumes/a/b/c", "source_workspace_url": "https://src",
              "source_sp_client_id": "cid", "source_sp_secret_scope": "kv",
              "source_sp_secret_key": "k", "state_catalog": "cat", "state_schema": "sch",
              "content_fetch_workers": "8"}
    params.update(over)
    return render_template(t, tokens={"REPO_PATH": "/Repos/me/wsmig", "RUN_AS_SP": "sp-app-id"},
                           params=params, run_as={"service_principal_name": "sp-app-id"})


def test_render_substitutes_tokens_and_leaves_no_placeholders():
    spec = _render("direct_end_to_end_dry_run")
    blob = json.dumps(spec)
    assert "{{" not in blob, "a placeholder survived rendering"
    assert spec["run_as"]["service_principal_name"] == "sp-app-id"
    for task in spec["tasks"]:
        assert task["notebook_task"]["notebook_path"].startswith("/Repos/me/wsmig/notebooks/")
        # the direct templates leave connectivity_mode BLANK so the installer's config fills it
        assert task["notebook_task"]["base_parameters"]["connectivity_mode"] == "direct"


def test_params_are_projected_only_onto_declared_keys():
    """An inventory task must not grow state_catalog; an import task must not grow
    content_fetch_workers — each task keeps only the keys it declared."""
    inv = load_template(f"{_JOBS_DIR}/inventory.job.json")
    assert "state_catalog" not in declared_param_keys(inv)
    assert "content_fetch_workers" not in declared_param_keys(inv)

    imp = load_template(f"{_JOBS_DIR}/import.job.json")
    assert "state_catalog" in declared_param_keys(imp)
    assert "content_fetch_workers" not in declared_param_keys(imp)

    # after rendering, the inventory job's task has staging_location filled but no state_catalog key
    spec = _render("inventory")
    bp = spec["tasks"][0]["notebook_task"]["base_parameters"]
    assert bp["staging_location"] == "/Volumes/a/b/c" and "state_catalog" not in bp


def test_dry_and_live_end_to_end_differ_only_in_the_import_dry_run():
    dry = _render("direct_end_to_end_dry_run")
    live = _render("direct_end_to_end_live")
    dry_imp = next(t for t in dry["tasks"] if t["task_key"] == "import")
    live_imp = next(t for t in live["tasks"] if t["task_key"] == "import")
    assert dry_imp["notebook_task"]["base_parameters"]["dry_run"] == "true"
    assert live_imp["notebook_task"]["base_parameters"]["dry_run"] == "false"
    # the graphs are otherwise identical (same task keys + dependency edges)
    assert [t["task_key"] for t in dry["tasks"]] == [t["task_key"] for t in live["tasks"]]


def test_secret_scope_pointer_is_persisted_and_spn_secret_value_stays_blank_by_default():
    """The scope pointer is safe to bake in. `spn_secret_value` is a declared key now, but with the
    default config (no raw secret projected) it must stay BLANK — the scope path leaks nothing."""
    spec = _render("direct_end_to_end_live")   # _render passes scope keys, no spn_secret_value
    blob = json.dumps(spec)
    assert "kv" in blob and "\"source_sp_secret_scope\"" in blob   # pointer kept
    for task in spec["tasks"]:
        bp = task["notebook_task"]["base_parameters"]
        # declared (so a raw-secret opt-in CAN fill it), but empty here
        assert bp.get("spn_secret_value", "") == "", "a raw secret must not leak by default"


def test_raw_secret_is_baked_in_only_when_explicitly_opted_in():
    """The installer's opt-in path: passing spn_secret_value in params (which the notebook does only
    when allow_secret_in_job_params=true) fills the declared key on every source-reading task."""
    spec = _render("direct_end_to_end_dry_run", source_sp_secret_scope="", source_sp_secret_key="",
                   spn_secret_value="RAW-SECRET-XYZ")
    for task in spec["tasks"]:
        bp = task["notebook_task"]["base_parameters"]
        # every task that reads the source now carries the raw secret
        if "source_sp_client_id" in bp:
            assert bp["spn_secret_value"] == "RAW-SECRET-XYZ"


def test_airgap_job_pins_connectivity_mode_even_when_config_says_direct():
    """A non-blank template default is a PIN: deploying airgap_source with the installer's default
    `direct` config must NOT flip the job to direct."""
    spec = _render("airgap_source", connectivity_mode="direct")
    for task in spec["tasks"]:
        assert task["notebook_task"]["base_parameters"]["connectivity_mode"] == "airgap", \
            "the airgap job's pinned connectivity_mode was overwritten by the common config"


def test_a_leftover_placeholder_refuses_to_render():
    t = load_template(f"{_JOBS_DIR}/inventory.job.json")
    with pytest.raises(ValueError, match="unfilled placeholders"):
        # omit RUN_AS_SP so {{RUN_AS_SP}} in run_as survives
        render_template(t, tokens={"REPO_PATH": "/Repos/me/wsmig"}, params={})


# ── E: create-or-reset is idempotent (keyed by name) ────────────────────────

class _FakeJobsClient:
    def __init__(self, existing=None):
        self._jobs = list(existing or [])   # list of {"job_id", "settings": {"name"}}
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if "jobs/list" in path:
            name = (params or {}).get("name")
            jobs = [j for j in self._jobs if j["settings"]["name"] == name]
            return {"jobs": jobs}
        return {}

    def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        if path.endswith("jobs/create"):
            jid = str(1000 + len(self._jobs))
            self._jobs.append({"job_id": jid, "settings": {"name": body["name"]}})
            return {"job_id": jid}
        if path.endswith("jobs/reset"):
            return {}
        return {}


def test_create_then_reset_is_idempotent_by_name():
    client = _FakeJobsClient()
    spec = _render("inventory")
    first = create_or_reset(client, spec)
    assert first["action"] == "create" and first["job_id"]
    # a second install of the SAME name resets rather than duplicating
    second = create_or_reset(client, spec)
    assert second["action"] == "reset" and second["job_id"] == first["job_id"]
    assert len(client._jobs) == 1, "a duplicate job was created — install is not idempotent"
    resets = [c for c in client.calls if c[0] == "POST" and c[1].endswith("jobs/reset")]
    assert resets and resets[0][2]["job_id"] == int(first["job_id"])


def test_find_job_id_paginates():
    pages = [
        {"jobs": [{"job_id": "1", "settings": {"name": "other"}}], "next_page_token": "t2"},
        {"jobs": [{"job_id": "2", "settings": {"name": "wsmig - inventory"}}]},
    ]

    class Paged:
        def __init__(self):
            self.i = 0

        def get(self, path, params=None):
            page = pages[self.i]
            self.i += 1
            return page

    assert find_job_id_by_name(Paged(), "wsmig - inventory") == "2"
