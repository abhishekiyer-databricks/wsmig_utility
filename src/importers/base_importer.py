"""
BaseImporter — the write-side mirror of BaseCollector, and the home of the fail-soft contract.

THE INVARIANT THIS FILE ENFORCES (D21, §7d(1a)):

    Every importer is FAIL-SOFT PER UNIT. One asset's failure — for ANY reason: missing
    prerequisite, API rejection, permission denied, unresolved dependency, unsupported operation —
    becomes that unit's `failed` row + report line, and NEVER propagates. The job's exit status
    reflects *the run completing*, not *every unit succeeding*.

Only four WHOLE-RUN preconditions abort, all before any unit is attempted, and all cases where
continuing would produce a *wrong* target rather than an incomplete one (bad manifest, preflight
NO-GO, unreachable state schema when live, unauthenticatable source client). Those are the
runner's business, not an importer's.

The per-unit flow (`run()`), which no subclass overrides:

    load units → for each: is it selected / retryable? → decide (state row + LIVE existence check)
              → dry-run or act → record outcome in the state store + the checkpoint → next unit

Two independent guards make this idempotent (§4), which is why a re-run converges whether or not
the checkpoint survived:
  1. the state-table decision (create/update/skip/adopt by fingerprint), and
  2. a LIVE existence check by natural key on the target, which catches objects created outside the
     tool or by an attempt that died between the API call and the bookkeeping write → ADOPT rather
     than duplicate. `RESOURCE_ALREADY_EXISTS` from any create is caught and downgraded to an adopt
     too, since a race between the check and the create is possible.

Subclasses implement the small asset-specific parts: `load()`, `existing_keys()`, `create_one()`,
and optionally `update_one()`. Everything else — ordering, dry-run, checkpointing, state,
error classification, reporting — lives here so it is written once and behaves the same everywhere.
"""
from __future__ import annotations

import posixpath
import time
from abc import ABC, abstractmethod
from collections import Counter, namedtuple
from typing import Any, Optional

from src.state.state_store import (ACTION_ADOPTED, ACTION_CREATED, ACTION_CREATED_WITH_WARNING,
                                   ACTION_FAILED, ACTION_MANUAL, ACTION_NOT_SELECTED,
                                   ACTION_SKIPPED, ACTION_SKIPPED_NO_OBJECT, ACTION_UPDATED,
                                   CAT_API_ERROR, CAT_DEPENDENCY_UNRESOLVED, CAT_NOT_SUPPORTED,
                                   CAT_PERMISSION_DENIED, CAT_PREREQUISITE_MISSING, UpsertAction)
from src.utils.helpers import (folder_natural_key, home_owner, looks_like_app_id, normalize_ws_path,
                               safe_str)
from src.utils.logger import get_logger

# The single decision a `/Users/<owner>/...` path resolves to (PLAN 9, lifted here in PLAN 11
# Finding-8 so the workspace importer AND the four folder-placed importers — SQL queries/alerts,
# Lakeview dashboards, Genie spaces — share ONE home-resolution decision instead of a second,
# divergent copy). `kind` ∈:
#   "not_home"      – not under /Users → the caller proceeds unchanged
#   "skip_root"     – the /Users/<owner> ROOT itself, provisioned not created (no-op)
#   "remapped_sp"   – owner is a recreated SP (sp_mapping) → /Users/<newAppId>/… (IMP-6)
#   "normal_home"   – owner's real home exists on target → use it as-is
#   "backup"        – divert to <backup_root>/<owner>/… (orphaned/deleted-in-source owner)
#   "prerequisite"  – owner absent on target and NOT eligible for backup → wait / recover on retry
HomeResolution = namedtuple("HomeResolution", ["target_path", "kind", "note"])

# Flush the checkpoint every N units. Every write to a UC Volume is a FULL-file rewrite (verified
# live — memory `uc-volume-file-io-limits`), so per-item flushing is O(n²) bytes; one flush at the
# end would lose all bookkeeping on a crash. Same batch size and rationale as the export pass.
CHECKPOINT_BATCH = 200

# API error text → (failure_category, remediation HINT). The hint is only ever APPENDED to the
# actual server error, never a replacement for it: the operator must always see what the target
# really said (a canned string that guesses the cause — e.g. "needs workspace-admin" when the real
# reason was a missing UC table — sends people down the wrong path). Matching only picks the retry
# BUCKET (category) and adds guidance; the raw message is surfaced verbatim by `classify_error`.
# Matched case-insensitively as substrings, first match wins.
_ERROR_MAP: tuple[tuple[str, str, str], ...] = (
    ("RESOURCE_DOES_NOT_EXIST", CAT_DEPENDENCY_UNRESOLVED,
     "hint: a referenced object does not exist on target — most often an unrecreated Git folder "
     "or a notebook path that was never imported. Recreate the dependency, then re-run with "
     "retry_mode=failed_only"),
    # PLAN 8 Bug 12: a job whose run_as relies on a warehouse grant 403s at CREATE time because ACLs
    # (the warehouse CAN_USE grant) are applied in the FINAL phase, AFTER jobs — an ORDERING artifact
    # that self-heals on retry, NOT a missing-access defect. Matched before PERMISSION_DENIED so this
    # precise hint wins. Filed prerequisite_missing so retry_mode=failed_only re-attempts it.
    ("not authorized to use or monitor this sql", CAT_PREREQUISITE_MISSING,
     "hint: EXPECTED on the first run — the run_as identity's warehouse grant (CAN_USE) is applied "
     "in the FINAL ACL phase, AFTER jobs are created, so this 403s at create time. Re-run with "
     "retry_mode=failed_only after the full run and it succeeds; this is NOT a missing-access defect "
     "and the tool must not auto-grant warehouse access."),
    ("PERMISSION_DENIED", CAT_PERMISSION_DENIED,
     "hint: this is usually workspace-admin on the target, but can also be a referenced object "
     "(warehouse / UC table) the identity cannot see — read the server message above to tell "
     "which"),
    ("must have userAADToken", CAT_PREREQUISITE_MISSING,
     "hint: linking an Azure Key Vault-backed secret scope needs an AZURE AD token, not a "
     "Databricks token — the run-as identity must be an Entra SP / managed identity (Plan 3 §6c)"),
    ("FEATURE_DISABLED", CAT_NOT_SUPPORTED,
     "hint: the feature is not enabled on the target workspace — enable it or accept the gap"),
    ("QUOTA_EXCEEDED", CAT_PREREQUISITE_MISSING,
     "hint: a target-region quota was exceeded — raise the quota, then retry with "
     "retry_mode=failed_only"),
    ("INVALID_PARAMETER_VALUE", CAT_API_ERROR,
     "hint: the target rejected a field in the payload — the offending field is named above"),
    ("TABLE_OR_VIEW_NOT_FOUND", CAT_PREREQUISITE_MISSING,
     "hint: the payload references a Unity Catalog table that does not exist on target. UC is OUT "
     "OF SCOPE for this utility, so the table must be created by the UC migration before this "
     "asset will work"),
    ("does not exist", CAT_DEPENDENCY_UNRESOLVED,
     "hint: a referenced object does not exist on target — check that the prerequisite family was "
     "imported first"),
)

