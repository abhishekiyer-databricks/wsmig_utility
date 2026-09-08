"""
Inventory view metadata + data adapter — shared by the HTML and Excel generators.

This is a faithful port of the customer's existing inventory script
(`workspace_inventory_nb.ipynb` → `wsinv_lib.py`) rendering layer: the SAME icons,
labels, per-asset column definitions, cell formatters and card ordering, so the HTML
and Excel this utility emits match the output the customer is already used to.

Two differences, both deliberate (see CLAUDE.md / PLAN_0 §6a):
  • UC / MLflow assets are OUT of scope for this migration utility, so their cards are
    omitted (the customer approved "omit UC cards entirely"). Every card shown is an
    asset we actually collect.
  • A few in-scope assets the reference script misses are ADDED as extra cards, in the
    reference's own style: legacy SQL dashboards, IP access lists, workspace conf.

`adapt(objects_by_type)` bridges OUR collectors (which bucket assets into coarse
`object_type`s and keep the raw API object under `_raw`) to the reference renderer's
expected shape: a dict keyed by the fine-grained asset key, each a list of raw-ish
objects the column formatters can `_deep_get` into.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Icons / labels / columns — ported verbatim from the reference script,
# trimmed to the in-scope keys (+ our additions).
# ---------------------------------------------------------------------------

_ICONS = {
    "users":              ("👤", "#4f46e5"),
    "groups":             ("👥", "#7c3aed"),
    "service_principals": ("🔑", "#9333ea"),
    "notebooks":          ("📓", "#0284c7"),
    "workspace_files":    ("📄", "#0369a1"),
    "sql_queries":        ("📝", "#059669"),
    "jobs":               ("⚙️",  "#d97706"),
    "clusters":           ("🖥️",  "#059669"),
    "instance_pools":     ("🏊",  "#10b981"),
    "cluster_policies":   ("📋",  "#6d28d9"),
    "cluster_libraries":  ("📚",  "#7c3aed"),
    "global_init_scripts":("🌐",  "#0d9488"),
    "sql_warehouses":     ("🗄️",  "#dc2626"),
    "sql_alerts":         ("🔔",  "#ca8a04"),
    "sql_dashboards":     ("📈",  "#be185d"),
    "dlt_pipelines":      ("🔀",  "#ea580c"),
    "lakeview_dashboards":("📊",  "#be185d"),
    "genie_spaces":       ("✨",  "#7c3aed"),
    "serving_endpoints":  ("🚀",  "#b91c1c"),
    "secret_scopes":      ("🔒",  "#0f766e"),
    "repos":              ("📦",  "#1d4ed8"),
    "apps":               ("📱",  "#2563eb"),
    "lakebase_projects":  ("🐘",  "#336791"),
    "workspace_conf":     ("🔧",  "#475569"),
    "object_permissions": ("🔐",  "#9333ea"),
}

_LABELS = {
    "users":              "Users",
    "groups":             "Groups",
    "service_principals": "Service Principals",
    "notebooks":          "Notebooks",
    "workspace_files":    "Workspace Files",
    "sql_queries":        "SQL Queries",
    "jobs":               "Jobs",
    "clusters":           "All-Purpose Clusters",
    "instance_pools":     "Instance Pools",
    "cluster_policies":   "Cluster Policies",
    "cluster_libraries":  "Cluster Libraries",
    "global_init_scripts":"Global Init Scripts",
    "sql_warehouses":     "SQL Warehouses",
    "sql_alerts":         "SQL Alerts",
    "sql_dashboards":     "Legacy SQL Dashboards",
    "dlt_pipelines":      "DLT Pipelines",
    "lakeview_dashboards":"AI/BI Dashboards",
    "genie_spaces":       "Genie AI Spaces",
    "serving_endpoints":  "Model Serving Endpoints",
    "secret_scopes":      "Secret Scopes",
    "repos":              "Git Repos",
    "apps":               "Databricks Apps",
    "lakebase_projects":  "Lakebase Instances",
    "workspace_conf":     "Workspace Conf",
    "object_permissions": "Object Permissions (ACLs)",
}

# Column definitions per component: (key_in_obj, display_label, cell_formatter_name).
# Dotted keys are resolved with `_deep_get`. Ported from the reference script.
_COLUMNS: Dict[str, List[tuple]] = {
    "users": [
        ("userName",     "Username",       "plain"),
        ("displayName",  "Display Name",   "plain"),
        ("active",       "Active",         "badge_bool"),
        ("emails",       "Email",          "first_email"),
        ("classification","Managed By",    "cls_managed"),
        ("_entitlements","Entitlements",   "plain"),
        ("id",           "SCIM ID",        "mono"),
    ],
    "groups": [
        ("displayName",   "Group Name",    "plain"),
        ("_managed",      "Managed By",    "badge_managed"),
        ("_member_count", "Members",       "plain"),
        ("_nested",       "Nested Groups", "badge_bool"),
        ("roles",         "Roles",         "count"),
        ("_entitlements", "Entitlements",  "plain"),
        ("id",            "SCIM ID",       "mono"),
    ],
    "service_principals": [
        ("displayName",   "Display Name",  "plain"),
        ("applicationId", "App ID",        "mono"),
        ("_managed",      "Managed By",    "badge_managed"),
        ("active",        "Active",        "badge_bool"),
        ("_entitlements", "Entitlements",  "plain"),
        ("_has_secrets",  "Has Secrets",   "badge_bool_unknown"),
        ("id",            "SCIM ID",       "mono"),
    ],
    "notebooks": [
        ("path",        "Path",          "path"),
        ("object_type", "Type",          "badge_type"),
        ("language",    "Language",      "badge_lang"),
        ("_dab",        "Deployed by DAB","badge_type"),
        ("_acls",       "ACL Grants",    "plain"),
        ("object_id",   "Object ID",     "mono"),
    ],
    "workspace_files": [
        ("path",        "Path",          "path"),
        ("object_type", "Type",          "badge_type"),
        ("language",    "Language",      "badge_lang"),
        ("_dab",        "Deployed by DAB","badge_type"),
        ("_acls",       "ACL Grants",    "plain"),
        ("object_id",   "Object ID",     "mono"),
    ],
    "sql_queries": [
        ("display_name",      "Query Name",   "plain"),
        ("id",                "ID",           "mono"),
        ("owner_user_name",   "Owner",        "plain"),
        ("warehouse_id",      "Warehouse ID", "mono"),
        ("_acls",             "ACL Grants",   "plain"),
        ("update_time",       "Updated",      "iso_ts"),
    ],
    "jobs": [
        ("settings.name",     "Job Name",      "plain"),
        ("job_id",            "Job ID",        "mono"),
        ("_job_type",         "Type",          "badge_type"),
        ("_task_count",       "Tasks",         "plain"),
        ("_deployed_by_dab",  "Deployed by DAB","badge_bool"),
        ("settings.schedule", "Schedule",      "schedule"),
        ("_run_as",           "Run As",        "plain"),
        ("_acls",             "ACL Grants",    "plain"),
        ("creator_user_name", "Creator",       "plain"),
    ],
    "clusters": [
        ("cluster_name",    "Cluster Name",    "plain"),
        ("cluster_id",      "Cluster ID",      "mono"),
        ("state",           "State",           "badge_state"),
        ("cluster_source",  "Source",          "plain"),
        ("_pinned",         "Pinned",          "badge_bool"),
        ("spark_version",   "Spark Version",   "plain"),
        ("node_type_id",    "Node Type",       "plain"),
        ("_dab",            "Deployed by DAB", "badge_type"),
        ("_acls",           "ACL Grants",      "plain"),
        ("creator_user_name","Creator",         "plain"),
    ],
    # Instance pools are NOT a DAB resource type (absent from the CLI bundle schema, verified
    # v0.291.0), so NO "Deployed by DAB" column here — it could only ever say "Manual", which
    # misreads as "migrate by hand". Pools ARE auto-migratable and imported normally (INV-2).
    "instance_pools": [
        ("instance_pool_name", "Pool Name",      "plain"),
        ("instance_pool_id",   "Pool ID",        "mono"),
        ("node_type_id",       "Node Type",      "plain"),
        ("state",              "State",          "badge_state"),
        ("min_idle_instances", "Min Idle",       "plain"),
        ("max_capacity",       "Max Capacity",   "plain"),
        ("_acls",              "ACL Grants",     "plain"),
    ],
    "cluster_policies": [
        ("name",              "Policy Name",   "plain"),
        ("policy_id",         "Policy ID",     "mono"),
        ("description",       "Description",   "trunc"),
        ("_acls",             "ACL Grants",    "plain"),
        ("created_at_timestamp","Created",      "epoch_ms"),
    ],
    "cluster_libraries": [
        ("cluster_id",                  "Cluster ID",   "mono"),
        ("library_type",                "Type",         "badge_type"),
        ("library_name",                "Library",      "plain"),
        ("status",                      "Status",       "badge_state"),
        ("is_library_for_all_clusters", "All Clusters", "badge_bool"),
    ],
    "global_init_scripts": [
        ("name",              "Script Name",  "plain"),
        ("position",          "Order",        "plain"),
        ("enabled",           "Enabled",      "badge_bool"),
        ("created_by",        "Created By",   "plain"),
        ("updated_by",        "Updated By",   "plain"),
        ("updated_at",        "Updated",      "epoch_ms"),
    ],
    "sql_warehouses": [
        ("name",              "Warehouse Name", "plain"),
        ("id",                "ID",             "mono"),
        ("state",             "State",          "badge_state"),
        ("warehouse_type",    "Type",           "plain"),
        ("cluster_size",      "Size",           "plain"),
        ("num_clusters",      "Clusters",       "plain"),
        ("auto_stop_mins",    "Auto-Stop (min)","plain"),
        ("_dab",              "Deployed by DAB","badge_type"),
        ("_acls",             "ACL Grants",     "plain"),
        ("creator_name",      "Creator",        "plain"),
    ],
    "sql_alerts": [
        ("display_name",      "Alert Name",   "plain"),
        ("_alert_kind",       "Kind",         "badge_type"),
        ("_dab",              "Deployed by DAB","badge_type"),
        ("id",                "ID",           "mono"),
        ("owner_user_name",   "Owner",        "plain"),
        ("parent_path",       "Parent Path",  "path"),
        ("_acls",             "ACL Grants",   "plain"),
        ("create_time",       "Created",      "iso_ts"),
    ],
    # Legacy (Redash) SQL dashboards are not a DAB resource type → no "Deployed by DAB" column.
    "sql_dashboards": [
        ("name",              "Dashboard Name", "plain"),
        ("id",                "ID",             "mono"),
        ("user.name",         "Owner",          "plain"),
        ("parent",            "Parent",         "path"),
        ("_acls",             "ACL Grants",     "plain"),
        ("updated_at",        "Updated",        "iso_ts"),
    ],
    "dlt_pipelines": [
        ("name",              "Pipeline Name",  "plain"),
        ("pipeline_id",       "Pipeline ID",    "mono"),
        ("state",             "State",          "badge_state"),
        ("_deployed_by_dab",  "Deployed by DAB","badge_bool"),
        ("cluster_label",     "Cluster",        "plain"),
        ("creator_user_name", "Creator",        "plain"),
        ("continuous",        "Continuous",     "badge_bool"),
        ("_acls",             "ACL Grants",     "plain"),
    ],
    "lakeview_dashboards": [
        ("display_name",      "Dashboard Name", "plain"),
        ("dashboard_id",      "Dashboard ID",   "mono"),
        ("lifecycle_state",   "State",          "badge_state"),
        ("_dab",              "Deployed by DAB","badge_type"),
        ("_acls",             "ACL Grants",     "plain"),
        ("create_time",       "Created",        "iso_ts"),
        ("update_time",       "Updated",        "iso_ts"),
    ],
    "genie_spaces": [
        ("title",             "Space Name",     "plain"),
        ("space_id",          "Space ID",       "mono"),
        ("description",       "Description",    "trunc"),
        ("warehouse_id",      "Warehouse ID",   "mono"),
        ("_dab",              "Deployed by DAB","badge_type"),
        ("_acls",             "ACL Grants",     "plain"),
        ("created_timestamp", "Created",        "epoch_ms"),
    ],
    "serving_endpoints": [
        ("name",              "Endpoint Name",  "plain"),
        ("state.ready",       "Ready",          "plain"),
        ("creator",           "Creator",        "plain"),
        ("_dab",              "Deployed by DAB","badge_type"),
        ("_migratable",       "Auto-Migratable","badge_bool"),
        ("_migration_note",   "Migration Note", "trunc"),
        ("_acls",             "ACL Grants",     "plain"),
        ("creation_timestamp","Created",        "epoch_ms"),
        ("last_updated_timestamp","Updated",    "epoch_ms"),
    ],
    "secret_scopes": [
        ("name",              "Scope Name",     "plain"),
        ("backend_type",      "Backend",        "badge_type"),
        ("keyvault_metadata", "Key Vault",      "kv_dns"),
        ("_key_count",        "Secret Keys",    "plain"),
        ("_values_migratable","Values Migrate", "badge_bool"),
        ("_dab",              "Deployed by DAB","badge_type"),
        ("_acls",             "ACL Grants",     "plain"),
    ],
    "repos": [
        ("path",              "Path",           "path"),
        ("url",               "Repository URL", "url_link"),
        ("provider",          "Provider",       "badge_type"),
        ("branch",            "Branch",         "plain"),
        ("_acls",             "ACL Grants",     "plain"),
        ("head_commit_id",    "Commit",         "short_mono"),
    ],
    # Inventory-only (migration flagged manual for v1).
    "apps": [
        ("name",              "App Name",       "plain"),
        ("description",       "Description",    "trunc"),
        ("app_status.state",  "State",          "badge_state"),
        ("creator",           "Creator",        "plain"),
        ("_migratable",       "Auto-Migratable","badge_bool"),
        ("_acls",             "ACL Grants",     "plain"),
        ("url",               "URL",            "url_link"),
    ],
    "lakebase_projects": [
        ("name",                "Resource Name",   "plain"),
        ("status.display_name", "Display Name",    "plain"),
        ("status.pg_version",   "PG Version",      "plain"),
        ("_migratable",         "Auto-Migratable", "badge_bool"),
    ],
    # ── Added by this utility (not in the reference script) ──────────────────
    "workspace_conf": [
        ("key",               "Setting Key",    "plain"),
        ("value",             "Value",          "plain"),
    ],
    # Every ACL grant is a countable, migratable row (one per object×principal×permission).
    "object_permissions": [
        ("object_type",       "Object Type",    "badge_type"),
        ("object_key",        "Object",         "plain"),
        ("principal",         "Principal",      "plain"),
        ("permission",        "Permission",     "badge_type"),
        ("inherited",         "Inherited",      "badge_bool"),
    ],
}

# Card / tab order (in-scope only), grouped the way the reference script groups them.
_SUMMARY_CARD_KEYS = [
    # Identity
    "users", "groups", "service_principals",
    # Workspace
    "notebooks", "workspace_files", "sql_queries",
    # Compute & Jobs
    "jobs", "clusters", "instance_pools", "cluster_policies",
    "cluster_libraries", "global_init_scripts",
    # SQL & BI
    "sql_warehouses", "sql_alerts", "sql_dashboards", "dlt_pipelines",
    "lakeview_dashboards", "genie_spaces",
    # AI
    "serving_endpoints",
    # Platform
    "secret_scopes", "repos", "workspace_conf",
    # Permissions (every ACL grant, countable)
    "object_permissions",
    # Inventory-only (migration flagged manual for v1)
    "apps", "lakebase_projects",
]


# ---------------------------------------------------------------------------
# Value helpers — ported from the reference script.
# ---------------------------------------------------------------------------

def _deep_get(obj: Any, dotted_key: str) -> Any:
    """Get a value from a nested dict using dot notation."""
    for part in dotted_key.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _esc(s: Any) -> str:
    s = str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _flatten_library(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one of our misc cluster_library records into the reference row shape.

    Our record: {cluster_id, library: {"pypi": {...}} / {"jar": "..."} / ..., status}.
    Reference row: cluster_id, library_type, library_name, status, is_library_for_all_clusters.
    """
    lib = rec.get("library", {}) or {}
    ltype = next(iter(lib), "")
    lspec = lib.get(ltype, "")
    if isinstance(lspec, dict):
        name = (lspec.get("package") or lspec.get("coordinates")
                or lspec.get("path") or json.dumps(lspec))
    else:
        name = str(lspec)
    return {
        "cluster_id": rec.get("cluster_id"),
        "library_type": ltype,
        "library_name": name,
        "status": rec.get("status"),
        "is_library_for_all_clusters": rec.get("is_library_for_all_clusters"),
    }


