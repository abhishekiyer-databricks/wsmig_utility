"""
asset_export — turn inventory records into create-ready per-unit EXPORT RECORDS (Plan 2 §3, §5).

Input  : `objects_by_type` (the collectors' output, as stored in inventory.json).
Output : `units_by_type` = {asset_type: [unit, ...]}, where a *unit* is the per-unit export
         record (§3) PLUS a `payload` (the normalized, runtime-stripped create body).

Each bucket produced by a collector (coarse `object_type`) is EXPLODED into the fine-grained
`asset_type` taxonomy the reconciliation report reconciles on (master §9 / Plan 2 §3). For most
assets the payload is `strip_runtime(asset_type, <source object>)`; a few (identity, jobs, DLT,
serving, dashboards, secrets, misc) need small custom source selection because the create body
is a sub-object (`settings` / `spec` / `config`) or an assembled dict rather than the raw list item.

Design boundaries (kept deliberately narrow):
  • No I/O, no network, no threads here — pure transform of in-memory inventory records. Content
    BYTES (notebooks/files) are fetched separately (`content_fetcher`, under the parallel pool);
    this module only marks those units `mode="content"` and leaves `content_ref=None` for the
    fetcher to fill.
  • No ACL grants counted here — `acl_writer` is the single source of truth for ACLs; the runner
    stamps the per-unit `acl_grants` count back onto these units so the two never disagree.
  • Reference remap (source ids → target ids) is TARGET-side; payloads keep source ids verbatim.
"""
from __future__ import annotations

from typing import Any, Optional

from src.transform.transforms import fingerprint, strip_runtime
from src.utils.helpers import folder_natural_key, safe_str

# migration_mode → default export_status (the fetcher/runner may override for content/skip).
# `covered` = the object is the on-disk twin of an asset already exported via its NATIVE API
# (a .lvdash.json / .dbalert.json whose dashboard/alert is captured as a lakeview_dashboard /
# alert_v2 unit). It's recorded — so reconciliation stays complete and nothing is silently
# dropped — but carries NO payload and NO bytes (import recreates it via the native asset, not
# by uploading the file), avoiding a double-create.
# `export_status` answers ONE question: did EXPORT capture this unit? It is NOT a prediction of
# what import will do — that's `migration_mode` + `import_action` (below).
#   success           — payload/bytes captured, ready for import
#   manual            — cannot be auto-recreated at all (secret values, oversize, UC-backed)
#   dab               — bundle-owned; recorded in the ledger but payload DELIBERATELY skipped
#   covered           — already exported via its native asset (no double-create)
#   skip              — toggled off
#   skipped_oversize  — exceeded an API size cap (set by the content fetcher)
#   failure           — export itself errored
# `dab` stays its own status rather than collapsing into `success`, because the create payload
# genuinely is NOT captured for these — the bundle owns the definition. It renders as
# "Skipped (DAB)" so it reads as a deliberate skip rather than a failure, and the paired
# `import_action` ("dab_redeploy") says who does recreate it.
_MODE_STATUS = {"auto": "success", "content": "success", "manual": "manual", "dab": "dab",
                "covered": "covered"}

# What the TARGET side will do with a unit, derived from its identity classification. Export
# status stays `success` for all of these (we DID export them) — this field carries the
# create-vs-assign distinction so the report can't be misread as "the tool will create it".
#   create           — the utility creates the object on target (new id minted)
#   adopt_or_assign  — account user/SPN: adopted/assigned AUTOMATICALLY by the workspace SCIM POST
#   assign_on_target — account GROUP: must ALREADY exist in the target account; the utility assigns
#                      it to the workspace + sets entitlements, but never creates it
#   review_required   — low-confidence classification; a human must confirm before import
#
# Plan 6 kinds. `account` covers users, SPs and account groups alike: for users/SPs the workspace
# SCIM POST itself performs the assignment (so no account admin is needed), while an account GROUP
# is assigned via permissionassignments. Both are "assign_on_target" from the reader's point of
# view — the utility does not mint a new identity.
# `adopt_or_assign` vs `assign_on_target` — the distinction MATTERS to a reader planning the cutover:
#   adopt_or_assign  (users/SPNs, and account groups already assigned) — FULLY AUTOMATIC. The
#                    workspace SCIM POST creates-at-account-and-assigns, and an SPN POST carrying
#                    applicationId adopts the existing account SPN. No account admin, no waiting.
#   assign_on_target (account GROUPS) — the group must already exist in the TARGET ACCOUNT; the
#                    utility assigns it, but cannot create it (that would make a blocking shadow).
# Collapsing both into "ASSIGN (must pre-exist)" was misleading: it told the operator to chase
# account-admin work for every user and SPN that the tool already does by itself.
_IMPORT_ACTION_BY_CLASS = {
    "account": "adopt_or_assign",          # overridden to assign_on_target for GROUPS below
    "workspace_local": "create",
    "system": "add_members",
    "system_generated": "skip_generated",  # Databricks-minted artifact (users-clone-…) → do nothing
    "needs_review": "review_required",
    # Legacy classifications, kept so a bundle exported by an older version still imports.
    "entra_user": "adopt_or_assign",
    "umi_or_entra_sp": "adopt_or_assign",
    "account_group": "assign_on_target",
    "db_managed_sp": "create",
    "db_managed_group": "create",
    "builtin_group": "assign_on_target",
    "unknown": "review_required",
}

# ── import_action for EVERY unit, not just identity ──────────────────────────────────────────
# `export_status` says whether export captured a unit; `import_action` says what the TARGET side
# will do with it. Without the second column a reader has to infer intent from the status, which
# is exactly how "DAB" got misread as "not exported". So every unit carries one of these:
#   create             — the utility creates the object on target via REST
#   create_and_upload  — create the object, then push its BYTES (notebooks / workspace files)
#   adopt_or_assign    — account user/SPN: the utility adopts or assigns it AUTOMATICALLY (no
#                        account-admin step; an SPN keeps its applicationId)
#   assign_on_target   — account GROUP: must ALREADY exist in the target ACCOUNT; assign + entitle
#   add_members        — built-in group: PATCH members onto the group that already exists
#   dab_redeploy       — bundle-owned; import SKIPS it, the customer's bundle redeploy owns it
#   via_native_asset   — created as a side effect of its native asset (the on-disk twin)
#   install            — attached to an existing object rather than created (cluster libraries)
#   set_conf           — a workspace setting written via the conf API
#   apply_acl          — permission grants replayed once the object exists
#   manual             — no REST path; a human does it on target
#   review_required    — low-confidence classification; confirm before import
#   none               — nothing to import (toggled off, or export failed)
_ACTION_CREATE = "create"
_ACTION_DAB = "dab_redeploy"

