"""Offline tests for the dual-mode config + auth work (Plan 3 §2, §2a).

The load-bearing behaviours here are (a) the per-mode validation that stops a mis-wired run before
it does anything, (b) the explicit secret precedence so there is never doubt which credential a
run used, and (c) REDACTION — the widget-supplied SP secret must appear in no artifact or log.
"""
from __future__ import annotations

import json

import pytest

from src.config.config_manager import (MODE_AIRGAP, MODE_DIRECT, ROLE_SOURCE, ROLE_TARGET,
                                       Config, ImportOptions)


class FakeWidgets:
    def __init__(self, values):
        self._v = dict(values)

    def get(self, name):
        if name not in self._v:
            raise Exception(f"no widget {name}")
        return self._v[name]


class FakeSecrets:
    def __init__(self, table):
        self._t = table

    def get(self, scope=None, key=None):
        return self._t[(scope, key)]


class FakeDbutils:
    def __init__(self, widgets=None, secrets=None):
        self.widgets = FakeWidgets(widgets or {})
        self.secrets = FakeSecrets(secrets or {})


_DIRECT_WIDGETS = {
    "role": "target",
    "connectivity_mode": "direct",
    "source_workspace_id": "1234567890123456",
    "target_staging_location": "/Volumes/cat/sch/vol",
    "source_workspace_url": "https://adb-1234567890123456.18.azuredatabricks.net",
    "source_sp_client_id": "abc-123-client",
    "run_id": "20260805_120000",
    "dry_run": "true",
}


# ── mode routing ───────────────────────────────────────────────────────────

def test_direct_mode_uses_target_staging_for_both_halves():
    """In `direct` mode there is no handoff: BOTH halves read/write target_staging_location, so
    `source_staging_location` must be ignored even if set."""
    cfg = Config.from_dbutils(
        FakeDbutils({**_DIRECT_WIDGETS, "spn_secret_value": "s3cr3t",
                     "source_staging_location": "/Volumes/ignored/me/now"}), spark=None)
    assert cfg.is_direct
    assert cfg.staging_location == "/Volumes/cat/sch/vol"
    assert cfg.output_path == "/Volumes/cat/sch/vol/wsmig/1234567890123456/20260805_120000"


def test_airgap_staging_follows_the_role():
    src = Config.from_dict({"role": ROLE_SOURCE, "source_workspace_id": "1", "run_id": "r",
                            "source_staging_location": "/Volumes/s/x/y",
                            "target_staging_location": "/Volumes/t/x/y"})
    tgt = Config.from_dict({"role": ROLE_TARGET, "source_workspace_id": "1", "run_id": "r",
                            "source_staging_location": "/Volumes/s/x/y",
                            "target_staging_location": "/Volumes/t/x/y"})
    assert src.staging_location == "/Volumes/s/x/y"
    assert tgt.staging_location == "/Volumes/t/x/y"
    # Same bundle path shape on both sides — that's what makes the handoff a plain file copy.
    assert src.output_path.endswith("/wsmig/1/r") and tgt.output_path.endswith("/wsmig/1/r")


# ── per-mode validation (fail fast, before anything happens) ───────────────

def test_direct_mode_requires_source_url_client_id_and_a_secret():
    for missing, expect in (("source_workspace_url", "source_workspace_url"),
                            ("source_sp_client_id", "source_sp_client_id")):
        w = {**_DIRECT_WIDGETS, "spn_secret_value": "s"}
        w[missing] = ""
        with pytest.raises(ValueError, match=expect):
            Config.from_dbutils(FakeDbutils(w), spark=None)
    # neither secret path configured
    with pytest.raises(ValueError, match="spn_secret_value"):
        Config.from_dbutils(FakeDbutils(_DIRECT_WIDGETS), spark=None)


def test_direct_mode_rejects_role_source():
    """`direct` runs everything in the TARGET, so role=source is a mis-wiring, not a variant."""
    with pytest.raises(ValueError, match="role must be 'target'"):
        Config.from_dbutils(
            FakeDbutils({**_DIRECT_WIDGETS, "role": "source", "spn_secret_value": "s"}),
            spark=None)


