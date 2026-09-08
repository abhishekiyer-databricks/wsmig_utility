"""
acl_writer — collect every object + secret-scope ACL into `export/acls.json` (Plan 2 §5, D5).

ACLs live in ONE file, separate from the create payloads, because on the target each grant's
principal is a SOURCE entity id/name (SP/group/user) that must be REMAPPED to the new target
entity id before the ACL is applied — and for Databricks-managed SPs/groups that new id only
exists after identity import. Keeping ACLs in one remappable file makes the target-side ACL pass
clean and lets each object payload stay principal-free.

Output is keyed by the SAME `(asset_type, natural_key, source_id)` unit key the export records
use (asset_export), so the runner can stamp an `acl_grants` count onto each unit and the target
can join ACLs to the objects it (re)creates. Principals are captured VERBATIM (source ids/names);
`admins`-group and `inherited` grants are kept here (dropped on import, master §10a) so the target
has full information.

`acls.json` shape (a flat list):
  [{asset_type, natural_key, source_id, perm_object_type,
    grants:[{principal, principal_type, permission_level, inherited}]}]
"""
from __future__ import annotations

from src.utils.helpers import safe_str

# principal field on an access_control entry → principal_type.
_PRINCIPAL_FIELDS = (("user_name", "user"),
                     ("group_name", "group"),
                     ("service_principal_name", "service_principal"))


def _grants_from_object_acl(acl) -> list[dict]:
    """Flatten a permissions-API access_control_list into grant rows (one per principal×level)."""
    out: list[dict] = []
    for entry in acl or []:
        if not isinstance(entry, dict):
            continue
        principal, ptype = "", "unknown"
        for field, kind in _PRINCIPAL_FIELDS:
            if entry.get(field):
                principal, ptype = safe_str(entry.get(field)), kind
                break
        for perm in entry.get("all_permissions") or []:
            if not isinstance(perm, dict):
                continue
            out.append({
                "principal": principal,
                "principal_type": ptype,
                "permission_level": safe_str(perm.get("permission_level")),
                "inherited": bool(perm.get("inherited")),
            })
    return out


def _entry(asset_type: str, natural_key: str, source_id, perm_object_type: str,
           grants: list[dict]) -> dict:
    return {
        "asset_type": asset_type,
        "natural_key": natural_key,
        "source_id": safe_str(source_id),
        "perm_object_type": perm_object_type,
        "grants": grants,
    }


def collect_acls(objects_by_type: dict[str, list]) -> list[dict]:
    """Build the `export/acls.json` list from the inventory objects.

    Only objects that carry at least one grant are emitted (an empty ACL is not a migratable
    unit of work). The `(asset_type, natural_key, source_id)` key matches asset_export exactly.
    """
    acls: list[dict] = []

    def add(asset_type, natural_key, source_id, perm_type, raw_acl):
        grants = _grants_from_object_acl(raw_acl)
        if grants:
            acls.append(_entry(asset_type, natural_key, source_id, perm_type, grants))

    # ── Compute ───────────────────────────────────────────────────────────
    for c in objects_by_type.get("compute", []) or []:
        ct = c.get("compute_type")
        if ct == "instance_pool":
            add("instance_pool", safe_str(c.get("_natural_key")), c.get("instance_pool_id"),
                "instance-pools", c.get("acl"))
        elif ct == "cluster_policy":
            add("cluster_policy", safe_str(c.get("_natural_key")), c.get("policy_id"),
                "cluster-policies", c.get("acl"))
        elif ct == "cluster":
            add("cluster", safe_str(c.get("_natural_key")), c.get("cluster_id"),
                "clusters", c.get("acl"))

    # ── Workspace content ─────────────────────────────────────────────────
    _WS_PERM = {"NOTEBOOK": "notebooks", "FILE": "files", "DIRECTORY": "directories",
                "REPO": "repos"}
    _WS_ASSET = {"NOTEBOOK": "notebook", "FILE": "workspace_file", "DIRECTORY": "directory",
                 "REPO": "repo"}
    for w in objects_by_type.get("workspace_object", []) or []:
        otype = safe_str(w.get("object_type"))
        add(_WS_ASSET.get(otype, otype.lower()), safe_str(w.get("path")),
            w.get("object_id") or w.get("repo_id"), _WS_PERM.get(otype, "notebooks"), w.get("acl"))

    # ── Jobs ──────────────────────────────────────────────────────────────
    for j in objects_by_type.get("job", []) or []:
        add("job", safe_str(j.get("name")), j.get("job_id"), "jobs", j.get("acl"))

    # ── SQL ───────────────────────────────────────────────────────────────
    _SQL_PERM = {"warehouse": "sql/warehouses", "legacy_query": "queries",
                 "legacy_alert": "alerts", "legacy_dashboard": "dashboards", "alert": "alertsv2"}
    _SQL_ASSET = {"warehouse": "sql_warehouse", "legacy_query": "legacy_query",
                  "legacy_alert": "legacy_alert", "legacy_dashboard": "legacy_dashboard",
                  "alert": "alert_v2"}
    for s in objects_by_type.get("sql", []) or []:
        st = safe_str(s.get("sql_type"))
        add(_SQL_ASSET.get(st, st), safe_str(s.get("_natural_key") or s.get("name")),
            s.get("id"), _SQL_PERM.get(st, st), s.get("acl"))

    # ── DLT / dashboards / genie / serving / apps ─────────────────────────
    for p in objects_by_type.get("dlt_pipeline", []) or []:
        add("dlt_pipeline", safe_str(p.get("name")), p.get("pipeline_id"), "pipelines", p.get("acl"))
    for d in objects_by_type.get("lakeview_dashboard", []) or []:
        add("lakeview_dashboard", safe_str(d.get("display_name")), d.get("dashboard_id"),
            "dashboards", d.get("acl"))
    for g in objects_by_type.get("genie_space", []) or []:
        add("genie_space", safe_str(g.get("title")), g.get("space_id"), "genie", g.get("acl"))
    for e in objects_by_type.get("serving_endpoint", []) or []:
        add("serving_endpoint", safe_str(e.get("name")), e.get("name"), "serving-endpoints",
            e.get("acl"))
    for a in objects_by_type.get("app", []) or []:
        add("app", safe_str(a.get("name")), a.get("name"), "apps", a.get("acl"))

    # ── Secret scopes (different shape: {principal, permission}) ──────────
    for sc in objects_by_type.get("secret_scope", []) or []:
        grants = []
        for item in sc.get("acls") or []:
            if not isinstance(item, dict):
                continue
            grants.append({
                "principal": safe_str(item.get("principal")),
                "principal_type": "unknown",   # secret ACL API doesn't type the principal
                "permission_level": safe_str(item.get("permission")),
                "inherited": False,
            })
        if grants:
            acls.append(_entry("secret_scope", safe_str(sc.get("name")), safe_str(sc.get("name")),
                               "secret-scope", grants))

    return acls


def acl_counts(acls: list[dict]) -> dict[tuple, int]:
    """Map `(asset_type, natural_key)` → total grant count, for stamping onto export records."""
    counts: dict[tuple, int] = {}
    for e in acls:
        key = (e["asset_type"], e["natural_key"])
        counts[key] = counts.get(key, 0) + len(e.get("grants") or [])
    return counts