# The CLOSED vocabulary. `export_excel` renders a human label per action and any value missing
# from its map silently degrades to "—", so the label map is checked against this set at import
# time (see export_excel) — a new action can't be added here and forgotten there.
IMPORT_ACTIONS = frozenset({
    "create", "create_and_upload", "assign_on_target", "adopt_or_assign", "add_members",
    "dab_redeploy", "skip_generated",
    "via_native_asset", "install", "set_conf", "apply_acl", "manual", "review_required", "none",
})

# migration_mode → import_action, for units with no identity classification and no per-type
# override below. Content units upload bytes; manual units stay manual; covered units are
# created by their native asset.
_ACTION_BY_MODE = {
    "auto": _ACTION_CREATE,
    "content": "create_and_upload",
    "manual": "manual",
    "dab": _ACTION_DAB,
    "covered": "via_native_asset",
}

# asset_types whose import verb is NOT "create" even though their mode is `auto`. A cluster
# library is INSTALLED onto an existing cluster and a workspace conf key is SET, neither of
# which is a create; calling both "create" would misdescribe what import does.
_ACTION_BY_ASSET_TYPE = {
    "cluster_library": "install",
    "workspace_conf": "set_conf",
}

# export_status values that mean there is nothing for import to do at all. Checked AFTER the
# mode/type mapping so a toggled-off or failed unit never advertises an action.
_ACTION_BY_STATUS = {
    "skip": "none",
    "failure": "none",
    # An oversize notebook/file WAS recorded but its bytes never made it into the bundle, so
    # import cannot recreate it — it's a manual copy, not a create_and_upload.
    "skipped_oversize": "manual",
}


# Workspace content that lives inside a Databricks Asset Bundle's root folder. The CLI's default
# `root_path` is `<home>/.bundle/<bundle>/<target>`, so this one path segment identifies the whole
# bundle-owned tree: the deployed source under `files/` AND the deployment state under `state/`.
#
# Deliberately matched on the DIRECTORY, not on state filenames: the state format is already
# mid-migration (CLI ≥1.x writes `state/resources.json`, older CLIs `state/terraform.tfstate` — both
# appear live on fvm1) and the newer direct-deployment engine drops Terraform altogether. A rule
# keyed to filenames would silently stop covering what it was written to protect; a rule keyed to
# the folder survives the engine change.
#
# Uploading this content is not merely redundant, it is HARMFUL: `state/terraform.tfstate` maps
# bundle resources to SOURCE-workspace object ids (verified live: a tfstate on fvm1 pins
# databricks_job → 627957782356291). Landing that on the target makes the customer's next
# `bundle deploy` believe those resources already exist under those ids, so it updates or deletes
# the wrong objects. The bundle recreates every one of these files on redeploy anyway.
#
# So they are EXPORTED (34 KB, and the only record of what was actually deployed if a customer's
# bundle has drifted from git) but never imported — same `dab_redeploy` action as the bundle-owned
# jobs/pipelines themselves, so the whole DAB story reads consistently. A customer who overrides
# `root_path` is not detected here; the consequence is a redundant upload, never a skipped asset,
# because DAB-OWNERSHIP of real assets is decided by `dab_registry` from the state files, not by
# this path check.
_DAB_ROOT_SEGMENT = "/.bundle/"

# asset_types whose units can sit inside a bundle root (plain workspace content only — a job or
# warehouse is bundle-owned via `dab_registry`, and its `migration_mode` is already "dab").
_DAB_CONTENT_TYPES = {"notebook", "workspace_file", "directory"}


def _is_configured_bundle_root_dir(key: str, roots) -> bool:
    """Whether `key` IS a configured bundle-root directory itself (holds bundle state) — the
    generalisation of the `.bundle` container-dir case for PLAN 11 Finding-12's configurable roots."""
    import fnmatch
    from src.utils.helpers import _resolve_dab_roots, normalize_ws_path
    norm = normalize_ws_path(key)
    segs = [s for s in norm.split("/") if s]
    last = segs[-1] if segs else ""
    for m in _resolve_dab_roots(roots):
        if m.startswith("/"):
            if norm == m.rstrip("/"):
                return True
        elif last and fnmatch.fnmatch(last, m):
            return True
    return False


def is_dab_content_path(asset_type: str, natural_key: str, roots=None) -> bool:
    """Whether this unit is workspace content inside a bundle's root folder (never imported).

    Matches the bundle-root container directory ITSELF as well as everything under it. Testing only
    for the `/.bundle/` segment let `/Shared/.bundle` (and `/Users/<email>/.bundle`) fall through
    to `create`, so the one directory that exists purely to hold bundle state read "CREATE on
    target" while every file inside it correctly read "DAB REDEPLOY".

    PLAN 11 Finding-12: the default `.bundle` behaviour below is byte-identical to before; any
    additional configured `roots` (e.g. a Team-B directory root with no `.bundle` segment) are then
    matched via `dab_path_info` + the root-dir check, so a team that roots a bundle at a plain
    directory has its content skipped at export just like a `.bundle` deployment."""
    if safe_str(asset_type) not in _DAB_CONTENT_TYPES:
        return False
    key = safe_str(natural_key)
    # Fast path — the CLI-standard `.bundle`, unchanged.
    if _DAB_ROOT_SEGMENT in key or key.endswith(_DAB_ROOT_SEGMENT.rstrip("/")):
        return True
    # Finding-12: any additional configured bundle roots (under a root, or the root dir itself).
    from src.utils.helpers import dab_path_info
    if dab_path_info(key, roots)["deployed_by_dab"]:
        return True
    return _is_configured_bundle_root_dir(key, roots)