def test_live_import_requires_the_state_catalog_and_schema():
    """dry_run=false with no state table is a correctness hazard (no durable source→target id map
    ⇒ the next run cannot tell CREATE from UPDATE and may duplicate), so it must fail fast."""
    w = {"role": "target", "connectivity_mode": "airgap", "source_workspace_id": "1",
         "staging_location": "/Volumes/a/b/c", "dry_run": "false"}
    with pytest.raises(ValueError, match="state_catalog"):
        Config.from_dbutils(FakeDbutils(w), spark=None)
    # with them supplied it validates, and the FQNs resolve to the tool-owned table names
    cfg = Config.from_dbutils(FakeDbutils({**w, "state_catalog": "cat",
                                          "state_schema": "sch"}), spark=None)
    assert cfg.state_table_fqn == "cat.sch.wsmig_migration_state"
    assert cfg.identity_map_table_fqn == "cat.sch.wsmig_identity_map"


def test_dry_run_uses_a_separate_state_table():
    """A rehearsal must never pollute the real source→target map — hence a separate table rather
    than a dry_run column (and `DROP TABLE` is then a one-liner)."""
    cfg = Config.from_dbutils(FakeDbutils({
        "role": "target", "connectivity_mode": "airgap", "source_workspace_id": "1",
        "staging_location": "/Volumes/a/b/c",
        "dry_run": "true", "state_catalog": "cat", "state_schema": "sch"}), spark=None)
    assert cfg.state_table_fqn == "cat.sch.wsmig_migration_state_dryrun"


def test_bad_retry_mode_and_bad_family_are_rejected():
    base = {"role": "target", "connectivity_mode": "airgap", "source_workspace_id": "1",
            "staging_location": "/Volumes/a/b/c"}
    with pytest.raises(ValueError, match="retry_mode"):
        Config.from_dbutils(FakeDbutils({**base, "retry_mode": "sometimes"}), spark=None)
    with pytest.raises(ValueError, match="import_assets"):
        Config.from_dbutils(FakeDbutils({**base, "import_assets": "identity,nonsense"}),
                            spark=None)


def test_no_state_needed_for_a_first_look_dry_run():
    """A first-look rehearsal must need no UC setup at all."""
    cfg = Config.from_dbutils(FakeDbutils({
        "role": "target", "connectivity_mode": "airgap", "source_workspace_id": "1",
        "staging_location": "/Volumes/a/b/c", "dry_run": "true"}), spark=None)
    assert cfg.state_enabled is False


# ── secret precedence (§2a) ────────────────────────────────────────────────

def test_secret_scope_wins_when_both_are_set():
    """Explicit precedence: scope+key beats the widget, so a customer who has set up a scope can
    leave a stale `spn_secret_value` behind with no surprise about which one was used."""
    cfg = Config.from_dbutils(FakeDbutils(
        {**_DIRECT_WIDGETS, "source_sp_secret_scope": "kv", "source_sp_secret_key": "sp-secret",
         "spn_secret_value": "STALE-WIDGET-VALUE"},
        secrets={("kv", "sp-secret"): "FROM-SCOPE"}), spark=None)
    dbu = FakeDbutils(secrets={("kv", "sp-secret"): "FROM-SCOPE"})
    assert cfg.resolve_source_secret(dbu) == "FROM-SCOPE"
    assert cfg.source.uses_secret_scope is True


def test_widget_secret_used_only_when_the_pair_is_empty():
    cfg = Config.from_dbutils(FakeDbutils({**_DIRECT_WIDGETS,
                                           "spn_secret_value": "FROM-WIDGET"}), spark=None)
    assert cfg.resolve_source_secret(FakeDbutils()) == "FROM-WIDGET"
    assert cfg.source.uses_secret_scope is False


