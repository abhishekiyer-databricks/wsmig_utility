#!/usr/bin/env bash
#
# create_target_test_catalog.sh
# ------------------------------------------------------------------------------
# Recreates the source test catalog on a TARGET Databricks workspace so that
# Genie-space and Lakeflow-pipeline migrations have their referenced UC tables
# present (the target metastore is in a DIFFERENT region / metastore than the
# source, so the source catalog is NOT reachable and must be reproduced).
#
# It provisions the FULL stack, all parametrized by catalog name:
#   Azure : storage account + container
#           access connector (system-assigned managed identity)
#           "Storage Blob Data Contributor" role assignment on the account
#   UC    : storage credential (MI-backed) -> external location -> catalog
#           -> schema -> 3 EMPTY tables (trips, zones, wsmig_test_bronze)
#
# The tables are created EMPTY (DDL only). `wsmig_test_bronze` is a MATERIALIZED
# VIEW on the source; an MV cannot exist empty without a defining query, so here
# it is created as a plain empty managed Delta table with the identical schema
# (enough to resolve any FQN reference from a migrated Genie space / pipeline).
#
# Idempotent: every create checks-if-exists first and is safe to re-run.
#
# Requirements (all already satisfied in this environment):
#   - databricks CLI authenticated to the target profile   (-p / --profile)
#   - az CLI authenticated to the SAME Azure subscription that backs the
#     target metastore's region
#   - a SQL warehouse in the target workspace to run CREATE TABLE
#
# Usage:
#   ./create_target_test_catalog.sh                 # uses defaults below
#   CATALOG=my_cat SCHEMA=my_schema ./create_target_test_catalog.sh
#   DB_PROFILE=target_ws ./create_target_test_catalog.sh
# ------------------------------------------------------------------------------
set -euo pipefail

# ---- Parameters (override via env) -------------------------------------------
CATALOG="${CATALOG:-catalog_ws_3n37r1}"
SCHEMA="${SCHEMA:-wsmig_test}"
DB_PROFILE="${DB_PROFILE:-target_ws}"

# Azure resource group / region. Region MUST match the target metastore region.
AZ_RG="${AZ_RG:-wsmig-test-rg}"
AZ_LOCATION="${AZ_LOCATION:-eastus2}"

# A short, DNS-safe slug derived from the catalog for globally-unique names.
SLUG="$(echo "$CATALOG" | tr -cd 'a-z0-9' | cut -c1-14)"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-st${SLUG}wsmig}"   # <=24 chars, lower alnum
CONTAINER="${CONTAINER:-uc-root}"
ACCESS_CONNECTOR="${ACCESS_CONNECTOR:-ac-${SLUG}-wsmig}"

# UC object names (kept aligned with the FEVM naming convention).
STORAGE_CRED="${STORAGE_CRED:-sc-${SLUG}-wsmig}"
EXTERNAL_LOCATION="${EXTERNAL_LOCATION:-el-${SLUG}-wsmig}"

# SQL warehouse to run DDL on. Auto-detected if left blank.
WAREHOUSE_ID="${WAREHOUSE_ID:-}"

DBX() { databricks "$@" -p "$DB_PROFILE"; }
# Quiet DBX: run a databricks command, discard stdout+stderr, return exit code.
DBXQ() { databricks "$@" -p "$DB_PROFILE" >/dev/null 2>&1; }
log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()  { printf '    \033[1;32m✓ %s\033[0m\n' "$*"; }

log "Config"
cat <<EOF
    catalog          = $CATALOG
    schema           = $SCHEMA
    db profile       = $DB_PROFILE
    az resource group= $AZ_RG ($AZ_LOCATION)
    storage account  = $STORAGE_ACCOUNT
    container         = $CONTAINER
    access connector  = $ACCESS_CONNECTOR
    storage cred      = $STORAGE_CRED
    external location = $EXTERNAL_LOCATION
EOF

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"

# ---- 1. Azure: resource group -------------------------------------------------
log "1/9  Azure resource group"
if [ "$(az group exists --name "$AZ_RG")" = "true" ]; then
  ok "resource group $AZ_RG exists"
else
  az group create --name "$AZ_RG" --location "$AZ_LOCATION" -o none
  ok "created resource group $AZ_RG"
