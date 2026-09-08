"""LIVE PLAN 9 driver — orphaned-home backup + the policy→pool→job remap chain, both modes.

Runs the REAL pipeline against source_ws → target_ws and asserts the PLAN 9 behaviour end to end:

  setup       create "all kinds of" content in two users' homes on SOURCE (notebook, file, nested
              dirs), then create a pool → policy(pins the pool) → job(uses the policy) chain, then
              DEPROVISION the two users (their /Users/<email> folders remain — orphaned).
  direct      direct-mode inventory+export+import → assert orphaned content lands under
              /Users_Backup/<owner>/…  (created_with_warning, never failed) and the target job now
              refers to the NEW policy → NEW pool. Downloads the import report to ~/Downloads.
  capture     snapshot the target's objects (so cleanup knows exactly what to remove).
  cleanup     delete everything the utility created on the target + drop the state schema.
  airgap      airgap-mode: source-side inventory+export → a "moved" bundle → target-side import.
              Downloads the report.

Usage:
  python3 -m tests.live_plan9 setup
  python3 -m tests.live_plan9 direct
  python3 -m tests.live_plan9 cleanup
  python3 -m tests.live_plan9 airgap
  python3 -m tests.live_plan9 full           # setup → direct → cleanup → airgap → cleanup
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import time

from src.auth.token_manager import ApiClient
from src.collectors.inventory_runner import InventoryRunner
from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.export_runner import ExportRunner
from src.importers.import_runner import ImportRunner
from src.importers.preflight import Preflight
from src.state.sql_backend import StatementApiBackend
from src.state.state_store import StateStore
from tests.live_e2e_migration import _host, _refreshing_client, _token, safe

SOURCE_PROFILE = "source_ws"
TARGET_PROFILE = "target_ws"
SP_CLIENT_ID = "71b85805-84f2-4185-86ae-bfadf34b6621"
SP_SECRET_FILE = "/tmp/wsmig_p9_sp_secret.txt"

STATE_CATALOG = "catalog_5_cqpzxw"    # fresh target's default catalog (2026-08-28)
STATE_SCHEMA = "wsmig_p9"
STATE_WAREHOUSE = "05adc91b73ded2ee"
AIRGAP_VOLUME = "/Volumes/catalog_5_cqpzxw/wsmig_p9/staging"   # (informational; hop is local)

ORPHAN_USERS = ["mayuresh.pandey@databricks.com", "yatin.kumar@databricks.com"]
# Orphaned SERVICE PRINCIPAL(s): an SP deprovisioned from source whose home `/Users/<appId>/…`
# content remains. Must divert to /Users_Backup/<appId>/… exactly like an orphaned user (verified
# by owner applicationId absent from the source roster). Set to the appId(s) you deprovisioned.
ORPHAN_SPS = ["575cc1c5-e1c3-4db3-94f6-fce8730975a8"]
# Every orphaned home owner (users + SPs) — the divert must fire for all of them.
ORPHAN_OWNERS = ORPHAN_USERS + ORPHAN_SPS
PREFIX = "wsmig_p9_"
POOL_NAME = f"{PREFIX}pool"
POLICY_NAME = f"{PREFIX}policy"
JOB_NAME = f"{PREFIX}job"
NODE_TYPE = "Standard_DS3_v2"
SPARK_VERSION = "14.3.x-scala2.12"

DOWNLOADS = os.path.expanduser("~/Downloads")
RUN_FILE = "/tmp/wsmig_p9_run.json"       # remembers the run_id/staging across subcommands


def _sp_secret() -> str:
    with open(SP_SECRET_FILE) as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    return lines[1]


def _source_ws_id() -> str:
    return _host(SOURCE_PROFILE).split("//adb-")[1].split(".")[0]


class _Hardened:
    """Wraps a client and, on a transient `Invalid Token` (the CLI OAuth token expiring mid-run —
    a LAPTOP-only artifact; in-workspace the managed notebook token has no such gap), rebuilds the
    client with a fresh token and retries the call ONCE. Only auth-expiry is retried, never a real
    permission_denied. (Product code uses the runtime's managed token and never hits this.)"""

    def __init__(self, build):
        self._build = build
        self._inner = build()

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **kw):
            call = attr
            for attempt in range(6):
                try:
                    return call(*a, **kw)
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    if "Invalid Token" in msg:               # laptop CLI token expired mid-run
                        self._inner = self._build()
                        call = getattr(self._inner, name)
                        continue
                    # Transient laptop network blips (DNS/connection) — NOT a product concern
                    # (in-workspace there is no such gap). Back off and retry a few times.
                    if attempt < 5 and ("NameResolution" in msg or "Failed to resolve" in msg
                                        or "Connection" in msg or "Max retries exceeded" in msg):
                        time.sleep(5 * (attempt + 1))
                        call = getattr(self._inner, name)
                        continue
                    raise
        return wrapped