def test_a_half_configured_scope_falls_through_to_the_widget():
    """scope without key (or vice versa) is not a usable pointer — it must not silently produce a
    lookup with a blank key, it falls through to the widget path."""
    cfg = Config.from_dbutils(FakeDbutils({**_DIRECT_WIDGETS,
                                           "source_sp_secret_scope": "kv",
                                           "spn_secret_value": "FROM-WIDGET"}), spark=None)
    assert cfg.source.uses_secret_scope is False
    assert cfg.resolve_source_secret(FakeDbutils()) == "FROM-WIDGET"


# ── REDACTION — the secret must reach no artifact or log ───────────────────

def test_redacted_strips_the_spn_secret_and_the_context_token():
    cfg = Config.from_dbutils(FakeDbutils({**_DIRECT_WIDGETS,
                                           "spn_secret_value": "SUPER-SECRET-LITERAL"}),
                              spark=None)
    cfg.ctx.token = "TOKEN-LITERAL"
    blob = json.dumps(cfg.redacted())
    assert "SUPER-SECRET-LITERAL" not in blob, "the SP secret leaked into config_resolved.json"
    assert "TOKEN-LITERAL" not in blob, "the context token leaked into config_resolved.json"
    # The non-secret pointers ARE kept — they document which credential a run used.
    assert "abc-123-client" in blob
    assert cfg.redacted()["source"]["secret_source"] == "widget"


def test_redacted_records_which_secret_path_was_used():
    scoped = Config.from_dbutils(FakeDbutils(
        {**_DIRECT_WIDGETS, "source_sp_secret_scope": "kv", "source_sp_secret_key": "k"}),
        spark=None)
    assert scoped.redacted()["source"]["secret_source"] == "secret_scope"
    airgap = Config.from_dict({"role": "source", "source_workspace_id": "1",
                               "source_staging_location": "/Volumes/a/b/c"})
    assert airgap.redacted()["source"]["secret_source"] == "none"


def test_secret_never_written_into_the_bundle_artifacts():
    """End-to-end redaction check (§11): grep EVERY file the run writes for the literal."""
    import os
    import tempfile
    from src.exporters.artifact_writer import ArtifactWriter
    from src.exporters.export_runner import ExportRunner
    from tests.fakes import FakeClient

    literal = "SUPER-SECRET-LITERAL-DO-NOT-PERSIST"
    with tempfile.TemporaryDirectory() as d:
        cfg = Config.from_dbutils(FakeDbutils({
            **_DIRECT_WIDGETS, "spn_secret_value": literal,
            "target_staging_location": d}), spark=None)
        aw = ArtifactWriter(cfg)
        aw.ensure_output_path()
        aw.write_json("inventory.json", {"objects_by_type": {
            "workspace_object": [{"object_type": "NOTEBOOK", "path": "/n", "object_id": "1",
                                  "language": "PYTHON"}]}})
        aw.write_json("config_resolved.json", cfg.redacted())
        ExportRunner(FakeClient(download_table={"api/2.0/workspace/export": b"x=1"}),
                     cfg, aw, content_fetch_workers=1).run()

        offenders = []
        for dirpath, _dirs, names in os.walk(d):
            for n in names:
                p = os.path.join(dirpath, n)
                try:
                    with open(p, "rb") as f:
                        if literal.encode() in f.read():
                            offenders.append(os.path.relpath(p, d))
                except OSError:
                    pass
    assert not offenders, f"the SP secret literal was written into: {offenders}"


# ── import_assets selector semantics (§5) ──────────────────────────────────

def test_selector_defaults_to_everything_and_narrows_correctly():
    assert ImportOptions().selects("genie") is True                   # default "all"
    assert ImportOptions(import_assets=[]).selects("genie") is True   # blank = all
    only_acls = ImportOptions(import_assets=["acls"])
    assert only_acls.selects("acls") is True
    assert only_acls.selects("identity") is False
    # selected_families keeps PHASE order regardless of the order typed in the widget
    mixed = ImportOptions(import_assets=["jobs", "identity", "compute"])
    assert mixed.selected_families == ("identity", "compute", "jobs")


# ── OAuth M2M provider (offline: caching + refresh, no network) ────────────