fi

# ---- 2. Azure: storage account (ADLS Gen2 = hierarchical namespace) -----------
log "2/9  Azure storage account (ADLS Gen2)"
if az storage account show -n "$STORAGE_ACCOUNT" -g "$AZ_RG" -o none 2>/dev/null; then
  ok "storage account $STORAGE_ACCOUNT exists"
else
  az storage account create \
    --name "$STORAGE_ACCOUNT" --resource-group "$AZ_RG" --location "$AZ_LOCATION" \
    --sku Standard_LRS --kind StorageV2 --enable-hierarchical-namespace true \
    --min-tls-version TLS1_2 -o none
  ok "created storage account $STORAGE_ACCOUNT"
fi

# ---- 3. Azure: container ------------------------------------------------------
log "3/9  Azure container"
if az storage container show \
      --name "$CONTAINER" --account-name "$STORAGE_ACCOUNT" --auth-mode login -o none 2>/dev/null; then
  ok "container $CONTAINER exists"
else
  az storage container create \
    --name "$CONTAINER" --account-name "$STORAGE_ACCOUNT" --auth-mode login -o none
  ok "created container $CONTAINER"
fi

# ---- 4. Azure: access connector (system-assigned managed identity) ------------
log "4/9  Azure Databricks access connector"
if az resource show \
      --resource-type Microsoft.Databricks/accessConnectors \
      -n "$ACCESS_CONNECTOR" -g "$AZ_RG" -o none 2>/dev/null; then
  ok "access connector $ACCESS_CONNECTOR exists"
else
  az resource create \
    --resource-type Microsoft.Databricks/accessConnectors \
    -n "$ACCESS_CONNECTOR" -g "$AZ_RG" -l "$AZ_LOCATION" \
    --properties '{}' --is-full-object \
    --properties "$(printf '{"location":"%s","identity":{"type":"SystemAssigned"},"properties":{}}' "$AZ_LOCATION")" \
    -o none
  ok "created access connector $ACCESS_CONNECTOR"
fi

CONNECTOR_ID="$(az resource show --resource-type Microsoft.Databricks/accessConnectors \
  -n "$ACCESS_CONNECTOR" -g "$AZ_RG" --query id -o tsv)"
CONNECTOR_PRINCIPAL="$(az resource show --resource-type Microsoft.Databricks/accessConnectors \
  -n "$ACCESS_CONNECTOR" -g "$AZ_RG" --query identity.principalId -o tsv)"
ok "connector id        = $CONNECTOR_ID"
ok "connector principal = $CONNECTOR_PRINCIPAL"

# ---- 5. Azure: grant Storage Blob Data Contributor to the connector MI --------
log "5/9  Role assignment (Storage Blob Data Contributor)"
STORAGE_ID="$(az storage account show -n "$STORAGE_ACCOUNT" -g "$AZ_RG" --query id -o tsv)"
if az role assignment list --assignee "$CONNECTOR_PRINCIPAL" --scope "$STORAGE_ID" \
     --role "Storage Blob Data Contributor" --query "[0].id" -o tsv 2>/dev/null | grep -q .; then
  ok "role already assigned"
else
  # MI propagation can lag; retry a few times.
  for i in 1 2 3 4 5; do
    if az role assignment create \
         --assignee-object-id "$CONNECTOR_PRINCIPAL" --assignee-principal-type ServicePrincipal \
         --role "Storage Blob Data Contributor" --scope "$STORAGE_ID" -o none 2>/dev/null; then
      ok "assigned Storage Blob Data Contributor"
      break
    fi
    echo "    ...MI not propagated yet, retry $i/5"; sleep 15
  done
fi

echo "    waiting 30s for role propagation before UC validation..."
sleep 30

# ---- 6. UC: storage credential ------------------------------------------------
log "6/9  UC storage credential"
if DBXQ storage-credentials get "$STORAGE_CRED"; then
  ok "storage credential $STORAGE_CRED exists"
else
  DBX storage-credentials create --json "$(cat <<JSON
{
  "name": "$STORAGE_CRED",
  "azure_managed_identity": { "access_connector_id": "$CONNECTOR_ID" },
  "comment": "wsmig target test catalog credential for $CATALOG"
}
JSON
)" >/dev/null
  ok "created storage credential $STORAGE_CRED"
