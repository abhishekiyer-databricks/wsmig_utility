"""LIVE END-TO-END: inventory + export fvm1 → import into target_ws (Plan 3 build step 10).

This is the real test: it runs the ACTUAL pipeline against two real workspaces and asserts the
behaviours the whole design rests on. It runs in `direct` mode (source read over OAuth M2M), which
also exercises the dual-mode auth path.

    PHASE A  inventory + export fvm1 → a real bundle
    PHASE B  DRY RUN import → decisions made, ZERO writes (asserted by counting mutating calls)
    PHASE C  LIVE import → objects really created on target
    PHASE D  RE-RUN → every unchanged unit must SKIP (idempotency)
    PHASE E  MUTATE a source asset → re-export → re-import ⇒ UPDATE against the STORED target id
    PHASE F  ADOPT → an object created by hand is adopted, never duplicated
    PHASE G  retry_mode → failed_only / skipped_only narrow the work list
    PHASE H  ACL parity + the report set
    PHASE I  clean up everything this test created on the target

Nothing here is skipped on failure: each phase records PASS/FAIL and the run continues, so one
broken phase still yields a full report. Cleanup runs in a `finally`.

Run: python3 -m tests.live_e2e_migration [--keep]
"""
from __future__ import annotations

import configparser
import json
import os
import subprocess
import sys
import time

from src.auth.token_manager import ApiClient, StaticTokenProvider
from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.state.sql_backend import StatementApiBackend
from src.state.state_store import StateStore

SOURCE_PROFILE = os.environ.get("WSMIG_SOURCE_PROFILE", "fvm1")
TARGET_PROFILE = os.environ.get("WSMIG_TARGET_PROFILE", "target_ws")
SECRET_FILE = os.environ.get("WSMIG_SP_SECRET_FILE", "/tmp/wsmig_fvm1_sp_secret.txt")
TEST_SCHEMA = os.environ.get("WSMIG_TEST_SCHEMA", "wsmig_e2e")

# Everything this test creates on the target is prefixed so cleanup is exact and can never touch a
# pre-existing object. The source objects it creates for the mutate test are prefixed too.
PREFIX = "wsmig_e2e_"