def test_m2m_token_is_cached_then_refreshed_before_expiry(monkeypatch):
    """One token mint per validity window, and a refresh once it's within the skew — otherwise a
    long phase would either hammer the token endpoint or fail on an aged-out token mid-flight."""
    from src.auth import token_manager as tm

    mints = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(tm.time, "time", lambda: clock["t"])

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            mints.append(1)
            return {"access_token": f"tok{len(mints)}", "expires_in": 600}

    monkeypatch.setattr(tm.requests, "post", lambda *a, **kw: FakeResp())

    prov = tm.oauth_m2m_token_provider("https://src.example.net", "cid", "sec")
    assert prov() == "tok1"
    assert prov() == "tok1", "token was not cached"
    assert len(mints) == 1
    # advance to inside the refresh skew (600 - 60 = 540s of validity)
    clock["t"] += 545
    assert prov() == "tok2", "token was not refreshed before expiry"
    assert len(mints) == 2


def test_m2m_error_does_not_echo_the_secret(monkeypatch):
    """An OAuth error body can quote the request — the raised message must not carry the secret."""
    from src.auth import token_manager as tm

    class FakeResp:
        status_code = 401
        text = '{"error":"invalid_client","request":"client_secret=THE-SECRET"}'

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(tm.requests, "post", lambda *a, **kw: FakeResp())
    prov = tm.oauth_m2m_token_provider("https://src.example.net", "cid", "THE-SECRET")
    with pytest.raises(RuntimeError) as exc:
        prov()
    assert "THE-SECRET" not in str(exc.value)


def test_build_clients_airgap_returns_the_local_client_twice():
    from src.auth.token_manager import ApiClient, build_clients
    cfg = Config.from_dict({"role": "source", "source_workspace_id": "1",
                            "source_staging_location": "/Volumes/a/b/c",
                            "ctx": {"workspace_url": "https://src", "token": "t"}})
    src, tgt = build_clients(cfg)
    assert isinstance(src, ApiClient) and src is tgt, \
        "airgap must never build a second, cross-workspace client"


def test_build_clients_direct_binds_source_to_the_source_host(monkeypatch):
    from src.auth import token_manager as tm

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "src-tok", "expires_in": 3600}

    monkeypatch.setattr(tm.requests, "post", lambda *a, **kw: FakeResp())
    cfg = Config.from_dbutils(FakeDbutils({**_DIRECT_WIDGETS,
                                           "spn_secret_value": "sec"}), spark=None)
    cfg.ctx.workspace_url, cfg.ctx.token = "https://target.example.net", "target-tok"
    src, tgt = tm.build_clients(cfg, dbutils=FakeDbutils())
    assert src.base_url == _DIRECT_WIDGETS["source_workspace_url"]
    assert tgt.base_url == "https://target.example.net"
    assert src is not tgt


# ── dry-run purity guard ───────────────────────────────────────────────────

def test_an_api_error_carries_the_servers_explanation(monkeypatch):
    """REGRESSION (found live): `raise_for_status()` alone yields "400 Client Error: Bad Request for
    url: …", which tells the operator NOTHING about what to fix — every rejection looked identical in
    the import report. Databricks always explains itself in the body, and that text is also what
    `classify_error` matches on to recognise RESOURCE_ALREADY_EXISTS / PERMISSION_DENIED.
    """
    from src.auth.token_manager import ApiClient, StaticTokenProvider

    class FakeResp:
        status_code = 400
        text = '{"error_code":"INVALID_PARAMETER_VALUE","message":"cluster_name is required"}'
        headers: dict = {}

        @staticmethod
        def json():
            return {"error_code": "INVALID_PARAMETER_VALUE",
                    "message": "cluster_name is required"}

    client = ApiClient("https://target", StaticTokenProvider("tok"))
    monkeypatch.setattr(client._s, "request", lambda *a, **kw: FakeResp())

    with pytest.raises(Exception) as exc:
        client.post("api/2.0/clusters/create", {})
    message = str(exc.value)
    assert "INVALID_PARAMETER_VALUE" in message, "the error_code must reach the report"
    assert "cluster_name is required" in message, "the server's message must reach the report"


