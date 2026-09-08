"""
ConfigManager — centralises all runtime configuration for the workspace migration utility.

TWO connectivity modes, both supported (master §1a; the `connectivity_mode` widget picks one).
The ROLE is DERIVED from the pipeline stage + mode (PLAN 7 §C — `role_for_stage`), not a widget,
and there is a single `staging_location` widget rather than two:

  • MODE A `airgap` — the two-sided model. The tool runs on two sides that never talk to each other:
      - inventory/export (role=source): read THIS (source) workspace; WRITE the bundle to
        `staging_location`. Ops then physically moves the bundle.
      - import (role=target): READ the bundle from `staging_location`; write THIS (target) workspace.
  • MODE B `direct` (default) — EVERY stage runs in the TARGET workspace (role=target throughout).
    Inventory/export reach the SOURCE over REST using a source workspace-admin SP's client id +
    secret (OAuth M2M), and the bundle is written straight to the single `staging_location`, so
    there is no manual hop and the whole migration can run as one Job.

Both modes emit the IDENTICAL bundle, so transform/import are mode-agnostic — the mode only decides
*who reads the source* and *whether the file hop is manual*.

Auth: the workspace a notebook RUNS IN is always reached with the run-as SP's notebook-context
token. In `direct` mode the SOURCE is additionally reached via OAuth M2M (auth/token_manager.py).
No PATs in either mode. The M2M secret is NEVER persisted — `redacted()` strips it, and a test
asserts the literal appears in no written artifact.

Mirrors the `Config` dataclass pattern of uc-inventory-migration, EXTENDED to hold the derived role +
mode + the single staging location + per-asset TOGGLES (all default True) + transform options + the
target-side import controls (state table, selector, retry mode).
Config is WIDGET-based: `from_dbutils(..., stage=)` reads notebook widgets / job params. No config
files. (The old `source_/target_staging_location` widgets survive only as an upgrade fallback.)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

ROLE_SOURCE = "source"
ROLE_TARGET = "target"
_VALID_ROLES = (ROLE_SOURCE, ROLE_TARGET)

MODE_AIRGAP = "airgap"
MODE_DIRECT = "direct"
_VALID_MODES = (MODE_AIRGAP, MODE_DIRECT)

# Pipeline stages, so a notebook can DERIVE its role rather than expose a `role` widget (PLAN 7 §C):
# inventory + export are the SOURCE-READING stages (role=source in airgap, role=target in direct,
# because in direct EVERY stage runs in the target and reaches the source over REST); import is
# always the target stage.
STAGE_INVENTORY = "inventory"
STAGE_EXPORT = "export"
STAGE_IMPORT = "import"
_SOURCE_READING_STAGES = (STAGE_INVENTORY, STAGE_EXPORT)


def role_for_stage(stage: str, mode: str) -> str:
    """The role a given pipeline stage runs as, derived from the connectivity mode (PLAN 7 §C).

    This replaces the `role` widget: the role was never a free choice, it was always a function of
    "which stage is this" and "which mode". `import` is always target; the source-reading stages are
    source in airgap (they run inside the source) and target in direct (they run in the target and
    read the source over REST).
    """
    if stage == STAGE_IMPORT:
        return ROLE_TARGET
    if stage in _SOURCE_READING_STAGES:
        return ROLE_TARGET if mode == MODE_DIRECT else ROLE_SOURCE
    raise ValueError(f"unknown stage {stage!r}; expected one of "
                     f"{(STAGE_INVENTORY, STAGE_EXPORT, STAGE_IMPORT)}")

# The asset families `import_assets` can select, in phase order (Plan 3 §5, §6). `acls` is
# deliberately selectable ON ITS OWN — replaying permissions is the pass most likely to need a
# second attempt after identities are fixed up or a DAB redeploy lands.
IMPORT_FAMILIES = ("identity", "compute", "workspace", "secrets", "jobs", "sql", "dlt",
                   "dashboards", "genie", "serving", "misc", "acls")

# Retry modes (D22). ONE dropdown, not three booleans — booleans permit the meaningless
# `failed_only + skipped_only` combination ("both, or neither?"), a dropdown cannot be invalid.
RETRY_MODES = ("off", "failed_only", "skipped_only", "failed_and_skipped")

# Table names are owned by the TOOL, not the operator (D19): nothing to typo, and every one of
# the 100+ workspace pairs lands in the same place (every row keyed by source_workspace_id).
STATE_TABLE = "wsmig_migration_state"
IDENTITY_MAP_TABLE = "wsmig_identity_map"
STATE_TABLE_DRYRUN = "wsmig_migration_state_dryrun"


@dataclass
class WorkspaceContext:
    """THIS workspace's context (resolved from the run-as SP via SDK / notebook context)."""
    workspace_url: str = ""          # derived from context
    token: str = ""                  # notebook-context token of the run-as SP
    account_id: str = ""             # optional (target side); enables account-level preflight