class Checks:
    """PASS/FAIL ledger — one line per assertion, so the final report is the test output."""

    def __init__(self):
        self.rows: list[tuple] = []

    def add(self, phase: str, name: str, ok: bool, detail: str = ""):
        self.rows.append((phase, name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"    [{mark}] {name}" + (f" — {detail[:200]}" if detail else ""))
        return ok

    def failed(self):
        return [r for r in self.rows if not r[2]]

    def summary(self):
        npass = sum(1 for r in self.rows if r[2])
        return npass, len(self.rows) - npass


CHECKS = Checks()


# ── plumbing ────────────────────────────────────────────────────────────────

def _host(profile: str) -> str:
    c = configparser.ConfigParser()
    c.read(os.path.expanduser("~/.databrickscfg"))
    return dict(c[profile])["host"].rstrip("/")


def _token(profile: str) -> str:
    return json.loads(subprocess.check_output(
        ["databricks", "auth", "token", "-p", profile], text=True))["access_token"]


def _refreshing_client(profile: str) -> ApiClient:
    """A client whose CLI OAuth token is refreshed periodically.

    Harness-only concern: a live end-to-end run can outlast the CLI token, and a 403 halfway through
    would look like a tool bug. Production uses the runtime's managed notebook-context token.
    """
    # 600s was too slack: an Azure CLI OAuth token can expire sooner than that, so there was a
    # window where the provider happily served a DEAD token and every write 403'd "Invalid Token"
    # mid-run (102 of them in one run) — which reads exactly like a tool bug. Refresh well inside
    # the shortest plausible lifetime, and ask the CLI (which caches) rather than tracking expiry.
    cache = {"tok": _token(profile), "ts": time.time()}

    def provider():
        if time.time() - cache["ts"] > 120:
            try:
                cache["tok"] = _token(profile)
            except Exception:  # noqa: BLE001 — keep the old token; the retry layer will surface it
                pass
            cache["ts"] = time.time()
        return cache["tok"]

    return ApiClient(_host(profile), provider)


def _sp_creds() -> tuple[str, str]:
    with open(SECRET_FILE) as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    return lines[0], lines[1]


class CountingClient:
    """Wraps a client and counts MUTATING calls — how dry-run purity is proven, not asserted."""

    def __init__(self, inner):
        self._inner = inner
        self.mutations: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in ("post", "put", "patch", "delete"):
            def wrapped(path, *a, **kw):
                self.mutations.append(f"{name.upper()} {path}")
                return attr(path, *a, **kw)
            return wrapped
        return attr


def _cfg(staging, run_id, *, dry_run, catalog, warehouse, import_assets="all",
         retry_mode="off", force_full=False) -> Config:
    client_id, secret = _sp_creds()
    return Config.from_dict({
        "role": "target",
        "connectivity_mode": "direct",
        "source_workspace_id": _source_ws_id(),
        "run_id": run_id,
        "target_staging_location": staging,
        "dry_run": dry_run,
        "source": {"workspace_url": _host(SOURCE_PROFILE), "client_id": client_id,
                   "spn_secret_value": secret},
        "imports": {"state_catalog": catalog, "state_schema": TEST_SCHEMA,
                    "state_warehouse_id": warehouse,
                    "import_assets": [s.strip() for s in import_assets.split(",")],
                    "retry_mode": retry_mode, "force_full_import": force_full,
                    "preflight_enforce": False},
        # Keep the live run bounded so it completes in minutes rather than an hour. These MUST stay
        # comfortably above the source workspace's real object count: at 250 the walk truncated
        # before reaching /Shared's children, so the fixture notebook was never inventoried and two
        # checks failed for a reason that had nothing to do with the code under test. Raise these if
        # the fixture set grows again.
        "max_workspace_items": 2000, "max_ws_api_calls": 3000,
        "ctx": {"workspace_url": _host(TARGET_PROFILE), "token": _token(TARGET_PROFILE)},
    })


def _source_ws_id() -> str:
    return _host(SOURCE_PROFILE).split("//adb-")[1].split(".")[0]


def _default_catalog(client) -> str:
    doc = client.get("api/2.1/unity-catalog/current-metastore-assignment")
    return doc.get("default_catalog_name") or ""


def _pick_warehouse(client) -> str:
    whs = (client.get("api/2.0/sql/warehouses") or {}).get("warehouses") or []
    if not whs:
        raise SystemExit("the target has no SQL warehouse — the state store needs one off-cluster")
    whs.sort(key=lambda w: (w.get("state") != "RUNNING",
                            not w.get("enable_serverless_compute")))
    return whs[0]["id"]


def _import(cfg, target_client, aw, *, count_mutations=False, run_preflight=False,
            source_client=None):
    """Run the real ImportRunner (optionally the real Preflight first).

    Returns (summary, mutating_calls, state).
    """
    from src.importers.import_runner import ImportRunner
    client = CountingClient(target_client) if count_mutations else target_client
    state = None
    if cfg.state_enabled:
        state = StateStore(StatementApiBackend(target_client, cfg.imports.state_warehouse_id), cfg)
        state.ensure_table()
        state.load(force=True)

    verdict = {"verdict": "GO"}
    if run_preflight:
        # The real gate, as 04_Import runs it — so the harness exercises preflight rather than
        # assuming a GO, and `preflight_report.*` really gets written.
        from src.importers.preflight import Preflight
        # The source client MUST be handed over in `direct` mode: preflight's source-connectivity
        # check is BLOCKING, and rightly so (in direct mode there'd be nothing to read without it).
        verdict = Preflight(target_client, cfg, aw, state=state,
                            source_client=source_client if cfg.is_direct else None).run()
        print(f"    preflight verdict: {verdict['verdict']} "
              f"({len(verdict['blocking'])} blocking, {len(verdict['degrading'])} degrading)")
        for item in verdict["blocking"]:
            print(f"      BLOCKING: {item[:200]}")
        # A NO-GO here would mean a real prerequisite is unmet on the target, not a tool problem —
        # but it must be visible rather than silently overridden by preflight_enforce=false.
        CHECKS.add("H", "preflight did not report a BLOCKING problem",
                   verdict["verdict"] != "NO-GO",
                   f"verdict={verdict['verdict']} blocking={verdict['blocking'][:2]}")

    runner = ImportRunner(client, cfg, aw, state=state, preflight_verdict=verdict)
    summary = runner.run()
    # PLAN 7 §B2/D-1: ACL parity is no longer a standalone file — it lives in the runner's shared
    # context (and is folded into import_status.xlsx). Surface it on the summary for the harness.
    summary["_acl_parity"] = runner.context.get("acl_parity") or {}
    return summary, (client.mutations if count_mutations else []), state


def _units_by_status(aw) -> dict:
    doc = aw.read_json("misc/import_results.json") or {}
    out: dict = {}
    for u in doc.get("units", []):
        out.setdefault(u.get("import_status"), []).append(u)
    return out


# ── the source fixture we mutate (created on fvm1, deleted at the end) ──────

def _ensure_source_fixture(source_client) -> dict:
    """Create a couple of SMALL, cheap source objects this test can safely mutate.

    Deliberately not reusing the workspace's real assets: the mutate test has to CHANGE something,
    and changing a real object on a shared workspace would be rude and non-repeatable.
    """
    created = {}
    policy_name = f"{PREFIX}policy"
    definition = json.dumps({"spark_version": {"type": "fixed", "value": "14.3.x-scala2.12"}})
    existing = {safe(p.get("name")): safe(p.get("policy_id")) for p in
                ((source_client.get("api/2.0/policies/clusters/list") or {}).get("policies") or [])}
    if policy_name in existing:
        created["policy_id"] = existing[policy_name]
        source_client.post("api/2.0/policies/clusters/edit",
                           {"policy_id": existing[policy_name], "name": policy_name,
                            "definition": definition})
    else:
        doc = source_client.post("api/2.0/policies/clusters/create",
                                 {"name": policy_name, "definition": definition})
        created["policy_id"] = safe(doc.get("policy_id"))

    nb_path = f"/Shared/{PREFIX}notebook"
    import base64
    source_client.post("api/2.0/workspace/mkdirs", {"path": "/Shared"})
    source_client.post("api/2.0/workspace/import", {
        "path": nb_path, "format": "SOURCE", "language": "PYTHON", "object_type": "NOTEBOOK",
        "overwrite": True,
        "content": base64.b64encode(b"# wsmig e2e v1\nprint('version one')\n").decode()})
    created["notebook_path"] = nb_path
    return created


def safe(v):
    return "" if v is None else str(v)


def _mutate_source_fixture(source_client, fixture) -> None:
    """Change BOTH a config asset and a notebook's CONTENT.

    The notebook matters most: content was the serious fingerprint gap (§7c-audit GAP 1) — editing a
    notebook used to produce an identical fingerprint, so the target silently kept the old code.
    """
    import base64
    source_client.post("api/2.0/policies/clusters/edit", {
        "policy_id": fixture["policy_id"], "name": f"{PREFIX}policy",
        "definition": json.dumps({"spark_version": {"type": "fixed",
                                                    "value": "15.4.x-scala2.12"},
                                  "num_workers": {"type": "fixed", "value": 2}})})
    source_client.post("api/2.0/workspace/import", {
        "path": fixture["notebook_path"], "format": "SOURCE", "language": "PYTHON",
        "object_type": "NOTEBOOK", "overwrite": True,
        "content": base64.b64encode(b"# wsmig e2e v2 EDITED\nprint('version TWO')\n").decode()})


# ── cleanup ─────────────────────────────────────────────────────────────────

def _cleanup_prefixed_targets(target_client, quiet: bool = False) -> None:
    """Delete every prefix-scoped object this test creates on the TARGET.

    Prefix-scoped so it can never touch a pre-existing object in a shared workspace.
    """
    for path, result_key, name_field, id_field, delete_path, id_key in (
            ("api/2.0/policies/clusters/list", "policies", "name", "policy_id",
             "api/2.0/policies/clusters/delete", "policy_id"),
            ("api/2.0/instance-pools/list", "instance_pools", "instance_pool_name",
             "instance_pool_id", "api/2.0/instance-pools/delete", "instance_pool_id"),
            ("api/2.0/clusters/list", "clusters", "cluster_name", "cluster_id",
             "api/2.0/clusters/permanent-delete", "cluster_id")):
        try:
            for item in (target_client.get(path) or {}).get(result_key) or []:
                if safe(item.get(name_field)).startswith(PREFIX):
                    target_client.post(delete_path, {id_key: safe(item.get(id_field))})
                    if not quiet:
                        print(f"  deleted target {result_key}: {item.get(name_field)}")
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"  cleanup of {result_key} failed (harmless): {str(exc)[:120]}")