# Git repos: inventoried + exported as metadata, never imported (customer 2026-08-05, §6a).
REPO_MANUAL_NOTE = ("git repos are OUT OF SCOPE for import — recreate manually on target "
                    "(Repos ▸ Add Repo with this url/provider/branch at this path), then "
                    "re-apply its permissions from the ACL report")

DAB_CONTENT_NOTE = ("inside a DAB bundle root — exported for reference but NOT imported; "
                    "`databricks bundle deploy` against the target recreates it "
                    "(importing bundle state would point the bundle at source-workspace ids)")


def dab_bundle_root(natural_key: str, roots=None) -> str:
    """`/Shared/.bundle/my_bundle` for any path inside it, else "" — one row per bundle in the
    manual-actions report, rather than one per file. Finding-12: the default `.bundle` grouping is
    unchanged; a configured non-`.bundle` root falls back to `dab_path_info`'s bundle_root."""
    key = safe_str(natural_key)
    idx = key.find(_DAB_ROOT_SEGMENT)
    if idx >= 0:
        after = key[idx + len(_DAB_ROOT_SEGMENT):].split("/")
        return key[:idx] + _DAB_ROOT_SEGMENT + (after[0] if after else "")
    from src.utils.helpers import dab_path_info
    return dab_path_info(key, roots).get("bundle_root", "") or ""


def derive_import_action(unit: dict, roots=None) -> str:
    """The TARGET-side action for one unit: classification first, then mode/type, then status.

    Identity units keep the classification-driven answer (create vs assign is load-bearing —
    creating an Azure UMI instead of assigning it mints a new applicationId and orphans every
    ACL that referenced it). Everything else is derived from `migration_mode`, with a couple of
    per-asset_type verb overrides, and finally overridden by a status that means "nothing to do".

    `roots` = the configured `dab_bundle_roots` (Finding-12): a team that roots a bundle at a
    non-`.bundle` directory (e.g. `/Shared/DAB_Deployments/<bundle>`) must have its CONTENT skipped
    on import, not just its native resources. Without threading the roots here the content matched
    only the hard-coded `.bundle` fast path, so a Team-B directory-root bundle's notebooks/files/dirs
    were re-created on target — duplicating what the customer's bundle redeploy owns (PLAN 11 Run 3).
    """
    status = safe_str(unit.get("export_status"))
    if status in _ACTION_BY_STATUS:
        return _ACTION_BY_STATUS[status]
    asset_type = safe_str(unit.get("asset_type"))
    mode = safe_str(unit.get("migration_mode"))
    # Bundle-root content: exported, never imported (see _DAB_ROOT_SEGMENT). Checked before the
    # mode mapping so `content`/`auto` can't win and advertise CREATE + UPLOAD. `migration_mode`
    # is intentionally NOT changed — that would drop these units out of the payload files and
    # strand their ACL grants, so the unit still travels with its permissions and the importer
    # skips creation on the strength of this action. Honour BOTH the path check (roots-aware) AND
    # the collector's already-stamped `deployed_by_dab` flag (computed with the configured roots),
    # so a configured non-`.bundle` root is caught however the unit reached us.
    if asset_type in _DAB_CONTENT_TYPES and (
            is_dab_content_path(asset_type, unit.get("natural_key"), roots)
            or unit.get("deployed_by_dab")):
        return _ACTION_DAB
    # Built-in group MEMBERSHIP is an action the utility DOES perform (PATCH members onto the
    # pre-existing group), so it must not inherit the group's own "assign_on_target".
    if asset_type == "group_membership":
        return "add_members"
    cls = safe_str(unit.get("classification"))
    if cls:
        # A bundle-owned identity would still be redeployed by the bundle, so mode wins over
        # classification there; otherwise the classification is the authority.
        if mode == "dab":
            return _ACTION_DAB
        action = _IMPORT_ACTION_BY_CLASS.get(cls, "review_required")
        # An account GROUP is the only identity that can still need a human: it must already exist
        # in the TARGET ACCOUNT before it can be assigned (the utility must not create it — that
        # makes a workspace-local shadow which permanently blocks the real group). Users and SPNs
        # keep `adopt_or_assign`, which is fully automatic.
        if action == "adopt_or_assign" and asset_type == "group":
            return "assign_on_target"
        return action
    if mode == "auto" and asset_type in _ACTION_BY_ASSET_TYPE:
        return _ACTION_BY_ASSET_TYPE[asset_type]
    return _ACTION_BY_MODE.get(mode, "review_required")

# asset_type → bundle-relative artifact file that holds its units' payloads (Plan 2 §4).
ARTIFACT_PATH: dict[str, str] = {
    "user": "export/identity/users.json",
    "service_principal": "export/identity/service_principals.json",
    "group": "export/identity/groups.json",
    # membership of the BUILT-IN groups (admins/users) — the groups themselves are never
    # recreated, but their members must be re-added on target.
    "group_membership": "export/identity/builtin_group_membership.json",
    "instance_pool": "export/compute/instance_pools.json",
    "cluster_policy": "export/compute/cluster_policies.json",
    "cluster": "export/compute/clusters.json",
    "directory": "export/workspace/objects.json",
    "notebook": "export/workspace/objects.json",
    "workspace_file": "export/workspace/objects.json",
    "repo": "export/workspace/repos.json",
    "secret_scope": "export/secrets/scopes.json",
    "secret_value": "export/secrets/scopes.json",
    "job": "export/jobs.json",
    "sql_warehouse": "export/sql/warehouses.json",
    "legacy_query": "export/sql/legacy_queries.json",
    "legacy_alert": "export/sql/legacy_alerts.json",
    "legacy_dashboard": "export/sql/legacy_dashboards.json",
    "alert_v2": "export/sql/alerts_v2.json",
    "dlt_pipeline": "export/dlt/pipelines.json",
    "lakeview_dashboard": "export/dashboards/lakeview.json",
    "genie_space": "export/genie/spaces.json",
    "serving_endpoint": "export/serving/endpoints.json",
    "global_init_script": "export/misc/global_init_scripts.json",
    "cluster_library": "export/misc/cluster_libraries.json",
    "workspace_conf": "export/misc/workspace_conf.json",
    "app": "export/manual/apps.json",
    "lakebase_project": "export/manual/lakebase.json",
}

