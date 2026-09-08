"""
preflight — the pre-import gate (Plan 3 §9, D8, D14). **VERIFY ONLY: it creates nothing, ever.**

It runs per workspace BEFORE each import (the account-level checks are the once-per-account part;
everything else is per-pair). What it prevents is worth stating plainly: importing against a target
that is missing its account identities produces thousands of half-migrated ACLs, which is far more
work to unwind than to prevent.

Findings are GRADED, because blocking a whole migration on a repo nobody references would be wrong:

  BLOCKING  — import cannot produce a correct target (bad bundle, missing state schema, no admin
              rights, unauthenticatable source). With `preflight_enforce=true` (default) `04_Import`
              never runs behind a NO-GO.
  DEGRADING — import proceeds, but SPECIFIC NAMED units will be incomplete (unassigned account
              identities, unrecreated Git repos, unpopulated secret values, absent UC tables).
  COSMETIC  — no effect on other assets (a legacy dashboard to rebuild).

So the answer to "must every manual step be done first?" is not "all or nothing" — it is
"preflight tells you which grade you are in, and blocks only when proceeding would be wrong."
"""
from __future__ import annotations

from src.exporters import bundle_paths as BP
from src.utils.helpers import now_iso, safe_str
from src.utils.logger import get_logger

_LOG = get_logger("preflight")

BLOCKING = "BLOCKING"
DEGRADING = "DEGRADING"
COSMETIC = "COSMETIC"

GO = "GO"
GO_WITH_WARNINGS = "GO-WITH-WARNINGS"
NO_GO = "NO-GO"


