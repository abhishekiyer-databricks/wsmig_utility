"""LIVE harness: the migration state store against a REAL Delta table (Plan 3 build step 2, §11).

The offline tests prove the store's logic against a fake backend. This proves the SQL itself —
that the DDL, the MERGE-on-PK, the filtered reads and the escaping all actually work on a real
Delta table in the target workspace's UC. That matters because the store is the component whose
failure is worst (a lost source→target id map means duplicates + silently dropped updates), and a
hand-rolled MERGE is exactly the kind of thing that passes a mock and fails on Delta.

Uses the SQL Statement Execution API (no Spark on a laptop). Creates its own throwaway schema and
DROPS it at the end, so the workspace is left as found.

Run: python3 -m tests.live_state_store
"""
from __future__ import annotations

import configparser
import json
import os
import subprocess
import sys

from src.auth.token_manager import ApiClient, StaticTokenProvider
from src.config.config_manager import Config
from src.state.sql_backend import StatementApiBackend
from src.state.state_store import (ACTION_CREATED, ACTION_FAILED, ACTION_MANUAL, ACTION_SKIPPED,
                                   StateStore, UpsertAction)

PROFILE = os.environ.get("WSMIG_TARGET_PROFILE", "target_ws")
# A throwaway schema in the workspace's own default catalog (CREATE CATALOG fails on these
# default-storage metastores — memory `fvm1-test-fixtures-and-akv-state`).
TEST_SCHEMA = os.environ.get("WSMIG_TEST_SCHEMA", "wsmig_state_livetest")


def _profile() -> dict:
    c = configparser.ConfigParser()
    c.read(os.path.expanduser("~/.databrickscfg"))
    return dict(c[PROFILE])


def _token() -> str:
    return json.loads(subprocess.check_output(
        ["databricks", "auth", "token", "-p", PROFILE], text=True))["access_token"]


def _pick_warehouse(client) -> str:
    doc = client.get("api/2.0/sql/warehouses")
    whs = doc.get("warehouses") or []
    if not whs:
        raise SystemExit("no SQL warehouse on the target workspace")
    # prefer a serverless/running one; any will do (the API starts it)
    whs.sort(key=lambda w: (w.get("state") != "RUNNING", not w.get("enable_serverless_compute")))
    return whs[0]["id"]


def _default_catalog(client) -> str:
    """The workspace's own default catalog.

    `default_catalog_name` lives on the metastore ASSIGNMENT, not on `metastore_summary` (which
    describes the metastore itself and has no such field). Using the workspace's pre-provisioned
    catalog matters because `CREATE CATALOG` fails on these default-storage metastores.
    """
    doc = client.get("api/2.1/unity-catalog/current-metastore-assignment")
    catalog = doc.get("default_catalog_name") or ""
    if not catalog:
        raise SystemExit("could not resolve the workspace's default catalog")
    return catalog