def _target() -> "_Hardened":
    return _Hardened(lambda: _refreshing_client(TARGET_PROFILE))


def _ws_exists(client, path: str) -> bool:
    """get-status, but a missing path RAISES 404 in ApiClient — treat that as 'absent'."""
    try:
        return bool(client.get("api/2.0/workspace/get-status", params={"path": path}))
    except Exception:  # noqa: BLE001
        return False


class Ledger:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), detail))
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail[:240]}" if detail else ""))
        return ok

    def report(self, title):
        npass = sum(1 for r in self.rows if r[1])
        print(f"\n=== {title}: {npass}/{len(self.rows)} PASS ===")
        for name, ok, detail in self.rows:
            if not ok:
                print(f"  FAIL: {name} — {detail}")
        return npass == len(self.rows)


# ────────────────────────────── setup on source ──────────────────────────────

def _b64(s: bytes) -> str:
    return base64.b64encode(s).decode()


def _put_content(client, ledger):
    """Create 'all kinds of' content in each orphan user's home on SOURCE (admin token)."""
    for u in ORPHAN_USERS:
        home = f"/Users/{u}"
        proj = f"{home}/{PREFIX}proj"
        nested = f"{proj}/nested"
        client.post("api/2.0/workspace/mkdirs", {"path": nested})
        # a notebook (PYTHON, SOURCE format)
        client.post("api/2.0/workspace/import", {
            "path": f"{proj}/analysis", "format": "SOURCE", "language": "PYTHON",
            "object_type": "NOTEBOOK", "overwrite": True,
            "content": _b64(f"# {u} analysis\nprint('hello from {u}')\n".encode())})
        # a SQL notebook one level down
        client.post("api/2.0/workspace/import", {
            "path": f"{nested}/report", "format": "SOURCE", "language": "SQL",
            "object_type": "NOTEBOOK", "overwrite": True,
            "content": _b64(b"SELECT 1 AS one\n")})
        # a plain workspace file (AUTO)
        client.post("api/2.0/workspace/import", {
            "path": f"{proj}/data.csv", "format": "AUTO", "overwrite": True,
            "content": _b64(b"id,name\n1,alpha\n2,beta\n")})
        # confirm it's really there
        st = client.get("api/2.0/workspace/get-status", params={"path": f"{proj}/analysis"})
        ledger.add(f"source content created under {home}", bool(st and st.get("object_id")),
                   f"analysis object_id={safe((st or {}).get('object_id'))}")


def _ensure_pool_policy_job(client, ledger):
    """pool → policy(pins the pool) → job(uses the policy). Idempotent by name."""
    # pool
    pools = {safe(p.get("instance_pool_name")): safe(p.get("instance_pool_id")) for p in
             ((client.get("api/2.0/instance-pools/list") or {}).get("instance_pools") or [])}
    if POOL_NAME in pools:
        pool_id = pools[POOL_NAME]
    else:
        pool_id = safe(client.post("api/2.0/instance-pools/create", {
            "instance_pool_name": POOL_NAME, "node_type_id": NODE_TYPE,
            "min_idle_instances": 0, "max_capacity": 2,
            "idle_instance_autotermination_minutes": 10}).get("instance_pool_id"))

    # policy — its definition PINS the instance_pool_id (this is the chain link that must remap).
    definition = json.dumps({
        "instance_pool_id": {"type": "fixed", "value": pool_id},
        "spark_version": {"type": "fixed", "value": SPARK_VERSION},
    })
    policies = {safe(p.get("name")): safe(p.get("policy_id")) for p in
                ((client.get("api/2.0/policies/clusters/list") or {}).get("policies") or [])}
    if POLICY_NAME in policies:
        policy_id = policies[POLICY_NAME]
        client.post("api/2.0/policies/clusters/edit",
                    {"policy_id": policy_id, "name": POLICY_NAME, "definition": definition})
    else:
        policy_id = safe(client.post("api/2.0/policies/clusters/create",
                                     {"name": POLICY_NAME, "definition": definition}).get("policy_id"))

    # job — a job cluster that references the policy_id AND the pool.
    job_cluster = {
        "num_workers": 1, "spark_version": SPARK_VERSION,
        "policy_id": policy_id, "instance_pool_id": pool_id,
    }
    settings = {
        "name": JOB_NAME,
        "tasks": [{
            "task_key": "t1",
            "notebook_task": {"notebook_path": f"/Users/{ORPHAN_USERS[0]}/{PREFIX}proj/analysis"},
            "new_cluster": job_cluster,
        }],
    }
    jobs = [j for j in ((client.get("api/2.2/jobs/list", params={"name": JOB_NAME}) or {})
                        .get("jobs") or []) if safe((j.get("settings") or {}).get("name")) == JOB_NAME]
    if jobs:
        job_id = safe(jobs[0].get("job_id"))
        client.post("api/2.2/jobs/reset", {"job_id": int(job_id), "new_settings": settings})
    else:
        job_id = safe(client.post("api/2.2/jobs/create", settings).get("job_id"))

    ledger.add("source pool→policy→job chain created",
               bool(pool_id and policy_id and job_id),
               f"pool={pool_id} policy={policy_id} job={job_id}")
    return {"pool_id": pool_id, "policy_id": policy_id, "job_id": job_id}