@dataclass
class SourceConnection:
    """`direct`-mode only: how to reach the SOURCE workspace over REST (master §1a, Plan 3 §2a).

    `client_id` is an applicationId — not a secret. The SECRET arrives one of two supported ways
    and is never persisted:
      • preferred: `secret_scope` + `secret_key` pointers, read at runtime via
        `dbutils.secrets.get(scope, key)` in the TARGET workspace (Databricks auto-redacts those
        values from notebook output);
      • fallback: `spn_secret_value` typed into a widget — convenient for a first smoke test, but
        a widget value is visible on the Job/run page and retained in run history.
    """
    workspace_url: str = ""
    client_id: str = ""
    secret_scope: str = ""
    secret_key: str = ""
    spn_secret_value: str = ""       # NEVER persisted — stripped by Config.redacted()

    @property
    def uses_secret_scope(self) -> bool:
        """Whether the (preferred) scope+key path is fully configured."""
        return bool(self.secret_scope and self.secret_key)


@dataclass
class AssetToggles:
    """Per-asset migration switches. ALL default True; operator flips to False to skip."""
    identity: bool = True
    compute: bool = True
    workspace: bool = True
    secrets: bool = True
    jobs: bool = True
    sql: bool = True
    dlt: bool = True
    dashboards: bool = True
    genie: bool = True
    serving: bool = True
    misc: bool = True


@dataclass
class TransformConfig:
    """Mapping + exclude + schedule options applied inside 04_Import."""
    pause_job_schedules: bool = True
    user_domain_mapping: dict = field(default_factory=dict)   # old.com -> new.com
    user_id_mapping: dict = field(default_factory=dict)       # old@a.com -> new@b.com
    exclude_path_patterns: list = field(default_factory=list)
    exclude_job_name_patterns: list = field(default_factory=list)


@dataclass
class ImportOptions:
    """Target-side import controls (Plan 3 §3, §5, §7b, §7d)."""
    # Which families to import THIS session. Empty/["all"] = every family in the bundle. This is
    # SEPARATE from the migrate_* toggles and narrower: toggles are bundle scope (set identically
    # on both sides), the selector is this session's work list over what the bundle contains.
    import_assets: list = field(default_factory=lambda: ["all"])
    retry_mode: str = "off"
    state_catalog: str = ""          # required when dry_run=false; assumed to already exist
    state_schema: str = ""           # required when dry_run=false; assumed to already exist
    preflight_enforce: bool = True
    skip_manifest_verify: bool = False
    force_full_import: bool = False
    allow_deletes: bool = False              # D5 — deletes are never automatic
    library_force_start_clusters: bool = False   # D6 — never burn DBUs by default
    # PLAN 9: divert orphaned (deleted-in-source) home content to a top-level backup root instead
    # of failing it as prerequisite_missing. On by default; False restores the prerequisite
    # behaviour. The root defaults to /Users_Backup (a top-level create is allowed; only /Users is
    # protected) — normalised in validate() (leading /, no trailing /).
    workspace_home_backup: bool = True
    workspace_home_backup_root: str = "/Users_Backup"
    # Warehouse used by the state store when it runs OUTSIDE a notebook (no `spark`), e.g. the
    # live test harness driving the Statement Execution API. Blank in a notebook, where spark.sql
    # is used instead.
    state_warehouse_id: str = ""

    def selects(self, family: str) -> bool:
        """Whether `family` is in this session's work list."""
        sel = [s.strip().lower() for s in (self.import_assets or []) if str(s).strip()]
        if not sel or "all" in sel:
            return True
        return family in sel

    @property
    def selected_families(self) -> tuple:
        """The families this session will actually attempt, in phase order."""
        return tuple(f for f in IMPORT_FAMILIES if self.selects(f))


