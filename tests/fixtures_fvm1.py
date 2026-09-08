"""
fixtures_fvm1 — populate a SOURCE workspace with a COMPLETE test fixture set covering
every inventory asset type + edge cases, so the export utility can be tested end-to-end.

Idempotent-ish: uses stable `wsmig_test*` names / paths; re-running skips or overwrites where the
API allows. Writes ONLY to the source workspace. Run phases individually:
    python3 -m tests.fixtures_fvm1 <phase>
where phase ∈ identity compute workspace secrets uc sql jobs dlt dashboards genie misc dab acls all

Everything is namespaced so it's easy to find/remove:
  • names prefixed `wsmig_test_`
  • workspace paths under /Shared/wsmig_test/ and /Users/<me>/wsmig_test/
  • UC under <default catalog>.wsmig_test

NOTHING here is workspace-specific: the profile, catalog, identity and Azure coordinates are
all resolved at runtime (or overridden by env), so the same file populates any workspace pair.
    WSMIG_PROFILE       databrickscfg profile for the source WORKSPACE   (default source_ws)
    WSMIG_ACCT_PROFILE  databrickscfg profile for the ACCOUNT console    (default source_acct)
    WSMIG_CATALOG       UC catalog for fixture tables    (default: the workspace default catalog)
    WSMIG_CLI           databricks CLI to use for bundle deploys (needs >= 1.5.0 for genie_spaces)
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
SCHEMA = "wsmig_test"
# The four Entra/SCIM-provisioned test users, plus one deliberately NON-Entra user
# (no externalId at account level) so the collector's "needs review" path is exercised.
ENTRA_USERS = ["aman.bansal@databricks.com", "sanket.kelkar@databricks.com",
               "vivek.ravichandiran@databricks.com", "idris.chakera@databricks.com"]
NON_ENTRA_USER = "vivek.ravichandran@databricks.com"
TEST_USERS = ENTRA_USERS + [NON_ENTRA_USER]
# The Entra/UMI-backed service principal to assign (must already exist in the account).
UMI_SP_NAME = "ai27_umi"

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


def _assign_to_workspace(principal_id, permissions=("USER",)):
    """Assign an account-level identity to this workspace.

    Uses the WORKSPACE-scoped PermissionAssignments route rather than the account-scoped
    `/accounts/{id}/workspaces/{ws}/...` one: the latter 404s for a workspace-admin token, while
    this one works with the ambient workspace credentials we already have.
    """
    return w.api_client.do(
        "PUT", f"/api/2.0/preview/permissionassignments/principals/{principal_id}",
        body={"permissions": list(permissions)})


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
    """
    object_id, provenance = _aad_object_id(name)
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
    if existing:
        log(f"account group exists: {existing.display_name!r} "
            f"(id={existing.id}, ext={existing.external_id})")
        gid = existing.id
    else:
        g = a.groups.create(display_name=name, external_id=object_id)
        log(f"account group created: {name} (id={g.id}, ext={object_id})")
        gid = g.id
    try:
        _assign_to_workspace(gid)
        log(f"  + assigned to workspace: {name}")
    except Exception as e:
        log(f"  assign {name}: {str(e)[:110]}")
    return gid


