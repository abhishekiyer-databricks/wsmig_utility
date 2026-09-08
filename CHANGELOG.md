# Changelog

All notable changes to the Workspace Migration Utility are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (`MAJOR.MINOR.PATCH`). The
version here is the single source of truth in [`VERSION`](VERSION); the tool stamps it into every
bundle's `manifest.json`, `export_index.json`, and the migration state table.


## [Unreleased]

_Nothing yet._

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
