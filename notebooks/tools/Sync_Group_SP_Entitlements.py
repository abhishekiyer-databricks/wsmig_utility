# Databricks notebook source
# MAGIC %md
# MAGIC # Sync GROUP + SERVICE PRINCIPAL entitlements: SOURCE → TARGET, and report what is missing
# MAGIC
# MAGIC Run this notebook **in the TARGET workspace** on **classic compute**, as a **target workspace admin**.
# MAGIC It reads the **source** workspace with a source workspace-admin service principal (OAuth M2M).
# MAGIC
# MAGIC **Step 1: list** every group and service principal (SP) on both workspaces.
# MAGIC Groups are matched by **name**, SPs by **applicationId** (stable across workspaces of the same account).
# MAGIC Each one is marked **direct** (assigned to the workspace itself) or **via group** (only a member of an
# MAGIC assigned group). "Migrated" means DIRECTLY assigned on the target, the same as on the source.
# MAGIC
# MAGIC **Step 2 (optional, `assign_missing=true`): bring source-only groups / SPs to the target.**
# MAGIC * ACCOUNT group / SP → **assigned** to the target workspace (it already exists in the account; same id).
# MAGIC   This includes one that is direct on the source but on the target only via a group.
# MAGIC * Workspace-local groups are NOT brought over (reported as `SKIPPED`; recreate them manually).
# MAGIC   Users are never assigned.
# MAGIC * Right before each assignment the principal is checked again; one that is already directly assigned
# MAGIC   (for example as ADMIN) is left unchanged, never overwritten.
# MAGIC * Never brought: the reader SP itself, and an SP whose NAME the target already has with a different
# MAGIC   applicationId (shown as `SKIPPED` so a human decides).
# MAGIC * The notebook waits until each assigned item is listed on the target (assignment takes ~1 min).
# MAGIC
# MAGIC **Step 3: sync entitlements** for every group / SP that exists on BOTH sides:
# MAGIC 1. `GET` it on the source and on the target, and compute which source entitlements the target lacks.
# MAGIC    **Admin access** is included as `admin-access`: in SCIM it is not an entitlement but DIRECT
# MAGIC    membership of the workspace `admins` group, so it is read from the object's `groups` and granted
# MAGIC    by adding the object to the target's `admins` group (what the UI "Admin access" checkbox does).
# MAGIC 2. Nothing missing → `IN_SYNC`. `dry_run=true` → `WOULD_ADD` (no write).
# MAGIC 3. Otherwise `PATCH` **only the missing entitlements** (add-only, never removes), then `GET` again and
# MAGIC    **verify** → `ADDED`, or `VERIFY_FAILED` if the target does not show them after the write.
# MAGIC
# MAGIC **Step 4: report the differences** (report only, nothing is created or deleted here):
# MAGIC * **Directly on SOURCE, not on TARGET**: not migrated. Groups show their source entitlements and member counts
# MAGIC   by type (users / SPs / groups); SPs show whether the target has an SP with the same NAME but a
# MAGIC   different applicationId.
# MAGIC * **On TARGET, not on SOURCE**: created on the target directly, or by something else.
# MAGIC
# MAGIC Every API error is shown with its HTTP status and the server's own message (`ERROR`). The run continues
# MAGIC and **fails at the end** if anything ended in `ERROR`, `VERIFY_FAILED` or `NOT_VISIBLE`. The differences
# MAGIC never fail it.
# MAGIC
# MAGIC **Never touched:** the entitlements of the `users` / `admins` system groups (Databricks locks them).
# MAGIC The only change ever made to `admins` is adding a member that has Admin access on the source.
# MAGIC Entitlements the target has but the source doesn't are only shown (`extra_on_target`), never removed.
# MAGIC
# MAGIC **Test first:** set `principal_type` + `principal_name` to ONE group or SP and keep `dry_run=true`;
# MAGIC then `dry_run=false` for it; then clear `principal_name` to run everything. `assign_missing` follows
# MAGIC the same rule: dry run first. It shows exactly what would be assigned.

# COMMAND ----------

dbutils.widgets.text("source_workspace_url", "", "1. Source workspace URL (https://adb-....azuredatabricks.net)")
dbutils.widgets.text("source_sp_client_id", "", "2. Source SP client id (application id)")
dbutils.widgets.text("source_sp_secret_scope", "", "3a. Secret scope holding the SP secret (preferred)")
dbutils.widgets.text("source_sp_secret_key", "", "3b. Secret key in that scope")
dbutils.widgets.text("spn_secret_value", "", "3c. OR the SP secret itself (fallback)")
dbutils.widgets.dropdown("principal_type", "all", ["all", "groups", "service_principals"], "4. What to process")
dbutils.widgets.text("principal_name", "", "5. ONE group name / SP name / SP application id (blank = all)")
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "6. Dry run (no writes)")
dbutils.widgets.dropdown("assign_missing", "false", ["false", "true"], "7. Bring source-only groups/SPs to target first")