class Preflight:
    """Runs every gate check and returns a graded verdict. Never mutates the target."""

    def __init__(self, client, config, artifact_writer, state=None, dbutils=None,
                 source_client=None) -> None:
        self.client = client
        self.config = config
        self.aw = artifact_writer
        self.state = state
        self.dbutils = dbutils
        self.source_client = source_client
        self.findings: list[dict] = []

    # ── finding helpers ───────────────────────────────────────────────────
    def _add(self, check: str, ok: bool, grade: str, detail: str = "",
             affected: list = None) -> None:
        """Record one check. `affected` NAMES the units an unmet prerequisite will damage.

        Naming them is the point: "some ACLs may be wrong" is unactionable, "these 14 notebooks
        under /Users/leaver@corp.com will not import" is a task.
        """
        self.findings.append({"check": check, "ok": bool(ok), "grade": grade, "detail": detail,
                              "affected_units": list(affected or [])})
        level = "ok" if ok else grade
        (_LOG.info if ok else _LOG.warning)("preflight check", check=check, result=level,
                                           detail=detail[:200])

    # ── the checks ────────────────────────────────────────────────────────
    def check_bundle(self) -> None:
        """BLOCKING: a partial upload must never present as a partial migration (D7)."""
        if self.config.imports.skip_manifest_verify:
            self._add("bundle integrity", True, DEGRADING,
                      "manifest verification was SKIPPED by skip_manifest_verify=true — the bundle "
                      "was not checked for completeness")
            return
        verify = self.aw.verify_manifest()
        if verify["ok"]:
            self._add("bundle integrity", True, BLOCKING,
                      f"{len(verify['manifest'].get('files', []))} files checksummed OK")
            return
        self._add("bundle integrity", False, BLOCKING,
                  f"{len(verify['missing'])} missing and {len(verify['mismatched'])} corrupted "
                  f"file(s): {(verify['missing'] + verify['mismatched'])[:5]}. Re-copy the whole run "
                  f"directory from the source staging location.")

    def check_staging_readable(self) -> None:
        """BLOCKING: if the bundle dir isn't listable there is nothing to import."""
        import os
        root = self.aw.root
        try:
            listed = os.listdir(root)
            self._add("staging readable from target", bool(listed),
                      BLOCKING, f"{len(listed)} entries in {root}")
        except Exception as exc:  # noqa: BLE001
            self._add("staging readable from target", False, BLOCKING,
                      f"cannot list {root}: {exc}. In airgap mode, confirm ops uploaded the run "
                      f"directory to target_staging_location and the workspace can read that Volume.")

    def check_target_admin(self) -> None:
        """BLOCKING: without workspace-admin, identity and permissions calls fail en masse."""
        try:
            self.client.get("api/2.0/preview/scim/v2/Groups", params={"count": 1})
            scim_ok = True
        except Exception as exc:  # noqa: BLE001
            scim_ok = False
            self._add("target workspace-admin (SCIM readable)", False, BLOCKING,
                      f"cannot read SCIM Groups on the target: {str(exc)[:200]}. The run-as identity "
                      f"must be a workspace admin.")
        if scim_ok:
            self._add("target workspace-admin (SCIM readable)", True, BLOCKING)

    def check_source_connectivity(self) -> None:
        """BLOCKING in `direct` mode: if the source can't be read there is nothing to export.

        Two checks, because they fail for different reasons: the token must MINT (wrong client id /
        secret), and it must reach an ADMIN-ONLY endpoint (the SP isn't a workspace admin). Probing
        both here means a mis-scoped SP fails in 2 seconds rather than halfway through a 40-minute
        inventory.
        """
        if not self.config.is_direct:
            self._add("source connectivity", True, BLOCKING,
                      "airgap mode — the source is never called; the bundle is the only thing that "
                      "crosses")
            return
        if self.source_client is None:
            self._add("source connectivity", False, BLOCKING,
                      "direct mode, but no source client was built — check source_workspace_url, "
                      "source_sp_client_id and the secret (scope+key, or spn_secret_value)")
            return
        try:
            doc = self.source_client.get("api/2.0/preview/scim/v2/Groups", params={"count": 1})
            self._add("source connectivity (OAuth M2M + admin scope)", True, BLOCKING,
                      f"token minted and SCIM readable on the source "
                      f"(totalResults={doc.get('totalResults')})")
        except Exception as exc:  # noqa: BLE001
            self._add("source connectivity (OAuth M2M + admin scope)", False, BLOCKING,
                      f"could not read the source over REST: {str(exc)[:250]}. Either the token did "
                      f"not mint (check client id / secret) or the SP is not a workspace admin on "
                      f"the source.")

    def check_state_schema(self) -> None:
        """BLOCKING when live: without durable state, a re-run cannot tell CREATE from UPDATE.

        That is a correctness hazard rather than an inconvenience — every create risks becoming a
        duplicate on the next run — so it blocks a live import but not a rehearsal.
        """
        if self.config.dry_run and not self.config.state_enabled:
            self._add("migration state table", True, DEGRADING,
                      "dry run with no state_catalog — the rehearsal needs no UC setup, but a LIVE "
                      "import will require state_catalog + state_schema")
            return
        if not self.config.state_enabled:
            self._add("migration state table", False, BLOCKING,
                      "dry_run=false requires state_catalog + state_schema (a shared, already-"
                      "existing catalog+schema). Without durable state a re-run cannot distinguish "
                      "CREATE from UPDATE and may duplicate objects.")
            return
        if self.state is None:
            self._add("migration state table", False, BLOCKING,
                      "no state store was built — check that spark (in a notebook) or "
                      "state_warehouse_id (off-cluster) is available")
            return
        try:
            self.state.ensure_table()
            self.state.load(force=True)
            self._add("migration state table", True, BLOCKING,
                      f"{self.config.state_table_fqn} reachable and writable "
                      f"({len(self.state._cache)} existing rows for this workspace pair)")
        except Exception as exc:  # noqa: BLE001
            self._add("migration state table", False, BLOCKING, str(exc)[:400])

    def check_account_identities(self) -> None:
        """DEGRADING per identity: an account identity not on target cannot be created by this tool.

        WARN rather than BLOCK, and each missing identity NAMES itself, because the fix is a
        customer-IT / account-admin action (Entra SCIM assignment) and the rest of the migration is
        still worth running. Every affected unit is listed so the operator knows the cost of
        proceeding.
        """
        classification = self.aw.read_json(BP.IDENTITY_CLASSIFICATION_JSON) or {}
        identities = classification.get("identities") or []
        if not identities:
            self._add("account identities present on target", True, DEGRADING,
                      "no identity classification in the bundle — nothing to verify")
            return

        # Plan 6: users and SPs no longer need pre-assignment — the workspace SCIM POST creates
        # them at the account and assigns them, and an SP POST carrying `applicationId` ADOPTS the
        # existing account SP with its id intact. So the only identity that can still block is an
        # account GROUP, which must already exist in the target account before it can be assigned.
        def _kind(identity: dict) -> str:
            return safe_str(identity.get("kind")) or safe_str(identity.get("classification"))

        account_managed = [i for i in identities
                           if i.get("identity_type") == "group"
                           and _kind(i) in ("account", "account_group")]
        if not account_managed:
            self._add("account groups present on target account", True, DEGRADING,
                      "the bundle has no account groups — every group is workspace-local and will "
                      "be recreated by this tool; users and SPs are assigned automatically")
            return

        target_users, target_sps, target_groups = set(), set(), set()
        try:
            target_users = {safe_str(u.get("userName")) for u in self.client.get_scim("Users")}
            target_sps = {safe_str(s.get("applicationId"))
                          for s in self.client.get_scim("ServicePrincipals")}
            target_groups = {safe_str(g.get("displayName"))
                             for g in self.client.get_scim("Groups")}
        except Exception as exc:  # noqa: BLE001
            self._add("account identities present on target", False, BLOCKING,
                      f"could not list target identities: {str(exc)[:200]}")
            return

        # A workspace-local group on TARGET holding an account group's name is worse than missing:
        # it permanently blocks the assignment (Plan 6 F6) and needs a manual delete, so it is
        # reported separately and as BLOCKING for that group.
        target_group_kinds = {}
        try:
            for g in self.client.get_scim("Groups"):
                target_group_kinds[safe_str(g.get("displayName"))] = safe_str(
                    (g.get("meta") or {}).get("resourceType")).lower()
        except Exception:  # noqa: BLE001 — already reported above if listing failed
            pass

        missing, shadowed = [], []
        for identity in account_managed:
            key = safe_str(identity.get("displayName"))
            if not key:
                continue
            if target_group_kinds.get(key) == "workspacegroup":
                shadowed.append(key)
            elif key not in target_groups:
                missing.append(
                    f"{key} ({'Entra/SCIM' if identity.get('entra_backed') else 'account admin'})")

        if shadowed:
            self._add(
                "account groups not shadowed on target", False, BLOCKING,
                f"{len(shadowed)} account group(s) already exist on the TARGET as WORKSPACE-LOCAL "
                f"groups of the same name. That shadow permanently blocks assigning the real "
                f"account group ('Workspace group with name X already exists'). Delete each "
                f"workspace-local group on target, then re-run.", affected=shadowed)
        else:
            self._add("account groups not shadowed on target", True, BLOCKING,
                      "no account group is shadowed by a workspace-local group of the same name")

        if missing:
            self._add(
                "account groups present on target account", False, DEGRADING,
                f"{len(missing)} of {len(account_managed)} account groups are not yet in the "
                f"target account/workspace. They cannot be created by this tool (that would make a "
                f"workspace-local shadow), so Entra SCIM or an account admin must provision them; "
                f"then re-run with retry_mode=failed_only. Users and SPs are unaffected — they are "
                f"assigned automatically.", affected=missing)
        else:
            self._add("account groups present on target account", True, DEGRADING,
                      f"all {len(account_managed)} account groups are present")

    def check_account_admin_capability(self) -> None:
        """DEGRADING: account-admin is OPTIONAL — it only unlocks auto-assignment.

        The credential baseline is workspace-admin. Without account-admin, unassigned account
        identities become a customer-IT prerequisite that is REPORTED rather than a failure.
        """
        account_id = safe_str(self.config.ctx.account_id)
        if not account_id:
            self._add("account-admin capability", True, DEGRADING,
                      "no account_id supplied — account-level assignment is not attempted; any "
                      "unassigned identity is a customer-IT prerequisite (this is the expected "
                      "workspace-admin baseline)")
            return
        self._add("account-admin capability", True, DEGRADING,
                  f"account_id {account_id} supplied; unassigned identities will be reported for "
                  f"account-admin action rather than created")

    def check_warehouses(self) -> None:
        """DEGRADING: without a warehouse, queries/dashboards/genie import but cannot run."""
        try:
            warehouses = (self.client.get("api/2.0/sql/warehouses") or {}).get("warehouses") or []
        except Exception as exc:  # noqa: BLE001
            self._add("target SQL warehouse availability", False, DEGRADING, str(exc)[:200])
            return
        if warehouses:
            self._add("target SQL warehouse availability", True, DEGRADING,
                      f"{len(warehouses)} warehouse(s) on target")
        else:
            self._add("target SQL warehouse availability", False, DEGRADING,
                      "the target has NO SQL warehouse. Warehouses in the bundle will be created, "
                      "but any query/alert/dashboard/genie space imported before them cannot be "
                      "attached to one — run the `sql` family first.")

    def check_akv_scopes(self) -> None:
        """DEGRADING: AKV-backed scopes need an AAD token AND vault access (§6c/D4).

        Probed up front so this is known BEFORE the secrets phase rather than during it, and the two
        causes are reported separately because the customer fixes them differently.
        """
        index = self.aw.read_json(BP.EXPORT_INDEX_JSON) or {}
        scopes = self.aw.read_json("export/secrets/scopes.json") or {}
        akv = [u for u in (scopes.get("units") or [])
               if safe_str((u.get("payload") or {}).get("backend_type")).upper() == "AZURE_KEYVAULT"]
        if not akv:
            self._add("Azure Key Vault-backed secret scopes", True, DEGRADING,
                      "the bundle has no AKV-backed scopes")
            return
        vaults = sorted({safe_str(((u.get("payload") or {}).get("keyvault_metadata") or {})
                                  .get("dns_name")) for u in akv})
        self._add(
            "Azure Key Vault-backed secret scopes", False, DEGRADING,
            f"{len(akv)} AKV-backed scope(s) reference vault(s) {vaults}. Two prerequisites: (1) the "
            f"run-as identity must be an Entra SP / managed identity so an AZURE AD token can be "
            f"minted (a Databricks token cannot make the linking call), and (2) that identity needs "
            f"`get`+`list` ON THE VAULT (access policy or Key Vault Secrets User). NOTE these are "
            f"region-1 vaults being read by a region-2 workspace — a deliberate cross-region "
            f"dependency per the customer's decision.",
            affected=[safe_str(u.get("natural_key")) for u in akv])

    def check_uc_references(self) -> None:
        """DEGRADING: UC is out of scope, and this is the top cause of a 'successful' broken import.

        A dashboard/genie space/pipeline can import perfectly and still be unusable because its
        tables aren't on target. Flagging it here is the difference between a known gap and a
        confused customer.
        """
        counts = {"lakeview_dashboard": 0, "genie_space": 0, "dlt_pipeline": 0}
        affected: list[str] = []
        for rel, asset_type in (("export/dashboards/lakeview.json", "lakeview_dashboard"),
                                ("export/genie/spaces.json", "genie_space"),
                                ("export/dlt/pipelines.json", "dlt_pipeline")):
            doc = self.aw.read_json(rel) or {}
            for unit in doc.get("units") or []:
                payload = unit.get("payload") or {}
                blob = safe_str(payload.get("serialized_dashboard")
                                or payload.get("serialized_space")
                                or payload.get("catalog"))
                if blob:
                    counts[asset_type] += 1
                    affected.append(f"{asset_type}:{safe_str(unit.get('natural_key'))}")
        total = sum(counts.values())
        if not total:
            self._add("Unity Catalog prerequisites", True, DEGRADING,
                      "no UC-referencing assets in the bundle")
            return
        self._add(
            "Unity Catalog prerequisites", False, DEGRADING,
            f"{total} asset(s) reference Unity Catalog by fully-qualified name "
            f"({', '.join(f'{k}={v}' for k, v in counts.items() if v)}). UC is OUT OF SCOPE for this "
            f"utility, so those catalogs/schemas/tables must already exist on the target — otherwise "
            f"these import successfully but render empty or fail on first run. This is the single "
            f"most common cause of a 'clean' import producing a broken dashboard.",
            affected=affected[:50])

    def check_job_notebook_paths(self) -> None:
        """DEGRADING: a job whose notebook_path is missing CREATES FINE and fails at first RUN.

        Which is exactly why this is a static pre-check: relying on create failures would never
        surface it, and the customer would find out in production (D14).
        """
        jobs = self.aw.read_json("export/jobs.json") or {}
        content_paths = set()
        for rel in ("export/workspace/objects.json",):
            doc = self.aw.read_json(rel) or {}
            for unit in doc.get("units") or []:
                content_paths.add(safe_str(unit.get("natural_key")))

        unresolvable: list[str] = []
        for unit in jobs.get("units") or []:
            for task in (unit.get("payload") or {}).get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                spec = task.get("notebook_task")
                if not isinstance(spec, dict):
                    continue
                path = safe_str(spec.get("notebook_path"))
                if not path or path in content_paths:
                    continue
                if self._exists_on_target(path):
                    continue
                unresolvable.append(f"{safe_str(unit.get('natural_key'))} → {path}")

        if unresolvable:
            self._add(
                "job/pipeline notebook paths resolvable", False, DEGRADING,
                f"{len(unresolvable)} task path(s) are neither in the bundle nor on the target. The "
                f"Jobs API does NOT validate paths, so these jobs will be created successfully and "
                f"FAIL AT FIRST RUN — usually a notebook inside a Git folder, which is out of scope "
                f"for import and must be recreated by hand.", affected=unresolvable[:50])
        else:
            self._add("job/pipeline notebook paths resolvable", True, DEGRADING)

    def check_repos(self) -> None:
        """DEGRADING: repos are out of scope for import (D9) — listed so nobody is surprised."""
        repos = self.aw.read_json("export/workspace/repos.json") or {}
        units = repos.get("units") or []
        if not units:
            self._add("Git repos (out of scope for import)", True, COSMETIC,
                      "the bundle has no repos")
            return
        self._add(
            "Git repos (out of scope for import)", False, DEGRADING,
            f"{len(units)} Git repo(s) will NOT be created on target (out of scope by decision). "
            f"Recreate each by hand from its url/provider/branch/path in manual_actions, then re-run "
            f"with import_assets=acls + retry_mode=skipped_only to apply their permissions. Any job "
            f"referencing a notebook inside one will fail at first run until then.",
            affected=[safe_str(u.get("natural_key")) for u in units])

    def check_legacy_dashboards(self) -> None:
        """COSMETIC: rebuilt by hand, and affects nothing else."""
        doc = self.aw.read_json("export/sql/legacy_dashboards.json") or {}
        units = doc.get("units") or []
        if not units:
            self._add("legacy SQL dashboards", True, COSMETIC, "none in the bundle")
            return
        self._add("legacy SQL dashboards", False, COSMETIC,
                  f"{len(units)} legacy SQL dashboard(s) cannot be created via the API on modern "
                  f"workspaces — rebuild them as AI/BI dashboards. Their underlying queries DO "
                  f"migrate, so only the visual layout is hand-built.",
                  affected=[safe_str(u.get("natural_key")) for u in units])

    def _exists_on_target(self, path: str) -> bool:
        try:
            return bool(self.client.get("api/2.0/workspace/get-status", params={"path": path}))
        except Exception:  # noqa: BLE001
            return False

    # ── run + verdict ─────────────────────────────────────────────────────
    def run(self) -> dict:
        """Run every check and return the graded verdict. Creates nothing."""
        # Named explicitly rather than derived from the method, so a finding is always labelled with
        # the CHECK's name — `__name__` gives "<lambda>" for anything wrapped or monkeypatched, which
        # would leave the operator unable to tell which check broke.
        checks = (
            ("bundle integrity", self.check_bundle),
            ("staging readable from target", self.check_staging_readable),
            ("target workspace-admin", self.check_target_admin),
            ("source connectivity", self.check_source_connectivity),
            ("migration state table", self.check_state_schema),
            ("account identities present on target", self.check_account_identities),
            ("account-admin capability", self.check_account_admin_capability),
            ("target SQL warehouse availability", self.check_warehouses),
            ("Azure Key Vault-backed secret scopes", self.check_akv_scopes),
            ("Unity Catalog prerequisites", self.check_uc_references),
            ("job/pipeline notebook paths resolvable", self.check_job_notebook_paths),
            ("Git repos (out of scope for import)", self.check_repos),
            ("legacy SQL dashboards", self.check_legacy_dashboards),
        )
        for name, check in checks:
            try:
                check()
            except Exception as exc:  # noqa: BLE001 — a check that errors must not hide the others
                self._add(name, False, DEGRADING,
                          f"the check itself failed to run: {str(exc)[:200]}")

        blocking = [f for f in self.findings if not f["ok"] and f["grade"] == BLOCKING]
        degrading = [f for f in self.findings if not f["ok"] and f["grade"] == DEGRADING]
        cosmetic = [f for f in self.findings if not f["ok"] and f["grade"] == COSMETIC]

        verdict = NO_GO if blocking else (GO_WITH_WARNINGS if degrading else GO)
        report = {
            "verdict": verdict,
            "run_id": self.config.run_id,
            "source_workspace_id": self.config.source_workspace_id,
            "connectivity_mode": self.config.connectivity_mode,
            "dry_run": self.config.dry_run,
            "generated_utc": now_iso(),
            "blocking": [f"{f['check']}: {f['detail']}" for f in blocking],
            "degrading": [f"{f['check']}: {f['detail']}" for f in degrading],
            "cosmetic": [f"{f['check']}: {f['detail']}" for f in cosmetic],
            "findings": self.findings,
        }
        # PLAN 7 §B2: preflight prints its graded verdict inline in the notebook, so the HTML twin
        # was redundant and is gone. The machine-readable `misc/preflight_report.json` stays (small,
        # for audit; the manifest already excludes it).
        self.aw.write_json(BP.PREFLIGHT_REPORT_JSON, report)
        _LOG.info("preflight verdict", verdict=verdict, blocking=len(blocking),
                  degrading=len(degrading), cosmetic=len(cosmetic))
        return report
