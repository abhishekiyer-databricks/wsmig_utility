"""
transforms — payload normalization for Export (SOURCE side) + reference-remap stubs (target).

Two distinct responsibilities live here:

1. EXPORT side (Plan 2 — IMPLEMENTED here):
     • `strip_runtime(asset_type, payload)` — drop non-importable *runtime* fields (server ids,
       state, timestamps, live counters) so the create-ready payload carries only what the
       target needs to recreate the asset. Driven by the per-asset `STRIP_FIELDS` registry;
       entries may be exact keys OR simple glob patterns (`state*`, `*_time`, `*_by_user*`).
     • `fingerprint(payload)` — sha256 over the canonical JSON of the STRIPPED payload
       (master §9). Deterministic + stable across runs iff the migratable content is unchanged,
       so the target-side state store can decide create/update/skip. Server ids/timestamps/state
       are stripped BEFORE hashing, so a re-run of an unchanged asset yields the SAME fingerprint.
     • `normalize(payload)` — the canonical JSON string the fingerprint hashes (sorted keys, no
       whitespace); exposed so callers/tests can inspect exactly what was hashed.

2. TARGET side (applied inside 04_Import; the cross-stage review/diff report is deferred to Plan 4):
     apply_user_mappings / apply_excludes / pause_schedules / remap_references.
   Reference remapping (source ids → new target ids) is deliberately NOT done at export — the
   export payloads still reference source ids; the map old→new only exists after identity import.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Per-asset runtime-field strip registry (Plan 2 §5).
#
# Keys are the fine-grained `asset_type`. Values are field patterns removed from the payload
# before it is written / fingerprinted. A pattern is either an exact top-level key or a glob
# (fnmatch) — globs let us catch families like every `*_time` / `*_by_user*` on a cluster
# without listing each one. Only TOP-LEVEL keys are matched (runtime cruft lives at the top of
# these API objects); nested specs (tasks, definition, serialized_dashboard) are preserved whole.
# ---------------------------------------------------------------------------

STRIP_FIELDS: dict[str, list[str]] = {
    # ── Identity (payload = SCIM _raw) ────────────────────────────────────
    # `id` is the source SCIM id (new id minted on target); `meta`/`groups` are server-derived.
    # `groups` is a server-derived membership back-reference (source ids) — membership is
    # rebuilt target-side from each group's `members` list, so strip it from user + SP.
    "user": ["id", "meta", "groups"],
    "service_principal": ["id", "meta", "groups"],
    "group": ["id", "meta", "groups"],
    # Built-in group membership: payload is {displayName, members}. Member entries carry the
    # SOURCE scim id in `value` plus a `$ref` back-link; the importer resolves each member by
    # its `display`/userName on the target, so the source-local ids are noise for the hash.
    "group_membership": [],
    # ── Compute ───────────────────────────────────────────────────────────
    "instance_pool": ["instance_pool_id", "stats", "status", "default_tags",
                      "state"],
    # `is_default`/`policy_family_version`/`creator_user_name` are server-derived — not
    # accepted by cluster-policies/create (verified against the SDK create signature).
    "cluster_policy": ["policy_id", "created_at_timestamp", "creator_user_name",
                       "is_default", "policy_family_version"],
    # Verified live: the clusters/get response also carries `creator_user_name`,
    # `driver_healthy`, `cluster_source`, `*_instance_source`, `disk_spec` (pool-only field),
    # and the resolved `effective_spark_version`/`release_version` — none are create fields.
    "cluster": ["cluster_id", "state", "state_message", "spark_context_id",
                "default_tags", "*_time", "*_by_user*", "last_state_loss_time",
                "last_restarted_time", "cluster_log_status", "termination_reason",
                "driver", "executors", "jdbc_port", "cluster_memory_mb", "cluster_cores",
                "start_time", "terminated_time", "init_scripts_safe_mode",
                "creator_user_name", "driver_healthy", "cluster_source", "disk_spec",
                "instance_source", "driver_instance_source", "effective_spark_version",
                "release_version", "spec"],
    # ── Workspace content ─────────────────────────────────────────────────
    "directory": [],
    "notebook": [],          # bytes live in content/; metadata payload has nothing runtime
    "workspace_file": [],
    # repos/create takes url+provider+path(+sparse_checkout); `branch`/`head_commit_id` are
    # checkout STATE (a branch is selected post-create via update) and git_cli_enabled is
    # a workspace-level server flag.
    "repo": ["id", "head_commit_id", "git_cli_enabled"],
    # ── Secrets ───────────────────────────────────────────────────────────
    "secret_scope": [],
    # ── Jobs (payload = settings) ─────────────────────────────────────────
    # `settings` itself has no server ids; run/trigger state lives outside it. Custom builder
    # strips the outer job_id/created_time/creator; nothing runtime remains inside settings.
    "job": [],
    # ── SQL ───────────────────────────────────────────────────────────────
    # `creator_id` is the numeric server id (create takes `creator_name`); `size` is the
    # read-only echo of `cluster_size`, which is the real create field.
    "sql_warehouse": ["id", "state", "health", "num_active_sessions", "num_clusters",
                      "jdbc_url", "odbc_params", "creator_name", "creator_id", "size"],
    # The current /api/2.0/sql/queries surface returns create_time/update_time/
    # owner_user_name/last_modifier_user_name/lifecycle_state (the old redash names
    # created_at/updated_at/user never appear) — strip both spellings.
    "legacy_query": ["id", "created_at", "updated_at", "user", "last_modified_by",
                     "user_id", "last_modified_by_id",
                     "create_time", "update_time", "owner_user_name",
                     "last_modifier_user_name", "lifecycle_state"],
    "legacy_alert": ["id", "created_at", "updated_at", "state", "user",
                     "create_time", "update_time", "owner_user_name", "lifecycle_state",
                     "trigger_time"],
    "legacy_dashboard": ["id", "created_at", "updated_at", "user",
                         "create_time", "update_time", "owner_user_name", "lifecycle_state"],
    # `effective_run_as` is the server-resolved identity (the settable field is `run_as`).
    "alert_v2": ["id", "create_time", "update_time", "lifecycle_state", "owner_user_name",
                 "trigger_state", "effective_run_as", "run_as_user_name"],
    # ── DLT (payload = spec) ──────────────────────────────────────────────
    # The pipeline SPEC nests the pipeline id as `id` (not `pipeline_id`), and
    # `pipeline_type` is server-derived — carrying either to target creates a conflict.
    "dlt_pipeline": ["pipeline_id", "state", "cluster_id", "latest_updates",
                     "creator_user_name", "run_as_user_name", "last_modified",
                     "id", "pipeline_type"],
    # ── AI/BI dashboards (payload = _raw with serialized_dashboard) ───────
    "lakeview_dashboard": ["dashboard_id", "create_time", "update_time", "path",
                           "lifecycle_state", "etag"],
    # ── Serving (payload = config) ────────────────────────────────────────
    "serving_endpoint": ["state", "creation_timestamp", "last_updated_timestamp",
                         "config_version", "id", "creator"],
    # ── Misc ──────────────────────────────────────────────────────────────
    "global_init_script": ["script_id", "created_at", "updated_at", "created_by", "updated_by"],
    "cluster_library": ["status", "is_library_for_all_clusters", "messages"],
    "workspace_conf": [],
    # ── Genie (auto — payload is our assembled create body: title+description+warehouse_id+
    # serialized_space). No runtime fields in that shape, so nothing to strip; the fingerprint
    # covers serialized_space so a changed space re-exports with a new fingerprint. ──
    "genie_space": [],
}


def _matches_any(key: str, patterns: Iterable[str]) -> bool:
    """True if `key` equals or glob-matches any pattern (exact keys never contain glob chars)."""
    for pat in patterns:
        if key == pat or (any(ch in pat for ch in "*?[") and fnmatch.fnmatch(key, pat)):
            return True
    return False


def strip_runtime(asset_type: str, payload: dict) -> dict:
    """Return a shallow copy of `payload` with this asset's runtime fields removed.

    Unknown asset types strip nothing (safe default — better to over-carry than to drop a
    field we didn't anticipate; a missing strip only makes a fingerprint slightly noisier).
    """
    if not isinstance(payload, dict):
        return payload
    patterns = STRIP_FIELDS.get(asset_type, [])
    if not patterns:
        return dict(payload)
    return {k: v for k, v in payload.items() if not _matches_any(k, patterns)}


def _canonical(value: Any) -> Any:
    """Recursively order-normalize a payload for hashing.

    Some source APIs return LIST members in a different order on every call — SCIM group
    `members` is the proven case (verified live: three consecutive GETs on the same group
    returned the same 9 members in three different orders). Hashing the raw order made an
    UNCHANGED group produce a new fingerprint on every export, which would make the target's
    cross-run UPSERT re-"update" it forever. Sorting lists by their own canonical form makes
    the fingerprint depend on SET content, not on server ordering.

    Order is not semantically meaningful for the payload lists we fingerprint (members,
    entitlements, tasks, libraries, grants). We only sort for the HASH — `normalize` is used
    for fingerprinting, never to produce the create body that gets written to the bundle, so
    the payload the target replays keeps its original order.
    """
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [_canonical(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"),
                                                      default=str, ensure_ascii=False))
    return value


def normalize(payload: Any) -> str:
    """Canonical JSON string used for fingerprinting: sorted keys, ordered lists, no whitespace.

    `default=str` keeps it total (any stray non-JSON value becomes its string form) so
    fingerprinting never raises on an unexpected payload shape.
    """
    return json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"), default=str,
                      ensure_ascii=False)


def fingerprint(payload: Any) -> str:
    """`sha256:<hex>` over the canonical JSON of `payload` (master §9).

    Caller passes the ALREADY-STRIPPED payload (Export strips first, then fingerprints), so the
    hash reflects only importable content — an unchanged asset re-exports to the same fingerprint.
    """
    digest = hashlib.sha256(normalize(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# TARGET-side transforms (Plans 3–7) — applied on STAGED copies, never the raw export.
# Kept as stubs here so the export build doesn't pull them in prematurely.
# ---------------------------------------------------------------------------

def apply_user_mappings(obj: dict, transform_cfg) -> dict:
    raise NotImplementedError


def apply_excludes(items: list, patterns: list) -> list:
    raise NotImplementedError


def pause_schedules(job: dict, transform_cfg) -> dict:
    raise NotImplementedError


def remap_references(obj: dict, identity_map) -> dict:
    raise NotImplementedError
