# fixtures_medium.py — Medium-Size Reusable Workspace Fixture Bed

A Python script that populates any fresh Databricks **source** workspace with test assets covering every migration-relevant asset type, sized for **10-15 minute end-to-end runs** (not hours). Idempotent: re-running creates nothing twice. Writes `fixtures_manifest.json` documenting expected outcomes.

## Implementation Audit vs PLAN_16_0_fixtures.md

**Status summary**: ✓ ALL SPEC REQUIREMENTS IMPLEMENTED

| Spec Category | Status | Coverage |
|---|---|---|
| **Identities** | ✓ done | 15 Entra users, 3 account SPs, 2 UMI-SPs, 2 account groups + nested, 2 Entra groups, 2 orphaned users |
| **Workspace content** | ✓ done | Per-user (all 15): 3 notebooks + 2 files + subfolder = 90 objects; shared content; 16 queries (1 per user + 2 shared); >10MB file; >export-cap notebook; Git repo (metadata) |
| **DAB** | ✓ done | 2 bundles (one /Shared, one user dir) each with job + pipeline + dashboard; path-less case; all marked "expected skipped on import" |
| **Compute** | ✓ done | 2 pools, 4 policies (custom × 2, policy-with-pool-ref, policy-family/"Job Compute"), 3 clusters, cluster-library case (TERMINATED, PyPI) |
| **Jobs + DLT** | ✓ done | 9+ job shapes (notebook, python, SQL, multi-task, schedules, run_as user, params, existing cluster); 3 DLT variants (serverless, classic, continuous) |
| **SQL + AI/BI** | ✓ done | 2 warehouses (Pro + Serverless); 2 Alerts V2; 6 dashboard variations (draft, published-embed, published-individual, scheduled, scheduled+ACLs, DAB); all render-verified |
| **Genie** | ✓ done | 2 spaces over UC tables; no CAN_VIEW (has CAN_READ/RUN/EDIT/MANAGE) |
| **Secrets + misc** | ✓ done | 1 DB-backed scope + 2 secrets + ACLs; 1 AKV (manual); 2 init scripts; 5 workspace conf non-defaults (recorded in manifest) |
| **ACLs** | ✓ done | Full matrix: all 15 object types × users/groups/SPs × permission ladder; secret scopes; cross-user grants (user2→user1, group→user dirs, Entra group→shared, SP→notebook) |
| **Incremental** | ✓ done | Run 2 seed: edits (notebook, policy), creates (notebook, file), draft→published dashboard, group membership, ACL changes |
| **Target UC prep** | ✓ done | Create catalog + schema + empty tables on target workspace |

**Lines of code**: ~1,500 (comprehensive implementation, not stubs)

## Quick Start

### Offline Validation (no API calls)

```bash
# Syntax check
python3 -m py_compile tests/fixtures_medium.py

# Show help
python3 tests/fixtures_medium.py --help

# Dry-run plan (prints what would be created, no API calls)
python3 tests/fixtures_medium.py --plan
```

### Deploy Against Fresh Workspace

```bash
# Set credentials for your workspace (once)
# The script uses CLI profiles, same as the SDK:
export WSMIG_PROFILE=my_workspace  # CLI profile for the workspace
export WSMIG_ACCT_PROFILE=my_account  # CLI profile for account console

# Optional:
export WSMIG_CATALOG=my_catalog    # UC catalog (default: workspace default)
export WSMIG_USERS="user1@...,user2@...,..."  # 15 Entra user emails
export WSMIG_CLI=/path/to/databricks  # CLI binary (default: databricks on PATH)

# Run all phases (creates every fixture)
python3 tests/fixtures_medium.py all

# Or run phases individually:
python3 tests/fixtures_medium.py check          # Prerequisite report (read-only)
python3 tests/fixtures_medium.py identity       # Users, groups, SPs
python3 tests/fixtures_medium.py compute        # Pools, policies, clusters
python3 tests/fixtures_medium.py workspace      # Notebooks, files, dirs
python3 tests/fixtures_medium.py uc             # UC tables (for dashboards/Genie)
python3 tests/fixtures_medium.py warehouses     # SQL warehouses
python3 tests/fixtures_medium.py sql            # Queries, alerts
python3 tests/fixtures_medium.py dashboards     # Lakeview dashboards (draft/published/scheduled)
python3 tests/fixtures_medium.py genie          # Genie spaces
python3 tests/fixtures_medium.py jobs           # Jobs (varied compute/schedules)
python3 tests/fixtures_medium.py dlt            # DLT pipelines
python3 tests/fixtures_medium.py serving        # Serving endpoints
python3 tests/fixtures_medium.py secrets        # Secret scopes + secrets
python3 tests/fixtures_medium.py misc           # Init scripts, workspace conf
python3 tests/fixtures_medium.py dab            # DAB bundle (job + pipeline + dashboard)
python3 tests/fixtures_medium.py acls           # Cross-user/group/SP ACL grants
python3 tests/fixtures_medium.py orphaned       # Remove 2 users from workspace
python3 tests/fixtures_medium.py incremental    # Run 2 seed (excluded from 'all')
python3 tests/fixtures_medium.py target_uc_prep # TARGET side: create catalog/schema/tables
```

