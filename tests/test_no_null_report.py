"""No-null report test (Plan 1a): every card populated, every visible cell non-empty.

The customer requires that nothing in the inventory renders as null/blank when there IS data.
This builds a rich `objects_by_type` covering every in-scope card + every new column
(Managed By, has_secrets, deployed_by_dab, ACL grants on warehouses/legacy-sql/dashboards/
genie), runs the real adapter + HTML cell renderer, and asserts no visible cell is empty
("—" / blank) for a populated object.

Run: python3 -m tests.test_no_null_report
"""
from __future__ import annotations

from src.reports.html_generator import _cell_html
from src.reports.inventory_view import (
    _COLUMNS, _SUMMARY_CARD_KEYS, _deep_get, _resolve_items, adapt, build_counts,
)

_ACL = [{"user_name": "a@corp.com",
         "all_permissions": [{"permission_level": "CAN_MANAGE", "inherited": False}]}]


def _objects_by_type() -> dict:
    """Every collector bucket, fully populated so every card + column has a real value."""
    return {
        "identity": [
            {"identity_type": "user", "id": "u1", "userName": "alice@corp.com",
             "displayName": "Alice", "email": "alice@corp.com", "active": True,
             "externalId": "ext-1", "classification": "entra_user",
             "entitlements": ["allow-cluster-create", "databricks-sql-access"],
             "_raw": {"id": "u1", "userName": "alice@corp.com", "displayName": "Alice",
                      "active": True, "emails": [{"value": "alice@corp.com", "primary": True}]}},
            {"identity_type": "service_principal", "id": "s1", "applicationId": "app-dbx",
             "displayName": "dbx-sp", "active": True, "externalId": "",
             "classification": "db_managed_sp", "entitlements": ["workspace-access"],
             "has_secrets": True,
             "_raw": {"id": "s1", "applicationId": "app-dbx", "displayName": "dbx-sp", "active": True}},
            {"identity_type": "group", "id": "g1", "displayName": "eng",
             "classification": "db_managed_group", "member_count": 5, "has_nested_groups": True,
             "roles": [{"value": "admin"}], "entitlements": ["databricks-sql-access"],
             "_raw": {"id": "g1", "displayName": "eng", "roles": [{"value": "admin"}]}},
        ],
        "compute": [
            {"compute_type": "cluster", "cluster_id": "c1", "cluster_name": "analytics",
             "cluster_source": "UI", "pinned": True, "acl": _ACL,
             "_raw": {"cluster_id": "c1", "cluster_name": "analytics", "state": "RUNNING",
                      "cluster_source": "UI", "spark_version": "14.3.x-scala2.12",
                      "node_type_id": "Standard_DS3_v2", "autotermination_minutes": 30,
                      "creator_user_name": "alice@corp.com"}},
            {"compute_type": "instance_pool", "instance_pool_id": "p1",
             "instance_pool_name": "pool-a", "acl": _ACL,
             "_raw": {"instance_pool_id": "p1", "instance_pool_name": "pool-a",
                      "node_type_id": "Standard_DS3_v2", "state": "ACTIVE",
                      "min_idle_instances": 1, "max_capacity": 10}},
            {"compute_type": "cluster_policy", "policy_id": "pol1", "name": "policy-1", "acl": _ACL,
             "_raw": {"policy_id": "pol1", "name": "policy-1", "description": "standard policy",
                      "created_at_timestamp": 1700000000000}},
        ],
        "workspace_object": [
            {"path": "/Users/alice/nb", "object_type": "NOTEBOOK", "language": "PYTHON",
             "object_id": "111", "is_user_root": False, "acl": _ACL},
            {"path": "/Users/alice/data.csv", "object_type": "FILE", "language": "PYTHON",
             "object_id": "222", "is_user_root": False, "acl": _ACL},
            {"path": "/Repos/alice/repo", "object_type": "REPO", "repo_id": "r1",
             "url": "https://github.com/x/y", "branch": "main", "acl": _ACL,
             "_raw": {"path": "/Repos/alice/repo", "url": "https://github.com/x/y",
                      "provider": "gitHub", "branch": "main", "head_commit_id": "abcdef123456"}},
        ],
        "job": [
            {"job_id": "j1", "name": "nightly", "format": "MULTI_TASK", "job_type": "MULTI_TASK",
             "task_count": 3, "run_as": {"service_principal_name": "app-dbx"},
             "deployed_by_dab": True, "acl": _ACL,
             "settings": {"name": "nightly", "schedule": {"quartz_cron_expression": "0 0 * * *",
                                                          "timezone_id": "UTC"}},
             "_raw": {"job_id": "j1", "creator_user_name": "alice@corp.com",
                      "created_time": 1700000000000,
                      "settings": {"name": "nightly",
                                   "schedule": {"quartz_cron_expression": "0 0 * * *",
                                                "timezone_id": "UTC"}}}},
        ],
        "sql": [
            {"sql_type": "warehouse", "id": "w1", "name": "wh-a", "acl": _ACL,
             "_raw": {"id": "w1", "name": "wh-a", "state": "RUNNING", "warehouse_type": "PRO",
                      "cluster_size": "Small", "num_clusters": 1, "auto_stop_mins": 10,
                      "creator_name": "alice@corp.com"}},
            {"sql_type": "legacy_query", "id": "q1", "name": "top-sales", "acl": _ACL,
             "_raw": {"id": "q1", "display_name": "top-sales", "owner_user_name": "alice@corp.com",
                      "warehouse_id": "w1", "update_time": "2026-01-01T00:00:00"}},
            {"sql_type": "legacy_alert", "id": "a1", "name": "sla-alert", "acl": _ACL,
             "_raw": {"id": "a1", "display_name": "sla-alert", "owner_user_name": "alice@corp.com",
                      "parent_path": "/Users/alice", "create_time": "2026-01-01T00:00:00"}},
            {"sql_type": "legacy_dashboard", "id": "d0", "name": "legacy-dash", "acl": _ACL,
             "_raw": {"id": "d0", "name": "legacy-dash", "user": {"name": "alice@corp.com"},
                      "parent": "/Users/alice", "updated_at": "2026-01-01T00:00:00"}},
        ],
        "dlt_pipeline": [
            {"pipeline_id": "dlt1", "name": "bronze", "deployed_by_dab": True, "acl": _ACL,
             "_raw": {"pipeline_id": "dlt1", "name": "bronze", "state": "RUNNING",
                      "cluster_label": "default", "creator_user_name": "alice@corp.com",
                      "continuous": False}},
        ],
        "lakeview_dashboard": [
            {"dashboard_id": "lv1", "display_name": "KPIs", "warehouse_id": "w1",
             "parent_path": "/Users/alice", "acl": _ACL,
             "_raw": {"dashboard_id": "lv1", "display_name": "KPIs", "lifecycle_state": "ACTIVE",
                      "create_time": "2026-01-01T00:00:00", "update_time": "2026-01-02T00:00:00"}},
        ],
        "genie_space": [
            {"space_id": "gs1", "title": "Sales Genie", "warehouse_id": "w1", "acl": _ACL,
             "_raw": {"space_id": "gs1", "title": "Sales Genie", "description": "ask sales",
                      "warehouse_id": "w1", "created_timestamp": 1700000000000}},
        ],
        "serving_endpoint": [
            {"name": "model-a", "acl": _ACL, "migratable": True,
             "migration_note": "Non-UC served model — auto-migratable.",
             "_raw": {"name": "model-a", "state": {"ready": "READY"}, "creator": "alice@corp.com",
                      "creation_timestamp": 1700000000000, "last_updated_timestamp": 1700000000000}},
        ],
        "secret_scope": [
            {"name": "kv-scope", "backend_type": "AZURE_KEYVAULT",
             "keyvault_metadata": {"dns_name": "kv.vault.azure.net", "resource_id": "/sub/rg/kv"},
             "key_names": ["k1", "k2"], "values_migratable": False,
             "acls": [{"principal": "eng", "permission": "MANAGE"}],
             "_raw": {"name": "kv-scope", "backend_type": "AZURE_KEYVAULT",
                      "keyvault_metadata": {"dns_name": "kv.vault.azure.net"}}},
        ],
        "app": [
            {"name": "my-app", "description": "demo app", "creator": "alice@corp.com",
             "url": "https://app", "migratable": False, "acl": _ACL,
             "_raw": {"name": "my-app", "description": "demo app", "creator": "alice@corp.com",
                      "app_status": {"state": "RUNNING"}, "url": "https://app"}},
        ],
        "lakebase_project": [
            {"name": "lb1", "display_name": "orders-pg", "pg_version": "15", "migratable": False,
             "_raw": {"name": "lb1", "status": {"display_name": "orders-pg", "pg_version": "15"}}},
        ],
        "misc": [
            {"misc_type": "global_init_script", "script_id": "g1", "name": "setup",
             "position": 0, "enabled": True,
             "_raw": {"name": "setup", "position": 0, "enabled": True, "created_by": "alice@corp.com",
                      "updated_by": "alice@corp.com", "updated_at": 1700000000000}},
            {"misc_type": "cluster_library", "cluster_id": "c1",
             "library": {"pypi": {"package": "pandas"}}, "status": "INSTALLED",
             "is_library_for_all_clusters": False},
            {"misc_type": "workspace_conf", "key": "enableIpAccessLists", "value": "true"},
        ],
    }