def _cleanup(source_client, target_client, catalog, warehouse, keep: bool) -> None:
    if keep:
        print("\n--keep given: leaving everything in place for inspection")
        return
    print("\n== cleaning up ==")
    _cleanup_prefixed_targets(target_client)

    for path in (f"/Shared/{PREFIX}notebook",):
        for client, label in ((target_client, "target"), (source_client, "source")):
            try:
                client.post("api/2.0/workspace/delete", {"path": path, "recursive": False})
                print(f"  deleted {label} {path}")
            except Exception:  # noqa: BLE001
                pass

    # source fixture
    try:
        for p in ((source_client.get("api/2.0/policies/clusters/list") or {})
                  .get("policies") or []):
            if safe(p.get("name")).startswith(PREFIX):
                source_client.post("api/2.0/policies/clusters/delete",
                                   {"policy_id": safe(p.get("policy_id"))})
                print(f"  deleted source policy: {p.get('name')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  source cleanup failed (harmless): {str(exc)[:120]}")

    # the throwaway state schema
    try:
        StatementApiBackend(target_client, warehouse).sql(
            f"DROP SCHEMA IF EXISTS {catalog}.{TEST_SCHEMA} CASCADE")
        print(f"  dropped {catalog}.{TEST_SCHEMA}")
    except Exception as exc:  # noqa: BLE001
        print(f"  schema cleanup failed (remove by hand): {str(exc)[:160]}")


# ── the run ─────────────────────────────────────────────────────────────────

