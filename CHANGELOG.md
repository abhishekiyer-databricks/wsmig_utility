# Changelog

All notable changes to the Workspace Migration Utility are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (`MAJOR.MINOR.PATCH`). The
version here is the single source of truth in [`VERSION`](VERSION); the tool stamps it into every
bundle's `manifest.json`, `export_index.json`, and the migration state table.


## [Unreleased]

### Changed
- **Import state writes no longer commit once per failed object** (PLAN 16.1 §4.4). The migration
  state table and the import checkpoint are now written every 200 objects **or 5 minutes**, whichever
  comes first, plus at every phase end and run end. Before, every failure forced its own Delta MERGE
  (~3–7 s each, plus auto-OPTIMIZE churn), so runs with many failures spent most of their time in
  bookkeeping. A hard crash loses at most < 200 objects / 5 minutes of bookkeeping; the re-run adopts
  them (dashboards / Genie / alerts / legacy queries may be duplicated — see the Runbook).

### Fixed
- The import checkpoint now records each outcome's `asset_type`, so the start-of-run recovery replay
  restores lost rows for multi-type families (identity, compute, workspace, secrets, SQL, misc), not
  only single-type ones. Older checkpoints still replay as before.

## [1.0.0] - 2026-09-07

First production release.

### Added
- Notebook-based migration of **non-UC workspace assets** from a source to a target Databricks
  workspace (Azure region → Azure region).
- **Two connectivity modes:** `airgap` (two-sided, manual bundle handoff, no cross-workspace
  connectivity) and `direct` (default; all stages run in the target, source read over OAuth M2M).
- **Four notebooks** — `01_Inventory`, `02_Export`, `04_Import`, `00_Install_Jobs` — plus
  pre-packaged Jobs API 2.2 definitions in `jobs/`.
- **Identity migration** including Databricks-managed groups (membership + entitlements), with a
  persisted `old → new` id map.
- **Idempotent, incremental runs** via a Delta state store; **dry-run** default; a graded
  **preflight** go/no-go gate; per-unit **fail-soft** execution.
- **ACL replay** in a final phase with a source-vs-target parity report.
- Per-asset-type **Excel reports** and a **manual-actions** runbook in each bundle.
- Configurable DAB detection (`dab_bundle_roots`).
- Full **documentation set** under `docs/` (architecture, configuration, runbook, permissions).

[Unreleased]: https://example.com/compare/v1.0.0...HEAD
[1.0.0]: https://example.com/releases/tag/v1.0.0