fi

# ---- 7. UC: external location -------------------------------------------------
CONTAINER_URL="abfss://${CONTAINER}@${STORAGE_ACCOUNT}.dfs.core.windows.net/"
log "7/9  UC external location -> $CONTAINER_URL"
if DBXQ external-locations get "$EXTERNAL_LOCATION"; then
  ok "external location $EXTERNAL_LOCATION exists"
else
  DBX external-locations create --json "$(cat <<JSON
{
  "name": "$EXTERNAL_LOCATION",
  "url": "$CONTAINER_URL",
  "credential_name": "$STORAGE_CRED",
  "comment": "wsmig target test catalog location for $CATALOG"
}
JSON
)" >/dev/null
  ok "created external location $EXTERNAL_LOCATION"
fi

# ---- 8. UC: catalog + schema --------------------------------------------------
CATALOG_STORAGE_ROOT="${CONTAINER_URL}${CATALOG}"
log "8/9  UC catalog + schema"
if DBXQ catalogs get "$CATALOG"; then
  ok "catalog $CATALOG exists"
else
  DBX catalogs create --json "$(cat <<JSON
{
  "name": "$CATALOG",
  "storage_root": "$CATALOG_STORAGE_ROOT",
  "comment": "wsmig migration test catalog (recreated on target)"
}
JSON
)" >/dev/null
  ok "created catalog $CATALOG"
fi

if DBXQ schemas get "${CATALOG}.${SCHEMA}"; then
  ok "schema ${CATALOG}.${SCHEMA} exists"
else
  DBX schemas create "$SCHEMA" "$CATALOG" >/dev/null
  ok "created schema ${CATALOG}.${SCHEMA}"
fi

# ---- 9. UC: empty tables (DDL only) via SQL warehouse -------------------------
log "9/9  Create empty tables"
if [ -z "$WAREHOUSE_ID" ]; then
  WAREHOUSE_ID="$(DBX warehouses list -o json | python3 -c "import sys,json; ws=json.load(sys.stdin); ws=ws if isinstance(ws,list) else ws.get('warehouses',[]); print(ws[0]['id'] if ws else '')")"
fi
[ -z "$WAREHOUSE_ID" ] && { echo "ERROR: no SQL warehouse found; set WAREHOUSE_ID"; exit 1; }
ok "using warehouse $WAREHOUSE_ID"

run_sql() {
  local stmt="$1"
  local payload
  payload="$(SQL_WH="$WAREHOUSE_ID" SQL_STMT="$stmt" python3 -c "import json,os; print(json.dumps({'warehouse_id': os.environ['SQL_WH'], 'statement': os.environ['SQL_STMT'], 'wait_timeout': '50s'}))")"
  DBX api post /api/2.0/sql/statements --json "$payload" | python3 -c "
import sys,json
r=json.load(sys.stdin)
st=r.get('status',{}).get('state')
if st!='SUCCEEDED':
    print('    SQL FAILED:', json.dumps(r.get('status',{})))
    sys.exit(1)
"
}

# trips  : zip string, trips int, avg_dist double
run_sql "CREATE TABLE IF NOT EXISTS \`$CATALOG\`.\`$SCHEMA\`.\`trips\` (
  zip STRING,
  trips INT,
  avg_dist DOUBLE
) USING DELTA"
ok "trips"

# zones  : zip string, borough string
run_sql "CREATE TABLE IF NOT EXISTS \`$CATALOG\`.\`$SCHEMA\`.\`zones\` (
  zip STRING,
  borough STRING
) USING DELTA"
ok "zones"

# wsmig_test_bronze : MV on source -> empty Delta table here (same schema)
run_sql "CREATE TABLE IF NOT EXISTS \`$CATALOG\`.\`$SCHEMA\`.\`wsmig_test_bronze\` (
  zip STRING,
  trips INT,
  avg_dist DOUBLE
) USING DELTA"
ok "wsmig_test_bronze"

log "DONE"
echo "    Catalog $CATALOG.$SCHEMA now has: trips, zones, wsmig_test_bronze (all empty)."