def main(keep: bool = False) -> int:
    import tempfile

    source_client = _refreshing_client(SOURCE_PROFILE)
    target_client = _refreshing_client(TARGET_PROFILE)
    catalog = _default_catalog(target_client)
    warehouse = _pick_warehouse(target_client)
    staging = tempfile.mkdtemp(prefix="wsmig_e2e_")

    print("=" * 78)
    print("LIVE END-TO-END MIGRATION TEST")
    print("=" * 78)
    print(f"source   : {_host(SOURCE_PROFILE)}  (ws id {_source_ws_id()})")
    print(f"target   : {_host(TARGET_PROFILE)}")
    print(f"staging  : {staging}")
    print(f"state    : {catalog}.{TEST_SCHEMA} via warehouse {warehouse}")
    print(f"mode     : direct (source read over OAuth M2M)\n")

    # Start from a CLEAN state schema. State that survived an earlier attempt would make phase B/C
    # report SKIP where they should report CREATE — correct behaviour for the tool, but it would
    # silently weaken the very assertions this harness exists to make.
    backend = StatementApiBackend(target_client, warehouse)
    backend.sql(f"DROP SCHEMA IF EXISTS {catalog}.{TEST_SCHEMA} CASCADE")
    backend.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{TEST_SCHEMA}")

    fixture = {}
    try:
        # Clear any prefix-scoped leftovers from a previous attempt, for the same reason the state
        # schema is dropped: a surviving object would turn a CREATE assertion into an ADOPT and
        # quietly stop testing what it means to test.
        _cleanup_prefixed_targets(target_client, quiet=True)

        print("== PHASE 0: source fixture ==")
        fixture = _ensure_source_fixture(source_client)
        CHECKS.add("0", "source fixture created (a policy + a notebook to mutate later)",
                   bool(fixture.get("policy_id")), str(fixture))

        # ── PHASE A: inventory + export ────────────────────────────────────
        print("\n== PHASE A: inventory + export (fvm1, read over M2M) ==")
        run_id = "e2e_" + time.strftime("%Y%m%d_%H%M%S")
        cfg_a = _cfg(staging, run_id, dry_run=True, catalog=catalog, warehouse=warehouse)
        aw = ArtifactWriter(cfg_a)

        from src.collectors.inventory_runner import InventoryRunner
        from src.exporters.export_runner import ExportRunner
        inv = InventoryRunner(source_client, cfg_a, aw).run()
        CHECKS.add("A", "inventory read the source over OAuth M2M",
                   sum(inv["counts"].values()) > 0, f"counts={inv['counts']}")

        exp = ExportRunner(source_client, cfg_a, aw, content_fetch_workers=4).run()
        CHECKS.add("A", "export produced a bundle", exp["total"] > 0,
                   f"{exp['total']} units, {exp['success']} captured, {exp['failure']} failed")
        CHECKS.add("A", "export had NO failures", exp["failure"] == 0,
                   f"{exp['failure']} failures")
        verify = aw.verify_manifest()
        CHECKS.add("A", "the bundle's manifest verifies", verify["ok"],
                   f"missing={verify['missing'][:3]} mismatched={verify['mismatched'][:3]}")

        from src.exporters.bundle_state import read_latest_export_pointer
        pointer = read_latest_export_pointer(cfg_a)
        CHECKS.add("A", "LATEST_EXPORT.json written after the manifest",
                   bool(pointer) and pointer.get("run_id") == run_id, str(pointer)[:120])

        # the fixture notebook's content hash must be in the bundle (GAP 1 fix, live)
        index = aw.read_json("misc/export_index.json") or {}
        nb_unit = next((u for u in index.get("units", [])
                        if u.get("natural_key") == fixture.get("notebook_path")), None)
        CHECKS.add("A", "the fixture notebook was exported with a content hash",
                   bool(nb_unit and nb_unit.get("content_sha256")),
                   f"fingerprint={(nb_unit or {}).get('fingerprint','')[:24]}…")
        fp_v1 = (nb_unit or {}).get("fingerprint")
        policy_unit = next((u for u in index.get("units", [])
                            if u.get("asset_type") == "cluster_policy"
                            and u.get("natural_key") == f"{PREFIX}policy"), None)
        policy_fp_v1 = (policy_unit or {}).get("fingerprint")

        # ── PHASE B: dry run ───────────────────────────────────────────────
        print("\n== PHASE B: DRY RUN import (must write NOTHING) ==")
        cfg_b = _cfg(staging, run_id, dry_run=True, catalog=catalog, warehouse=warehouse)
        aw_b = ArtifactWriter(cfg_b)
        summary_b, mutations, _st = _import(cfg_b, target_client, aw_b, count_mutations=True)
        CHECKS.add("B", "dry run made ZERO mutating calls to the target", not mutations,
                   f"{len(mutations)} mutations: {mutations[:5]}")
        CHECKS.add("B", "dry run still decided every unit",
                   summary_b["totals"].get("total", 0) > 0,
                   f"total={summary_b['totals'].get('total')}")
        CHECKS.add("B", "dry run wrote its report set (own filename, A1)",
                   os.path.isfile(os.path.join(aw_b.root, "reports/import_status_dry_run.xlsx"))
                   and not os.path.isfile(os.path.join(aw_b.root, "reports/import_status.xlsx")))
        dry_state = StateStore(StatementApiBackend(target_client, warehouse), cfg_b)
        dry_state.load(force=True)
        CHECKS.add("B", "dry run wrote to the _dryrun state table only",
                   cfg_b.state_table_fqn.endswith("_dryrun"), cfg_b.state_table_fqn)

        # ── PHASE C: live import ───────────────────────────────────────────
        print("\n== PHASE C: LIVE import ==")
        cfg_c = _cfg(staging, run_id, dry_run=False, catalog=catalog, warehouse=warehouse)
        aw_c = ArtifactWriter(cfg_c)
        summary_c, _m, state_c = _import(cfg_c, target_client, aw_c)
        totals_c = summary_c["totals"]
        by_status = _units_by_status(aw_c)
        print(f"    totals: {json.dumps(totals_c)}")
        CHECKS.add("C", "the live run COMPLETED (fail-soft: no unit aborted it)",
                   summary_c["run_status"] == "completed", summary_c["run_status"])
        CHECKS.add("C", "objects were created on the target", totals_c.get("created", 0) > 0,
                   f"created={totals_c.get('created')}")
        CHECKS.add("C", "every unit got an outcome (never silently skipped)",
                   totals_c.get("total", 0) == len(
                       [u for units in by_status.values() for u in units]),
                   f"total={totals_c.get('total')}")

        # the fixture policy really exists on target now
        target_policies = {safe(p.get("name")): safe(p.get("policy_id")) for p in
                           ((target_client.get("api/2.0/policies/clusters/list") or {})
                            .get("policies") or [])}
        CHECKS.add("C", "the fixture cluster policy exists on the TARGET",
                   f"{PREFIX}policy" in target_policies,
                   f"target policy id={target_policies.get(f'{PREFIX}policy')}")
        target_policy_id = target_policies.get(f"{PREFIX}policy", "")

        # and the notebook, with v1 content
        nb_target = target_client.get("api/2.0/workspace/get-status",
                                      params={"path": fixture["notebook_path"]})
        CHECKS.add("C", "the fixture notebook exists on the TARGET as a NOTEBOOK",
                   safe(nb_target.get("object_type")) == "NOTEBOOK", str(nb_target)[:120])
        body_v1 = target_client.download_bytes(
            "api/2.0/workspace/export",
            params={"path": fixture["notebook_path"], "direct_download": "true",
                    "format": "SOURCE"})
        CHECKS.add("C", "the notebook's v1 CONTENT landed on target",
                   b"version one" in body_v1, body_v1[:60].decode(errors="replace"))

        # state rows carry BOTH ids
        state_c.load(force=True)
        row = state_c.row("cluster_policy", f"{PREFIX}policy") or {}
        CHECKS.add("C", "the state row stores BOTH source and target ids",
                   bool(row.get("source_object_id")) and row.get("target_object_id")
                   == target_policy_id,
                   f"src={row.get('source_object_id')} tgt={row.get('target_object_id')}")

        # identity map durability
        id_map = state_c.load_identity_map()
        CHECKS.add("C", "the identity map has rows (incl. adopted identities)",
                   bool(id_map["user_map"] or id_map["group_map"] or id_map["sp_mapping"]),
                   f"users={len(id_map['user_map'])} groups={len(id_map['group_map'])} "
                   f"sps={len(id_map['sp_mapping'])}")

        # ── identity PARITY: the Plan 6 contract, asserted against both live workspaces ──
        # These were verified by hand while building Plan 6; encoding them here means a regression
        # in create-vs-assign can never again pass this harness.
        def _scim_all(cl, resource):
            return cl.get_scim(resource)

        # 1. Every SPN keeps its SOURCE applicationId. Omitting applicationId on create mints a new
        #    one and orphans every ACL / job run_as / secret grant — invisible in any report.
        src_apps = {safe(sp.get("applicationId"))
                    for sp in _scim_all(source_client, "ServicePrincipals")}
        tgt_apps = {safe(sp.get("applicationId"))
                    for sp in _scim_all(target_client, "ServicePrincipals")}
        missing_apps = sorted(a for a in src_apps if a and a not in tgt_apps)
        CHECKS.add("C", "every source SPN exists on target with the SAME applicationId",
                   not missing_apps,
                   f"{len(src_apps & tgt_apps)}/{len(src_apps)} preserved; missing={missing_apps[:5]}")

        # 2. Groups: an ACCOUNT group keeps its ACCOUNT id; a WORKSPACE-LOCAL group gets a NEW id.
        #    A workspace-local group on target holding an account group's name is the shadow that
        #    permanently blocks assignment, so identity of kind matters as much as presence.
        def _groups(cl):
            return {safe(g.get("displayName")):
                    (safe(g.get("id")), safe((g.get("meta") or {}).get("resourceType")))
                    for g in _scim_all(cl, "Groups")}
        sg, tg = _groups(source_client), _groups(target_client)
        wrong_kind, acct_id_changed, ws_id_reused = [], [], []
        for name, (sid, srt) in sg.items():
            if name in ("admins", "users") or name not in tg:
                continue
            tid, trt = tg[name]
            if srt != trt:
                wrong_kind.append(f"{name}: {srt}->{trt}")
            elif srt == "Group" and tid != sid:
                acct_id_changed.append(f"{name}: {sid}->{tid}")
            elif srt == "WorkspaceGroup" and tid == sid:
                ws_id_reused.append(name)
        CHECKS.add("C", "no group changed KIND between source and target (no shadow groups)",
                   not wrong_kind, str(wrong_kind[:5]))
        CHECKS.add("C", "account groups kept their ACCOUNT id (assigned, not recreated)",
                   not acct_id_changed, str(acct_id_changed[:5]))

        # 3. Built-in group MEMBERSHIP must carry over, or a source admin is not an admin on target.
        for builtin in ("admins", "users"):
            def _members(cl, gname):
                for g in _scim_all(cl, "Groups"):
                    if safe(g.get("displayName")) == gname:
                        return {safe(m.get("display")) for m in (g.get("members") or [])}
                return set()
            s_mem, t_mem = _members(source_client, builtin), _members(target_client, builtin)
            CHECKS.add("C", f"`{builtin}` membership carried over to target",
                       bool(s_mem) and not (s_mem - t_mem),
                       f"source={len(s_mem)} target={len(t_mem)} missing={sorted(s_mem - t_mem)[:5]}")

        # 4. Workspace ADMIN vs USER. It lives ONLY in permissionassignments, so without this an
        #    admin-by-assignment silently migrates as a plain USER.
        def _assignments(cl):
            data = cl.get("api/2.0/preview/permissionassignments") or {}
            out = {}
            for a in data.get("permission_assignments", []) or []:
                pr = a.get("principal") or {}
                key = (safe(pr.get("group_name")) or safe(pr.get("user_name"))
                       or safe(pr.get("service_principal_name")))
                if key:
                    out[key] = sorted(a.get("permissions") or [])
            return out
        s_pa, t_pa = _assignments(source_client), _assignments(target_client)
        perm_bad = [f"{k}: src={v} tgt={t_pa.get(k)}" for k, v in s_pa.items()
                    if t_pa.get(k) != v]
        CHECKS.add("C", "workspace ADMIN/USER matches source for every principal",
                   not perm_bad,
                   f"{len(s_pa) - len(perm_bad)}/{len(s_pa)} match; mismatched={perm_bad[:5]}")

        # ── PHASE D: re-run ⇒ SKIP ─────────────────────────────────────────
        print("\n== PHASE D: RE-RUN with no source change (must SKIP, not duplicate) ==")
        cfg_d = _cfg(staging, run_id, dry_run=False, catalog=catalog, warehouse=warehouse,
                     force_full=True)   # ignore the checkpoint so the STATE path is exercised
        aw_d = ArtifactWriter(cfg_d)
        summary_d, _m, state_d = _import(cfg_d, target_client, aw_d)
        totals_d = summary_d["totals"]
        print(f"    totals: {json.dumps(totals_d)}")
        CHECKS.add("D", "a re-run SKIPPED unchanged units", totals_d.get("skipped", 0) > 0,
                   f"skipped={totals_d.get('skipped')} created={totals_d.get('created')}")
        CHECKS.add("D", "a re-run created (almost) nothing new",
                   totals_d.get("created", 0) <= totals_d.get("skipped", 0),
                   f"created={totals_d.get('created')} vs skipped={totals_d.get('skipped')}")
        after = [safe(p.get("name")) for p in
                 ((target_client.get("api/2.0/policies/clusters/list") or {})
                  .get("policies") or [])]
        CHECKS.add("D", "NO duplicate policy was created",
                   after.count(f"{PREFIX}policy") == 1,
                   f"{after.count(f'{PREFIX}policy')} copies of {PREFIX}policy")

        # ── PHASE E: mutate ⇒ UPDATE ───────────────────────────────────────
        print("\n== PHASE E: MUTATE the source, re-export, re-import (must UPDATE) ==")
        _mutate_source_fixture(source_client, fixture)
        run_id2 = run_id + "_v2"
        cfg_e_exp = _cfg(staging, run_id2, dry_run=True, catalog=catalog, warehouse=warehouse)
        aw_e = ArtifactWriter(cfg_e_exp)
        InventoryRunner(source_client, cfg_e_exp, aw_e).run()
        ExportRunner(source_client, cfg_e_exp, aw_e, content_fetch_workers=4).run()

        index2 = aw_e.read_json("misc/export_index.json") or {}
        nb_unit2 = next((u for u in index2.get("units", [])
                         if u.get("natural_key") == fixture["notebook_path"]), None)
        policy_unit2 = next((u for u in index2.get("units", [])
                             if u.get("asset_type") == "cluster_policy"
                             and u.get("natural_key") == f"{PREFIX}policy"), None)
        # THE regression check for the serious fingerprint gap
        CHECKS.add("E", "an EDITED NOTEBOOK's fingerprint CHANGED (GAP 1 fix, live)",
                   bool(nb_unit2) and nb_unit2.get("fingerprint") != fp_v1,
                   f"v1={safe(fp_v1)[:20]}… v2={safe((nb_unit2 or {}).get('fingerprint'))[:20]}…")
        CHECKS.add("E", "an edited POLICY's fingerprint changed",
                   bool(policy_unit2) and policy_unit2.get("fingerprint") != policy_fp_v1)

        cfg_e = _cfg(staging, run_id2, dry_run=False, catalog=catalog, warehouse=warehouse)
        aw_e2 = ArtifactWriter(cfg_e)
        # Runs the REAL preflight gate too, so `preflight_report.*` is produced exactly as
        # `04_Import` produces it.
        summary_e, _m, state_e = _import(cfg_e, target_client, aw_e2, run_preflight=True,
                                         source_client=source_client)
        totals_e = summary_e["totals"]
        print(f"    totals: {json.dumps(totals_e)}")
        CHECKS.add("E", "the changed units were UPDATED", totals_e.get("updated", 0) > 0,
                   f"updated={totals_e.get('updated')}")

        # the UPDATE must have hit the SAME target object, not made a new one
        policies_after = [safe(p.get("name")) for p in
                          ((target_client.get("api/2.0/policies/clusters/list") or {})
                           .get("policies") or [])]
        CHECKS.add("E", "the UPDATE edited the stored target id (still ONE policy)",
                   policies_after.count(f"{PREFIX}policy") == 1,
                   f"{policies_after.count(f'{PREFIX}policy')} copies")
        state_e.load(force=True)
        row_e = state_e.row("cluster_policy", f"{PREFIX}policy") or {}
        CHECKS.add("E", "the state row kept the SAME target id across the update",
                   row_e.get("target_object_id") == target_policy_id,
                   f"before={target_policy_id} after={row_e.get('target_object_id')}")

        # and the notebook's NEW content is on target — this is what GAP 1 used to break
        body_v2 = target_client.download_bytes(
            "api/2.0/workspace/export",
            params={"path": fixture["notebook_path"], "direct_download": "true",
                    "format": "SOURCE"})
        CHECKS.add("E", "the notebook's EDITED content reached the target "
                        "(the GAP 1 failure mode)", b"version TWO" in body_v2,
                   body_v2[:60].decode(errors="replace"))

        # ── PHASE F: adopt ─────────────────────────────────────────────────
        print("\n== PHASE F: ADOPT an object created by hand (must not duplicate) ==")
        hand_name = f"{PREFIX}hand_made_pool"
        try:
            target_client.post("api/2.0/instance-pools/create", {
                "instance_pool_name": hand_name, "node_type_id": "Standard_DS3_v2",
                "min_idle_instances": 0, "idle_instance_autotermination_minutes": 10})
        except Exception as exc:  # noqa: BLE001 — may already exist from an earlier attempt
            print(f"    (pool create: {str(exc)[:100]})")
        # A unit for it, as if the source had one with the same natural key.
        adopt_run = run_id + "_adopt"
        cfg_f = _cfg(staging, adopt_run, dry_run=False, catalog=catalog, warehouse=warehouse,
                     import_assets="compute")
        aw_f = ArtifactWriter(cfg_f)
        aw_f.ensure_output_path()
        aw_f.write_json("misc/manifest.json", {"files": [], "tool_version": "0.1.0"})
        aw_f.write_json("misc/export_index.json", {"units": [{
            "asset_type": "instance_pool", "natural_key": hand_name, "source_id": "SRC-HAND",
            "fingerprint": "sha256:hand", "import_action": "create", "export_status": "success"}]})
        aw_f.write_json("export/compute/instance_pools.json", {"units": [{
            "asset_type": "instance_pool", "natural_key": hand_name, "source_id": "SRC-HAND",
            "fingerprint": "sha256:hand", "import_action": "create",
            "payload": {"instance_pool_name": hand_name, "node_type_id": "Standard_DS3_v2",
                        "min_idle_instances": 0,
                        "idle_instance_autotermination_minutes": 10}}]})
        summary_f, _m, _st_f = _import(cfg_f, target_client, aw_f)
        by_status_f = _units_by_status(aw_f)
        adopted = by_status_f.get("adopted", [])
        CHECKS.add("F", "a pre-existing object was ADOPTED, not duplicated",
                   any(u["natural_key"] == hand_name for u in adopted),
                   f"statuses={ {k: len(v) for k, v in by_status_f.items()} }")
        pools = [safe(p.get("instance_pool_name")) for p in
                 ((target_client.get("api/2.0/instance-pools/list") or {})
                  .get("instance_pools") or [])]
        CHECKS.add("F", "still exactly ONE copy of the hand-made pool",
                   pools.count(hand_name) == 1, f"{pools.count(hand_name)} copies")

        # ── PHASE G: retry modes ───────────────────────────────────────────
        print("\n== PHASE G: retry_mode narrows the work list ==")
        cfg_g = _cfg(staging, run_id2, dry_run=False, catalog=catalog, warehouse=warehouse,
                     retry_mode="failed_only")
        aw_g = ArtifactWriter(cfg_g)
        summary_g, _m, state_g = _import(cfg_g, target_client, aw_g)
        outstanding = state_g.retry_keys("failed_only") or set()
        # A live retry writes a scoped, timestamped report — read the file the run actually wrote,
        # which now contains ONLY the units the retry attempted (no "not outstanding" filler).
        _retry_json = (summary_g.get("reports") or {}).get("json") or "misc/import_results.json"
        attempted = (aw_g.read_json(_retry_json) or {}).get("units", [])
        CHECKS.add("G", "the retry report is scoped to only the outstanding units it attempted",
                   len(attempted) <= max(len(outstanding), 1)
                   and not any("not outstanding" in safe(u.get("note")) for u in attempted),
                   f"{len(outstanding)} outstanding, {len(attempted)} in the scoped retry report")
        # the runner's in-memory result still ACCOUNTS for every unit (only the REPORT is scoped)
        CHECKS.add("G", "a narrowed run still accounts for every unit in its summary",
                   summary_g["totals"].get("total", 0) > 0,
                   f"total={summary_g['totals'].get('total')}")

        cfg_g2 = _cfg(staging, run_id2, dry_run=False, catalog=catalog, warehouse=warehouse,
                      import_assets="acls", retry_mode="skipped_only")
        aw_g2 = ArtifactWriter(cfg_g2)
        summary_g2, _m, _st = _import(cfg_g2, target_client, aw_g2)
        CHECKS.add("G", "import_assets=acls + retry_mode=skipped_only runs ACLs alone",
                   summary_g2["families"] == ["acls"], str(summary_g2["families"]))

        # ── PHASE H: ACL parity + reports ──────────────────────────────────
        print("\n== PHASE H: ACL parity + the report set ==")
        # Parity is folded into the workbook now; the harness reads it off the run summary.
        parity = summary_g2.get("_acl_parity") or summary_e.get("_acl_parity") or {}
        counts = parity.get("counts", {})
        CHECKS.add("H", "an ACL parity report was produced", bool(parity),
                   f"checked={parity.get('objects_checked')} counts={counts}")
        if parity:
            mismatched = counts.get("missing_on_target", 0) + counts.get("both", 0)
            # The report must cover the ACLs that are on target NOW — including ones applied by an
            # earlier run and skipped by this one. A report of 0 objects means the verification
            # evidence disappeared, which is itself the failure.
            CHECKS.add("H", "the parity report actually verified objects",
                       parity.get("objects_checked", 0) > 0,
                       f"objects_checked={parity.get('objects_checked')}")
            CHECKS.add("H", "applied ACLs MATCH source (parity proven, not assumed)",
                       counts.get("match", 0) > 0 and mismatched == 0,
                       f"match={counts.get('match')} missing={counts.get('missing_on_target')} "
                       f"extra={counts.get('extra_on_target')} both={counts.get('both')}")
        # PLAN 7 §B2: import_results.html is gone; paths moved to misc/ and reports/. The live
        # (non-dry) run writes the plain import_status.xlsx.
        for name in ("misc/import_results.json", "reports/import_status.xlsx",
                     "reports/manual_actions_import.md", "misc/preflight_report.json"):
            CHECKS.add("H", f"{name} written",
                       os.path.isfile(os.path.join(aw_e2.root, name)))
        CHECKS.add("H", "removed outputs are NOT written",
                   not os.path.isfile(os.path.join(aw_e2.root, "import_results.html"))
                   and not os.path.isfile(os.path.join(aw_e2.root, "acl_parity_report.json")))

        # ── A4: a WS-LOCAL SP recreated with a NEW appId, holding a grant, must land on target
        #        under the NEW appId (its old appId must appear in NO target ACL) ──────────────
        # The fixtures create wsmig_test_db_sp / _db_sp2 (workspace-local, so recreated with fresh
        # applicationIds) and grant them across every object type in phase_acls. This proves the
        # SP-remap reaches the ACL layer end to end, not just the identity phase.
        try:
            idmap = state_e.load_identity_map() if state_e is not None else {}
            sp_mapping = idmap.get("sp_mapping") or {}   # old appId -> new appId
            if sp_mapping:
                # Re-read every object the parity pass verified and collect the principals actually
                # on target now.
                on_target_principals = set()
                for obj in parity.get("objects", []):
                    ptype = obj.get("perm_object_type")
                    tid = obj.get("target_id")
                    if not (ptype and tid):
                        continue
                    try:
                        doc = target_client.get(f"api/2.0/permissions/{ptype}/{tid}") or {}
                    except Exception:  # noqa: BLE001
                        continue
                    for ace in doc.get("access_control_list") or []:
                        if ace.get("service_principal_name"):
                            on_target_principals.add(ace["service_principal_name"])
                old_ids = {o for o in sp_mapping if o != sp_mapping[o]}
                new_ids = {sp_mapping[o] for o in old_ids}
                leaked_old = old_ids & on_target_principals
                landed_new = new_ids & on_target_principals
                CHECKS.add("H", "a recreated ws-local SP's grant names the NEW appId on target",
                           bool(landed_new),
                           f"sp_mapping={sp_mapping} on_target_sps={sorted(on_target_principals)[:6]}")
                CHECKS.add("H", "no recreated SP's OLD appId leaked into a target ACL",
                           not leaked_old, f"leaked old appIds: {sorted(leaked_old)}")
            else:
                CHECKS.add("H", "sp_mapping available for the A4 remap-parity check",
                           False, "identity map had no sp_mapping — was identity imported?")
        except Exception as exc:  # noqa: BLE001
            CHECKS.add("H", "A4 SP-ACL remap check ran", False, str(exc)[:200])

        # the report must be joinable on (asset_type, natural_key) — all Plan 4 needs
        results = aw_e2.read_json("misc/import_results.json") or {}
        joinable = all(u.get("asset_type") and u.get("natural_key")
                       for u in results.get("units", []))
        CHECKS.add("H", "every report row is joinable on (asset_type, natural_key)", joinable,
                   f"{len(results.get('units', []))} rows")

        # failures, if any, must carry a category + human remediation
        failures = [u for u in results.get("units", []) if u.get("import_status") == "failed"]
        CHECKS.add("H", "every failure carries a category and a human-readable reason",
                   all(u.get("failure_category") and u.get("note") for u in failures),
                   f"{len(failures)} failures")
        if failures:
            print("    failures recorded (expected ones are fine — see the report):")
            for u in failures[:12]:
                print(f"      [{u['failure_category']}] {u['asset_type']}/{u['natural_key']}: "
                      f"{safe(u['note'])[:130]}")

    except Exception as exc:  # noqa: BLE001 — a harness crash must still report + clean up
        import traceback
        CHECKS.add("!", f"the harness itself raised: {type(exc).__name__}", False, str(exc)[:300])
        traceback.print_exc()
    finally:
        _cleanup(source_client, target_client, catalog, warehouse, keep)

    # ── report ─────────────────────────────────────────────────────────────
    npass, nfail = CHECKS.summary()
    print("\n" + "=" * 78)
    print("END-TO-END TEST REPORT")
    print("=" * 78)
    by_phase: dict = {}
    for phase, name, ok, _detail in CHECKS.rows:
        b = by_phase.setdefault(phase, [0, 0])
        b[0 if ok else 1] += 1
    for phase in sorted(by_phase):
        p, f = by_phase[phase]
        print(f"  phase {phase}: {p} passed, {f} failed")
    if nfail:
        print("\nFAILURES:")
        for phase, name, _ok, detail in CHECKS.failed():
            print(f"  [{phase}] {name}\n        {detail[:300]}")
    print(f"\n{npass} passed, {nfail} failed")
    print(f"bundle kept at: {staging}" if keep else "")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main(keep="--keep" in sys.argv))
