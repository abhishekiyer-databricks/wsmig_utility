"""
fixtures_medium — populate a SOURCE workspace with a MEDIUM-size reusable test fixture covering
every inventory asset type, all code paths for the waves, and edge cases. For PLAN 16 QA + future
testing of the workspace migration utility.

Idempotent: uses stable `wsmig_test*` names/paths; re-running skips or overwrites where the API
allows. Writes ONLY to the source workspace. Run phases individually or all:
    python3 tests/fixtures_medium.py [phase ...|all|check|--plan]
where phase ∈ identity compute warehouses uc workspace sql genie dashboards jobs dlt serving
            misc akv dab dab_pathless bigfiles libraries b3_policy_family b7_dashboards acls
            orphaned_users incremental

Everything is generic: the profile, catalog, identity and Azure coordinates are resolved at
runtime (or overridden by env), so the same file populates any workspace pair.
    WSMIG_PROFILE        databrickscfg profile for the source WORKSPACE   (default source_ws)
    WSMIG_ACCT_PROFILE   databrickscfg profile for the ACCOUNT console    (default source_acct)
    WSMIG_CATALOG        UC catalog for fixture tables    (default: the workspace default catalog)
    WSMIG_TARGET_PROFILE target workspace profile (for --plan, phasing; default target_ws)
    WSMIG_TARGET_CATALOG target UC catalog         (for --plan, phasing; default same as WSMIG_CATALOG)
    WSMIG_CLI            databricks CLI for bundle deploys (needs >= 1.5.0)
    WSMIG_USERS          comma-separated Entra emails for 15 test users (default: 15 Databricks emails)

Identity spec (all account-level, no workspace-local):
    • 15 Entra/SCIM users from WSMIG_USERS (default list)
    • 3 Databricks account SPs: ai27_acc_spn_1, ai27_acc_spn_2, ai27_acc_spn_3
    • 2 Azure UMI-backed SPs: ai27_umi_1, ai27_umi_2 (looked up, not created)
    • 2 Databricks account groups: ai27_account_grp_1, ai27_account_grp_2 (grp_2 nested in grp_1)
    • 2 Entra groups: ai27_entragrp_1, ai27_entragrp_2 (looked up, not created)
"""
from __future__ import annotations

import base64
import functools
import json
import os
import subprocess
import sys
import time

from databricks.sdk import WorkspaceClient

PROFILE = os.environ.get("WSMIG_PROFILE", "source_ws")
ACCT_PROFILE = os.environ.get("WSMIG_ACCT_PROFILE", "source_acct")
TARGET_PROFILE = os.environ.get("WSMIG_TARGET_PROFILE", "target_ws")
SCHEMA = "wsmig_test"

# 15 Entra/SCIM-provisioned test users from WSMIG_USERS. Default: 15 real Databricks emails.
_DEFAULT_USERS = (
    # required by the user (2026-10-04)
    "abhishek.iyer@databricks.com,tanveer.singh@databricks.com,sanket.kelkar@databricks.com,"
    "aman.bansal@databricks.com,vivek.ravichandiran@databricks.com,"
    # 10 more existing account users (any are fine; the 2 orphans are picked from these)
    "aakanksha.mishra@databricks.com,aakash.khosla@databricks.com,aakrati.talati@databricks.com,"
    "aaron.chong@databricks.com,aaron.newman@databricks.com,aaron.pan@databricks.com,"
    "aaron.vinsky@databricks.com,aarron.hecksher@databricks.com,aarti.budhiraja@databricks.com,"
    "aaryan.jain@databricks.com")
ENTRA_USERS = [u.strip() for u in os.environ.get("WSMIG_USERS", _DEFAULT_USERS).split(",") if u.strip()]
# Truncate to 15 if env supplies more
ENTRA_USERS = ENTRA_USERS[:15]
# Orphaned users (PLAN 9 path): 2 of the 10 "extra" users, never one of the 5 required ones.
ORPHAN_USERS = [ENTRA_USERS[11], ENTRA_USERS[13]] if len(ENTRA_USERS) > 13 else []
# Real Entra groups are looked up live (az ad group show); no hard-coded objectIds.
REAL_ENTRA_GROUPS: dict = {}

# The customer's SPs are ALL account-level (ZERO workspace-local SPs). An account SP is a
# Databricks-managed service principal at the ACCOUNT (stable applicationId, no Azure/Entra backing,
# no externalId) ASSIGNED to the workspace with the SAME appId. 3 account SPs: one is workspace-admin.
ACCOUNT_SP_NAMES = ["ai27_acc_spn_1", "ai27_acc_spn_2", "ai27_acc_spn_3"]
ACCOUNT_SP_ENTS = {
    "ai27_acc_spn_1": ["allow-cluster-create"],
    "ai27_acc_spn_2": ["databricks-sql-access", "allow-instance-pool-create"],
    "ai27_acc_spn_3": ["workspace-access"],  # 3rd SP
}

# UMI-backed SPs: look up via `az identity list`, if missing → FLAG (user creates them).
UMI_SP_NAMES = ["ai27_umi_1", "ai27_umi_2"]

# Entra-backed account groups (looked up, not created; if missing → FLAG).
ENTRA_GROUP_NAMES = ["ai27_entragrp_1", "ai27_entragrp_2"]

# Account-level DB-managed groups (no externalId): ai27_account_grp_2 nested in ai27_account_grp_1.
ACCOUNT_GROUP_NAMES = ["ai27_account_grp_1", "ai27_account_grp_2"]
ACCOUNT_NESTED_PARENT = "ai27_account_grp_1"
ACCOUNT_NESTED_CHILD = "ai27_account_grp_2"

w = WorkspaceClient(profile=PROFILE)


def log(msg):
    print(f"  {msg}", flush=True)


@functools.lru_cache(maxsize=1)
def _me() -> str:
    """The running user — fixture paths live under their home dir."""
    return w.current_user.me().user_name


@functools.lru_cache(maxsize=1)
def _catalog() -> str:
    """The workspace's own default catalog.

    CREATE CATALOG fails on these workspaces ("Default Storage is enabled"), so fixture tables
    go in the pre-provisioned default catalog rather than one we make.
    """
    if os.environ.get("WSMIG_CATALOG"):
        return os.environ["WSMIG_CATALOG"]
    return w.metastores.current().default_catalog_name


# Resolved once, from the live workspace, so the phase bodies below can use them as plain
# constants exactly as they did when these were hardcoded literals.
ME = _me()
CATALOG = _catalog()
SHARED = "/Shared/wsmig_test"
USERDIR = f"/Users/{ME}/wsmig_test"


# ─────────────────────────── identity ──────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _acct():
    """Account console client — needed for anything Entra-backed.

    The WORKSPACE SCIM API silently DROPS `externalId` on create, so an Entra-backed group or SP
    can ONLY be made at account level and then assigned into the workspace.
    """
    from databricks.sdk import AccountClient
    return AccountClient(profile=ACCT_PROFILE)


def _assigned_principal_ids() -> set:
    """Principal ids currently assigned to this workspace (workspace-scoped read)."""
    doc = w.api_client.do("GET", "/api/2.0/preview/permissionassignments") or {}
    return {str((pa.get("principal") or {}).get("principal_id"))
            for pa in doc.get("permission_assignments", []) or []}


def _assign_to_workspace(principal_id, permissions=("USER",)):
    """Assign an account-level identity to this workspace.

    Uses the WORKSPACE-scoped PermissionAssignments route rather than the account-scoped
    `/accounts/{id}/workspaces/{ws}/...` one: the latter 404s for a workspace-admin token, while
    this one works with the ambient workspace credentials we already have.
    """
    # NEVER downgrade an existing assignment (e.g. the runner's own ADMIN): a PUT REPLACES the
    # permission set, so keep whatever the principal already has and only ADD what we want.
    current: set = set()
    try:
        doc = w.api_client.do("GET", "/api/2.0/preview/permissionassignments") or {}
        for pa in doc.get("permission_assignments", []) or []:
            if str((pa.get("principal") or {}).get("principal_id")) == str(principal_id):
                current = set(pa.get("permissions") or [])
                break
    except Exception as e:
        log(f"  read current assignment for {principal_id}: {str(e)[:90]}")
    wanted = current | set(permissions)
    if wanted == current:
        return None   # already assigned with at least these permissions — nothing to change
    return w.api_client.do(
        "PUT", f"/api/2.0/preview/permissionassignments/principals/{principal_id}",
        body={"permissions": sorted(wanted)})


def _aad_object_id(name: str) -> tuple[str, str]:
    """An externalId to back an Entra-classified Databricks group.

    Uses an AAD group of our OWN if we can make one, otherwise a **synthetic uuid**.

    NEVER borrow a real pre-existing AAD group's objectId. Tried that on 2026-08-06 and it did
    real damage: Entra SCIM recognises the objectId as a group it manages, takes ownership of the
    Databricks group and RENAMES it to the real group's name — so `wsmig_test_entra_grp` silently
    became someone else's `aso_ril_1` demo group, entangling fixtures with live directory objects.
    (No Azure-side harm — we only ever create Databricks-side objects — but the fixture is gone
    and a stranger's group appears in the workspace.)

    A synthetic uuid is what we want anyway: the collector classifies on the PRESENCE of
    `externalId`, not on what it resolves to, so this exercises the identical Entra/SCIM path with
    nothing for a connector to claim. Creating our own group needs Graph group-create rights,
    which a GUEST (`#EXT#`) account in the tenant does not have (`Authorization_RequestDenied`).
    """
    import uuid

    # Preferred: a REAL Entra security group the customer pre-created for this fixture. Its genuine
    # objectId makes the Databricks group truly Entra-backed (the account console then shows it as a
    # provisioned group, not "removed from identity provider"). These are groups made FOR us, so —
    # unlike borrowing a stranger's group — there is nothing to accidentally hijack.
    if name in REAL_ENTRA_GROUPS:
        return REAL_ENTRA_GROUPS[name], "real Entra group (customer-provided objectId)"

    r = _az("ad", "group", "show", "--group", name, "-o", "json")
    if not r.returncode:
        return json.loads(r.stdout)["id"], "own AAD group"

    r = _az("ad", "group", "create", "--display-name", name,
            "--mail-nickname", name.replace("_", "-"), "-o", "json")
    if not r.returncode:
        return json.loads(r.stdout)["id"], "own AAD group (created)"

    # Deterministic per name, so a re-run reuses the same externalId instead of churning it.
    synthetic = str(uuid.uuid5(uuid.NAMESPACE_URL, f"wsmig-fixture-entra-group/{name}"))
    return synthetic, "synthetic uuid (no AAD create rights; never a borrowed real group)"


def _entra_group(name: str, members: list[str] | None = None) -> str | None:
    """An Entra-backed Databricks group.

    The collector classifies a group as Entra/SCIM-managed by the presence of `externalId`, and
    the WORKSPACE SCIM API silently drops it — so this has to be an ACCOUNT group carrying an
    externalId, then assigned into the workspace.

    HEALS a stale externalId: an older run may have made this group with a *synthetic* uuid (the
    account console flags those "removed from identity provider"). If the desired objectId is now a
    REAL Entra group and the existing one differs, the group is deleted + recreated so it becomes
    genuinely Entra-backed. Members (account ids) are set on create and PATCHed in on re-run.
    """
    from databricks.sdk.service import iam
    object_id, provenance = _aad_object_id(name)
    members = list(members or [])
    log(f"entra group {name}: externalId={object_id} ({provenance})")

    a = _acct()
    # Match on externalId as well as displayName: if a connector ever renames our group, looking
    # up by name alone would miss it and create a duplicate on every re-run.
    existing = next(iter(a.groups.list(filter=f'displayName eq "{name}"')), None)
    if existing is None:
        existing = next((g for g in a.groups.list() if g.external_id == object_id), None)
        if existing is not None:
            log(f"  found by externalId under a DIFFERENT name: {existing.display_name!r} "
                f"— a SCIM connector has claimed it")
    if existing and existing.external_id and existing.external_id != object_id:
        # stale/synthetic externalId → convert to the real one (recreate; externalId is immutable)
        log(f"  healing stale externalId {existing.external_id} → {object_id} (recreate)")
        a.groups.delete(id=existing.id)
        existing = None
    if existing:
        log(f"account group exists: {existing.display_name!r} "
            f"(id={existing.id}, ext={existing.external_id})")
        gid = existing.id
        have = {m.value for m in (existing.members or [])}
        missing = [m for m in members if m not in have]
        if missing:
            a.groups.patch(id=gid,
                           schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                           operations=[iam.Patch(op=iam.PatchOp.ADD, path="members",
                                                 value=[{"value": m} for m in missing])])
            log(f"  +{len(missing)} members added")
    else:
        g = a.groups.create(display_name=name, external_id=object_id,
                            members=[iam.ComplexValue(value=m) for m in members])
        log(f"account group created: {name} (id={g.id}, ext={object_id}, members={len(members)})")
        gid = g.id
    try:
        _assign_to_workspace(gid)
        log(f"  + assigned to workspace: {name}")
    except Exception as e:
        log(f"  assign {name}: {str(e)[:110]}")
    return gid


def _account_sp(name: str, entitlements: list[str] | None = None) -> str | None:
    """A Databricks ACCOUNT-level service principal (stable appId, NO Azure/Entra backing, no
    externalId) — the customer's SP model. The "account identity → assign to the workspace with the
    SAME appId, never recreate" path (as opposed to a workspace-local SP, which the tool recreates +
    remaps). Create-or-adopt at the ACCOUNT level (account-admin only — no Azure RBAC, no UMI), then
    assign to the workspace + set workspace-scoped entitlements. Fully re-creatable here across a
    cleanup; the same account SP assigned to BOTH source and target keeps one appId end to end.
    """
    from databricks.sdk.service import iam
    a = _acct()
    acct_sp = next(iter(a.service_principals.list(filter=f'displayName eq "{name}"')), None)
    if acct_sp:
        log(f"account SP exists: {name} (appId={acct_sp.application_id})")
    else:
        acct_sp = a.service_principals.create(display_name=name, active=True)
        log(f"account SP created: {name} (appId={acct_sp.application_id})")
    try:
        _assign_to_workspace(acct_sp.id)
        log(f"  + assigned to workspace: {name}")
    except Exception as e:
        log(f"  assign {name}: {str(e)[:110]}")
    if entitlements:
        ws_sp = next(iter(w.service_principals.list(filter=f'displayName eq "{name}"')), None)
        if ws_sp:
            try:
                w.service_principals.patch(
                    id=ws_sp.id,
                    schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                    operations=[iam.Patch(op=iam.PatchOp.ADD, path="entitlements",
                                          value=[{"value": e} for e in entitlements])])
                log(f"  entitlements on {name}: {', '.join(entitlements)}")
            except Exception as e:
                log(f"  entitlements {name}: {str(e)[:90]}")
    return acct_sp.id


def _account_group(name: str, members: list[str] | None = None) -> str | None:
    """A genuine ACCOUNT-level (Databricks-managed, NO externalId) group assigned to the workspace.

    Distinct from Entra-backed (externalId) and workspace-local (recreate+remap): the importer's
    job is to ASSIGN it to the target WS, never recreate it. Members are account-level ids.
    """
    from databricks.sdk.service import iam
    members = list(members or [])
    a = _acct()
    existing = next(iter(a.groups.list(filter=f'displayName eq "{name}"')), None)
    if existing:
        gid = existing.id
        have = {m.value for m in (existing.members or [])}
        missing = [m for m in members if m not in have]
        if missing:
            a.groups.patch(id=gid,
                           schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                           operations=[iam.Patch(op=iam.PatchOp.ADD, path="members",
                                                 value=[{"value": m} for m in missing])])
        log(f"account group exists: {name} (id={gid}, +{len(missing)} members)")
    else:
        g = a.groups.create(display_name=name,
                            members=[iam.ComplexValue(value=m) for m in members])
        gid = g.id
        log(f"account group created: {name} (id={gid}, no externalId, members={len(members)})")
    try:
        _assign_to_workspace(gid)
        log(f"  + assigned to workspace: {name}")
    except Exception as e:
        log(f"  assign {name}: {str(e)[:110]}")
    return gid


def _acct_user_ids(emails: list[str]) -> list[str]:
    """Resolve account-level user ids for the given emails (members for account/Entra groups)."""
    a = _acct()
    ids = []
    for em in emails:
        u = next(iter(a.users.list(filter=f'userName eq "{em}"')), None)
        if u:
            ids.append(u.id)
    return ids


