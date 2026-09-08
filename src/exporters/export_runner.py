"""
ExportRunner — orchestrates `02_Export` (SOURCE side). Mirrors InventoryRunner (Plan 2 §6).

Flow:
  1. Load `inventory.json` from the resolved run's bundle dir (Export reuses inventory — it does
     NOT re-list the source; §2). If absent, run inventory first so the run is self-consistent.
  2. Build create-ready per-unit export records from the inventory (`asset_export.build_all`).
  3. Apply per-asset toggles → toggled-off families become `skip` rows (kept in the index).
  4. Collect ACLs → `export/acls.json`; stamp `acl_grants` counts onto units (`acl_writer`).
  5. Parallel content pass: fetch notebook/file BYTES (§7c) — the only slow step; resumable via
     the checkpoint so a re-run skips already-fetched content (§7a).
  6. Write per-asset payload files, `export_index.json` (the tie-back ledger), `oversize_
     artifacts.json`, `manual_actions.md`, `export_status.xlsx`, then `manifest.json` LAST
     (its presence = the bundle is complete, which drives resume detection).

Fail-soft throughout: one unit's failure is recorded (status+note+WARNING log) and the run
continues; the bundle is always finished + manifested. No target calls, no secrets.
"""
from __future__ import annotations

from src.exporters.acl_writer import acl_counts, collect_acls
from src.exporters.asset_export import (
    DAB_CONTENT_NOTE,
    TOGGLE_FOR,
    _ACTION_DAB,
    build_all,
    dab_bundle_root,
    derive_import_action,
    index_record,
    is_dab_content_path,
)
from src.exporters import bundle_paths as BP
from src.exporters.content_fetcher import ContentFetcher
from src.exporters.parallel import Locked, parallel_map
from src.transform.transforms import fingerprint
from src.utils.helpers import now_iso
from src.utils.logger import get_logger

_LOG = get_logger("export")

# Statuses that mean "the payload/content was actually produced" (counted as done).
_PRODUCED = {"success", "skipped_oversize"}

# How many fetched content items to accumulate before rewriting checkpoint.json. A deliberate
# middle ground, NOT a tunable — see the rationale in `_fetch_content`. 200 keeps the Volume
# writes negligible (~25 rewrites for 5k notebooks) while capping what a crash re-fetches.
CHECKPOINT_BATCH = 200


def _apply_content_fingerprint(unit: dict, content_sha256: str) -> None:
    """Re-fingerprint a content unit over `payload + the content hash` (§7c-audit GAP 1).

    A notebook/workspace-file unit's payload is only `{path, object_type, language}`, so the
    fingerprint built at unit-construction time is blind to the file's actual CONTENT. Editing a
    notebook's code on source therefore produced an IDENTICAL fingerprint, the target's upsert
    decided SKIP, and the target kept the old code — on a fully green report. Hashing the bytes
    alongside the payload is what makes "the source changed" detectable for the assets this tool
    exists to move.

    `_content_sha256` is a FINGERPRINT INPUT ONLY — it is deliberately not added to `payload`,
    which must stay a valid create body (the workspace import API would reject the extra field).
    A blank hash leaves the fingerprint untouched, so a failed/oversize unit (no bytes fetched)
    keeps its metadata-only hash rather than silently hashing the empty string.
    """
    if not content_sha256:
        return
    unit["content_sha256"] = content_sha256
    unit["fingerprint"] = fingerprint({**(unit.get("payload") or {}),
                                       "_content_sha256": content_sha256})


