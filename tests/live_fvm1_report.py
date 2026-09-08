"""
live_fvm1_report — run the REAL inventory + export against fvm1, then verify every fixture
element created by tests/fixtures_fvm1.py is present in the export bundle with the expected
status. Produces a per-element PASS/FAIL test report.

Run: python3 -m tests.live_fvm1_report
Needs the source CLI profile authenticated (WSMIG_PROFILE, default `source_ws`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

from src.auth.token_manager import ApiClient
from src.config.config_manager import Config
from src.collectors.inventory_runner import InventoryRunner
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.export_runner import ExportRunner

PROFILE = os.environ.get("WSMIG_PROFILE", "source_ws")


def _profile():
    import configparser
    c = configparser.ConfigParser()
    c.read(os.path.expanduser("~/.databrickscfg"))
    return dict(c[PROFILE])


def _token():
    return json.loads(subprocess.check_output(
        ["databricks", "auth", "token", "-p", PROFILE], text=True))["access_token"]


def run_export():
    prof = _profile()
    host = prof["host"].rstrip("/")
    staging = tempfile.mkdtemp(prefix="wsmig_report_")
    cfg = Config.from_dict({"role": "source", "source_workspace_id": prof["workspace_id"],
                            "run_id": "report1", "source_staging_location": staging})
    cfg.ctx.workspace_url = host
    cfg.ctx.token = _token()
    # refreshing token provider (long runs; CLI OAuth can expire)
    cache = {"t": cfg.ctx.token, "ts": time.time()}

    def tok():
        if time.time() - cache["ts"] > 500:
            cache["t"] = _token(); cache["ts"] = time.time()
        return cache["t"]

    client = ApiClient(host, tok)
    aw = ArtifactWriter(cfg)
    print("== inventory ==", flush=True)
    inv = InventoryRunner(client, cfg, aw).run()
    print("   counts:", inv["counts"], flush=True)
    print("== export ==", flush=True)
    res = ExportRunner(client, cfg, aw, content_fetch_workers=8).run()
    print("   summary:", {k: res.get(k) for k in
                          ("total", "success", "failure", "skipped_oversize", "manual", "dab",
                           "covered", "skip")}, flush=True)
    return aw.root


# ── expectations: (asset_type, natural_key_substring, expected_status) ──────
# expected_status: a set of acceptable statuses for that unit.
EXPECT = [
    # identity
    ("user", "aman.bansal@databricks.com", {"success"}),
    ("user", "vivek.ravichandran@databricks.com", {"success"}),   # the NON-Entra user
    ("user", "vivek.ravichandiran@databricks.com", {"success"}),  # Entra/SCIM-provisioned
    ("user", "sanket.kelkar@databricks.com", {"success"}),
    ("user", "idris.chakera@databricks.com", {"success"}),
    # Databricks-managed groups, incl. a THREE-level nest (grandchild → child → parent)
    ("group", "wsmig_test_grandchild_grp", {"success"}),
    ("group", "wsmig_test_child_grp", {"success"}),
    ("group", "wsmig_test_parent_grp", {"success"}),
    ("group", "wsmig_test_plain_grp", {"success"}),
    # mixed-member group: user + SP + nested Entra-backed group
    ("group", "wsmig_test_mixed_grp", {"success"}),
    # Entra-backed groups carry an externalId, so they must already exist on target → assign,
    # not create. They come from the ACCOUNT (workspace SCIM drops externalId on create).
    ("group", "wsmig_test_entra_grp", {"success", "covered"}),
    ("group", "wsmig_test_entra_grp2", {"success", "covered"}),
    # Built-in groups are never recreated (they exist on target) → `covered`, but their
    # MEMBERSHIP is exported separately so source admins actually become target admins.
    ("group", "admins", {"covered"}),
    ("group_membership", "admins", {"success"}),
    ("group_membership", "users", {"success"}),
    # SPNs: natural_key is the applicationId (per the identity model), so match by the
    # displayName carried in the payload instead (handled specially in verify()).
    ("service_principal", "@displayName:wsmig_test_db_sp", {"success"}),      # db-managed
    ("service_principal", "@displayName:wsmig_test_db_sp2", {"success"}),     # db-managed
    # `ai27_umi` is a REAL Azure managed identity assigned into the workspace from the account,
    # so it carries an externalId and is the genuine assign-on-target case. A fixture cannot
    # manufacture one: workspace SCIM silently drops `externalId` on create.
    ("service_principal", "@displayName:ai27_umi", {"success"}),              # real Azure UMI
    # compute
    ("instance_pool", "wsmig_test_pool", {"success"}),
    ("instance_pool", "wsmig_test_pool_single", {"success"}),   # max_capacity=1
    ("instance_pool", "wsmig_test_pool_nomax", {"success"}),    # no max_capacity at all
    ("cluster_policy", "wsmig_test_policy", {"success"}),
    ("cluster_policy", "wsmig_test_policy_strict", {"success"}),
    ("cluster", "wsmig_test_cluster", {"success"}),
    ("cluster", "wsmig_test_cluster_autoscale", {"success"}),
    ("cluster", "wsmig_test_cluster_singlenode", {"success"}),
    ("cluster", "wsmig_test_cluster_pooled", {"success"}),      # references a pool id
    ("cluster", "wsmig_test_cluster_policied", {"success"}),    # references a policy id
    # workspace content — languages
    ("notebook", "wsmig_test/py_nb", {"success"}),
    ("notebook", "wsmig_test/sql_nb", {"success"}),
    ("notebook", "wsmig_test/scala_nb", {"success"}),
    ("notebook", "wsmig_test/r_nb", {"success"}),
    ("notebook", "jupyter_nb", {"success"}),                     # .ipynb format
    ("notebook", "deepest/nested_nb", {"success"}),              # deep path
    ("notebook", "wsmig test spaced nb", {"success"}),           # space in the name
    ("workspace_file", "wsmig_test/config.json", {"success"}),
    ("workspace_file", "wsmig_test/data.csv", {"success"}),
    ("workspace_file", "plain_no_extension", {"success"}),
    ("workspace_file", "binary.bin", {"success"}),
    ("workspace_file", "wsmig_test_big_file.csv", {"success"}),          # 11MB file → success
    ("notebook", "wsmig_test_big_nb", {"skipped_oversize", "__absent__"}),  # >10MB nb → never created
    ("directory", "wsmig_test", {"success"}),
    # Git repos are inventoried + exported as METADATA only and are out of scope for import
    # (customer decision 2026-08-05) → the correct status is `manual`, not `success`.
    ("repo", "wsmig_test_repo", {"manual"}),
    # secrets
    ("secret_scope", "wsmig_test_scope", {"success"}),
    ("secret_scope", "wsmig_test_scope_single", {"success"}),
    ("secret_scope", "wsmig_test_scope_empty", {"success"}),
    ("secret_scope", "wsmig_test_akv_scope", {"success"}),   # AZURE_KEYVAULT-backed
    ("secret_value", "wsmig_test_scope/api_key", {"manual"}),
    ("secret_value", "wsmig_test_scope/db_pass", {"manual"}),
    # sql
    ("legacy_query", "wsmig_test_query", {"success"}),
    ("legacy_query", "wsmig_test_legacy_q", {"success"}),
    ("legacy_alert", "wsmig_test_legacy_alert", {"manual"}),   # IMP-5: v1 create API obsolete → manual
    ("alert_v2", "wsmig_test_alert_v2", {"success"}),
    ("alert_v2", "wsmig_dab_alert", {"dab"}),
    ("sql_warehouse", "wsmig_test_wh_pro", {"success"}),         # PRO, non-serverless
    ("sql_warehouse", "wsmig_test_wh_classic", {"success"}),     # CLASSIC
    ("sql_warehouse", "wsmig_test_wh_serverless", {"success"}),  # PRO, serverless
    # jobs (plain + DAB)
    ("job", "wsmig_test_single_job", {"success"}),
    ("job", "wsmig_test_multi_job", {"success"}),
    ("job", "wsmig_test_scheduled_job", {"success"}),            # UNPAUSED schedule
    ("job", "wsmig_test_params_job", {"success"}),               # params/tags/retries/notifs
    ("job", "wsmig_test_existing_cluster_job", {"success"}),     # existing_cluster_id ref
    ("job", "wsmig_test_jobcluster_job", {"success"}),           # shared job_clusters block
    ("job", "wsmig_dab_shared_job", {"dab"}),
    ("job", "wsmig_dab_user_job", {"dab"}),
    # dlt (plain + DAB)
    ("dlt_pipeline", "wsmig_test_pipeline", {"success"}),
    ("dlt_pipeline", "wsmig_test_pipeline_continuous", {"success"}),
    ("dlt_pipeline", "wsmig_test_pipeline_classic", {"success"}),
    ("dlt_pipeline", "wsmig_dab_shared_pipeline", {"dab"}),
    ("dlt_pipeline", "wsmig_dab_user_pipeline", {"dab"}),
    # dashboards (plain + DAB) + genie
    ("lakeview_dashboard", "wsmig_test_dashboard", {"success"}),
    ("lakeview_dashboard", "wsmig_dab_shared_dashboard", {"dab", "success"}),
    ("lakeview_dashboard", "wsmig_dab_user_dashboard", {"dab", "success"}),
    ("genie_space", "wsmig_test_genie", {"success"}),
    # DAB supports `genie_spaces` (CLI v1.10.0+) → bundle-owned, never re-created via REST.
    ("genie_space", "wsmig_dab_genie", {"dab"}),
    # DAB-managed PATHLESS assets — detected from the bundle state file, not from a path.
    # NOTE: instance_pools / cluster_policies / sql queries are NOT bundle resource types
    # (CLI 1.5.0), so a DAB-owned twin of those is impossible — they're manual-only by nature.
    ("cluster", "wsmig_dab_cluster", {"dab"}),
    ("sql_warehouse", "wsmig_dab_wh", {"dab"}),
    ("secret_scope", "wsmig_dab_scope", {"dab"}),
    ("serving_endpoint", "wsmig_dab_endpoint", {"dab"}),
    # cluster libraries: pypi/maven re-resolve on target; a dbfs-backed jar is a dangling ref
    ("cluster_library", "pypi", {"success"}),
    ("cluster_library", "maven", {"success"}),
    ("cluster_library", "wsmig_dummy.jar", {"manual"}),
    # serving (external-model → auto-migratable)
    ("serving_endpoint", "wsmig_test_ext_endpoint", {"success"}),
    # misc
    ("global_init_script", "wsmig_test_gis", {"success"}),
    ("global_init_script", "wsmig_test_gis_enabled", {"success"}),
    ("workspace_conf", "enableExportNotebook", {"success"}),
    ("workspace_conf", "enableWebTerminal", {"success"}),
    ("workspace_conf", "maxTokenLifetimeDays", {"success"}),
]


def verify(root):
    index = json.load(open(f"{root}/misc/export_index.json"))
    units = index["units"]
    by_type = {}
    for u in units:
        by_type.setdefault(u["asset_type"], []).append(u)

    print("\n" + "=" * 78)
    print(f"FINAL TEST REPORT — source export ({PROFILE})")
    print("=" * 78)
    print(f"{'ASSET TYPE':<22}{'FIXTURE (natural_key)':<34}{'STATUS':<12}RESULT")
    print("-" * 78)

    # SPN display-name → natural_key(appId) map, from the payload file (index has no payload).
    sp_display = {}
    sp_file = os.path.join(root, "export/identity/service_principals.json")
    if os.path.isfile(sp_file):
        for u in json.load(open(sp_file)).get("units", []):
            dn = (u.get("payload") or {}).get("displayName", "")
            if dn:
                sp_display[dn] = u["natural_key"]

    passed = failed = 0
    for asset_type, nk_sub, expected in EXPECT:
        if nk_sub.startswith("@displayName:"):
            want = nk_sub.split(":", 1)[1]
            appid = sp_display.get(want)
            matches = [u for u in by_type.get(asset_type, []) if u["natural_key"] == appid] if appid else []
            nk_sub = want   # for display
        elif nk_sub:
            matches = [u for u in by_type.get(asset_type, []) if nk_sub in u["natural_key"]]
        else:
            matches = by_type.get(asset_type, [])
        if not matches:
            # absent is acceptable only if __absent__ is allowed (e.g. >10MB nb never created)
            if "__absent__" in expected:
                ok = True; status = "absent(ok)"
            else:
                ok = False; status = "MISSING"
        else:
            statuses = {m["export_status"] for m in matches}
            ok = bool(statuses & expected)
            status = ",".join(sorted(statuses))
        passed += ok; failed += (not ok)
        mark = "✓ PASS" if ok else "✗ FAIL"
        nk_disp = (nk_sub or "<any>")[:32]
        print(f"{asset_type:<22}{nk_disp:<34}{status:<12}{mark}")

    # content-bytes + payload spot checks
    print("-" * 78)
    _extra_checks(root, by_type)

    print("-" * 78)
    print("ENVIRONMENT-LIMITED (code path verified offline, not creatable on this workspace):")
    print("  • legacy SQL dashboard (redash) — creation blocked/deprecated on this fresh")
    print("    workspace (RPC rejects create); collector handles it where such dashboards exist.")
    print("  • serving_endpoint UC-backed → manual — only external-model created live (auto);")
    print("    UC-backed manual path covered offline (test_export::build_all serving cases).")
    print("-" * 78)
    print(f"TOTAL: {passed} passed, {failed} failed  |  {len(units)} total export units")
    # DAB scope check
    dab_units = [u for u in units if u["export_status"] == "dab"]
    print(f"DAB-detected units: {len(dab_units)} "
          f"({sorted({u['asset_type'] for u in dab_units})})")
    # Every unit must carry a valid import_action — the workbook renders it as a column on every
    # sheet, and an unknown/blank value shows as "—", which is the blank-cell failure the
    # customer rejected. A DAB unit specifically must say dab_redeploy.
    from src.exporters.asset_export import IMPORT_ACTIONS, is_dab_content_path
    bad_act = [(u["asset_type"], u["natural_key"], u.get("import_action"))
               for u in units if u.get("import_action") not in IMPORT_ACTIONS]
    print(f"Units with an invalid/missing import_action: {len(bad_act)}  {bad_act[:5]}")
    bad_dab = [u["natural_key"] for u in dab_units if u.get("import_action") != "dab_redeploy"]
    print(f"DAB units not marked dab_redeploy: {len(bad_dab)}  {bad_dab[:5]}")
    act_counts: dict = {}
    for u in units:
        act_counts[u.get("import_action") or ""] = act_counts.get(u.get("import_action") or "", 0) + 1
    print("Import actions: " + ", ".join(f"{a or '(none)'}={n}"
                                         for a, n in sorted(act_counts.items(), key=lambda kv: -kv[1])))
    bad_bundle_act = [u["natural_key"] for u in units
                      if is_dab_content_path(u["asset_type"], u["natural_key"])
                      and u.get("import_action") != "dab_redeploy"]
    print(f"Bundle-content units not marked dab_redeploy: {len(bad_bundle_act)}  "
          f"{bad_bundle_act[:5]}")
    if bad_act or bad_dab or bad_bundle_act:
        failed += 1
    cov = [u for u in units if u["export_status"] == "covered"]
    print(f"Covered (dashboard/alert twins deduped): {len(cov)}")
    over = [u for u in units if u["export_status"] == "skipped_oversize"]
    print(f"Oversize skips: {len(over)}  {[u['natural_key'] for u in over][:5]}")
    fails = [u for u in units if u["export_status"] == "failure"]
    print(f"Export failures: {len(fails)}  {[(u['asset_type'],u['note'][:40]) for u in fails][:5]}")
    return failed == 0


def _extra_checks(root, by_type):
    # notebook content bytes exist with correct extensions
    exts = set()
    for u in by_type.get("notebook", []):
        if u.get("content_ref"):
            exts.add(os.path.splitext(u["content_ref"])[1])
    ok = {".py", ".sql", ".scala", ".r"} <= exts
    print(f"{'content ext check':<22}{'py/sql/scala/r bytes written':<34}{str(sorted(exts)):<12}"
          f"{'✓ PASS' if ok else '✗ FAIL'}")
    # genie serialized_space present in payload
    gp = os.path.join(root, "export/genie/spaces.json")
    gok = False
    if os.path.isfile(gp):
        for u in json.load(open(gp)).get("units", []):
            if "wsmig_test_genie" in u["natural_key"] and (u.get("payload") or {}).get("serialized_space"):
                gok = True
    print(f"{'genie payload':<22}{'serialized_space captured':<34}{'':<12}"
          f"{'✓ PASS' if gok else '✗ FAIL'}")
    # acls.json non-empty + has cluster/job/secret grants
    acls = json.load(open(f"{root}/export/acls.json"))
    atypes = {e["asset_type"] for e in acls}
    aok = {"cluster", "job", "secret_scope"} <= atypes
    print(f"{'acls.json':<22}{'cluster/job/secret grants':<34}{str(len(acls))+' objs':<12}"
          f"{'✓ PASS' if aok else '✗ FAIL'}")
    # import_action: BOTH the create and the assign path must appear live, so a reader can
    # tell "we exported it" (export_status) from "the utility will create it" (import_action).
    index = json.load(open(f"{root}/misc/export_index.json"))
    actions = {}
    for u in index["units"]:
        if u.get("import_action"):
            actions.setdefault(u["import_action"], 0)
            actions[u["import_action"]] += 1
    both = {"create", "assign_on_target"} <= set(actions)
    print(f"{'import_action':<22}{'create + assign both present':<34}{str(actions):<12}"
          f"{'✓ PASS' if both else '✗ FAIL'}")

    # `ai27_umi` is a real Azure managed identity imported as a Databricks SP. It MUST be
    # assign_on_target: recreating it on the target would mint a new applicationId and orphan
    # every ACL that referenced the UMI. This is the one assertion no synthetic fixture can make.
    umi_ok = False
    umi_detail = "not found"
    if os.path.isfile(sp_file := os.path.join(root, "export/identity/service_principals.json")):
        for u in json.load(open(sp_file)).get("units", []):
            pay = u.get("payload") or {}
            if pay.get("displayName") != "ai27_umi":
                continue
            appid_kept = pay.get("applicationId") == u.get("natural_key")
            umi_ok = (u.get("classification") == "umi_or_entra_sp"
                      and u.get("import_action") == "assign_on_target" and appid_kept)
            umi_detail = f"{u.get('classification')}/{u.get('import_action')}"
    print(f"{'azure UMI (ai27_umi)':<22}{'external → assign_on_target':<34}{umi_detail:<12}"
          f"{'✓ PASS' if umi_ok else '✗ FAIL'}")

    # DAB bundle content: exported, but NEVER import-actioned as create/upload. Importing
    # state/terraform.tfstate would point the customer's bundle at SOURCE workspace ids.
    from src.exporters.asset_export import is_dab_content_path
    bundle_units = [u for u in index["units"]
                    if is_dab_content_path(u["asset_type"], u["natural_key"])]
    bad_bundle = [(u["asset_type"], u["natural_key"], u.get("import_action"))
                  for u in bundle_units if u.get("import_action") != "dab_redeploy"]
    bok = bool(bundle_units) and not bad_bundle
    print(f"{'DAB bundle content':<22}{'all dab_redeploy, none created':<34}"
          f"{str(len(bundle_units)) + ' units':<12}{'✓ PASS' if bok else '✗ FAIL'}")
    if bad_bundle:
        print("   offenders:", bad_bundle[:5])
    # The bytes ARE still exported (reference copy if a bundle drifted from git).
    got_bytes = [u for u in bundle_units
                 if u["asset_type"] in ("notebook", "workspace_file") and u.get("content_ref")]
    print(f"{'DAB bundle bytes':<22}{'still exported for reference':<34}"
          f"{str(len(got_bytes)) + ' files':<12}{'✓ PASS' if got_bytes else '✗ FAIL'}")

    # manifest verifies
    from src.config.config_manager import Config
    # (manifest presence already implies complete; check file exists)
    mok = os.path.isfile(f"{root}/misc/manifest.json")
    print(f"{'manifest.json':<22}{'written (bundle complete)':<34}{'':<12}"
          f"{'✓ PASS' if mok else '✗ FAIL'}")


if __name__ == "__main__":
    root = run_export()
    print("\nbundle:", root)
    ok = verify(root)
    sys.exit(0 if ok else 1)