def test_classify_error_always_surfaces_the_actual_server_message():
    """IMP-2 / user directive: NEVER replace the server error with a hardcoded string. A matched
    entry may only APPEND a remediation hint. This was the genie bug — a real "table not found" /
    warehouse-permission failure was reported as the canned "needs workspace-admin", sending the
    operator down the wrong path when the SP already WAS an admin.
    """
    from src.importers.base_importer import (classify_error, CAT_PERMISSION_DENIED,
                                             CAT_API_ERROR, PrerequisiteMissing)

    # A PERMISSION_DENIED whose real cause is a missing table: the actual text must survive, and it
    # must NOT be reduced to only "needs workspace-admin".
    real = ("POST api/2.0/genie/spaces -> 403: PERMISSION_DENIED: cannot access table "
            "main.sales.orders referenced by the space")
    cat, msg = classify_error(RuntimeError(real))
    assert cat == CAT_PERMISSION_DENIED
    assert "main.sales.orders" in msg, "the actual server error must be surfaced verbatim"
    assert msg.strip() != "the run-as identity lacks permission for this call — it needs " \
                          "workspace-admin on the target", "must not be the old canned-only string"

    # An unmapped error still carries the raw text (no marker matched).
    cat, msg = classify_error(RuntimeError("SOME_NEW_CODE: totally novel failure"))
    assert cat == CAT_API_ERROR and "totally novel failure" in msg

    # Raiser-authored messages pass through verbatim (they're already the actionable text).
    cat, msg = classify_error(PrerequisiteMissing("assign user X to the workspace first"))
    assert msg == "assign user X to the workspace first"

    # PLAN 8 Bug 12: a job whose run_as relies on a warehouse grant 403s at CREATE time (ACLs are
    # applied in the FINAL phase) — an ORDERING artifact that self-heals on retry, not a defect. The
    # message must keep the server text AND append the ordering hint, and be filed prerequisite (so
    # retry_mode=failed_only re-attempts it), NOT permission_denied.
    from src.importers.base_importer import CAT_PREREQUISITE_MISSING
    wh = ("403: piyush.rohida is not authorized to use or monitor this SQL Endpoint")
    cat, msg = classify_error(RuntimeError(wh))
    assert cat == CAT_PREREQUISITE_MISSING, "warehouse-403 self-heals on retry — prerequisite, not perm"
    assert "piyush.rohida" in msg, "the actual server error must survive"
    assert "retry_mode=failed_only" in msg and "final acl phase" in msg.lower()


def test_an_already_exists_error_is_recognised_from_the_body(monkeypatch):
    """The adopt-on-race path depends on MATCHING the body text, so it only works if the body is
    carried through — this ties the two fixes together."""
    from src.auth.token_manager import ApiClient, StaticTokenProvider
    from src.importers.base_importer import is_already_exists

    class FakeResp:
        status_code = 400
        text = '{"error_code":"RESOURCE_ALREADY_EXISTS","message":"a pool with that name exists"}'
        headers: dict = {}

        @staticmethod
        def json():
            return {"error_code": "RESOURCE_ALREADY_EXISTS",
                    "message": "a pool with that name exists"}

    client = ApiClient("https://target", StaticTokenProvider("tok"))
    monkeypatch.setattr(client._s, "request", lambda *a, **kw: FakeResp())
    try:
        client.post("api/2.0/instance-pools/create", {})
        raise AssertionError("should have raised")
    except Exception as exc:  # noqa: BLE001
        assert is_already_exists(exc), \
            "an ALREADY_EXISTS rejection must be recognisable, or the adopt path never fires"


def test_mutation_guard_blocks_writes_but_passes_reads_through():
    from src.auth.token_manager import MutationGuard
    from tests.fakes import FakeClient

    guarded = MutationGuard(FakeClient(get_table={"api/2.0/clusters/list": {"clusters": []}}))
    assert guarded.get("api/2.0/clusters/list") == {"clusters": []}
    for verb in ("post", "put", "patch", "delete"):
        with pytest.raises(AssertionError, match="dry-run violation"):
            getattr(guarded, verb)("api/2.0/anything", {})
    assert len(guarded.attempted) == 4