# Columns intentionally allowed to be blank on SOME rows (legitimately optional / split-derived).
# Everything else must render a real value on a populated object.
_OPTIONAL = {
    ("serving_endpoints", "state.ready"),   # only set once ready
}


def test_no_null_report():
    obt = _objects_by_type()
    data = adapt(obt)
    counts = build_counts(data)

    blanks = []
    for key in _SUMMARY_CARD_KEYS:
        items = _resolve_items(data, key)
        assert items, f"card {key!r} has no items — every card must be populated for this test"
        cols = _COLUMNS.get(key, [])
        for item in items:
            for col_key, col_label, fmt in cols:
                if (key, col_key) in _OPTIONAL:
                    continue
                rendered = _cell_html(_deep_get(item, col_key), fmt)
                if 'class="na"' in rendered:  # the "—" empty marker
                    blanks.append(f"{key}.{col_label} ({col_key}) blank on {item.get('name') or item.get('path') or item.get('userName') or item.get('displayName') or item.get('title')}")

    # object_permissions rows must also be fully populated.
    for r in data["object_permissions"]:
        for col_key, col_label, fmt in _COLUMNS["object_permissions"]:
            rendered = _cell_html(_deep_get(r, col_key), fmt)
            if 'class="na"' in rendered and col_key != "inherited":
                blanks.append(f"object_permissions.{col_label} ({col_key}) blank")

    assert not blanks, "NULL/blank cells found:\n  " + "\n  ".join(blanks)
    print(f"no-null OK: {len(_SUMMARY_CARD_KEYS)} cards populated, "
          f"{counts['object_permissions']} ACL grant rows, no blank cells")