def phase_identity():
    from databricks.sdk.service import iam
    print("== identity ==")

    # 1. Test users. These are ACCOUNT identities (Entra/SCIM-provisioned), so the right move is
    #    to ASSIGN them to this workspace, not create them — creating would make a workspace-local
    #    duplicate. NON_ENTRA_USER has no externalId at account level, which is exactly the
    #    "Needs review" classification case.
    a = _acct()
    for email in TEST_USERS:
        acct_user = next(iter(a.users.list(filter=f'userName eq "{email}"')), None)
        if not acct_user:
            log(f"user {email}: NOT in account — cannot assign (needs Entra/SCIM provisioning)")
            continue
        try:
            _assign_to_workspace(acct_user.id)
            log(f"user assigned: {email} (ext={acct_user.external_id})")
        except Exception as e:
            log(f"user {email}: {str(e)[:90]}")

    # 1b. Entitlements are WORKSPACE-scoped, so set them per identity once assigned — and give
    #     each user a DIFFERENT set so the entitlement-apply path is really exercised.
    ent_by_user = {
        ENTRA_USERS[0]: ["allow-cluster-create"],
        ENTRA_USERS[1]: ["databricks-sql-access"],
        ENTRA_USERS[2]: ["workspace-access", "databricks-sql-access",
                         "allow-instance-pool-create"],
        ENTRA_USERS[3]: ["allow-cluster-create", "allow-instance-pool-create"],
        NON_ENTRA_USER: ["workspace-access"],
    }
    for email, ents in ent_by_user.items():
        ws_user = next(iter(w.users.list(filter=f'userName eq "{email}"')), None)
        if not ws_user:
            continue
        try:
            w.users.patch(
                id=ws_user.id,
                schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                operations=[iam.Patch(op=iam.PatchOp.ADD, path="entitlements",
                                      value=[{"value": e} for e in ents])])
            log(f"  entitlements set on {email}: {', '.join(ents)}")
        except Exception as e:
            log(f"  entitlements {email}: {str(e)[:90]}")

    # 2. Entra-backed groups (account group carrying an externalId → assigned to workspace).
    _entra_group("wsmig_test_entra_grp")
    _entra_group("wsmig_test_entra_grp2")

    # 3. Databricks-managed groups (workspace-local, no externalId) + entitlements
    def mk_group(name, entitlements=None, members=None):
        """Create-or-update a workspace-local group.

        Must converge on re-run: an existing group has to have its members PATCHed in, because on
        a first run the group is often created before the users it should contain are assigned
        (or before a nested child group exists), leaving it silently under-populated.
        """
        existing = next(iter(w.groups.list(filter=f'displayName eq "{name}"')), None)
        if existing is None:
            try:
                g = w.groups.create(
                    display_name=name,
                    entitlements=[iam.ComplexValue(value=e) for e in (entitlements or [])],
                    members=[iam.ComplexValue(value=m) for m in (members or [])],
                )
                log(f"group created: {name} (id={g.id}, members={len(members or [])})")
                return g.id
            except Exception as e:
                log(f"group {name}: {str(e)[:90]}")
                return None

        have = {m.value for m in (existing.members or [])}
        missing = [m for m in (members or []) if m not in have]
        if missing:
            try:
                w.groups.patch(
                    id=existing.id,
                    schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                    operations=[iam.Patch(op=iam.PatchOp.ADD, path="members",
                                          value=[{"value": m} for m in missing])])
                log(f"group exists: {name} (+{len(missing)} members added)")
            except Exception as e:
                log(f"group {name} member patch: {str(e)[:90]}")
        else:
            log(f"group exists: {name} (members already correct)")
        return existing.id

    # resolve user ids for membership (workspace-local ids, post-assignment)
    uid = {}
    for email in [ME] + TEST_USERS:
        for u in w.users.list(filter=f'userName eq "{email}"'):
            uid[email] = u.id
    # A three-level nest: grandchild → child → parent, so nested-first creation ordering on the
    # import side has to actually be correct (a two-level nest can pass by accident).
    grandchild = mk_group("wsmig_test_grandchild_grp",
                          entitlements=["databricks-sql-access"],
                          members=[uid[e] for e in ENTRA_USERS[:1] if e in uid])
    child = mk_group("wsmig_test_child_grp",
                     entitlements=["databricks-sql-access"],
                     members=([grandchild] if grandchild else [])
                             + [uid[e] for e in ENTRA_USERS[:2] if e in uid])
    # parent group with the child nested + cluster-create entitlement + me
    mk_group("wsmig_test_parent_grp",
             entitlements=["allow-cluster-create", "workspace-access"],
             members=([child] if child else []) + ([uid[ME]] if ME in uid else []))
    # a plain group, no entitlements
    mk_group("wsmig_test_plain_grp",
             members=[uid[NON_ENTRA_USER]] if NON_ENTRA_USER in uid else [])

    # 4. Databricks-managed SPNs (workspace-local; no externalId → DB-managed).
    #    Different entitlements per SP so the entitlement-apply path is exercised per identity.
    #    NOTE: unlike groups, SP create does NOT reject a duplicate displayName — it happily makes
    #    a second SP with a new applicationId — so this must check for an existing one first or
    #    every re-run silently doubles them.
    for sp_name, ents in (("wsmig_test_db_sp", ["allow-cluster-create"]),
                          ("wsmig_test_db_sp2", ["databricks-sql-access",
                                                 "allow-instance-pool-create"])):
        found = next(iter(w.service_principals.list(
            filter=f'displayName eq "{sp_name}"')), None)
        if found:
            log(f"db-managed SPN exists: {sp_name} (appId={found.application_id})")
            continue
        try:
            sp = w.service_principals.create(
                display_name=sp_name,
                entitlements=[iam.ComplexValue(value=e) for e in ents])
            log(f"db-managed SPN created: {sp_name} (appId={sp.application_id})")
        except Exception as e:
            log(f"db SPN {sp_name}: {str(e)[:70]}")

    # 5. Entra/UMI-backed SP — must be ASSIGNED from the account, never created.
    #    The workspace SCIM API silently DROPS `externalId` on create (verified live 2026-08-03),
    #    so a workspace-created SP can never be Entra-backed; only a real account SP carries one.
    acct_sp = next(iter(a.service_principals.list(
        filter=f'displayName eq "{UMI_SP_NAME}"')), None)
    if not acct_sp:
        log(f"UMI SP {UMI_SP_NAME}: not found in account — skipping")
    else:
        try:
            _assign_to_workspace(acct_sp.id)
            log(f"UMI SP assigned: {UMI_SP_NAME} (appId={acct_sp.application_id}, "
                f"ext={acct_sp.external_id})")
        except Exception as e:
            log(f"UMI SP assign: {str(e)[:100]}")
        ws_sp = next(iter(w.service_principals.list(
            filter=f'displayName eq "{UMI_SP_NAME}"')), None)
        if ws_sp:
            try:
                w.service_principals.patch(
                    id=ws_sp.id,
                    schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                    operations=[iam.Patch(op=iam.PatchOp.ADD, path="entitlements",
                                          value=[{"value": "workspace-access"},
                                                 {"value": "databricks-sql-access"}])])
                log(f"  entitlements set on {UMI_SP_NAME}")
            except Exception as e:
                log(f"  entitlements {UMI_SP_NAME}: {str(e)[:90]}")

    # 6. An OAuth secret on a DB-managed SP — the has_secrets flag (values never exported).
    #    Secrets are an ACCOUNT-level sub-resource, so this goes through the account client even
    #    though the SP itself was created workspace-locally.
    ws_sp = next(iter(w.service_principals.list(
        filter='displayName eq "wsmig_test_db_sp"')), None)
    if ws_sp:
        try:
            existing = a.api_client.do(
                "GET", f"/api/2.0/accounts/{a.config.account_id}"
                       f"/servicePrincipals/{ws_sp.id}/credentials/secrets") or {}
            if existing.get("secrets"):
                log("OAuth secret already present on wsmig_test_db_sp (has_secrets=True)")
            else:
                a.api_client.do("POST", f"/api/2.0/accounts/{a.config.account_id}"
                                        f"/servicePrincipals/{ws_sp.id}/credentials/secrets")
                log("OAuth secret created on wsmig_test_db_sp (has_secrets=True)")
        except Exception as e:
            log(f"sp secret: {str(e)[:100]}")

    # 7. A group mixing all three member kinds — user + SP + Entra-backed group. Runs LAST so the
    #    SPs and the Entra group it references already exist.
    mixed = []
    ent_grp = next(iter(w.groups.list(filter='displayName eq "wsmig_test_entra_grp"')), None)
    if ent_grp:
        mixed.append(ent_grp.id)
    for sp_name in ("wsmig_test_db_sp", "wsmig_test_db_sp2"):
        sp = next(iter(w.service_principals.list(filter=f'displayName eq "{sp_name}"')), None)
        if sp:
            mixed.append(sp.id)
    if ME in uid:
        mixed.append(uid[ME])
    mk_group("wsmig_test_mixed_grp", entitlements=["workspace-access"], members=mixed)


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
        f"INSERT INTO {CATALOG}.{SCHEMA}.trips VALUES ('94103', 120, 3.4), ('94107', 88, 2.1)",
        f"CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.zones (zip STRING, borough STRING)",
        f"INSERT INTO {CATALOG}.{SCHEMA}.zones VALUES ('94103','SF'), ('94107','SF')",
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

