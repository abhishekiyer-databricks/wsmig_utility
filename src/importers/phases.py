"""
phases — the import phase ORDER, the prerequisite graph, and selector validation (Plan 3 §5, §6).

Two separate concerns live here, both pure data + pure functions so they are trivially testable:

1. **Phase order.** Assets are created in dependency order, identity first and ACLs dead last. A
   grant names a *principal* AND an *object*, so it can only be applied once both id maps exist —
   which is why ACLs are their own final phase rather than a step inside each asset's importer.

2. **Prerequisite validation.** The operator may run one family at a time (`import_assets=genie`).
   That is a first-class flow, not a hack — but selecting a family whose prerequisites are neither
   selected NOR already recorded in the migration state table is a HARD ERROR listing what's
   missing, because e.g. `jobs` without compute cannot remap `existing_cluster_id` and would
   silently create jobs pointing at source-workspace cluster ids.

   The "already recorded" half is what makes phase-at-a-time migration work at all, and it is
   exactly why the state table stores target ids: a prerequisite satisfied by a PREVIOUS session
   loads its id map from the table and the run proceeds normally.
"""
from __future__ import annotations

from typing import Optional

# Phase order (Plan 3 §6). The tuple order IS the execution order.
#
# PLAN 11 (Finding-10 follow-up): sql + dlt run BEFORE jobs. A job task can trigger a SQL warehouse
# (`sql_task.warehouse_id`), a DLT pipeline (`pipeline_task.pipeline_id`) or another job
# (`run_job_task.job_id`) — i.e. jobs DEPEND on sql and dlt, never the reverse (a warehouse/pipeline
# never references a job). With jobs after sql+dlt, those references resolve on the FIRST pass
# instead of erroring as a (retryable) prerequisite and healing only on a retry run. Job→job
# (`run_job_task`) is intra-phase and still resolves on the first pass when the referenced job is
# earlier in the bundle, else on `retry_mode=failed_only` — we do not topologically sort within the
# jobs phase.
PHASE_ORDER = ("identity", "compute", "workspace", "secrets", "sql", "dlt", "jobs",
               "dashboards", "genie", "serving", "misc", "acls")

# family → the families it needs, either selected in THIS session or already in the state table.
#   • identity gates everything that names a principal (owners, run_as, scope MANAGE, ACLs).
#   • compute + workspace gate jobs/dlt (cluster ids and notebook paths must exist to remap).
#   • sql gates dlt/dashboards/genie because all three carry a `warehouse_id` to remap.
#   • acls needs every id map, hence it depends on all of them.
PREREQUISITES: dict[str, tuple] = {
    "identity": (),
    "compute": ("identity",),
    "workspace": ("identity",),
    "secrets": ("identity",),
    "jobs": ("identity", "compute", "workspace"),
    "sql": ("identity",),
    "dlt": ("identity", "compute", "workspace", "sql"),
    "dashboards": ("sql",),
    "genie": ("sql",),
    "serving": ("identity",),
    "misc": ("compute",),          # cluster libraries attach to clusters
    "acls": ("identity",),         # object families are checked leniently — see validate_selection
}

# family → the asset_types it produces. Used to ask the state table "is this prerequisite already
# satisfied from an earlier session?" and to build each phase's id map.
FAMILY_ASSET_TYPES: dict[str, tuple] = {
    "identity": ("user", "service_principal", "group", "group_membership"),
    "compute": ("instance_pool", "cluster_policy", "cluster"),
    "workspace": ("directory", "notebook", "workspace_file", "repo"),
    "secrets": ("secret_scope", "secret_value"),
    "jobs": ("job",),
    "sql": ("sql_warehouse", "legacy_query", "legacy_alert", "legacy_dashboard", "alert_v2"),
    "dlt": ("dlt_pipeline",),
    "dashboards": ("lakeview_dashboard",),
    "genie": ("genie_space",),
    "serving": ("serving_endpoint",),
    "misc": ("global_init_script", "cluster_library", "workspace_conf"),
    "acls": ("acl",),
}

# The coarse `migrate_*` toggle that governs each family, so import can report "this family is not
# in the bundle because export was told to skip it" rather than silently doing nothing.
TOGGLE_FOR_FAMILY = {f: ("identity" if f == "acls" else f) for f in PHASE_ORDER}


def ordered(families) -> list[str]:
    """The given families in PHASE order (so widget input order is irrelevant)."""
    wanted = {str(f).strip().lower() for f in families}
    return [f for f in PHASE_ORDER if f in wanted]


def asset_types_for(families) -> tuple:
    """Every asset_type produced by these families."""
    out: list[str] = []
    for f in ordered(families):
        out.extend(FAMILY_ASSET_TYPES.get(f, ()))
    return tuple(out)


def family_of(asset_type: str) -> str:
    """The family an asset_type belongs to ("" if unknown, e.g. an inventory-only app)."""
    for family, types in FAMILY_ASSET_TYPES.items():
        if asset_type in types:
            return family
    return ""


def validate_selection(selected, state_store=None) -> list[str]:
    """Return the unmet prerequisites for `selected`, as human-readable strings. [] = OK.

    A prerequisite is MET when it is either (a) selected in this session, or (b) already present in
    the migration state table for this workspace pair — (b) is what makes running one family at a
    time work, and it's why the table stores target ids.

    Returns messages rather than raising so the caller decides (the notebook raises; a test can
    assert on the text). Each message names the family, what it needs, and how to fix it — a bare
    "unmet prerequisite" would leave the operator guessing.
    """
    chosen = set(ordered(selected))
    problems: list[str] = []
    for family in ordered(selected):
        for prereq in PREREQUISITES.get(family, ()):
            if prereq in chosen:
                continue
            if state_store is not None and state_store.has_family(
                    FAMILY_ASSET_TYPES.get(prereq, ())):
                continue
            problems.append(
                f"`{family}` needs `{prereq}` — it is neither selected in this run nor recorded in "
                f"the migration state table for this workspace pair. Without it, {family} cannot "
                f"resolve its {_why(family, prereq)}. Fix: add `{prereq}` to import_assets, or "
                f"import it in an earlier run.")
    return problems


def _why(family: str, prereq: str) -> str:
    """The concrete reference that would be unresolvable — so the error teaches, not just blocks."""
    reasons = {
        ("jobs", "compute"): "`existing_cluster_id` / `instance_pool_id` / `policy_id` references",
        ("jobs", "workspace"): "task `notebook_path` references",
        ("jobs", "identity"): "`run_as` and `IS_OWNER` principals",
        ("dlt", "compute"): "cluster/policy references",
        ("dlt", "workspace"): "pipeline library `notebook` paths",
        ("dlt", "sql"): "warehouse references",
        ("dashboards", "sql"): "`warehouse_id`",
        ("genie", "sql"): "`warehouse_id`",
        ("misc", "compute"): "`cluster_id` for library installs",
        ("secrets", "identity"): "`initial_manage_principal`",
        ("acls", "identity"): "grant principals (users / SPs / groups)",
        ("compute", "identity"): "policy/pool ACL principals",
        ("workspace", "identity"): "`/Users/<email>` home directories (the user must exist first)",
        ("sql", "identity"): "query/alert owners",
        ("serving", "identity"): "endpoint permissions",
    }
    return reasons.get((family, prereq), f"{prereq} references")