def test_export_status_column_resolves_for_every_card():
    """Every card's rows must JOIN to the export index, for all of them, not just identity.

    Regression: an identity-only redefinition of `_CARD_ASSET_TYPE` shadowed the full card map,
    so `_resolve_status` returned "" for every non-identity card and the Export Status column
    silently rendered "—" — including for genuinely oversize files. A blank status is worse than
    a wrong one: the operator sees nothing to act on.
    """
    from src.exporters.export_excel import (_CARD_ASSET_TYPE, _CARD_NK_FIELDS, _resolve_status,
                                            _row_asset_type, _row_natural_key)
    from src.reports.inventory_view import _SUMMARY_CARD_KEYS
    # the full card map must still cover every summary card (minus the two computed ones)
    computed = {"object_permissions", "sql_alerts"}
    missing = [k for k in _SUMMARY_CARD_KEYS
               if k not in computed and k not in _CARD_ASSET_TYPE]
    assert not missing, f"cards absent from _CARD_ASSET_TYPE (status would blank): {missing}"
    # cluster_libraries is exempt: its natural_key embeds a JSON library blob, so it joins via
    # the dedicated _cluster_library_status() matcher rather than a natural-key field list.
    missing_nk = [k for k in _CARD_ASSET_TYPE
                  if k not in _CARD_NK_FIELDS and k != "cluster_libraries"]
    assert not missing_nk, f"cards absent from _CARD_NK_FIELDS: {missing_nk}"

    # and the join must actually resolve BOTH columns end-to-end for a non-identity card
    row = {"path": "/Users/a/big.bin"}
    index = {"units": [{"asset_type": "workspace_file", "natural_key": "/Users/a/big.bin",
                        "export_status": "skipped_oversize", "note": "too big",
                        "import_action": "manual"}]}
    from src.exporters.export_excel import _import_action_lookup, _status_lookup
    status, note, action = _resolve_status("workspace_files", row, _status_lookup(index),
                                           index["units"], _import_action_lookup(index))
    assert status == "skipped_oversize", f"expected oversize status, got {status!r}"
    # Import Action is on every sheet now, not just the identity ones — an oversize file can't be
    # recreated from the bundle, so it must read MANUAL rather than a create.
    assert action == "manual", f"expected manual action, got {action!r}"
    assert _row_asset_type("workspace_files", row) == "workspace_file"
    assert _row_natural_key("workspace_files", row) == "/Users/a/big.bin"