def phase_sql():
    print("== sql (queries, legacy alert, alerts v2, legacy dashboard) ==")
    wh = _warehouse_id()
    # 1. Query via the current /api/2.0/sql/queries API (collector tags this legacy_query).
    qid = None
    try:
        from databricks.sdk.service.sql import CreateQueryRequestQuery
        q = w.queries.create(query=CreateQueryRequestQuery(
            display_name="wsmig_test_query", warehouse_id=wh,
            query_text=f"SELECT * FROM {CATALOG}.{SCHEMA}.trips"))
        qid = q.id
        log(f"query: wsmig_test_query ({qid})")
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
        r = w.alerts_v2.create_alert(alert=av2)
        log(f"alert_v2: wsmig_test_alert_v2 ({getattr(r,'id',None)})")
    except Exception as e:
        log(f"alert_v2: {str(e)[:140]}")

    # 3. Legacy alert (/api/2.0/sql/alerts family via alerts_legacy) — needs a legacy query.
    try:
        from databricks.sdk.service.sql import AlertOptions
        lq = w.queries_legacy.create(name="wsmig_test_legacy_q", query="SELECT 1 AS v",
                                     data_source_id=_legacy_data_source_id(wh))
        la = w.alerts_legacy.create(name="wsmig_test_legacy_alert",
                                    query_id=lq.id,
                                    options=AlertOptions(column="v", op=">", value="0"))
        log(f"legacy_alert: wsmig_test_legacy_alert ({la.id})")
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
    print("== genie space ==")
    wh = _warehouse_id()
    serialized = json.dumps({
        "version": 2,
        "data_sources": {"tables": [
            {"identifier": f"{CATALOG}.{SCHEMA}.trips"},
            {"identifier": f"{CATALOG}.{SCHEMA}.zones"}]},
    })
    try:
        for sp in (w.genie.list_spaces().spaces or []):
            if sp.title == "wsmig_test_genie":
                log(f"genie space exists: wsmig_test_genie ({sp.space_id})")
                return
    except Exception as e:
        log(f"genie list: {str(e)[:80]}")
    try:
        r = w.genie.create_space(warehouse_id=wh, serialized_space=serialized,
                                 title="wsmig_test_genie", description="test genie space")
        log(f"genie space: wsmig_test_genie ({getattr(r,'space_id',None)})")
    except Exception as e:
        log(f"genie: {str(e)[:150]}")