# Coarse collector toggle that governs each asset_type (Config.AssetToggles fields). asset_types
# with no toggle (apps/lakebase) are inventory-only and always emitted as manual regardless.
TOGGLE_FOR: dict[str, str] = {
    "user": "identity", "service_principal": "identity", "group": "identity",
    "group_membership": "identity",
    "instance_pool": "compute", "cluster_policy": "compute", "cluster": "compute",
    "directory": "workspace", "notebook": "workspace", "workspace_file": "workspace",
    "repo": "workspace",
    # non-content workspace objects surfaced by the walk (recorded manual; §5 no-silent-gaps)
    "lakeview_dashboard_file": "workspace", "alert_v2_file": "workspace",
    "mlflow_experiment": "workspace", "workspace_library": "workspace",
    "secret_scope": "secrets", "secret_value": "secrets",
    "job": "jobs",
    "sql_warehouse": "sql", "legacy_query": "sql", "legacy_alert": "sql",
    "legacy_dashboard": "sql", "alert_v2": "sql",
    "dlt_pipeline": "dlt",
    "lakeview_dashboard": "dashboards",
    "genie_space": "genie",
    "serving_endpoint": "serving",
    "global_init_script": "misc", "cluster_library": "misc", "workspace_conf": "misc",
}


def _make_unit(asset_type: str, natural_key: str, source_id: str, payload_source: Optional[dict],
               *, mode: str, migratable: bool = True, note: str = "",
               content_ref: Optional[str] = None, extra: Optional[dict] = None,
               fingerprint_extra: Optional[dict] = None) -> dict:
    """Assemble one export record + its stripped/fingerprinted payload.

    `payload_source` None → no create payload (manual/dab units): payload={} and the fingerprint
    is computed over the empty dict (stable, meaningless — such units aren't upserted by content).

    `fingerprint_extra` adds fields to the FINGERPRINT INPUT ONLY, never to `payload` — for
    source-side metadata that matters for change detection but is not a create field (§7c-audit).
    """
    stripped = strip_runtime(asset_type, payload_source) if isinstance(payload_source, dict) else {}
    fp_input = {**stripped, **fingerprint_extra} if fingerprint_extra else stripped
    unit = {
        "asset_type": asset_type,
        "natural_key": natural_key,
        "source_id": safe_str(source_id),
        "fingerprint": fingerprint(fp_input),
        "migratable": bool(migratable),
        "migration_mode": mode,
        "export_status": _MODE_STATUS.get(mode, "success"),
        "artifact": ARTIFACT_PATH.get(asset_type, ""),
        "content_ref": content_ref,
        "note": note,
        "acl_grants": 0,          # stamped by the runner from acl_writer (single source of truth)
        "payload": stripped,
    }
    if extra:
        unit.update(extra)
    # EVERY unit carries the target-side action explicitly, so `export_status=success` can never
    # be mistaken for "the utility will create this on target" (and `dab` can never be misread
    # as "not exported"). The runner re-derives this after toggles/content so a unit whose status
    # changed late — toggled off, oversize, failed — reports the right action.
    unit["import_action"] = derive_import_action(unit)
    return unit


def index_record(unit: dict) -> dict:
    """The unit MINUS its payload — what goes into export_index.json (the ledger, §3)."""
    return {k: v for k, v in unit.items() if k != "payload"}


# ---------------------------------------------------------------------------
# Per-bucket builders. Each takes the collector's list for one coarse object_type and returns
# a flat list of units (possibly across several fine-grained asset_types).
# ---------------------------------------------------------------------------

# Library keys whose value is a PATH to a binary artifact (as opposed to pypi/maven/cran, which
# name a package a repo re-resolves on the target).
_LIBRARY_PATH_KEYS = ("jar", "whl", "egg", "requirements")
# Path prefixes whose CONTENT this tool does not export. `dbfs:/` (incl. the /dbfs FUSE spelling)
# is out of scope per PLAN_2 §5b. UC Volumes + workspace files DO migrate, so they're fine.
_UNEXPORTED_PREFIXES = ("dbfs:/", "/dbfs/")


def _dbfs_library_ref(library: Any) -> str:
    """Return the DBFS path a library points at, or "" if it needs no exported bytes.

    Used to flag libraries that would migrate as a DANGLING reference: the definition carries
    over but the artifact itself never reaches the target.
    """
    if not isinstance(library, dict):
        return ""
    for key in _LIBRARY_PATH_KEYS:
        val = safe_str(library.get(key))
        if val and val.lower().startswith(_UNEXPORTED_PREFIXES):
            return val
    return ""


def _job_dbfs_library_refs(settings: dict) -> list[str]:
    """Every DBFS-backed library referenced by a job's tasks (task-level + job-level env)."""
    refs: list[str] = []
    if not isinstance(settings, dict):
        return refs
    for task in settings.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        for lib in task.get("libraries") or []:
            ref = _dbfs_library_ref(lib)
            if ref:
                refs.append(ref)
    return refs