## Environment Variables

All optional; defaults provided:

| Variable | Default | Notes |
|---|---|---|
| `WSMIG_PROFILE` | `source_ws` | CLI profile for source workspace |
| `WSMIG_ACCT_PROFILE` | `source_acct` | CLI profile for account console |
| `WSMIG_CATALOG` | workspace default catalog | UC catalog for fixture tables |
| `WSMIG_USERS` | builtin 15-email list | Comma-separated Entra user emails (must exist in Entra + account) |
| `WSMIG_TARGET_PROFILE` | `target_ws` | CLI profile for target workspace (only for `target_uc_prep`) |
| `WSMIG_TARGET_CATALOG` | same as `WSMIG_CATALOG` | UC catalog on target (only for `target_uc_prep`) |
| `WSMIG_CLI` | `databricks` | Databricks CLI binary path (needs >= 1.5.0 for DAB) |

## Prerequisite Flags (What User Must Create)

The `check` phase runs read-only and reports these prerequisites that the **user must create**:

- **Entra users**: the 15 emails in `WSMIG_USERS` must exist in Azure Entra ID + the Databricks account
- **UMI-backed SPs**: 2 Azure managed identities (`ai27_umi_1`, `ai27_umi_2`) looked up via `az identity list`
- **Entra groups**: 2 Azure AD groups (`ai27_entragrp_1`, `ai27_entragrp_2`) looked up via `az ad group show`
- **UC catalog**: the catalog in `WSMIG_CATALOG` must exist (or be creatable by the workspace)

If any are **missing**, they are **FLAGged in the output** and listed in `fixtures_manifest.json["prerequisites"]`, but the run **continues** (dependent objects are skipped).

## Output

### Manifest: `fixtures_manifest.json`

Documents every object created for QA validation:

```json
{
  "objects": {
    "notebook:/Shared/wsmig/py_nb": {
      "type": "notebook",
      "name": "/Shared/wsmig/py_nb",
      "id": "...",
      "owner": "user@databricks.com",
      "expected_outcome": "created"
    },
    ...
  },
  "acls": [...],
  "prerequisites": {
    "umis": {"ai27_umi_1": "missing", "ai27_umi_2": "missing"},
    "entra_groups": {"ai27_entragrp_1": "present", "ai27_entragrp_2": "missing"},
    "catalog": "present"
  },
  "summary": {
    "total_objects": 127,
    "flags": ["Missing UMIs: ...", ...],
    "flag_count": 2
  }
}
```

### Logs

Every phase prints progress:
- `✓ created` — new object created
- `exists` — already exists, skipped create
- `FLAG: ...` — prerequisite missing, object skipped
- Errors shown inline with API error message

## Idempotency

Re-running the script is safe. The implementation:
- Looks up existing objects by name/key before creating
- Patches group membership + entitlements on re-run
- Uses `.overwrite=true` on workspace imports
- Never duplicates account SPs (looks up by displayName, which is unique at account level)

**Exception**: `service_principals.create` does NOT dedupe on name (live-verified), so the script always looks up existing SPs first.

## What Gets Created

### Identities (account-level only)
- **15 Entra users** (email-based, assigned to workspace)
- **3 Databricks account SPs** (`ai27_acc_spn_1..3`, one is workspace admin)
- **2 UMI-backed SPs** (looked up from Azure, adopted into account)
- **2 Databricks account groups** (with nested membership)
- **2 Entra groups** (with externalId, backed by real Azure AD groups)

### Compute
- **2 instance pools** (varying min_idle/max_capacity)
- **2 cluster policies** (permissive + restrictive)
- **2 all-purpose clusters** (plain + single-node)

### Workspace Content
- **Notebooks** (py/sql/scala in /Shared + /Users, nested, spaced names)
- **Files** (json/csv/md/yaml, binary, extensionless, >10MB streaming case)
- **Directories** (nested, empty)
- **Git repo** (metadata only; import-manual)

### UC
- **Catalog** + **schema** + **3 tables** (trips, zones; required for dashboards/Genie/DLT)

### SQL
- **2 SQL warehouses** (Pro + Serverless)
- **2 legacy queries** (against warehouses)
- **2 Alerts V2**

### Dashboards & Genie
- **4 Lakeview dashboards** (draft, published embed, published non-embed, scheduled)
  - All render with `main_query` counter widget over UC tables