def phase_identity():
    from databricks.sdk.service import iam
    print("== identity ==")
    a = _acct()
    flags = []

    # 1. 15 Entra/SCIM users — ACCOUNT identities, ASSIGN to workspace with varied entitlements.
    for i, email in enumerate(ENTRA_USERS):
        if email == ME:
            log(f"user {email}: the runner — assignment left untouched (keeps ADMIN)")
            continue
        acct_user = next(iter(a.users.list(filter=f'userName eq "{email}"')), None)
        if not acct_user:
            flags.append(f"user {email}: NOT in account (needs Entra/SCIM provisioning)")
            log(f"FLAG: {flags[-1]}")
            continue
        try:
            _assign_to_workspace(acct_user.id)
            log(f"user assigned: {email} (ext={acct_user.external_id})")
        except Exception as e:
            log(f"user {email}: {str(e)[:90]}")

    # Entitlements: spread across the 15 users to exercise the path
    ent_patterns = [
        ["allow-cluster-create"],
        ["databricks-sql-access"],
        ["workspace-access", "databricks-sql-access", "allow-instance-pool-create"],
        ["allow-cluster-create", "allow-instance-pool-create"],
        ["workspace-access"],
    ]
    for i, email in enumerate(ENTRA_USERS):
        if email == ME:
            continue   # never change the runner's own entitlements
        ws_user = next(iter(w.users.list(filter=f'userName eq "{email}"')), None)
        if not ws_user:
            continue
        ents = ent_patterns[i % len(ent_patterns)]
        try:
            w.users.patch(
                id=ws_user.id,
                schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                operations=[iam.Patch(op=iam.PatchOp.ADD, path="entitlements",
                                      value=[{"value": e} for e in ents])])
            log(f"  entitlements on {email}: {', '.join(ents)}")
        except Exception as e:
            log(f"  entitlements {email}: {str(e)[:90]}")

    # 2. UMI-backed SPs (ai27_umi_1, ai27_umi_2) — looked up via az identity list, NOT created.
    #    If missing → FLAG, but don't fail the whole phase.
    # Preferred (user 2026-10-04): the UMI SPs are already added to the account AND assigned to the
    # workspace by the user — adopt the ASSIGNED account SP by displayName, no Azure call. (Stale
    # same-named SPs from a deleted workspace may exist in the account; the assigned one wins.)
    assigned_ids = _assigned_principal_ids()
    pending_umis = []
    for umi_name in UMI_SP_NAMES:
        cands = list(a.service_principals.list(filter=f'displayName eq "{umi_name}"'))
        pick = next((s for s in cands if str(s.id) in assigned_ids), None)
        if pick:
            log(f"UMI SP adopted (already in account + workspace): {umi_name} "
                f"(appId={pick.application_id})")
            if len(cands) > 1:
                flags.append(f"UMI {umi_name}: {len(cands) - 1} stale same-named account SP(s) "
                             f"not assigned to this workspace (ignored)")
                log(f"FLAG: {flags[-1]}")
        else:
            pending_umis.append(umi_name)
    subs = []
    if pending_umis:
        r = _az("account", "list", "--query", "[].id", "-o", "json")
        if not r.returncode:
            subs = json.loads(r.stdout) or []
    for umi_name in pending_umis:
        umi = None
        for sub in subs or [None]:
            args = ["identity", "list", "--query", f"[?name=='{umi_name}']", "-o", "json"]
            if sub:
                args += ["--subscription", sub]
            r = _az(*args)
            if not r.returncode and json.loads(r.stdout or "[]"):
                umi = json.loads(r.stdout)[0]
                break
        if not umi or not umi.get("clientId"):
            flags.append(f"UMI {umi_name}: NOT found in any subscription (user must create the Azure UMI)")
            log(f"FLAG: {flags[-1]}")
            continue
        app_id = umi["clientId"]
        try:
            # service_principals.create does NOT dedupe — look up by applicationId first.
            sp = next(iter(a.service_principals.list(filter=f'applicationId eq "{app_id}"')), None)
            if sp:
                log(f"UMI SP exists in account: {umi_name} (appId={app_id})")
            else:
                sp = a.service_principals.create(application_id=app_id, display_name=umi_name,
                                                 active=True)
                log(f"UMI SP added to account: {umi_name} (appId={app_id})")
            _assign_to_workspace(sp.id)
            log(f"  + assigned to workspace: {umi_name}")
        except Exception as e:
            log(f"UMI {umi_name}: {str(e)[:120]}")

    # 3. Entra-backed groups (ai27_entragrp_1, ai27_entragrp_2) — looked up via az ad group show.
    #    If missing → FLAG; provision to account with externalId, assign to workspace.
    for eg in ENTRA_GROUP_NAMES:
        # Preferred (user 2026-10-04): the user already added the Entra group to the account and
        # the workspace → adopt it as-is, no Azure call, no membership edits (Entra owns it).
        pre = next(iter(a.groups.list(filter=f'displayName eq "{eg}"')), None)
        if pre and str(pre.id) in assigned_ids:
            log(f"Entra group adopted (already in account + workspace): {eg} "
                f"(externalId={pre.external_id})")
            continue
        r = _az("ad", "group", "show", "--group", eg, "-o", "json")
        if r.returncode:
            flags.append(f"Entra group {eg}: NOT found (user must create Azure Entra group)")
            log(f"FLAG: {flags[-1]}")
            continue
        aad_obj = json.loads(r.stdout)
        ext_id = aad_obj.get("id")
        members = _acct_user_ids(ENTRA_USERS[3:6])  # 3 users per Entra group
        try:
            # Create or update the account group with the externalId (Entra-backed)
            existing = next(iter(a.groups.list(filter=f'displayName eq "{eg}"')), None)
            if existing and existing.external_id != ext_id:
                # Never delete an account group (it may be SCIM-provisioned / in use). Adopt + FLAG.
                flags.append(f"Entra group {eg}: account group exists with externalId="
                             f"{existing.external_id}, Entra objectId={ext_id} (adopted as-is)")
                log(f"FLAG: {flags[-1]}")
            if not existing:
                g = a.groups.create(display_name=eg, external_id=ext_id,
                                   members=[iam.ComplexValue(value=m) for m in members])
                log(f"Entra group {eg}: created (ext={ext_id}, members={len(members)})")
            else:
                log(f"Entra group {eg}: exists (ext={ext_id})")
            gid = existing.id if existing else g.id
            _assign_to_workspace(gid)
            log(f"  + assigned to workspace: {eg}")
        except Exception as e:
            log(f"Entra group {eg}: {str(e)[:90]}")

    # 4. Account-level DB-managed groups: ai27_account_grp_1, ai27_account_grp_2 (grp_2 nested in grp_1).
    grp_ids = {}
    for grp_name in ACCOUNT_GROUP_NAMES:
        members = _acct_user_ids(ENTRA_USERS[1:3])  # 2nd and 3rd users as members
        gid = _account_group(grp_name, members=members)
        if gid:
            grp_ids[grp_name] = gid

    # Nesting: grp_2 nested into grp_1
    if ACCOUNT_NESTED_PARENT in grp_ids and ACCOUNT_NESTED_CHILD in grp_ids:
        try:
            parent = next(iter(a.groups.list(filter=f'displayName eq "{ACCOUNT_NESTED_PARENT}"')), None)
            if parent:
                have = {m.value for m in (parent.members or [])}
                if grp_ids[ACCOUNT_NESTED_CHILD] not in have:
                    a.groups.patch(
                        id=parent.id,
                        schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                        operations=[iam.Patch(op=iam.PatchOp.ADD, path="members",
                                             value=[{"value": grp_ids[ACCOUNT_NESTED_CHILD]}])])
                    log(f"  nesting: {ACCOUNT_NESTED_CHILD} added to {ACCOUNT_NESTED_PARENT}")
        except Exception as e:
            log(f"nesting {ACCOUNT_NESTED_PARENT}/{ACCOUNT_NESTED_CHILD}: {str(e)[:90]}")

    # 5. Account-level SPs (ai27_acc_spn_1, ai27_acc_spn_2, ai27_acc_spn_3) — create-or-adopt.
    sp_ids = []
    for sp_name in ACCOUNT_SP_NAMES:
        sid = _account_sp(sp_name, entitlements=ACCOUNT_SP_ENTS.get(sp_name, ["workspace-access"]))
        if sid:
            sp_ids.append(sid)
    if sp_ids:
        try:
            _account_group(ACCOUNT_NESTED_PARENT, members=sp_ids[:2])   # SP members on grp_1
        except Exception as e:
            log(f"grp_1 SP members: {str(e)[:90]}")
    # one account SP is a workspace admin (spec)
    try:
        sp = next(iter(w.service_principals.list(filter=f'displayName eq "{ACCOUNT_SP_NAMES[0]}"')), None)
        adm = next(iter(w.groups.list(filter='displayName eq "admins"')), None)
        if sp and adm:
            w.groups.patch(id=adm.id,
                           schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                           operations=[iam.Patch(op=iam.PatchOp.ADD, path="members",
                                                 value=[{"value": sp.id}])])
            log(f"  {ACCOUNT_SP_NAMES[0]} added to workspace admins")
    except Exception as e:
        log(f"SP admin: {str(e)[:90]}")

    if flags:
        log(f"\n  FLAGS: {len(flags)} items need attention (see above)")


# ─────────────────────────── compute ───────────────────────────────────────

NODE = "Standard_DS3_v2"
SPARK_VERSION = "16.4.x-scala2.12"


def phase_compute():
    print("== compute ==")
    node = NODE

    # Instance pools. None of these creates dedupe on name, so look first — a re-run would
    # otherwise pile up duplicates the way the SP create did.
    have_pools = {p.instance_pool_name: p.instance_pool_id for p in w.instance_pools.list()}
    pools = {
        # the ordinary pool
        "wsmig_test_pool": {"min_idle_instances": 0, "max_capacity": 2},
        # a pool capped at a SINGLE instance — max_capacity=1 is its own edge case
        "wsmig_test_pool_single": {"min_idle_instances": 1, "max_capacity": 1},
        # a pool with NO max_capacity at all (unbounded), so the field is absent from the payload
        "wsmig_test_pool_nomax": {"min_idle_instances": 0},
    }
    for pname, spec in pools.items():
        if pname in have_pools:
            log(f"instance pool exists: {pname} ({have_pools[pname]})")
            continue
        try:
            p = w.instance_pools.create(instance_pool_name=pname, node_type_id=node, **spec)
            log(f"instance pool: {pname} ({p.instance_pool_id}) {spec}")
        except Exception as e:
            log(f"pool {pname}: {str(e)[:90]}")

    # Cluster policies — a permissive one and a restrictive one (so definitions differ
    # meaningfully and a policy EDIT on re-export is detectable).
    have_pol = {p.name for p in w.cluster_policies.list()}
    policies = {
        "wsmig_test_policy": {"node_type_id": {"type": "allowlist", "values": [node]},
                              "spark_version": {"type": "regex", "pattern": ".*"}},
        "wsmig_test_policy_strict": {
            "node_type_id": {"type": "fixed", "value": node},
            "num_workers": {"type": "range", "minValue": 1, "maxValue": 4},
            "autotermination_minutes": {"type": "fixed", "value": 20},
        },
    }
    for pol_name, definition in policies.items():
        if pol_name in have_pol:
            log(f"cluster policy exists: {pol_name}")
            continue
        try:
            pol = w.cluster_policies.create(name=pol_name, definition=json.dumps(definition))
            log(f"cluster policy: {pol_name} ({pol.policy_id})")
        except Exception as e:
            log(f"policy {pol_name}: {str(e)[:90]}")

    # All-purpose clusters. Created over raw REST (not clusters.create().result) so we don't
    # block waiting for a cluster to actually come up — export only needs the config to exist.
    have_clusters = {c.cluster_name: c.cluster_id for c in w.clusters.list()}
    pool_id = {p.instance_pool_name: p.instance_pool_id for p in w.instance_pools.list()}
    policy_id = {p.name: p.policy_id for p in w.cluster_policies.list()}
    clusters = {
        # plain cluster
        "wsmig_test_cluster": {"spark_version": SPARK_VERSION, "node_type_id": node,
                               "num_workers": 1, "autotermination_minutes": 10},
        # autoscaling + custom spark conf/env/tags — more of the config surface to strip+replay
        "wsmig_test_cluster_autoscale": {
            "spark_version": SPARK_VERSION, "node_type_id": node,
            "autoscale": {"min_workers": 1, "max_workers": 3},
            "autotermination_minutes": 15,
            "spark_conf": {"spark.sql.shuffle.partitions": "8"},
            "spark_env_vars": {"WSMIG_TEST": "1"},
            "custom_tags": {"wsmig_purpose": "fixture"},
        },
        # single-node cluster — the num_workers=0 + special conf/tag shape
        "wsmig_test_cluster_singlenode": {
            "spark_version": SPARK_VERSION, "node_type_id": node, "num_workers": 0,
            "autotermination_minutes": 10,
            "spark_conf": {"spark.master": "local[*]",
                           "spark.databricks.cluster.profile": "singleNode"},
            "custom_tags": {"ResourceClass": "SingleNode"},
        },
    }
    # a cluster that draws from a pool AND is governed by a policy — cross-references that the
    # importer has to remap to the NEW target pool/policy ids
    if "wsmig_test_pool" in pool_id:
        clusters["wsmig_test_cluster_pooled"] = {
            "spark_version": SPARK_VERSION, "num_workers": 1,
            "instance_pool_id": pool_id["wsmig_test_pool"],
            "autotermination_minutes": 10,
        }
    if "wsmig_test_policy" in policy_id:
        clusters["wsmig_test_cluster_policied"] = {
            "spark_version": SPARK_VERSION, "node_type_id": node, "num_workers": 1,
            "policy_id": policy_id["wsmig_test_policy"], "autotermination_minutes": 10,
        }

    for cname, body in clusters.items():
        if cname in have_clusters:
            log(f"cluster exists: {cname} ({have_clusters[cname]})")
            continue
        try:
            resp = w.api_client.do("POST", "/api/2.0/clusters/create",
                                   body={"cluster_name": cname, **body})
            log(f"cluster: {cname} ({resp.get('cluster_id')})")
        except Exception as e:
            log(f"cluster {cname}: {str(e)[:120]}")


# ─────────────────────────── workspace content ─────────────────────────────

def phase_workspace():
    from databricks.sdk.service import workspace
    print("== workspace content ==")
    # A DEEP directory tree, not just one level: directory creation order matters on import, and
    # a nested empty dir is its own case (it has no children to imply it).
    for d in (SHARED, USERDIR, f"{SHARED}/sub", f"{SHARED}/sub/deeper",
              f"{SHARED}/sub/deeper/deepest", f"{SHARED}/empty_dir",
              f"{USERDIR}/sub", "/Shared/wsmig_test_space dir"):
        try:
            w.workspace.mkdirs(d)
        except Exception as e:
            log(f"mkdir {d}: {str(e)[:50]}")
    log("directory tree created (incl. nested, empty, and a space in the name)")

    # notebooks in each language → tests all SOURCE extensions
    nbs = {
        "PYTHON": ("py_nb", "# Databricks notebook source\nprint('hi from python')\n"),
        "SQL": ("sql_nb", "-- Databricks notebook source\nSELECT 1 AS x\n"),
        "SCALA": ("scala_nb", "// Databricks notebook source\nprintln(\"hi scala\")\n"),
        "R": ("r_nb", "# Databricks notebook source\nprint('hi from R')\n"),
    }
    for lang, (name, src) in nbs.items():
        for base_dir in (SHARED, USERDIR):
            path = f"{base_dir}/{name}"
            try:
                w.workspace.import_(path=path, language=getattr(workspace.Language, lang),
                                    format=workspace.ImportFormat.SOURCE,
                                    content=base64.b64encode(src.encode()).decode(),
                                    overwrite=True)
            except Exception as e:
                log(f"nb {path}: {str(e)[:60]}")
    log("notebooks (py/sql/scala/r) x (Shared+Users) created")

    # A notebook deep in the tree, and one with a space + unicode in its name — path handling on
    # export/import is a common breakage and neither case is covered by the flat set above.
    for path, src in ((f"{SHARED}/sub/deeper/deepest/nested_nb",
                       "# Databricks notebook source\nprint('deeply nested')\n"),
                      (f"{SHARED}/wsmig test spaced nb",
                       "# Databricks notebook source\nprint('spaced name')\n")):
        try:
            w.workspace.import_(path=path, language=workspace.Language.PYTHON,
                                format=workspace.ImportFormat.SOURCE,
                                content=base64.b64encode(src.encode()).decode(), overwrite=True)
            log(f"notebook created: {path}")
        except Exception as e:
            log(f"nb {path}: {str(e)[:70]}")

    # A JUPYTER-format notebook (.ipynb). Its on-disk format differs from SOURCE, so export has
    # to round-trip it as a distinct case rather than as a plain .py.
    ipynb = json.dumps({
        "cells": [{"cell_type": "code", "source": ["print('hi from jupyter')"],
                   "metadata": {}, "outputs": [], "execution_count": None}],
        "metadata": {"language_info": {"name": "python"}},
        "nbformat": 4, "nbformat_minor": 5,
    })
    try:
        w.workspace.import_(path=f"{SHARED}/jupyter_nb", language=workspace.Language.PYTHON,
                            format=workspace.ImportFormat.JUPYTER,
                            content=base64.b64encode(ipynb.encode()).decode(), overwrite=True)
        log("notebook created: jupyter (.ipynb) format")
    except Exception as e:
        log(f"jupyter nb: {str(e)[:90]}")

    # workspace files (non-notebook) — a spread of extensions AND an extensionless one, since
    # the collector decides notebook-vs-file partly on how the path looks.
    files = {f"{SHARED}/config.json": b'{"key":"value"}\n',
             f"{USERDIR}/data.csv": b"a,b,c\n1,2,3\n",
             f"{SHARED}/script.sh": b"#!/bin/bash\necho hi\n",
             f"{SHARED}/requirements.txt": b"requests==2.31.0\n",
             f"{SHARED}/README.md": b"# wsmig test\n",
             f"{SHARED}/sub/deeper/nested.yaml": b"key: value\n",
             f"{SHARED}/plain_no_extension": b"just bytes, no extension\n",
             f"{USERDIR}/binary.bin": bytes(range(256)),
             f"{SHARED}/wsmig test spaced file.txt": b"spaced file name\n"}
    for path, content in files.items():
        try:
            w.workspace.upload(path=path, content=content,
                               format=workspace.ImportFormat.RAW, overwrite=True)
        except Exception as e:
            log(f"file {path}: {str(e)[:60]}")
    log(f"workspace files created ({len(files)} incl. binary, extensionless, spaced, nested)")

    # >10MB FILE (edge case: file streaming works)
    big_path = f"{USERDIR}/wsmig_test_big_file.csv"
    try:
        w.api_client.do("POST", f"/api/2.0/workspace-files/import-file{big_path}",
                        query={"overwrite": "true"},
                        data=b"col1,col2\n" + b"x" * (11 * 1024 * 1024),
                        headers={"Content-Type": "application/octet-stream"})
        log("11MB file created (edge case)")
    except Exception as e:
        log(f"big file: {str(e)[:90]}")

    # >10MB NOTEBOOK edge case — PROVE it cannot be created as a notebook
    big_nb = "# Databricks notebook source\n" + "# " + "y" * 80 + "\n" * 1
    big_nb_content = "# Databricks notebook source\n" + ("# filler " + "y" * 80 + "\n") * 150000
    try:
        w.workspace.import_(path=f"{USERDIR}/wsmig_test_big_nb",
                            language=workspace.Language.PYTHON,
                            format=workspace.ImportFormat.SOURCE,
                            content=base64.b64encode(big_nb_content.encode()).decode(),
                            overwrite=True)
        log("!! big notebook import unexpectedly SUCCEEDED (size=%d)" % len(big_nb_content))
    except Exception as e:
        log(f">10MB notebook import correctly REJECTED: {str(e)[:80]}")

    # Object ACLs on workspace content are set in phase_acls, so that every object type's full
    # permission ladder is granted in one place rather than piecemeal per asset phase.

    # a Repo (git folder) — public repo, no creds needed to register.
    # Repos are inventory/export-only (out of scope for import), so this exists purely so the
    # collector has one to enumerate and so it can prove it never descends into the git folder.
    try:
        w.repos.create(url="https://github.com/databricks/databricks-sdk-py",
                       provider="gitHub", path=f"/Repos/{ME}/wsmig_test_repo")
        log("repo created")
    except Exception as e:
        log(f"repo: {str(e)[:80]}")


# ─────────────────────────── secrets ───────────────────────────────────────