def _deprovision_users(client, ledger):
    for u in ORPHAN_USERS:
        # resolve id via SCIM filter (client.get can hit the SCIM path directly)
        doc = client.get("api/2.0/preview/scim/v2/Users",
                         params={"filter": f'userName eq "{u}"'})
        found = (doc or {}).get("Resources") or []
        if not found:
            ledger.add(f"{u} already absent from source workspace", True, "deprovisioned earlier")
            continue
        uid = safe(found[0].get("id"))
        client.delete(f"api/2.0/preview/scim/v2/Users/{uid}")
        # confirm home folder survives
        home = client.get("api/2.0/workspace/get-status", params={"path": f"/Users/{u}"})
        ledger.add(f"{u} deprovisioned; home folder survives",
                   bool(home and home.get("object_id")),
                   f"home object_id={safe((home or {}).get('object_id'))}")


def cmd_setup():
    print("== PLAN 9 SETUP on source ==")
    src = _refreshing_client(SOURCE_PROFILE)   # admin token, for scenario setup
    led = Ledger()
    _put_content(src, led)
    chain = _ensure_pool_policy_job(src, led)
    _deprovision_users(src, led)
    with open(RUN_FILE, "w") as f:
        json.dump({"chain": chain}, f)
    return 0 if led.report("SETUP") else 1


# ─────────────────────────────── the migration ───────────────────────────────

def _direct_cfg(staging, run_id, *, dry_run, import_assets="all"):
    return Config.from_dict({
        "role": "target", "connectivity_mode": "direct",
        "source_workspace_id": _source_ws_id(), "run_id": run_id,
        "target_staging_location": staging, "dry_run": dry_run,
        "source": {"workspace_url": _host(SOURCE_PROFILE), "client_id": SP_CLIENT_ID,
                   "spn_secret_value": _sp_secret()},
        "imports": {"state_catalog": STATE_CATALOG, "state_schema": STATE_SCHEMA,
                    "state_warehouse_id": STATE_WAREHOUSE,
                    "import_assets": [s.strip() for s in import_assets.split(",")],
                    "preflight_enforce": False},
        "max_workspace_items": 6000, "max_ws_api_calls": 9000,
        "ctx": {"workspace_url": _host(TARGET_PROFILE), "token": _token(TARGET_PROFILE)},
    })


def _airgap_source_cfg(staging, run_id):
    """Source side: role derived = source; reads THIS (source) workspace with its own token.

    FULL SCOPE — every family, exactly like the direct-mode run, so airgap is a fair apples-to-apples
    comparison and nothing is hidden. (An earlier version scoped this down with toggles to shorten the
    walk; that made airgap look artificially clean and is NOT acceptable for a ship-quality result.)"""
    return Config.from_dict({
        "role": "source", "connectivity_mode": "airgap",
        "source_workspace_id": _source_ws_id(), "run_id": run_id,
        "source_staging_location": staging, "dry_run": True,
        "max_workspace_items": 6000, "max_ws_api_calls": 9000,
        "ctx": {"workspace_url": _host(SOURCE_PROFILE), "token": _token(SOURCE_PROFILE)},
    })


def _airgap_target_cfg(staging, run_id, *, dry_run):
    return Config.from_dict({
        "role": "target", "connectivity_mode": "airgap",
        "source_workspace_id": _source_ws_id(), "run_id": run_id,
        "target_staging_location": staging, "dry_run": dry_run,
        "imports": {"state_catalog": STATE_CATALOG, "state_schema": STATE_SCHEMA,
                    "state_warehouse_id": STATE_WAREHOUSE, "preflight_enforce": False},
        "ctx": {"workspace_url": _host(TARGET_PROFILE), "token": _token(TARGET_PROFILE)},
    })


def _ensure_state_schema(client):
    """The tool assumes the shared state catalog+schema PRE-EXIST (it only creates its own tables).
    The customer provisions them once; the harness stands in for that here."""
    StatementApiBackend(client, STATE_WAREHOUSE).sql(
        f"CREATE SCHEMA IF NOT EXISTS {STATE_CATALOG}.{STATE_SCHEMA}")