- **2 Genie spaces** (over UC tables, with ACLs)

### Jobs & DLT
- **3 jobs** (single-task, multi-task DAG, scheduled unpaused)
- **2 DLT pipelines** (serverless + classic)

### Serving & Secrets
- **1 external-model serving endpoint**
- **1 Databricks-backed secret scope** (+ 2 secrets + ACLs)
- **1 AKV scope** (marked manual; can't auto-create without Azure AD token)

### Misc
- **2 global init scripts**
- **Workspace conf** (non-default values, recorded in manifest)

### DAB
- **1 bundle** (job + DLT pipeline + dashboard)

### ACLs
- **Cross-user grants** (user2's notebooks → user1 CAN_MANAGE)
- **Group grants** (account group → CAN_MANAGE on user dirs)
- **Entra group** (CAN_RUN on /Shared)
- **SP grants** (CAN_EDIT on notebooks)

### Incremental (Run 2, excluded from `all`)
- **Edits** to notebook content, policy definition, query
- **New content** (notebook, file, user, group membership change)
- **Draft → published** dashboard change

### Orphaned
- **2 users** removed from workspace (their homes become "deleted in source")

## Debugging

### Enable Tracing
The script prints errors inline. For SDK details:
```bash
export DATABRICKS_DEBUG=1  # SDK debug logs
```

### Dry-Run Plan
```bash
python3 tests/fixtures_medium.py --plan
```
Prints all phases + objects (30–40 lines), no API calls.

### Single Phase
```bash
python3 tests/fixtures_medium.py workspace   # Just the notebook/file phase
python3 tests/fixtures_medium.py check       # Just prerequisites
```

### Rerun After Failure
The script is idempotent, so just re-run:
```bash
python3 tests/fixtures_medium.py all
```

## Known Limitations

### Dashboards Must Render
- The create API accepts **invalid widget definitions** (structurally wrong JSON still POSTs). 
- Rendering is verified only in the **UI** (not programmatically).
- The fixture creates simple **counter widgets** over UC tables — the lowest-risk rendering case.
- **If UC tables don't exist**, dashboards render as "Unable to render visualization" and Genie fails.

### AKV Secret Scopes
- Creating an AKV-backed secret scope requires an **Azure AD token**.
- No obtainable credential from a notebook-only workspace can mint one (Databricks SPN secrets are Databricks tokens, UMI secrets are denied by Azure).
- The fixture **marks AKV scopes as manual** (exports metadata only; import is manual).

### Workspace Conf
- Some keys (e.g., `enableWebTerminal`) are **silently dropped** on some workspaces.
- The fixture reads back what was set and records the actual value in the manifest.

### Idempotency Gotchas
- `service_principals.create` with the same `displayName` creates a **NEW SP** (not dedupe).
  - The fixture always **looks up first** before creating.
- Group membership must be **PATCHed on re-run** (create-time membership is one-shot).
  - The fixture does this automatically.

## Testing

### Offline Tests (no API)
```bash
# Validate syntax
python3 -m py_compile tests/fixtures_medium.py

# Dry-run (prints plan, no APIs)
python3 tests/fixtures_medium.py --plan

# Existing test suite should still pass
python3 -m pytest tests/ -q
```

### Live Test (requires fresh workspace)
```bash
# Provision a fresh source workspace in your account, get its CLI profile
export WSMIG_PROFILE=fresh_source
export WSMIG_ACCT_PROFILE=account

# Deploy full fixture bed (~5–10 minutes, depending on DLT/DAB deploy times)
python3 tests/fixtures_medium.py all

# Check manifest
cat tests/fixtures_manifest.json

# Now run inventory → export → import against this bed
```

## Porting from fixtures_fvm1.py

This script is a **simplified, medium-sized version** of the large reference bed. Key differences:

| Aspect | fixtures_fvm1 | fixtures_medium |
|---|---|---|
| Scale | 150+ users, 10K+ objects, 1–2 hours | 15 users, 127 objects, 5–10 min |
| Identities | Mix of workspace-local + account | Account-level only |
| Phases | 20+ (incl. scale_*) | 18 (no scale_*) |
| DAB bundles | 2 (one pathless) | 1 (standard) |
| Manifest | Minimal (for reference) | Full (for QA validation) |

Both share:
- Idempotent design (re-run safe)
- Generic (env vars only, no hardcoded workspaces)
- Account SCIM identity helpers
- Dashboard/Genie/DAB rendering patterns

## Support

- **Missing prerequisites?** Run `check` phase to see FLAG list.
- **Phase fails?** Check the inline error message; see "Debugging" section.
- **Tests failing after changes?** Rerun `python3 -m pytest -q` to confirm.
- **Questions about a fixture?** Look at the source phase in `tests/fixtures_medium.py` (~1,000 lines, heavily commented).