@dataclass
class Config:
    """Top-level runtime config for one workspace migration run."""
    role: str = ""                   # "source" | "target"; guards mis-runs
    connectivity_mode: str = MODE_AIRGAP
    ctx: WorkspaceContext = field(default_factory=WorkspaceContext)
    source: SourceConnection = field(default_factory=SourceConnection)
    toggles: AssetToggles = field(default_factory=AssetToggles)
    transform: TransformConfig = field(default_factory=TransformConfig)
    imports: ImportOptions = field(default_factory=ImportOptions)
    run_id: str = ""
    source_workspace_id: str = ""    # identifies the bundle: .../wsmig/<source_ws_id>/<run_id>
    # ONE staging location (PLAN 7 §C): a UC Volume path ("/Volumes/…"; managed or ADLS-backed
    # external volume; never raw abfss://). Each run writes/reads exactly one location — the airgap
    # file hop is just "source side sets location A, target side sets location B", two separate runs
    # each with their own single value. `source_staging_location`/`target_staging_location` are
    # retained ONLY as an upgrade fallback in `from_dbutils` so an in-flight job-param JSON that
    # still carries the old names keeps working; the merged value is the source of truth.
    staging_location_widget: str = ""
    source_staging_location: str = ""   # DEPRECATED (upgrade fallback only)
    target_staging_location: str = ""   # DEPRECATED (upgrade fallback only)
    dry_run: bool = True
    # Source-side safety caps (0 = unlimited), carried from the inventory script.
    max_scim: int = 0
    max_workspace_items: int = 0
    max_ws_api_calls: int = 0
    # PLAN 11 Finding-12: DAB bundle-root indicators for PATH-based DAB detection. Each entry is
    # either a folder-name glob (e.g. `.bundle`, `*.bundle`) or an absolute directory prefix (e.g.
    # `/Users/dab-deployer@corp.com`). Default `.bundle` = the CLI standard, byte-identical to the
    # pre-Finding-12 tool. Set on the SOURCE side (inventory/export); import consumes the STAMPED
    # `deployed_by_dab` flag but also carries this for any residual path re-derivation. Jobs/DLT
    # pipelines are UNAFFECTED (they use `deployment.kind`, the reliable field signal).
    dab_bundle_roots: list = field(default_factory=lambda: [".bundle"])

    # ── mode helpers ──────────────────────────────────────────────────────
    @property
    def is_direct(self) -> bool:
        return self.connectivity_mode == MODE_DIRECT

    @property
    def staging_location(self) -> str:
        """The single staging root for THIS run (PLAN 7 §C).

        Each run writes/reads exactly ONE location, so there is one `staging_location`. The old
        `source_staging_location`/`target_staging_location` are consulted ONLY as an upgrade
        fallback (an in-flight job-param JSON that still carries them), picked by role/mode exactly
        as before so nothing in flight breaks. New runs set `staging_location_widget` and the
        role/mode fallback never fires.
        """
        loc = self.staging_location_widget
        if not loc:
            if self.is_direct:
                loc = self.target_staging_location
            else:
                loc = (self.source_staging_location if self.role == ROLE_SOURCE
                       else self.target_staging_location)
        return (loc or "").rstrip("/")

    @property
    def output_path(self) -> str:
        """Run-isolated bundle dir: <staging_location>/wsmig/<source_ws_id>/<run_id>."""
        if not self.staging_location:
            raise ValueError("staging_location is empty for role=%r mode=%r"
                             % (self.role, self.connectivity_mode))
        if not self.source_workspace_id:
            raise ValueError("source_workspace_id is required to build the bundle path")
        if not self.run_id:
            raise ValueError("run_id is required to build the bundle path")
        return f"{self.staging_location}/wsmig/{self.source_workspace_id}/{self.run_id}"

    # ── state table FQNs (target side) ────────────────────────────────────
    def _state_fqn(self, table: str) -> str:
        if not (self.imports.state_catalog and self.imports.state_schema):
            return ""
        return f"{self.imports.state_catalog}.{self.imports.state_schema}.{table}"

    @property
    def state_table_fqn(self) -> str:
        """The main per-object state table — or the DRY-RUN twin when rehearsing.

        A rehearsal writes to a SEPARATE table (`…_dryrun`) rather than a `dry_run` column, so it
        can never pollute the real source→target id map, and dropping the rehearsal state is a
        one-line DROP TABLE.
        """
        return self._state_fqn(STATE_TABLE_DRYRUN if self.dry_run else STATE_TABLE)

    @property
    def identity_map_table_fqn(self) -> str:
        return self._state_fqn(IDENTITY_MAP_TABLE)

    @property
    def state_enabled(self) -> bool:
        """Whether the Delta state store is in play.

        Skipped entirely when `dry_run=true` AND no catalog/schema was given, so a first-look
        rehearsal needs no UC setup at all. With `dry_run=false` the catalog/schema are REQUIRED
        (validate() enforces it): without durable state every create risks becoming a duplicate on
        the next run, which is worse than not starting.
        """
        return bool(self.imports.state_catalog and self.imports.state_schema)

    # ── widget helpers ────────────────────────────────────────────────────
    @staticmethod
    def _widget(dbutils, name: str, default: str = "") -> str:
        """Read a widget value; return default if the widget is absent/blank."""
        try:
            val = dbutils.widgets.get(name)
        except Exception:
            return default
        return val if val not in (None, "") else default

    @classmethod
    def from_dbutils(cls, dbutils, spark, context_resolver=None, stage=None) -> "Config":
        """Build config from widgets / job params + this workspace's notebook context.

        `stage` (PLAN 7 §C) is the pipeline stage the calling notebook is — one of
        `inventory`/`export`/`import`. When given, the role is DERIVED from stage + mode and no
        `role` widget is read (the trimmed notebooks pass it). When omitted, the legacy `role`
        widget is honoured, so an in-flight job-param JSON still works.

        `context_resolver` is an optional callable returning a WorkspaceContext-like object
        (used for testing / to inject auth.token_manager.resolve_context). If None, the
        caller is expected to populate `cfg.ctx` afterwards (notebooks do this via
        auth.build_client). Widget parsing itself needs no workspace calls.
        """
        from src.utils.helpers import now_compact, parse_bool, parse_csv, parse_kv_list

        w = lambda n, d="": cls._widget(dbutils, n, d)  # noqa: E731

        mode = (w("connectivity_mode", MODE_DIRECT) or MODE_DIRECT).strip().lower()
        if mode not in _VALID_MODES:
            raise ValueError(f"widget `connectivity_mode` must be one of {_VALID_MODES}, "
                             f"got {mode!r}")

        if stage is not None:
            role = role_for_stage(stage, mode)   # derived, no `role` widget (PLAN 7 §C)
        else:
            role = (w("role") or "").strip().lower()
            if role not in _VALID_ROLES:
                raise ValueError(f"widget `role` must be one of {_VALID_ROLES}, got {role!r}")

        # ONE staging widget now (PLAN 7 §C), with the old two as an upgrade fallback so an
        # in-flight job-param JSON that still carries them keeps working.
        staging_widget = w("staging_location")

        toggles = AssetToggles(
            identity=parse_bool(w("migrate_identity", "true"), True),
            compute=parse_bool(w("migrate_compute", "true"), True),
            workspace=parse_bool(w("migrate_workspace", "true"), True),
            secrets=parse_bool(w("migrate_secrets", "true"), True),
            jobs=parse_bool(w("migrate_jobs", "true"), True),
            sql=parse_bool(w("migrate_sql", "true"), True),
            dlt=parse_bool(w("migrate_dlt", "true"), True),
            dashboards=parse_bool(w("migrate_dashboards", "true"), True),
            genie=parse_bool(w("migrate_genie", "true"), True),
            serving=parse_bool(w("migrate_serving", "true"), True),
            misc=parse_bool(w("migrate_misc", "true"), True),
        )

        transform = TransformConfig(
            pause_job_schedules=parse_bool(w("pause_job_schedules", "true"), True),
            user_domain_mapping=parse_kv_list(w("user_domain_mapping")),
            user_id_mapping=parse_kv_list(w("user_id_mapping")),
            exclude_path_patterns=parse_csv(w("exclude_path_patterns")),
            exclude_job_name_patterns=parse_csv(w("exclude_job_name_patterns")),
        )

        imports = ImportOptions(
            import_assets=parse_csv(w("import_assets", "all")) or ["all"],
            retry_mode=(w("retry_mode", "off") or "off").strip().lower(),
            state_catalog=w("state_catalog"),
            state_schema=w("state_schema"),
            preflight_enforce=parse_bool(w("preflight_enforce", "true"), True),
            skip_manifest_verify=parse_bool(w("skip_manifest_verify", "false"), False),
            force_full_import=parse_bool(w("force_full_import", "false"), False),
            allow_deletes=parse_bool(w("allow_deletes", "false"), False),
            library_force_start_clusters=parse_bool(
                w("library_force_start_clusters", "false"), False),
            state_warehouse_id=w("state_warehouse_id"),
            workspace_home_backup=parse_bool(w("workspace_home_backup", "true"), True),
            workspace_home_backup_root=w("workspace_home_backup_root", "/Users_Backup")
            or "/Users_Backup",
        )

        source = SourceConnection(
            workspace_url=(w("source_workspace_url") or "").rstrip("/"),
            client_id=w("source_sp_client_id"),
            secret_scope=w("source_sp_secret_scope"),
            secret_key=w("source_sp_secret_key"),
            spn_secret_value=w("spn_secret_value"),
        )

        cfg = cls(
            role=role,
            connectivity_mode=mode,
            source=source,
            toggles=toggles,
            transform=transform,
            imports=imports,
            run_id=w("run_id") or now_compact(),
            source_workspace_id=w("source_workspace_id"),
            staging_location_widget=staging_widget,
            source_staging_location=w("source_staging_location"),
            target_staging_location=w("target_staging_location"),
            dry_run=parse_bool(w("dry_run", "true"), True),
            max_scim=int(w("max_scim", "0") or 0),
            max_workspace_items=int(w("max_workspace_items", "0") or 0),
            max_ws_api_calls=int(w("max_ws_api_calls", "0") or 0),
            # Finding-12: CSV of bundle-root matchers; alias-accepts a single value. Default .bundle.
            dab_bundle_roots=parse_csv(w("dab_bundle_roots", ".bundle")) or [".bundle"],
        )
        cfg.ctx.account_id = w("account_id")

        if context_resolver is not None:
            cfg.ctx = context_resolver()

        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Fail fast on mis-configuration (wrong staging widget for the mode/role, etc.)."""
        if not self.source_workspace_id:
            raise ValueError("`source_workspace_id` is required")

        # A single staging location is required in every mode/role now (PLAN 7 §C) — the merged
        # `staging_location` property resolves the widget (or the old two as an upgrade fallback).
        if not self.staging_location:
            raise ValueError(
                "`staging_location` is required (a UC Volume path, e.g. /Volumes/cat/sch/vol). In "
                "airgap mode the source side sets its own location and the target side sets the "
                "location ops uploaded the bundle to; in direct mode both halves use the one "
                "target-side location.")

        if self.is_direct:
            if self.role != ROLE_TARGET:
                raise ValueError(
                    "connectivity_mode=direct runs every stage in the TARGET workspace, so "
                    f"role must be 'target' (got {self.role!r})")
            if not self.source.workspace_url:
                raise ValueError("connectivity_mode=direct requires `source_workspace_url`")
            if not self.source.client_id:
                raise ValueError(
                    "connectivity_mode=direct requires `source_sp_client_id` (the source "
                    "workspace-admin SP's applicationId — not a secret)")
            if not (self.source.uses_secret_scope or self.source.spn_secret_value):
                raise ValueError(
                    "connectivity_mode=direct needs the source SP secret via EITHER "
                    "`source_sp_secret_scope`+`source_sp_secret_key` (preferred — a widget value "
                    "is visible on the run page and kept in run history) OR `spn_secret_value`")

        if self.imports.retry_mode not in RETRY_MODES:
            raise ValueError(f"`retry_mode` must be one of {RETRY_MODES}, "
                             f"got {self.imports.retry_mode!r}")

        # Normalise the home-backup root: a leading /, no trailing / (PLAN 9 §5). A blank value
        # falls back to the default rather than becoming "" (which would build paths at the root).
        root = (self.imports.workspace_home_backup_root or "/Users_Backup").strip().rstrip("/")
        if not root.startswith("/"):
            root = "/" + root
        self.imports.workspace_home_backup_root = root or "/Users_Backup"

        unknown = [f for f in self.imports.import_assets
                   if f.strip().lower() not in IMPORT_FAMILIES + ("all",)]
        if unknown:
            raise ValueError(f"`import_assets` has unknown families {unknown}; "
                             f"valid: {('all',) + IMPORT_FAMILIES}")

        # A live import with no durable state is a correctness hazard, not an inconvenience: a
        # crash leaves no source→target id map, so the next run cannot take the UPDATE path and
        # may create duplicates. So the catalog/schema are mandatory once dry_run=false.
        if self.role == ROLE_TARGET and not self.dry_run and not self.state_enabled:
            raise ValueError(
                "dry_run=false requires `state_catalog` + `state_schema` (the shared, "
                "already-existing catalog+schema for the migration state table). Without durable "
                "state a re-run cannot tell CREATE from UPDATE and may duplicate objects.")

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        """Build config from a plain dict (for tests / SDK callers). Does NOT validate, so a test
        can construct a deliberately partial config."""
        cfg = cls(
            role=d.get("role", ""),
            connectivity_mode=d.get("connectivity_mode", MODE_AIRGAP),
            toggles=AssetToggles(**d.get("toggles", {})),
            transform=TransformConfig(**d.get("transform", {})),
            imports=ImportOptions(**d.get("imports", {})),
            source=SourceConnection(**d.get("source", {})),
            run_id=d.get("run_id", ""),
            source_workspace_id=d.get("source_workspace_id", ""),
            staging_location_widget=d.get("staging_location", d.get("staging_location_widget", "")),
            source_staging_location=d.get("source_staging_location", ""),
            target_staging_location=d.get("target_staging_location", ""),
            dry_run=d.get("dry_run", True),
            max_scim=d.get("max_scim", 0),
            max_workspace_items=d.get("max_workspace_items", 0),
            max_ws_api_calls=d.get("max_ws_api_calls", 0),
            dab_bundle_roots=d.get("dab_bundle_roots", [".bundle"]),
        )
        cfg.ctx = WorkspaceContext(**d.get("ctx", {}))
        return cfg

    def redacted(self) -> dict:
        """Config as a dict with EVERY credential removed (for config_resolved.json).

        Two secrets exist and both must go: the workspace context token, and — on the widget
        path — the source SP's OAuth secret. The scope/key NAMES are kept (they're pointers, not
        secrets, and they document which credential a run used). A test asserts the literal
        `spn_secret_value` appears in no written artifact or log line.
        """
        d = asdict(self)
        if isinstance(d.get("ctx"), dict):
            d["ctx"] = {k: v for k, v in d["ctx"].items() if k != "token"}
        if isinstance(d.get("source"), dict):
            src = {k: v for k, v in d["source"].items() if k != "spn_secret_value"}
            # Say WHICH path supplied the secret, so an auditor can tell without seeing it.
            src["secret_source"] = ("secret_scope" if self.source.uses_secret_scope
                                    else ("widget" if self.source.spn_secret_value else "none"))
            d["source"] = src
        return d

    def resolve_source_secret(self, dbutils=None) -> str:
        """The source SP's OAuth secret for `direct` mode (Plan 3 §2a). Never logged, never stored.

        Precedence is explicit so there is never doubt about which credential a run used:
          scope+key (if BOTH set) → `spn_secret_value` → fail fast naming both options.
        The scope path wins when present, so a customer who has set up a scope can leave
        `spn_secret_value` blank (or stale) with no surprise.
        """
        if self.source.uses_secret_scope:
            if dbutils is None:
                raise RuntimeError(
                    "`source_sp_secret_scope`/`source_sp_secret_key` are set but no dbutils was "
                    "passed, so the secret cannot be read. Pass dbutils, or use spn_secret_value.")
            return dbutils.secrets.get(scope=self.source.secret_scope, key=self.source.secret_key)
        if self.source.spn_secret_value:
            return self.source.spn_secret_value
        raise RuntimeError(
            "No source SP secret available. Set EITHER `source_sp_secret_scope` + "
            "`source_sp_secret_key` (preferred) OR `spn_secret_value`.")