def test_dab_column_absent_or_na_where_dab_impossible():
    """INV-2: the 'Deployed by DAB' column must only carry real values for asset types a DAB
    bundle CAN own. Instance pools & legacy Redash dashboards are NOT bundle resources, so the
    column is dropped entirely (a bare "Manual" reads as "migrate by hand", which is wrong — they
    auto-migrate). On the mixed Alerts tab, legacy alerts read "NA" while Alerts V2 keep the real
    label."""
    from src.reports.inventory_view import _COLUMNS, adapt, _dab_label
    from src.collectors.dab_registry import DAB_CAPABLE_ASSET_TYPES

    def _labels(key):
        return [lbl for _f, lbl, _s in _COLUMNS[key]]

    # column removed entirely for the two non-DAB-capable dedicated tabs
    assert "Deployed by DAB" not in _labels("instance_pools")
    assert "Deployed by DAB" not in _labels("sql_dashboards")
    # still present for representative capable types
    for key in ("jobs", "clusters", "sql_warehouses", "genie_spaces", "secret_scopes"):
        assert "Deployed by DAB" in _labels(key), key

    # capability set is the single source of truth
    assert "instance_pool" not in DAB_CAPABLE_ASSET_TYPES
    assert "cluster_policy" not in DAB_CAPABLE_ASSET_TYPES
    assert {"job", "cluster", "genie_space", "alert_v2"} <= DAB_CAPABLE_ASSET_TYPES

    # mixed Alerts tab: legacy → NA, V2 → real label; per-row, not per-column
    data = adapt({"sql": [
        {"sql_type": "legacy_alert", "name": "old", "_natural_key": "old",
         "deployed_by_dab": False},
        {"sql_type": "alert", "name": "v2dab", "_natural_key": "v2dab",
         "deployed_by_dab": True, "dab_scope": "shared"}]})
    by = {r["name"]: r for r in data["sql_alerts"]}
    assert by["old"]["_dab"] == "NA"
    assert by["v2dab"]["_dab"] == "DAB (Shared)"
    # the helper itself: capable=False is NA, not Manual
    assert _dab_label({"deployed_by_dab": False}, capable=False) == "NA"
    assert _dab_label({"deployed_by_dab": False}, capable=True) == "Manual"


if __name__ == "__main__":
    import sys
    try:
        test_no_null_report()
        test_export_status_column_resolves_for_every_card()
        print("\nPASS  test_no_null_report")
    except Exception:
        import traceback
        traceback.print_exc()
        print("\nFAIL")
        sys.exit(1)