# ─────────────────────────── Lakeview (AI/BI) dashboard ────────────────────

def phase_dashboards():
    print("== lakeview (AI/BI) dashboard ==")
    wh = _warehouse_id()
    serialized = json.dumps({
        "datasets": [{"name": "ds1", "displayName": "trips",
                      "queryLines": [f"SELECT * FROM {CATALOG}.{SCHEMA}.trips"]}],
        "pages": [{"name": "p1", "displayName": "Page 1",
                   "layout": [{"position": {"x": 0, "y": 0, "width": 6, "height": 6},
                               "widget": {"name": "w1",
                                          "queries": [{"name": "q1", "query": {
                                              "datasetName": "ds1", "fields": [
                                                  {"name": "zip", "expression": "`zip`"}],
                                              "disaggregated": True}}],
                                          "spec": {"version": 1, "widgetType": "table",
                                                   "encodings": {}}}}]}],
    })
    for d in w.lakeview.list():
        if d.display_name == "wsmig_test_dashboard":
            log(f"lakeview dashboard exists: wsmig_test_dashboard ({d.dashboard_id})")
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
    # is applied individually so one rejected key doesn't drop the rest.
    conf = {
        "enableExportNotebook": "true",
        "enableWebTerminal": "true",
        "enableTokensConfig": "true",
        "maxTokenLifetimeDays": "90",
        "enableIpAccessLists": "false",
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
    vault = f"wsmigtestkv{ME.split('@')[0].replace('.', '')[:8]}"
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

    dash_serialized = json.dumps({
        "datasets": [{"name": "ds1", "displayName": "trips",
                      "queryLines": [f"SELECT * FROM {CATALOG}.{SCHEMA}.trips"]}],
        "pages": [{"name": "p1", "displayName": "Page 1",
                   "layout": [{"position": {"x": 0, "y": 0, "width": 6, "height": 6},
                               "widget": {"name": "w1",
                                          "queries": [{"name": "q1", "query": {
                                              "datasetName": "ds1",
                                              "fields": [{"name": "zip", "expression": "`zip`"}],
                                              "disaggregated": True}}],
                                          "spec": {"version": 1, "widgetType": "table",
                                                   "encodings": {}}}}]}],
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
"""
        with open(f"{d}/databricks.yml", "w") as f:
            f.write(bundle)
        r = subprocess.run([cli, "bundle", "deploy", "-p", PROFILE],
                           cwd=d, capture_output=True, text=True, env=env)
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
    r = subprocess.run([cli, "bundle", "deploy", "-p", PROFILE],
                       cwd=d, capture_output=True, text=True, env=env)
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
    """One principal of each KIND, so the importer's principal remapping is fully exercised.

    The kinds matter independently: users and Entra groups keep their identity across the
    migration, while DB-managed groups and SPs get NEW ids on target and so must be remapped.
    """
    out = []
    u = next(iter(w.users.list(filter=f'userName eq "{ENTRA_USERS[0]}"')), None)
    if u:
        out.append(("user_name", u.user_name, "entra user"))
    # Two db-managed groups and one Entra-backed one. The kind labels are DISTINCT per principal
    # so the coverage report below counts real principals, not collapsed duplicate labels.
    for gname, kind in (("wsmig_test_parent_grp", "db group (parent)"),
                        ("wsmig_test_child_grp", "db group (child)"),
                        ("wsmig_test_entra_grp", "entra group"),
                        ("wsmig_test_plain_grp", "db group (plain)")):
        g = next(iter(w.groups.list(filter=f'displayName eq "{gname}"')), None)
        if g:
            out.append(("group_name", gname, kind))
    # Both SPs, so the SP-remap path gets more than a single grant.
    for sp_name in ("wsmig_test_db_sp", "wsmig_test_db_sp2"):
        sp = next(iter(w.service_principals.list(
            filter=f'displayName eq "{sp_name}"')), None)
        if sp:
            out.append(("service_principal_name", sp.application_id, f"db SP ({sp_name[-1]})"))
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
        for i, level in enumerate(ladder):
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


# Dependency-ordered: identity before ACLs (needs principals), warehouses+uc before anything
# that references a warehouse or table, compute+workspace before jobs/libraries, and acls LAST
# so every object it grants on already exists.
PHASES = {
    "identity": phase_identity,
    "warehouses": phase_warehouses,
    "uc": phase_uc,
    "compute": phase_compute,
    "workspace": phase_workspace,
    "secrets": phase_secrets,
    "akv": phase_akv,
    "sql": phase_sql,
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
    "acls": phase_acls,
}

if __name__ == "__main__":
    requested = sys.argv[1:] or ["all"]
    if requested == ["all"]:
        requested = list(PHASES)
    unknown = [p for p in requested if p not in PHASES]
    if unknown:
        print("unknown phase(s):", unknown, "| known:", list(PHASES) + ["all"])
        sys.exit(1)
    for name in requested:
        PHASES[name]()
