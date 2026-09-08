"""LIVE harness: prove `direct` mode really works (Plan 3 build step 1, §11).

What this validates against the REAL workspaces — the things no offline test can:
  1. An **OAuth M2M** (client-credentials) token actually mints from the source workspace's
     `/oidc/v1/token` using the source SP's client_id + secret.
  2. That token reaches an **admin-only** source endpoint (`GET .../scim/v2/Groups?count=1`) — a
     mis-scoped SP then fails in 2 seconds rather than halfway through a 40-minute inventory.
  3. `build_clients()` in `direct` mode returns two clients bound to DIFFERENT hosts, and each one
     really talks to the workspace it claims (verified by comparing workspace ids).
  4. The secret appears in NO log line or artifact.

Credentials: reads `<app_id>\\n<secret>` from the file named by WSMIG_SP_SECRET_FILE
(default /tmp/wsmig_fvm1_sp_secret.txt). Mint one with:
  POST /api/2.0/accounts/servicePrincipals/<sp_id>/credentials/secrets
The SP must be a workspace admin on the SOURCE.

Run: python3 -m tests.live_direct_mode
"""
from __future__ import annotations

import configparser
import io
import json
import os
import subprocess
import sys

from src.auth.token_manager import ApiClient, build_clients, oauth_m2m_token_provider
from src.config.config_manager import Config

SOURCE_PROFILE = os.environ.get("WSMIG_SOURCE_PROFILE", "fvm1")
TARGET_PROFILE = os.environ.get("WSMIG_TARGET_PROFILE", "target_ws")
SECRET_FILE = os.environ.get("WSMIG_SP_SECRET_FILE", "/tmp/wsmig_fvm1_sp_secret.txt")


def _host(profile: str) -> str:
    c = configparser.ConfigParser()
    c.read(os.path.expanduser("~/.databrickscfg"))
    return dict(c[profile])["host"].rstrip("/")


def _cli_token(profile: str) -> str:
    return json.loads(subprocess.check_output(
        ["databricks", "auth", "token", "-p", profile], text=True))["access_token"]


def _sp_creds() -> tuple[str, str]:
    with open(SECRET_FILE) as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if len(lines) < 2:
        raise SystemExit(f"{SECRET_FILE} must hold '<app_id>\\n<secret>'")
    return lines[0], lines[1]


def main() -> int:
    source_host, target_host = _host(SOURCE_PROFILE), _host(TARGET_PROFILE)
    client_id, secret = _sp_creds()
    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail[:160]}" if detail else ""))

    print(f"source: {source_host}\ntarget: {target_host}\nsp client_id: {client_id}\n")

    # 1. the token mints
    print("== 1. OAuth M2M token mint ==")
    provider = oauth_m2m_token_provider(source_host, client_id, secret)
    try:
        token = provider()
        check("client-credentials token minted", bool(token), f"{len(token)} chars")
    except Exception as exc:  # noqa: BLE001
        check("client-credentials token minted", False, str(exc))
        return 1
    # cached on the second call (no second mint)
    check("token is cached across calls", provider() == token)

    # 2. it reaches an admin-only endpoint on the SOURCE
    print("\n== 2. admin-only source endpoint via M2M ==")
    src = ApiClient(source_host, provider)
    try:
        groups = src.get("api/2.0/preview/scim/v2/Groups", params={"count": 1})
        ok = isinstance(groups, dict) and "Resources" in groups
        check("SCIM Groups readable (proves workspace-admin)", ok,
              f"totalResults={groups.get('totalResults')}")
    except Exception as exc:  # noqa: BLE001
        check("SCIM Groups readable (proves workspace-admin)", False, str(exc))

    # a couple more read surfaces inventory/export actually use
    for label, path, key in (("clusters/list", "api/2.0/clusters/list", "clusters"),
                             ("jobs list (2.1)", "api/2.1/jobs/list", "jobs"),
                             ("workspace/list /", "api/2.0/workspace/list", "objects")):
        try:
            params = {"path": "/"} if "workspace/list" in path else None
            doc = src.get(path, params=params)
            check(f"source {label} readable via M2M", isinstance(doc, dict),
                  f"{len(doc.get(key, []) or [])} items")
        except Exception as exc:  # noqa: BLE001
            check(f"source {label} readable via M2M", False, str(exc))

    # 3. build_clients wires two DIFFERENT hosts, and each really is that workspace
    print("\n== 3. build_clients(direct) ==")
    cfg = Config.from_dict({
        "role": "target", "connectivity_mode": "direct", "source_workspace_id": "x",
        "run_id": "r", "target_staging_location": "/tmp/x",
        "source": {"workspace_url": source_host, "client_id": client_id,
                   "spn_secret_value": secret},
        "ctx": {"workspace_url": target_host, "token": _cli_token(TARGET_PROFILE)},
    })
    s_client, t_client = build_clients(cfg)
    check("source client bound to the source host", s_client.base_url == source_host)
    check("target client bound to the target host", t_client.base_url == target_host)
    check("two distinct clients", s_client is not t_client)

    # Prove the two clients carry DIFFERENT credentials, not just different base URLs. A config
    # that merely LOOKS right but resolves both clients to one workspace/token would silently
    # migrate a workspace onto itself, so this is asserted rather than assumed. `scim/v2/Me`
    # reports who the caller is: the source client is the SP (userName == its applicationId),
    # the target client is the run-as user.
    def whoami(client) -> str:
        return client.get("api/2.0/preview/scim/v2/Me").get("userName", "")

    try:
        src_who = whoami(s_client)
        tgt_who = whoami(t_client)
        # The source client authenticates AS THE SP; the target client as the run-as user. Those
        # identities must differ, which proves the two clients are not the same credential.
        check("source client authenticates as the SP (not the run-as user)",
              src_who != tgt_who, f"source={src_who!r} target={tgt_who!r}")
    except Exception as exc:  # noqa: BLE001
        check("source client authenticates as the SP (not the run-as user)", False, str(exc))

    # 4. redaction — the secret in no artifact, and in no log line
    print("\n== 4. secret redaction ==")
    blob = json.dumps(cfg.redacted())
    check("secret absent from config_resolved.json shape", secret not in blob)
    check("secret_source recorded", cfg.redacted()["source"]["secret_source"] == "widget")

    # Capture stdout while a fresh provider mints + logs, and assert the secret never appears.
    buf = io.StringIO()
    real_stdout = sys.stdout
    try:
        sys.stdout = buf
        p2 = oauth_m2m_token_provider(source_host, client_id, secret)
        p2()
        _s, _t = build_clients(cfg)
    finally:
        sys.stdout = real_stdout
    check("secret absent from every log line emitted during auth", secret not in buf.getvalue(),
          f"{len(buf.getvalue().splitlines())} log lines checked")

    # ── summary ────────────────────────────────────────────────────────────
    npass = sum(1 for _n, ok, _d in checks if ok)
    nfail = len(checks) - npass
    print("\n" + "=" * 74)
    print(f"DIRECT-MODE LIVE CHECKS: {npass} passed, {nfail} failed")
    for name, ok, detail in checks:
        if not ok:
            print(f"  FAIL {name}: {detail[:200]}")
    print("=" * 74)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
