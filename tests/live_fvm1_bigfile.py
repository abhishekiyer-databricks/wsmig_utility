"""LIVE test: >10MB content through the REAL ContentFetcher against fvm1.

Proves the two size-boundary behaviours the plan flagged, on a real workspace:
  1. An 11 MB workspace FILE → fetched successfully via workspace/export?direct_download
     (files have NO 10 MB cap; verified endpoint).
  2. A >10 MB NOTEBOOK cannot even be created via API (base64 import rejects it, streaming
     stores it as a FILE), so we simulate the export-side rejection to prove the fetcher marks
     it skipped_oversize with NO bytes — matching the decision.

Needs the `fvm1` CLI profile. Creates + deletes its own test file (no residue).
Run: python3 -m tests.live_fvm1_bigfile
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import requests

from src.auth.token_manager import ApiClient, StaticTokenProvider, DownloadHTTPError
from src.config.config_manager import Config
from src.exporters.artifact_writer import ArtifactWriter
from src.exporters.content_fetcher import ContentFetcher, FILE_CAP

PROFILE = os.environ.get("WSMIG_LIVE_PROFILE", "fvm1")


def _profile():
    import configparser
    c = configparser.ConfigParser()
    c.read(os.path.expanduser("~/.databrickscfg"))
    return dict(c[PROFILE])


def _token():
    return json.loads(subprocess.check_output(
        ["databricks", "auth", "token", "-p", PROFILE], text=True))["access_token"]


def main() -> int:
    prof = _profile()
    host = prof["host"].rstrip("/")
    tok = _token()
    H = {"Authorization": f"Bearer {tok}"}
    big_path = "/Users/abhishek.iyer@databricks.com/wsmig_bigfile_livetest.csv"
    data = b"col1,col2\n" + b"x" * (11 * 1024 * 1024)   # 11 MB
    ok = True

    # ── upload the 11 MB FILE via the streaming import-file route ──
    r = requests.post(f"{host}/api/2.0/workspace-files/import-file{big_path}",
                      headers=H, params={"overwrite": "true"}, data=data)
    print(f"[setup] uploaded 11MB file: HTTP {r.status_code} ({len(data)} bytes)")

    try:
        # ── run the REAL ContentFetcher against it ──
        staging = tempfile.mkdtemp(prefix="wsmig_bigfile_")
        cfg = Config.from_dict({"role": "source", "source_workspace_id": "live", "run_id": "bf",
                                "source_staging_location": staging})
        cfg.ctx.workspace_url = host
        cfg.ctx.token = tok
        client = ApiClient(host, StaticTokenProvider(tok))
        aw = ArtifactWriter(cfg)
        aw.ensure_output_path()
        fetcher = ContentFetcher(client, aw)

        # 1. 11 MB FILE → success (no 10 MB cap on files)
        res = fetcher.fetch({"asset_type": "workspace_file", "natural_key": big_path,
                             "payload": {}})
        full = os.path.join(aw.root, res.content_ref) if res.content_ref else ""
        got = os.path.getsize(full) if full and os.path.isfile(full) else 0
        print(f"\n[TEST 1] 11MB FILE fetch: status={res.status} route={res.content_route} "
              f"bytes_written={got}")
        if res.status == "success" and got == len(data):
            print("  ✓ PASS: 11MB file fetched + written whole (files have no 10MB cap)")
        else:
            print(f"  ✗ FAIL: expected success with {len(data)} bytes"); ok = False

        # 2. >10MB NOTEBOOK → skipped_oversize, NO bytes. We can't create a >10MB notebook object
        #    on the workspace (API forbids it), so drive the fetcher with a client whose notebook
        #    export raises the real MAX_NOTEBOOK_SIZE_EXCEEDED to prove the skip path.
        class _BigNbClient:
            def download_bytes(self, path, params=None, max_bytes=0):
                raise DownloadHTTPError(400, "MAX_NOTEBOOK_SIZE_EXCEEDED")
        nb_fetcher = ContentFetcher(_BigNbClient(), aw)
        res2 = nb_fetcher.fetch({"asset_type": "notebook",
                                 "natural_key": "/Users/x/hypothetical_big_nb",
                                 "payload": {"language": "PYTHON"}})
        print(f"\n[TEST 2] >10MB NOTEBOOK: status={res2.status} content_ref={res2.content_ref}")
        if res2.status == "skipped_oversize" and res2.content_ref is None:
            print(f"  ✓ PASS: notebook skipped (no bytes). reason: {res2.oversize.get('reason')[:80]}...")
        else:
            print("  ✗ FAIL: expected skipped_oversize with no content_ref"); ok = False

        # confirm real import-cap on the workspace (belt-and-suspenders): base64 import rejects.
        nb_data = b"# Databricks notebook source\n" + b"# y\n" * (3 * 1024 * 1024)  # ~12MB
        import base64
        r2 = requests.post(f"{host}/api/2.0/workspace/import", headers=H, json={
            "path": "/Users/abhishek.iyer@databricks.com/wsmig_bignb_livetest",
            "format": "SOURCE", "language": "PYTHON", "overwrite": "true",
            "content": base64.b64encode(nb_data).decode()})
        rejected = r2.status_code >= 400 and "size" in r2.text.lower()
        print(f"\n[TEST 3] base64 import of ~12MB notebook: HTTP {r2.status_code} "
              f"rejected_for_size={rejected}")
        if rejected:
            print("  ✓ PASS: workspace API rejects >10MB notebook import (confirms skip is correct)")
        else:
            print(f"  ! note: import returned {r2.status_code}: {r2.text[:120]}")
    finally:
        d = requests.post(f"{host}/api/2.0/workspace/delete", headers=H,
                          json={"path": big_path})
        print(f"\n[cleanup] deleted test file: HTTP {d.status_code}")

    print("\n=== RESULT:", "ALL PASS" if ok else "FAILURES", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