def phase_secrets():
    """Databricks-backed secret scopes. (AKV-backed lives in phase_akv; DAB-owned in
    phase_dab_pathless.)

    Secret VALUES are never returned by any API, so only scope names + ACLs can migrate and the
    values are a manual re-populate on target, by design. Key COUNT still matters — the export
    records the key list — so this covers multi-key, single-key and zero-key scopes.
    """
    print("== secrets ==")
    scopes = {
        "wsmig_test_scope": [("api_key", "secret-value-1"), ("db_pass", "secret-value-2"),
                             ("token", "secret-value-3")],
        "wsmig_test_scope_single": [("only_key", "single-value")],
        # a scope with NO keys at all — the empty-scope case
        "wsmig_test_scope_empty": [],
    }
    existing = {s.name for s in (w.secrets.list_scopes() or [])}
    for scope, kvs in scopes.items():
        if scope in existing:
            log(f"secret scope exists: {scope}")
        else:
            try:
                w.secrets.create_scope(scope=scope)
                log(f"secret scope: {scope}")
            except Exception as e:
                log(f"scope {scope}: {str(e)[:70]}")
        for k, v in kvs:
            try:
                w.secrets.put_secret(scope=scope, key=k, string_value=v)
            except Exception as e:
                log(f"secret {scope}/{k}: {str(e)[:50]}")
        log(f"  {scope}: {len(kvs)} key(s) (values non-exportable by design)")


# ─────────────────────────── UC tables ─────────────────────────────────────

def phase_uc():
    """Tables the Genie space / dashboards / DLT pipelines reference.

    UC itself is out of migration scope; these exist only so those assets have something real to
    point at (their specs reference tables by FQN).
    """
    print("== uc tables (for genie/dashboard refs) ==")
    wh = _warehouse_id()
    stmts = [
        # the schema has to come first — without it every CREATE TABLE below fails
        f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}",
        f"CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.trips (zip STRING, trips INT, avg_dist DOUBLE)",
        f"INSERT OVERWRITE {CATALOG}.{SCHEMA}.trips VALUES ('94103', 120, 3.4), ('94107', 88, 2.1)",
        f"CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.zones (zip STRING, borough STRING)",
        f"INSERT OVERWRITE {CATALOG}.{SCHEMA}.zones VALUES ('94103','SF'), ('94107','SF')",
    ]
    for s in stmts:
        try:
            r = w.statement_execution.execute_statement(warehouse_id=wh, statement=s,
                                                        wait_timeout="30s")
            # A failed statement comes back as a FAILED *result*, not an exception, so the state
            # has to be checked explicitly or real errors get logged as "ok".
            if r.status.state.value != "SUCCEEDED":
                err = getattr(r.status.error, "message", None) or r.status.state.value
                log(f"sql FAILED: {s[:50]}… → {str(err)[:120]}")
            else:
                log(f"sql ok: {s[:55]}…")
        except Exception as e:
            log(f"sql err: {str(e)[:70]}")


def _warehouse_id():
    """A warehouse to run fixture SQL against — prefer a serverless/pro one so it starts fast."""
    whs = list(w.warehouses.list())
    for wh in whs:
        if getattr(wh, "enable_serverless_compute", False):
            return wh.id
    return whs[0].id if whs else None


# ─────────────────────────── SQL warehouses ────────────────────────────────

def phase_warehouses():
    """SQL warehouses across both types.

    The workspace ships with a serverless PRO "Starter" warehouse; these add an explicit
    non-serverless PRO and a CLASSIC one, so all three shapes exist (the DAB-owned CLASSIC twin
    comes from phase_dab_pathless). `warehouse_type` + `enable_serverless_compute` are the two
    fields that distinguish them and both have to survive export/import.
    """
    print("== sql warehouses ==")
    have = {wh.name for wh in w.warehouses.list()}
    warehouses = {
        "wsmig_test_wh_pro": {"warehouse_type": "PRO", "cluster_size": "2X-Small",
                              "enable_serverless_compute": False, "max_num_clusters": 1,
                              "auto_stop_mins": 10},
        "wsmig_test_wh_classic": {"warehouse_type": "CLASSIC", "cluster_size": "2X-Small",
                                  "enable_serverless_compute": False, "max_num_clusters": 2,
                                  "auto_stop_mins": 15},
        "wsmig_test_wh_serverless": {"warehouse_type": "PRO", "cluster_size": "2X-Small",
                                     "enable_serverless_compute": True, "max_num_clusters": 1,
                                     "auto_stop_mins": 5},
    }
    for name, body in warehouses.items():
        if name in have:
            log(f"warehouse exists: {name}")
            continue
        try:
            r = w.api_client.do("POST", "/api/2.0/sql/warehouses", body={"name": name, **body})
            log(f"warehouse: {name} ({r.get('id')}) "
                f"{body['warehouse_type']}/serverless={body['enable_serverless_compute']}")
        except Exception as e:
            log(f"warehouse {name}: {str(e)[:110]}")


# ─────────────────────────── SQL (queries + all alert types + legacy dash) ─

class _Exists(Exception):
    """Control flow: the object already exists → skip its create (idempotent re-run)."""


def phase_sql():
    print("== sql (queries, legacy alert, alerts v2, legacy dashboard) ==")
    wh = _warehouse_id()
    # Idempotency: NONE of these creates dedupe by name — a re-run silently makes timestamp-
    # suffixed duplicate queries/alerts — so look each one up first.
    have_q = {q.display_name for q in w.queries.list()}
    have_a2 = {a.display_name for a in w.alerts_v2.list_alerts()}
    have_la = {a.name for a in w.alerts_legacy.list()}
    # 1. Query via the current /api/2.0/sql/queries API (collector tags this legacy_query).
    qid = None
    try:
        from databricks.sdk.service.sql import CreateQueryRequestQuery
        if "wsmig_test_query" in have_q:
            raise _Exists("query exists: wsmig_test_query")
        q = w.queries.create(query=CreateQueryRequestQuery(
            display_name="wsmig_test_query", warehouse_id=wh,
            query_text=f"SELECT * FROM {CATALOG}.{SCHEMA}.trips"))
        qid = q.id
        log(f"query: wsmig_test_query ({qid})")
    except _Exists as e:
        log(str(e))
    except Exception as e:
        log(f"query: {str(e)[:90]}")

    # 2. Alerts V2 (/api/2.0/alerts) — the current alert surface.
    try:
        from databricks.sdk.service.sql import (AlertV2, AlertV2Evaluation, AlertV2OperandColumn,
                                                AlertV2Operand, AlertV2OperandValue,
                                                ComparisonOperator, CronSchedule)
        ev = AlertV2Evaluation(
            comparison_operator=ComparisonOperator.GREATER_THAN,
            source=AlertV2OperandColumn(name="c"),
            threshold=AlertV2Operand(value=AlertV2OperandValue(double_value=0)))
        av2 = AlertV2(display_name="wsmig_test_alert_v2", warehouse_id=wh,
                      query_text=f"SELECT count(*) AS c FROM {CATALOG}.{SCHEMA}.trips",
                      evaluation=ev,
                      schedule=CronSchedule(quartz_cron_schedule="0 0 9 * * ?",
                                            timezone_id="UTC"))
        if "wsmig_test_alert_v2" in have_a2:
            log("alert_v2 exists: wsmig_test_alert_v2")
        else:
            r = w.alerts_v2.create_alert(alert=av2)
            log(f"alert_v2: wsmig_test_alert_v2 ({getattr(r,'id',None)})")
        # A SECOND alert_v2 — different operator + UNSCHEDULED — alerts have been bug-prone
        # (alert_v2 update-detected-but-never-applied, object-type spellings), so cover >1 shape.
        ev2 = AlertV2Evaluation(
            comparison_operator=ComparisonOperator.LESS_THAN,
            source=AlertV2OperandColumn(name="c"),
            threshold=AlertV2Operand(value=AlertV2OperandValue(double_value=1000000)))
        av2b = AlertV2(display_name="wsmig_test_alert_v2_b", warehouse_id=wh,
                       query_text=f"SELECT count(*) AS c FROM {CATALOG}.{SCHEMA}.zones",
                       evaluation=ev2,
                       schedule=CronSchedule(quartz_cron_schedule="0 0 18 * * ?",
                                             timezone_id="UTC"))
        if "wsmig_test_alert_v2_b" in have_a2:
            log("alert_v2 exists: wsmig_test_alert_v2_b")
        else:
            rb = w.alerts_v2.create_alert(alert=av2b)
            log(f"alert_v2: wsmig_test_alert_v2_b ({getattr(rb,'id',None)})")
    except Exception as e:
        log(f"alert_v2: {str(e)[:140]}")

    # 3. Legacy alert (/api/2.0/sql/alerts family via alerts_legacy) — needs a legacy query.
    try:
        from databricks.sdk.service.sql import AlertOptions
        if "wsmig_test_legacy_alert" in have_la:
            raise _Exists("legacy_alert exists: wsmig_test_legacy_alert")
        lq = w.queries_legacy.create(name="wsmig_test_legacy_q", query="SELECT 1 AS v",
                                     data_source_id=_legacy_data_source_id(wh))
        la = w.alerts_legacy.create(name="wsmig_test_legacy_alert",
                                    query_id=lq.id,
                                    options=AlertOptions(column="v", op=">", value="0"))
        log(f"legacy_alert: wsmig_test_legacy_alert ({la.id})")
    except _Exists as e:
        log(str(e))
    except Exception as e:
        log(f"legacy_alert: {str(e)[:110]}")

    # 4. Legacy dashboard (redash /api/2.0/preview/sql/dashboards; `dashboard_filters_enabled`
    #    is required by the RPC).
    try:
        r = w.api_client.do("POST", "/api/2.0/preview/sql/dashboards",
                            body={"name": "wsmig_test_legacy_dashboard",
                                  "dashboard_filters_enabled": False,
                                  "is_draft": False})
        log(f"legacy_dashboard: wsmig_test_legacy_dashboard ({r.get('id')})")
    except Exception as e:
        log(f"legacy_dashboard: {str(e)[:110]}")


def _legacy_data_source_id(warehouse_id):
    """Legacy query needs a data_source_id (the redash id of a warehouse), not the warehouse id."""
    try:
        for ds in w.api_client.do("GET", "/api/2.0/preview/sql/data_sources") or []:
            if ds.get("warehouse_id") == warehouse_id:
                return ds.get("id")
        # fallback: first data source
        dss = w.api_client.do("GET", "/api/2.0/preview/sql/data_sources") or []
        return dss[0]["id"] if dss else None
    except Exception:
        return None


# ─────────────────────────── Genie space ───────────────────────────────────

def phase_genie():
    """Two Genie spaces, so the bed exercises more than one AND the ACL matrix grants on both
    (a single space couldn't prove the genie ACL remap repeats across spaces). A DAB-deployed Genie
    space is added separately by phase_dab (bundle `genie_spaces` resource)."""
    print("== genie spaces (2 directly-created) ==")
    wh = _warehouse_id()
    serialized = json.dumps({
        "version": 2,
        "data_sources": {"tables": [
            {"identifier": f"{CATALOG}.{SCHEMA}.trips"},
            {"identifier": f"{CATALOG}.{SCHEMA}.zones"}]},
    })
    existing = {}
    try:
        for sp in (w.genie.list_spaces().spaces or []):
            if sp.title:
                existing[sp.title] = sp.space_id
    except Exception as e:
        log(f"genie list: {str(e)[:80]}")
    for title, desc in (("wsmig_test_genie", "test genie space (trips + zones)"),
                        ("wsmig_test_genie2", "second genie space (multi-space + ACL coverage)")):
        if title in existing:
            log(f"genie space exists: {title} ({existing[title]})")
            continue
        try:
            r = w.genie.create_space(warehouse_id=wh, serialized_space=serialized,
                                     title=title, description=desc)
            log(f"genie space: {title} ({getattr(r, 'space_id', None)})")
        except Exception as e:
            log(f"genie {title}: {str(e)[:150]}")


# ─────────────────────────── Lakeview (AI/BI) dashboard ────────────────────

def _active_dashboards() -> dict:
    """`{display_name: dashboard_id}` for NON-trashed Lakeview dashboards.

    `lakeview.list()` returns TRASHED dashboards too, so a plain name match would treat a trashed
    dashboard as "exists" and (a) skip recreating it and (b) fail any publish/schedule on the dead
    id. Filter them out so re-creating after a cleanup works and existence means a live object."""
    out = {}
    for d in w.lakeview.list():
        if "TRASHED" in str(getattr(d, "lifecycle_state", "") or ""):
            continue
        if d.display_name:
            out[d.display_name] = d.dashboard_id
    return out


def _valid_dashboard_serialized() -> str:
    """A RENDERING AI/BI dashboard draft (lvdash.json): one counter widget over `trips`.

    The renderer binds a widget to a query named **`main_query`** (naming it `main` gave
    "Missing query 'main_query'"); the counter's `encodings.value.fieldName` must match a field the
    query produces. Dataset = raw `trips` rows; the counter aggregates `COUNT(zip)` → one number.
    Kept to a SINGLE counter: the create API accepts structurally-invalid widgets, so render
    validity cannot be confirmed programmatically — a counter is the lowest-risk rendering widget."""
    return json.dumps({
        "datasets": [
            {"name": "ds_trips", "displayName": "trips",
             "queryLines": [f"SELECT * FROM {CATALOG}.{SCHEMA}.trips"]},
        ],
        "pages": [{"name": "page_main", "displayName": "Overview", "layout": [
            {"widget": {"name": "counter_trips",
                        "queries": [{"name": "main_query", "query": {
                            "datasetName": "ds_trips",
                            "fields": [{"name": "trip_count", "expression": "COUNT(`zip`)"}],
                            "disaggregated": False}}],
                        "spec": {"version": 2, "widgetType": "counter",
                                 "encodings": {"value": {"fieldName": "trip_count",
                                                         "displayName": "Trip count"}}}},
             "position": {"x": 0, "y": 0, "width": 3, "height": 4}}]}],
    })


def phase_dashboards():
    print("== lakeview (AI/BI) dashboard ==")
    wh = _warehouse_id()
    serialized = _valid_dashboard_serialized()
    if "wsmig_test_dashboard" in _active_dashboards():
        log("lakeview dashboard exists: wsmig_test_dashboard")
        return
    try:
        from databricks.sdk.service.dashboards import Dashboard
        d = w.lakeview.create(dashboard=Dashboard(
            display_name="wsmig_test_dashboard", warehouse_id=wh,
            serialized_dashboard=serialized))
        log(f"lakeview dashboard: wsmig_test_dashboard ({d.dashboard_id})")
    except Exception as e:
        log(f"lakeview: {str(e)[:150]}")


# ─────────────────────────── jobs (plain, non-DAB) ─────────────────────────

def phase_jobs():
    """Non-DAB jobs across the shapes that differ on export/import.

    The migration-relevant axes are: task count, whether a SCHEDULE exists (and whether it's
    paused — import is expected to land schedules PAUSED), which compute the tasks use (a new
    job cluster vs an existing all-purpose cluster vs a pool, the latter two being id references
    that must be remapped), and task TYPE (notebook / SQL / python-wheel-less spark_python).
    """
    print("== jobs (single, multi-task, scheduled, unscheduled, varied compute) ==")
    from databricks.sdk.service import jobs

    nb = f"{SHARED}/py_nb"
    sql_nb = f"{SHARED}/sql_nb"
    have = {j.settings.name for j in w.jobs.list()}

    existing_cluster_id = next((c.cluster_id for c in w.clusters.list()
                                if c.cluster_name == "wsmig_test_cluster"), None)

    specs = {}

    # 1. single task, NO schedule
    specs["wsmig_test_single_job"] = dict(
        tasks=[jobs.Task(task_key="t1", notebook_task=jobs.NotebookTask(notebook_path=nb),
                         new_cluster=_job_cluster())])

    # 2. multi-task DAG (a → b, c) with a PAUSED cron schedule
    specs["wsmig_test_multi_job"] = dict(
        tasks=[jobs.Task(task_key="a", notebook_task=jobs.NotebookTask(notebook_path=nb),
                         new_cluster=_job_cluster()),
               jobs.Task(task_key="b", depends_on=[jobs.TaskDependency(task_key="a")],
                         notebook_task=jobs.NotebookTask(notebook_path=nb),
                         new_cluster=_job_cluster()),
               jobs.Task(task_key="c", depends_on=[jobs.TaskDependency(task_key="a")],
                         notebook_task=jobs.NotebookTask(notebook_path=sql_nb),
                         new_cluster=_job_cluster())],
        schedule=jobs.CronSchedule(quartz_cron_expression="0 0 12 * * ?", timezone_id="UTC",
                                   pause_status=jobs.PauseStatus.PAUSED))

    # 3. an UNPAUSED schedule — so the "import pauses schedules" behaviour has something to act
    #    on (a job that is already paused proves nothing).
    specs["wsmig_test_scheduled_job"] = dict(
        tasks=[jobs.Task(task_key="t1", notebook_task=jobs.NotebookTask(notebook_path=nb),
                         new_cluster=_job_cluster())],
        schedule=jobs.CronSchedule(quartz_cron_expression="0 30 6 * * ?", timezone_id="UTC",
                                   pause_status=jobs.PauseStatus.UNPAUSED))

    # 4. job with parameters, tags, timeout, retries and an email notification — the settings
    #    fields most likely to be dropped by an over-eager payload strip.
    specs["wsmig_test_params_job"] = dict(
        tasks=[jobs.Task(task_key="t1",
                         notebook_task=jobs.NotebookTask(
                             notebook_path=nb, base_parameters={"env": "test", "n": "1"}),
                         new_cluster=_job_cluster(), timeout_seconds=3600,
                         max_retries=2, min_retry_interval_millis=10000)],
        tags={"wsmig_purpose": "fixture", "team": "migration"},
        timeout_seconds=7200,
        max_concurrent_runs=2,
        email_notifications=jobs.JobEmailNotifications(on_failure=[ME]))

    # 5. task on an EXISTING all-purpose cluster — an id cross-reference to remap, unlike the
    #    self-contained new_cluster jobs above.
    if existing_cluster_id:
        specs["wsmig_test_existing_cluster_job"] = dict(
            tasks=[jobs.Task(task_key="t1",
                             notebook_task=jobs.NotebookTask(notebook_path=nb),
                             existing_cluster_id=existing_cluster_id)])

    # 6. a job_clusters (shared cluster) definition reused by two tasks, plus a job-level
    #    parameter — a different compute shape again.
    specs["wsmig_test_jobcluster_job"] = dict(
        job_clusters=[jobs.JobCluster(job_cluster_key="shared", new_cluster=_job_cluster())],
        tasks=[jobs.Task(task_key="a", notebook_task=jobs.NotebookTask(notebook_path=nb),
                         job_cluster_key="shared"),
               jobs.Task(task_key="b", depends_on=[jobs.TaskDependency(task_key="a")],
                         notebook_task=jobs.NotebookTask(notebook_path=nb),
                         job_cluster_key="shared")],
        parameters=[jobs.JobParameterDefinition(name="run_date", default="2026-01-01")])

    # 7. SQL task — runs a real query on a real warehouse (both are id references to remap).
    wh_id = next((x.id for x in w.warehouses.list() if x.name == "wsmig_test_wh_pro"), None)
    q_id = next((q.id for q in w.queries.list() if q.display_name == "wsmig_test_query"), None)
    if wh_id and q_id:
        specs["wsmig_test_sql_task_job"] = dict(
            tasks=[jobs.Task(task_key="sql_task", sql_task=jobs.SqlTask(
                query=jobs.SqlTaskQuery(query_id=q_id), warehouse_id=wh_id))])
    else:
        log(f"sql_task job skipped: warehouse={wh_id} query={q_id}")

    # 8. dbt_task (workspace-sourced project dir; the job is never run, only migrated)
    specs["wsmig_test_dbt_job"] = dict(
        tasks=[jobs.Task(task_key="dbt_task",
                         dbt_task=jobs.DbtTask(commands=["dbt deps", "dbt run"],
                                               project_directory=f"/Workspace{SHARED}/dbt_project",
                                               source=jobs.Source.WORKSPACE),
                         new_cluster=_job_cluster())])

    # 9. spark_python_task on a real workspace .py file (written by phase_users_content)
    specs["wsmig_test_python_job"] = dict(
        tasks=[jobs.Task(task_key="python",
                         spark_python_task=jobs.SparkPythonTask(
                             python_file=f"/Workspace/Users/{ME}/wsmig/helpers.py",
                             source=jobs.Source.WORKSPACE),
                         new_cluster=_job_cluster())])

    for name, spec in specs.items():
        if name in have:
            log(f"job exists: {name}")
            continue
        try:
            j = w.jobs.create(name=name, **spec)
            log(f"job: {name} ({j.job_id}) tasks={len(spec.get('tasks', []))}"
                f"{' scheduled' if 'schedule' in spec else ''}")
        except Exception as e:
            log(f"job {name}: {str(e)[:120]}")

    # 10. run_job_task → a REAL job id (created above), a job→job id reference to remap.
    by_name = {j.settings.name: j.job_id for j in w.jobs.list()}
    if "wsmig_test_run_job_task_job" in by_name:
        log("job exists: wsmig_test_run_job_task_job")
    elif by_name.get("wsmig_test_single_job"):
        try:
            j = w.jobs.create(name="wsmig_test_run_job_task_job", tasks=[jobs.Task(
                task_key="trigger",
                run_job_task=jobs.RunJobTask(job_id=by_name["wsmig_test_single_job"]))])
            log(f"job: wsmig_test_run_job_task_job ({j.job_id}) → runs "
                f"wsmig_test_single_job ({by_name['wsmig_test_single_job']})")
        except Exception as e:
            log(f"job wsmig_test_run_job_task_job: {str(e)[:120]}")
    # 11. the pipeline_task job is created at the end of phase_dlt (needs a real pipeline id).

    # 12. run_as an ACCOUNT SP (appId must stay stable on target; the importer keeps it as-is).
    sp = next(iter(w.service_principals.list(filter=f'displayName eq "{ACCOUNT_SP_NAMES[0]}"')),
              None)
    if "wsmig_test_runas_sp_job" in by_name:
        log("job exists: wsmig_test_runas_sp_job")
    elif not sp:
        log(f"FLAG: wsmig_test_runas_sp_job skipped — {ACCOUNT_SP_NAMES[0]} not on the workspace")
    else:
        _ensure_sp_user_role(sp.application_id)
        try:
            j = w.jobs.create(name="wsmig_test_runas_sp_job",
                              run_as=jobs.JobRunAs(service_principal_name=sp.application_id),
                              tasks=[jobs.Task(task_key="t1",
                                               notebook_task=jobs.NotebookTask(notebook_path=nb),
                                               new_cluster=_job_cluster())])
            log(f"job: wsmig_test_runas_sp_job ({j.job_id}) run_as={ACCOUNT_SP_NAMES[0]}")
        except Exception as e:
            log(f"job wsmig_test_runas_sp_job: {str(e)[:120]}")


