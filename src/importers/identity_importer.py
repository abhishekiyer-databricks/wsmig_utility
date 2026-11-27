"""
IdentityImporter — phase 1, the highest-risk write in the tool (Plan 3 §6, §7.1; master §7).

Order WITHIN the phase is fixed and load-bearing:
    users → service principals → groups (created EMPTY) → group members → entitlements

**Why groups are two-pass.** A group's members can include other groups, in any order, and forward
references are normal. Creating every group empty first and PATCHing members afterwards makes
membership resolve regardless of order, with no topological sort to get wrong.

**Users and SPs need no create-vs-assign decision at all (Plan 6 F3/F4/F5).** A POST to WORKSPACE
SCIM `/Users` or `/ServicePrincipals` creates the principal AT THE ACCOUNT and assigns it here,
returning the account's own id. So:

    user  → POST with `userName`       → dedupes by userName, adopts if it already exists
    SP    → POST with `applicationId`  → ADOPTS the account SP, applicationId PRESERVED

Passing `applicationId` is the whole ballgame: omit it and the target mints a NEW applicationId,
silently orphaning every ACL, job `run_as` and secret grant that referenced the SP. That is what
produced 13 duplicate SPs at account level. With it, `sp_mapping` is an IDENTITY map.

**Groups are the one type where the decision is real, and getting it wrong is unrecoverable.**
    kind=workspace_local → POST workspace SCIM (created EMPTY, members in pass 2)
    kind=account         → ASSIGN via permissionassignments; NEVER POST
    kind=system          → never create; membership only
POSTing an account group creates a workspace-local SHADOW with the same name, and that shadow then
permanently blocks assigning the real account group ("Workspace group with name X already exists").
See `_process_one`, where the guard has to live — a shadow IS in `existing_keys()`, so the base class
would otherwise silently ADOPT it.

**Multi-workspace safety.** One account is shared by 100+ workspace pairs, so this importer is
strictly ADDITIVE at account level: it never deletes an account principal, never patches an account
group's (account-global) membership, and never replays `account_admin`. Only workspace-scoped
entitlements and assignments are written.

**Why the id map is written per identity, immediately.** Every later permission call resolves a
principal by its TARGET SCIM id, which differs from the source id even when the natural key doesn't.
Hence a row per identity as it happens, plus the mandatory phase-boundary flush, and a row even for
identities we deliberately do NOT create.

Members and ACL principals are matched **by name** (userName / applicationId / displayName), never
by source id — source ids are meaningless on the target.

**Nested identities stay nested (PLAN 16.3.1 Part B).** An identity that is only a MEMBER of an
assigned group on the source (`workspace_permissions == []`, never directly assigned) is never
assigned here: no `permissionassignments` PUT for a group, and no workspace-SCIM POST for a user/SP
(the POST itself creates a DIRECT assignment, PB1). Account-group membership is account-global, so
the identity becomes visible on target through its parent; PASS 3 waits for that, then applies its
entitlements. Existing extra direct assignments are reported, NEVER removed (DB3).

**Entitlements are verified (DB5).** Every entitlement/role write is GET → PATCH only the missing
values → GET; each identity row says what was copied / already present / failed, and a failure
fails the row (`not_applied`) so `retry_mode=failed_only` re-applies it.
"""
from __future__ import annotations

import json
import time

from src.importers.base_importer import (BaseImporter, DeferredToNestedPass,
                                         EntitlementsNotApplied, PrerequisiteMissing,
                                         SkippedNoObject, CAT_NOT_SUPPORTED, classify_error)
from src.state.state_store import (ACTION_ADOPTED, ACTION_CREATED, ACTION_CREATED_WITH_WARNING,
                                   ACTION_FAILED, CAT_NOT_APPLIED, CAT_PREREQUISITE_MISSING,
                                   UpsertAction)
from src.utils.helpers import safe_str
from src.utils.logger import fmt_elapsed, get_logger

_LOG = get_logger("IdentityImporter")

# SCIM create whitelists (master §10a). Sending the whole source object back fails or writes
# server-derived junk, so only these fields travel; entitlements and roles are applied as
# SEPARATE PATCH passes after create, because several workspaces reject them inline.
_USER_CREATE_FIELDS = ("userName", "displayName", "emails", "name", "active")
# `applicationId` is LOAD-BEARING, not cosmetic (Plan 6 F4, verified live on target 2026-08-06):
#   POST with it    → adopts the existing account SP, SAME applicationId and SAME SCIM id
#   POST without it → mints a BRAND-NEW applicationId, orphaning every ACL/run_as/secret grant
# Omitting it is what produced 13 duplicate `wsmig_test_db_sp` appIds at account level.
_SP_CREATE_FIELDS = ("displayName", "active", "applicationId")
_GROUP_CREATE_FIELDS = ("displayName",)

_SCIM = "api/2.0/preview/scim/v2"
_ASSIGNMENTS = "api/2.0/preview/permissionassignments"

# Account-level roles must NEVER be replayed: they are account-GLOBAL, so granting one while
# migrating workspace N would escalate that identity across every other workspace in the account.
# Only workspace-scoped entitlements migrate.
_ACCOUNT_LEVEL_ROLES = {"account_admin"}

# PASS 3 (nested identities) wait budget. Visibility through a parent takes 3-6 s (PB6), the same as
# a direct assignment, so 300 s is a wide margin; each poll is ONE SCIM list per needed kind.
NESTED_POLL_SEC = 10
NESTED_WAIT_SEC = 300
# DB5 read-back: after a PATCH, re-read up to this many times, this far apart, until every sent
# value shows (SCIM is read-your-writes in practice; the retries only absorb a slow replica).
ENTITLEMENT_READ_TRIES = 3
ENTITLEMENT_READ_SEC = 3

# The built-in groups are never "nested only" — their membership is migrated declaratively.
_SYSTEM_GROUP_NAMES = ("users", "admins")


def _sleep(seconds: float) -> None:
    """Indirection so offline tests can make the waits instant."""
    time.sleep(seconds)


def _ent_label(attribute: str, value: str) -> str:
    """How an entitlement/role value is named in the report (`role:` prefix for roles)."""
    return f"role:{value}" if attribute == "roles" else value

# Group kinds, as stamped into the bundle by src/identity/classifier.py.
KIND_ACCOUNT = "account"
KIND_WORKSPACE_LOCAL = "workspace_local"
KIND_SYSTEM = "system"
KIND_SYSTEM_GENERATED = "system_generated"   # Databricks-created artifact (users-clone-…) → SKIP

# Bundles exported before Plan 6 carry the OLD classification vocabulary and no `kind`. Mapping them
# forward keeps an old bundle importable instead of degrading every group to NEEDS_REVIEW (which
# would skip them all). `db_managed_group` maps to workspace_local because that is what it meant.
_LEGACY_KIND = {
    "account_group": KIND_ACCOUNT, "entra_user": KIND_ACCOUNT, "umi_or_entra_sp": KIND_ACCOUNT,
    "db_managed_sp": KIND_ACCOUNT,          # an SP is ALWAYS an account principal (Plan 6 F3)
    "db_managed_group": KIND_WORKSPACE_LOCAL,
    "builtin_group": KIND_SYSTEM,
}


def _kind_of(unit: dict) -> str:
    """The unit's kind, falling back to the legacy `classification` for pre-Plan-6 bundles.

    Also re-detects a system-generated group (`users-clone-…`) from the NAME even if the bundle
    predates that classification — so an old bundle can't sneak one past the skip (IMP-7a)."""
    kind = safe_str(unit.get("kind"))
    if kind in (KIND_ACCOUNT, KIND_WORKSPACE_LOCAL, KIND_SYSTEM, KIND_SYSTEM_GENERATED):
        return kind
    from src.identity.classifier import is_system_generated_group
    if is_system_generated_group({"displayName": safe_str(unit.get("natural_key"))}):
        return KIND_SYSTEM_GENERATED
    return _LEGACY_KIND.get(safe_str(unit.get("classification")), kind)


