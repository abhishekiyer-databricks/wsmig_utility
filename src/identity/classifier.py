"""
Identity classifier (v2 — Plan 6).

Decides, per identity, whether the target-side importer CREATEs it or merely ASSIGNs it.
Verified live 2026-08-06; see plans/PLAN_6_identity_v2.md §2 for the probe evidence.

**Only GROUPS need classifying.** A `POST` to WORKSPACE SCIM `/Users` or `/ServicePrincipals`
creates the principal AT THE ACCOUNT and assigns it to this workspace, returning the *same* id the
account has (Plan 6 F3). So there is no such thing as a workspace-local user or service principal —
every one of them is an account principal, and the only question is whether it is assigned here.
That is why `classify_user`/`classify_service_principal` do not exist: they had nothing to decide,
and the old `externalId`-based versions produced false `NEEDS_REVIEW` on every Databricks-native
user.

For groups the signal is `meta.resourceType` from the workspace SCIM LIST response, NOT
`externalId`:
  • "WorkspaceGroup" → workspace-local  → RECREATE on target
  • "Group"          → account group    → ASSIGN via permissionassignments; NEVER POST

`externalId` is deliberately NOT used to decide anything. An Entra group is just an account group
whose `externalId` carries the Entra object id, and it resolves at the account by `displayName` or
`externalId` interchangeably (F2) — so Entra-backed and Databricks-native account groups take the
identical code path. `externalId` survives only as the reported `entra_backed` attribute, used to
word the remediation message when a group is missing from the target account, and as a fallback
lookup key if a group was renamed between regions.

**Why a wrong answer here is expensive (F6).** POSTing a group that is really an account group
creates a workspace-local SHADOW with the same name, and that shadow then *permanently blocks*
assigning the real account group (`PUT permissionassignments` → "Workspace group with name X
already exists"). Hence `NEEDS_REVIEW` when `resource_type` is absent: never guess.
"""
from __future__ import annotations

from enum import Enum

from src.utils.helpers import safe_str


class IdentityKind(str, Enum):
    ACCOUNT = "account"                  # exists at account level → ASSIGN, never create
    WORKSPACE_LOCAL = "workspace_local"  # workspace-scoped group → RECREATE on target
    SYSTEM = "system"                    # admins/users — always exist; membership only
    SYSTEM_GENERATED = "system_generated"  # Databricks-created artifact (e.g. `users-clone-…`) → SKIP
    NEEDS_REVIEW = "needs_review"        # group kind undetermined; operator must confirm


# Built-in workspace groups. They exist on every workspace, so they are never created — but their
# MEMBERSHIP does not carry over, so it is still migrated (a source workspace admin must stay one).
_SYSTEM_GROUPS = {"admins", "users"}

# meta.resourceType values, as returned by workspace SCIM GET /Groups.
_RT_WORKSPACE = "workspacegroup"
_RT_ACCOUNT = "group"


def is_system_generated_group(group: dict) -> bool:
    """A group Databricks created for its own bookkeeping, which must NOT be migrated (IMP-7a).

    When identity federation / SCIM is (re)configured, Databricks mints internal groups named like
    `users-clone-2026-08-06-2010-UTC (created by Databricks)`. They exist only on the source as a
    platform artifact; recreating one on target makes a meaningless workspace-local group (and its
    members resolve poorly since it mirrors the built-in `users`). Detected by the explicit
    "(created by Databricks)" marker or the `<name>-clone-<UTC timestamp>` shape.
    """
    name = safe_str(group.get("displayName"))
    if "(created by databricks)" in name.lower():
        return True
    import re
    # e.g. "users-clone-2026-08-06-2010-UTC"
    return bool(re.search(r"-clone-\d{4}-\d{2}-\d{2}-\d{4}-utc", name.lower()))


def is_entra_backed(obj: dict) -> bool:
    """Whether an identity came from Entra/SCIM provisioning (reporting only — never a code path)."""
    return bool(safe_str(obj.get("externalId")).strip())


def classify_group(group: dict) -> IdentityKind:
    """Workspace-local vs account vs system vs system-generated, from name + `meta.resourceType`."""
    if is_system_generated_group(group):
        return IdentityKind.SYSTEM_GENERATED
    if safe_str(group.get("displayName")) in _SYSTEM_GROUPS:
        return IdentityKind.SYSTEM
    resource_type = safe_str(group.get("resource_type")).strip().lower()
    if resource_type == _RT_WORKSPACE:
        return IdentityKind.WORKSPACE_LOCAL
    if resource_type == _RT_ACCOUNT:
        return IdentityKind.ACCOUNT
    # No resourceType (old bundle, or a workspace version that omits it). Guessing either way is
    # unsafe: guess ACCOUNT and a real workspace-local group is never recreated; guess
    # WORKSPACE_LOCAL and we shadow an account group, blocking it permanently (F6). Escalate.
    return IdentityKind.NEEDS_REVIEW


def classify_identity(obj: dict) -> IdentityKind:
    """Dispatch on `identity_type`. Users and SPs are ALWAYS account principals (F3)."""
    identity_type = obj.get("identity_type")
    if identity_type in ("user", "service_principal"):
        return IdentityKind.ACCOUNT
    if identity_type == "group":
        return classify_group(obj)
    return IdentityKind.NEEDS_REVIEW


def classify_all(identities: list[dict]) -> list[dict]:
    """Annotate each identity in place with `kind` + `entra_backed`; return the list."""
    for obj in identities:
        obj["kind"] = classify_identity(obj).value
        obj["entra_backed"] = is_entra_backed(obj)
    return identities


def classification_summary(identities: list[dict]) -> dict:
    """Count identities per kind, for the report + go/no-go gate."""
    out: dict = {}
    for obj in identities:
        kind = obj.get("kind", IdentityKind.NEEDS_REVIEW.value)
        out[kind] = out.get(kind, 0) + 1
    return out


def needs_account_action(identities: list[dict]) -> list[dict]:
    """Account groups — the ONLY identities that can require a human (Plan 6 §1 rows 6/7).

    Users, SPs and workspace-local groups are all handled automatically by the importer, so this
    list is the entire account-admin / Entra-IT worklist. Emitted by inventory so the gap is known
    BEFORE import runs rather than discovered as a failure mid-run.
    """
    out = []
    for obj in identities:
        if obj.get("identity_type") != "group" or obj.get("kind") != IdentityKind.ACCOUNT.value:
            continue
        out.append({
            "displayName": safe_str(obj.get("displayName")),
            "externalId": safe_str(obj.get("externalId")),
            "entra_backed": bool(obj.get("entra_backed")),
            "workspace_permissions": obj.get("workspace_permissions"),
            "required_action": (
                "Entra SCIM must provision this group into the TARGET account (customer IT)"
                if obj.get("entra_backed") else
                "an account admin must ensure this account group exists in the TARGET account"),
        })
    return out