def _ensure_sp_user_role(app_id: str) -> None:
    """Binding a job's run_as to an SP needs `servicePrincipal.user` on that SP for the creator.
    The SP's creator is its manager, so it can grant itself the role (additive, idempotent)."""
    acct = _acct().config.account_id
    name = f"accounts/{acct}/servicePrincipals/{app_id}/ruleSets/default"
    me = f"users/{ME}"
    try:
        rs = w.api_client.do("GET", "/api/2.0/preview/accounts/access-control/rule-sets",
                             query={"name": name, "etag": ""})
        rules = rs.get("grant_rules", []) or []
        r = next((x for x in rules if x.get("role") == "roles/servicePrincipal.user"), None)
        if r and me in (r.get("principals") or []):
            return
        if r:
            r["principals"] = list(r.get("principals") or []) + [me]
        else:
            rules.append({"role": "roles/servicePrincipal.user", "principals": [me]})
        w.api_client.do("PUT", "/api/2.0/preview/accounts/access-control/rule-sets",
                        body={"name": name, "rule_set": {"name": name, "grant_rules": rules,
                                                         "etag": rs.get("etag", "")}})
        log(f"  servicePrincipal.user granted to {ME} on {app_id}")
    except Exception as e:
        log(f"  servicePrincipal.user on {app_id}: {str(e)[:110]}")


def _pipeline_task_job():
    """A job whose task triggers a REAL DLT pipeline (job→pipeline id reference to remap)."""
    from databricks.sdk.service import jobs
    name = "wsmig_test_pipeline_task_job"
    if any(j.settings.name == name for j in w.jobs.list()):
        log(f"job exists: {name}")
        return
    pid = next((p.pipeline_id for p in w.pipelines.list_pipelines()
                if p.name == "wsmig_test_pipeline"), None)
    if not pid:
        log(f"job {name} skipped: pipeline wsmig_test_pipeline not found")
        return
    try:
        j = w.jobs.create(name=name, tasks=[jobs.Task(
            task_key="pipeline_trig", pipeline_task=jobs.PipelineTask(pipeline_id=pid))])
        log(f"job: {name} ({j.job_id}) → pipeline {pid}")
    except Exception as e:
        log(f"job {name}: {str(e)[:120]}")


def _job_cluster():
    from databricks.sdk.service import compute
    return compute.ClusterSpec(spark_version=SPARK_VERSION, node_type_id=NODE, num_workers=1)


# ─────────────────────────── DLT pipeline (plain, non-DAB) ─────────────────

def phase_dlt():
    """Non-DAB DLT pipelines: serverless, classic-compute, and continuous.

    The pipeline SPEC references its source notebook by path and its output by catalog/target
    FQN, so all three shapes exist to prove those references survive (the catalog/target ones
    can't be remapped — UC is out of scope — which is itself worth showing).
    """
    print("== dlt pipelines ==")
    from databricks.sdk.service import pipelines
    from databricks.sdk.service import workspace as wssvc

    dlt_src = ("# Databricks notebook source\n"
               "import dlt\n"
               "@dlt.table\n"
               "def wsmig_test_bronze():\n"
               f"    return spark.read.table('{CATALOG}.{SCHEMA}.trips')\n")
    dlt_nb = f"{SHARED}/dlt_nb"
    try:
        w.workspace.import_(path=dlt_nb, language=wssvc.Language.PYTHON,
                            format=wssvc.ImportFormat.SOURCE,
                            content=base64.b64encode(dlt_src.encode()).decode(), overwrite=True)
    except Exception as e:
        log(f"dlt nb: {str(e)[:60]}")

    lib = [pipelines.PipelineLibrary(notebook=pipelines.NotebookLibrary(path=dlt_nb))]
    have = {p.name for p in w.pipelines.list_pipelines()}
    specs = {
        # serverless + development mode
        "wsmig_test_pipeline": dict(libraries=lib, catalog=CATALOG, target=SCHEMA,
                                    development=True, serverless=True),
        # CONTINUOUS (not triggered) + production mode — different lifecycle fields
        "wsmig_test_pipeline_continuous": dict(libraries=lib, catalog=CATALOG, target=SCHEMA,
                                               development=False, serverless=True,
                                               continuous=True),
        # classic compute, so the pipeline carries a `clusters` block to strip/replay
        "wsmig_test_pipeline_classic": dict(
            libraries=lib, catalog=CATALOG, target=SCHEMA, development=True,
            clusters=[pipelines.PipelineCluster(label="default", node_type_id=NODE,
                                                num_workers=1)],
            configuration={"wsmig.test": "1"}),
    }
    for name, spec in specs.items():
        if name in have:
            log(f"dlt pipeline exists: {name}")
            continue
        try:
            p = w.pipelines.create(name=name, **spec)
            log(f"dlt pipeline: {name} ({p.pipeline_id})")
        except Exception as e:
            log(f"dlt pipeline {name}: {str(e)[:120]}")
    _pipeline_task_job()


# ─────────────────────────── misc (GIS, cluster lib, ws conf) ──────────────

def phase_misc():
    print("== misc (global init scripts, workspace conf) ==")

    # Global init scripts. Two of them, with different `position`/`enabled`, because ORDER is
    # part of a GIS's meaning — they run in sequence — so it has to survive the migration.
    have_gis = {g.name: g.script_id for g in (w.global_init_scripts.list() or [])}
    scripts = {
        "wsmig_test_gis": (b"#!/bin/bash\necho wsmig-test\n", False, 0),
        "wsmig_test_gis_enabled": (b"#!/bin/bash\necho wsmig-test-two\n", True, 1),
    }
    for name, (body, enabled, position) in scripts.items():
        if name in have_gis:
            log(f"global init script exists: {name} ({have_gis[name]})")
            continue
        try:
            gis = w.global_init_scripts.create(
                name=name, script=base64.b64encode(body).decode(),
                enabled=enabled, position=position)
            log(f"global init script: {name} ({gis.script_id}) enabled={enabled} pos={position}")
        except Exception as e:
            log(f"gis {name}: {str(e)[:90]}")

    # Workspace conf. These are per-workspace settings the target must be SET to match; each key
    # is applied individually so one rejected key doesn't drop the rest. Set to NON-DEFAULT values
    # per spec so changes are visible on the target.
    conf = {
        "enableExportNotebook": "false",
        "enableResultsDownloading": "false",
        "enableNotebookTableClipboard": "false",
        "maxTokenLifetimeDays": "30",
        "enableWebTerminal": "false",
        "enableTokensConfig": "true",  # reference bed key
        "enableIpAccessLists": "false",  # reference bed key
    }
    ok = []
    for k, v in conf.items():
        try:
            w.api_client.do("PATCH", "/api/2.0/workspace-conf", body={k: v})
            ok.append(f"{k}={v}")
        except Exception as e:
            log(f"ws conf {k}: {str(e)[:80]}")
    log(f"workspace conf set: {', '.join(ok)}")
    log("note: cluster libraries need a RUNNING cluster — see the `libraries` phase")


# ─────────────────────────── serving (external model endpoint) ─────────────

def phase_serving():
    """External-model serving endpoint = the only auto-migratable serving kind.

    Created via RAW REST (the SDK's EndpointCoreConfigInput errors with "missing name").
    Uses a dummy api key — the endpoint only has to EXIST for export to capture it.
    """
    print("== serving (external model endpoint) ==")
    try:
        r = w.api_client.do("POST", "/api/2.0/serving-endpoints", body={
            "name": "wsmig_test_ext_endpoint",
            "config": {"served_entities": [{
                "name": "openai_gpt",
                "external_model": {
                    "name": "gpt-4o-mini", "provider": "openai", "task": "llm/v1/chat",
                    "openai_config": {"openai_api_key_plaintext": "sk-dummy-not-a-real-key"},
                },
            }]},
        })
        log(f"serving endpoint: wsmig_test_ext_endpoint ({r.get('id')})")
    except Exception as e:
        log(f"serving: {str(e)[:160]}")


# ─────────────────────────── AKV-backed secret scope ───────────────────────

AKV_RG = os.environ.get("WSMIG_AKV_RG", "wsmig-test-rg")
AKV_LOCATION = os.environ.get("WSMIG_AKV_LOCATION", "eastus2")
# The AzureDatabricks first-party application id — the resource an AAD token must target
# so the workspace can verify Key Vault control when registering an AKV-backed scope.
AZURE_DATABRICKS_APP_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"


def _az(*args):
    return subprocess.run(["az", *args], capture_output=True, text=True)


@functools.lru_cache(maxsize=1)
def _tenant() -> str:
    """The AAD tenant of the current az login (the workspace lives in the same one)."""
    r = _az("account", "show", "-o", "json")
    return json.loads(r.stdout)["tenantId"] if not r.returncode else ""


def phase_akv():
    """AKV-backed secret scope.

    Registering one needs an **Azure AD** bearer token for the AzureDatabricks resource — a
    Databricks OAuth token carries no AAD identity and the RPC fails with
    "must have userAADToken defined!". So we mint the AAD token with the az CLI and POST
    secrets/scopes ourselves rather than going through the SDK client.
    """
    print("== akv-backed secret scope ==")
    # Vault names are GLOBAL and a deleted vault stays soft-deleted (name unusable) for 90 days, so
    # derive the name from the workspace id (unique per source bed; 7+16 = 23 ≤ 24 chars).
    vault = os.environ.get("WSMIG_AKV_NAME") or f"wsmigkv{w.get_workspace_id()}"[:24]
    have = [s_.name for s_ in w.secrets.list_scopes() if s_.name == "wsmig_test_akv_scope"]
    if have:
        log("AKV-backed secret scope exists: wsmig_test_akv_scope")
        return
    az = _az

    # The db_fe management group denies any resource without an `owner` tag, so tag both.
    tags = [f"owner={ME}", "purpose=wsmig-export-test"]
    r = az("group", "create", "-n", AKV_RG, "-l", AKV_LOCATION, "--tags", *tags, "-o", "json")
    if r.returncode:
        log(f"rg: {r.stderr[:120]}")
        return
    r = az("keyvault", "create", "-n", vault, "-g", AKV_RG, "-l", AKV_LOCATION,
           "--enable-rbac-authorization", "false", "--tags", *tags, "-o", "json")
    if r.returncode:
        # may already exist from a prior run — fall back to reading it
        r2 = az("keyvault", "show", "-n", vault, "-g", AKV_RG, "-o", "json")
        if r2.returncode:
            log(f"vault create: {(r.stderr or '')[:300]}")
            return
        r = r2
    vinfo = json.loads(r.stdout)
    vault_id = vinfo["id"]
    vault_uri = vinfo["properties"]["vaultUri"]
    log(f"key vault: {vault} ({vault_uri})")

    # a secret in the vault so the scope has content to enumerate
    az("keyvault", "secret", "set", "--vault-name", vault, "-n", "wsmig-akv-key",
       "--value", "akv-secret-value", "-o", "none")

    # AAD token for the AzureDatabricks resource (NOT the Databricks OAuth token)
    r = az("account", "get-access-token", "--resource", AZURE_DATABRICKS_APP_ID,
           "--tenant", _tenant(), "-o", "json")
    if r.returncode:
        log(f"aad token: {r.stderr[:160]}")
        return
    aad = json.loads(r.stdout)["accessToken"]

    import requests
    resp = requests.post(
        f"{_host()}/api/2.0/secrets/scopes/create",
        headers={"Authorization": f"Bearer {aad}"},
        json={"scope": "wsmig_test_akv_scope",
              "scope_backend_type": "AZURE_KEYVAULT",
              "backend_azure_keyvault": {"resource_id": vault_id, "dns_name": vault_uri},
              "initial_manage_principal": "users"},
        timeout=60)
    if resp.status_code == 200:
        log("AKV-backed secret scope created: wsmig_test_akv_scope")
    else:
        log(f"akv scope: HTTP {resp.status_code} {resp.text[:250]}")


def _host():
    import configparser
    c = configparser.ConfigParser()
    c.read(__import__("os").path.expanduser("~/.databrickscfg"))
    return dict(c[PROFILE])["host"].rstrip("/")


# ─────────────────────────── DAB bundles ───────────────────────────────────

# `databricks bundle deploy` shells out to terraform; the account's PGP signing key is expired,
# so every deploy points the CLI at a pre-downloaded binary instead of letting it fetch+verify one.
TF_BIN = os.environ.get("WSMIG_TF_BIN", "/tmp/tfbin/terraform")


def _bundle_cli() -> str:
    """A CLI new enough for every bundle resource type we deploy.

    `genie_spaces` only became a bundle resource in CLI 1.x, so prefer an explicit override,
    then the homebrew build, then whatever is on PATH.
    """
    if os.environ.get("WSMIG_CLI"):
        return os.environ["WSMIG_CLI"]
    for cand in ("/opt/homebrew/bin/databricks", "databricks"):
        r = subprocess.run([cand, "--version"], capture_output=True, text=True)
        if r.returncode == 0:
            ver = r.stdout.strip().split()[-1].lstrip("v")
            if int(ver.split(".")[0]) >= 1 and ver.split(".")[0] != "0":
                return cand
    return "databricks"


def _bundle_env() -> dict:
    return dict(os.environ, DATABRICKS_TF_EXEC_PATH=TF_BIN, DATABRICKS_TF_VERSION="1.9.8")