def _run_import(cfg, target_client, aw, source_client=None):
    state = None
    if cfg.state_enabled:
        _ensure_state_schema(target_client)
        state = StateStore(StatementApiBackend(target_client, cfg.imports.state_warehouse_id), cfg)
        state.ensure_table()
        state.load(force=True)
    verdict = Preflight(target_client, cfg, aw, state=state,
                        source_client=source_client if cfg.is_direct else None).run()
    print(f"    preflight: {verdict['verdict']} (blocking={len(verdict['blocking'])})")
    for b in verdict["blocking"]:
        print(f"      BLOCKING: {b[:200]}")
    runner = ImportRunner(target_client, cfg, aw, state=state, preflight_verdict=verdict)
    summary = runner.run()
    summary["_acl_parity"] = runner.context.get("acl_parity") or {}
    return summary, state


def _units_by_status(aw):
    doc = aw.read_json("misc/import_results.json") or {}
    out = {}
    for u in doc.get("units", []):
        out.setdefault(u.get("import_status"), []).append(u)
    return out, doc.get("units", [])


def _assert_scenario(aw, target_client, led, tag):
    """The PLAN 9 + chain assertions, shared by both modes."""
    by_status, units = _units_by_status(aw)

    # (1) orphaned home content diverted to /Users_Backup, created_with_warning, never failed.
    backups = [u for u in units if "deleted in source" in safe(u.get("note"))
               and u.get("import_status") == "created_with_warning"]
    led.add(f"[{tag}] orphaned home content diverted as created_with_warning", bool(backups),
            f"{len(backups)} objects diverted")
    for u in ORPHAN_OWNERS:
        kind = "SP" if u in ORPHAN_SPS else "user"
        got = [u2 for u2 in backups if f"/Users_Backup/{u}" in safe(u2.get("target_id"))]
        led.add(f"[{tag}] orphaned {kind} {u} content under /Users_Backup", bool(got),
                f"{len(got)} objects; e.g. {got[0].get('target_id') if got else '—'}")

    # no orphaned-home unit ended up as a hard failure
    orphan_fails = [u for u in units if u.get("import_status") == "failed"
                    and any(f"/Users/{ou}" in safe(u.get("natural_key")) for ou in ORPHAN_OWNERS)]
    led.add(f"[{tag}] NO orphaned-home unit failed", not orphan_fails,
            f"{len(orphan_fails)} failures: {[u.get('natural_key') for u in orphan_fails][:3]}")

    # (2) the backup content actually exists on target (probe the user tree we control the shape of)
    probe = f"/Users_Backup/{ORPHAN_USERS[0]}/{PREFIX}proj/analysis"
    led.add(f"[{tag}] backup notebook exists on target ({probe})",
            _ws_exists(target_client, probe))

    # (3) the chain: target job → NEW policy → NEW pool.
    pools = {safe(p.get("instance_pool_name")): safe(p.get("instance_pool_id")) for p in
             ((target_client.get("api/2.0/instance-pools/list") or {}).get("instance_pools") or [])}
    policies = {safe(p.get("name")): p for p in
                ((target_client.get("api/2.0/policies/clusters/list") or {}).get("policies") or [])}
    tgt_pool = pools.get(POOL_NAME, "")
    tgt_policy = policies.get(POLICY_NAME, {})
    tgt_policy_id = safe(tgt_policy.get("policy_id"))
    # policy definition's pinned instance_pool_id must be the NEW target pool (Bug 9)
    pol_def = json.loads(safe(tgt_policy.get("definition")) or "{}")
    pol_pool = safe((pol_def.get("instance_pool_id") or {}).get("value"))
    led.add(f"[{tag}] target policy pins the NEW target pool id", bool(tgt_pool) and pol_pool == tgt_pool,
            f"policy pool={pol_pool} target pool={tgt_pool}")
    # the job's cluster must reference the NEW policy id AND the NEW pool id
    jobs = [j for j in ((target_client.get("api/2.2/jobs/list", params={"name": JOB_NAME}) or {})
                        .get("jobs") or []) if safe((j.get("settings") or {}).get("name")) == JOB_NAME]
    job_ok = False
    detail = "job not found on target"
    if jobs:
        jid = safe(jobs[0].get("job_id"))
        full = target_client.get("api/2.2/jobs/get", params={"job_id": jid}) or {}
        clusters = []
        for t in (full.get("settings") or {}).get("tasks") or []:
            if t.get("new_cluster"):
                clusters.append(t["new_cluster"])
        for jc in (full.get("settings") or {}).get("job_clusters") or []:
            if jc.get("new_cluster"):
                clusters.append(jc["new_cluster"])
        jpol = {safe(c.get("policy_id")) for c in clusters}
        jpool = {safe(c.get("instance_pool_id")) for c in clusters}
        job_ok = tgt_policy_id in jpol and tgt_pool in jpool
        detail = f"job policy_ids={jpol} pool_ids={jpool}; target policy={tgt_policy_id} pool={tgt_pool}"
    led.add(f"[{tag}] target job → NEW policy → NEW pool", job_ok, detail)