# COMMAND ----------

import json
import time
import requests

SCIM = "/api/2.0/preview/scim/v2"
SYSTEM_GROUPS = {"users", "admins"}      # entitlements locked by Databricks — never touched
PAGE_SIZE = 100
HTTP_TIMEOUT = 60
MAX_ATTEMPTS = 5                          # for 429 / 5xx / connection errors only
VERIFY_ATTEMPTS = 4                       # read-back after a write, in case of a short propagation lag
VERIFY_WAIT_SEC = 3

GROUP, SP = "group", "service_principal"
_RESOURCE = {GROUP: "Groups", SP: "ServicePrincipals"}
BAD_STATUSES = ("ERROR", "VERIFY_FAILED", "NOT_VISIBLE")
WAIT_POLLS = 30                           # after assigning: poll the target list up to 30 x 10 s for it to appear
WAIT_POLL_SEC = 10
ADMIN = "admin-access"                    # pseudo-entitlement: DIRECT membership of the workspace `admins` group


class ApiError(Exception):
    """An API call that did not succeed. Always carries the HTTP status and the server's own message."""

    def __init__(self, side, method, path, status, body):
        self.status = status
        message = body
        try:
            parsed = json.loads(body)
            message = parsed.get("detail") or parsed.get("message") or parsed.get("error_description") or body
            code = parsed.get("error_code") or parsed.get("scimType") or parsed.get("error")
            if code:
                message = f"{code}: {message}"
        except Exception:
            pass
        super().__init__(f"[{side}] {method} {path} → HTTP {status}: {str(message)[:500]}")