def phase_dab():
    """Deploy REAL Databricks Asset Bundles so the export's DAB detection is exercised.

    Two bundles — one landing in /Users/<me>/.bundle/, one in /Shared/.bundle/ — each with a
    job + a pipeline + a dashboard, so DAB-deployed twins of those asset types exist alongside
    the manually-created ones.
    """
    import tempfile
    print("== dab bundles ==")
    if not os.path.isfile(TF_BIN):
        log(f"terraform binary missing at {TF_BIN} — run the tf download first; skipping DAB")
        return
    cli, env = _bundle_cli(), _bundle_env()
    wh = _warehouse_id()
    # The CLI's U2M OAuth token-cache collides across multiple profiles/workspaces on one machine
    # ("Refresh token context does not match the request context"), which breaks `bundle deploy -p`.
    # The SDK auth works, so mint a bearer token from it and give the deploy DIRECT host+token auth
    # (no profile), bypassing the broken cache refresh. Fall back to -p if no token can be minted.
    _bearer = (w.config.authenticate() or {}).get("Authorization", "").replace("Bearer ", "")
    if _bearer:
        env = dict(env, DATABRICKS_HOST=w.config.host, DATABRICKS_TOKEN=_bearer)
        env.pop("DATABRICKS_CONFIG_PROFILE", None)
        deploy_cmd = [cli, "bundle", "deploy"]
    else:
        deploy_cmd = [cli, "bundle", "deploy", "-p", PROFILE]

    # Valid, rendering dashboard (shared helper) — the old inline def used an empty-encodings table
    # over a `zip` field, so the DAB-deployed dashboard rendered "Invalid widget definition" too.
    dash_serialized = _valid_dashboard_serialized()
    # A Genie space the bundle deploys (DAB-managed twin of the directly-created ones) — same
    # trips+zones data sources, so the DAB-detection path is exercised for genie as well.
    genie_serialized = json.dumps({
        "version": 2,
        "data_sources": {"tables": [
            {"identifier": f"{CATALOG}.{SCHEMA}.trips"},
            {"identifier": f"{CATALOG}.{SCHEMA}.zones"}]},
    })

    for tag, root_path in (("shared", "/Shared/.bundle/wsmig_test_shared"),
                           ("user", f"/Users/{ME}/.bundle/wsmig_test_user")):
        d = tempfile.mkdtemp(prefix=f"wsmig_dab_{tag}_")
        # bundle sources
        with open(f"{d}/dab_nb.py", "w") as f:
            f.write("# Databricks notebook source\nprint('dab notebook')\n")
        with open(f"{d}/dab_dlt.py", "w") as f:
            f.write("# Databricks notebook source\nimport dlt\n@dlt.table\n"
                    f"def wsmig_dab_{tag}_bronze():\n"
                    f"    return spark.read.table('{CATALOG}.{SCHEMA}.trips')\n")
        with open(f"{d}/dab_dash.lvdash.json", "w") as f:
            f.write(dash_serialized)
        with open(f"{d}/dab_genie.json", "w") as f:
            f.write(genie_serialized)
        bundle = f"""
bundle:
  name: wsmig_test_{tag}

workspace:
  root_path: {root_path}

resources:
  jobs:
    wsmig_dab_{tag}_job:
      name: wsmig_dab_{tag}_job
      tasks:
        - task_key: t1
          notebook_task:
            notebook_path: ./dab_nb.py
          new_cluster:
            spark_version: 16.4.x-scala2.12
            node_type_id: Standard_DS3_v2
            num_workers: 1
  pipelines:
    wsmig_dab_{tag}_pipeline:
      name: wsmig_dab_{tag}_pipeline
      catalog: {CATALOG}
      target: {SCHEMA}
      serverless: true
      libraries:
        - notebook:
            path: ./dab_dlt.py
  dashboards:
    wsmig_dab_{tag}_dashboard:
      display_name: wsmig_dab_{tag}_dashboard
      warehouse_id: {wh}
      file_path: ./dab_dash.lvdash.json
  genie_spaces:
    wsmig_dab_{tag}_genie:
      title: wsmig_dab_{tag}_genie
      warehouse_id: {wh}
      file_path: ./dab_genie.json
"""
        with open(f"{d}/databricks.yml", "w") as f:
            f.write(bundle)
        r = subprocess.run(deploy_cmd, cwd=d, capture_output=True, text=True, env=env)
        if r.returncode:
            log(f"dab {tag} deploy FAILED: {(r.stderr or r.stdout)[-400:]}")
        else:
            log(f"dab {tag} deployed → {root_path}")


# ─────────────────────────── cluster libraries ─────────────────────────────

def phase_libraries():
    """Install all three library kinds on a RUNNING cluster.

    Libraries can only be installed on a running cluster, so this starts it (and leaves it to
    autoterminate). Covers the migration-relevant distinction:
      • pypi / maven  → re-resolve from their repos on target        → auto-migratable
      • jar on dbfs:/ → the FILE is never exported (DBFS out of scope) → must be flagged manual
    """
    print("== cluster libraries ==")
    cid = None
    for c in w.clusters.list():
        if c.cluster_name == "wsmig_test_cluster":
            cid = c.cluster_id
    if not cid:
        log("wsmig_test_cluster not found — run the compute phase first")
        return
    log(f"starting cluster {cid} (libraries need it RUNNING)…")
    try:
        w.clusters.start_and_wait(cluster_id=cid, timeout=__import__("datetime").timedelta(minutes=15))
    except Exception as e:
        if "already" not in str(e).lower() and "unexpected state" not in str(e).lower():
            log(f"cluster start: {str(e)[:110]}")

    # a dummy jar on DBFS so the dangling-reference case is real
    try:
        w.api_client.do("POST", "/api/2.0/dbfs/put",
                        body={"path": "/FileStore/wsmig_test/wsmig_dummy.jar",
                              "contents": base64.b64encode(b"PK\x03\x04dummy-jar-bytes").decode(),
                              "overwrite": True})
        log("dbfs jar staged: dbfs:/FileStore/wsmig_test/wsmig_dummy.jar")
    except Exception as e:
        log(f"dbfs put: {str(e)[:90]}")

    try:
        w.api_client.do("POST", "/api/2.0/libraries/install", body={
            "cluster_id": cid,
            "libraries": [{"pypi": {"package": "tabulate==0.9.0"}},
                          {"maven": {"coordinates": "com.google.code.gson:gson:2.10.1"}},
                          {"jar": "dbfs:/FileStore/wsmig_test/wsmig_dummy.jar"}]})
        log("libraries installed: pypi + maven + dbfs jar")
    except Exception as e:
        log(f"library install: {str(e)[:110]}")
    time.sleep(20)
    try:
        st = w.api_client.do("GET", "/api/2.0/libraries/cluster-status",
                             query={"cluster_id": cid})
        for ls in st.get("library_statuses", []):
            log(f"  {json.dumps(ls['library'])[:60]} → {ls['status']}")
    except Exception as e:
        log(f"status: {str(e)[:80]}")


# ─────────────────────────── oversize workspace files ──────────────────────

def phase_bigfiles():
    """Large workspace files, for the oversize/size-tier reporting path.

    Notes on the real caps (verified live):
      • a >10 MB NOTEBOOK cannot be created at all — the import API rejects it, and uploading
        a >10 MB `.py` with the notebook header fails the same way. So the oversize-NOTEBOOK row
        is only reachable offline (tests/test_export) or by lowering the cap.
      • workspace FILES cap at 500 MB. 60/120 MB files upload fine and export fine; they exist so
        tests/live_fvm1_oversize.py can trip a lowered cap and show the real report rows.
    """
    print("== oversize workspace files ==")
    for mb, name in ((60, "wsmig_test_60mb.bin"), (120, "wsmig_test_120mb.bin")):
        path = f"{USERDIR}/{name}"
        try:
            w.api_client.do("POST", f"/api/2.0/workspace-files/import-file{path}",
                            query={"overwrite": "true"}, data=b"x" * (mb * 1024 * 1024),
                            headers={"Content-Type": "application/octet-stream"})
            log(f"{mb}MB file created: {name}")
        except Exception as e:
            log(f"{mb}MB file: {str(e)[:90]}")
    # prove the >10MB notebook really is impossible (documents the limit rather than hiding it)
    big = "# Databricks notebook source\n" + ("# filler " + "z" * 80 + "\n") * 140000
    try:
        w.api_client.do("POST", f"/api/2.0/workspace-files/import-file{USERDIR}/wsmig_big_nb.py",
                        query={"overwrite": "true"}, data=big.encode(),
                        headers={"Content-Type": "application/octet-stream"})
        log("!! >10MB .py unexpectedly accepted")
    except Exception as e:
        log(f">10MB notebook-source correctly REJECTED: {str(e)[:80]}")


# ─────────────────────────── DAB: pathless assets + genie ──────────────────

def phase_dab_pathless():
    """Deploy DAB-managed assets that have NO workspace path, plus a DAB Genie space.

    These are the cases path-based `.bundle/` detection CANNOT see (a cluster/pool/warehouse/
    scope has no workspace path at all), so they exercise the bundle-state-file detection in
    src/collectors/dab_registry.py.

    Coverage note (CLI 1.5.0 bundle schema): `instance_pools`, `cluster_policies` and SQL
    `queries` are NOT bundle resource types, so a DAB-owned twin of those three is impossible —
    they only ever exist manually created. `genie_spaces`, `alerts` and
    `model_serving_endpoints` ARE, and are covered here.
    """
    import tempfile
    print("== dab pathless assets + genie/alert/serving ==")
    cli, env = _bundle_cli(), _bundle_env()
    ver = subprocess.run([cli, "--version"], capture_output=True, text=True).stdout.strip()
    log(f"using CLI: {ver}")
    wh = _warehouse_id()

    d = tempfile.mkdtemp(prefix="wsmig_dab_pathless_")
    with open(f"{d}/dab_genie.geniespace.json", "w") as f:
        json.dump({"version": 2, "data_sources": {
            "tables": [{"identifier": f"{CATALOG}.{SCHEMA}.trips"}]}}, f)
    with open(f"{d}/databricks.yml", "w") as f:
        f.write(f"""
bundle:
  name: wsmig_test_pathless

workspace:
  root_path: /Shared/.bundle/wsmig_test_pathless

# A bundle deployed outside /Users must declare its permissions explicitly — the CLI refuses an
# unrestricted /Shared deployment otherwise.
permissions:
  - group_name: users
    level: CAN_MANAGE

resources:
  clusters:
    wsmig_dab_cluster:
      cluster_name: wsmig_dab_cluster
      spark_version: 16.4.x-scala2.12
      node_type_id: Standard_DS3_v2
      num_workers: 1
      autotermination_minutes: 10
  sql_warehouses:
    wsmig_dab_wh:
      name: wsmig_dab_wh
      cluster_size: 2X-Small
      max_num_clusters: 1
      warehouse_type: CLASSIC
  secret_scopes:
    wsmig_dab_scope:
      name: wsmig_dab_scope
  genie_spaces:
    wsmig_dab_genie:
      title: wsmig_dab_genie
      description: DAB-deployed genie space
      warehouse_id: {wh}
      file_path: ./dab_genie.geniespace.json
  alerts:
    wsmig_dab_alert:
      display_name: wsmig_dab_alert
      warehouse_id: {wh}
      query_text: SELECT count(*) AS c FROM {CATALOG}.{SCHEMA}.trips
      # the alerts API rejects a create without an explicit cron schedule
      schedule:
        quartz_cron_schedule: 0 0 10 * * ?
        timezone_id: UTC
      evaluation:
        comparison_operator: GREATER_THAN
        source:
          name: c
        threshold:
          value:
            double_value: 0
  model_serving_endpoints:
    wsmig_dab_endpoint:
      name: wsmig_dab_endpoint
      config:
        served_entities:
          - name: dab_openai
            external_model:
              name: gpt-4o-mini
              provider: openai
              task: llm/v1/chat
              openai_config:
                openai_api_key_plaintext: sk-dummy-not-a-real-key
""")
    # Same CLI OAuth-cache workaround as phase_dab: direct host+token auth minted from the SDK.
    _bearer = (w.config.authenticate() or {}).get("Authorization", "").replace("Bearer ", "")
    if _bearer:
        env = dict(env, DATABRICKS_HOST=w.config.host, DATABRICKS_TOKEN=_bearer)
        env.pop("DATABRICKS_CONFIG_PROFILE", None)
        deploy_cmd = [cli, "bundle", "deploy"]
    else:
        deploy_cmd = [cli, "bundle", "deploy", "-p", PROFILE]
    r = subprocess.run(deploy_cmd, cwd=d, capture_output=True, text=True, env=env)
    if r.returncode:
        log(f"deploy FAILED: {(r.stderr or r.stdout)[-800:]}")
    else:
        log("deployed (DAB-owned): cluster + warehouse + secret scope + genie space "
            "+ alert + serving endpoint")


# ─────────────────────────── object ACLs ───────────────────────────────────

# The full permission ladder per permissions-API object type. The point is to cover EVERY level,
# not just CAN_MANAGE: a fixture set that only ever grants CAN_MANAGE can't tell whether the
# importer preserves the specific level or just re-grants admin to everyone.
#
# IS_OWNER is deliberately excluded — it can't be granted to an arbitrary principal alongside
# other grants (the API requires exactly one owner, and jobs/queries own theirs already).
ACL_LADDER = {
    "clusters": ["CAN_ATTACH_TO", "CAN_RESTART", "CAN_MANAGE"],
    "instance-pools": ["CAN_ATTACH_TO", "CAN_MANAGE"],
    "cluster-policies": ["CAN_USE"],
    "jobs": ["CAN_VIEW", "CAN_MANAGE_RUN", "CAN_MANAGE"],
    "notebooks": ["CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"],
    "files": ["CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"],
    "directories": ["CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"],
    "pipelines": ["CAN_VIEW", "CAN_RUN", "CAN_MANAGE"],
    "sql/warehouses": ["CAN_USE", "CAN_MONITOR", "CAN_MANAGE"],
    "dashboards": ["CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"],
    # NOTE the object-type spellings the permissions API actually accepts, verified live:
    # Alerts V2 are "alertsv2" (numeric id), LEGACY alerts are "alerts" (uuid) — two distinct
    # object types, not aliases — and genie's ladder starts at CAN_READ, it has no CAN_VIEW.
    "alertsv2": ["CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"],
    "alerts": ["CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"],
    "queries": ["CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"],
    "genie": ["CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"],
    "serving-endpoints": ["CAN_VIEW", "CAN_QUERY", "CAN_MANAGE"],
}

# Secret scope ACLs are a DIFFERENT API (secrets/acls/put, not permissions/...) with its own
# vocabulary — a permissions/ PUT against a scope 404s.
SECRET_ACL_LEVELS = ["READ", "WRITE", "MANAGE"]


def _acl_principals():
    """One principal of each KIND, so the importer's principal handling is fully exercised.

    All identities here are ACCOUNT-level (the customer has no workspace-local groups or SPs), so the
    migration ASSIGNS them to the target and preserves their ids — Entra users, Entra-backed account
    groups, DB-managed account groups (flat/nested/mixed) and account SPs — none are remapped.
    """
    out = []
    # Users: two NON-runner, non-orphan users. Never ME: the runner OWNS every fixture object, so a
    # grant to it is invisible, and on jobs/pipelines it demotes the owner ("exactly one owner").
    # Also not ENTRA_USERS[1]/[3] (they own a job/pipeline/warehouse via phase_cross_acls).
    for idx, kind in ((2, "entra user"), (5, "entra user 2")):
        if len(ENTRA_USERS) > idx and ENTRA_USERS[idx] != ME:
            u = next(iter(w.users.list(filter=f'userName eq "{ENTRA_USERS[idx]}"')), None)
            if u:
                out.append(("user_name", u.user_name, kind))
    # Account-level groups ONLY (the customer has no workspace-local groups): both Entra-backed
    # groups + a DB-managed account group + the nested parent + the mixed group. DISTINCT kind labels
    # so the coverage report counts real principals, not collapsed duplicate labels.
    for gname, kind in ((ENTRA_GROUP_NAMES[0], "entra group"),
                        (ENTRA_GROUP_NAMES[1], "entra group 2"),
                        (ACCOUNT_NESTED_PARENT, "account group (nested parent)"),
                        (ACCOUNT_NESTED_CHILD, "account group (nested child)")):
        g = next(iter(w.groups.list(filter=f'displayName eq "{gname}"')), None)
        if g:
            out.append(("group_name", gname, kind))
    # Account-level SPs (stable appId, identity preserved on target — assigned, NOT remapped), so the
    # ACL replay covers the "keep the applicationId" SP principal kind. Both, for more than one grant.
    sp_kinds = ([(n, f"account SP ({i + 1})") for i, n in enumerate(ACCOUNT_SP_NAMES)]
                + [(n, f"UMI SP ({i + 1})") for i, n in enumerate(UMI_SP_NAMES)])
    for sp_name, kind in sp_kinds:
        # WORKSPACE SCIM = only SPs actually assigned here. A missing one is FLAGGED and skipped —
        # granting to it would fail every object it rotates onto ("principal does not exist").
        sp = next(iter(w.service_principals.list(filter=f'displayName eq "{sp_name}"')), None)
        if sp and sp.application_id:
            out.append(("service_principal_name", sp.application_id, kind))
        else:
            log(f"FLAG: {sp_name} is NOT assigned to this workspace — no ACLs granted to it "
                f"(assign it, then re-run `acls`)")
    return out


def _acl_targets():
    """Resolve (object_type, object_id, label) for every wsmig_* object worth granting on."""
    t = []

    for c in w.clusters.list():
        if (c.cluster_name or "").startswith("wsmig"):
            t.append(("clusters", c.cluster_id, c.cluster_name))
    for p in w.instance_pools.list():
        if (p.instance_pool_name or "").startswith("wsmig"):
            t.append(("instance-pools", p.instance_pool_id, p.instance_pool_name))
    for p in w.cluster_policies.list():
        if (p.name or "").startswith("wsmig"):
            t.append(("cluster-policies", p.policy_id, p.name))
    for j in w.jobs.list():
        if (j.settings.name or "").startswith("wsmig"):
            t.append(("jobs", j.job_id, j.settings.name))
    for p in w.pipelines.list_pipelines():
        if (p.name or "").startswith("wsmig"):
            t.append(("pipelines", p.pipeline_id, p.name))
    for wh in w.warehouses.list():
        if (wh.name or "").startswith("wsmig"):
            t.append(("sql/warehouses", wh.id, wh.name))
    for d in w.lakeview.list():
        if (d.display_name or "").startswith("wsmig"):
            t.append(("dashboards", d.dashboard_id, d.display_name))
    for e in w.serving_endpoints.list():
        if (e.name or "").startswith("wsmig"):
            t.append(("serving-endpoints", e.id, e.name))
    try:
        for q in w.queries.list():
            if (q.display_name or "").startswith("wsmig"):
                t.append(("queries", q.id, q.display_name))
    except Exception as e:
        log(f"queries list: {str(e)[:70]}")
    try:
        for al in w.alerts_v2.list_alerts():
            if (al.display_name or "").startswith("wsmig"):
                t.append(("alertsv2", al.id, al.display_name))
    except Exception as e:
        log(f"alerts_v2 list: {str(e)[:70]}")
    try:
        for al in w.alerts_legacy.list():
            if (al.name or "").startswith("wsmig"):
                t.append(("alerts", al.id, al.name))
    except Exception as e:
        log(f"legacy alerts list: {str(e)[:70]}")
    try:
        for sp_ in w.genie.list_spaces().spaces or []:
            if (sp_.title or "").startswith("wsmig"):
                t.append(("genie", sp_.space_id, sp_.title))
    except Exception as e:
        log(f"genie list: {str(e)[:70]}")

    # Workspace content: notebooks, files and directories are three distinct permissions-API
    # object types even though they all live in the workspace tree.
    from databricks.sdk.service import workspace as wssvc

    def walk(path):
        try:
            for o in w.workspace.list(path):
                if o.object_type == wssvc.ObjectType.DIRECTORY:
                    if ".bundle" in (o.path or ""):
                        continue
                    t.append(("directories", o.object_id, o.path))
                    walk(o.path)
                elif o.object_type == wssvc.ObjectType.NOTEBOOK:
                    t.append(("notebooks", o.object_id, o.path))
                elif o.object_type == wssvc.ObjectType.FILE:
                    t.append(("files", o.object_id, o.path))
        except Exception as e:
            log(f"walk {path}: {str(e)[:60]}")

    walk(SHARED)
    walk(USERDIR)
    for email in ENTRA_USERS:            # per-user content (phase_users_content), orphans included
        base = _user_base(email)
        oid = _obj_id(base)
        if oid:
            t.append(("directories", oid, base))
        walk(base)
    return t