def _identity_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        itype = r.get("identity_type")
        raw = r.get("_raw") if isinstance(r.get("_raw"), dict) else {}
        # Plan 6: `kind` is the authority; `classification` is kept as its wire alias so an older
        # importer still reads something meaningful and the reports keep one column name.
        cls = safe_str(r.get("kind")) or safe_str(r.get("classification"))
        # Carried on EVERY identity unit: the importer needs `kind` to choose create-vs-assign,
        # `entra_backed` only to word a remediation message, and `workspace_permissions` to
        # reproduce workspace ADMIN vs USER (invisible in SCIM entitlements).
        id_extra = {
            "classification": cls,
            "kind": cls,
            "entra_backed": bool(r.get("entra_backed")),
            "workspace_permissions": r.get("workspace_permissions"),
        }
        if itype == "user":
            out.append(_make_unit("user", safe_str(r.get("userName")), r.get("id"), raw,
                                  mode="auto", extra=id_extra,
                                  fingerprint_extra={"_ws_perms": r.get("workspace_permissions")}))
        elif itype == "service_principal":
            # Tri-state, NOT bool(): None means the check itself failed (a workspace-admin SP
            # cannot read another SP's credentials — needs account_admin). Reporting that as
            # "no secrets" would silently drop a manual action, so it gets its own note.
            has_secrets = r.get("has_secrets")
            if has_secrets is None:
                note = ("could not check for OAuth client secret(s) — the running identity lacks "
                        "account_admin; if this SPN has secrets they are NOT exportable and must "
                        "be recreated on target manually. VERIFY MANUALLY.")
            elif has_secrets:
                note = ("OAuth client secret(s) present — NOT exportable; recreate on target "
                        "manually.")
            else:
                note = ""
            # `has_secrets` is collected but is NOT a SCIM create field, so it never reached the
            # payload — meaning creating an OAuth secret on an existing source SPN left the
            # stripped SCIM object byte-identical, the fingerprint unmoved, and the manual action
            # silently skipped on every later run (§7c-audit GAP 2). Hashing it as a
            # fingerprint-only input makes false→true move the hash, so the state store reports
            # `updated` and re-emits the manual action on the run where it became true. It cannot
            # migrate the secret — client secrets are never readable — only resurface the ask.
            out.append(_make_unit("service_principal", safe_str(r.get("applicationId")),
                                  r.get("id"), raw, mode="auto", note=note,
                                  extra=id_extra,
                                  fingerprint_extra={"_has_secrets": has_secrets,
                                                     "_ws_perms": r.get("workspace_permissions")}))
        elif itype == "group":
            name = safe_str(r.get("displayName"))
            if cls in ("system", "builtin_group"):
                # The built-in group OBJECT already exists on target and must never be recreated
                # — but its MEMBERSHIP does not carry over by itself. Without this, a source
                # workspace admin would silently not be an admin on target. So we emit the group
                # as `covered` (exists, nothing to create) and a SEPARATE membership unit whose
                # payload is the member list, which the importer replays as a PATCH (add members
                # to the existing group) rather than a create.
                out.append(_make_unit("group", name, r.get("id"), None,
                                      mode="covered",
                                      note="built-in group — exists on target; membership "
                                           "migrated via the group_membership unit",
                                      extra=id_extra))
                out.append(_make_unit(
                    "group_membership", name, r.get("id"),
                    {"displayName": name, "members": raw.get("members") or []},
                    mode="auto",
                    note="add these members to the EXISTING built-in group on target "
                         "(PATCH members; never create the group)",
                    extra=id_extra))
            else:
                # An ACCOUNT group's members are account-global: patching them on target would
                # change that group in every OTHER workspace sharing the account, and Entra would
                # revert it anyway. Flagged so the importer assigns and leaves membership alone.
                group_extra = dict(id_extra)
                group_extra["members_are_account_owned"] = (cls == "account")
                out.append(_make_unit("group", name, r.get("id"), raw,
                                      mode="auto", extra=group_extra,
                                      fingerprint_extra={
                                          "_ws_perms": r.get("workspace_permissions")}))
    return out


def _dab_unit(asset_type: str, natural_key: str, source_id: Any, resource_word: str) -> dict:
    """A bundle-owned asset: recorded in the ledger, but no create payload.

    Recreating it via REST would produce a duplicate the bundle no longer manages, so the
    customer's bundle redeploy owns it. Kept in the index so reconciliation has no silent gap.
    """
    return _make_unit(asset_type, natural_key, source_id, None, mode="dab", migratable=False,
                      note=f"handled by DAB redeploy (bundle `{resource_word}` resource)")


def _compute_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        ct = r.get("compute_type")
        raw = r.get("_raw") if isinstance(r.get("_raw"), dict) else {}
        nk = safe_str(r.get("_natural_key"))
        if ct == "instance_pool":
            if r.get("deployed_by_dab"):
                out.append(_dab_unit("instance_pool", nk, r.get("instance_pool_id"),
                                     "instance_pools"))
            else:
                out.append(_make_unit("instance_pool", nk, r.get("instance_pool_id"), raw,
                                      mode="auto"))
        elif ct == "cluster_policy":
            # DAB has no cluster_policies resource type, so these are always hand-managed.
            out.append(_make_unit("cluster_policy", nk, r.get("policy_id"), raw, mode="auto"))
        elif ct == "cluster":
            if r.get("deployed_by_dab"):
                out.append(_dab_unit("cluster", nk, r.get("cluster_id"), "clusters"))
            else:
                out.append(_make_unit("cluster", nk, r.get("cluster_id"), raw, mode="auto"))
    return out


def _workspace_units(records: list[dict], native_paths: Optional[dict] = None) -> list[dict]:
    """Build units for the workspace walk.

    `native_paths` maps a workspace PATH → the native asset_type that already exports it (e.g. a
    `.lvdash.json` path → "lakeview_dashboard"). Used to DEDUPE the walk's DASHBOARD/ALERT file
    twins against their native units so each dashboard/alert is counted once (Plan 2 review).
    """
    native_paths = native_paths or {}
    out: list[dict] = []
    for r in records:
        otype = safe_str(r.get("object_type"))
        path = safe_str(r.get("path"))
        oid = r.get("object_id") or r.get("repo_id")
        if otype == "DIRECTORY":
            out.append(_make_unit("directory", path, oid, {"path": path}, mode="auto"))
        elif otype == "NOTEBOOK":
            out.append(_make_unit(
                "notebook", path, oid,
                {"path": path, "object_type": "NOTEBOOK", "language": safe_str(r.get("language"))},
                mode="content", extra={"owner": _owner_of(path)}))
        elif otype == "FILE":
            out.append(_make_unit(
                "workspace_file", path, oid, {"path": path, "object_type": "FILE"},
                mode="content", extra={"owner": _owner_of(path)}))
        elif otype == "REPO":
            raw = r.get("_raw") if isinstance(r.get("_raw"), dict) else {}
            src = raw or {"path": path, "url": safe_str(r.get("url")),
                          "provider": safe_str(r.get("provider")), "branch": safe_str(r.get("branch"))}
            # Repos are OUT OF SCOPE for import (customer 2026-08-05, D9/§6a): never created on
            # target, always `manual`. The PAYLOAD is deliberately kept — it is metadata only
            # (url/provider/branch/path; the collector never descends into a git folder, so zero
            # file bytes and zero content-fetch calls), and that metadata IS the manual recreate
            # runbook. Dropping it would make the manual step worse for no saving.
            note = (REPO_MANUAL_NOTE if src.get("url") else
                    "no repo URL — cannot auto-recreate; manual")
            out.append(_make_unit("repo", path, r.get("repo_id"), src, mode="manual",
                                  migratable=False, note=note))
        else:
            # Object types that are NOT plain workspace content: DASHBOARD/ALERT are the on-disk
            # twins of assets exported via their NATIVE API; MLFLOW_EXPERIMENT is out of scope.
            # Never dropped silently, never re-exported as file bytes.
            out.append(_workspace_other_unit(otype, path, oid, native_paths, r))
    return out