class Api:
    """Minimal REST client for ONE workspace. `token_fn()` returns a currently valid bearer token."""

    def __init__(self, side, host, token_fn):
        self.side = side
        self.host = host.rstrip("/")
        self.token_fn = token_fn
        self.session = requests.Session()

    def call(self, method, path, params=None, body=None):
        url = self.host + path
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self.session.request(
                    method, url, params=params,
                    data=json.dumps(body) if body is not None else None,
                    headers={"Authorization": f"Bearer {self.token_fn()}",
                             "Content-Type": "application/scim+json" if path.startswith(SCIM)
                             else "application/json"},
                    timeout=HTTP_TIMEOUT)
            except requests.RequestException as exc:
                last_error = ApiError(self.side, method, path, "no response", repr(exc))
                time.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = ApiError(self.side, method, path, resp.status_code, resp.text)
                wait = int(resp.headers.get("Retry-After") or 0) or min(2 ** attempt, 30)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise ApiError(self.side, method, path, resp.status_code, resp.text)
            return resp.json() if resp.text.strip() else {}
        raise last_error

    def _list(self, resource, attributes):
        out, start = [], 1
        while True:
            page = self.call("GET", f"{SCIM}/{resource}",
                             params={"attributes": attributes, "startIndex": start, "count": PAGE_SIZE})
            resources = page.get("Resources") or []
            out.extend(resources)
            total = int(page.get("totalResults") or 0)
            start += len(resources)
            if not resources or start > total:
                return out

    def list_groups(self):
        """{displayName: [{"id", "name", "local"}, ...]} (a list per key, so a duplicate is detected).

        `local` = workspace-local group (exists only in this workspace); otherwise an ACCOUNT group."""
        by_key = {}
        for g in self._list("Groups", "id,displayName,meta"):
            name = g.get("displayName") or ""
            local = ((g.get("meta") or {}).get("resourceType") or "") == "WorkspaceGroup"
            by_key.setdefault(name, []).append({"id": str(g.get("id")), "name": name, "local": local})
        return by_key

    def list_service_principals(self):
        """{applicationId: [{"id", "name", "app_id"}, ...]}."""
        by_key = {}
        for s in self._list("ServicePrincipals", "id,applicationId,displayName"):
            app_id = s.get("applicationId") or ""
            by_key.setdefault(app_id, []).append(
                {"id": str(s.get("id")), "name": s.get("displayName") or "", "app_id": app_id})
        return by_key

    @staticmethod
    def _entitlements_of(obj):
        """DIRECT entitlements + `admin-access` if the object is a DIRECT member of `admins`.

        Admin access is not in SCIM `entitlements`; it is membership of the `admins` system group. Only a
        DIRECT membership counts: an indirect one comes from a parent group, which is synced on its own."""
        ents = {e.get("value") for e in (obj.get("entitlements") or []) if e.get("value")}
        if any((g.get("display") or "") == "admins" and (g.get("type") or "direct") == "direct"
               for g in (obj.get("groups") or [])):
            ents.add(ADMIN)
        return ents

    def get_entitlements(self, kind, obj_id):
        """The object's DIRECT entitlements (incl. admin-access), read with a single-object GET."""
        o = self.call("GET", f"{SCIM}/{_RESOURCE[kind]}/{obj_id}", params={"attributes": "id,entitlements,groups"})
        return self._entitlements_of(o)

    def admins_group_id(self):
        if not getattr(self, "_admins_id", None):
            page = self.call("GET", f"{SCIM}/Groups",
                             params={"filter": 'displayName eq "admins"', "attributes": "id,displayName"})
            hits = [g for g in page.get("Resources") or [] if g.get("displayName") == "admins"]
            if len(hits) != 1:
                raise ApiError(self.side, "GET", f"{SCIM}/Groups?filter=admins", "n/a",
                               f"expected exactly one 'admins' group, found {len(hits)}")
            self._admins_id = str(hits[0]["id"])
        return self._admins_id

    def add_entitlements(self, kind, obj_id, values):
        values = set(values)
        regular = sorted(values - {ADMIN})
        if regular:
            self.call("PATCH", f"{SCIM}/{_RESOURCE[kind]}/{obj_id}", body={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "add", "path": "entitlements", "value": [{"value": v} for v in regular]}]})
        if ADMIN in values:
            self.call("PATCH", f"{SCIM}/Groups/{self.admins_group_id()}", body={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "add", "path": "members", "value": [{"value": str(obj_id)}]}]})

    def group_detail(self, group_id):
        """Entitlements + member counts by type, for the 'not on target' report."""
        g = self.call("GET", f"{SCIM}/Groups/{group_id}", params={"attributes": "id,entitlements,groups,members"})
        counts = {"users": 0, "service_principals": 0, "groups": 0, "other": 0}
        for m in g.get("members") or []:
            ref = (m.get("$ref") or "").split("/")[0]
            key = {"Users": "users", "ServicePrincipals": "service_principals", "Groups": "groups"}.get(ref, "other")
            counts[key] += 1
        return sorted(self._entitlements_of(g)), counts

    def direct_assignment_ids(self):
        """Ids of every principal DIRECTLY assigned to this workspace (one call). An account principal that is
        only a member of an assigned group is listed by SCIM too, but is NOT in this set."""
        page = self.call("GET", "/api/2.0/preview/permissionassignments")
        return {str((a.get("principal") or {}).get("principal_id")) for a in page.get("permission_assignments") or []}

    def is_directly_assigned(self, principal_id):
        """Per-principal check right before an assignment, so an existing assignment (e.g. ADMIN) is never
        overwritten even if the bulk list above were ever incomplete. 200 = assigned, 404 = not."""
        try:
            self.call("GET", f"/api/2.0/identity/workspace-assignments/{principal_id}")
            return True
        except ApiError as exc:
            if exc.status == 404:
                return False
            raise

    def assign_to_workspace(self, principal_id):
        """Assign an existing ACCOUNT group / SP to THIS workspace (as USER; entitlements are set separately)."""
        return self.call("PUT", f"/api/2.0/preview/permissionassignments/principals/{principal_id}",
                         body={"permissions": ["USER"]})

    def me(self):
        return self.call("GET", f"{SCIM}/Me")


