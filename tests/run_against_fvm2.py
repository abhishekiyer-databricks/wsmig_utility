"""Live inventory run against the `fvm2` workspace (local harness).

Exercises the REAL collectors / pagination / ACL fetches / classifier / reports against a
real workspace. The ONLY difference from the notebook path is the token source: here we build
the ApiClient from the SDK profile token instead of `resolve_context` (notebook context).

Run: python3 -m tests.run_against_fvm2
"""
from __future__ import annotations

import tempfile

from databricks.sdk import WorkspaceClient

from src.auth.token_manager import ApiClient, StaticTokenProvider
from src.config.config_manager import Config
from src.collectors.inventory_runner import InventoryRunner
from src.exporters.artifact_writer import ArtifactWriter
from src.utils import logger as _logger

PROFILE = "fvm2"


def build_config_and_client():
    w = WorkspaceClient(profile=PROFILE)
    host = w.config.host.rstrip("/")
    token = w.config.authenticate()["Authorization"].split(" ", 1)[1]
    ws_id = w.get_workspace_id() if hasattr(w, "get_workspace_id") else "fvm2"

    staging = tempfile.mkdtemp(prefix="fvm2_inv_")
    cfg = Config.from_dict({
        "role": "source",
        "source_workspace_id": str(ws_id),
        "run_id": "fvm2_live",
        "source_staging_location": staging,
        # keep the live run bounded so it finishes quickly on a big workspace
        "max_scim": 0,
        "max_workspace_items": 0,
        "max_ws_api_calls": 200,
    })
    cfg.ctx.workspace_url = host
    cfg.ctx.token = token
    client = ApiClient(host, StaticTokenProvider(token))
    return cfg, client


def main():
    cfg, client = build_config_and_client()
    print(f"Workspace : {cfg.ctx.workspace_url}")
    print(f"Staging   : {cfg.output_path}\n")

    aw = ArtifactWriter(cfg)
    _logger.set_log_file(aw.ensure_output_path() + "/execution_inventory.log")
    result = InventoryRunner(client, cfg, aw).run()

    print("\n=== counts ===")
    for k, v in sorted(result["counts"].items()):
        print(f"  {k:<22} {v:>6}")
    print("\n=== identity classification ===")
    for k, v in sorted(result["identity_summary"].items()):
        print(f"  {k:<22} {v:>6}")
    if result["warnings"]:
        print("\n=== warnings ===")
        for wmsg in result["warnings"]:
            print("  -", wmsg)
    print(f"\nArtifacts written to: {result['output_path']}")
    _logger.flush_log_file()   # mirror the log to the staging dir (append there silently fails)


if __name__ == "__main__":
    main()