# Substrings meaning "it's already there" — a create that races the existence check. Treated as an
# ADOPT, never a failure: the object exists, which is the outcome we wanted.
_ALREADY_EXISTS_MARKERS = ("RESOURCE_ALREADY_EXISTS", "already exists", "ALREADY_EXISTS")


class PrerequisiteMissing(RuntimeError):
    """A prerequisite the tool must NOT work around — raised by an importer, not by the API.

    Defined here (rather than per-importer) so `classify_error` can recognise it by type and file it
    as `prerequisite_missing`. That distinction is one the customer asked for: "waiting on customer
    IT / an account admin" and "the API rejected our payload" need different actions, so they must
    not look the same in the report.

    The raiser writes the ACTIONABLE message — which identity to assign, which vault to permission —
    and it is passed through verbatim rather than replaced by a generic one.
    """


class HardRemapFailure(RuntimeError):
    """A reference to an object that is NOT in the bundle at all — the `("", "")` case of
    `remap_id`, and a HARD, non-retryable failure (PLAN 11 Finding-10, the lift-and-shift rule).

    Distinct from PrerequisiteMissing so `retry_mode=failed_only` does NOT sweep it up (retrying
    forever against an object that will never appear is noise, not recovery). Classified
    `dependency_unresolved`; the raiser writes the actionable message, passed through verbatim.
    """


class SkippedNoObject(RuntimeError):
    """A declarative apply (an ACL) whose TARGET OBJECT does not exist (§6b-i, D23).

    Deliberately its own status — `skipped_no_object`, NOT `failed`. The object legitimately doesn't
    exist yet, usually BY DESIGN: bundle-owned content the customer's `bundle deploy` recreates, an
    out-of-scope Git repo, or a family deferred by `import_assets`. Filing it as a failure would make
    every bundle-using workspace show permanent red, which is precisely how an operator learns to
    ignore red. It sits in the `skipped_only` retry bucket, which is where "take it up later" belongs.

    `category` carries WHICH of the seven cases applied, so "which permissions are still outstanding,
    and why" is a SQL query rather than a hunt through Excel.
    """

    def __init__(self, message: str, category: str = "") -> None:
        super().__init__(message)
        self.category = category


class UnsupportedOperation(RuntimeError):
    """This asset has no REST create path in scope, so the tool never attempts it.

    Filed as `not_supported` rather than a failure, because attempting it would produce a permanent
    red result on every run forever, which trains the operator to ignore red (D10).
    """


def classify_error(exc: Exception) -> tuple[str, str]:
    """`(failure_category, human_message)` for an error.

    The message ALWAYS carries the actual server error verbatim — never a hardcoded string that
    replaces it. A matched `_ERROR_MAP` entry only adds a remediation HINT (and picks the retry
    bucket); it never hides what the target said. This is deliberate: a canned "needs
    workspace-admin" once masked a missing-UC-table / warehouse-permission genie failure, which is
    exactly the misdiagnosis this avoids. The full raw text is also kept in `error_raw`.
    """
    raw = str(exc).strip()
    # Raiser-authored messages (PrerequisiteMissing / UnsupportedOperation) are already the actual,
    # actionable text — pass through verbatim.
    if isinstance(exc, PrerequisiteMissing):
        return CAT_PREREQUISITE_MISSING, raw
    if isinstance(exc, HardRemapFailure):
        return CAT_DEPENDENCY_UNRESOLVED, raw
    if isinstance(exc, UnsupportedOperation):
        return CAT_NOT_SUPPORTED, raw
    for marker, category, hint in _ERROR_MAP:
        if marker.lower() in raw.lower():
            # actual server error FIRST, remediation hint appended — the operator sees both.
            return category, f"{raw[:400]}  |  {hint}"
    return CAT_API_ERROR, f"the target API rejected this call: {raw[:400]}"


def is_already_exists(exc: Exception) -> bool:
    raw = str(exc)
    return any(m.lower() in raw.lower() for m in _ALREADY_EXISTS_MARKERS)


class ImportResult:
    """Per-importer counters + the per-unit rows that become `import_results.json`."""

    def __init__(self, component: str) -> None:
        self.component = component
        self.total = 0
        self.created = 0
        self.updated = 0
        self.adopted = 0
        self.skipped = 0
        self.failed = 0
        self.manual = 0
        self.not_selected = 0
        self.skipped_no_object = 0
        self.warned = 0
        self.dry_run = 0
        self.elapsed_sec = 0.0
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.units: list[dict] = []      # one row per unit, joinable on (asset_type, natural_key)

    _COUNTER_FOR = {
        ACTION_CREATED: "created",
        ACTION_UPDATED: "updated",
        ACTION_ADOPTED: "adopted",
        ACTION_SKIPPED: "skipped",
        ACTION_FAILED: "failed",
        ACTION_MANUAL: "manual",
        ACTION_NOT_SELECTED: "not_selected",
        "skipped_no_object": "skipped_no_object",
        ACTION_CREATED_WITH_WARNING: "warned",
    }

    def add(self, row: dict) -> None:
        """Record one unit's outcome and bump the matching counter."""
        self.total += 1
        self.units.append(row)
        counter = self._COUNTER_FOR.get(safe_str(row.get("import_status")))
        if counter:
            setattr(self, counter, getattr(self, counter) + 1)
        if row.get("dry_run"):
            self.dry_run += 1
        if safe_str(row.get("import_status")) == ACTION_FAILED:
            self.errors.append(f"{row.get('asset_type')}/{row.get('natural_key')}: "
                               f"{row.get('note')}")
        elif safe_str(row.get("import_status")) == ACTION_CREATED_WITH_WARNING:
            self.warnings.append(f"{row.get('asset_type')}/{row.get('natural_key')}: "
                                 f"{row.get('note')}")

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "adopted": self.adopted,
            "skipped": self.skipped,
            "failed": self.failed,
            "manual": self.manual,
            "not_selected": self.not_selected,
            "skipped_no_object": self.skipped_no_object,
            "created_with_warning": self.warned,
            "dry_run": self.dry_run,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