# ---------------------------------------------------------------------------
# Adapter: OUR collectors' output  →  reference renderer's `data` dict.
#
# Design principle (customer instruction): the inventory is the reconciliation
# baseline for the final result report, so every migratable item must be a
# countable line item carrying its migration-critical metadata as VISIBLE columns
# (e.g. a group's Entra-vs-Databricks-managed classification, a cluster's ephemeral
# flag, a job's owner-ACL presence). We therefore MERGE our collectors' enrichment
# onto the raw API object under distinct `_`-prefixed keys (raw keeps the reference
# columns intact; our enrichment adds the metadata columns) instead of showing raw
# alone.
# ---------------------------------------------------------------------------

def _merge(rec: Dict[str, Any], **enrichment) -> Dict[str, Any]:
    """Merge the raw API object (base — feeds the reference columns) with our collector's
    enrichment (added under distinct keys — feeds the metadata columns). Falls back to our
    normalized record as the base when a collector didn't keep `_raw`."""
    raw = rec.get("_raw")
    base = dict(raw) if isinstance(raw, dict) and raw else {
        k: v for k, v in rec.items() if k != "_raw"}
    # Enrichment values win; drop Nones so empty cells render as "—".
    for k, v in enrichment.items():
        if v is not None:
            base[k] = v
    return base