class IdentityImporter(BaseImporter):
    component = "identity"
    asset_types = ("user", "service_principal", "group", "group_membership")
    # Built-in group membership is declarative against a group that ALWAYS exists on target, so an
    # ADOPT must still perform the member PATCH — otherwise a source workspace admin silently
    # would not be an admin on target.
    declarative_asset_types = ("group_membership",)
    # PLAN 16.3.1 §3B.4: an identity that already exists on target when the phase starts is
    # RECONCILED, not just recorded — its entitlements are applied (the customer's "group A's
    # Workspace access was not copied" case) and its direct assignment checked (never for a nested
    # one). `adopt_one` does it.
    reconcile_on_adopt_types = ("user", "service_principal", "group")

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # name → target SCIM id, filled as we go and consumed by the member PATCH pass.
        self._target_users: dict = {}
        self._target_sps: dict = {}
        self._target_groups: dict = {}
        # name → lowercased meta.resourceType on TARGET. Needed to detect the shadow case: a
        # workspace-local group already occupying an account group's name (Plan 6 F6).
        self._target_group_kinds: dict = {}
        # displayName → target SCIM id, for users AND SPs. SCIM group members are identified by
        # `display` (a person's display name), not by userName/applicationId, so name-based member
        # resolution needs this index as well as the natural-key ones.
        self._target_by_display: dict = {}
        # principal_id → permissions already on target; None = could not read (never {}).
        self._target_assignments = None
        # PASS 2 work list: (group_name, target_group_id, members, unit, result_row). Membership is
        # deliberately NOT patched during a group's own create — see `run()`.
        self._member_pass: list[tuple] = []
        # PASS 3 work list (PLAN 16.3.1): nested-only identities NOT visible at phase start. They
        # are never assigned/POSTed; they become visible through their parent group.
        self._nested_pass: list[dict] = []
        # Group members that are nested-only identities still waiting for PASS 3 when their group's
        # membership was applied: (asset_type, group_key, group_id, members, added, total,
        # unresolved, member_note). They are added after PASS 3 instead of being reported as
        # "could not resolve" — they are not missing, just not visible yet.
        self._held_members: list[tuple] = []
        # (asset_type, natural_key) → source parent group names, derived from the bundle (no call).
        self._parents_by_key: dict = {}
        # principal ids whose workspace assignment was already ensured this run (one PUT at most).
        self._ensured: set = set()

    # ── load: the phase's fixed internal order ────────────────────────────
    def load(self) -> list[dict]:
        """Users → SPs → groups → built-in group membership.

        `group_membership` units come last because they PATCH members onto groups that must already
        exist (the built-in `admins`/`users`, which are never created).
        """
        units = self.units_for("user", "service_principal", "group", "group_membership")
        units += self._oauth_secret_manual_units(units)
        self._parents_by_key = self._source_parents(units)
        return units

    # ── nested identities (PLAN 16.3.1 Part B) ────────────────────────────
    @staticmethod
    def _recorded_permissions(unit: dict):
        """The exported `workspace_permissions` — payload first, then unit (same lookup as
        `_permissions_for_unit`): a list (direct = ["USER"|"ADMIN"], nested only = []) or None
        (could not check / pre-Plan-6 bundle)."""
        payload = unit.get("payload") or {}
        permissions = payload.get("workspace_permissions")
        if permissions is None:
            permissions = unit.get("workspace_permissions")
        return permissions

    def _is_nested_only(self, unit: dict) -> bool:
        """True ONLY for an account identity the source recorded as NOT directly assigned —
        `workspace_permissions` is exactly `[]` — i.e. it was in the workspace only as a member of
        an assigned group (PB3). Never for `None` (unknown → today's USER fallback), a
        workspace-local group (records ['USER'], and the kind check is a second guard), a system
        group, or a `group_membership` unit."""
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type not in ("user", "service_principal", "group"):
            return False
        if asset_type == "group" and _kind_of(unit) != KIND_ACCOUNT:
            return False
        if self.natural_key(unit).lower() in _SYSTEM_GROUP_NAMES:
            return False
        permissions = self._recorded_permissions(unit)
        return isinstance(permissions, list) and len(permissions) == 0

    def _source_parents(self, units: list) -> dict:
        """`{(asset_type, natural_key): [parent group names]}` from the bundle's group members.

        A member is matched by its source SCIM id (`value` == the child's `source_id`, exact), with
        the display name as the fallback for a member entry without an id. System and generated
        groups (`users`, `admins`, `users-clone-…`) are not parents — they confer no visibility.
        (Users' own `groups` attribute is stripped from the export payload, so the group side is
        the one source that covers users, SPs and groups alike.)"""
        by_id: dict = {}
        by_display: dict = {}
        for u in units:
            at = safe_str(u.get("asset_type"))
            if at not in ("user", "service_principal", "group"):
                continue
            key = (at, self.natural_key(u))
            if safe_str(u.get("source_id")):
                by_id[safe_str(u.get("source_id"))] = key
            payload = u.get("payload") or {}
            for name in {self.natural_key(u), safe_str(payload.get("displayName"))}:
                if name:
                    by_display.setdefault(name, key)
        parents: dict = {}
        for g in units:
            if safe_str(g.get("asset_type")) != "group":
                continue
            if _kind_of(g) in (KIND_SYSTEM, KIND_SYSTEM_GENERATED):
                continue
            parent = self.natural_key(g)
            for m in (g.get("payload") or {}).get("members") or []:
                if not isinstance(m, dict):
                    continue
                child = by_id.get(safe_str(m.get("value"))) if safe_str(m.get("value")) else None
                if child is None and not safe_str(m.get("value")):
                    child = by_display.get(safe_str(m.get("display")))
                if child and child != ("group", parent):
                    bucket = parents.setdefault(child, [])
                    if parent not in bucket:
                        bucket.append(parent)
        return parents

    def _parents_of(self, unit: dict) -> list:
        return list(self._parents_by_key.get(
            (safe_str(unit.get("asset_type")), self.natural_key(unit)), []))

    def _parents_text(self, unit: dict) -> str:
        parents = self._parents_of(unit)
        return ", ".join(f"`{p}`" for p in parents) if parents else "a group not in this bundle"

    def _directly_assigned_on_target(self, target_id: str) -> bool:
        """Whether the target ALREADY lists this principal in permissionassignments (read once at
        phase start; a nested identity is never PUT, so this is always pre-existing)."""
        return bool(target_id) and self._target_assignments is not None and \
            safe_str(target_id) in self._target_assignments

    def _db3_note(self, unit: dict) -> str:
        return (f"directly assigned on target, nested only on source (via "
                f"{self._parents_text(unit)}); left unchanged — remove its workspace assignment "
                f"manually if the source layout must be mirrored (RUNBOOK §Nested identities)")

    def _assignment_label(self, unit: dict, target_id: str) -> str:
        """The report's "Assignment" column for an identity row."""
        asset_type = safe_str(unit.get("asset_type"))
        if asset_type not in ("user", "service_principal", "group"):
            return ""
        if asset_type == "group" and _kind_of(unit) not in (KIND_ACCOUNT, KIND_WORKSPACE_LOCAL):
            return ""
        if self._is_nested_only(unit):
            if self._directly_assigned_on_target(target_id):
                return "direct on target (pre-existing); nested on source"
            return f"via group ({', '.join(self._parents_of(unit)) or 'not in bundle'})"
        return "direct"

    def dry_run_intent(self, unit: dict, action) -> str:
        """A nested-only identity is never assigned: the rehearsal says it will be reached through
        its group (no polling in a dry run)."""
        if self._is_nested_only(unit) and action is UpsertAction.CREATE:
            return (f"would ADOPT (nested on source via {self._parents_text(unit)}; no direct "
                    f"assignment)")
        return ""

    def _oauth_secret_manual_units(self, units: list) -> list:
        """PLAN 8 Bug 3 + PLAN 11 Finding-7: an SP whose SOURCE had OAuth client secret(s) is a
        STANDING manual task ONLY when the SP does NOT keep its account-level identity on target.

        The manual `service_principal_secret` unit is KIND-scoped:
          • **workspace-local Databricks-managed SP** — recreated on target with a NEW applicationId,
            so its source secret cannot come across (write-only + new identity) → genuine `manual`
            re-issue. Keyed by the source applicationId, separate asset_type, so it sits beside the
            SP in the report; `manual` so it rides skipped_only/failed_and_skipped, never failed_only.
          • **account-level SP (account db-managed / UMI / Entra), adopted same-account** — the
            applicationId is stable and the OAuth secret / UMI credential lives at the ACCOUNT level
            and is intact; assigning the SP carries the identity, so there is NOTHING to migrate.
            Emitting a `manual` step here is false work that would sit in the cumulative Outstanding
            sheet forever (a manual row never clears) → SUPPRESSED (no unit at all). Cross-account
            re-provisioning is an account-admin task the preflight already flags, not this unit's job.
        This also fixes the has_secrets==unknown conservatism for account SPs: an inconclusive check
        must never manufacture a false outstanding manual task on an account-owned identity.
        """
        out = []
        for u in units:
            if safe_str(u.get("asset_type")) != "service_principal":
                continue
            note = safe_str(u.get("note"))
            if not note:      # export sets a note ONLY when has_secrets is True or unknown
                continue
            # Finding-7: only a workspace-local SP (new applicationId on target) needs a re-issue.
            # An account-owned SP keeps its appId + account-level credential → nothing to migrate.
            if _kind_of(u) != KIND_WORKSPACE_LOCAL:
                continue
            app_id = self.natural_key(u)
            out.append({
                "asset_type": "service_principal_secret",
                "natural_key": app_id,
                "source_id": safe_str(u.get("source_id")),
                "import_action": "manual",
                "export_status": "success",
                "fingerprint": "",
                "note": (f"service principal `{app_id}` was recreated with a NEW applicationId on "
                         f"target: {note} Create a new OAuth secret on target and update whatever "
                         f"authenticates with it — a secret VALUE is never readable via any API, so "
                         f"the tool cannot migrate it."),
            })
        return out

    # ── existence checks (paginated — a truncated list would DUPLICATE) ───
    def existing_keys(self) -> dict:
        """`{natural_key: target_scim_id}` across all three SCIM types.

        Uses `get_scim`, which paginates via startIndex/count. That matters more here than anywhere:
        a bare list that silently truncated would report an existing user as absent and create a
        duplicate — and SCIM pagination is a known latent bug in the reference `databrickslabs/
        migrate` tool, so it is implemented and asserted rather than assumed.
        """
        for user in self.client.get_scim("Users"):
            key = safe_str(user.get("userName"))
            if key:
                self._target_users[key] = safe_str(user.get("id"))
            # SCIM group members identify each member by `display`, which for a USER is the DISPLAY
            # NAME ("Aman Bansal"), not the userName. Without this index every member of `admins`
            # and `users` failed to resolve and those groups imported EMPTY — i.e. a source
            # workspace admin silently was not an admin on target.
            display = safe_str(user.get("displayName"))
            if display:
                self._target_by_display[display] = safe_str(user.get("id"))
        for sp in self.client.get_scim("ServicePrincipals"):
            key = safe_str(sp.get("applicationId"))
            if key:
                self._target_sps[key] = safe_str(sp.get("id"))
            display = safe_str(sp.get("displayName"))
            if display:
                self._target_by_display[display] = safe_str(sp.get("id"))
        for group in self.client.get_scim("Groups"):
            key = safe_str(group.get("displayName"))
            if key:
                self._target_groups[key] = safe_str(group.get("id"))
                # Recorded so an ACCOUNT group can detect a same-named workspace-local SHADOW
                # already sitting on target, which would block its assignment forever (F6).
                self._target_group_kinds[key] = safe_str(
                    (group.get("meta") or {}).get("resourceType")).lower()

        # Existing workspace permissions, so the assignment pass only PUTs real deltas. None (not
        # {}) when unreadable — {} would look like "nobody is assigned" and re-PUT everything.
        self._target_assignments = self._read_target_assignments()

        # Publish for later phases (job run_as, secret-scope MANAGE, ACL principals).
        self.context.setdefault("target_identities", {}).update({
            "users": self._target_users, "service_principals": self._target_sps,
            "groups": self._target_groups})

        # One flat map for the base class's existence check. A user and a group can share a name in
        # principle; that only risks a spurious ADOPT, and the per-type maps above are what the
        # create paths actually use, so the flat view is safe for its one purpose.
        out: dict = {}
        out.update(self._target_users)
        out.update(self._target_sps)
        out.update(self._target_groups)
        return out

    def _index_display(self, created: dict, payload: dict, target_id: str,
                       fallback: str = "") -> None:
        """Record an identity's DISPLAY NAME → target scim id, for PASS-2 member resolution (IMP-7b).

        SCIM group members are named by `display` (the display name). The per-kind maps are keyed by
        userName / applicationId / group name, so without a display-name index a member pointing at
        an identity CREATED in this run cannot be resolved. `existing_keys()` indexes pre-existing
        identities; this does the same for ones we create/adopt, so the two populations behave
        identically. Prefer the server's echoed displayName, then the payload's, then the fallback."""
        display = (safe_str((created or {}).get("displayName"))
                   or safe_str((payload or {}).get("displayName"))
                   or safe_str(fallback))
        if display and target_id:
            self._target_by_display[display] = target_id

    def _read_target_assignments(self):
        """`{principal_id: [permissions]}` on target, or None if it could not be read."""
        try:
            data = self.client.get(_ASSIGNMENTS)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not read target workspace permission assignments; the "
                             "assignment pass will PUT unconditionally", error=str(exc)[:200])
            return None
        out: dict = {}
        for entry in (data or {}).get("permission_assignments", []) or []:
            principal = entry.get("principal") or {}
            pid = safe_str(principal.get("principal_id"))
            if pid:
                out[pid] = [p for p in (entry.get("permissions") or []) if p]
        return out

    # ── create / update ───────────────────────────────────────────────────
    def create_one(self, unit: dict) -> dict:
        asset_type = safe_str(unit.get("asset_type"))
        if self._is_nested_only(unit):
            # PLAN 16.3.1 §3B.3: NO permissionassignments PUT (group) and NO workspace-SCIM POST
            # (user/SP — the POST itself makes a direct assignment, PB1). PASS 3 resolves it once it
            # is visible through its parent and writes its row.
            self._nested_pass.append(unit)
            raise DeferredToNestedPass(self.natural_key(unit))
        if asset_type == "user":
            return self._create_user(unit)
        if asset_type == "service_principal":
            return self._create_sp(unit)
        if asset_type == "group":
            return self._create_group(unit)
        if asset_type == "group_membership":
            return self._add_builtin_members(unit)
        raise RuntimeError(f"identity importer got an unexpected asset_type {asset_type!r}")

    def update_one(self, unit: dict, target_id: str) -> dict:
        """Re-apply the parts of an identity that CAN change: entitlements, roles, membership.

        Deliberately not a wholesale SCIM PUT: that would fight Entra/SCIM provisioning on an
        account-managed identity, and on a group it would drop members added on target by hand.
        """
        asset_type = safe_str(unit.get("asset_type"))
        payload = unit.get("payload") or {}
        nested = self._is_nested_only(unit)
        if asset_type in ("user", "service_principal"):
            resource = "Users" if asset_type == "user" else "ServicePrincipals"
            ent = self._apply_entitlements(resource, target_id, payload)
            # PLAN 8 Bug 5: name the ACTUAL change (entitlement/role added, or removed-in-source and
            # NOT removed on target) instead of the old fixed "entitlements/roles re-applied" string,
            # which hid both a secret change (now a manual unit, Bug 3) and an entitlement removal.
            note = self._diff_note(asset_type, unit, payload)
            if nested and self._directly_assigned_on_target(target_id):
                note = f"{note}; {self._db3_note(unit)}"
            note = self._with_entitlements(note, ent)
            self._raise_if_entitlements_failed(unit, target_id, ent)
            return {"target_id": target_id, "note": note, "entitlements": ent,
                    "source_detail": self._snapshot_json(asset_type, payload)}
        if asset_type == "group":
            ent = self._apply_entitlements("Groups", target_id, payload)
            if _kind_of(unit) == KIND_ACCOUNT:
                # Never re-sync an account group's members: account-global (§multi-workspace) and
                # Entra would revert it. Permissions ARE re-checked, since ADMIN/USER can change —
                # EXCEPT for a nested-only group, which is never (re)assigned (PLAN 16.3.1: 1.2.0
                # re-PUT it on every re-run, undoing any manual cleanup).
                # PLAN 11 Finding-2: the member delta IS named for detection/reporting
                # (members_applied=False), and the snapshot now INCLUDES members so a later run can
                # diff a membership-only change (the fingerprint already moves on one).
                note = self._diff_note("group", unit, payload, members_applied=False)
                if nested:
                    note = f"{note}; workspace entitlements re-applied (nested on source via " \
                           f"{self._parents_text(unit)}; not assigned)"
                    if self._directly_assigned_on_target(target_id):
                        note = f"{note}; {self._db3_note(unit)}"
                else:
                    self._ensure_assignment(target_id, unit, self.natural_key(unit))
                    note = f"{note}; workspace entitlements/permissions re-applied"
                note = self._with_entitlements(note, ent)
                self._raise_if_entitlements_failed(unit, target_id, ent)
                return {"target_id": target_id, "note": note, "entitlements": ent,
                        "source_detail": self._snapshot_json("group", payload)}
            member_note = self._sync_members(target_id, payload.get("members") or [],
                                             group_key=self.natural_key(unit))
            # The additive PATCH (member_note) says what was ADDED/unresolved; the diff names what
            # was removed IN SOURCE — which is REPORTED, not applied (the removed member stays on
            # target for review), per the "report, don't act" stance for source-side removals.
            diff = self._diff_note("group", unit, payload)
            note = f"{diff}; {member_note}" if diff else member_note
            self._raise_if_entitlements_failed(unit, target_id, ent)
            return {"target_id": target_id, "note": self._with_entitlements(note, ent),
                    "entitlements": ent,
                    "warning": member_note if "could not resolve" in member_note else "",
                    "source_detail": self._snapshot_json("group", payload)}
        if asset_type == "group_membership":
            return self._add_builtin_members(unit)
        return {"target_id": target_id}

    def adopt_one(self, unit: dict, target_id: str) -> dict:
        """RECONCILE an identity that was already on target when the phase started (PLAN 16.3.1
        §3B.4). 1.2.0 only recorded `adopted` here — no entitlements — which is how an account group
        the customer pre-assigned kept no Workspace access.

          • nested only on source → apply entitlements, NO assignment call; DB3 note if the target
            already has a (1.2.0-made) direct assignment for it — left unchanged.
          • direct on source (["USER"|"ADMIN"]) or unknown (None) → apply entitlements and ENSURE the
            direct assignment (fixes "direct on source, only via a group on target").
          • system / undetermined-kind groups → unchanged (adopted; built-in membership is the
            declarative `group_membership` unit).
        `changed` (→ `updated`) only when an entitlement was copied (§3B.4)."""
        asset_type = safe_str(unit.get("asset_type"))
        payload = unit.get("payload") or {}
        key = self.natural_key(unit)
        adopted_note = "already existed on target — adopted into the migration state (not duplicated)"
        if asset_type == "group" and _kind_of(unit) not in (KIND_ACCOUNT, KIND_WORKSPACE_LOCAL):
            return {"changed": False, "target_id": target_id, "note": adopted_note}
        resource = {"user": "Users", "service_principal": "ServicePrincipals",
                    "group": "Groups"}[asset_type]
        ent = self._apply_entitlements(resource, target_id, payload)
        changed = bool(ent.get("copied"))
        if self._is_nested_only(unit):
            note = (f"{adopted_note}; nested on source via {self._parents_text(unit)} — reached "
                    f"through its group on the target, not directly assigned (as on source)")
            if self._directly_assigned_on_target(target_id):
                note = f"{adopted_note}; {self._db3_note(unit)}"
        else:
            note = adopted_note
            if self._ensure_assignment(target_id, unit, key):
                # Not `changed`: a pre-1.2.1 adopt applied the same assignment and read `adopted`.
                note = f"{note}; direct workspace assignment applied (as on source)"
        note = self._with_entitlements(note, ent)
        self._raise_if_entitlements_failed(unit, target_id, ent)
        return {"changed": changed, "target_id": target_id, "note": note, "entitlements": ent,
                "source_detail": self._snapshot_json(asset_type, payload)}

    # ── users ─────────────────────────────────────────────────────────────
    def _create_user(self, unit: dict) -> dict:
        """POST workspace SCIM — which ADOPTS the account user, or creates+assigns it.

        There is no workspace-local user (Plan 6 F3): a workspace-SCIM POST writes at the ACCOUNT
        and assigns to this workspace, returning the account's own id. And it dedupes by `userName`
        (F5), so a user that already exists at the account is adopted, not duplicated — which is
        exactly what makes this safe to run against 100 workspaces sharing one account.

        This is why there is no longer a `PrerequisiteMissing` branch here: the earlier code refused
        to act and asked an account admin to pre-assign every user by hand, which the API makes
        unnecessary.
        """
        payload = unit.get("payload") or {}
        key = self.natural_key(unit)

        body = {k: payload[k] for k in _USER_CREATE_FIELDS if k in payload}
        body["userName"] = key
        created = self.client.post(f"{_SCIM}/Users", body)
        target_id = safe_str(created.get("id"))
        self._target_users[key] = target_id
        # Index the display name too (IMP-7b): a group member is identified by `display` (the
        # display name), so a member referring to a user CREATED in this same pass must resolve in
        # PASS 2 — not only users that pre-existed on target. Without this, freshly-created users
        # (e.g. "Sanket Kelkar") could not be added to their groups, leaving them under-populated.
        self._index_display(created, payload, target_id, fallback=key)
        self._record_identity_row("user", key, target_id, key, unit, ACTION_CREATED)
        ent = self._apply_entitlements("Users", target_id, payload)
        # Workspace ADMIN vs USER lives only in permissionassignments (F8) — a source workspace
        # admin would otherwise land on target as a plain USER.
        self._ensure_assignment(target_id, unit, key)
        self._raise_if_entitlements_failed(unit, target_id, ent)
        return {"target_id": target_id,
                "note": self._with_entitlements(
                    f"adopted/created the account user `{key}` (userName preserved)", ent),
                "entitlements": ent,
                "source_detail": self._snapshot_json("user", payload)}

    # ── service principals ────────────────────────────────────────────────
    def _create_sp(self, unit: dict) -> dict:
        """POST workspace SCIM WITH the source `applicationId`, which ADOPTS the account SP.

        The `applicationId` in the body is the whole point (Plan 6 F4). Passing it makes the target
        return the SAME applicationId and the SAME account SCIM id as the source; omitting it mints
        a new applicationId, which orphans every ACL, job `run_as` and secret grant that referenced
        the SP — silently, because a fresh SP looks perfectly healthy.

        Consequence: `sp_mapping` is now an IDENTITY map, so SP principals in ACLs need no
        translation. The map row is still recorded because ACL application resolves principals by
        target SCIM id.
        """
        payload = unit.get("payload") or {}
        source_app_id = self.natural_key(unit)

        body = {k: payload[k] for k in _SP_CREATE_FIELDS if k in payload}
        body.setdefault("displayName", source_app_id)
        # Never trust the payload alone: the natural key IS the source applicationId, and adopting
        # the right account SP depends entirely on sending it.
        if source_app_id:
            body["applicationId"] = source_app_id

        try:
            created = self.client.post(f"{_SCIM}/ServicePrincipals", body)
        except Exception as exc:  # noqa: BLE001
            # Already assigned to this workspace (a re-run, or another pair got here first). That is
            # an ADOPT, not a failure — resolve the existing id instead of creating anything.
            if "already exists" not in str(exc).lower():
                raise
            existing_id = self._lookup_sp_id(source_app_id)
            if not existing_id:
                raise
            self._target_sps[source_app_id] = existing_id
            self._index_display({}, payload, existing_id, fallback=source_app_id)
            self._record_identity_row("service_principal", source_app_id, existing_id,
                                      source_app_id, unit, ACTION_ADOPTED)
            ent = self._apply_entitlements("ServicePrincipals", existing_id, payload)
            self._raise_if_entitlements_failed(unit, existing_id, ent)
            return {"target_id": existing_id,
                    "note": self._with_entitlements(
                        f"adopted the existing account service principal {source_app_id} "
                        f"(applicationId preserved)", ent),
                    "entitlements": ent,
                    "source_detail": self._snapshot_json("service_principal", payload)}

        target_id = safe_str(created.get("id"))
        new_app_id = safe_str(created.get("applicationId"))
        if new_app_id:
            self._target_sps[new_app_id] = target_id
        # Also index by the OLD appId so a member/ACL referencing the source id still resolves, and
        # by display name for PASS-2 membership (IMP-7b).
        if source_app_id:
            self._target_sps.setdefault(source_app_id, target_id)
        self._index_display(created, payload, target_id, fallback=new_app_id or source_app_id)

        self._record_identity_row("service_principal", source_app_id, target_id, new_app_id,
                                  unit, ACTION_CREATED)
        ent = self._apply_entitlements("ServicePrincipals", target_id, payload)
        self._ensure_assignment(target_id, unit, source_app_id)
        self._raise_if_entitlements_failed(unit, target_id, ent)

        warning = ""
        if new_app_id and source_app_id and new_app_id != source_app_id:
            # Should be impossible now that applicationId is sent. If it ever happens, every ACL
            # referencing this SP is silently orphaned, so it must be loud rather than logged.
            warning = (f"applicationId CHANGED ({source_app_id} → {new_app_id}) despite being "
                       f"requested — ACLs, job run_as and secret grants referencing the source SP "
                       f"will NOT resolve on target and must be remapped by hand")
            note = warning
        else:
            note = f"applicationId {new_app_id or source_app_id} preserved from source"

        if safe_str(unit.get("note")):
            # OAuth client secrets are never exportable. This is NOT a warning on the SP (the SP is
            # created fine) — it is a standing MANUAL task tracked as its own `service_principal_secret`
            # unit (Bug 3), emitted every run. Here we only cross-reference it in the SP's note.
            note = (f"{note}. NOTE: this SP had OAuth client secret(s) on source — see the separate "
                    f"manual action for `{source_app_id}` (a secret value can never be migrated).")
        return {"target_id": target_id, "note": self._with_entitlements(note, ent),
                "warning": warning, "entitlements": ent,
                "source_detail": self._snapshot_json("service_principal", payload)}

    def _lookup_sp_id(self, app_id: str) -> str:
        """Target SCIM id for an applicationId, re-reading SCIM (the local map may predate it)."""
        if not app_id:
            return ""
        if app_id in self._target_sps:
            return self._target_sps[app_id]
        for sp in self.client.get_scim("ServicePrincipals"):
            if safe_str(sp.get("applicationId")) == app_id:
                return safe_str(sp.get("id"))
        return ""

    # ── groups (two-pass) ─────────────────────────────────────────────────
    def _create_group(self, unit: dict) -> dict:
        """Groups are the ONE identity type where create-vs-assign is a real decision.

        `WORKSPACE_LOCAL` → POST workspace SCIM (created empty; members in pass 2).
        `ACCOUNT`         → resolve the account group and ASSIGN it. **Never POST.**

        Why POSTing an account group is the worst failure in this importer (Plan 6 F6, verified live
        on target 2026-08-06): the POST succeeds and creates a workspace-local group with the SAME
        NAME — a shadow. From then on, assigning the real account group fails forever with
        "Workspace group with name X already exists", and nothing in the report looks wrong, because
        a group by that name does exist. Recovery needs a human to delete the shadow.
        """
        payload = unit.get("payload") or {}
        name = self.natural_key(unit)
        kind = _kind_of(unit)

        if kind == KIND_SYSTEM_GENERATED:
            # A Databricks-minted artifact (e.g. `users-clone-…`) — exists only as source-side
            # platform bookkeeping. Recreating it makes a meaningless workspace-local group, so it
            # is skipped with a clear note rather than migrated (IMP-7a).
            raise SkippedNoObject(
                f"group `{name}` is a Databricks-generated artifact (identity-federation "
                f"bookkeeping), not a real customer group — skipped, never recreated on target.",
                category=CAT_NOT_SUPPORTED)

        if kind == KIND_SYSTEM:
            # admins/users exist on every workspace; only their membership migrates.
            return self._add_builtin_members(unit)

        if kind == KIND_ACCOUNT:
            return self._assign_account_group(unit, name)

        if kind != KIND_WORKSPACE_LOCAL:
            # NEEDS_REVIEW (no meta.resourceType in the bundle). Refuse rather than guess: guessing
            # workspace-local risks the permanent shadow above; guessing account silently drops a
            # real group. An operator can resolve it from the inventory report.
            raise PrerequisiteMissing(
                f"group `{name}` has an undetermined kind ({kind or 'missing'}) — the bundle has no "
                f"`meta.resourceType` for it. Creating it could shadow an account group of the same "
                f"name and permanently block assigning the real one, so it is skipped. Re-run "
                f"inventory/export with this version of the tool, then retry.")

        # A workspace-local group whose name is ALREADY taken by an account group on target would
        # 400 anyway; catching it here gives the operator the actual reason.
        body = {k: payload[k] for k in _GROUP_CREATE_FIELDS if k in payload}
        body["displayName"] = name
        created = self.client.post(f"{_SCIM}/Groups", body)
        target_id = safe_str(created.get("id"))
        self._target_groups[name] = target_id
        self._record_identity_row("group", name, target_id, name, unit, ACTION_CREATED)
        ent = self._apply_entitlements("Groups", target_id, payload)

        # Membership is DEFERRED to pass 2, not patched here. Patching now would resolve members
        # against the groups created SO FAR, so a parent listed before its child would silently
        # lose the child — which is the exact failure two-pass exists to prevent.
        self._defer_members(name, target_id, payload.get("members") or [], unit)
        self._raise_if_entitlements_failed(unit, target_id, ent)
        return {"target_id": target_id,
                "note": self._with_entitlements("group created empty; members applied in pass 2",
                                                ent),
                "entitlements": ent,
                "source_detail": self._snapshot_json("group", payload)}

    def _defer_members(self, name: str, target_id: str, members: list, unit: dict) -> None:
        """Queue a group's membership for pass 2 (after EVERY group exists)."""
        if members:
            self._member_pass.append((name, target_id, members, unit))

    # ── account groups: assign, never create ──────────────────────────────
    def _assign_account_group(self, unit: dict, name: str) -> dict:
        """Assign an existing ACCOUNT group to this workspace via permissionassignments.

        Members are deliberately NOT touched. An account group's membership is account-GLOBAL, so
        patching it while migrating workspace N would alter that same group in every other workspace
        in the account — and for an Entra-backed group, SCIM would revert it anyway. Assignment
        brings the members along automatically (verified live: an assigned account group appeared on
        target already carrying its 2 members).

        Workspace-scoped ENTITLEMENTS are applied, because those are per-workspace and are the
        correct thing to set here.
        """
        payload = unit.get("payload") or {}
        target_id = self._target_groups.get(name, "")

        if target_id:
            # Already visible in target SCIM. If it is a workspace-local group of the same name, the
            # shadow already exists and the account group can never be assigned — surface it.
            if self._target_group_kinds.get(name) == "workspacegroup":
                raise PrerequisiteMissing(
                    f"`{name}` is an ACCOUNT group on the source, but the target already has a "
                    f"WORKSPACE-LOCAL group with the same name. That shadow permanently blocks "
                    f"assigning the account group. Delete the workspace-local group `{name}` on the "
                    f"target, then re-run with retry_mode=failed_only.")
            ent = self._apply_entitlements("Groups", target_id, payload)
            self._ensure_assignment(target_id, unit, name)
            self._raise_if_entitlements_failed(unit, target_id, ent)
            return {"target_id": target_id,
                    "note": self._with_entitlements(
                        "account group already assigned; entitlements re-applied "
                        "(members are account-owned and were not modified)", ent),
                    "entitlements": ent,
                    # Finding-2: snapshot INCLUDES members (for detection only) so a later run can
                    # diff a membership-only change and name the delta.
                    "source_detail": self._snapshot_json("group", payload)}

        account_id = self._resolve_account_group_id(unit, name)
        if not account_id:
            entra = bool(unit.get("entra_backed"))
            raise PrerequisiteMissing(
                f"account group `{name}` does not exist in the TARGET account, so it cannot be "
                f"assigned to this workspace. " + (
                    "It is Entra-backed — Entra SCIM provisioning must create it in the target "
                    "account first (customer IT task)." if entra else
                    "An account admin must create it in the target account first.") +
                " Then re-run with retry_mode=failed_only.")

        try:
            self.client.put(f"{_ASSIGNMENTS}/principals/{account_id}",
                            {"permissions": self._permissions_for_unit(unit)})
        except Exception as exc:  # noqa: BLE001
            entra = bool(unit.get("entra_backed"))
            # The id came from the bundle or a name lookup; if the account doesn't have that
            # principal the PUT is where we find out. Translate it into the same actionable
            # prerequisite an up-front miss produces, rather than a raw API error.
            raise PrerequisiteMissing(
                f"account group `{name}` could not be assigned to this workspace (tried account "
                f"principal id {account_id}): {str(exc)[:200]}. " + (
                    "It is Entra-backed — Entra SCIM must provision it into the target account "
                    "first (customer IT task)." if entra else
                    "An account admin must create it in the target account first.") +
                " Then re-run with retry_mode=failed_only.") from exc
        self._target_groups[name] = account_id
        self._ensured.add(safe_str(account_id))
        if self._target_assignments is not None:
            self._target_assignments[safe_str(account_id)] = self._permissions_for_unit(unit)
        self._record_identity_row("group", name, account_id, name, unit, ACTION_ADOPTED)
        ent = self._apply_entitlements("Groups", account_id, payload)
        self._raise_if_entitlements_failed(unit, account_id, ent)
        return {"target_id": account_id,
                "note": self._with_entitlements(
                    f"assigned the existing account group (id {account_id} preserved); members "
                    f"are account-owned and were not modified", ent),
                "entitlements": ent,
                # Finding-2: snapshot INCLUDES members (detection only) for a later membership diff.
                "source_detail": self._snapshot_json("group", payload)}

    def _resolve_account_group_id(self, unit: dict, name: str) -> str:
        """The TARGET ACCOUNT's id for this group — by displayName, else externalId.

        Both keys resolve to the same account record (Plan 6 F2), so `externalId` is a fallback for
        a group renamed between regions rather than a separate lookup path.

        Enumeration needs account read access. In `airgap` mode with workspace-admin only we cannot
        list the account, so an optional `account_principal_ids.json` in the bundle (populated by
        the caller into `context`) is consulted first — which keeps the air-gap intact while still
        letting the workspace-side PUT do the actual work.
        """
        preresolved = (self.context.get("account_principal_ids") or {}).get("groups") or {}
        for key in (name, safe_str(unit.get("externalId"))):
            if key and safe_str(preresolved.get(key)):
                return safe_str(preresolved[key])

        account_client = self.context.get("account_client")
        if account_client is None:
            # No account credentials. An account group's WORKSPACE SCIM id IS its ACCOUNT id
            # (verified live: `wsmig_acc_mixed_grp` is 152592557989155 in both), so when source and
            # target share an account the exported `source_id` is already the account id — and
            # `permissionassignments` accepts it workspace-side (F7). Probing it is safe: a wrong id
            # simply 404s/400s, which is reported exactly as a missing group would be.
            return safe_str(unit.get("source_id"))
        external_id = safe_str(unit.get("externalId"))
        try:
            for group in account_client.get_scim("Groups"):
                if safe_str(group.get("displayName")) == name:
                    return safe_str(group.get("id"))
                if external_id and safe_str(group.get("externalId")) == external_id:
                    return safe_str(group.get("id"))
        except Exception as exc:  # noqa: BLE001 — degrade to "unresolved", never abort the phase
            self.log.warning("could not enumerate account groups", group=name, error=str(exc)[:200])
        return ""

    def _permissions_for_unit(self, unit: dict) -> list:
        """Exported workspace permissions, defaulting to USER.

        `None` means inventory COULD NOT read permissionassignments (not "no permissions"), so it
        must not be treated as an empty grant — USER is the minimum that makes a principal usable,
        and over-granting ADMIN on a guess would be worse.

        PLAN 16.3.1 §3B.8: a nested-only identity (recorded `[]`) must never reach here — every
        caller skips it. Failing loud makes a future regression a red test, not a silent re-assign.
        """
        if self._is_nested_only(unit):
            raise RuntimeError("nested-only identity reached the assignment path")
        permissions = self._recorded_permissions(unit)
        if not permissions:
            return ["USER"]
        return [p for p in permissions if safe_str(p)] or ["USER"]

    def _ensure_assignment(self, principal_id: str, unit: dict, name: str) -> bool:
        """PUT the workspace permission if it differs from what the target already has. Returns
        whether a PUT was sent and accepted.

        This is the only carrier of workspace ADMIN vs USER (Plan 6 F8) — SCIM entitlements do not
        express it, so without this an admin on source silently lands as a plain USER on target.
        At most once per principal per run (the adopt reconcile and the post-adopt hook both call
        it). NEVER called for a nested-only identity (PLAN 16.3.1) — `_permissions_for_unit` raises.
        """
        wanted = self._permissions_for_unit(unit)
        pid = safe_str(principal_id)
        if pid in self._ensured:
            return False
        self._ensured.add(pid)
        if self._target_assignments is not None:
            current = self._target_assignments.get(pid)
            if current is not None and sorted(current) == sorted(wanted):
                self.log.debug(f"workspace permission {name}: already {sorted(wanted)}")
                return False
        self.log.debug(f"assigning workspace permission {name} → {wanted}",
                       principal_id=principal_id)
        try:
            self.client.put(f"{_ASSIGNMENTS}/principals/{principal_id}", {"permissions": wanted})
            self.log.debug(f"workspace permission {name} → assigned {wanted}")
        except Exception as exc:  # noqa: BLE001 — the identity exists; don't fail it over this
            self.log.warning("could not set workspace permissions", principal=name,
                             error=str(exc)[:200])
            self.result.warnings.append(
                f"group/{name}: could not set workspace permissions {wanted}: {str(exc)[:160]}")
            return False
        if self._target_assignments is not None:
            self._target_assignments[pid] = list(wanted)
        return True

    def run(self):
        """Pass 1 (create/adopt every identity) → PASS 2 (apply group membership).

        The second pass is what makes membership order-independent: by the time it runs, every user,
        SP and group exists on target, so a parent group listed before its child still resolves the
        child. Doing it inside each group's create would resolve against only the groups made so far
        and silently under-populate any group whose members come later in the bundle.
        """
        result = super().run()
        self._apply_deferred_members()
        # PASS 3 (PLAN 16.3.1 §3B.6): nested-only identities, reached through their parent group.
        self._apply_nested_pass()
        self._apply_held_members()
        self.flush_checkpoint()
        return result

    def _apply_deferred_members(self) -> None:
        """PASS 2: patch every deferred group's members, now that all identities exist."""
        if not self._member_pass:
            self.log.info("group membership pass 2: no groups need members applied")
            return
        t0 = time.time()
        total = len(self._member_pass)
        self.log.info(f"Phase: group membership pass 2 — {total:,} groups")
        degraded_n = failed_n = 0
        for i, (name, target_id, members, unit) in enumerate(self._member_pass, 1):
            self.log.debug(f"applying members to group {name} ({len(members or [])} members)")
            try:
                note = self._sync_members(target_id, members, group_key=name)
            except Exception as exc:  # noqa: BLE001 — fail-soft per group (D21)
                from src.importers.base_importer import classify_error
                category, message = classify_error(exc)
                self._amend_row(name, "created_with_warning",
                                f"group created, but its members could not be applied: {message}",
                                category=category, error_raw=str(exc))
                self.log.warning(f"group membership {name} → FAILED {category}: {message}")
                failed_n += 1
                self.log.progress("group membership pass 2", i, total, started=t0,
                                  degraded=degraded_n, failed=failed_n)
                continue
            # An unresolvable member is a real gap — the group EXISTS but is under-populated — so it
            # is flagged rather than reported clean. `retry_mode=failed_only` picks it up once the
            # missing identity is assigned.
            degraded = "could not resolve" in note
            self._amend_row(name, "created_with_warning" if degraded else "", note,
                            category="prerequisite_missing" if degraded else "")
            if degraded:
                degraded_n += 1
                self.log.warning(f"group membership {name} → under-populated: {note}")
            else:
                self.log.debug(f"group membership {name} → {note}")
            self.log.progress("group membership pass 2", i, total, started=t0,
                              degraded=degraded_n, failed=failed_n)
        self.log.info(f"Phase complete: group membership pass 2 — {total:,} groups, "
                      f"under-populated {degraded_n}, failed {failed_n} "
                      f"({fmt_elapsed(time.time() - t0)})")

    def _amend_row(self, natural_key: str, status: str, note: str, *, category: str = "",
                   error_raw: str = "", asset_type: str = "group") -> None:
        """Update an already-recorded row's status/note after pass 2.

        Pass 1 recorded the group as `created` before its membership was known; the outcome the
        operator needs is the COMBINED one, so the row is amended in place (report + state) rather
        than a second row being appended for the same unit.
        """
        for row in self.result.units:
            if safe_str(row.get("natural_key")) != natural_key or row.get("asset_type") != asset_type:
                continue
            old_status = safe_str(row.get("import_status"))
            new_status = status or old_status
            if new_status != old_status:
                # keep the counters honest when a `created` becomes `created_with_warning`
                if old_status in self.result._COUNTER_FOR:
                    attr = self.result._COUNTER_FOR[old_status]
                    setattr(self.result, attr, max(getattr(self.result, attr) - 1, 0))
                if new_status in self.result._COUNTER_FOR:
                    attr = self.result._COUNTER_FOR[new_status]
                    setattr(self.result, attr, getattr(self.result, attr) + 1)
                row["import_status"] = new_status
            row["note"] = note
            if category:
                row["failure_category"] = category
            if new_status == "created_with_warning":
                self.result.warnings.append(f"{asset_type}/{natural_key}: {note}")
            if self.state is not None:
                self.state.record(asset_type, natural_key, action=new_status,
                                  fingerprint=safe_str(row.get("fingerprint")),
                                  source_object_id=safe_str(row.get("source_id")),
                                  target_object_id=safe_str(row.get("target_id")),
                                  error=note if new_status != "created" else "",
                                  error_raw=error_raw, failure_category=category)
            self._pending_cp_results[natural_key] = {
                "import_status": new_status, "target_id": safe_str(row.get("target_id")),
                "fingerprint": safe_str(row.get("fingerprint")),
                "source_id": safe_str(row.get("source_id")), "note": note}
            if natural_key not in self._pending_cp:
                self._pending_cp.append(natural_key)
            return

    def _sync_members(self, group_id: str, members: list, *, group_key: str = "",
                      asset_type: str = "group") -> str:
        """PATCH the group's members, resolving each on the target.

        A member is matched by its SOURCE id first: an ACCOUNT identity (user, account SP, account
        group) keeps the same id in every workspace of the account, so `value` IS its target id once
        it is on the target. Only when the id is not there (a recreated workspace-local identity)
        does it fall back to the name — and display names are NOT stable across workspaces for the
        same account user (seen live 2026-11-25: 13 of `users`' 175 members), so name-first matching
        reported members as missing that were already on the target.

        A member that is a nested-only identity still waiting for PASS 3 is HELD and added after
        PASS 3 (`_apply_held_members`), not reported as unresolved. A member that resolves neither
        way is skipped and NAMED — sending a dangling id would fail the whole PATCH and lose the
        resolvable members too, so partial success with an explicit list beats all-or-nothing.
        """
        known_ids = self._known_target_ids()
        nested_ids, nested_names = self._pending_nested_keys()
        resolved, unresolved, held = [], [], []
        for member in members or []:
            if not isinstance(member, dict):
                continue
            display = safe_str(member.get("display"))
            value = safe_str(member.get("value"))
            tid = self._resolve_member(display, safe_str(member.get("kind")), value=value,
                                       known_ids=known_ids)
            if tid:
                resolved.append({"value": tid})
            elif (value and value in nested_ids) or (display and display in nested_names):
                held.append(member)
            elif display:
                unresolved.append(f"{safe_str(member.get('kind')) or 'member'}:{display}")

        if resolved:
            self.client.patch(f"{_SCIM}/Groups/{group_id}", {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "add", "path": "members", "value": resolved}],
            })
        note = self._member_note(len(resolved), len(members or []), unresolved, held=len(held))
        if held and group_key:
            self._held_members.append((asset_type, group_key, group_id, held, len(resolved),
                                       len(members or []), unresolved, note))
        return note

    @staticmethod
    def _member_note(added: int, total: int, unresolved: list, *, held: int = 0,
                     after_nested: int = 0) -> str:
        parts = [f"{added}/{total} members added"
                 + (f" ({after_nested} after the nested-identity pass)" if after_nested else "")]
        if held:
            parts.append(f"{held} member(s) nested on source are added after the nested-identity "
                         f"pass")
        if unresolved:
            parts.append(f"could not resolve {len(unresolved)} member(s) on target "
                         f"({', '.join(unresolved[:5])}) — they were not created/assigned, so the "
                         f"group stays under-populated until they are")
        return "; ".join(parts)

    def _known_target_ids(self) -> set:
        """Every target SCIM id known so far (users, SPs, groups) — for id-first member matching."""
        return (set(self._target_users.values()) | set(self._target_sps.values())
                | set(self._target_groups.values())) - {""}

    def _pending_nested_keys(self) -> tuple:
        """(source ids, names) of the nested-only identities queued for PASS 3."""
        ids, names = set(), set()
        for unit in self._nested_pass:
            if safe_str(unit.get("source_id")):
                ids.add(safe_str(unit.get("source_id")))
            for n in (self.natural_key(unit), safe_str((unit.get("payload") or {}).get("displayName"))):
                if n:
                    names.add(n)
        return ids, names

    def _apply_held_members(self) -> None:
        """After PASS 3: add the held nested-only members to their groups and amend each row so
        the report states the FINAL membership (not the interim "held" note)."""
        held_groups, self._held_members = self._held_members, []
        for asset_type, key, group_id, held, added, total, unresolved, old_note in held_groups:
            known_ids = self._known_target_ids()
            resolved, still = [], list(unresolved)
            for member in held:
                display = safe_str(member.get("display"))
                tid = self._resolve_member(display, safe_str(member.get("kind")),
                                           value=safe_str(member.get("value")), known_ids=known_ids)
                if tid:
                    resolved.append({"value": tid})
                else:
                    still.append(f"{safe_str(member.get('kind')) or 'member'}:{display}")
            try:
                if resolved:
                    self.client.patch(f"{_SCIM}/Groups/{group_id}", {
                        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                        "Operations": [{"op": "add", "path": "members", "value": resolved}],
                    })
            except Exception as exc:  # noqa: BLE001 — fail-soft per group, like pass 2
                still = list(unresolved) + [
                    f"{safe_str(m.get('kind')) or 'member'}:{safe_str(m.get('display'))}"
                    for m in held]
                resolved = []
                self.log.warning(f"group membership {key}: adding nested members failed",
                                 error=str(exc)[:200])
            new_note = self._member_note(added + len(resolved), total, still,
                                         after_nested=len(resolved))
            row_note = next((safe_str(r.get("note")) for r in self.result.units
                             if r.get("asset_type") == asset_type
                             and safe_str(r.get("natural_key")) == key), old_note)
            note = row_note.replace(old_note, new_note) if old_note in row_note else new_note
            degraded = bool(still)
            self._amend_row(key, "created_with_warning" if degraded else "", note,
                            category="prerequisite_missing" if degraded else "",
                            asset_type=asset_type)
            self.log.info(f"group membership {key} → {new_note}")

    def _resolve_member(self, display: str, kind: str, *, value: str = "",
                        known_ids: set | None = None) -> str:
        """A member's TARGET SCIM id: by source id first (account identities keep their id across
        workspaces), then by name. Falls back across types when `kind` is unknown."""
        if value and value in (known_ids if known_ids is not None else self._known_target_ids()):
            return value
        if not display:
            return ""
        maps = {"user": self._target_users, "service_principal": self._target_sps,
                "group": self._target_groups}
        if kind in maps and display in maps[kind]:
            return maps[kind][display]
        # A DB-managed SP was recreated under a NEW appId, so the SOURCE appId won't match any
        # target key — go through the id map, which is precisely what it exists for.
        if kind in ("service_principal", "unknown"):
            mapped = (self.identity_map.get("sp_mapping") or {}).get(display, "")
            if mapped and mapped in self._target_sps:
                return self._target_sps[mapped]
        # Fall back to the DISPLAY-NAME index: SCIM member entries carry `display`, which for a user
        # is their display name rather than their userName, so the per-kind maps above cannot match.
        if display in self._target_by_display:
            return self._target_by_display[display]
        # `kind` is "unknown" when the source $ref was absent — try every type before giving up.
        for m in (self._target_users, self._target_sps, self._target_groups):
            if display in m:
                return m[display]
        return ""

    def _add_builtin_members(self, unit: dict) -> dict:
        """Add members to a group that ALREADY exists (`admins`/`users`), never create it.

        Without this a source workspace admin would silently NOT be an admin on target: the built-in
        group object exists on every workspace, but its membership does not carry over.
        """
        payload = unit.get("payload") or {}
        name = safe_str(payload.get("displayName")) or self.natural_key(unit)
        target_id = self._target_groups.get(name, "")
        if not target_id:
            raise PrerequisiteMissing(
                f"built-in group `{name}` was not found on the target workspace, which should be "
                f"impossible — check that the run-as identity can read SCIM Groups")
        self._record_identity_row("group", name, target_id, name, unit, ACTION_ADOPTED)
        note = self._sync_members(target_id, payload.get("members") or [],
                                  group_key=self.natural_key(unit), asset_type="group_membership")
        return {"target_id": target_id, "note": f"members added to the existing group: {note}",
                "warning": note if "could not resolve" in note else "",
                "source_detail": self._snapshot_json("group", payload)}

    # ── entitlements + roles (SEPARATE PATCH passes after create) ──────────
    def _apply_entitlements(self, resource: str, target_id: str, payload: dict) -> dict:
        """Apply `entitlements` then `roles` — VERIFIED (PLAN 16.3.1 DB5) — and return
        `{"copied", "present", "failed", "errors"}` (role values prefixed `role:`).

          1. GET the identity's current entitlements/roles (`before`);
          2. PATCH (op add, one per attribute) ONLY the values `before` lacks;
          3. GET again (up to ENTITLEMENT_READ_TRIES) until every sent value shows.
        copied = sent and read back; present = already there; failed = a PATCH error (with the
        server's message) or "sent, but not present after a fresh GET". Nothing to apply (most
        users) → no call at all, exactly as 1.2.0.

        Separate PATCHes because several workspaces reject them inline at create, and per
        attribute so one being unsupported doesn't lose the other. This never raises: the caller
        decides (`_raise_if_entitlements_failed` fails the row as `not_applied`, which replaced
        1.2.0's warning that never reached the report).

        Known, unfixable limitation (inherited from `databrickslabs/migrate`): a role granted BOTH
        directly and via a group is indistinguishable through the API, so only the group grant
        migrates. It surfaces in the ACL parity report rather than being quietly written off.
        """
        result: dict = {"copied": [], "present": [], "failed": [], "errors": []}
        wanted: dict = {}
        for attribute in ("entitlements", "roles"):
            values = payload.get(attribute) or []
            entries = [v if isinstance(v, dict) else {"value": v} for v in values]
            entries = [e for e in entries if safe_str(e.get("value"))]
            if attribute == "roles":
                # Account-level roles are account-GLOBAL. Replaying `account_admin` while migrating
                # workspace N would escalate that identity across every workspace in the account —
                # a privilege escalation, not a migration. Only workspace-scoped grants travel.
                dropped = [e for e in entries
                           if safe_str(e.get("value")).lower() in _ACCOUNT_LEVEL_ROLES]
                entries = [e for e in entries
                           if safe_str(e.get("value")).lower() not in _ACCOUNT_LEVEL_ROLES]
                for entry in dropped:
                    self.log.info("skipped account-level role (account-global, not migrated)",
                                  resource=resource, target_id=target_id,
                                  role=safe_str(entry.get("value")))
            seen: set = set()
            unique = []
            for e in entries:
                v = safe_str(e.get("value"))
                if v not in seen:
                    seen.add(v)
                    unique.append(e)
            if unique:
                wanted[attribute] = unique
        if not wanted or not target_id:
            return result

        path = f"{_SCIM}/{resource}/{target_id}"
        try:
            before = self._read_identity_attributes(path)
        except Exception as exc:  # noqa: BLE001 — unknown `before`: send everything (add is idempotent)
            self.log.warning("could not read identity entitlements before applying them",
                             resource=resource, target_id=target_id, error=str(exc)[:200])
            before = {}

        sent: dict = {}
        for attribute, entries in wanted.items():
            have = before.get(attribute, set())
            missing = []
            for e in entries:
                v = safe_str(e.get("value"))
                if v in have:
                    result["present"].append(_ent_label(attribute, v))
                else:
                    missing.append(e)
            if not missing:
                continue
            try:
                self.client.patch(path, {
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "add", "path": attribute, "value": missing}],
                })
            except Exception as exc:  # noqa: BLE001 — reported per value; the identity exists
                _cat, message = classify_error(exc)
                self.log.warning("could not apply identity attribute", attribute=attribute,
                                 resource=resource, target_id=target_id, error=str(exc)[:200])
                result["errors"].append(str(exc))
                for e in missing:
                    result["failed"].append(
                        f"{_ent_label(attribute, safe_str(e.get('value')))} ({message})")
                continue
            sent[attribute] = [safe_str(e.get("value")) for e in missing]

        if sent:
            after: dict = {}
            read_error = ""
            for attempt in range(ENTITLEMENT_READ_TRIES):
                try:
                    after = self._read_identity_attributes(path)
                    read_error = ""
                except Exception as exc:  # noqa: BLE001 — an unreadable result is not a success
                    after, read_error = {}, str(exc)
                if all(v in after.get(a, set()) for a, vals in sent.items() for v in vals):
                    break
                if attempt < ENTITLEMENT_READ_TRIES - 1:
                    _sleep(ENTITLEMENT_READ_SEC)
            for attribute, vals in sent.items():
                for v in vals:
                    if v in after.get(attribute, set()):
                        result["copied"].append(_ent_label(attribute, v))
                    else:
                        why = (f"sent, but could not be read back: {read_error[:200]}"
                               if read_error else "sent, but not present after a fresh GET")
                        result["failed"].append(f"{_ent_label(attribute, v)} ({why})")
                        result["errors"].append(read_error or
                                                f"{_ent_label(attribute, v)}: PATCH returned 2xx "
                                                f"but a fresh GET does not show it")
        return result

    def _read_identity_attributes(self, path: str) -> dict:
        """`{"entitlements": {values}, "roles": {values}}` as the target has them NOW."""
        data = self.client.get(path, params={"attributes": "entitlements,roles"}) or {}
        out: dict = {}
        for attribute in ("entitlements", "roles"):
            out[attribute] = {self._value_of(v) for v in (data.get(attribute) or [])
                              if self._value_of(v)}
        return out

    @staticmethod
    def _with_entitlements(note: str, ent: dict) -> str:
        """`note` + "; entitlements: copied …; already present …; failed …" (only non-empty parts)."""
        parts = []
        for field, label in (("copied", "copied"), ("present", "already present"),
                             ("failed", "failed")):
            if ent and ent.get(field):
                parts.append(f"{label} {', '.join(ent[field])}")
        if not parts:
            return note
        summary = "entitlements: " + "; ".join(parts)
        return f"{note}; {summary}" if note else summary

    def _raise_if_entitlements_failed(self, unit: dict, target_id: str, ent: dict) -> None:
        """DB5: an entitlement/role that was refused, or that a fresh GET does not show, fails the
        row (`not_applied`) — the identity exists, so the row still carries its target id, and
        `retry_mode=failed_only` re-applies + re-verifies it."""
        if not ent or not ent.get("failed"):
            return
        message = (f"{safe_str(unit.get('asset_type'))} `{self.natural_key(unit)}` exists on "
                   f"target, but entitlements could not be applied: {', '.join(ent['failed'])}. "
                   f"Re-run with retry_mode=failed_only.")
        raise EntitlementsNotApplied(message, target_id=safe_str(target_id), entitlements=ent,
                                     error_raw=safe_str((ent.get("errors") or [message])[0]))

    # ── PASS 3: nested-only identities (PLAN 16.3.1 §3B.6) ────────────────
    _NESTED_RESOURCE = {"group": "Groups", "service_principal": "ServicePrincipals",
                        "user": "Users"}
    _NESTED_KEY_FIELD = {"group": "displayName", "service_principal": "applicationId",
                         "user": "userName"}

    def _apply_nested_pass(self) -> None:
        """Resolve every deferred nested-only identity through its parent, then apply its
        entitlements. Never assigns anything.

        Polls ONE SCIM list per needed kind every NESTED_POLL_SEC, up to NESTED_WAIT_SEC (PB6
        measured 3-6 s), and stops as soon as all are visible. An identity still invisible after
        the budget is `prerequisite_missing` naming its parents and their outcome in this run
        (DB1) — never a fallback to a direct assignment."""
        pending = list(self._nested_pass)
        self._nested_pass = []
        if not pending:
            self.log.info("nested-identity pass 3: none")
            return
        t0 = time.time()
        total = len(pending)
        self.log.info(f"Phase: nested-identity pass 3 — {total:,} identities nested on source")
        if self.dry_run:            # defensive: a dry run never reaches create_one
            return
        resolved_n = 0
        deadline = time.time() + NESTED_WAIT_SEC
        while pending:
            kinds = {safe_str(u.get("asset_type")) for u in pending}
            visible: dict = {}
            for kind in sorted(kinds):
                field = self._NESTED_KEY_FIELD[kind]
                index: dict = {}
                try:
                    for obj in self.client.get_scim(self._NESTED_RESOURCE[kind]):
                        k = safe_str(obj.get(field))
                        if k:
                            index[k] = obj
                except Exception as exc:  # noqa: BLE001 — one failed poll; the next may succeed
                    self.log.warning("nested-identity pass 3: could not list target SCIM",
                                     resource=self._NESTED_RESOURCE[kind], error=str(exc)[:200])
                visible[kind] = index
            still = []
            for unit in pending:
                obj = visible.get(safe_str(unit.get("asset_type")), {}).get(self.natural_key(unit))
                if obj and safe_str(obj.get("id")):
                    self._resolve_nested(unit, obj)
                    resolved_n += 1
                else:
                    still.append(unit)
            pending = still
            self.log.info(f"nested-identity pass 3: {resolved_n}/{total} visible")
            if not pending or time.time() >= deadline:
                break
            _sleep(NESTED_POLL_SEC)
        for unit in pending:
            self._record_nested_unresolved(unit)
        # Re-publish for the cross-phase consumers (job run_as, secret-scope MANAGE, ACL principals)
        # so they see the identities pass 3 found (PB4/PB5: those grants succeed for them).
        self.context.setdefault("target_identities", {}).update({
            "users": self._target_users, "service_principals": self._target_sps,
            "groups": self._target_groups})
        self.log.info(f"Phase complete: nested-identity pass 3 — {total:,} identities, visible "
                      f"{resolved_n}, prerequisite_missing {len(pending)} "
                      f"({fmt_elapsed(time.time() - t0)})")

    def _resolve_nested(self, unit: dict, obj: dict) -> None:
        """Index a nested identity now visible on target, apply its entitlements, record its row."""
        asset_type = safe_str(unit.get("asset_type"))
        key = self.natural_key(unit)
        payload = unit.get("payload") or {}
        target_id = safe_str(obj.get("id"))
        if asset_type == "group":
            self._target_groups[key] = target_id
            self._target_group_kinds[key] = safe_str(
                (obj.get("meta") or {}).get("resourceType")).lower()
        elif asset_type == "service_principal":
            self._target_sps[key] = target_id
            self._index_display(obj, payload, target_id, fallback=key)
        else:
            self._target_users[key] = target_id
            self._index_display(obj, payload, target_id, fallback=key)
        ent = self._apply_entitlements(self._NESTED_RESOURCE[asset_type], target_id, payload)
        note = (f"nested on source via {self._parents_text(unit)}; reached through its group on "
                f"the target, not directly assigned (as on source)")
        note = self._with_entitlements(note, ent)
        source_detail = self._snapshot_json(asset_type, payload)
        try:
            self._raise_if_entitlements_failed(unit, target_id, ent)
        except EntitlementsNotApplied as exc:
            self._record(unit, ACTION_FAILED, target_id=target_id, note=str(exc),
                         error_raw=exc.error_raw, category=CAT_NOT_APPLIED, entitlements=ent)
        else:
            self._record(unit, ACTION_ADOPTED, target_id=target_id, note=note,
                         source_detail=source_detail, entitlements=ent)
            self._record_identity_row(asset_type, key, target_id, key, unit, ACTION_ADOPTED)
        self._stamp_assignment(unit, target_id)

    def _record_nested_unresolved(self, unit: dict) -> None:
        """DB1: no parent reached the target, so the identity is not visible — a retryable
        prerequisite naming each parent and what happened to it in this run."""
        key = self.natural_key(unit)
        outcomes = {safe_str(r.get("natural_key")): safe_str(r.get("import_status"))
                    for r in self.result.units if safe_str(r.get("asset_type")) == "group"}
        in_bundle = {self.natural_key(u) for u in self.units_by_type.get("group", []) or []}
        parents = self._parents_of(unit)
        described = [f"`{p}`: {outcomes.get(p) or ('not processed' if p in in_bundle else 'not in bundle')}"
                     for p in parents]
        note = (f"nested on source via {self._parents_text(unit)}; none of them is on the target, so "
                f"`{key}` is not visible here. Import or assign a parent, then re-run with "
                f"retry_mode=failed_only."
                + (f" Parents this run: {'; '.join(described)}." if described else ""))
        self._record(unit, ACTION_FAILED, note=note, error_raw=note,
                     category=CAT_PREREQUISITE_MISSING)
        self._stamp_assignment(unit, "")

    def _stamp_assignment(self, unit: dict, target_id: str) -> None:
        """Add the report's "Assignment" value to the unit's latest row."""
        if not self.result.units:
            return
        row = self.result.units[-1]
        if (safe_str(row.get("natural_key")) == self.natural_key(unit)
                and safe_str(row.get("asset_type")) == safe_str(unit.get("asset_type"))):
            label = self._assignment_label(unit, target_id)
            if label:
                row["assignment"] = label

    # ── source snapshot + cross-run diff (PLAN 8 Bug 5) ───────────────────
    @staticmethod
    def _value_of(x) -> str:
        return safe_str(x.get("value") if isinstance(x, dict) else x)

    def _source_snapshot(self, asset_type: str, payload: dict,
                         include_members: bool = True) -> dict:
        """The migration-relevant SOURCE state we persist per run and diff on the next: entitlements
        + roles (all identity types) and members (groups only). Each is a sorted list of stable
        string keys, so the diff is deterministic."""
        snap: dict = {
            "entitlements": sorted({self._value_of(e) for e in (payload.get("entitlements") or [])
                                    if self._value_of(e)}),
            "roles": sorted({self._value_of(r) for r in (payload.get("roles") or [])
                             if self._value_of(r)}),
        }
        if asset_type == "group" and include_members:
            snap["members"] = sorted({safe_str(m.get("display"))
                                      for m in (payload.get("members") or [])
                                      if isinstance(m, dict) and safe_str(m.get("display"))})
        return snap

    def _snapshot_json(self, asset_type: str, payload: dict, include_members: bool = True) -> str:
        return json.dumps(self._source_snapshot(asset_type, payload, include_members),
                          sort_keys=True)

    @staticmethod
    def _parse_snapshot(raw) -> dict:
        try:
            return json.loads(raw) if raw else {}
        except (TypeError, ValueError) as exc:   # degraded: no prior snapshot → no change diff
            _LOG.warning(f"stored source snapshot is not valid JSON — the change diff for this "
                         f"identity is skipped: {exc}")
            return {}

    def _diff_note(self, asset_type: str, unit: dict, payload: dict,
                   include_members: bool = True, members_applied: bool = True) -> str:
        """Name exactly what changed since the last run: what was ADDED, and what was REMOVED IN
        SOURCE (reported, not applied — the object on target keeps it, per the additive stance).

        PLAN 11 Finding-2: the member component is now computed for ACCOUNT/Entra groups too (for
        DETECTION/reporting — `members_applied=False` for them, since account/Entra-group membership
        is account-managed and the tool does NOT migrate it). The account branch used to drop the
        member component entirely, so a membership-only change fell through to the false
        "no source-side change detected" string — which contradicted the `updated` status that the
        moved fingerprint had already produced. The granularity needed already exists in
        `last_source_detail`; no new data is captured.

        First run after this column was added has no prior snapshot — degrade to a neutral note
        rather than crash on the null (the baseline is captured this run and diffs work thereafter).
        """
        prior = {}
        if self.state is not None:
            row = self.state.row(asset_type, self.natural_key(unit)) or {}
            prior = self._parse_snapshot(row.get("last_source_detail"))
        if not prior:
            return "re-applied; prior source detail not recorded (baseline captured this run)"
        cur = self._source_snapshot(asset_type, payload, include_members)
        parts = []
        for field, label in (("entitlements", "entitlements"), ("roles", "roles")):
            if field not in cur and field not in prior:
                continue
            p, c = set(prior.get(field) or []), set(cur.get(field) or [])
            added, removed = sorted(c - p), sorted(p - c)
            if added:
                parts.append(f"{label} added: {added} — applied on target (workspace-scoped)")
            if removed:
                parts.append(f"{label} removed in source ({removed}) — not removed on target (review)")
        if include_members and ("members" in cur or "members" in prior):
            p, c = set(prior.get("members") or []), set(cur.get("members") or [])
            added, removed = sorted(c - p), sorted(p - c)
            if members_applied:
                # Workspace-local group: ADDED members are applied (the PATCH-add pass); REMOVED
                # ones are REPORTED, not applied (retained on target — the additive stance).
                if added:
                    parts.append(f"members added: {added} — applied on target")
                if removed:
                    parts.append(
                        f"members removed in source (retained on target — review): {removed}")
            elif added or removed:
                # Account/Entra group: the membership change IS detected (this is why the
                # fingerprint moved and the status is `updated`), but account/Entra-group membership
                # is account-managed and NOT migrated by this tool.
                delta = []
                if added:
                    delta.append(f"added: {added}")
                if removed:
                    delta.append(f"removed: {removed}")
                parts.append(
                    f"membership changed in source ({'; '.join(delta)}); account/Entra-group "
                    f"membership is account-managed — NOT migrated by the tool (present via the "
                    f"account group in the same account; cross-account, provision via SCIM/Entra). "
                    f"Workspace entitlements/ACLs re-applied.")
        return "; ".join(parts) if parts else "re-applied; no source-side change detected"

    # ── the identity map row ──────────────────────────────────────────────
    def _record_identity_row(self, entity_type: str, source_key: str, target_id: str,
                             target_key: str, unit: dict, action: str) -> None:
        """Write the durable map row NOW, not at the end of the phase (§7.1)."""
        if self.state is None:
            return
        self.state.record_identity(entity_type, source_key, target_id=target_id,
                                   target_key=target_key or source_key,
                                   source_id=safe_str(unit.get("source_id")),
                                   classification=safe_str(unit.get("classification")),
                                   action=action)

    # ── adopt hook: record the map row for identities we DON'T create ─────
    def _process_one(self, unit: dict, existing: dict) -> None:
        """Wrap the base flow so an ADOPT/SKIP also records its identity-map row.

        First, though, the shadow check has to happen HERE rather than only in `_create_group`. A
        workspace-local group squatting an account group's name IS present in `existing_keys`, so the
        base class would classify it ADOPT and never call the create path at all — silently binding
        the migration to the shadow, whose members and ACLs are unrelated to the real account group.
        Catching it before the base flow turns the worst silent failure in this importer into a
        loud, actionable one.

        Not an optimisation — required. An account SP keeps its applicationId but its TARGET SCIM id
        differs, and every later permission call needs that target id. An adopt-without-recording
        would leave every ACL principal remap unable to resolve the principal.
        """
        if (safe_str(unit.get("asset_type")) == "group"
                and _kind_of(unit) == KIND_ACCOUNT
                and self._target_group_kinds.get(self.natural_key(unit)) == "workspacegroup"):
            # (also for a nested-only account group: the shadow blocks its visibility the same way)
            name = self.natural_key(unit)
            message = (
                f"`{name}` is an ACCOUNT group on the source, but the target already has a "
                f"WORKSPACE-LOCAL group with the same name — a shadow. It permanently blocks "
                f"assigning the account group ('Workspace group with name {name} already exists'), "
                f"and the shadow's members/ACLs are NOT the account group's. Delete the "
                f"workspace-local group `{name}` on the target, then re-run with "
                f"retry_mode=failed_only.")
            self._record(unit, ACTION_FAILED, note=message, error_raw=message,
                         category="prerequisite_missing")
            return

        before = len(self.result.units)
        super()._process_one(unit, existing)
        if len(self.result.units) == before:
            return        # deferred to PASS 3 (nested-only) — that pass records the row
        row = self.result.units[-1]
        if safe_str(row.get("natural_key")) != self.natural_key(unit):
            return
        self._stamp_assignment(unit, safe_str(row.get("target_id")))
        if row.get("dry_run") and safe_str(unit.get("asset_type")) in (
                "user", "service_principal", "group"):
            payload = unit.get("payload") or {}
            would = [self._value_of(e) for e in (payload.get("entitlements") or [])]
            would += [_ent_label("roles", self._value_of(r)) for r in (payload.get("roles") or [])
                      if self._value_of(r).lower() not in _ACCOUNT_LEVEL_ROLES]
            row["entitlements_would_copy"] = ", ".join(v for v in would if v)
        status = safe_str(row.get("import_status"))
        # The create path already wrote its row (with the NEW appId, which this generic path cannot
        # know), and a failure has no target id worth recording.
        if status in (ACTION_CREATED, ACTION_CREATED_WITH_WARNING, ACTION_FAILED):
            return
        target_id = safe_str(row.get("target_id"))
        if not target_id or row.get("dry_run"):
            return
        entity = {"user": "user", "service_principal": "service_principal",
                  "group": "group", "group_membership": "group"}.get(
                      safe_str(unit.get("asset_type")), "")
        if entity:
            self._record_identity_row(entity, self.natural_key(unit), target_id,
                                      self.natural_key(unit), unit, status)

        # An ADOPTed identity still needs its workspace permission applied. Found by the live run:
        # a user already present on target (adopted, never created) kept whatever ADMIN/USER it
        # happened to have, so a source ADMIN silently stayed a plain USER — the exact failure the
        # permissionassignments pass exists to prevent. The create paths call this themselves; this
        # covers every path that does NOT create. NEVER for a nested-only identity (PLAN 16.3.1 —
        # 1.2.0 PUT USER here on every re-run, directly assigning what the source only nested).
        if (safe_str(unit.get("asset_type")) in ("user", "service_principal", "group")
                and not self._is_nested_only(unit)):
            self._ensure_assignment(target_id, unit, self.natural_key(unit))
