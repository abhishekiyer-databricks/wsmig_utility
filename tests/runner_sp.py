"""The ONE run identity for live QA: `ai27_wsmig_runner` — an ACCOUNT service principal that is
workspace ADMIN on BOTH source and target, the direct-mode SOURCE read SP (client id + OAuth secret)
AND the target jobs' run-as. This is how the customer runs the utility.

    python3 tests/runner_sp.py ensure source_ws target_ws   # create-or-adopt + ADMIN on each ws
    python3 tests/runner_sp.py secret                       # mint an OAuth secret (only if none saved)
    python3 tests/runner_sp.py scope target_ws              # put client id + secret in a target scope

The secret is written ONCE to ~/.wsmig/ai27_wsmig_runner.json (chmod 600, outside the repo) and is
never printed. Account profile: WSMIG_ACCT_PROFILE (default source_acct). Idempotent everywhere:
assignments are ADDITIVE (never downgrade), the SP is looked up by displayName before create.
"""
from __future__ import annotations

import json
import os
import stat
import sys

from databricks.sdk import AccountClient, WorkspaceClient

NAME = "ai27_wsmig_runner"
ACCT_PROFILE = os.environ.get("WSMIG_ACCT_PROFILE", "source_acct")
SECRET_FILE = os.path.expanduser("~/.wsmig/ai27_wsmig_runner.json")
SCOPE, KEY_ID, KEY_SECRET = "wsmig_runner", "client_id", "client_secret"


def _acct() -> AccountClient:
    return AccountClient(profile=ACCT_PROFILE)


def _sp(a: AccountClient):
    sps = list(a.service_principals.list(filter=f'displayName eq "{NAME}"'))
    if len(sps) > 1:
        sys.exit(f"{len(sps)} account SPs named {NAME} — resolve the duplicate first")
    return sps[0] if sps else None


def _assign_admin(w: WorkspaceClient, principal_id: str) -> str:
    doc = w.api_client.do("GET", "/api/2.0/preview/permissionassignments") or {}
    cur = set()
    for pa in doc.get("permission_assignments", []) or []:
        if str((pa.get("principal") or {}).get("principal_id")) == str(principal_id):
            cur = set(pa.get("permissions") or [])
    if "ADMIN" in cur:
        return "already ADMIN"
    w.api_client.do("PUT", f"/api/2.0/preview/permissionassignments/principals/{principal_id}",
                    body={"permissions": sorted(cur | {"ADMIN"})})
    return "assigned ADMIN"


def _grant_self_sp_user(w: WorkspaceClient, acct_id: str, app_id: str) -> None:
    """Setting a job's run_as to this SP needs servicePrincipal.user for the job creator (me)."""
    me = f"users/{w.current_user.me().user_name}"
    name = f"accounts/{acct_id}/servicePrincipals/{app_id}/ruleSets/default"
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
    print(f"  servicePrincipal.user granted to {me} on {NAME}")


def ensure(profiles: list[str]) -> None:
    a = _acct()
    sp = _sp(a)
    if sp is None:
        sp = a.service_principals.create(display_name=NAME, active=True)
        print(f"account SP created: {NAME} (appId={sp.application_id})")
    else:
        print(f"account SP exists: {NAME} (appId={sp.application_id})")
    w = None
    for prof in profiles:
        w = WorkspaceClient(profile=prof)
        print(f"  {prof} (ws {w.get_workspace_id()}): {_assign_admin(w, sp.id)}")
    # The SP rule set is ACCOUNT-scoped → grant once; a fresh read can be stale for a few seconds
    # ("RBAC store resource conflict"), so retry on a re-read.
    for attempt in range(5):
        try:
            _grant_self_sp_user(w, a.config.account_id, sp.application_id)
            break
        except Exception as e:  # noqa: BLE001
            if "conflict" not in str(e).lower() or attempt == 4:
                raise
            __import__("time").sleep(5)


def secret() -> None:
    if os.path.isfile(SECRET_FILE):
        print(f"secret already saved at {SECRET_FILE} — not minting another")
        return
    a = _acct()
    sp = _sp(a)
    if sp is None:
        sys.exit(f"{NAME} does not exist — run `ensure` first")
    out = a.api_client.do(
        "POST", f"/api/2.0/accounts/{a.config.account_id}/servicePrincipals/{sp.id}/credentials/secrets",
        body={"lifetime": "31536000s"})   # 1 year
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump({"client_id": sp.application_id, "client_secret": out["secret"],
                   "secret_id": out.get("id"), "expire_time": out.get("expire_time")}, f)
    print(f"OAuth secret minted (id={out.get('id')}, expires {out.get('expire_time')}) "
          f"→ saved to {SECRET_FILE} (600)")


def scope(profile: str) -> None:
    creds = json.load(open(SECRET_FILE))
    w = WorkspaceClient(profile=profile)
    if SCOPE not in {s.name for s in w.secrets.list_scopes()}:
        w.secrets.create_scope(scope=SCOPE)
        print(f"  scope created: {SCOPE}")
    w.secrets.put_secret(scope=SCOPE, key=KEY_ID, string_value=creds["client_id"])
    w.secrets.put_secret(scope=SCOPE, key=KEY_SECRET, string_value=creds["client_secret"])
    # the runner (job run-as) must be able to READ its own secret
    w.secrets.put_acl(scope=SCOPE, principal=creds["client_id"],
                      permission=__import__("databricks.sdk.service.workspace",
                                            fromlist=["AclPermission"]).AclPermission.READ)
    print(f"  {profile}: {SCOPE}/{KEY_ID} + {SCOPE}/{KEY_SECRET} written (value not shown); "
          f"READ granted to the runner")


if __name__ == "__main__":
    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "ensure" and rest:
        ensure(rest)
    elif cmd == "secret":
        secret()
    elif cmd == "scope" and len(rest) == 1:
        scope(rest[0])
    else:
        sys.exit(__doc__)