# Non-content workspace object_types the walk can return → (asset_type, note).
_WS_OTHER = {
    "DASHBOARD": ("lakeview_dashboard_file",
                  "AI/BI dashboard artifact (.lvdash.json)"),
    "ALERT": ("alert_v2_file",
              "Alerts V2 artifact (.dbalert.json)"),
    "MLFLOW_EXPERIMENT": ("mlflow_experiment",
                          "MLflow is out of scope for this utility (assets-only migration)"),
    "LIBRARY": ("workspace_library",
                "workspace library object — handled via cluster libraries, not as content"),
}


def _workspace_other_unit(otype: str, path: str, oid, native_paths: dict,
                          record: Optional[dict] = None) -> dict:
    asset_type, note = _WS_OTHER.get(
        otype, (f"workspace_{otype.lower()}",
                f"unhandled workspace object_type {otype!r} — recorded, not exported"))
    # If this file is the on-disk twin of a natively-exported dashboard/alert → `covered`
    # (recorded, no gap, no double-create), else `manual` (MLflow/out-of-scope/unhandled).
    native = _native_twin_of(otype, record or {}, native_paths)
    if native and otype in ("DASHBOARD", "ALERT"):
        return _make_unit(asset_type, path, oid, None, mode="covered", migratable=False,
                          note=f"{note} — exported via native `{native}` unit (not re-uploaded)")
    if otype in ("DASHBOARD", "ALERT"):
        # Under a `.bundle/` path with no native match: this is the bundle's own SOURCE copy
        # (`<root>/files/…`), which the customer's bundle redeploy recreates. That's known, not
        # something for a human to review, so don't send it to the manual worklist.
        if (record or {}).get("deployed_by_dab"):
            return _dab_unit(asset_type, path, oid, "bundle source file")
        # No native match and not bundle-owned → recorded manual so it's never a silent gap.
        return _make_unit(asset_type, path, oid, None, mode="manual", migratable=False,
                          note=f"{note} — no native asset match; review")
    return _make_unit(asset_type, path, oid, None, mode="manual", migratable=False, note=note)


def _owner_of(path: str) -> str:
    """Owning user email for a `/Users/<email>/…` path, else "" (Plan 2 §6b)."""
    parts = [p for p in safe_str(path).split("/") if p]
    return parts[1] if len(parts) >= 2 and parts[0] == "Users" else ""


def _secret_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        name = safe_str(r.get("name"))
        if r.get("deployed_by_dab"):
            # DAB owns `secret_scopes` too. The scope is redeployed by the bundle; its VALUES
            # are still a manual step (below), since no API ever returns them.
            out.append(_dab_unit("secret_scope", name, name, "secret_scopes"))
            for key in r.get("key_names") or []:
                out.append(_make_unit(
                    "secret_value", f"{name}/{safe_str(key)}", "", None, mode="manual",
                    migratable=False,
                    note="scope redeployed by DAB, but the secret VALUE is never readable via "
                         "the API — re-populate manually (≤128 KB)"))
            continue
        backend = safe_str(r.get("backend_type")).upper() or "DATABRICKS"
        payload = {
            "name": name,
            "backend_type": backend,
            "keyvault_metadata": r.get("keyvault_metadata"),
            "key_names": r.get("key_names") or [],
        }
        if backend == "AZURE_KEYVAULT":
            # An AKV-backed scope needs an Azure AD `userAADToken` to create, which is NOT obtainable
            # in this deployment (Databricks SPN secret → Databricks token; MI-backed SPN → IMDS,
            # unreachable from a private/notebook-only workspace — IMP-4, proven live). So it is a
            # MANUAL step, marked as such AT EXPORT (never attempted at import), with the vault named.
            dns = safe_str((r.get("keyvault_metadata") or {}).get("dns_name")) or "the source vault"
            out.append(_make_unit(
                "secret_scope", name, name, payload, mode="manual", migratable=False,
                note=(f"AZURE KEY VAULT-backed — recreate by hand on target against vault {dns} "
                      f"(Create Scope → Azure Key Vault), then re-run with retry_mode=failed_only to "
                      f"adopt it. Cannot be automated: creating it needs an Azure AD token that a "
                      f"Databricks SPN credential / managed-identity-backed SPN cannot provide from a "
                      f"private, notebook-only workspace. Its VALUES are re-populated manually too.")))
        else:
            out.append(_make_unit("secret_scope", name, name, payload, mode="auto"))
        # Values are never exportable → one manual unit per key so the target report lists each
        # value to re-populate (cap 128 KB; never readable via the API).
        for key in r.get("key_names") or []:
            out.append(_make_unit(
                "secret_value", f"{name}/{safe_str(key)}", "", None, mode="manual",
                migratable=False,
                note="secret value re-populated manually (never readable via API; ≤128 KB)"))
    return out


def _job_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        name = safe_str(r.get("name"))
        jid = safe_str(r.get("job_id"))
        if r.get("deployed_by_dab"):
            out.append(_make_unit("job", name, jid, None, mode="dab", migratable=False,
                                  note="handled by DAB redeploy (Azure DevOps bundle pipeline)"))
        else:
            settings = r.get("settings") if isinstance(r.get("settings"), dict) else {}
            # A job task can pull a library from dbfs:/ too. The job itself still migrates
            # (mode auto — its definition is complete), but we surface the dangling artifact so
            # it lands in manual_actions.md instead of failing at first run.
            dbfs_refs = _job_dbfs_library_refs(settings)
            note = ("" if not dbfs_refs else
                    f"task library artifact(s) on DBFS not exported (DBFS out of scope): "
                    f"{', '.join(dbfs_refs[:3])} — copy to target before running")
            out.append(_make_unit("job", name, jid, settings, mode="auto", note=note))
    return out