def phase_acls():
    """Grant every object type its full permission ladder, across every principal KIND.

    Runs LAST: it grants on objects the earlier phases create. Uses PATCH (not PUT) so the
    existing owner/admin grants are preserved rather than replaced.
    """
    print("== object ACLs (full permission ladder per object type) ==")
    principals = _acl_principals()
    if not principals:
        log("no wsmig principals found — run the identity phase first")
        return
    log(f"principals: {', '.join(f'{v} ({k})' for _, v, k in principals)}")

    targets = _acl_targets()
    granted = failed = 0
    by_type = {}
    pairs_seen = set()
    for n_obj, (obj_type, obj_id, label) in enumerate(targets):
        ladder = ACL_LADDER.get(obj_type)
        if not ladder or obj_id is None:
            continue
        # Rotate which principal gets which level, so across the fixture set every
        # (object type × permission level) and every (principal kind × permission level)
        # combination appears, without granting the cross product on every single object.
        #
        # The offset ADVANCES PER OBJECT (`n_obj`), not just per level: starting every object at
        # principal[0] would mean principals beyond the longest ladder (4) — notably the
        # service principal — never receive a single grant, and SP grants are exactly the ones
        # that must be remapped on import.
        acl = []
        n_grants = min(max(len(ladder), 3), len(principals))
        for i in range(n_grants):
            level = ladder[i % len(ladder)]
            field, value, kind = principals[(n_obj + i) % len(principals)]
            acl.append({field: value, "permission_level": level})
            pairs_seen.add((kind, level))
        try:
            w.api_client.do("PATCH", f"/api/2.0/permissions/{obj_type}/{obj_id}",
                            body={"access_control_list": acl})
            granted += len(acl)
            by_type[obj_type] = by_type.get(obj_type, 0) + len(acl)
        except Exception as e:
            failed += 1
            if failed <= 8:
                log(f"acl {obj_type}/{label}: {str(e)[:100]}")

    for obj_type in sorted(by_type):
        log(f"  {obj_type}: {by_type[obj_type]} grants")
    log(f"object ACLs: {granted} grants across {len(by_type)} object types "
        f"({failed} objects failed)")
    # Report the coverage that was actually achieved, so a silently-narrow rotation is visible
    # rather than hidden behind a healthy-looking grant total.
    kinds_covered = {}
    for kind, level in sorted(pairs_seen):
        kinds_covered.setdefault(kind, []).append(level)
    log(f"principal-kind × level coverage: {len(pairs_seen)} pairs")
    for kind in sorted(kinds_covered):
        log(f"  {kind}: {', '.join(kinds_covered[kind])}")
    no_grants = [kind for _f, _v, kind in principals if kind not in kinds_covered]
    if no_grants:
        log(f"  !! principal kinds with NO grants at all: {sorted(set(no_grants))}")

    # Secret scope ACLs — different API, different vocabulary.
    from databricks.sdk.service.workspace import AclPermission
    scope_names = [s.name for s in (w.secrets.list_scopes() or [])
                   if (s.name or "").startswith("wsmig")]
    n = 0
    for i, scope in enumerate(scope_names):
        for j, level in enumerate(SECRET_ACL_LEVELS):
            field, value, _ = principals[(i + j) % len(principals)]
            if field == "service_principal_name":
                continue  # scope ACLs take a principal NAME, not an application id
            try:
                w.secrets.put_acl(scope=scope, principal=value,
                                  permission=getattr(AclPermission, level))
                n += 1
            except Exception as e:
                log(f"secret acl {scope}/{level}: {str(e)[:80]}")
    log(f"secret scope ACLs: {n} grants across {len(scope_names)} scopes")
    _verify_acls(targets, principals)


def _verify_acls(targets, principals):
    """READ-BACK: every target object must carry >=1 DIRECT grant to a fixture principal (not just
    owner/admins), and every fixture principal must hold grants somewhere. Prints a per-type table
    + any object without grants, so a silently-thin ACL bed is visible."""
    want = {v for _f, v, _k in principals}
    per_type, bare, held = {}, [], {v: 0 for v in want}
    for obj_type, obj_id, label in targets:
        if obj_type not in ACL_LADDER or obj_id is None:
            continue
        try:
            doc = w.api_client.do("GET", f"/api/2.0/permissions/{obj_type}/{obj_id}") or {}
        except Exception as e:
            bare.append(f"{obj_type} {label}: read failed {str(e)[:60]}")
            continue
        hits = 0
        for ace in doc.get("access_control_list", []) or []:
            who = ace.get("user_name") or ace.get("group_name") or ace.get("service_principal_name")
            direct = [pm for pm in ace.get("all_permissions", []) or [] if not pm.get("inherited")]
            if who in want and direct:
                hits += 1
                held[who] += 1
        t = per_type.setdefault(obj_type, [0, 0])
        t[0] += 1
        t[1] += 1 if hits else 0
        if not hits:
            bare.append(f"{obj_type} {label}")
    log("ACL read-back (objects with fixture grants / objects):")
    for ot in sorted(per_type):
        log(f"  {ot}: {per_type[ot][1]}/{per_type[ot][0]}")
    for _f, v, k in principals:
        log(f"  {k} {v}: grants on {held[v]} objects")
    if bare:
        log(f"  !! {len(bare)} objects WITHOUT fixture grants:")
        for b in bare[:40]:
            log(f"     - {b}")
    else:
        log("  ✓ every object carries fixture grants")


# ───────────────────── SCALE fixtures (B6 parallelism) ──────────────────────
# These stand up a REAL-scale bed: ~150 assigned account users each with a tree of workspace
# content, plus a batch of account-level groups, so the parallel ACL-enrichment pass (inventory)
# and the parallel sub-level import both have thousands of objects to chew through — the only way
# to prove B6 does the right thing under load rather than on a toy set. (No workspace-local SPs/
# groups: the customer has none.)


# ───────────────────── B-scenario gap fixtures (PLAN_13) ────────────────────

def phase_b3_policy_family():
    """B3: a cluster policy created FROM a policy family (family_id + overrides, NOT a raw def).

    Import must send `policy_family_id` + overrides and DROP `definition` (sending both 400s).
    """
    print("== B3: policy-family cluster policy ==")
    name = "wsmig_test_policy_family"
    if any((p.name == name) for p in w.cluster_policies.list()):
        log(f"policy exists: {name}")
        return
    try:
        fams = list(w.policy_families.list())
        if not fams:
            log("no policy families available on this workspace — skipping B3 fixture")
            return
        fam = next((f for f in fams if f.policy_family_id in
                    ("personal-vm", "job-cluster", "shared-compute")), fams[0])
        overrides = {"autotermination_minutes": {"type": "fixed", "value": 30}}
        pol = w.cluster_policies.create(name=name, policy_family_id=fam.policy_family_id,
                                        policy_family_definition_overrides=json.dumps(overrides))
        log(f"policy-family policy: {name} ({pol.policy_id}) family={fam.policy_family_id}")
    except Exception as e:
        log(f"b3 policy family: {str(e)[:140]}")


def phase_b7_dashboards():
    """B7: the AI/BI dashboard publish/schedule matrix.

    Adds, alongside the base draft dashboard:
      • a PUBLISHED dashboard that EMBEDS credentials (viewers run as the publisher's identity),
      • a PUBLISHED dashboard WITHOUT embedded credentials (viewers use their own identity),
      • a dashboard with a SCHEDULE (so schedule migration has something to carry).
    """
    print("== B7: dashboard publish/schedule matrix ==")
    from databricks.sdk.service.dashboards import Dashboard
    wh = _warehouse_id()
    serialized = _valid_dashboard_serialized()
    existing = _active_dashboards()

    def _ensure(name):
        if name in existing:
            log(f"dashboard exists: {name} ({existing[name]})")
            return existing[name]
        d = w.lakeview.create(dashboard=Dashboard(display_name=name, warehouse_id=wh,
                                                  serialized_dashboard=serialized))
        log(f"dashboard: {name} ({d.dashboard_id})")
        return d.dashboard_id

    # Published, embedded credentials (viewer runs as publisher).
    did_embed = _ensure("wsmig_test_dash_published_embed")
    try:
        w.lakeview.publish(dashboard_id=did_embed, embed_credentials=True, warehouse_id=wh)
        log("  published (embed_credentials=True)")
    except Exception as e:
        log(f"  publish embed: {str(e)[:110]}")

    # Published, NO embedded credentials (viewer uses own identity).
    did_noembed = _ensure("wsmig_test_dash_published_noembed")
    try:
        w.lakeview.publish(dashboard_id=did_noembed, embed_credentials=False, warehouse_id=wh)
        log("  published (embed_credentials=False)")
    except Exception as e:
        log(f"  publish noembed: {str(e)[:110]}")

    # A dashboard carrying a SCHEDULE. A schedule requires the dashboard to be PUBLISHED first
    # (create_schedule 404s "Unable to find published dashboard" on a draft-only dashboard).
    did_sched = _ensure("wsmig_test_dash_scheduled")
    try:
        w.lakeview.publish(dashboard_id=did_sched, embed_credentials=True, warehouse_id=wh)
    except Exception as e:
        log(f"  schedule-publish: {str(e)[:110]}")
    try:
        from databricks.sdk.service.dashboards import Schedule, CronSchedule
        w.lakeview.create_schedule(dashboard_id=did_sched,
                                   schedule=Schedule(cron_schedule=CronSchedule(
                                       quartz_cron_expression="0 0 8 * * ?",
                                       timezone_id="UTC")))
        log("  schedule created on wsmig_test_dash_scheduled")
    except Exception as e:
        log(f"  schedule: {str(e)[:110]}")


def phase_incremental():
    """INCREMENTAL Run-2 seed — mutate the EXISTING bed so a re-run exercises change-detection +
    UPSERT across every axis, plus one deliberate FAILURE that heals via a retry_mode=failed_only
    import-only run. NOT part of `all` (it would corrupt a clean rebuild); run explicitly:
    `python3 tests/fixtures_medium.py incremental`.

      A. ADD        — new notebooks + new workspace files (no prior state → export fetches, import creates)
      B. UPDATE     — overwrite existing notebooks + files (bytes change → modified_at bumps → fingerprint
                      moves → export re-fetches ONLY these; import UPDATES; unchanged content skips)
      C. DASHBOARD  — a NEW draft dashboard, then PUBLISH it (B7 publish-diff migration)
      D. POLICY     — edit an existing cluster policy's definition (metadata UPSERT, not a duplicate)
      E. JOB        — edit an existing job's settings (metadata UPSERT)
      F. USER       — add a new account user + assign to the workspace (new identity in the roster)
      G. ENTITLEMENT— add an entitlement to an existing user (entitlement diff applied on import)
      H. FAILURE    — install a NEW cluster library on a TERMINATED cluster → fails prerequisite_missing
                      on import; heals via an import-only run with retry_mode=failed_only +
                      library_force_start_clusters=true.
    Idempotent: a timestamp stamp makes each run move the fingerprint of the updated items."""
    from databricks.sdk.service import workspace, iam
    from databricks.sdk.service import jobs as jobs_svc
    from databricks.sdk.service.dashboards import Dashboard
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print("== INCREMENTAL seed (Run-2) ==")

    # A. ADDITIONS — new notebooks + files (no prior state row)
    for path, src in {f"{SHARED}/inc_nb_added_1": f"# Databricks notebook source\nprint('inc add 1 {stamp}')\n",
                      f"{USERDIR}/inc_nb_added_2": f"# Databricks notebook source\nprint('inc add 2 {stamp}')\n"}.items():
        try:
            w.workspace.import_(path=path, language=workspace.Language.PYTHON,
                                format=workspace.ImportFormat.SOURCE,
                                content=base64.b64encode(src.encode()).decode(), overwrite=True)
            log(f"  A add notebook: {path}")
        except Exception as e:
            log(f"  A add nb {path}: {str(e)[:70]}")
    for path, content in {f"{SHARED}/inc_added_file_1.csv": f"a,b\n{stamp},1\n".encode(),
                          f"{SHARED}/inc_added_file_2.txt": f"added {stamp}\n".encode()}.items():
        try:
            w.workspace.upload(path=path, content=content,
                               format=workspace.ImportFormat.RAW, overwrite=True)
            log(f"  A add file: {path}")
        except Exception as e:
            log(f"  A add file {path}: {str(e)[:70]}")

    # B. UPDATES — overwrite existing content (fingerprint moves; unchanged siblings must skip)
    for path, lang, src in [
            (f"{SHARED}/py_nb", workspace.Language.PYTHON, f"# Databricks notebook source\nprint('UPDATED {stamp}')\n"),
            (f"{SHARED}/sql_nb", workspace.Language.SQL, f"-- Databricks notebook source\nSELECT 2 AS x -- updated {stamp}\n"),
            (f"{SHARED}/scala_nb", workspace.Language.SCALA, f"// Databricks notebook source\nprintln(\"updated {stamp}\")\n")]:
        try:
            w.workspace.import_(path=path, language=lang, format=workspace.ImportFormat.SOURCE,
                                content=base64.b64encode(src.encode()).decode(), overwrite=True)
            log(f"  B update notebook: {path}")
        except Exception as e:
            log(f"  B update nb {path}: {str(e)[:70]}")
    for path, content in {f"{SHARED}/config.json": f'{{"key":"value","updated":"{stamp}"}}\n'.encode(),
                          f"{SHARED}/README.md": f"# wsmig test (updated {stamp})\n".encode()}.items():
        try:
            w.workspace.upload(path=path, content=content,
                               format=workspace.ImportFormat.RAW, overwrite=True)
            log(f"  B update file: {path}")
        except Exception as e:
            log(f"  B update file {path}: {str(e)[:70]}")

    # C. DASHBOARD — new draft, then PUBLISH
    try:
        wh = _warehouse_id()
        name = "wsmig_inc_dash_published"
        did = _active_dashboards().get(name)
        if not did:
            d = w.lakeview.create(dashboard=Dashboard(display_name=name, warehouse_id=wh,
                                                      serialized_dashboard=_valid_dashboard_serialized()))
            did = d.dashboard_id
            log(f"  C dashboard created (draft): {name} ({did})")
        w.lakeview.publish(dashboard_id=did, embed_credentials=True, warehouse_id=wh)
        log(f"  C dashboard PUBLISHED: {name}")
    except Exception as e:
        log(f"  C dashboard: {str(e)[:110]}")

    # D. POLICY — edit an existing cluster policy definition (metadata UPSERT)
    try:
        pol = next((p for p in w.cluster_policies.list() if p.name == "wsmig_test_policy"), None)
        if pol:
            d = json.loads(pol.definition)
            d["autotermination_minutes"] = {"type": "fixed", "value": 30}
            w.cluster_policies.edit(policy_id=pol.policy_id, name=pol.name, definition=json.dumps(d))
            log("  D policy updated: wsmig_test_policy (autotermination_minutes=30)")
    except Exception as e:
        log(f"  D policy: {str(e)[:90]}")

    # E. JOB — change an existing job's settings (metadata UPSERT)
    try:
        job = next((j for j in w.jobs.list() if j.settings.name == "wsmig_test_single_job"), None)
        if job:
            w.jobs.update(job_id=job.job_id,
                          new_settings=jobs_svc.JobSettings(timeout_seconds=5400,
                                                            tags={"wsmig_incremental": stamp}))
            log("  E job updated: wsmig_test_single_job (timeout + tag)")
    except Exception as e:
        log(f"  E job: {str(e)[:90]}")

    # F. USER — add a new account user + assign to the workspace
    try:
        a = _acct()
        new_email = "ai27.inc.user@databricks.com"
        u = next(iter(a.users.list(filter=f'userName eq "{new_email}"')), None)
        if not u:
            u = a.users.create(user_name=new_email, display_name="AI27 Incremental User", active=True)
            log(f"  F account user created: {new_email} ({u.id})")
        _assign_to_workspace(u.id)
        log(f"  F user assigned to workspace: {new_email}")
    except Exception as e:
        log(f"  F add user: {str(e)[:110]}")

    # G. ENTITLEMENT — add an entitlement to an existing user (diff applied on import)
    try:
        email = ENTRA_USERS[0]  # had only allow-cluster-create
        ws_user = next(iter(w.users.list(filter=f'userName eq "{email}"')), None)
        if ws_user:
            w.users.patch(id=ws_user.id,
                          schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                          operations=[iam.Patch(op=iam.PatchOp.ADD, path="entitlements",
                                                value=[{"value": "databricks-sql-access"}])])
            log(f"  G entitlement added to {email}: databricks-sql-access")
    except Exception as e:
        log(f"  G entitlement: {str(e)[:90]}")

    # H. FAILURE — new cluster library on a TERMINATED cluster (heals via failed_only + force_start)
    try:
        cid = next((c.cluster_id for c in w.clusters.list()
                    if c.cluster_name == "wsmig_test_cluster_singlenode"), None)
        if cid:
            w.api_client.do("POST", "/api/2.0/libraries/install",
                            body={"cluster_id": cid, "libraries": [{"pypi": {"package": "six==1.16.0"}}]})
            log("  H failure-seed: pypi six on wsmig_test_cluster_singlenode (will FAIL on terminated "
                "cluster; heal via retry_mode=failed_only + library_force_start_clusters=true)")
    except Exception as e:
        log(f"  H failure library: {str(e)[:90]}")


# ─────────────────────────── orphaned users ──────────────────────────────────

def phase_orphaned_users():
    """Remove 2 of the 15 Entra users from workspace assignment (BEFORE acls, so their homes are
    deleted-in-source on import). This exercises the PLAN 9 orphaned-home backup path.
    Re-runs are idempotent: if they are already unassigned, skip.
    """
    print("== orphaned users (remove 2 from workspace) ==")
    a = _acct()
    # Remove ENTRA_USERS[11] and ENTRA_USERS[13] (configurable)
    to_remove = list(ORPHAN_USERS)
    for email in to_remove:
        acct_user = next(iter(a.users.list(filter=f'userName eq "{email}"')), None)
        if not acct_user:
            log(f"  {email}: not in account")
            continue
        try:
            # Use account-level permissionassignments to remove (workspace-scoped removal)
            # This removes the user from the workspace assignment, leaving them in account.
            acct_id = a.config.account_id
            a.api_client.do("DELETE",
                           f"/api/2.0/accounts/{acct_id}/workspaces/{w.get_workspace_id()}/permissionassignments/"
                           f"principals/{acct_user.id}")
            log(f"  removed from workspace: {email}")
        except Exception as e:
            log(f"  {email}: {str(e)[:90]}")