def main() -> int:
    host = _profile()["host"].rstrip("/")
    client = ApiClient(host, StaticTokenProvider(_token()))
    warehouse = _pick_warehouse(client)
    catalog = _default_catalog(client)
    print(f"target : {host}\nwarehouse: {warehouse}\ncatalog: {catalog}\nschema : {TEST_SCHEMA}\n")

    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail[:160]}" if detail else ""))

    backend = StatementApiBackend(client, warehouse)
    backend.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{TEST_SCHEMA}")

    def store(ws_id="1234567890123456", run_id="live_r1", dry_run=False):
        cfg = Config.from_dict({
            "role": "target", "source_workspace_id": ws_id, "run_id": run_id,
            "target_staging_location": "/tmp/x", "dry_run": dry_run,
            "imports": {"state_catalog": catalog, "state_schema": TEST_SCHEMA,
                        "state_warehouse_id": warehouse}})
        st = StateStore(backend, cfg)
        st.ensure_table()
        st.load(force=True)
        return st

    try:
        # 1. DDL really works on Delta
        print("== 1. ensure_table (real Delta DDL) ==")
        st = store()
        tables = backend.sql(f"SHOW TABLES IN {catalog}.{TEST_SCHEMA}")
        names = {r.get("tableName") for r in tables}
        check("wsmig_migration_state created", "wsmig_migration_state" in names, str(sorted(names)))
        check("wsmig_identity_map created", "wsmig_identity_map" in names)

        # start from a clean slate for this pair (the schema may survive a failed earlier run)
        backend.sql(f"DELETE FROM {st.table_fqn} WHERE source_workspace_id = '1234567890123456'")
        backend.sql(f"DELETE FROM {st.identity_table_fqn} "
                    f"WHERE source_workspace_id = '1234567890123456'")
        st.load(force=True)

        # 2. MERGE inserts, then UPDATES the same PK rather than duplicating
        print("\n== 2. MERGE on the PK (insert then update, no duplicate) ==")
        st.record("cluster_policy", "p1", action=ACTION_CREATED, fingerprint="sha256:v1",
                  source_object_id="3", target_object_id="9")
        st.flush()
        rows = backend.sql(f"SELECT * FROM {st.table_fqn} WHERE natural_key = 'p1'")
        check("row inserted with BOTH ids", len(rows) == 1
              and rows[0]["source_object_id"] == "3" and rows[0]["target_object_id"] == "9",
              json.dumps(rows[0] if rows else {})[:200])

        st2 = store(run_id="live_r2")
        act = st2.decide("cluster_policy", "p1", "sha256:v2", exists_on_target=True)
        check("a changed fingerprint on a NEW store instance decides UPDATE",
              act is UpsertAction.UPDATE, str(act))
        check("the stored target id survived the round-trip",
              st2.get_target_id("cluster_policy", "p1") == "9")
        st2.record("cluster_policy", "p1", action="updated", fingerprint="sha256:v2",
                   target_object_id="9")
        st2.flush()
        rows = backend.sql(f"SELECT * FROM {st2.table_fqn} WHERE natural_key = 'p1'")
        check("MERGE updated in place — exactly ONE row for the PK", len(rows) == 1,
              f"{len(rows)} rows")
        check("last_action + fingerprint advanced",
              rows and rows[0]["last_action"] == "updated"
              and rows[0]["last_source_fingerprint"] == "sha256:v2")
        check("first_seen preserved across the update",
              rows and rows[0]["first_seen"] and rows[0]["first_seen"] <
              rows[0]["last_seen"])

        # 3. a batch bigger than one statement
        print("\n== 3. batched MERGE of many rows ==")
        st3 = store(run_id="live_r3")
        for i in range(50):
            st3.record("notebook", f"/Shared/live_nb_{i}", action=ACTION_CREATED,
                       fingerprint=f"sha256:n{i}", target_object_id=str(1000 + i))
        st3.flush()
        cnt = backend.sql(f"SELECT count(*) AS c FROM {st3.table_fqn} "
                          f"WHERE asset_type = 'notebook'")
        check("50 notebook rows merged in one batch", cnt and int(cnt[0]["c"]) == 50,
              f"count={cnt[0]['c'] if cnt else '?'}")
        ids = st3.target_ids_for("notebook")
        check("target id map reads back complete", len(ids) == 50
              and ids["/Shared/live_nb_7"] == "1007")

        # 4. the source_workspace_id filter really isolates pairs in ONE shared table
        print("\n== 4. pair isolation in a shared table ==")
        other = store(ws_id="9999999999", run_id="live_other")
        other.record("cluster_policy", "p1", action=ACTION_CREATED, fingerprint="sha256:other",
                     target_object_id="OTHER-TARGET")
        other.flush()
        again = store(run_id="live_r4")
        check("pair A still sees ITS target id, not pair B's",
              again.get_target_id("cluster_policy", "p1") == "9",
              f"got {again.get_target_id('cluster_policy', 'p1')!r}")
        check("pair B sees its own", other.get_target_id("cluster_policy", "p1") == "OTHER-TARGET")
        both = backend.sql(f"SELECT source_workspace_id FROM {again.table_fqn} "
                           f"WHERE natural_key = 'p1'")
        check("both pairs coexist as separate rows for the same natural_key", len(both) == 2,
              f"{len(both)} rows")

        # 5. escaping: names and errors that contain quotes
        print("\n== 5. quote/newline escaping on real SQL ==")
        nasty_key = "/Users/bob/Bob's \"ETL\" \\ pipeline"
        nasty_err = "can't create 'thing'\nsecond line"
        st5 = store(run_id="live_r5")
        st5.record("notebook", nasty_key, action=ACTION_FAILED, error=nasty_err,
                   error_raw='{"error_code":"BAD","message":"it\'s broken"}')
        st5.flush()
        st5.load(force=True)
        got = st5.row("notebook", nasty_key) or {}
        check("a key with quotes/backslashes round-trips exactly",
              got.get("natural_key") == nasty_key, repr(got.get("natural_key")))
        check("an error with quotes/newlines round-trips exactly",
              got.get("last_error") == nasty_err, repr(got.get("last_error")))

        # 6. identity map durability — the mapping that CANNOT be rebuilt from the target
        print("\n== 6. identity map durability ==")
        st6 = store(run_id="live_r6")
        st6.record_identity("service_principal", "old-app-uuid", target_id="scim-111",
                            target_key="brand-new-app-uuid", classification="db_managed_sp",
                            action="created")
        st6.record_identity("group", "finance-local", target_id="grp-222",
                            classification="db_managed_group", action="created")
        st6.flush()
        fresh = store(run_id="live_r7")
        m = fresh.load_identity_map()
        check("recreated SP's old→NEW appId survived (unrecoverable any other way)",
              m["sp_mapping"].get("old-app-uuid") == "brand-new-app-uuid", json.dumps(m)[:200])
        check("group name → target id survived", m["group_map"].get("finance-local") == "grp-222")

        # 7. retry work lists as real SQL-backed queries
        print("\n== 7. retry work lists ==")
        st7 = store(run_id="live_r8")
        st7.record("job", "live-failed", action=ACTION_FAILED, error="boom")
        st7.record("job", "live-warned", action="created_with_warning", target_object_id="5")
        st7.record("genie_space", "live-deferred", action="not_selected")
        st7.record("repo", "live-manual", action=ACTION_MANUAL)
        st7.record("job", "live-clean", action=ACTION_CREATED, fingerprint="f",
                   target_object_id="6")
        st7.flush()
        st7.load(force=True)
        failed = st7.retry_keys("failed_only")
        skipped = st7.retry_keys("skipped_only")
        check("failed_only picks up failed + created_with_warning",
              ("job", "live-failed") in failed and ("job", "live-warned") in failed
              and ("job", "live-clean") not in failed, f"{sorted(failed)[:4]}")
        check("skipped_only picks up not_selected + manual",
              ("genie_space", "live-deferred") in skipped and ("repo", "live-manual") in skipped)
        check("failed_and_skipped is the union",
              st7.retry_keys("failed_and_skipped") == failed | skipped)

        # 8. prerequisite satisfaction across SESSIONS (what makes phase-at-a-time work)
        print("\n== 8. prerequisites satisfied from a PRIOR session ==")
        st8 = store(run_id="live_r9")
        check("compute prerequisite satisfied from rows written earlier",
              st8.has_family(("notebook",)) is True)
        check("a family never imported is NOT satisfied",
              st8.has_family(("serving_endpoint",)) is False)

        # 9. dry-run rows land in a SEPARATE table
        print("\n== 9. dry-run isolation ==")
        dry = store(run_id="live_dry", dry_run=True)
        check("dry run targets the _dryrun table",
              dry.table_fqn.endswith("wsmig_migration_state_dryrun"), dry.table_fqn)
        dry.record("job", "rehearsal-only", action=ACTION_SKIPPED, fingerprint="f")
        dry.flush()
        real = backend.sql(f"SELECT count(*) AS c FROM {catalog}.{TEST_SCHEMA}."
                           f"wsmig_migration_state WHERE natural_key = 'rehearsal-only'")
        check("a rehearsal wrote NOTHING into the real state table",
              int(real[0]["c"]) == 0, f"count={real[0]['c']}")

    finally:
        print(f"\ncleaning up {catalog}.{TEST_SCHEMA} ...")
        try:
            backend.sql(f"DROP SCHEMA IF EXISTS {catalog}.{TEST_SCHEMA} CASCADE")
            print("  dropped")
        except Exception as exc:  # noqa: BLE001
            print(f"  cleanup failed (harmless, remove by hand): {exc}")

    npass = sum(1 for _n, ok, _d in checks if ok)
    nfail = len(checks) - npass
    print("\n" + "=" * 74)
    print(f"LIVE STATE-STORE CHECKS: {npass} passed, {nfail} failed")
    for name, ok, detail in checks:
        if not ok:
            print(f"  FAIL {name}: {detail[:200]}")
    print("=" * 74)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