def _sql_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        st = r.get("sql_type")
        raw = r.get("_raw") if isinstance(r.get("_raw"), dict) else {}
        nk = safe_str(r.get("_natural_key") or r.get("name"))
        sid = safe_str(r.get("id"))
        if st == "warehouse":
            if r.get("deployed_by_dab"):
                out.append(_dab_unit("sql_warehouse", nk, sid, "sql_warehouses"))
            else:
                out.append(_make_unit("sql_warehouse", nk, sid, raw, mode="auto"))
        elif st == "legacy_query":
            out.append(_make_unit("legacy_query", nk, sid, raw, mode="auto"))
        elif st == "legacy_alert":
            # Legacy alerts are MANUAL, like legacy dashboards (IMP-5). The v1 create API wants the
            # old flat `options{column,op,value}` shape, but the read API only returns the newer
            # `condition{}` shape, so a round-trip create fails — attempting it produced a permanent
            # red result on every run. The underlying `legacy_query` still migrates, so only the
            # alert's threshold/notification wrapper is rebuilt (as an Alerts V2 alert on target).
            out.append(_make_unit(
                "legacy_alert", nk, sid, None, mode="manual", migratable=False,
                note="legacy SQL alerts use an obsolete create API (v1 `options` shape) that modern "
                     "workspaces reject — rebuild this as an Alerts V2 alert on target. Its "
                     "underlying query HAS migrated, so only the alert condition/notification is "
                     "recreated by hand."))
        elif st == "legacy_dashboard":
            mode = "dab" if r.get("deployed_by_dab") else "auto"
            out.append(_make_unit("legacy_dashboard", nk, sid, None if mode == "dab" else raw,
                                  mode=mode, migratable=(mode != "dab"),
                                  note=("handled by DAB redeploy" if mode == "dab" else "")))
        elif st == "alert":
            mode = "dab" if r.get("deployed_by_dab") else "auto"
            out.append(_make_unit("alert_v2", nk, sid, None if mode == "dab" else raw,
                                  mode=mode, migratable=(mode != "dab"),
                                  note=("handled by DAB redeploy" if mode == "dab" else "")))
    return out


def _dlt_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        name = safe_str(r.get("name"))
        pid = safe_str(r.get("pipeline_id"))
        if r.get("deployed_by_dab"):
            out.append(_make_unit("dlt_pipeline", name, pid, None, mode="dab", migratable=False,
                                  note="handled by DAB redeploy (spec.deployment.kind=BUNDLE)"))
        else:
            spec = r.get("spec") if isinstance(r.get("spec"), dict) else {}
            out.append(_make_unit("dlt_pipeline", name, pid, spec, mode="auto"))
    return out


def _lakeview_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        name = safe_str(r.get("display_name"))
        did = safe_str(r.get("dashboard_id"))
        # PLAN 11 Finding-9: the natural key is the FULL PATH (`<parent_path>/<display_name>`), never
        # the bare display_name — two same-named dashboards in different folders must not collapse
        # onto one target (the 2nd would silently overwrite the 1st = data loss). The collector's
        # `natural_key()` already computes this for the report; the export UNIT (which is what flows
        # into the state table + import) MUST match, or the importer's full-path `existing_keys`
        # never lines up. Falls back to the bare name when parent_path is genuinely absent.
        nk = folder_natural_key(r.get("parent_path"), name)
        if r.get("deployed_by_dab"):
            out.append(_make_unit("lakeview_dashboard", nk, did, None, mode="dab",
                                  migratable=False, note="handled by DAB redeploy"))
        else:
            # PLAN 8 Bug 7 (Lakeview sibling): carry `parent_path` so a user-created dashboard's
            # `.lvdash.json` is recreated in the SAME user folder on target, not at the API default.
            payload = {"display_name": name, "warehouse_id": safe_str(r.get("warehouse_id")),
                       "serialized_dashboard": r.get("serialized_dashboard"),
                       "parent_path": safe_str(r.get("parent_path"))}
            out.append(_make_unit("lakeview_dashboard", nk, did, payload, mode="auto"))
    return out


def _genie_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        name = safe_str(r.get("title"))
        sid = safe_str(r.get("space_id"))
        serialized = r.get("serialized_space")
        # PLAN 11 Finding-9: full-path natural key (`<parent_path>/<title>`), never the bare title,
        # so two same-named Genie spaces in different folders don't collapse onto one target. Matches
        # the collector's `natural_key()` and the importer's full-path `existing_keys`.
        nk = folder_natural_key(r.get("parent_path"), name)
        if serialized:
            # AUTO-migratable (verified): create-ready body = serialized_space + title +
            # description + warehouse_id. Target calls create_space/update_space, remapping the
            # warehouse_id. serialized_space kept verbatim (string) — the API round-trips it.
            # DAB supports `genie_spaces` as a bundle resource type (CLI v1.10.0+), so a Genie
            # space CAN be bundle-managed — recreating it via REST would duplicate an asset the
            # bundle owns. The collector already flags it from parent_path (`.bundle/`); honour it.
            if r.get("deployed_by_dab"):
                out.append(_make_unit(
                    "genie_space", nk, sid, None, mode="dab", migratable=False,
                    note="handled by DAB redeploy (bundle `genie_spaces` resource)"))
                continue
            # PLAN 8 Bug 7 (Genie sibling): carry `parent_path` so the space is recreated in its
            # SOURCE folder (verified live: Genie create HONORS parent_path), not the default home.
            payload = {"title": name, "description": safe_str(r.get("description")),
                       "warehouse_id": safe_str(r.get("warehouse_id")),
                       "serialized_space": serialized,
                       "parent_path": safe_str(r.get("parent_path"))}
            out.append(_make_unit(
                "genie_space", nk, sid, payload, mode="auto",
                note="recreate via Genie create_space/update_space; remap warehouse_id; "
                     "serialized_space may reference UC tables that must pre-exist on target"))
        else:
            # No serialized_space returned (API/permission gap) → can't recreate → manual.
            payload = {"title": name, "description": safe_str(r.get("description")),
                       "warehouse_id": safe_str(r.get("warehouse_id"))}
            out.append(_make_unit(
                "genie_space", nk, sid, payload, mode="manual", migratable=False,
                note="serialized_space unavailable (not returned by the API) — recreate manually"))
    return out