# ─────────────────────────── check phase ──────────────────────────────────────

def phase_check():
    """Read-only prerequisite report. Checks CLI profiles, account access, identities, catalog.
    Prints FLAGS for any missing items (user must create); never aborts the whole run.
    """
    print("== check (prerequisites) ==")
    flags = []

    # 1. CLI profiles reachable
    a = _acct()
    try:
        acct_id = a.config.account_id
        log(f"account profile {ACCT_PROFILE}: ✓ (id={acct_id})")
    except Exception as e:
        flags.append(f"account profile {ACCT_PROFILE}: {str(e)[:80]}")
        log(f"  FLAG: {flags[-1]}")

    try:
        ws_id = w.get_workspace_id()
        log(f"workspace profile {PROFILE}: ✓ (id={ws_id})")
    except Exception as e:
        flags.append(f"workspace profile {PROFILE}: {str(e)[:80]}")
        log(f"  FLAG: {flags[-1]}")

    # 2. Entra users present
    missing_users = []
    for email in ENTRA_USERS:
        u = next(iter(a.users.list(filter=f'userName eq "{email}"')), None)
        if not u:
            missing_users.append(email)

    if missing_users:
        flags.append(f"Missing {len(missing_users)}/{len(ENTRA_USERS)} Entra users")
        log(f"  FLAG: {flags[-1]}")
        for em in missing_users[:3]:
            log(f"    - {em}")
    else:
        log(f"Entra users: ✓ ({len(ENTRA_USERS)} present)")

    # 3. UMIs + Entra groups: present in the ACCOUNT and ASSIGNED to this workspace (the user adds
    #    them; no Azure call).
    assigned = _assigned_principal_ids()
    missing_umis = []
    for umi_name in UMI_SP_NAMES:
        cands = list(a.service_principals.list(filter=f'displayName eq "{umi_name}"'))
        if not any(str(s.id) in assigned for s in cands):
            missing_umis.append(umi_name)

    if missing_umis:
        flags.append(f"Missing UMIs: {', '.join(missing_umis)}")
        log(f"  FLAG: {flags[-1]}")
    else:
        log(f"UMIs: ✓ ({len(UMI_SP_NAMES)} present)")

    # 4. Entra groups present (looked up via az ad group show)
    missing_entra_grps = []
    for grp in ENTRA_GROUP_NAMES:
        g = next(iter(a.groups.list(filter=f'displayName eq "{grp}"')), None)
        if not g or str(g.id) not in assigned or not g.external_id:
            missing_entra_grps.append(grp)

    if missing_entra_grps:
        flags.append(f"Missing Entra groups: {', '.join(missing_entra_grps)}")
        log(f"  FLAG: {flags[-1]}")
    else:
        log(f"Entra groups: ✓ ({len(ENTRA_GROUP_NAMES)} present)")

    # 5. UC catalog present
    try:
        cat = _catalog()
        log(f"UC catalog: ✓ ({cat})")
    except Exception as e:
        flags.append(f"UC catalog: {str(e)[:80]}")
        log(f"  FLAG: {flags[-1]}")

    # Summary
    if flags:
        log(f"\n✗ {len(flags)} FLAG(s) — user action required before migration")
        for f in flags:
            log(f"  • {f}")
    else:
        log(f"\n✓ All prerequisites present")


# Dependency-ordered: identity before ACLs (needs principals), warehouses+uc before anything
# that references a warehouse or table, compute+workspace before jobs/libraries, and acls LAST
# so every object it grants on already exists.

# ─────────────────────────── PLAN 16.0 additions ────────────────────────────────────────────────
# Per-user content, per-user queries, cross-user ACLs, the full dashboard matrix (bar + counter +
# filter), and the target-side UC prep. Added on top of the proven reference phases.

_NB_PY = ("# Databricks notebook source\n# owner: {u}\nimport datetime\n"
          "print('wsmig fixture for {u}', datetime.date.today())\n\n# COMMAND ----------\n\n"
          "rows = [(i, i * i) for i in range(5)]\nprint(rows)\n")
_NB_SQL = ("-- Databricks notebook source\n-- owner: {u}\nSELECT current_user() AS who, 1 AS x\n\n"
           "-- COMMAND ----------\n\nSELECT * FROM {cat}.{sch}.trips LIMIT 10\n")
_NB_SCALA = ("// Databricks notebook source\n// owner: {u}\nval xs = (1 to 5).map(i => i * 2)\n"
             "println(xs)\n\n// COMMAND ----------\n\nprintln(\"wsmig scala for {u}\")\n")
_FILE_PY = "# helper module for {u}\n\ndef add(a, b):\n    return a + b\n"
_FILE_CSV = "zip,trips,owner\n94103,120,{u}\n94107,88,{u}\n"


def _user_base(email: str) -> str:
    return f"/Users/{email}/wsmig"


def _put_notebook(path: str, lang: str, src: str) -> None:
    from databricks.sdk.service import workspace
    w.workspace.import_(path=path, language=getattr(workspace.Language, lang),
                        format=workspace.ImportFormat.SOURCE,
                        content=base64.b64encode(src.encode()).decode(), overwrite=True)


def _put_file(path: str, text: str) -> None:
    from databricks.sdk.service import workspace
    w.workspace.import_(path=path, format=workspace.ImportFormat.RAW,
                        content=base64.b64encode(text.encode()).decode(), overwrite=True)


def phase_users_content():
    """PER-USER content for ALL 15 users (incl. the 2 future orphans): under /Users/<email>/wsmig/
    a Python, SQL and Scala notebook with real cells, a .py and a .csv file, and a sub-folder with
    one more notebook. Written as the admin runner; importing into a not-yet-provisioned home
    creates it (verified live 2026-10-02). overwrite=True → idempotent."""
    print("== per-user content (15 users) ==")
    ok = fail = 0
    for email in ENTRA_USERS:
        base = _user_base(email)
        items = [
            ("nb", f"{base}/py_nb", "PYTHON", _NB_PY.format(u=email)),
            ("nb", f"{base}/sql_nb", "SQL", _NB_SQL.format(u=email, cat=CATALOG, sch=SCHEMA)),
            ("nb", f"{base}/scala_nb", "SCALA", _NB_SCALA.format(u=email)),
            ("file", f"{base}/helpers.py", None, _FILE_PY.format(u=email)),
            ("file", f"{base}/data.csv", None, _FILE_CSV.format(u=email)),
            ("nb", f"{base}/sub/sub_nb", "PYTHON", _NB_PY.format(u=email)),
        ]
        try:
            w.workspace.mkdirs(f"{base}/sub")
        except Exception as e:
            log(f"  mkdirs {base}/sub: {str(e)[:100]}")
        for kind, path, lang, src in items:
            try:
                _put_notebook(path, lang, src) if kind == "nb" else _put_file(path, src)
                ok += 1
            except Exception as e:
                fail += 1
                log(f"  {path}: {str(e)[:110]}")
    log(f"per-user content: {ok} objects written, {fail} failed ({len(ENTRA_USERS)} users)")


def phase_user_queries():
    """1 legacy SQL query per user, in that user's folder, + 2 in /Shared (spec)."""
    print("== per-user + shared queries ==")
    from databricks.sdk.service.sql import CreateQueryRequestQuery
    wh = _warehouse_id()
    have = set()
    try:
        have = {q.display_name for q in w.queries.list()}
    except Exception as e:
        log(f"queries list: {str(e)[:90]}")
    targets = [(f"wsmig_q_{e.split('@')[0].replace('.', '_')}", _user_base(e)) for e in ENTRA_USERS]
    targets += [("wsmig_q_shared_1", SHARED), ("wsmig_q_shared_2", SHARED)]
    for name, parent in targets:
        if name in have:
            log(f"  query exists: {name}")
            continue
        try:
            q = w.queries.create(query=CreateQueryRequestQuery(
                display_name=name, warehouse_id=wh, parent_path=parent,
                query_text=f"SELECT zip, SUM(trips) AS trips FROM {CATALOG}.{SCHEMA}.trips GROUP BY zip"))
            log(f"  query: {name} in {parent} ({q.id})")
        except Exception as e:
            log(f"  query {name}: {str(e)[:110]}")


def _rich_dashboard_serialized() -> str:
    """A rendering dashboard with a BAR chart + a COUNTER + a single-select FILTER on `zip`.

    Same rendering rules as `_valid_dashboard_serialized` (each visual widget's query is named
    `main_query`; encodings' fieldName must match a query field). The filter binds to the dataset's
    `zip` column. NOTE: the create API accepts invalid widgets, so rendering is confirmed by
    executing each widget's query (`_verify_dashboard_queries`) AND by opening the dashboard."""
    ds = "ds_trips"
    return json.dumps({
        "datasets": [{"name": ds, "displayName": "trips",
                      "queryLines": [f"SELECT * FROM {CATALOG}.{SCHEMA}.trips"]}],
        "pages": [{"name": "page_main", "displayName": "Overview", "layout": [
            {"widget": {"name": "filter_zip",
                        "queries": [{"name": "filter_zip_query", "query": {
                            "datasetName": ds,
                            "fields": [{"name": "zip", "expression": "`zip`"}],
                            "disaggregated": False}}],
                        "spec": {"version": 2, "widgetType": "filter-single-select",
                                 "encodings": {"fields": [{"fieldName": "zip", "displayName": "zip",
                                                           "queryName": "filter_zip_query"}]},
                                 "frame": {"showTitle": True, "title": "Zip"}}},
             "position": {"x": 0, "y": 0, "width": 2, "height": 2}},
            {"widget": {"name": "counter_trips",
                        "queries": [{"name": "main_query", "query": {
                            "datasetName": ds,
                            "fields": [{"name": "trip_count", "expression": "COUNT(`zip`)"}],
                            "disaggregated": False}}],
                        "spec": {"version": 2, "widgetType": "counter",
                                 "encodings": {"value": {"fieldName": "trip_count",
                                                         "displayName": "Trip count"}}}},
             "position": {"x": 2, "y": 0, "width": 2, "height": 4}},
            {"widget": {"name": "bar_trips_by_zip",
                        "queries": [{"name": "main_query", "query": {
                            "datasetName": ds,
                            "fields": [{"name": "zip", "expression": "`zip`"},
                                       {"name": "sum(trips)", "expression": "SUM(`trips`)"}],
                            "disaggregated": False}}],
                        "spec": {"version": 3, "widgetType": "bar",
                                 "encodings": {
                                     "x": {"fieldName": "zip", "scale": {"type": "categorical"},
                                           "displayName": "zip"},
                                     "y": {"fieldName": "sum(trips)",
                                           "scale": {"type": "quantitative"},
                                           "displayName": "Trips"}},
                                 "frame": {"showTitle": True, "title": "Trips by zip"}}},
             "position": {"x": 0, "y": 4, "width": 6, "height": 6}}]}],
    })


def _verify_dashboard_queries(wh: str) -> bool:
    """Execute the SQL behind every widget of the rich dashboard; all must return rows."""
    sqls = [f"SELECT COUNT(zip) AS trip_count FROM {CATALOG}.{SCHEMA}.trips",
            f"SELECT zip, SUM(trips) FROM {CATALOG}.{SCHEMA}.trips GROUP BY zip",
            f"SELECT DISTINCT zip FROM {CATALOG}.{SCHEMA}.trips"]
    good = True
    for q in sqls:
        r = w.statement_execution.execute_statement(warehouse_id=wh, statement=q, wait_timeout="30s")
        rows = (getattr(r.result, "data_array", None) or []) if r.result else []
        state = r.status.state.value
        log(f"  render-check {state} rows={len(rows)}: {q[:60]}")
        good = good and state == "SUCCEEDED" and len(rows) > 0
    return good


def phase_dash_matrix():
    """The 6 dashboard combinations (spec), each with bar + counter + filter:
    draft only · published Shared (embed) · published Individual · published+scheduled ·
    published+scheduled+ACLs. (The 6th — DAB-deployed — comes from the dab phases.)"""
    print("== dashboard matrix (bar + counter + filter) ==")
    from databricks.sdk.service.dashboards import Dashboard, Schedule, CronSchedule
    wh = _warehouse_id()
    if not _verify_dashboard_queries(wh):
        log("FLAG: dashboard widget queries do not all return rows — dashboards will render empty")
    ser = _rich_dashboard_serialized()
    existing = _active_dashboards()

    def ensure(name):
        if name in existing:
            log(f"  dashboard exists: {name}")
            return existing[name]
        d = w.lakeview.create(dashboard=Dashboard(display_name=name, warehouse_id=wh,
                                                  serialized_dashboard=ser, parent_path=SHARED))
        log(f"  dashboard: {name} ({d.dashboard_id})")
        return d.dashboard_id

    def publish(did, embed):
        try:
            w.lakeview.publish(dashboard_id=did, embed_credentials=embed, warehouse_id=wh)
        except Exception as e:
            log(f"  publish {did}: {str(e)[:110]}")

    def schedule(did, cron):
        try:
            have = list(w.lakeview.list_schedules(dashboard_id=did))
            if not have:
                w.lakeview.create_schedule(dashboard_id=did, schedule=Schedule(
                    cron_schedule=CronSchedule(quartz_cron_expression=cron, timezone_id="UTC")))
        except Exception as e:
            log(f"  schedule {did}: {str(e)[:110]}")

    ensure("wsmig_dash_draft")
    publish(ensure("wsmig_dash_pub_shared"), True)
    publish(ensure("wsmig_dash_pub_individual"), False)
    d = ensure("wsmig_dash_pub_sched")
    publish(d, True); schedule(d, "0 0 8 * * ?")
    d = ensure("wsmig_dash_pub_sched_acl")
    publish(d, True); schedule(d, "0 30 9 * * ?")
    acl = []
    if ENTRA_USERS:
        acl.append({"user_name": ENTRA_USERS[1], "permission_level": "CAN_MANAGE"})
    acl.append({"group_name": ACCOUNT_NESTED_PARENT, "permission_level": "CAN_RUN"})
    sp = next(iter(w.service_principals.list(filter=f'displayName eq "{ACCOUNT_SP_NAMES[1]}"')), None)
    if sp:
        acl.append({"service_principal_name": sp.application_id, "permission_level": "CAN_READ"})
    try:
        w.api_client.do("PATCH", f"/api/2.0/permissions/dashboards/{d}",
                        body={"access_control_list": acl})
        log(f"  ACLs on wsmig_dash_pub_sched_acl: {len(acl)} grants")
    except Exception as e:
        log(f"  dashboard ACLs: {str(e)[:110]}")
    log("Open the 5 wsmig_dash_* dashboards once in the UI to confirm they render (the API "
        "accepts invalid widgets).")


DEST_ADOPT = "wsmig_test_dest"            # also pre-created on TARGET → the adopt-by-name path
DEST_MISSING = "wsmig_test_dest_missing"  # source only → the "create destination on target" line


def _ensure_email_destination(client, name: str, email: str) -> str:
    """Idempotent EMAIL notification destination by display_name → its id."""
    have = client.api_client.do("GET", "/api/2.0/notification-destinations") or {}
    for d in have.get("results") or []:
        if d.get("display_name") == name:
            return d["id"]
    d = client.api_client.do("POST", "/api/2.0/notification-destinations", body={
        "display_name": name, "config": {"email": {"addresses": [email]}}})
    log(f"  notification destination created: {name} ({d['id']})")
    return d["id"]


def phase_dash_subscriptions():
    """PLAN 16.2 §8 step 0 — schedule SUBSCRIBERS on the source (the bed had none, F14):
    • `wsmig_dash_pub_sched` (publisher credentials): user subscriber tanveer.singh + destination
      subscribers DEST_ADOPT (pre-created on target too → adopted) and DEST_MISSING (source only →
      manual line).
    • NEW `wsmig_dash_pub_sched_viewer` (viewer credentials, published, one schedule): the CLI user
      self-subscribes (the only subscription a viewer-credential dashboard allows, F12) + 2 ACL grants.
    Idempotent: re-adding a subscriber returns the same subscription (F11)."""
    print("== dashboard schedule subscriptions ==")
    from databricks.sdk.service.dashboards import Dashboard, Schedule, CronSchedule
    wh = _warehouse_id()
    existing = _active_dashboards()
    me = w.current_user.me()

    def sub(did, sid, subscriber, label):
        try:
            w.api_client.do("POST", f"/api/2.0/lakeview/dashboards/{did}/schedules/{sid}/subscriptions",
                            body={"subscriber": subscriber})
            log(f"  subscribed {label}")
        except Exception as e:
            log(f"  FLAG subscribe {label}: {str(e)[:160]}")

    def first_schedule(did, cron):
        have = list(w.lakeview.list_schedules(dashboard_id=did))
        if have:
            return have[0].schedule_id
        return w.lakeview.create_schedule(dashboard_id=did, schedule=Schedule(
            cron_schedule=CronSchedule(quartz_cron_expression=cron, timezone_id="UTC"))).schedule_id

    # 1. publisher-credential dashboard: user + 2 destinations
    did = existing.get("wsmig_dash_pub_sched")
    if not did:
        log("FLAG: wsmig_dash_pub_sched missing — run dash_matrix first")
        return
    sid = first_schedule(did, "0 0 8 * * ?")
    tanveer = next(iter(w.users.list(filter='userName eq "tanveer.singh@databricks.com"')), None)
    if tanveer:
        sub(did, sid, {"user_subscriber": {"user_id": tanveer.id}}, "tanveer.singh → wsmig_dash_pub_sched")
    else:
        log("FLAG: tanveer.singh not on source")
    for name in (DEST_ADOPT, DEST_MISSING):
        dest = _ensure_email_destination(w, name, me.user_name)
        sub(did, sid, {"destination_subscriber": {"destination_id": dest}}, f"{name} → wsmig_dash_pub_sched")

    # 2. viewer-credential dashboard, self-subscribed
    name = "wsmig_dash_pub_sched_viewer"
    vid = existing.get(name)
    if not vid:
        vid = w.lakeview.create(dashboard=Dashboard(display_name=name, warehouse_id=wh,
                                                    serialized_dashboard=_rich_dashboard_serialized(),
                                                    parent_path=SHARED)).dashboard_id
        log(f"  dashboard: {name} ({vid})")
    try:
        pub = w.api_client.do("GET", f"/api/2.0/lakeview/dashboards/{vid}/published")
    except Exception:
        pub = None
    if not pub or pub.get("embed_credentials") is not False:
        w.lakeview.publish(dashboard_id=vid, embed_credentials=False, warehouse_id=wh)
        log(f"  published {name} (viewer credentials)")
    vsid = first_schedule(vid, "0 15 7 * * ?")
    sub(vid, vsid, {"user_subscriber": {"user_id": me.id}}, f"{me.user_name} (self) → {name}")
    acl = [{"group_name": ACCOUNT_NESTED_PARENT, "permission_level": "CAN_RUN"}]
    if len(ENTRA_USERS) > 2:
        acl.append({"user_name": ENTRA_USERS[2], "permission_level": "CAN_EDIT"})
    w.api_client.do("PATCH", f"/api/2.0/permissions/dashboards/{vid}", body={"access_control_list": acl})
    log(f"  ACLs on {name}: {len(acl)} grants")