def _acl_count(rec: Dict[str, Any]) -> int:
    """Number of non-inherited permission grants on an object (migratable ACL rows)."""
    acl = rec.get("acl") or rec.get("acls") or []
    if not isinstance(acl, list):
        return 0
    # Secret ACLs are {principal, permission}; object ACLs are access_control entries.
    return len(acl)


def adapt(objects_by_type: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    """Explode our coarse `object_type` buckets into the reference's fine-grained keys,
    each item carrying migration-critical metadata (merged from our enrichment).
    """
    identity = objects_by_type.get("identity", []) or []
    compute = objects_by_type.get("compute", []) or []
    ws = objects_by_type.get("workspace_object", []) or []
    sql = objects_by_type.get("sql", []) or []
    misc = objects_by_type.get("misc", []) or []

    def _by(items, field, value):
        return [i for i in items if i.get(field) == value]

    def _ent(rec):
        """Our normalized entitlements (list of strings) → comma string for display."""
        e = rec.get("entitlements") or []
        return ", ".join(str(x) for x in e) if isinstance(e, list) else ""

    data: Dict[str, List[dict]] = {}

    # ── Identity → users / groups / service_principals ───────────────────
    data["users"] = [
        _merge(i, classification=i.get("classification"), _entitlements=_ent(i))
        for i in _by(identity, "identity_type", "user")]
    data["service_principals"] = [
        _merge(i, classification=i.get("classification"), _entitlements=_ent(i),
               _managed=_sp_managed(i.get("kind") or i.get("classification"),
                                                    bool(i.get("entra_backed"))),
               # NOT bool(): `None` means "could not check" (insufficient privilege) and must
               # stay distinct from a real False, or the report understates manual work.
               _has_secrets=i.get("has_secrets"))
        for i in _by(identity, "identity_type", "service_principal")]
    data["groups"] = [
        _merge(i, classification=i.get("classification"), _entitlements=_ent(i),
               _managed=_group_managed(i.get("kind") or i.get("classification"),
                                                       bool(i.get("entra_backed"))),
               _member_count=i.get("member_count"),
               _nested=i.get("has_nested_groups"))
        for i in _by(identity, "identity_type", "group")]

    # ── Workspace content ────────────────────────────────────────────────
    # Notebooks/files: our records carry path/type/language/object_id + is_user_root + acl.
    data["workspace_items"] = [
        _merge(w, _acls=_acl_count(w), _is_user_root=w.get("is_user_root"),
               _dab=_dab_label(w))
        for w in ws if w.get("object_type") in ("NOTEBOOK", "FILE")]
    data["repos"] = [
        _merge(w, _acls=_acl_count(w)) for w in ws if w.get("object_type") == "REPO"]

    # ── Compute → clusters / instance_pools / cluster_policies ───────────
    # (Ephemeral job/DLT/model clusters are dropped by the collector — Plan 1a §8.)
    # Clusters/pools/warehouses/scopes/serving have NO workspace path, so their DAB flag can't be
    # inferred from one — it is stamped by `inventory_runner._stamp_dab_ownership` from the bundle
    # state files (`dab_registry`). Surfacing it here means every tab whose asset CAN be
    # bundle-owned says so up front, instead of leaving the reader to infer it from an export
    # status of "Skipped (DAB)" further along the row.
    data["clusters"] = [
        _merge(c, _pinned=bool(c.get("pinned")), _acls=_acl_count(c), _dab=_dab_label(c))
        for c in _by(compute, "compute_type", "cluster")]
    data["instance_pools"] = [
        _merge(c, _acls=_acl_count(c), _dab=_dab_label(c))
        for c in _by(compute, "compute_type", "instance_pool")]
    data["cluster_policies"] = [
        _merge(c, _acls=_acl_count(c)) for c in _by(compute, "compute_type", "cluster_policy")]

    # ── Jobs / DLT / dashboards / genie ──────────────────────────────────
    data["jobs"] = [
        _merge(j, _job_type=j.get("job_type"), _task_count=j.get("task_count"),
               _run_as=_run_as_str(j.get("run_as")), _acls=_acl_count(j),
               _deployed_by_dab=bool(j.get("deployed_by_dab")))
        for j in objects_by_type.get("job", []) or []]
    data["dlt_pipelines"] = [
        _merge(p, _acls=_acl_count(p), _deployed_by_dab=bool(p.get("deployed_by_dab")))
        for p in objects_by_type.get("dlt_pipeline", []) or []]
    data["lakeview_dashboards"] = [
        _merge(d, warehouse_id=d.get("warehouse_id"), parent_path=d.get("parent_path"),
               _acls=_acl_count(d), _dab=_dab_label(d))
        for d in objects_by_type.get("lakeview_dashboard", []) or []]
    data["genie_spaces"] = [
        _merge(g, warehouse_id=g.get("warehouse_id"), _acls=_acl_count(g), _dab=_dab_label(g))
        for g in objects_by_type.get("genie_space", []) or []]

    # ── SQL → warehouses / legacy queries / alerts / dashboards ──────────
    data["sql_warehouses"] = [
        _merge(s, _acls=_acl_count(s), _dab=_dab_label(s))
        for s in _by(sql, "sql_type", "warehouse")]
    data["sql_queries"] = [_merge(s, _acls=_acl_count(s)) for s in _by(sql, "sql_type", "legacy_query")]
    # Alerts tab shows BOTH legacy (/api/2.0/sql/alerts) and Alerts V2 (/api/2.0/alerts),
    # each tagged with a "Kind" so migration can tell them apart (only legacy migrate via
    # the legacy path; V2 alerts use the Alerts V2 API).
    # This tab is MIXED: a legacy alert can never be DAB-owned (→ NA), while an Alerts V2 can
    # (→ real Manual / DAB label). So capability is decided PER ROW by sql_type, not per column.
    data["sql_alerts"] = [
        _merge(s, _acls=_acl_count(s), _alert_kind=_alert_kind(s.get("sql_type")),
               _dab=_dab_label(s, capable=(s.get("sql_type") == "alert")))
        for s in sql if s.get("sql_type") in ("legacy_alert", "alert")]
    # Legacy (Redash) SQL dashboards are not a DAB resource type (the schema's `dashboards` is
    # Lakeview only) → no DAB column at all for this tab.
    data["sql_dashboards"] = [
        _merge(s, _acls=_acl_count(s))
        for s in _by(sql, "sql_type", "legacy_dashboard")]

    # ── Model serving endpoints (Agent Bricks agents are excluded at the collector —
    #    not recreatable via workspace REST) ─
    data["serving_endpoints"] = [
        _merge(e, _acls=_acl_count(e), _migratable=e.get("migratable"),
               _migration_note=e.get("migration_note"), _dab=_dab_label(e))
        for e in objects_by_type.get("serving_endpoint", []) or []]

    # ── Secrets ──────────────────────────────────────────────────────────
    data["secret_scopes"] = [
        _merge(s, backend_type=s.get("backend_type"),
               _key_count=len(s.get("key_names") or []),
               _values_migratable=s.get("values_migratable"),
               _acls=_acl_count(s), _dab=_dab_label(s))
        for s in objects_by_type.get("secret_scope", []) or []]

    # ── Apps / Lakebase (inventory-only, migration manual) ───────────────
    data["apps"] = [
        _merge(a, _migratable=a.get("migratable"), _acls=_acl_count(a))
        for a in objects_by_type.get("app", []) or []]
    data["lakebase_projects"] = [
        _merge(p, _migratable=p.get("migratable"))
        for p in objects_by_type.get("lakebase_project", []) or []]

    # ── Misc → global init scripts / cluster libs / IP lists / ws conf ───
    data["global_init_scripts"] = [
        _merge(m) for m in _by(misc, "misc_type", "global_init_script")]
    data["cluster_libraries"] = [
        _flatten_library(m) for m in _by(misc, "misc_type", "cluster_library")]
    data["workspace_conf"] = _by(misc, "misc_type", "workspace_conf")

    # ── Object permissions (ACLs) — flattened to countable grant rows ────
    data["object_permissions"] = _flatten_acls(objects_by_type)

    return data


# ── Metadata helpers (Entra-vs-Databricks-managed etc.) ───────────────────

# Raw `kind` (Plan 6) / legacy `classification` → the friendly "Managed By" label. Defined ONCE here
# and imported by both the HTML and Excel formatters, which previously each carried their own copy
# and knew only the pre-Plan-6 vocabulary — so a new `kind` rendered as the raw string "account".
MANAGED_BY_LABEL = {
    # Plan 6 kinds
    "account": "Account",
    "workspace_local": "Workspace-local",
    "system": "Built-in",
    "system_generated": "Databricks-generated (skipped)",
    "needs_review": "Needs review",
    # legacy classifications (older bundles / reports)
    "entra_user": "Entra / SCIM",
    "umi_or_entra_sp": "Entra / UMI",
    "db_managed_sp": "Databricks (account)",
    "account_group": "Account / Entra",
    "db_managed_group": "Workspace-local",
    "builtin_group": "Built-in",
}


def managed_by_label(value) -> str:
    """Friendly "Managed By" text for a raw kind/classification value."""
    return MANAGED_BY_LABEL.get(str(value), str(value))


def _sp_managed(kind: Any, entra_backed: bool = False) -> str:
    """Human 'Managed by' label for a service principal.

    Under Plan 6 every SP is an ACCOUNT principal (the workspace SCIM POST writes at the account),
    so `kind` no longer distinguishes Entra from Databricks-native — `entra_backed` does. The
    distinction is presentational only: both are adopted by `applicationId` on exactly the same
    code path.
    """
    if kind in ("account", "umi_or_entra_sp", "db_managed_sp"):
        # Legacy classifications still imply their origin; the new model reads entra_backed.
        if entra_backed or kind == "umi_or_entra_sp":
            return "Entra / UMI (account)"
        return "Databricks (account)"
    return ""


def _group_managed(kind: Any, entra_backed: bool = False) -> str:
    """Human 'Managed by' label for a group — the one identity where the kind changes behaviour."""
    if kind in ("account", "account_group"):
        return "Account / Entra" if (entra_backed or kind == "account_group") else "Account"
    if kind in ("workspace_local", "db_managed_group"):
        return "Workspace-local"
    if kind in ("system", "builtin_group"):
        return "Built-in"
    return ""


def _dab_label(rec: Dict[str, Any], capable: bool = True) -> str:
    """'Deployed by DAB' cell: NA / Manual / DAB (Shared) / DAB (User).

    `capable=False` (the asset type is not a DAB resource — see `DAB_CAPABLE_ASSET_TYPES`) yields
    "NA", NOT "Manual": "Manual" reads as "migrate this by hand", but these assets migrate
    automatically — DAB simply has no say over them, so the honest cell is "not applicable".

    The (Shared) vs (User) split matters to this customer: shared bundles are current staging +
    all prod; user-scoped bundles (username/uuid in path) are the legacy staging pattern."""
    if not capable:
        return "NA"
    from src.utils.helpers import dab_deploy_label
    return dab_deploy_label(rec.get("deployed_by_dab"), rec.get("dab_scope"))


def _alert_kind(sql_type: Any) -> str:
    """Human 'Kind' label for the SQL Alerts tab: Legacy (/api/2.0/sql/alerts) vs
    Alerts V2 (/api/2.0/alerts). Databricks stopped new legacy-alert creation and steers
    to V2, so migration treats the two differently."""
    return {"legacy_alert": "Legacy", "alert": "Alerts V2"}.get(str(sql_type), "")


def _run_as_str(run_as: Any) -> str:
    if isinstance(run_as, dict):
        return safe_str_local(run_as.get("service_principal_name")
                              or run_as.get("user_name"))
    return safe_str_local(run_as)


def safe_str_local(v: Any) -> str:
    return "" if v is None else str(v)


# ── ACL flattening: every grant becomes a countable, migratable row ───────

# Which object-type buckets carry object ACLs, and how to label + key each row.
def _flatten_acls(objects_by_type: Dict[str, List[dict]]) -> List[dict]:
    """Flatten every object's ACL into one row per (object, principal, permission).

    This is the migratable UNIT for permissions — so it must be a countable inventory line
    (the result report reconciles each grant on the target). Handles both the object
    permissions shape (access_control entries with `all_permissions`) and the secret-scope
    ACL shape (`{principal, permission}`).
    """
    rows: List[dict] = []

    def _obj_name(rec: dict) -> str:
        raw = rec.get("_raw") if isinstance(rec.get("_raw"), dict) else {}
        return safe_str_local(
            rec.get("cluster_name") or rec.get("instance_pool_name") or rec.get("name")
            or rec.get("title") or rec.get("display_name") or rec.get("path")
            or rec.get("displayName") or (rec.get("settings") or {}).get("name")
            or rec.get("job_id")
            # Fall back into the raw API object (legacy SQL alerts/queries keep the
            # human name only under _raw.display_name).
            or raw.get("display_name") or raw.get("name") or raw.get("title"))

    def _emit_object_acl(otype_label: str, rec: dict):
        for entry in rec.get("acl") or []:
            if not isinstance(entry, dict):
                continue
            principal = (entry.get("user_name") or entry.get("group_name")
                         or entry.get("service_principal_name") or "—")
            for perm in entry.get("all_permissions") or [{}]:
                rows.append({
                    "object_type": otype_label,
                    "object_key": _obj_name(rec),
                    "principal": principal,
                    "permission": safe_str_local(perm.get("permission_level")),
                    "inherited": bool(perm.get("inherited")),
                })

    # Object ACLs across all the buckets our collectors enrich with `acl`.
    for j in objects_by_type.get("job", []) or []:
        _emit_object_acl("job", j)
    for c in objects_by_type.get("compute", []) or []:
        _emit_object_acl(c.get("compute_type", "compute"), c)
    for s in objects_by_type.get("sql", []) or []:
        if s.get("acl"):
            # warehouse / legacy_query / legacy_alert / legacy_dashboard
            _emit_object_acl(safe_str_local(s.get("sql_type")) or "sql", s)
    for p in objects_by_type.get("dlt_pipeline", []) or []:
        _emit_object_acl("dlt_pipeline", p)
    for d in objects_by_type.get("lakeview_dashboard", []) or []:
        _emit_object_acl("lakeview_dashboard", d)
    for g in objects_by_type.get("genie_space", []) or []:
        _emit_object_acl("genie_space", g)
    for e in objects_by_type.get("serving_endpoint", []) or []:
        _emit_object_acl("serving_endpoint", e)
    for a in objects_by_type.get("app", []) or []:
        _emit_object_acl("app", a)
    for w in objects_by_type.get("workspace_object", []) or []:
        if w.get("acl"):
            _emit_object_acl(w.get("object_type", "workspace_object").lower(), w)

    # Secret-scope ACLs use a different shape ({principal, permission}).
    for sc in objects_by_type.get("secret_scope", []) or []:
        for item in sc.get("acls") or []:
            if not isinstance(item, dict):
                continue
            rows.append({
                "object_type": "secret_scope",
                "object_key": safe_str_local(sc.get("name")),
                "principal": safe_str_local(item.get("principal")),
                "permission": safe_str_local(item.get("permission")),
                "inherited": False,
            })

    return rows


def _resolve_items(data: Dict[str, Any], key: str) -> List[Dict]:
    """Return the item list for a component key, applying derived splits (ref parity)."""
    if key == "notebooks":
        return [x for x in data.get("workspace_items", []) if x.get("object_type") == "NOTEBOOK"]
    if key == "workspace_files":
        return [x for x in data.get("workspace_items", []) if x.get("object_type") == "FILE"]
    return data.get(key, []) or []


def build_counts(data: Dict[str, Any]) -> Dict[str, int]:
    """Per-card counts (in-scope keys only), with the notebook/file split."""
    counts: Dict[str, int] = {}
    for key in _SUMMARY_CARD_KEYS:
        counts[key] = len(_resolve_items(data, key))
    return counts


def fmt_epoch_ms(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)