def _serving_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        name = safe_str(r.get("name"))
        if r.get("deployed_by_dab"):
            out.append(_dab_unit("serving_endpoint", name, name, "model_serving_endpoints"))
            continue
        migratable = bool(r.get("migratable"))
        config = r.get("config") if isinstance(r.get("config"), dict) else {}
        out.append(_make_unit(
            "serving_endpoint", name, name, config if migratable else None,
            mode="auto" if migratable else "manual", migratable=migratable,
            note=safe_str(r.get("migration_note"))))
    return out


def _misc_units(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        mt = r.get("misc_type")
        if mt == "global_init_script":
            payload = {"name": safe_str(r.get("name")), "position": r.get("position"),
                       "enabled": r.get("enabled"), "script_b64": r.get("script_b64")}
            out.append(_make_unit("global_init_script", safe_str(r.get("_natural_key")),
                                  r.get("script_id"), payload, mode="auto"))
        elif mt == "cluster_library":
            lib = r.get("library")
            payload = {"cluster_id": safe_str(r.get("cluster_id")), "library": lib}
            # A library whose artifact lives on DBFS (jar/whl/egg at dbfs:/...) only exports as a
            # REFERENCE — DBFS content is out of scope (PLAN_2 §5b), so the bytes never reach the
            # bundle and the path won't exist on target. pypi/maven/cran re-resolve from their
            # repos, so only the dbfs-backed ones are affected. Flag them as manual instead of
            # reporting a `success` that would silently break the cluster on target.
            dbfs_ref = _dbfs_library_ref(lib)
            if dbfs_ref:
                out.append(_make_unit(
                    "cluster_library", safe_str(r.get("_natural_key")), r.get("cluster_id"),
                    payload, mode="manual", migratable=False,
                    note=f"library artifact `{dbfs_ref}` lives on DBFS — the FILE is not exported "
                         f"(DBFS out of scope); copy it to the target and re-point the library, "
                         f"or the cluster will fail to start"))
            else:
                out.append(_make_unit("cluster_library", safe_str(r.get("_natural_key")),
                                      r.get("cluster_id"), payload, mode="auto"))
        elif mt == "workspace_conf":
            payload = {"key": safe_str(r.get("key")), "value": r.get("value")}
            out.append(_make_unit("workspace_conf", safe_str(r.get("key")),
                                  safe_str(r.get("key")), payload, mode="auto"))
    return out


def _app_units(records: list[dict]) -> list[dict]:
    return [_make_unit("app", safe_str(r.get("name")), safe_str(r.get("name")), None,
                       mode="manual", migratable=False,
                       note="Databricks App — source + resource bindings migrated manually (v1)")
            for r in records]


def _lakebase_units(records: list[dict]) -> list[dict]:
    return [_make_unit("lakebase_project", safe_str(r.get("name")), safe_str(r.get("name")), None,
                       mode="manual", migratable=False,
                       note="Lakebase — data + connection topology migrated manually (v1)")
            for r in records]


# coarse object_type → builder
_BUILDERS = {
    "identity": _identity_units,
    "compute": _compute_units,
    "workspace_object": _workspace_units,
    "secret_scope": _secret_units,
    "job": _job_units,
    "sql": _sql_units,
    "dlt_pipeline": _dlt_units,
    "lakeview_dashboard": _lakeview_units,
    "genie_space": _genie_units,
    "serving_endpoint": _serving_units,
    "misc": _misc_units,
    "app": _app_units,
    "lakebase_project": _lakebase_units,
}


def _native_content_paths(objects_by_type: dict) -> dict[str, str]:
    """Identity key → native asset_type, for deduping the walk's dashboard/alert file twins.

    A dashboard/alert appears TWICE in the inventory: once via its native API (the real asset,
    with a create payload) and once as the `.lvdash.json` / `.dbalert.json` object the workspace
    walk returns. The twin must be marked `covered` so it is neither re-uploaded as bytes nor
    counted as a second asset.

    Matching is by **ASSET ID, not path**. Paths don't work: an Alerts V2 record returns
    `parent_path: null` even on a detail GET (verified live on fvm1), so its twin was falling
    through to `manual`. Worse, keying on a dashboard's `parent_path` maps a whole DIRECTORY —
    so an unrelated dashboard sitting in the same folder got silently marked `covered` and its
    own export was never checked. Ids are exact.

    Keys are `"id:<value>"` so they can't collide with the path keys still used for lookup.
    """
    out: dict[str, str] = {}
    for d in objects_by_type.get("lakeview_dashboard", []) or []:
        did = safe_str(d.get("dashboard_id"))
        if did:
            out[f"id:{did}"] = "lakeview_dashboard"
    for s in objects_by_type.get("sql", []) or []:
        if s.get("sql_type") == "alert":
            aid = safe_str(s.get("id"))
            if aid:
                out[f"id:{aid}"] = "alert_v2"
    return out


def _native_twin_of(otype: str, record: dict, native_keys: dict) -> str:
    """The native asset_type that already exports this DASHBOARD/ALERT walk entry, else "".

    A DASHBOARD's `resource_id` is the Lakeview `dashboard_id`; an ALERT's `object_id` is the
    Alerts V2 id (its `resource_id` is an unrelated uuid). Both are checked so a collector that
    populates only one of them still dedupes.
    """
    for candidate in (record.get("resource_id"), record.get("object_id")):
        key = f"id:{safe_str(candidate)}"
        if safe_str(candidate) and key in native_keys:
            return native_keys[key]
    return ""


def build_all(objects_by_type: dict[str, list]) -> dict[str, list[dict]]:
    """Build every unit from the inventory, grouped by fine-grained `asset_type`.

    Toggle handling is the RUNNER's job (it rewrites toggled-off families to `skip`); this
    function always builds full payloads so the transform stays pure + independently testable.
    """
    native_paths = _native_content_paths(objects_by_type)
    units_by_type: dict[str, list[dict]] = {}
    for object_type, builder in _BUILDERS.items():
        records = objects_by_type.get(object_type) or []
        if object_type == "workspace_object":
            units = builder(records, native_paths)
        else:
            units = builder(records)
        for unit in units:
            units_by_type.setdefault(unit["asset_type"], []).append(unit)
    return units_by_type