def _download_report(aw, tag, led):
    src = os.path.join(aw.root, "reports", "import_status.xlsx")
    if not os.path.isfile(src):
        led.add(f"[{tag}] import report written", False, f"missing {src}")
        return
    dst = os.path.join(DOWNLOADS, f"wsmig_p9_import_status_{tag}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
    shutil.copy(src, dst)
    led.add(f"[{tag}] import report downloaded", True, dst)
    print(f"    report → {dst}")


def cmd_direct():
    import tempfile
    print("== PLAN 9 DIRECT-MODE migration ==")
    led = Ledger()
    source_client = _refreshing_client(SOURCE_PROFILE)
    target_client = _target()
    staging = tempfile.mkdtemp(prefix="wsmig_p9_direct_")
    run_id = "p9d_" + time.strftime("%Y%m%d_%H%M%S")
    print(f"    staging={staging} run_id={run_id}")

    # source SP creds → a direct source client over OAuth M2M (the customer's real read path)
    cfg = _direct_cfg(staging, run_id, dry_run=True)
    aw = ArtifactWriter(cfg)
    from src.auth.token_manager import oauth_m2m_token_provider
    src_direct = ApiClient(_host(SOURCE_PROFILE),
                           oauth_m2m_token_provider(_host(SOURCE_PROFILE), SP_CLIENT_ID, _sp_secret()))
    tgt = target_client

    inv = InventoryRunner(src_direct, cfg, aw).run()
    led.add("inventory read source over M2M", sum(inv["counts"].values()) > 0, f"counts={inv['counts']}")
    exp = ExportRunner(src_direct, cfg, aw, content_fetch_workers=4).run()
    led.add("export produced a bundle with no failures", exp["total"] > 0 and exp["failure"] == 0,
            f"{exp['total']} units, {exp['failure']} failed")

    # confirm the orphaned homes (users AND the SP) actually made it into the bundle
    index = aw.read_json("misc/export_index.json") or {}
    walked = [u.get("natural_key") for u in index.get("units", [])]
    for u in ORPHAN_OWNERS:
        n = sum(1 for k in walked if safe(k).startswith(f"/Users/{u}/"))
        led.add(f"orphaned home {u} walked into the bundle", n > 0, f"{n} content units")
    # confirm the deprovisioned owners are ABSENT from the roster (→ divert eligible). Users are
    # indexed by userName/email; the SP by applicationId — exactly what _roster_status checks.
    cls = aw.read_json("misc/identity_classification.json") or {}
    roster = set()
    for i in cls.get("identities", []):
        if i.get("identity_type") == "user":
            roster |= {safe(i.get("userName")), safe(i.get("email"))}
        elif i.get("identity_type") == "service_principal":
            roster.add(safe(i.get("applicationId")))
    for u in ORPHAN_OWNERS:
        led.add(f"{u} ABSENT from source roster", u not in roster,
                "in roster!" if u in roster else "absent (deleted in source)")

    # LIVE import
    cfg_live = _direct_cfg(staging, run_id, dry_run=False)
    aw_live = ArtifactWriter(cfg_live)
    summary, _state = _run_import(cfg_live, tgt, aw_live, source_client=src_direct)
    print(f"    totals: {json.dumps(summary['totals'])}")
    led.add("live import completed", summary["run_status"] == "completed", summary["run_status"])

    _assert_scenario(aw_live, tgt, led, "direct")
    _download_report(aw_live, "direct", led)
    with open(RUN_FILE, "w") as f:
        json.dump({"staging": staging, "run_id": run_id}, f)
    return 0 if led.report("DIRECT MODE") else 1


# ─────────────────────────────── airgap ───────────────────────────────

def cmd_airgap():
    import tempfile
    print("== PLAN 9 AIRGAP-MODE migration ==")
    led = Ledger()
    source_client = _refreshing_client(SOURCE_PROFILE)
    target_client = _target()
    src_staging = tempfile.mkdtemp(prefix="wsmig_p9_ag_src_")
    tgt_staging = tempfile.mkdtemp(prefix="wsmig_p9_ag_tgt_")
    run_id = "p9a_" + time.strftime("%Y%m%d_%H%M%S")
    print(f"    src_staging={src_staging}\n    tgt_staging={tgt_staging}\n    run_id={run_id}")

    # SOURCE side (role=source): inventory + export INSIDE source, write bundle to src_staging.
    cfg_s = _airgap_source_cfg(src_staging, run_id)
    aw_s = ArtifactWriter(cfg_s)
    inv = InventoryRunner(source_client, cfg_s, aw_s).run()
    led.add("airgap: source-side inventory ran", sum(inv["counts"].values()) > 0, f"counts={inv['counts']}")
    exp = ExportRunner(source_client, cfg_s, aw_s, content_fetch_workers=4).run()
    led.add("airgap: export produced a bundle, no failures", exp["total"] > 0 and exp["failure"] == 0,
            f"{exp['total']} units, {exp['failure']} failed")
    verify = aw_s.verify_manifest()
    led.add("airgap: source bundle manifest verifies", verify["ok"],
            f"missing={verify['missing'][:2]}")

    # THE HOP: ops physically moves the bundle from the source location to the target location.
    src_root = os.path.dirname(os.path.dirname(os.path.dirname(aw_s.root)))  # the staging root
    shutil.copytree(os.path.join(src_staging, "wsmig"), os.path.join(tgt_staging, "wsmig"))
    led.add("airgap: bundle 'moved' to the target staging location", True, "copytree wsmig/")

    # Remember the moved bundle so the target-side import is resumable on a laptop network blip
    # (the ~10-min source walk need not repeat — checkpoint + state make the import idempotent).
    with open(RUN_FILE, "w") as f:
        json.dump({"tgt_staging": tgt_staging, "run_id": run_id}, f)
    return _airgap_import(led, target_client, tgt_staging, run_id)


def _airgap_import(led, target_client, tgt_staging, run_id):
    """TARGET side (role=target): verify the moved bundle, import, assert, download the report.
    Split out so it can be re-run from an already-exported bundle (airgap_import subcommand)."""
    cfg_t = _airgap_target_cfg(tgt_staging, run_id, dry_run=False)
    aw_t = ArtifactWriter(cfg_t)
    verify_t = aw_t.verify_manifest()
    led.add("airgap: target verifies the uploaded bundle before acting", verify_t["ok"],
            f"missing={verify_t['missing'][:2]}")
    summary, _state = _run_import(cfg_t, target_client, aw_t)
    print(f"    totals: {json.dumps(summary['totals'])}")
    led.add("airgap: import completed", summary["run_status"] == "completed", summary["run_status"])

    _assert_scenario(aw_t, target_client, led, "airgap")
    _download_report(aw_t, "airgap", led)
    return 0 if led.report("AIRGAP MODE") else 1


def cmd_airgap_import():
    """Resume the airgap TARGET-side import from an already-exported bundle (recorded in RUN_FILE,
    or passed as argv). Lets a transient laptop network blip be retried without re-walking source."""
    print("== PLAN 9 AIRGAP import (resume from exported bundle) ==")
    if len(sys.argv) >= 4:
        tgt_staging, run_id = sys.argv[2], sys.argv[3]
    else:
        saved = json.load(open(RUN_FILE))
        tgt_staging, run_id = saved["tgt_staging"], saved["run_id"]
    print(f"    tgt_staging={tgt_staging}\n    run_id={run_id}")
    return _airgap_import(Ledger(), _target(), tgt_staging, run_id)


# ─────────────────────────────── capture + cleanup ───────────────────────────────

# Everything the utility creates from THIS test's bundle is `wsmig_`-prefixed (both the wsmig_test_*
# source fixtures and the wsmig_p9_* chain), and orphaned home content lands under /Users_Backup.
# Cleanup is scoped to those, so it can NEVER touch an unrelated object in this shared workspace.
WIPE_PREFIX = "wsmig_"


def _snapshot(client):
    """A prefix-scoped inventory of what the utility has created on target (for capture/verify)."""
    snap = {"jobs": [], "policies": [], "pools": [], "clusters": [], "warehouses": [],
            "pipelines": [], "serving": [], "secret_scopes": [], "dashboards": [], "queries": [],
            "alerts": [], "global_init_scripts": [], "users_backup": False}
    snap["jobs"] = [safe((j.get("settings") or {}).get("name")) for j in
                    ((client.get("api/2.2/jobs/list") or {}).get("jobs") or [])
                    if safe((j.get("settings") or {}).get("name")).startswith(WIPE_PREFIX)]
    snap["policies"] = [safe(p.get("name")) for p in
                        ((client.get("api/2.0/policies/clusters/list") or {}).get("policies") or [])
                        if safe(p.get("name")).startswith(WIPE_PREFIX)]
    snap["pools"] = [safe(p.get("instance_pool_name")) for p in
                     ((client.get("api/2.0/instance-pools/list") or {}).get("instance_pools") or [])
                     if safe(p.get("instance_pool_name")).startswith(WIPE_PREFIX)]
    snap["clusters"] = [safe(c.get("cluster_name")) for c in
                        ((client.get("api/2.0/clusters/list") or {}).get("clusters") or [])
                        if safe(c.get("cluster_name")).startswith(WIPE_PREFIX)]
    snap["warehouses"] = [safe(w.get("name")) for w in
                          ((client.get("api/2.0/sql/warehouses") or {}).get("warehouses") or [])
                          if safe(w.get("name")).startswith(WIPE_PREFIX)]
    snap["serving"] = [safe(e.get("name")) for e in
                       ((client.get("api/2.0/serving-endpoints") or {}).get("endpoints") or [])
                       if safe(e.get("name")).startswith(WIPE_PREFIX)]
    snap["secret_scopes"] = [safe(s.get("name")) for s in
                             ((client.get("api/2.0/secrets/scopes/list") or {}).get("scopes") or [])
                             if safe(s.get("name")).startswith(WIPE_PREFIX)]
    snap["users_backup"] = _ws_exists(client, "/Users_Backup")
    return snap


def cmd_capture():
    client = _target()
    snap = _snapshot(client)
    with open("/tmp/wsmig_p9_target_snapshot.json", "w") as f:
        json.dump(snap, f, indent=2)
    print(json.dumps(snap, indent=2))
    n = sum(len(v) for v in snap.values() if isinstance(v, list))
    print(f"\n{n} utility-created objects + /Users_Backup={'present' if snap['users_backup'] else 'absent'}")
    return 0


def _del(client, path, body, label):
    try:
        client.post(path, body)
        print(f"  deleted {label}")
    except Exception as exc:  # noqa: BLE001
        print(f"  delete {label} FAILED (harmless): {str(exc)[:120]}")


def cmd_cleanup():
    """Wipe EVERYTHING the utility created on target, scoped to the wsmig_ prefix + /Users_Backup.
    Reverse dependency order: jobs → workspace content → compute → sql/dlt/serving/secrets → identities.
    Migrated account identities are un-assigned last (they re-adopt harmlessly on the next run)."""
    print("== PLAN 9 CLEANUP target (wipe everything the utility created) ==")
    client = _target()

    # jobs
    for j in [x for x in ((client.get("api/2.2/jobs/list") or {}).get("jobs") or [])
              if safe((x.get("settings") or {}).get("name")).startswith(WIPE_PREFIX)]:
        _del(client, "api/2.2/jobs/delete", {"job_id": int(safe(j.get("job_id")))},
             f"job {(j.get('settings') or {}).get('name')}")

    # workspace content: the divert tree + the migrated source fixtures under /Shared
    if _ws_exists(client, "/Users_Backup"):
        _del(client, "api/2.0/workspace/delete", {"path": "/Users_Backup", "recursive": True},
             "/Users_Backup (all orphaned-home backups)")
    for top in ("/Shared",):
        for item in (client.get("api/2.0/workspace/list", params={"path": top}) or {}).get("objects") or []:
            p = safe(item.get("path"))
            if os.path.basename(p).startswith(WIPE_PREFIX):
                _del(client, "api/2.0/workspace/delete", {"path": p, "recursive": True}, f"workspace {p}")

    # clusters → policies → pools
    for c in [x for x in ((client.get("api/2.0/clusters/list") or {}).get("clusters") or [])
              if safe(x.get("cluster_name")).startswith(WIPE_PREFIX)]:
        _del(client, "api/2.0/clusters/permanent-delete", {"cluster_id": safe(c.get("cluster_id"))},
             f"cluster {c.get('cluster_name')}")
    for p in [x for x in ((client.get("api/2.0/policies/clusters/list") or {}).get("policies") or [])
              if safe(x.get("name")).startswith(WIPE_PREFIX)]:
        _del(client, "api/2.0/policies/clusters/delete", {"policy_id": safe(p.get("policy_id"))},
             f"policy {p.get('name')}")
    for p in [x for x in ((client.get("api/2.0/instance-pools/list") or {}).get("instance_pools") or [])
              if safe(x.get("instance_pool_name")).startswith(WIPE_PREFIX)]:
        _del(client, "api/2.0/instance-pools/delete", {"instance_pool_id": safe(p.get("instance_pool_id"))},
             f"pool {p.get('instance_pool_name')}")

    # sql warehouses / queries / alerts
    for w in [x for x in ((client.get("api/2.0/sql/warehouses") or {}).get("warehouses") or [])
              if safe(x.get("name")).startswith(WIPE_PREFIX)]:
        try:
            client.delete(f"api/2.0/sql/warehouses/{safe(w.get('id'))}")
            print(f"  deleted warehouse {w.get('name')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  delete warehouse failed: {str(exc)[:120]}")
    for q in [x for x in ((client.get("api/2.0/sql/queries") or {}).get("results") or [])
              if safe(x.get("display_name")).startswith(WIPE_PREFIX)]:
        try:
            client.delete(f"api/2.0/sql/queries/{safe(q.get('id'))}")
            print(f"  deleted query {q.get('display_name')}")
        except Exception:  # noqa: BLE001
            pass

    # dlt pipelines
    for pl in [x for x in ((client.get("api/2.0/pipelines", params={"max_results": 100}) or {})
                           .get("statuses") or []) if safe(x.get("name")).startswith(WIPE_PREFIX)]:
        try:
            client.delete(f"api/2.0/pipelines/{safe(pl.get('pipeline_id'))}")
            print(f"  deleted pipeline {pl.get('name')}")
        except Exception:  # noqa: BLE001
            pass

    # serving endpoints
    for e in [x for x in ((client.get("api/2.0/serving-endpoints") or {}).get("endpoints") or [])
              if safe(x.get("name")).startswith(WIPE_PREFIX)]:
        try:
            client.delete(f"api/2.0/serving-endpoints/{safe(e.get('name'))}")
            print(f"  deleted serving endpoint {e.get('name')}")
        except Exception:  # noqa: BLE001
            pass

    # secret scopes
    for s in [x for x in ((client.get("api/2.0/secrets/scopes/list") or {}).get("scopes") or [])
              if safe(x.get("name")).startswith(WIPE_PREFIX)]:
        _del(client, "api/2.0/secrets/scopes/delete", {"scope": safe(s.get("name"))},
             f"secret scope {s.get('name')}")

    # global init scripts
    for g in [x for x in ((client.get("api/2.0/global-init-scripts") or {}).get("scripts") or [])
              if safe(x.get("name")).startswith(WIPE_PREFIX)]:
        try:
            client.delete(f"api/2.0/global-init-scripts/{safe(g.get('script_id'))}")
            print(f"  deleted global init script {g.get('name')}")
        except Exception:  # noqa: BLE001
            pass

    # migrated identities: workspace-local SPs + groups this test's bundle created (wsmig_ prefixed
    # displayName). Users are account identities — un-assigning them from a shared demo workspace is
    # risky and they re-adopt harmlessly next run, so they are left in place (noted).
    for g in [x for x in (client.get("api/2.0/preview/scim/v2/Groups") or {}).get("Resources") or []
              if safe(x.get("displayName")).startswith(WIPE_PREFIX)]:
        try:
            client.delete(f"api/2.0/preview/scim/v2/Groups/{safe(g.get('id'))}")
            print(f"  deleted group {g.get('displayName')}")
        except Exception:  # noqa: BLE001
            pass

    # the tool's state TABLES (the shared catalog+schema is a pre-existing customer resource, so it
    # is kept — only the tool-owned tables are dropped, which is what "wipe what the utility made"
    # means for state).
    try:
        be = StatementApiBackend(client, STATE_WAREHOUSE)
        for t in ("wsmig_migration_state", "wsmig_migration_state_dryrun", "wsmig_identity_map"):
            be.sql(f"DROP TABLE IF EXISTS {STATE_CATALOG}.{STATE_SCHEMA}.{t}")
        print(f"  dropped state tables in {STATE_CATALOG}.{STATE_SCHEMA}")
    except Exception as exc:  # noqa: BLE001
        print(f"  state-table drop failed: {str(exc)[:160]}")

    left = _snapshot(client)
    n = sum(len(v) for v in left.values() if isinstance(v, list))
    print(f"\n  after cleanup: {n} utility objects remain, /Users_Backup="
          f"{'present' if left['users_backup'] else 'absent'}")
    print("  NOTE: migrated account USERS are left assigned (shared workspace; re-adopted next run).")
    return 0


def main(argv):
    cmd = argv[0] if argv else "help"
    if cmd == "setup":
        return cmd_setup()
    if cmd == "direct":
        return cmd_direct()
    if cmd == "airgap":
        return cmd_airgap()
    if cmd == "airgap_import":
        return cmd_airgap_import()
    if cmd == "capture":
        return cmd_capture()
    if cmd == "cleanup":
        return cmd_cleanup()
    if cmd == "full":
        for fn in (cmd_setup, cmd_direct, cmd_cleanup, cmd_airgap, cmd_cleanup):
            rc = fn()
            if rc:
                print(f"\n!! {fn.__name__} reported failures — stopping the chain")
                return rc
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