class ExportRunner:
    def __init__(self, client, config, artifact_writer, dbutils=None,
                 content_fetch_workers: int = 8, force_full_export: bool = False) -> None:
        self.client = client
        self.config = config
        self.aw = artifact_writer
        self.dbutils = dbutils
        self.workers = int(content_fetch_workers or 1)
        self.force_full = bool(force_full_export)

    # ── inventory input ────────────────────────────────────────────────────
    def _load_inventory(self) -> dict:
        inv = self.aw.read_json(BP.INVENTORY_JSON)
        if inv is None:
            _LOG.warning("inventory.json absent — running inventory first for a consistent bundle")
            from src.collectors.inventory_runner import InventoryRunner
            InventoryRunner(self.client, self.config, self.aw, self.dbutils).run()
            inv = self.aw.read_json(BP.INVENTORY_JSON) or {}
        return inv

    def run(self) -> dict:
        self.aw.ensure_output_path()
        inventory = self._load_inventory()
        objects_by_type = inventory.get("objects_by_type", {}) or {}

        # 2. Build units (pure transform).
        units_by_type = build_all(objects_by_type)

        # 3. Toggles → skip toggled-off families (still recorded).
        self._apply_toggles(units_by_type)

        # 4. ACLs → acls.json + stamp counts.
        acls = collect_acls(objects_by_type)
        self.aw.write_json(BP.EXPORT_ACLS_JSON, acls)
        counts_by_key = acl_counts(acls)
        for units in units_by_type.values():
            for u in units:
                u["acl_grants"] = counts_by_key.get((u["asset_type"], u["natural_key"]), 0)

        # 5. Parallel content pass (resumable).
        oversize_rows = self._fetch_content(units_by_type)

        # Re-derive import_action now that every status is final. Toggles (step 3) and the
        # content pass (step 5) can change a unit's export_status AFTER build_all stamped it —
        # a toggled-off or oversize unit must not still advertise "CREATE on target".
        self._refresh_import_actions(units_by_type)

        # 6. Write artifacts.
        self._write_artifact_files(units_by_type)
        self.aw.write_json(BP.EXPORT_OVERSIZE_JSON, oversize_rows)
        self._write_manual_actions(units_by_type, oversize_rows)

        index = self._build_index(units_by_type)
        self.aw.write_json(BP.EXPORT_INDEX_JSON, index)
        self._append_export_config()
        self._write_excel(objects_by_type, index)

        # manifest LAST — its presence marks the bundle complete (resume detection, §7a).
        asset_counts = {t: len(u) for t, u in units_by_type.items()}
        manifest = self.aw.write_manifest(asset_counts)

        # LATEST_EXPORT.json AFTER the manifest (Plan 3 §3): the pointer import resolves a bundle
        # through. Writing it last is the point — its existence proves the bundle it names is
        # complete, which the inventory pointer cannot promise. Best-effort: a pointer hiccup must
        # not fail an otherwise-good export (import can still be given an explicit run_id).
        try:
            from src.exporters.bundle_state import write_latest_export_pointer
            write_latest_export_pointer(self.config, self.config.run_id, manifest, asset_counts)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("latest-export pointer not written", error=str(exc))

        summary = self._summary(index)
        _LOG.info("export complete", **{k: summary[k] for k in ("total", "success", "failure",
                                                                 "skipped_oversize", "manual",
                                                                 "dab", "skip")})
        summary["output_path"] = self.aw.root
        return summary

    # ── toggles ──────────────────────────────────────────────────────────
    def _apply_toggles(self, units_by_type: dict) -> None:
        for asset_type, units in units_by_type.items():
            toggle_name = TOGGLE_FOR.get(asset_type)
            if not toggle_name:
                continue   # inventory-only families (app/lakebase) have no toggle
            if not getattr(self.config.toggles, toggle_name, True):
                for u in units:
                    u["export_status"] = "skip"
                    u["note"] = f"toggle migrate_{toggle_name}=false"

    def _refresh_import_actions(self, units_by_type: dict) -> None:
        for units in units_by_type.values():
            for u in units:
                u["import_action"] = derive_import_action(
                    u, getattr(self.config, "dab_bundle_roots", None))
                # Stamp the reason HERE, not in _make_unit: the content pass overwrites `note`
                # with the fetch result, so a note set at build time would be lost. Only for
                # units that still carry the DAB action (a toggled-off or failed one reads
                # `none`/`manual`, and its own note is the more useful message).
                if u["import_action"] == _ACTION_DAB and is_dab_content_path(
                        u.get("asset_type"), u.get("natural_key"),
                        getattr(self.config, "dab_bundle_roots", None)):
                    u["note"] = DAB_CONTENT_NOTE

    # ── content fetch (parallel, resumable) ────────────────────────────────
    def _fetch_content(self, units_by_type: dict) -> list[dict]:
        """Fetch bytes for content units not toggled-off. Returns the oversize rows list.

        Resume (§7a): a content unit already marked done in the checkpoint is skipped and its
        prior index row (content_ref/status) reused from the previous export_index.json.
        """
        content_units = [u for at in ("notebook", "workspace_file")
                         for u in units_by_type.get(at, [])
                         if u["export_status"] != "skip"]
        if not content_units:
            return []

        prior = self._prior_index_by_key()
        # Outcomes recorded in the checkpoint by earlier batches of THIS run (or a crashed one).
        # Preferred over the prior index: export_index.json is only written after the whole pass,
        # so after a crash it is absent/stale and cannot resume anything — which made the
        # checkpoint's done-list dead weight for exactly the case it existed for.
        cp_results = {} if self.force_full else self.aw.get_results("export:content")
        fetcher = ContentFetcher(self.client, self.aw)
        shared = Locked({"oversize": []})

        # Split into resumable (already done) vs to-fetch.
        to_fetch = []
        for u in content_units:
            done = (not self.force_full) and self.aw.is_done("export:content", u["natural_key"])
            row = cp_results.get(u["natural_key"]) if done else None
            if row is None and done:
                row = prior.get((u["asset_type"], u["natural_key"]))
            if done and row and row.get("export_status") in _PRODUCED:
                # reuse prior result — bytes already on the Volume
                u["export_status"] = row["export_status"]
                u["content_ref"] = row.get("content_ref")
                u["content_route"] = row.get("content_route", "")
                u["note"] = row.get("note", u.get("note", ""))
                # The content hash MUST be restored too, or a resumed unit re-fingerprints on
                # metadata alone and a notebook edit becomes invisible to the target's upsert
                # (§7c-audit GAP 1 — the exact class of bug §4 warns about).
                _apply_content_fingerprint(u, row.get("content_sha256", ""))
                if row.get("export_status") == "skipped_oversize" and row.get("oversize"):
                    with shared as s:
                        s["oversize"].append(row["oversize"])
            else:
                to_fetch.append(u)

        _LOG.info("content pass", to_fetch=len(to_fetch), resumed=len(content_units) - len(to_fetch),
                  workers=self.workers)

        # parallel_map YIELDS (item, result, error) as each fetch completes; here item IS the unit
        # and result is the FetchResult (a worker that raised puts the exception in `error`, result
        # stays None). The loop body runs on the MAIN thread (the generator hands results over
        # one at a time), so unit mutation + the checkpoint flush need no lock.
        #
        # Done-keys AND their outcomes are flushed in BATCHES of CHECKPOINT_BATCH. Every flush
        # rewrites the whole checkpoint file — append doesn't work on a UC Volume — so per-file
        # flushing is O(n²) bytes (~750 MB for 5k notebooks), while one flush at the very end means
        # a crash mid-pass loses ALL download progress and re-fetches everything. Batching costs
        # ~25 rewrites for 5k files and caps the re-fetch on a crash at one batch (§7c).
        done_keys: list[str] = []
        pending: list[str] = []
        pending_results: dict = {}
        for unit, res, err in parallel_map(to_fetch, fetcher.fetch, self.workers):
            if err is not None:
                unit["export_status"] = "failure"
                unit["note"] = f"content fetch worker error: {err}"
                _LOG.warning("content worker error", path=unit["natural_key"], error=str(err))
                continue
            unit["export_status"] = res.status
            unit["content_ref"] = res.content_ref
            unit["content_route"] = res.content_route
            # Fold the CONTENT hash into the fingerprint — the metadata payload alone cannot
            # detect an edited notebook (§7c-audit GAP 1).
            _apply_content_fingerprint(unit, res.content_sha256)
            if res.note:
                unit["note"] = res.note
            if res.status == "skipped_oversize" and res.oversize:
                oversize_row = dict(res.oversize)
                oversize_row.update({"asset_type": unit["asset_type"],
                                     "natural_key": unit["natural_key"]})
                unit["oversize"] = oversize_row
                with shared as s:
                    s["oversize"].append(oversize_row)
            if res.status in _PRODUCED:
                key = unit["natural_key"]
                done_keys.append(key)
                pending.append(key)
                # Exactly the fields the resume branch above reads back — enough to rebuild the
                # unit without re-fetching, and small enough that the checkpoint stays a few
                # hundred KB even for thousands of notebooks.
                pending_results[key] = {"export_status": res.status,
                                        "content_ref": res.content_ref,
                                        "content_route": res.content_route,
                                        "note": res.note or "",
                                        "content_sha256": res.content_sha256,
                                        "oversize": unit.get("oversize")}
                if len(pending) >= CHECKPOINT_BATCH:
                    self.aw.mark_done_bulk("export:content", pending, pending_results)
                    _LOG.info("content checkpoint", done=len(done_keys), of=len(to_fetch))
                    pending, pending_results = [], {}
        # Final flush for the remainder (and the whole batch when to_fetch < CHECKPOINT_BATCH).
        self.aw.mark_done_bulk("export:content", pending, pending_results)

        return shared.value["oversize"]

    def _prior_index_by_key(self) -> dict:
        prior = self.aw.read_json(BP.EXPORT_INDEX_JSON) or {}
        out = {}
        for row in prior.get("units", []) or []:
            out[(row.get("asset_type"), row.get("natural_key"))] = row
        return out

    # ── artifact files ─────────────────────────────────────────────────────
    def _write_artifact_files(self, units_by_type: dict) -> None:
        """Group payload-bearing units by their artifact file and write each.

        Only units with a real create payload (mode auto/content, not skipped) are written into
        the per-asset files; skip/manual/dab units live only in the index (with their reason).
        """
        from src.exporters.asset_export import ARTIFACT_PATH
        by_file: dict[str, list] = {}
        for units in units_by_type.values():
            for u in units:
                if u["migration_mode"] not in ("auto", "content"):
                    continue
                if u["export_status"] == "skip":
                    continue
                path = u.get("artifact") or ARTIFACT_PATH.get(u["asset_type"], "")
                if not path:
                    continue
                by_file.setdefault(path, []).append(self._artifact_unit(u))
        for path, units in by_file.items():
            self.aw.write_json(path, {"generated_utc": now_iso(), "units": units})

    @staticmethod
    def _artifact_unit(u: dict) -> dict:
        """The unit as written into a per-asset payload file (payload kept; index-only noise out)."""
        # `import_action` and `kind` ride along with the payload, not just in the index: the importer
        # reads these per-asset files to decide CREATE vs ASSIGN, and that distinction is
        # load-bearing. Creating an account SPN instead of adopting it mints a new applicationId and
        # orphans its ACLs; POSTing an account GROUP makes a workspace-local shadow that permanently
        # blocks assigning the real one. `members_are_account_owned` keeps import from patching an
        # account group's account-global membership, and `entra_backed` only words the remediation.
        keep = ("asset_type", "natural_key", "source_id", "fingerprint", "migration_mode",
                "content_ref", "content_route", "payload", "classification", "kind",
                "entra_backed", "members_are_account_owned", "workspace_permissions", "externalId",
                "import_action", "owner")
        return {k: u[k] for k in keep if k in u}

    # ── index (the ledger) ─────────────────────────────────────────────────
    def _build_index(self, units_by_type: dict) -> dict:
        units = [index_record(u) for units in units_by_type.values() for u in units]
        units.sort(key=lambda r: (r["asset_type"], r["natural_key"]))
        counts: dict[str, dict] = {}
        # Per-asset_type status counts, plus a flat count by import_action — the latter answers
        # "how much does the tool do vs. the bundle vs. a human?" in one glance, which no
        # per-type status table can.
        action_counts: dict[str, int] = {}
        for r in units:
            at = r["asset_type"]
            bucket = counts.setdefault(at, {"total": 0})
            bucket["total"] += 1
            st = r["export_status"]
            bucket[st] = bucket.get(st, 0) + 1
            act = r.get("import_action") or ""
            action_counts[act] = action_counts.get(act, 0) + 1
        return {
            "run_id": self.config.run_id,
            "source_workspace_id": self.config.source_workspace_id,
            "generated_utc": now_iso(),
            "tool_version": _tool_version(),
            "units": units,
            "counts": counts,
            "action_counts": action_counts,
        }

    def _summary(self, index: dict) -> dict:
        out = {"total": 0, "success": 0, "failure": 0, "skipped_oversize": 0,
               "manual": 0, "dab": 0, "skip": 0, "covered": 0, "incomplete": 0}
        for r in index["units"]:
            out["total"] += 1
            out[r["export_status"]] = out.get(r["export_status"], 0) + 1
        out["counts"] = index["counts"]
        out["action_counts"] = index.get("action_counts", {})
        return out

    # ── manual actions + config append + excel ──────────────────────────────
    def _write_manual_actions(self, units_by_type: dict, oversize_rows: list) -> None:
        lines = ["# Manual actions required after import", "",
                 "Generated by 02_Export. These units cannot be auto-recreated by this tool.", ""]
        buckets: dict[str, list] = {}
        for units in units_by_type.values():
            for u in units:
                if u["migration_mode"] == "manual" or u["export_status"] == "manual":
                    buckets.setdefault(u["asset_type"], []).append(u)
        for asset_type in sorted(buckets):
            lines.append(f"## {asset_type} ({len(buckets[asset_type])})")
            for u in sorted(buckets[asset_type], key=lambda x: x["natural_key"]):
                note = f" — {u['note']}" if u.get("note") else ""
                lines.append(f"- `{u['natural_key']}`{note}")
            lines.append("")
        # Bundle roots: one line per BUNDLE, not per file (44 file rows would bury the actual
        # instruction, which is a single redeploy per bundle).
        dab_roots = getattr(self.config, "dab_bundle_roots", None)
        roots: dict[str, int] = {}
        for units in units_by_type.values():
            for u in units:
                if is_dab_content_path(u.get("asset_type"), u.get("natural_key"), dab_roots):
                    root = dab_bundle_root(u["natural_key"], dab_roots)
                    if root:
                        roots[root] = roots.get(root, 0) + 1
        if roots:
            lines.append(f"## DAB bundles — redeploy against the target ({len(roots)})")
            lines.append("")
            lines.append("Content under these bundle roots is exported for reference but NOT "
                         "imported. Re-point each bundle at the target workspace and run "
                         "`databricks bundle deploy`; that recreates the files AND the "
                         "jobs/pipelines/dashboards the bundle owns.")
            lines.append("")
            for root in sorted(roots):
                lines.append(f"- `{root}` ({roots[root]} exported objects, not imported)")
            lines.append("")
        if oversize_rows:
            lines.append(f"## oversize — manual copy needed ({len(oversize_rows)})")
            for o in oversize_rows:
                lines.append(f"- `{o.get('natural_key', o.get('path'))}` "
                             f"({o.get('size', 0)} bytes) — {o.get('recommended', '')}")
            lines.append("")
        self.aw.write_bytes(BP.EXPORT_MANUAL_ACTIONS_MD, "\n".join(lines).encode("utf-8"))

    def _append_export_config(self) -> None:
        cfg = self.aw.read_json(BP.CONFIG_RESOLVED_JSON) or {}
        cfg["export_options"] = {"content_fetch_workers": self.workers,
                                 "force_full_export": self.force_full}
        self.aw.write_json(BP.CONFIG_RESOLVED_JSON, cfg)

    def _write_excel(self, objects_by_type: dict, index: dict) -> None:
        try:
            from src.exporters.export_excel import generate_export_excel
            self.aw.write_text_local_then_copy(
                BP.EXPORT_STATUS_XLSX,
                lambda local: generate_export_excel(objects_by_type, index, local, self.config),
            )
        except Exception as exc:  # noqa: BLE001 — Excel is a convenience; never fail the run
            _LOG.warning("export excel skipped", error=str(exc))


def _tool_version() -> str:
    from src.exporters.artifact_writer import TOOL_VERSION
    return TOOL_VERSION