def oauth_m2m_token_fn(host, client_id, client_secret):
    """Token function for a service principal (OAuth client-credentials), refreshed before expiry."""
    state = {"token": None, "expires_at": 0}

    def token():
        if state["token"] and time.time() < state["expires_at"] - 300:
            return state["token"]
        resp = requests.post(f"{host.rstrip('/')}/oidc/v1/token",
                             auth=(client_id, client_secret),
                             data={"grant_type": "client_credentials", "scope": "all-apis"},
                             timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            err = ApiError("source", "POST", "/oidc/v1/token", resp.status_code, resp.text)
            if resp.status_code in (400, 401):
                err.args = (str(err) + " | Check source_sp_client_id is the SP's application id and the "
                            "secret is a current OAuth secret of THAT SP (created in the account console).",)
            raise err
        payload = resp.json()
        state["token"] = payload["access_token"]
        state["expires_at"] = time.time() + int(payload.get("expires_in") or 3600)
        return state["token"]
    return token


def is_workspace_admin(api):
    """True if the calling identity is in the workspace `admins` group (read from SCIM /Me)."""
    me = api.me()
    return any((g.get("display") or "") == "admins" for g in (me.get("groups") or [])), me


# COMMAND ----------

def _label(kind, obj):
    return obj["name"] if kind == GROUP else f"{obj['name']} ({obj['app_id']})"


def sync_one(kind, key, source, target, src_index, tgt_index, dry_run):
    """Sync one group / SP that should exist on both sides. Returns a result row; never raises."""
    src_objs, tgt_objs = src_index.get(key) or [], tgt_index.get(key) or []
    label = _label(kind, src_objs[0]) if src_objs else key
    row = {"type": kind, "name": label, "status": "", "source_entitlements": "", "target_before": "",
           "added": "", "target_after": "", "extra_on_target": "", "detail": ""}
    try:
        if len(src_objs) != 1 or len(tgt_objs) != 1:
            row.update(status="ERROR", detail=f"expected exactly one match on each side, found "
                                              f"{len(src_objs)} on source / {len(tgt_objs)} on target "
                                              f"(ids {[o['id'] for o in src_objs]} / {[o['id'] for o in tgt_objs]}); "
                                              f"not guessing which one to change")
            return row
        src_ents = source.get_entitlements(kind, src_objs[0]["id"])
        row["source_entitlements"] = ", ".join(sorted(src_ents))
        tgt_id = tgt_objs[0]["id"]

        before = target.get_entitlements(kind, tgt_id)
        row["target_before"] = ", ".join(sorted(before))
        row["extra_on_target"] = ", ".join(sorted(before - src_ents))
        missing = src_ents - before
        if not missing:
            row.update(status="IN_SYNC", target_after=row["target_before"])
            return row
        row["added"] = ", ".join(sorted(missing))
        if dry_run:
            row.update(status="WOULD_ADD", detail="dry run: nothing written")
            return row

        target.add_entitlements(kind, tgt_id, missing)

        after = set()
        for _ in range(VERIFY_ATTEMPTS):
            after = target.get_entitlements(kind, tgt_id)
            if missing <= after:
                break
            time.sleep(VERIFY_WAIT_SEC)
        row["target_after"] = ", ".join(sorted(after))
        still_missing = missing - after
        if still_missing:
            row.update(status="VERIFY_FAILED",
                       detail=f"PATCH returned success but a fresh GET does not show: "
                              f"{', '.join(sorted(still_missing))}")
        else:
            row.update(status="ADDED", detail="verified by GET after the write")
    except Exception as exc:  # report it on the row and carry on
        row.update(status="ERROR", detail=str(exc))
    return row


def read_inventory(source, target, kinds, log=print):
    """{kind: (source_index, target_index)}. Every object also gets `direct`: True if it is DIRECTLY assigned
    to its workspace (or is a workspace-local group), False if it is only there via an assigned group."""
    log("Listing groups and service principals on both workspaces …")
    inv = {}
    direct = {"source": source.direct_assignment_ids(), "target": target.direct_assignment_ids()}
    for kind in kinds:
        lister = "list_groups" if kind == GROUP else "list_service_principals"
        src, tgt = getattr(source, lister)(), getattr(target, lister)()
        if kind == GROUP:
            for d in (src, tgt):
                for name in SYSTEM_GROUPS:
                    d.pop(name, None)
        for side, index in (("source", src), ("target", tgt)):
            for objs in index.values():
                for o in objs:
                    o["direct"] = bool(o.get("local")) or o["id"] in direct[side]
        inv[kind] = (src, tgt)
        note = "   (excluding users/admins)" if kind == GROUP else ""
        n_direct = lambda ix: sum(o["direct"] for objs in ix.values() for o in objs)
        log(f"  {kind + 's':<19} source: {sum(map(len, src.values())):>5} ({n_direct(src)} direct)   target: "
            f"{sum(map(len, tgt.values())):>5} ({n_direct(tgt)} direct)   on both: {len(set(src) & set(tgt)):>5}{note}")
    return inv


def select_one(inv, principal_name):
    """Single-item mode: find the ONE group / SP the operator named, on the SOURCE. Returns [(kind, key)]."""
    hits = []
    for kind, (src, _) in inv.items():
        if kind == GROUP and principal_name in src:
            hits.append((kind, principal_name))
        if kind == SP:
            if principal_name in src:                       # an applicationId
                hits.append((kind, principal_name))
            else:
                by_name = [k for k, objs in src.items() if any(o["name"] == principal_name for o in objs)]
                if len(by_name) > 1:
                    raise ValueError(f"{len(by_name)} source SPs are named '{principal_name}' "
                                     f"(applicationIds {by_name}); put the applicationId in principal_name")
                hits += [(kind, k) for k in by_name]
    if not hits:
        raise ValueError(f"'{principal_name}' is not a group name, SP name or SP applicationId on the SOURCE "
                         f"workspace (names are case-sensitive; users/admins are not processed)")
    return hits


def plan_missing(inv, items, reader_app_id=""):
    """Which SOURCE-only items (of `items` = [(kind, key)]) to bring to the target, and how.

    ACCOUNT group / SP → `assign` (it already exists in the account; only the workspace assignment is missing).
    Workspace-local group → `skip` (out of scope for now; reported so it can be recreated manually).
    Also `assign`: DIRECTLY assigned on the source but on the target only via an assigned group (it would lose
    access if that group went away, unlike on the source).
    Never brought: the reader SP itself, and an SP whose NAME the target already has with another
    applicationId (a likely intended replacement: a human decides). Items that are only present via a group on
    the source are left alone: they come along with that group."""
    plan = []
    for kind, key in items:
        src, tgt = inv[kind]
        if key not in src:
            continue
        objs = src[key]
        on_target = tgt.get(key) or []
        if on_target and (any(o["direct"] for o in on_target) or not objs[0].get("direct")):
            continue                                   # migrated, or indirect on both sides (comes with its group)
        p = {"kind": kind, "key": key, "obj": objs[0], "action": "", "detail": ""}
        if not on_target and not objs[0].get("direct"):
            continue                                   # only via a group on the source too: comes with that group
        if len(objs) != 1:
            p.update(action="error", detail=f"{len(objs)} source objects share this key; not guessing")
        elif on_target:
            p.update(action="assign", detail="on the target only via group membership; assigning it directly, "
                                             "as on the source")
        elif kind == GROUP and objs[0].get("local"):
            p.update(action="skip", detail="workspace-local group: not brought over by this notebook; recreate it "
                                           "manually on the target (members + entitlements are in Step 4)")
        elif kind == GROUP:
            p["action"] = "assign"
        elif objs[0].get("app_id") == reader_app_id:
            p.update(action="skip", detail="this is the source SP this notebook uses to read the source")
        else:
            twin = [k for k, t in tgt.items() for o in t if o["name"] == objs[0]["name"] and k != key]
            if twin:
                p.update(action="skip", detail=f"the target already has an SP named '{objs[0]['name']}' with "
                                               f"applicationId {twin[0]}; decide manually which one is intended")
            else:
                p["action"] = "assign"
        plan.append(p)
    return plan


def _wait_visible(target, keys_by_kind, log):
    """Poll until every assigned item is DIRECTLY assigned AND listed on the target.

    `keys_by_kind` = {kind: {key: principal_id}}. Returns {kind: {keys still pending}}."""
    missing = {k: dict(v) for k, v in keys_by_kind.items() if v}
    for attempt in range(WAIT_POLLS):
        if not any(missing.values()):
            break
        if attempt:
            time.sleep(WAIT_POLL_SEC)
        direct = target.direct_assignment_ids()
        for kind in list(missing):
            if missing[kind]:
                listed = target.list_groups() if kind == GROUP else target.list_service_principals()
                missing[kind] = {k: i for k, i in missing[kind].items() if not (k in listed and i in direct)}
        log(f"  waiting for assignments to appear on the target … still pending: "
            f"{sum(map(len, missing.values()))}")
    return {k: set(v) for k, v in missing.items()}


def bring_missing(plan, source, target, dry_run, log=print):
    """Step 2: assign source-only (or via-group-only) ACCOUNT groups / SPs to the target workspace.

    Returns result rows {type, name, action, detail}. Never raises; errors land on the row."""
    rows, pairs, assigned = [], [], {GROUP: {}, SP: {}}
    n_bring = sum(p["action"] == "assign" for p in plan)
    log(f"\nBringing source-only items to the target: {n_bring} to bring, {len(plan) - n_bring} skipped/error "
        f"(dry_run={dry_run})")
    log("-" * 110)

    def row(p, action, detail):
        r = {"type": p["kind"], "name": _label(p["kind"], p["obj"]), "action": action, "detail": detail}
        rows.append(r)
        pairs.append((p, r))
        return r

    for p in plan:
        try:
            if p["action"] in ("skip", "error"):
                row(p, "SKIPPED" if p["action"] == "skip" else "ERROR", p["detail"])
            elif p["action"] == "assign":
                why = p["detail"] or "account principal: not on the target"
                if target.is_directly_assigned(p["obj"]["id"]):
                    row(p, "SKIPPED", "already directly assigned on the target; left unchanged")
                elif dry_run:
                    row(p, "WOULD_ASSIGN", f"{why}; would be assigned to the target workspace")
                else:
                    target.assign_to_workspace(p["obj"]["id"])
                    assigned[p["kind"]][p["key"]] = p["obj"]["id"]
                    row(p, "ASSIGNED", f"{why}; assigned to the target workspace (verified in its direct "
                                       f"assignment list)")
        except Exception as exc:
            row(p, "ERROR", str(exc))

    if not dry_run and any(assigned.values()):
        still = _wait_visible(target, assigned, log)
        for p, r in pairs:
            if r["action"] == "ASSIGNED" and p["key"] in still.get(p["kind"], set()):
                r.update(action="NOT_VISIBLE", detail=f"assigned, but not shown as directly assigned on the target "
                                                     f"after {WAIT_POLLS * WAIT_POLL_SEC}s; re-run to check")

    for i, r in enumerate(rows, 1):
        log(f"[{i}/{len(rows)}] {r['action']:<14} {r['type']:<17} {r['name']} | {r['detail']}")
    return rows


def run_sync(source, target, kinds, principal_name, dry_run, log=print, inv=None, pending=frozenset()):
    """Step 3: sync entitlements for what exists on both sides. Returns (inventory, rows).

    `pending` = (kind, key) pairs a DRY run would bring to the target first; they are shown as WOULD_ADD
    with every source entitlement (the assignment itself grants none)."""
    if principal_name in SYSTEM_GROUPS:
        raise ValueError(f"'{principal_name}' is a system group; Databricks locks its entitlements")
    inv = inv or read_inventory(source, target, kinds, log)
    if principal_name:
        todo = select_one(inv, principal_name)
    else:
        todo = [(kind, key) for kind in kinds for key in sorted(set(inv[kind][0]) & set(inv[kind][1]))]
    todo = list(dict.fromkeys(list(todo) + sorted(pending)))
    log(f"\nSyncing entitlements for {len(todo)} item(s) that exist on both sides  (dry_run={dry_run})")
    log("-" * 110)
    rows = []
    for i, (kind, key) in enumerate(todo, 1):
        src, tgt = inv[kind]
        if key not in tgt and (kind, key) in pending:
            try:
                ents = ", ".join(sorted(source.get_entitlements(kind, src[key][0]["id"])))
                rows.append({"type": kind, "name": _label(kind, src[key][0]), "status": "WOULD_ADD" if ents else "IN_SYNC",
                             "source_entitlements": ents, "target_before": "", "added": ents, "target_after": "",
                             "extra_on_target": "", "detail": "dry run: would be brought to the target first, then these added"})
            except Exception as exc:
                rows.append({"type": kind, "name": _label(kind, src[key][0]), "status": "ERROR", "source_entitlements": "",
                             "target_before": "", "added": "", "target_after": "", "extra_on_target": "", "detail": str(exc)})
        elif key not in tgt:
            rows.append({"type": kind, "name": _label(kind, src[key][0]), "status": "NOT_ON_TARGET",
                         "source_entitlements": "", "target_before": "", "added": "", "target_after": "",
                         "extra_on_target": "", "detail": "does not exist on the target; nothing written"})
        else:
            rows.append(sync_one(kind, key, source, target, src, tgt, dry_run))
        r = rows[-1]
        line = f"[{i}/{len(todo)}] {r['status']:<14} {kind:<17} {r['name']}"
        if r["added"]:
            line += f" | add: {r['added']}"
        if r["status"] in BAD_STATUSES + ("NOT_ON_TARGET",):
            line += f" | {r['detail']}"
        log(line)
    return inv, rows


def find_differences(source, target, inv, log=print, source_reader_app_id="", planned=None):
    """Step 3: what exists on only one side. Returns (rows, error_rows)."""
    diffs, errors = [], []
    for kind, (src, tgt) in inv.items():
        tgt_sp_names = {o["name"]: k for k, objs in tgt.items() for o in objs} if kind == SP else {}
        src_sp_names = {o["name"]: k for k, objs in src.items() for o in objs} if kind == SP else {}
        def direct_keys(ix):
            return {k for k, objs in ix.items() if any(o.get("direct", True) for o in objs)}
        for side, mine, other in (("source_only", src, tgt), ("target_only", tgt, src)):
            api = source if side == "source_only" else target
            for key in sorted(direct_keys(mine) - direct_keys(other)):
                for obj in mine[key]:
                    row = {"side": side, "type": kind, "name": obj["name"],
                           "application_id": obj.get("app_id", ""), "entitlements": "",
                           "members_users": None, "members_sps": None, "members_groups": None, "note": ""}
                    try:
                        if kind == GROUP:
                            ents, counts = api.group_detail(obj["id"])
                            row.update(entitlements=", ".join(ents), members_users=counts["users"],
                                       members_sps=counts["service_principals"], members_groups=counts["groups"])
                            if counts["service_principals"] and not counts["users"] and not counts["groups"]:
                                row["note"] = "only service principals as members"
                        else:
                            row["entitlements"] = ", ".join(sorted(api.get_entitlements(kind, obj["id"])))
                            twin = (tgt_sp_names if side == "source_only" else src_sp_names).get(obj["name"])
                            twin = twin if twin and twin != key else ""     # same appId = itself, not a twin
                            if side == "source_only" and obj.get("app_id") == source_reader_app_id:
                                row["note"] = "this is the source SP this notebook uses to read the source"
                            elif twin:
                                row["note"] = (f"the {'target' if side == 'source_only' else 'source'} has an SP "
                                               f"with the SAME NAME but applicationId {twin}")
                    except Exception as exc:
                        row["note"] = f"could not read details: {exc}"
                        errors.append(row)
                    if key in other:
                        where = "target" if side == "source_only" else "source"
                        row["note"] = "; ".join(x for x in (f"on the {where} only via group membership "
                                                            f"(not directly assigned)", row["note"]) if x)
                    if side == "source_only" and (planned or {}).get((kind, key)):
                        row["note"] = "; ".join(x for x in (planned[(kind, key)], row["note"]) if x)
                    diffs.append(row)

    for side, title in (("source_only", "DIRECTLY ASSIGNED ON SOURCE, NOT ON TARGET (not migrated)"),
                        ("target_only", "DIRECTLY ASSIGNED ON TARGET, NOT ON SOURCE")):
        rows = [d for d in diffs if d["side"] == side]
        log(f"\n=== {title}: {len(rows)} ===")
        for d in rows:
            extra = ""
            if d["type"] == GROUP and d["members_users"] is not None:
                extra = (f" | members: {d['members_users']} users, {d['members_sps']} SPs, "
                         f"{d['members_groups']} groups")
            log(f"  {d['type']:<17} {d['name']}" + (f" ({d['application_id']})" if d["application_id"] else "")
                + f" | entitlements: {d['entitlements'] or '-'}{extra}" + (f" | {d['note']}" if d["note"] else ""))
    return diffs, errors


def summarize(rows, log=print):
    counts = {}
    for r in rows:
        counts[(r["type"], r["status"])] = counts.get((r["type"], r["status"]), 0) + 1
    log("-" * 110)
    log("SYNC SUMMARY: " + (", ".join(f"{t} {s}={n}" for (t, s), n in sorted(counts.items())) or "nothing to sync"))
    for status in BAD_STATUSES:
        hits = [r for r in rows if r["status"] == status]
        if hits:
            log(f"\n{status} ({len(hits)}):")
            for r in hits:
                log(f"  - {r['type']} {r['name']}: {r['detail']}")
    problems = [r for r in rows if r["status"] in BAD_STATUSES]
    return counts, problems

# COMMAND ----------

# MAGIC %md ## Connect + pre-checks

# COMMAND ----------

def _w(name):
    return dbutils.widgets.get(name).strip()

source_url = _w("source_workspace_url").rstrip("/")
client_id = _w("source_sp_client_id")
scope, key, secret_value = _w("source_sp_secret_scope"), _w("source_sp_secret_key"), _w("spn_secret_value")
principal_type = _w("principal_type") or "all"
principal_name = _w("principal_name")
dry_run = _w("dry_run").lower() != "false"
assign_missing = _w("assign_missing").lower() == "true"
kinds = {"all": [GROUP, SP], "groups": [GROUP], "service_principals": [SP]}[principal_type]

if not source_url.startswith("https://"):
    raise ValueError("source_workspace_url must start with https://")
if not client_id:
    raise ValueError("source_sp_client_id is required")
if scope and key:
    client_secret, secret_from = dbutils.secrets.get(scope, key).strip(), f"secret scope '{scope}' / key '{key}'"
elif secret_value:
    client_secret, secret_from = secret_value, "the spn_secret_value widget"
else:
    raise ValueError("Provide the SP secret: source_sp_secret_scope + source_sp_secret_key, or spn_secret_value")

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
target_url = _ctx.apiUrl().get().rstrip("/")
_target_token = _ctx.apiToken().get()

source = Api("source", source_url, oauth_m2m_token_fn(source_url, client_id, client_secret))
target = Api("target", target_url, lambda: _target_token)

print(f"Source : {source_url}  (SP {client_id}, secret from {secret_from})")
try:
    _target_name = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
except Exception:
    _target_name = target_url
print(f"Target : {_target_name}  (this workspace)")
if source_url in (target_url, _target_name):
    raise ValueError("source_workspace_url is THIS workspace; run the notebook in the TARGET workspace")

src_admin, src_me = is_workspace_admin(source)
print(f"Source identity: {src_me.get('displayName') or src_me.get('userName')}  workspace admin={src_admin}")
if not src_admin:
    raise PermissionError("The source SP is NOT a workspace admin on the source. A non-admin gets incomplete "
                          "identity data, so the results would be wrong. Add it to the source 'admins' group.")
tgt_admin, tgt_me = is_workspace_admin(target)
print(f"Target identity: {tgt_me.get('userName') or tgt_me.get('displayName')}  workspace admin={tgt_admin}")
if not tgt_admin:
    raise PermissionError("You (the identity running this notebook) are not a workspace admin on the target.")
print(f"Processing: {principal_type}" + (f", only '{principal_name}'" if principal_name else "")
      + f" | assign_missing={assign_missing}"
      + f" | dry_run={dry_run}")

# COMMAND ----------

# MAGIC %md ## Step 1: list both sides

# COMMAND ----------

if principal_name in SYSTEM_GROUPS:
    raise ValueError(f"'{principal_name}' is a system group; Databricks locks its entitlements")
inventory = read_inventory(source, target, kinds)
scope_items = (select_one(inventory, principal_name) if principal_name
               else [(k, key) for k in kinds for key in sorted(inventory[k][0])])

# COMMAND ----------

# MAGIC %md ## Step 2: bring source-only groups / SPs to the target (only if `assign_missing=true`)

# COMMAND ----------

plan, assign_rows, pending = [], [], frozenset()
if assign_missing:
    plan = plan_missing(inventory, scope_items, reader_app_id=client_id)
    assign_rows = bring_missing(plan, source, target, dry_run)
    if dry_run:
        pending = frozenset((p["kind"], p["key"]) for p in plan if p["action"] == "assign")
    elif any(r["action"] == "ASSIGNED" for r in assign_rows):
        print("\nRe-reading the target after the changes …")
        inventory = read_inventory(source, target, kinds)
else:
    print("assign_missing=false: source-only groups / SPs are only REPORTED (Step 4), not brought over.")

# COMMAND ----------

# MAGIC %md ## Step 3: sync entitlements for what exists on both sides

# COMMAND ----------

inventory, rows = run_sync(source, target, kinds, principal_name, dry_run, inv=inventory, pending=pending)
counts, problems = summarize(rows)

# COMMAND ----------

# MAGIC %md ## Step 4: groups / SPs that exist on only one side

# COMMAND ----------

diffs, diff_errors = [], []
if principal_name:
    print("Single-item mode: difference report skipped (clear principal_name to get it).")
else:
    _planned = {(p["kind"], p["key"]): "WOULD BE ASSIGNED (assign_missing)"
                for p in plan if p["action"] == "assign"} if dry_run else {}
    diffs, diff_errors = find_differences(source, target, inventory, source_reader_app_id=client_id,
                                          planned=_planned)

# COMMAND ----------

# MAGIC %md ## Results tables

# COMMAND ----------

if assign_rows:
    display(spark.createDataFrame(assign_rows, schema="type string, name string, action string, detail string"))
if rows:
    display(spark.createDataFrame(rows).select(
        "type", "name", "status", "source_entitlements", "target_before", "added", "target_after",
        "extra_on_target", "detail"))
if diffs:
    display(spark.createDataFrame(diffs, schema=(
        "side string, type string, name string, application_id string, entitlements string, "
        "members_users int, members_sps int, members_groups int, note string")).select(
        "side", "type", "name", "application_id", "entitlements", "members_users", "members_sps",
        "members_groups", "note"))

# COMMAND ----------

assign_problems = [r for r in assign_rows if r["action"] in BAD_STATUSES]
if problems or diff_errors or assign_problems:
    raise RuntimeError(
        f"{len(assign_problems)} bring-over problem(s), {len(problems)} sync problem(s) ("
        + ", ".join(f"{t} {s}={n}" for (t, s), n in sorted(counts.items()) if s in BAD_STATUSES)
        + f") and {len(diff_errors)} difference-report read error(s). See the output above.")
_src_only = sum(1 for d in diffs if d["side"] == "source_only")
_tgt_only = sum(1 for d in diffs if d["side"] == "target_only")
print("Done: " + ("dry run, nothing written." if dry_run else "every write verified.")
      + (f" {_src_only} on source only, {_tgt_only} on target only (listed above)." if diffs else ""))