class BaseImporter(ABC):
    """Base class for every target-writing importer.

    `component` is the family name (identity/compute/…); `asset_types` are the fine-grained types
    it handles, in the order they must be created WITHIN the phase.
    """

    component: str = "unknown"
    asset_types: tuple = ()
    # asset_types whose work is DECLARATIVE against an object that already exists — adding members
    # to a built-in group, or PUTting an object's permissions. For these, "the object is on target"
    # does NOT mean "the operation was applied", so an ADOPT must still perform the call rather than
    # skip it. Without this, built-in group membership would never migrate (the `admins` group
    # always exists, so every run would adopt and do nothing) and a source admin would silently not
    # be an admin on target. Fingerprint-based SKIP still applies, so a re-run with unchanged
    # membership/grants is still a no-op.
    declarative_asset_types: tuple = ()

    def __init__(self, client, config, staging, state=None, identity_map=None, dbutils=None,
                 context=None, units_by_type=None, retry_keys=None) -> None:
        self.client = client          # auth.ApiClient bound to the TARGET
        self.config = config
        self.staging = staging        # exporters.ArtifactWriter (read the bundle, checkpoint)
        self.state = state            # state.StateStore (may be a disabled no-op store)
        self.identity_map = identity_map or {}
        self.dbutils = dbutils
        # Every unit in the bundle, so an importer can reach a sibling asset_type (e.g. the ACL
        # phase needs to know which objects other phases created).
        self.units_by_type = units_by_type or {}
        # None = "attempt everything"; a set narrows the work list to outstanding units (retry_mode,
        # §7d). Retry narrows the LIST only — each unit still runs the full upsert decision, so a
        # retry can never duplicate an object a previous attempt created but failed to record.
        self.retry_keys = retry_keys
        # Shared cross-phase scratch space: id maps built by earlier phases (cluster name → target
        # id, warehouse name → target id, …). The runner owns it; importers read and contribute.
        self.context = context if context is not None else {}
        self.log = get_logger(self.__class__.__name__)
        self.result = ImportResult(self.component)
        self._pending_cp: list[str] = []
        self._pending_cp_results: dict = {}

    # ── properties ────────────────────────────────────────────────────────
    @property
    def dry_run(self) -> bool:
        return bool(self.config.dry_run)

    # ── the small asset-specific surface subclasses implement ─────────────
    @abstractmethod
    def load(self) -> list[dict]:
        """Units to import, from the bundle, in the order they must be created."""

    def existing_keys(self) -> dict:
        """`{natural_key: target_id}` for objects ALREADY on target, by natural key.

        This is the live existence check — the guard that catches an object created outside the tool
        or by an attempt that died before its bookkeeping write, so it is ADOPTED rather than
        duplicated. Must PAGINATE: a bare list that silently truncates would report an existing
        object as absent and create a DUPLICATE, which is the worst failure mode here.
        """
        return {}

    @abstractmethod
    def create_one(self, unit: dict) -> dict:
        """Create one object on the target. Return `{"target_id": ..., "note": ..., "warning": ...}`.

        Raise on failure — `run()` catches, classifies and records it, then continues.
        """

    def update_one(self, unit: dict, target_id: str) -> dict:
        """Update an existing target object via its EDIT api, against the STORED target id.

        Default: no edit API for this asset → report it rather than pretending it updated.
        """
        return {"target_id": target_id, "warning":
                f"{unit.get('asset_type')} has no update API in this tool — the target object was "
                f"left as it is. Recreate it by hand if the source change matters."}

    # ── unit-level helpers subclasses use ─────────────────────────────────
    @staticmethod
    def natural_key(unit: dict) -> str:
        return safe_str(unit.get("natural_key"))

    def units_for(self, *asset_types: str) -> list[dict]:
        """Bundle units of the given asset_types, in the given order, minus toggled-off ones.

        `export_status == "skip"` means the operator turned that family off at EXPORT time, so
        there is no payload to import — those units are already recorded in the ledger with their
        reason and must not be re-reported here as an import problem.
        """
        out: list[dict] = []
        for at in asset_types:
            for u in self.units_by_type.get(at, []) or []:
                if safe_str(u.get("export_status")) == "skip":
                    continue
                out.append(u)
        return out

    def in_work_list(self, unit: dict) -> bool:
        """Whether `retry_mode` includes this unit (always True when retry_mode=off)."""
        if self.retry_keys is None:
            return True
        return (safe_str(unit.get("asset_type")), self.natural_key(unit)) in self.retry_keys

    def source_id_to_key(self, asset_type: str) -> dict:
        """`{source_object_id: natural_key}` from the bundle, for THIS asset_type.

        The remap chain is always `source id → natural key → target id`, never `source id → target
        id` directly. That indirection is deliberate: the natural key (name/path) is the only thing
        stable across two workspaces, and it lets a reference be resolved against an object imported
        in an EARLIER session (whose target id lives in the state table) just as easily as one
        created in this run.
        """
        out: dict = {}
        for u in self.units_by_type.get(asset_type, []) or []:
            src = safe_str(u.get("source_id"))
            if src:
                out[src] = safe_str(u.get("natural_key"))
        return out

    def target_id_map(self, asset_type: str) -> dict:
        """`{natural_key: target_id}` for an asset_type: this run's creations PLUS earlier sessions.

        Merging both is what makes phase-at-a-time migration work — remapping a job onto a cluster
        imported last week must succeed without re-importing compute.
        """
        combined: dict = {}
        if self.state is not None:
            combined.update(self.state.target_ids_for(asset_type))
        combined.update(self.context.get(f"{asset_type}_target_ids", {}) or {})
        return {k: v for k, v in combined.items() if v}

    def remap_id(self, asset_type: str, source_id: str) -> tuple[str, str]:
        """Resolve a SOURCE object id to its TARGET id. Returns `(target_id, natural_key)`.

        `("", key)` means "we know what it was called but it isn't on target"; `("", "")` means the
        source id isn't in the bundle at all. Callers distinguish these because the remediation
        differs: import the missing family, versus investigate a reference to something that was
        never exported.
        """
        src = safe_str(source_id)
        if not src:
            return "", ""
        key = self.source_id_to_key(asset_type).get(src, "")
        if not key:
            return "", ""
        return safe_str(self.target_id_map(asset_type).get(key, "")), key

    def folder_existing_keys(self, asset_type: str, target_list: list, name_key: str,
                             id_key: str) -> dict:
        """Collapse-proof `{source_natural_key: target_id}` for a folder-placed family whose LIST
        API omits `parent_path` (PLAN 11 Finding-9 §3).

        The natural key is the SOURCE full path (`<parent>/<name>`), but the target LIST only gives
        a name, so matching on name would re-collapse distinct same-named objects. Two mechanisms,
        neither of which can collapse:
          1. **id-anchor (primary)** — the state row (keyed by the SOURCE full path) stores the
             EXACT target object id; if that id still exists on target, the object is present.
          2. **first-run adoption by name** — applied ONLY when the leaf name is unique on BOTH the
             bundle side AND the target side, so it can never map two source objects to one target.
        Everything unmatched falls through to CREATE, which (on distinct keys) yields distinct
        objects rather than one overwrite.
        """
        present_ids = {safe_str(o.get(id_key)) for o in target_list if o.get(id_key)}
        units = self.units_by_type.get(asset_type, []) or []
        found: dict = {}

        if self.state is not None:
            for unit in units:
                key = self.natural_key(unit)
                tid = safe_str(self.state.get_target_id(asset_type, key))
                if tid and tid in present_ids:
                    found[key] = tid

        def _leaf(k: str) -> str:
            return k.rsplit("/", 1)[-1] if "/" in k else k

        src_counts = Counter(_leaf(self.natural_key(u)) for u in units)
        tgt_counts = Counter(safe_str(o.get(name_key)) for o in target_list)
        tgt_by_name = {safe_str(o.get(name_key)): safe_str(o.get(id_key)) for o in target_list}
        for unit in units:
            key = self.natural_key(unit)
            if key in found:
                continue
            name = _leaf(key)
            if name and src_counts[name] == 1 and tgt_counts.get(name) == 1 and tgt_by_name.get(name):
                found[key] = tgt_by_name[name]
        return found

    def remap_parent_path(self, body: dict) -> HomeResolution:
        """Preserve + remap an asset's workspace FOLDER (`parent_path`) in place (PLAN 8 Bug 7 and
        its Lakeview/Genie siblings). Shared by SQL queries/alerts, Lakeview dashboards and Genie
        spaces — each is otherwise created at the API DEFAULT location instead of its source folder
        (a user-created dashboard's `.lvdash.json` must land back in the user's directory).

        Normalises the read API's `/Workspace` prefix and routes the folder through the SAME
        home-resolution decision as workspace content (`_resolve_home_target`, PLAN 11 Finding-8):
          • recreated-SP home → `/Users/<newAppId>/…` (IMP-6);
          • ORPHANED owner (deleted-in-source, absent from the roster) → diverted to the backup root,
            the divert recorded so the create records `created_with_warning` and a re-run ADOPTS the
            already-migrated object rather than re-creating it;
          • an in-roster / unknown owner whose home isn't present yet → the SOURCE path is kept, so
            the create 404s and `missing_parent_prerequisite` files a clean, retryable prerequisite
            (unchanged reactive behaviour — never a proactive divert into backup).
        Returns the HomeResolution so the caller can surface a backup divert as a warning. An
        empty/absent path is dropped so the body is clean."""
        raw = safe_str(body.get("parent_path"))
        if not raw:
            body.pop("parent_path", None)
            return HomeResolution("", "not_home", "")
        res = self._resolve_home_target(raw)
        if res.kind == "backup":
            # Divert to the backup root, ensure the folder exists (the create APIs for these
            # families do NOT auto-provision a parent, unlike workspace/import+mkdirs), and record
            # the source→target divert so the ACL phase / a re-run can find the object.
            body["parent_path"] = res.target_path
            self._ensure_folder(res.target_path)
            self._note_path_remap(normalize_ws_path(raw), res.target_path)
        else:
            # not_home / normal_home / remapped_sp / skip_root / prerequisite: the resolved path IS
            # the source path for everything except an SP-home remap; prerequisite keeps the source
            # path on purpose so a genuinely-missing parent 404s into missing_parent_prerequisite.
            body["parent_path"] = res.target_path or normalize_ws_path(raw)
            if res.kind == "remapped_sp":
                self._note_path_remap(normalize_ws_path(raw), res.target_path)
        return res

    def missing_parent_prerequisite(self, exc: Exception, parent_path, natural_key: str) -> None:
        """Turn a create's "parent folder does not exist" into a clean prerequisite (the path is NOT
        silently dropped) — shared by every folder-placed asset. Re-raise otherwise."""
        msg = str(exc).lower()
        if parent_path and ("does not exist" in msg or "tree node" in msg):
            raise PrerequisiteMissing(
                f"`{natural_key}` targets workspace folder `{parent_path}`, which does not exist on "
                f"target yet — provision/assign its owner (a user home is created on first login) or "
                f"import the folder's content family first, then re-run with retry_mode=failed_only.")

    # ── the shared home-target resolver (PLAN 9 / PLAN 11 Finding-8) ───────
    # ONE copy, used by the workspace importer (content) AND the folder-placed importers (SQL
    # queries/alerts, Lakeview dashboards, Genie spaces). The decision — SP-home remap, present-home
    # passthrough, orphaned-home divert, or prerequisite — must be identical everywhere, or an
    # orphaned owner's query would hard-fail while their notebook is preserved.
    def _get_status(self, path: str) -> dict:
        """`workspace/get-status` for a path, or {} if absent. Never raises — absent 404s."""
        try:
            return self.client.get("api/2.0/workspace/get-status", params={"path": path}) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _roster_status(self, owner: str) -> str:
        """Whether a home `owner` segment appears in the SOURCE identity roster (PLAN 9 A2). Cached.

        Reads `identity_classification.json` from the bundle once and indexes it by BOTH kinds of
        home-owner segment: a service principal's applicationId, and a user's userName + every
        email. Returns "in_roster" (existed on source, not migrated this run), "absent" (deleted in
        source — its home content has nowhere to land under /Users/), or "unknown" (the file is
        missing/unreadable, so we cannot tell — never a silent divert)."""
        if not hasattr(self, "_roster_cache"):
            roster: set = set()
            have_roster = False
            try:
                from src.exporters import bundle_paths as _BP
                doc = self.staging.read_json(_BP.IDENTITY_CLASSIFICATION_JSON) or {}
                identities = doc.get("identities")
                if identities is not None:
                    have_roster = True
                    for ident in identities:
                        itype = safe_str(ident.get("identity_type"))
                        if itype == "service_principal":
                            aid = safe_str(ident.get("applicationId"))
                            if aid:
                                roster.add(aid)
                        elif itype == "user":
                            for k in (ident.get("userName"), ident.get("email")):
                                if safe_str(k):
                                    roster.add(safe_str(k))
                            for e in ident.get("emails") or []:
                                val = safe_str(e.get("value") if isinstance(e, dict) else e)
                                if val:
                                    roster.add(val)
            except Exception:  # noqa: BLE001 — a missing/garbled file just means "unknown"
                have_roster = False
            self._roster_cache = (roster if have_roster else None)
        cache = self._roster_cache
        if cache is None:
            return "unknown"
        return "in_roster" if safe_str(owner) in cache else "absent"

    def _remap_home_path(self, path: str) -> tuple:
        """Remap a service-principal HOME path from the source appId to the target appId (IMP-6).

        Returns `(remapped_path, note)`; `note` is "" when nothing was remapped. Account SPs and
        users keep their identifier (email / preserved appId), so they never appear in `sp_mapping`
        and pass through unchanged — membership in `sp_mapping` IS the "recreated SP" signal (a
        genuine appId is UUID-shaped, but the map, not the shape, is what decides the remap)."""
        owner = home_owner(path)
        if not owner:
            return normalize_ws_path(path), ""
        new_app_id = (self.identity_map.get("sp_mapping") or {}).get(owner, "")
        if not new_app_id or new_app_id == owner:
            return normalize_ws_path(path), ""
        remapped = normalize_ws_path(path).replace(f"/Users/{owner}", f"/Users/{new_app_id}", 1)
        return remapped, (f"service-principal home remapped {owner} → {new_app_id} "
                          f"(the SP was recreated with a new applicationId on target)")

    def _home_present(self, home_root: str) -> bool:
        """Cached: does the `/Users/<owner>` home exist on target (remapped for a recreated SP)?"""
        if not home_root:
            return True
        cache = getattr(self, "_home_present_cache", None)
        if cache is None:
            cache = self._home_present_cache = {}
        if home_root not in cache:
            owner = home_owner(home_root)
            sp_map = self.identity_map.get("sp_mapping") or {}
            if sp_map.get(owner):
                cache[home_root] = True   # an SP's home is auto-provisioned at SP-create
            else:
                remapped, _ = self._remap_home_path(home_root)
                cache[home_root] = bool(self._get_status(remapped))
        return cache[home_root]

    def _backup_path(self, path: str, owner: str) -> str:
        """`<backup_root>/<owner>/<path-relative-to-/Users/owner>` (PLAN 9 §4.2)."""
        norm = normalize_ws_path(path)
        home_root = f"/Users/{owner}"
        rest = norm[len(home_root):].lstrip("/")
        root = safe_str(getattr(self.config.imports, "workspace_home_backup_root", "")) \
            or "/Users_Backup"
        return posixpath.join(root, owner, rest) if rest else posixpath.join(root, owner)

    def _note_path_remap(self, source_path: str, target_path: str) -> None:
        """Record a source→target path divert (SP-home remap OR backup) in the shared context so
        the ACL phase can attach an orphaned/remapped object's permissions to its ACTUAL target
        path (PLAN 9 §4.5). Only recorded when the target differs from the source natural key."""
        if target_path and target_path != source_path:
            self.context.setdefault("workspace_path_remap", {})[source_path] = target_path

    def _ensure_folder(self, path: str) -> None:
        """`mkdirs` a folder best-effort (idempotent). Used to provision a backup divert folder so a
        folder-placed create does not 404 on a missing parent."""
        if not path:
            return
        try:
            self.client.post("api/2.0/workspace/mkdirs", {"path": path})
        except Exception:  # noqa: BLE001 — idempotent; a real problem resurfaces on the create
            pass

    def _resolve_home_target(self, path: str) -> HomeResolution:
        """Decide where a `/Users/<owner>/<rest>` path (root OR descendant) should be created.

        Folds the home logic (SP remap IMP-6, presence guard, orphaned-home divert) into ONE
        decision (PLAN 9 §4.1; lifted to the base class in PLAN 11 Finding-8). Order:
          1. owner is a recreated SP (`sp_mapping`) → remap to /Users/<newAppId>/… (IMP-6).
          2. else owner's real home is present on target → use it as-is.
          3. else owner absent on target:
             - `workspace_home_backup` off → prerequisite (the pre-PLAN-9 behaviour).
             - owner ABSENT from the source roster (deleted in source) → divert to the backup root.
             - owner in-roster / unknown → prerequisite; recovers into the REAL home on
               retry_mode=failed_only, never a silent divert.
        """
        norm = normalize_ws_path(path)
        owner = home_owner(norm)
        if not owner:
            return HomeResolution(norm, "not_home", "")
        home_root = f"/Users/{owner}"
        is_root = norm == home_root

        sp_map = self.identity_map.get("sp_mapping") or {}
        if sp_map.get(owner):
            remapped, note = self._remap_home_path(norm)
            if is_root:
                return HomeResolution(
                    remapped, "skip_root",
                    f"SP home root — auto-provisioned when the SP was created; {note}"
                    if note else "SP home root — auto-provisioned when the SP was created")
            return HomeResolution(remapped, "remapped_sp", note)

        if self._home_present(home_root):
            if is_root:
                return HomeResolution(norm, "skip_root",
                                      "user home directory — already provisioned")
            return HomeResolution(norm, "normal_home", "")

        backup_on = bool(getattr(self.config.imports, "workspace_home_backup", False))
        if not backup_on:
            return HomeResolution(norm, "prerequisite", "")
        if self._roster_status(owner) == "absent":
            backup_path = self._backup_path(norm, owner)
            who = "service principal" if looks_like_app_id(owner) else "user"
            note = (f"owner ({who}) `{owner}` was deleted in source (absent from the source "
                    f"roster) — its home cannot be recreated under /Users/; object preserved at "
                    f"`{backup_path}`. Reassign to the intended owner if needed.")
            return HomeResolution(backup_path, "backup", note)
        return HomeResolution(norm, "prerequisite", "")

    def require_remap(self, ref_type: str, source_id: str, referenced_by: str = "") -> str:
        """Resolve a SOURCE object id to the TARGET object THIS TOOL created for it — exact or fail
        loud (PLAN 11 Finding-10, the lift-and-shift rule).

        The ONLY legitimate remap is `source object → the target object we recreated for it`. Never
        substitute a different object, never silently drop the reference, never leave a dangling
        source id. `remap_id` already returns the three outcomes that make the rule deterministic:
          • (target_id, key)  → resolved → return it.
          • ("", key)         → the object IS in the bundle but not yet on target (dependency order,
                                 a deselected family, or a prior create failed) → RETRYABLE
                                 PrerequisiteMissing that `retry_mode=failed_only` heals.
          • ("", "")          → the object is NOT in the bundle at all (deleted on source / never
                                 exported / out of scope) → a HARD failure; there is nothing to
                                 remap to, and lift-and-shift does not invent a substitute.
        `referenced_by` is folded into the message so the operator knows which asset carries the
        reference. Returns the empty string only when `source_id` itself is blank."""
        src = safe_str(source_id)
        if not src:
            return ""
        by = f"`{referenced_by}` " if referenced_by else ""
        target_id, key = self.remap_id(ref_type, src)
        if target_id:
            return target_id
        if key:
            raise PrerequisiteMissing(
                f"{by}references {ref_type} `{key}` which is in the bundle but not yet created on "
                f"target (its family is deselected, later in dependency order, or a prior create "
                f"failed) — import that family, then re-run with retry_mode=failed_only.")
        raise HardRemapFailure(
            f"{by}references {ref_type} id `{src}` which is not available on source and not in this "
            f"migration (deleted on source, never exported, or out of scope). Lift-and-shift does "
            f"not substitute a different object — fix it on source and re-export.")

    def resolve_principal(self, principal: str, principal_type: str = "") -> str:
        """Map a SOURCE principal (email / appId / group name) to its TARGET equivalent.

        Matched BY NAME, never by source id (master §10a): emails and group displayNames are
        stable, and a recreated Databricks-managed SP's appId is looked up in `sp_mapping`. An
        unknown principal is returned unchanged — the caller decides whether that's fatal, since
        for most greenfield targets it means "an account identity we didn't create".
        """
        p = safe_str(principal)
        if not p:
            return p
        if principal_type == "service_principal" or p in (self.identity_map.get("sp_mapping") or {}):
            return (self.identity_map.get("sp_mapping") or {}).get(p, p)
        if principal_type == "user":
            return (self.identity_map.get("user_map") or {}).get(p, p)
        return p

    # ── the run loop (never raises) ───────────────────────────────────────
    def run(self) -> ImportResult:
        """load → decide → act → record, per unit. NEVER raises (D21).

        A unit-level exception is classified, recorded against that unit, and the loop continues to
        the next unit. Even an unexpected exception in `load()` or `existing_keys()` is contained:
        it becomes a component-level error rather than aborting the phase, because a whole family
        failing to list is still not a reason to abandon the other families.
        """
        t0 = time.time()
        try:
            units = self.load()
        except Exception as exc:  # noqa: BLE001 — a family that can't even load must not abort
            self.log.error("importer load failed", component=self.component, error=str(exc))
            self.result.errors.append(f"{self.component}: load failed: {exc}")
            self.result.elapsed_sec = time.time() - t0
            return self.result

        try:
            existing = self.existing_keys()
        except Exception as exc:  # noqa: BLE001
            # Failing OPEN here would risk duplicates, so treat "cannot list" as an empty map but
            # say so loudly — the create path's RESOURCE_ALREADY_EXISTS adopt is the safety net.
            self.log.warning("existence check failed — relying on ALREADY_EXISTS adopts",
                             component=self.component, error=str(exc))
            self.result.warnings.append(
                f"{self.component}: could not list existing objects ({exc}); duplicate protection "
                f"falls back to RESOURCE_ALREADY_EXISTS handling")
            existing = {}

        self.log.info("importing", component=self.component, units=len(units),
                      already_on_target=len(existing), dry_run=self.dry_run)

        for unit in units:
            try:
                self._process_one(unit, existing)
            except Exception as exc:  # noqa: BLE001 — THE fail-soft guarantee (D21)
                # Nothing a single unit does may end the run. This is the last line of defence:
                # _process_one already handles expected API errors, so reaching here means a bug or
                # an unforeseen shape — still recorded per-unit, still continuing.
                self._record(unit, ACTION_FAILED, note=f"unexpected importer error: {exc}",
                             error_raw=str(exc), category=CAT_API_ERROR)
                self.log.error("unit failed (unexpected)", component=self.component,
                               natural_key=self.natural_key(unit), error=str(exc))

        self.flush_checkpoint()
        self.result.elapsed_sec = time.time() - t0
        self.log.info("phase done", component=self.component, **{
            k: v for k, v in self.result.as_dict().items()
            if k in ("total", "created", "updated", "adopted", "skipped", "failed", "manual")})
        return self.result

    def _process_one(self, unit: dict, existing: dict) -> None:
        """Decide and act on ONE unit. Expected API errors are handled here."""
        key = self.natural_key(unit)
        asset_type = safe_str(unit.get("asset_type"))

        # 0. retry_mode narrowed the work list and this unit isn't in it. Recorded (in-memory) so
        #    the run's result still ACCOUNTS for every unit, but flagged `retry_out_of_scope` so the
        #    retry REPORT can leave it out — repeating the whole inventory (mostly "not outstanding"
        #    rows) in every retry file is noise, and the full-run report already has the whole
        #    picture. Nothing is attempted and no state row changes.
        if not self.in_work_list(unit):
            self.result.add({
                "asset_type": asset_type, "natural_key": key, "family": self.component,
                "source_id": safe_str(unit.get("source_id")), "target_id": "",
                "import_status": ACTION_SKIPPED,
                "action_taken": "Not in this retry's work list",
                "fingerprint": safe_str(unit.get("fingerprint")),
                "note": f"retry_mode={self.config.imports.retry_mode} narrowed this run to "
                        f"outstanding units; this one was not outstanding",
                "failure_category": "", "dry_run": self.dry_run,
                "retry_out_of_scope": True})
            return

        # 1. Units the bundle already marked as human work (repos, legacy dashboards, secret
        #    values, oversize notebooks, UC-backed endpoints). Never attempted — attempting
        #    produces a permanent red failure every run, which trains the operator to ignore red.
        if safe_str(unit.get("import_action")) in ("manual", "review_required"):
            self._record(unit, ACTION_MANUAL, note=safe_str(unit.get("note")) or
                         "requires a manual step on target")
            return

        # 2. Bundle-owned content: the customer's `databricks bundle deploy` recreates it, and
        #    importing bundle STATE would point their next deploy at source-workspace object ids
        #    (verified live). Branch on `import_action`, NEVER on `migration_mode` — see D10/§6d.
        if safe_str(unit.get("import_action")) == "dab_redeploy":
            self._record(unit, ACTION_SKIPPED, note=safe_str(unit.get("note")) or
                         "bundle-owned — recreated by `databricks bundle deploy` on the target")
            return

        # 2b. Databricks-generated artifact (e.g. a `users-clone-…` group, IMP-7a): a platform
        #     bookkeeping object that exists only on the source. There is nothing for a human to do
        #     and nothing to create — skip it cleanly rather than flagging it as manual work.
        if safe_str(unit.get("import_action")) == "skip_generated":
            self._record(unit, ACTION_SKIPPED, note=safe_str(unit.get("note")) or
                         "Databricks-generated artifact (identity-federation bookkeeping) — not a "
                         "real customer object; nothing to migrate")
            return

        # 3. Resume: this attempt already did it. The recorded OUTCOME is restored (not just a
        #    done-flag), because import_results.json is written only at the end and so never
        #    exists after a crash — the checkpoint is the only resumable record.
        #    BUT a unit the operator explicitly asked to retry (retry_mode != off, so retry_keys is
        #    a set and step 0 already kept only the targeted units) must NOT be short-circuited here:
        #    the checkpoint records the PRIOR failed/skipped outcome, so replaying it would make
        #    `retry_mode=failed_only` a no-op after a prerequisite is fixed. Re-attempting is safe —
        #    step 4's upsert is idempotent (live existence check + fingerprint + adopt), and it is
        #    the STATE TABLE (reloaded into retry_keys), not the checkpoint, that decides what is
        #    still outstanding, so a crashed retry re-run naturally drops the units that succeeded.
        retry_active = self.retry_keys is not None
        if not self.config.imports.force_full_import and not retry_active:
            prior = self.staging.get_results(self._cp_component()).get(key)
            if prior and self.staging.is_done(self._cp_component(), key):
                self._record(unit, safe_str(prior.get("import_status")) or ACTION_SKIPPED,
                             target_id=safe_str(prior.get("target_id")),
                             note=safe_str(prior.get("note")) or "already done in this run "
                                                                "(resumed from checkpoint)",
                             checkpoint=False)
                return

        # 4. The upsert decision: state row + LIVE existence check.
        fingerprint = safe_str(unit.get("fingerprint"))
        exists = key in existing
        action = (self.state.decide(asset_type, key, fingerprint, exists)
                  if self.state is not None else
                  (UpsertAction.ADOPT if exists else UpsertAction.CREATE))

        if action is UpsertAction.SKIP:
            self._record(unit, ACTION_SKIPPED, target_id=existing.get(key, ""),
                         note="unchanged since the last import (fingerprint match)")
            return

        if self.dry_run:
            # A dry run makes the real decision and reports it, but mutates nothing — that's what
            # makes a rehearsal's report meaningful rather than a guess.
            intended = {UpsertAction.CREATE: "would CREATE", UpsertAction.UPDATE: "would UPDATE",
                        UpsertAction.ADOPT: "would ADOPT (already on target)"}[action]
            self._record(unit, self._dry_status(action), target_id=existing.get(key, ""),
                         note=f"dry run: {intended}", dry=True)
            return

        if action is UpsertAction.ADOPT:
            target_id = safe_str(existing.get(key))
            # A DECLARATIVE unit's work isn't the object's existence — it's applying members/grants
            # TO that object. Adopting without performing it would silently migrate nothing.
            if asset_type in self.declarative_asset_types:
                self._do_declarative(unit, target_id)
                return
            # Otherwise: adopting isn't the end of it either — the object exists but may be STALE,
            # so compare fingerprints and update if the source has moved on since.
            row = self.state.row(asset_type, key) if self.state is not None else None
            if row and safe_str(row.get("last_source_fingerprint")) not in ("", fingerprint):
                self._do_update(unit, target_id)
            else:
                self._record(unit, ACTION_ADOPTED, target_id=target_id,
                             note="already existed on target — adopted into the migration state "
                                  "(not duplicated)")
            return

        if action is UpsertAction.UPDATE:
            stored = (self.state.get_target_id(asset_type, key) if self.state is not None else "")
            target_id = safe_str(stored) or safe_str(existing.get(key))
            self._do_update(unit, target_id)
            return

        # CREATE
        try:
            out = self.create_one(unit) or {}
        except SkippedNoObject as exc:
            # A declarative unit whose object isn't there — outstanding work, not an error (§6b-i).
            self._record(unit, ACTION_SKIPPED_NO_OBJECT, note=str(exc), category=exc.category)
            return
        except Exception as exc:  # noqa: BLE001
            if is_already_exists(exc):
                # Raced the existence check (or the existence map missed it) — the object exists,
                # which is the outcome we wanted. PLAN 11 BUG-1: this adopt path used to be a
                # DEAD-END for a stale object — it took target_id only from the (possibly empty)
                # existence map and NEVER updated, so a fingerprint-moved edit was silently dropped
                # and the state row was then stamped current, defeating every later retry. Now it
                # HEALS: resolve the real target id from the state row, and if the source fingerprint
                # moved, update in place — mirroring the ADOPT-branch staleness check above.
                target_id = safe_str(existing.get(key))
                if self.state is not None and not target_id:
                    target_id = safe_str(self.state.get_target_id(asset_type, key))
                row = self.state.row(asset_type, key) if self.state is not None else None
                stored_fp = safe_str((row or {}).get("last_source_fingerprint"))
                if (target_id and asset_type not in self.declarative_asset_types
                        and stored_fp not in ("", fingerprint)):
                    self._do_update(unit, target_id)
                    return
                self._record(unit, ACTION_ADOPTED, target_id=target_id,
                             note="already existed on target (create raced the existence check) — "
                                  "adopted, not duplicated")
                return
            category, message = classify_error(exc)
            self._record(unit, ACTION_FAILED, note=message, error_raw=str(exc), category=category)
            self.log.warning("create failed", component=self.component, natural_key=key,
                             category=category, error=str(exc)[:300])
            return
        warning = safe_str(out.get("warning"))
        self._record(unit, ACTION_CREATED_WITH_WARNING if warning else ACTION_CREATED,
                     target_id=safe_str(out.get("target_id")),
                     note=warning or safe_str(out.get("note")),
                     source_detail=safe_str(out.get("source_detail")))

    def _do_declarative(self, unit: dict, target_id: str) -> None:
        """Apply a declarative unit (members / grants) onto an object that already exists.

        Routed through `create_one`, because for these asset_types "create" IS the declarative
        apply — there is no separate object to make. Recorded as `created` when it applies cleanly,
        so the report reads as "we did the work" rather than "we found it already there".
        """
        key = self.natural_key(unit)
        try:
            out = self.create_one(unit) or {}
        except SkippedNoObject as exc:
            self._record(unit, ACTION_SKIPPED_NO_OBJECT, note=str(exc), category=exc.category)
            return
        except Exception as exc:  # noqa: BLE001
            category, message = classify_error(exc)
            self._record(unit, ACTION_FAILED, target_id=target_id, note=message,
                         error_raw=str(exc), category=category)
            self.log.warning("declarative apply failed", component=self.component,
                             natural_key=key, error=str(exc)[:300])
            return
        warning = safe_str(out.get("warning"))
        self._record(unit, ACTION_CREATED_WITH_WARNING if warning else ACTION_CREATED,
                     target_id=safe_str(out.get("target_id")) or target_id,
                     note=warning or safe_str(out.get("note")),
                     source_detail=safe_str(out.get("source_detail")))

    def _do_update(self, unit: dict, target_id: str) -> None:
        """The UPDATE path — always against the STORED target id, never a source id."""
        key = self.natural_key(unit)
        if not target_id:
            self._record(unit, ACTION_FAILED,
                         note="the source object changed but no target id is recorded, so there is "
                              "nothing to update. Re-run with force_full_import=true to recreate.",
                         category=CAT_DEPENDENCY_UNRESOLVED)
            return
        try:
            out = self.update_one(unit, target_id) or {}
        except Exception as exc:  # noqa: BLE001
            category, message = classify_error(exc)
            self._record(unit, ACTION_FAILED, target_id=target_id, note=message,
                         error_raw=str(exc), category=category)
            self.log.warning("update failed", component=self.component, natural_key=key,
                             target_id=target_id, error=str(exc)[:300])
            return
        warning = safe_str(out.get("warning"))
        self._record(unit, ACTION_CREATED_WITH_WARNING if warning else ACTION_UPDATED,
                     target_id=safe_str(out.get("target_id")) or target_id,
                     note=warning or safe_str(out.get("note")) or
                     "source changed since the last import — updated in place",
                     source_detail=safe_str(out.get("source_detail")))

    @staticmethod
    def _dry_status(action: UpsertAction) -> str:
        return {UpsertAction.CREATE: ACTION_CREATED, UpsertAction.UPDATE: ACTION_UPDATED,
                UpsertAction.ADOPT: ACTION_ADOPTED}.get(action, ACTION_SKIPPED)

    # ── recording (state table + checkpoint + result rows) ────────────────
    def _cp_component(self) -> str:
        return f"import:{self.component}"

    def _record(self, unit: dict, status: str, *, target_id: str = "", note: str = "",
                error_raw: str = "", category: str = "", dry: bool = False,
                checkpoint: bool = True, source_detail: str = "") -> None:
        """Record one outcome in all three places: state table, checkpoint, result rows.

        `source_detail` (PLAN 8 Bug 5) is an optional JSON snapshot of the unit's source-side
        members/entitlements/roles, carried into the state row so the NEXT run can diff and name
        exactly what changed. Empty for asset types that don't produce one (state.record carries the
        prior value forward, so a later record for the same key never blanks it)."""
        asset_type = safe_str(unit.get("asset_type"))
        key = self.natural_key(unit)
        row = {
            "asset_type": asset_type,
            "natural_key": key,
            "family": self.component,
            "source_id": safe_str(unit.get("source_id")),
            "target_id": safe_str(target_id),
            "import_status": status,
            "action_taken": _ACTION_TAKEN_LABEL.get(status, status),
            "fingerprint": safe_str(unit.get("fingerprint")),
            "note": note,
            # The COMPLETE, untruncated server error — the report shows it verbatim so nothing is
            # ever hidden behind a summarised note (blank for non-error outcomes).
            "error_raw": safe_str(error_raw),
            "failure_category": category,
            "dry_run": bool(dry),
        }
        self.result.add(row)

        if self.state is not None:
            # A FAILED outcome must NOT advance the stored fingerprint. The change never landed on
            # target, so if we recorded the NEW source fingerprint, `state.decide()` on the next
            # normal run would see "unchanged" and SKIP/ADOPT — stranding the object at its old
            # target value forever while the state falsely claims it is current (a permanent silent
            # staleness; caught live PLAN 11 Run 3 on a failed alert_v2 update). Passing "" keeps the
            # LAST SUCCESSFUL fingerprint (state.record carries the prior forward), so the source
            # still reads as "moved" next run and the update is retried — no reliance on
            # retry_mode=failed_only to eventually heal it.
            fp_to_store = "" if status == ACTION_FAILED else safe_str(unit.get("fingerprint"))
            self.state.record(
                asset_type, key, action=status, fingerprint=fp_to_store,
                source_object_id=safe_str(unit.get("source_id")), target_object_id=safe_str(target_id),
                error=note if status in (ACTION_FAILED, ACTION_CREATED_WITH_WARNING,
                                         ACTION_MANUAL, "skipped_no_object") else "",
                error_raw=error_raw, failure_category=category, source_detail=source_detail)

        # The checkpoint stores the OUTCOME, not just a done-key: a resumed unit needs its
        # target_id/status back, and import_results.json (written only at the end) cannot supply
        # them after a crash. Dry runs are deliberately NOT checkpointed — a rehearsal must not
        # make the next real run think the work is done.
        if checkpoint and not dry:
            self._pending_cp.append(key)
            self._pending_cp_results[key] = {"import_status": status, "target_id": safe_str(target_id),
                                             "fingerprint": safe_str(unit.get("fingerprint")),
                                             "source_id": safe_str(unit.get("source_id")),
                                             "note": note}
            if len(self._pending_cp) >= CHECKPOINT_BATCH:
                self.flush_checkpoint()

    def flush_checkpoint(self) -> None:
        """Write the pending checkpoint batch. Called per batch, at phase end, and in `finally`."""
        if not self._pending_cp and not self._pending_cp_results:
            return
        try:
            self.staging.mark_done_bulk(self._cp_component(), self._pending_cp,
                                        self._pending_cp_results)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not break the run
            self.log.warning("checkpoint flush failed", component=self.component, error=str(exc))
        self._pending_cp, self._pending_cp_results = [], {}


# Human label per status, for the Excel "Action Taken" column. Kept beside the vocabulary so a new
# status can't be added without a label (the report would otherwise render a bare enum).
_ACTION_TAKEN_LABEL = {
    ACTION_CREATED: "Created on target",
    ACTION_CREATED_WITH_WARNING: "Created — with a warning",
    ACTION_UPDATED: "Updated in place",
    ACTION_ADOPTED: "Adopted (already existed)",
    ACTION_SKIPPED: "Skipped — unchanged",
    ACTION_FAILED: "FAILED",
    ACTION_MANUAL: "Manual step required",
    ACTION_NOT_SELECTED: "Deferred — not selected this run",
    "skipped_no_object": "Skipped — target object absent",
}