RETRY_JOB = "wsmig_qa_retry_job"
RETRY_SP = "ai27_acc_spn_2"     # the runner has servicePrincipal.manager but NOT .user on it
RETRY_GENIE = "wsmig_qa_retry_genie"
RETRY_TABLE = "retry_tbl"       # created on SOURCE only; on target only by `retry_heal`


def phase_retry_seed():
    """PLAN 16.2 §8 step 4 (user-approved 2026-10-05) — two NEW source objects that FAIL on import
    until healed, each with its own ACL, so `retry_mode=failed_only` proves the object AND its ACL
    come back in the same run (§5a):
      • RETRY_JOB: run_as RETRY_SP; the runner lacks `servicePrincipal.user` on it → create refused.
      • RETRY_GENIE: uses `<catalog>.wsmig_test.retry_tbl`, which does not exist on target → create fails.
    Heal = `retry_heal`. Idempotent; EXCLUDED from `all`."""
    print("== retry seed: run-as job + Genie space that fail on target until healed ==")
    from databricks.sdk.service import jobs
    by_name = {j.settings.name: j.job_id for j in w.jobs.list()}
    sp = next(iter(w.service_principals.list(filter=f'displayName eq "{RETRY_SP}"')), None)
    if not sp:
        log(f"FLAG: {RETRY_SP} not on the source workspace")
        return
    jid = by_name.get(RETRY_JOB)
    if jid:
        log(f"job exists: {RETRY_JOB} ({jid})")
    else:
        _ensure_sp_user_role(sp.application_id)        # the CLI user (source creator) only
        jid = w.jobs.create(name=RETRY_JOB,
                            run_as=jobs.JobRunAs(service_principal_name=sp.application_id),
                            tasks=[jobs.Task(task_key="t1",
                                             notebook_task=jobs.NotebookTask(
                                                 notebook_path=f"{SHARED}/py_nb"),
                                             new_cluster=_job_cluster())]).job_id
        log(f"job: {RETRY_JOB} ({jid}) run_as={RETRY_SP}")
    acl = [{"group_name": ACCOUNT_NESTED_PARENT, "permission_level": "CAN_MANAGE_RUN"},
           {"user_name": ENTRA_USERS[3], "permission_level": "CAN_VIEW"}]
    w.api_client.do("PATCH", f"/api/2.0/permissions/jobs/{jid}", body={"access_control_list": acl})
    log(f"  ACLs on {RETRY_JOB}: {len(acl)} grants")

    # Genie space on a source-only table
    wh = _warehouse_id()
    fq = f"{CATALOG}.{SCHEMA}.{RETRY_TABLE}"
    r = w.statement_execution.execute_statement(
        warehouse_id=wh, wait_timeout="50s",
        statement=f"CREATE TABLE IF NOT EXISTS {fq} (id INT, label STRING)")
    log(f"  source table {fq}: {r.status.state.value}")
    spaces = w.api_client.do("GET", "/api/2.0/genie/spaces").get("spaces", []) or []
    gid = next((s["space_id"] for s in spaces if s.get("title") == RETRY_GENIE), None)
    if gid:
        log(f"genie exists: {RETRY_GENIE} ({gid})")
    else:
        import json as _json
        ser = {"version": 1, "data_sources": {"tables": [{"identifier": fq}]}}
        gid = w.api_client.do("POST", "/api/2.0/genie/spaces", body={
            "title": RETRY_GENIE, "description": "PLAN 16.2 retry seed", "warehouse_id": wh,
            "serialized_space": _json.dumps(ser)})["space_id"]
        log(f"genie: {RETRY_GENIE} ({gid}) on {fq}")
    gacl = [{"group_name": ACCOUNT_NESTED_PARENT, "permission_level": "CAN_RUN"},
            {"user_name": ENTRA_USERS[4], "permission_level": "CAN_EDIT"}]
    w.api_client.do("PATCH", f"/api/2.0/permissions/genie/{gid}", body={"access_control_list": gacl})
    log(f"  ACLs on {RETRY_GENIE}: {len(gacl)} grants")


def phase_retry_heal():
    """Heal for `retry_seed` (user-approved 2026-10-05), run right before the `failed_only` retry:
    grant the RUNNER `servicePrincipal.user` on RETRY_SP (account level, additive) and create the
    EMPTY RETRY_TABLE on target. EXCLUDED from `all`."""
    print("== retry heal ==")
    runner = "servicePrincipals/f09b29b1-7924-4b53-ac73-15af18c4f087"
    sp = next(iter(w.service_principals.list(filter=f'displayName eq "{RETRY_SP}"')), None)
    acct = _acct().config.account_id
    name = f"accounts/{acct}/servicePrincipals/{sp.application_id}/ruleSets/default"
    rs = w.api_client.do("GET", "/api/2.0/preview/accounts/access-control/rule-sets",
                         query={"name": name, "etag": ""})
    rules = rs.get("grant_rules", []) or []
    r = next((x for x in rules if x.get("role") == "roles/servicePrincipal.user"), None)
    if r and runner in (r.get("principals") or []):
        log("  runner already has servicePrincipal.user")
    else:
        if r:
            r["principals"] = list(r.get("principals") or []) + [runner]
        else:
            rules.append({"role": "roles/servicePrincipal.user", "principals": [runner]})
        w.api_client.do("PUT", "/api/2.0/preview/accounts/access-control/rule-sets",
                        body={"name": name, "rule_set": {"name": name, "grant_rules": rules,
                                                         "etag": rs.get("etag", "")}})
        log(f"  servicePrincipal.user granted to the runner on {RETRY_SP}")
    tw = WorkspaceClient(profile=TARGET_PROFILE)
    twh = next(iter(tw.warehouses.list())).id
    cat = os.environ.get("WSMIG_TARGET_CATALOG") or CATALOG
    res = tw.statement_execution.execute_statement(
        warehouse_id=twh, wait_timeout="50s",
        statement=f"CREATE TABLE IF NOT EXISTS {cat}.{SCHEMA}.{RETRY_TABLE} (id INT, label STRING)")
    while res.status.state.value in ("PENDING", "RUNNING"):     # warehouse may be starting
        time.sleep(3)
        res = tw.statement_execution.get_statement(res.statement_id)
    log(f"  target table {cat}.{SCHEMA}.{RETRY_TABLE}: {res.status.state.value}")


def phase_target_dest():
    """TARGET side (PLAN 16.2 §8): pre-create DEST_ADOPT so the destination subscriber is adopted by
    name; DEST_MISSING is deliberately NOT created (→ the manual line)."""
    print("== target notification destination ==")
    tw = WorkspaceClient(profile=TARGET_PROFILE)
    _ensure_email_destination(tw, DEST_ADOPT, tw.current_user.me().user_name)
    log(f"  target has {DEST_ADOPT}; {DEST_MISSING} intentionally absent")


def _obj_id(path: str):
    try:
        return w.workspace.get_status(path).object_id
    except Exception as e:
        log(f"  get-status {path}: {str(e)[:90]}")
        return None


def phase_cross_acls():
    """Cross-user ACLs (spec, explicit): user2's notebooks → user1 CAN_MANAGE; grp_1 → CAN_MANAGE on
    user4's and user5's wsmig dirs; an Entra group → CAN_RUN on a /Shared folder; an SP → CAN_EDIT on
    a user notebook. Plus IS_OWNER on a job, a pipeline and a warehouse held by another user."""
    print("== cross-user ACLs ==")
    if len(ENTRA_USERS) < 5:
        log("FLAG: need 5+ users for cross-user ACLs")
        return
    u1, u2, u4, u5 = ENTRA_USERS[0], ENTRA_USERS[1], ENTRA_USERS[3], ENTRA_USERS[4]

    def grant(obj_type, oid, acl, label):
        if not oid:
            return
        try:
            w.api_client.do("PATCH", f"/api/2.0/permissions/{obj_type}/{oid}",
                            body={"access_control_list": acl})
            log(f"  {label}")
        except Exception as e:
            log(f"  {label}: {str(e)[:110]}")

    for nb in ("py_nb", "sql_nb", "scala_nb", "sub/sub_nb"):
        grant("notebooks", _obj_id(f"{_user_base(u2)}/{nb}"),
              [{"user_name": u1, "permission_level": "CAN_MANAGE"}], f"{u2}/{nb} → {u1} CAN_MANAGE")
    for u in (u4, u5):
        grant("directories", _obj_id(_user_base(u)),
              [{"group_name": ACCOUNT_NESTED_PARENT, "permission_level": "CAN_MANAGE"}],
              f"{ACCOUNT_NESTED_PARENT} → CAN_MANAGE on {_user_base(u)}")
    grant("directories", _obj_id(SHARED),
          [{"group_name": ENTRA_GROUP_NAMES[0], "permission_level": "CAN_RUN"}],
          f"{ENTRA_GROUP_NAMES[0]} → CAN_RUN on {SHARED}")
    sp = next(iter(w.service_principals.list(filter=f'displayName eq "{ACCOUNT_SP_NAMES[2]}"')), None)
    if sp:
        grant("notebooks", _obj_id(f"{_user_base(ENTRA_USERS[2])}/py_nb"),
              [{"service_principal_name": sp.application_id, "permission_level": "CAN_EDIT"}],
              f"{ACCOUNT_SP_NAMES[2]} → CAN_EDIT on {ENTRA_USERS[2]}/py_nb")
    # IS_OWNER held by another user (not the runner) on one job, one pipeline, one warehouse.
    # An object has exactly ONE owner, so PATCH (additive) fails and warehouses reject owner PATCH
    # outright → PUT the object's current DIRECT grants minus the old IS_OWNER, plus the new owner.
    # Named, non-DAB objects so the case is deterministic.
    owner = u2 if u2 != ME else u4
    j = next((j for j in w.jobs.list() if j.settings.name == "wsmig_test_params_job"), None)
    p = next((p for p in w.pipelines.list_pipelines() if p.name == "wsmig_test_pipeline_classic"),
             None)
    wh = next((x for x in w.warehouses.list() if x.name == "wsmig_test_wh_classic"), None)
    # A warehouse owner must hold allow-cluster-create (verified live) → ENTRA_USERS[3] has it.
    wh_owner = ENTRA_USERS[3]
    for obj_type, oid, label, owner in (
            ("jobs", j and j.job_id, "job wsmig_test_params_job", owner),
            ("pipelines", p and p.pipeline_id, "pipeline wsmig_test_pipeline_classic", owner),
            ("sql/warehouses", wh and wh.id, "warehouse wsmig_test_wh_classic", wh_owner)):
        if not oid:
            log(f"  IS_OWNER on {label}: object not found")
            continue
        try:
            doc = w.api_client.do("GET", f"/api/2.0/permissions/{obj_type}/{oid}") or {}
            keep = []
            for ace in doc.get("access_control_list", []) or []:
                direct = [pm["permission_level"] for pm in ace.get("all_permissions", []) or []
                          if not pm.get("inherited") and pm.get("permission_level") != "IS_OWNER"]
                who = {k: ace[k] for k in ("user_name", "group_name", "service_principal_name")
                       if ace.get(k)}
                if (who.get("user_name") == owner) or not direct or not who:
                    continue
                keep.append(dict(who, permission_level=direct[0]))
            keep.append({"user_name": owner, "permission_level": "IS_OWNER"})
            w.api_client.do("PUT", f"/api/2.0/permissions/{obj_type}/{oid}",
                            body={"access_control_list": keep})
            log(f"  IS_OWNER {owner} on {label} (+{len(keep) - 1} direct grants kept)")
        except Exception as e:
            log(f"  IS_OWNER {owner} on {label}: {str(e)[:140]}")


def phase_target_uc_prep():
    """TARGET side: the SOURCE catalog name + schema with EMPTY tables (UC is out of the tool's
    scope; without them dashboards/Genie/DLT don't render on target). `main` has no catalog remap,
    so the target catalog defaults to the SOURCE catalog name. If it is missing it is created with a
    MANAGED LOCATION next to the target's default catalog storage (a metastore without root storage
    rejects a plain CREATE CATALOG), or at WSMIG_TARGET_CATALOG_LOCATION. Every statement is checked.
    Env: WSMIG_TARGET_PROFILE, WSMIG_TARGET_CATALOG (default = source catalog)."""
    print("== target UC prep ==")
    tw = WorkspaceClient(profile=TARGET_PROFILE)
    cat = os.environ.get("WSMIG_TARGET_CATALOG") or CATALOG
    whs = list(tw.warehouses.list())
    if not whs:
        log("FLAG: no SQL warehouse on target to run the DDL — create one first")
        return
    wh = next((x.id for x in whs if getattr(x, "enable_serverless_compute", False)), whs[0].id)

    def run(st):
        r = tw.statement_execution.execute_statement(warehouse_id=wh, statement=st,
                                                     wait_timeout="50s")
        while r.status.state.value in ("PENDING", "RUNNING"):
            time.sleep(3)
            r = tw.statement_execution.get_statement(r.statement_id)
        ok = r.status.state.value == "SUCCEEDED"
        log(f"  target {r.status.state.value}: {st[:90]}"
            + ("" if ok else f" — {(r.status.error.message if r.status.error else '')[:200]}"))
        return ok

    if not any(c.name == cat for c in tw.catalogs.list()):
        loc = os.environ.get("WSMIG_TARGET_CATALOG_LOCATION")
        if not loc:
            default = tw.catalogs.get(tw.metastores.current().default_catalog_name)
            root = (default.storage_root or "").rstrip("/")
            loc = f"{root.rsplit('/', 1)[0]}/{cat}" if root else ""
        if not loc:
            log("FLAG: cannot derive a managed location — set WSMIG_TARGET_CATALOG_LOCATION")
            return
        if not run(f"CREATE CATALOG IF NOT EXISTS {cat} MANAGED LOCATION '{loc}'"):
            return
    for st in (f"CREATE SCHEMA IF NOT EXISTS {cat}.{SCHEMA}",
               f"CREATE TABLE IF NOT EXISTS {cat}.{SCHEMA}.trips (zip STRING, trips INT, avg_dist DOUBLE)",
               f"CREATE TABLE IF NOT EXISTS {cat}.{SCHEMA}.zones (zip STRING, borough STRING)"):
        if not run(st):
            return
    log(f"target catalog ready: {cat} (= source catalog {CATALOG})")

PHASES = {
    "identity": phase_identity,
    "warehouses": phase_warehouses,
    "uc": phase_uc,
    "compute": phase_compute,
    "workspace": phase_workspace,
    "users_content": phase_users_content,
    "secrets": phase_secrets,
    "akv": phase_akv,
    "sql": phase_sql,
    "user_queries": phase_user_queries,
    "genie": phase_genie,
    "dashboards": phase_dashboards,
    "jobs": phase_jobs,
    "dlt": phase_dlt,
    "serving": phase_serving,
    "misc": phase_misc,
    "dab": phase_dab,
    "dab_pathless": phase_dab_pathless,
    "bigfiles": phase_bigfiles,
    "libraries": phase_libraries,
    # B-scenario gap fixtures (PLAN_13) — must run BEFORE ACL matrices
    "b3_policy_family": phase_b3_policy_family,
    "b7_dashboards": phase_b7_dashboards,
    "dash_matrix": phase_dash_matrix,
    "dash_subscriptions": phase_dash_subscriptions,   # PLAN 16.2 — schedule subscribers
    # Orphaned users — runs BEFORE ACLs so their homes are deleted_in_source on import
    "orphaned_users": phase_orphaned_users,
    # ACL matrices LAST — every object they grant on now exists
    "acls": phase_acls,
    "cross_acls": phase_cross_acls,
    # Incremental Run-2 seed — EXCLUDED from `all` (mutates the baseline); run explicitly.
    "incremental": phase_incremental,
    # TARGET-side UC prep — EXCLUDED from `all`; run explicitly against the target.
    "target_uc_prep": phase_target_uc_prep,
    "target_dest": phase_target_dest,
    "retry_seed": phase_retry_seed,
    "retry_heal": phase_retry_heal,
}

if __name__ == "__main__":
    import sys

    # Parse args: phases, "all", "check", or "--plan"
    args = sys.argv[1:]
    if not args or args == ["all"]:
        mode = "run"
        requested = [p for p in PHASES if p not in ("incremental", "target_uc_prep", "target_dest", "retry_seed", "retry_heal")]
    elif args == ["--plan"]:
        mode = "plan"
        requested = [p for p in PHASES if p not in ("incremental", "target_uc_prep", "target_dest", "retry_seed", "retry_heal")]
    elif args == ["check"]:
        mode = "run"
        requested = ["check"]
    elif args[0] == "--plan":
        # --plan with specific phases: python ... --plan identity compute
        mode = "plan"
        requested = args[1:]
    else:
        mode = "run"
        requested = args

    # Validate phases (except check, which is always valid)
    unknown = [p for p in requested if p not in PHASES and p != "check"]
    if unknown:
        avail = sorted(list(PHASES.keys()) + ["check", "all"])
        print(f"unknown phase(s): {unknown}")
        print(f"known phases: {', '.join(avail)}")
        print(f"usage: python tests/fixtures_medium.py [phase ...|all|check|--plan [phases]]")
        sys.exit(1)

    if mode == "plan":
        # --plan mode: print phases and counts without API calls
        print("Fixture phases (--plan mode, no network access):")
        for phase_name in requested:
            if phase_name == "check":
                print(f"  check          — read-only prerequisite report")
            else:
                print(f"  {phase_name:<14} — (use 'python tests/fixtures_medium.py {phase_name}' to run)")
        print(f"\nTo run all phases: python tests/fixtures_medium.py all")
        print(f"To check prerequisites: python tests/fixtures_medium.py check")
    elif mode == "run":
        # Run the requested phases
        for name in requested:
            if name == "check":
                phase_check()
            else:
                PHASES[name]()
