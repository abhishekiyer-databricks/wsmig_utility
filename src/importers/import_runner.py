"""
ImportRunner — orchestrates `04_Import` (TARGET side). Mirror of InventoryRunner/ExportRunner.

Flow:
  1. **Resolve which bundle to act on** (§3): run_id widget → resume an incomplete import →
     `LATEST_EXPORT.json` pointer → fail loudly. Never invent a run_id; that would import an empty
     bundle and report a spuriously clean run.
  2. **Verify the bundle** — a HARD gate (D7). Any checksum mismatch or missing file aborts BEFORE
     a single write: a partial upload must never present as a partial migration.
  3. **Prepare state** — ensure the tables, then REPLAY any checkpoint outcomes that never reached
     the table (§7a), so a crashed run's created objects are known before anything is decided.
  4. **Validate the selection** (§5) — a family whose prerequisites are neither selected nor already
     in the state table is a hard error naming them.
  5. **Run the phases in order**, each fail-soft per unit, each ending with a MANDATORY state flush
     so the next phase reads a complete id map.
  6. **Report** — `import_results.{json,html}`, `import_status.xlsx`, `manual_actions.md`,
     `acl_parity_report.*`.

The four WHOLE-RUN aborts (D21), all before any unit is attempted, all cases where continuing gives
a *wrong* target rather than an incomplete one: bad manifest, preflight NO-GO, unreachable state
schema when live, unauthenticatable source client. Everything per-unit is fail-soft. Even on an
abort the `finally` flush persists what was already done.
"""
from __future__ import annotations

import time
from typing import Optional

from src.exporters import bundle_paths as BP
from src.importers.phases import (FAMILY_ASSET_TYPES, LEGACY_CHECKPOINT_TYPE, PHASE_ORDER,
                                  asset_types_for, ordered, validate_selection)
from src.state.state_store import ACTED_STATUSES, ACTION_NOT_SELECTED, StateStore
from src.utils.helpers import now_iso, safe_str
from src.utils.logger import fmt_counts, fmt_elapsed, get_logger

_LOG = get_logger("import")


# run_status when every phase ran but the final state save failed (PLAN 16.1 §4.2): the target was
# changed, but the source→target id map was NOT persisted. The notebook raises on it so the job
# goes red; the checkpoint recovery replay re-merges the rows on the next run.
RUN_STATUS_STATE_NOT_SAVED = "completed_state_not_saved"


_DASHBOARD_CHILD_TYPES = ("lakeview_dashboard_publish", "lakeview_dashboard_schedule")


class BundleVerificationError(RuntimeError):
    """The bundle failed its manifest check — abort before any write (D7)."""


class PrerequisiteError(RuntimeError):
    """The selection is missing prerequisites that are neither selected nor already imported (§5)."""


def resolve_import_run_id(config, explicit_run_id: str = "") -> tuple[str, str]:
    """Which bundle does import act on? Returns (run_id, how). Raises if none resolvable (§3).

    Precedence mirrors Export's so the two behave alike:
      1. explicit run_id (widget or task value) → deliberate control; re-import a specific bundle
      2. the newest INCOMPLETE import (an import checkpoint present, no import_results.json) →
         RESUME it, which is what makes a plain re-run continue rather than restart
      3. `LATEST_EXPORT.json` → the newest bundle whose export COMPLETED (the pointer is written
         after the manifest, so its existence proves completeness)
      4. fail loudly — never invent a run_id
    """
    from src.exporters.bundle_state import list_run_ids, read_latest_export_pointer, run_dir
    import os

    explicit = (explicit_run_id or "").strip()
    if explicit:
        return explicit, "widget"

    if not config.imports.force_full_import:
        for rid in list_run_ids(config):
            d = run_dir(config, rid)
            started = os.path.isfile(os.path.join(d, BP.CHECKPOINT_JSON))
            finished = os.path.isfile(os.path.join(d, BP.IMPORT_RESULTS_JSON))
            # An import that started and never finished is resumable. Requiring a manifest too
            # keeps us from "resuming" a bundle whose EXPORT never completed.
            if started and not finished and os.path.isfile(os.path.join(d, BP.MANIFEST_JSON)):
                if _has_import_checkpoint(d):
                    return rid, "resume-incomplete-import"

    pointer = read_latest_export_pointer(config)
    if pointer and pointer.get("run_id"):
        return safe_str(pointer["run_id"]), "LATEST_EXPORT.json"

    from src.exporters.bundle_state import wsmig_root
    raise RuntimeError(
        "Cannot resolve which bundle to import: no run_id widget, no incomplete import, and no "
        f"LATEST_EXPORT.json under {wsmig_root(config)}. Run 02_Export first (in `airgap` mode, "
        "check ops copied the whole run dir AND LATEST_EXPORT.json), or pass an explicit run_id.")


def _has_import_checkpoint(run_dir_path: str) -> bool:
    """Whether the checkpoint carries IMPORT progress (not just export's content keys)."""
    import json
    import os
    p = os.path.join(run_dir_path, BP.CHECKPOINT_JSON)
    try:
        with open(p, encoding="utf-8") as f:
            cp = json.load(f) or {}
    except Exception as exc:  # noqa: BLE001 — degraded: an unreadable checkpoint is not resumable
        _LOG.warning(f"checkpoint unreadable — this run dir is not treated as a resumable import: "
                     f"{exc}", path=p)
        return False
    return any(str(k).startswith("import:") for k in cp)


class ImportRunner:
    # Fields that live on the PAYLOAD file rather than the ledger row, and must be merged back onto
    # each unit before the importers see it. `kind` and `members_are_account_owned` are here because
    # the identity importer branches on them: without `kind` every group degrades to NEEDS_REVIEW and
    # is skipped, and without the members flag an account group's account-global membership would be
    # patched — changing that group in every OTHER workspace sharing the account.
    _PAYLOAD_CARRY_FIELDS = ("content_route", "classification", "owner", "kind", "entra_backed",
                             "members_are_account_owned", "workspace_permissions", "externalId")

    def __init__(self, client, config, artifact_writer, state=None, dbutils=None,
                 preflight_verdict: Optional[dict] = None) -> None:
        self.client = client
        self.config = config
        self.aw = artifact_writer
        self.state = state
        self.dbutils = dbutils
        self.preflight_verdict = preflight_verdict or {}
        self.context: dict = {}          # cross-phase id maps
        self.results: list = []          # ImportResult per phase
        self.run_status = "completed"

    # ── gates (whole-run preconditions) ───────────────────────────────────
    def verify_bundle(self) -> dict:
        """Manifest verification — a HARD gate (D7), not a warning.

        `skip_manifest_verify` exists only for a customer who deliberately hand-pruned a bundle, and
        it stamps a loud warning into the report rather than passing silently.
        """
        _LOG.info("Phase: verify bundle manifest")
        t0 = time.time()
        if self.config.imports.skip_manifest_verify:
            _LOG.warning("MANIFEST VERIFICATION SKIPPED by skip_manifest_verify=true — the bundle "
                         "was NOT checked for completeness; a partial upload will present as a "
                         "partial migration")
            _LOG.info("Phase complete: verify bundle manifest — SKIPPED")
            return {"ok": True, "skipped": True, "missing": [], "mismatched": []}
        verify = self.aw.verify_manifest()
        if not verify["ok"]:
            raise BundleVerificationError(
                "The bundle failed its manifest check, so import will NOT start — a partial upload "
                "must never present as a partial migration.\n"
                f"  missing files   ({len(verify['missing'])}): {verify['missing'][:10]}\n"
                f"  checksum mismatch ({len(verify['mismatched'])}): {verify['mismatched'][:10]}\n"
                "Re-copy the whole run directory from the source staging location, or set "
                "skip_manifest_verify=true if you deliberately pruned it.")
        _LOG.info(f"Phase complete: verify bundle manifest — "
                  f"{len((verify.get('manifest') or {}).get('files', [])):,} files, "
                  f"{len(verify['missing'])} missing, {len(verify['mismatched'])} mismatched "
                  f"({fmt_elapsed(time.time() - t0)})")
        return verify

    def check_pointer_matches_bundle(self) -> Optional[str]:
        """Detect a `LATEST_EXPORT.json` left over from a DIFFERENT upload (§3).

        In `airgap` mode ops copies the run dir AND the pointer. If the pointer names this run but
        its `manifest_checksum` doesn't match the manifest actually present, the two came from
        different exports — refuse rather than import a mismatched bundle. Returns a warning string
        when the pointer names a different run (that's normal when run_id was passed explicitly).
        """
        from src.exporters.bundle_state import manifest_checksum, read_latest_export_pointer
        pointer = read_latest_export_pointer(self.config)
        if not pointer:
            return None
        if safe_str(pointer.get("run_id")) != safe_str(self.config.run_id):
            return (f"LATEST_EXPORT.json names run {pointer.get('run_id')!r} but this run is "
                    f"{self.config.run_id!r} (normal when run_id was passed explicitly)")
        manifest = self.aw.read_json(BP.MANIFEST_JSON) or {}
        expected = safe_str(pointer.get("manifest_checksum"))
        actual = manifest_checksum(manifest)
        if expected and expected != actual:
            raise BundleVerificationError(
                f"LATEST_EXPORT.json points at run {self.config.run_id!r} but its "
                f"manifest_checksum does not match the manifest.json present in that directory.\n"
                f"  pointer says: {expected}\n  bundle has  : {actual}\n"
                "The pointer and the bundle came from DIFFERENT exports — most likely the pointer "
                "was copied from a newer run than the run directory. Re-copy both from the same "
                "export, or pass run_id explicitly.")
        return None

    def enforce_preflight(self) -> None:
        """Abort on a preflight NO-GO when `preflight_enforce` (D8).

        The failure this prevents — importing against a target missing its account identities —
        produces thousands of half-migrated ACLs that are far more work to unwind than to prevent.
        """
        verdict = safe_str(self.preflight_verdict.get("verdict"))
        if verdict == "NO-GO" and self.config.imports.preflight_enforce:
            blockers = self.preflight_verdict.get("blocking") or []
            raise RuntimeError(
                "Preflight returned NO-GO, so import will not run:\n  - "
                + "\n  - ".join(str(b) for b in blockers[:10])
                + "\nFix the blocking prerequisites, or set preflight_enforce=false to proceed "
                  "with the gaps accepted (they will show as failures per unit).")

    # ── the run ───────────────────────────────────────────────────────────
    def run(self) -> dict:
        t0 = time.time()
        summary: dict = {}
        try:
            # ── whole-run preconditions, all BEFORE any unit is attempted ──
            _LOG.info("import run starting", run_id=self.config.run_id,
                      dry_run=self.config.dry_run, bundle=self.aw.root)
            self.verify_bundle()
            pointer_note = self.check_pointer_matches_bundle()
            if pointer_note:
                _LOG.info("export pointer note", note=pointer_note)
            self.enforce_preflight()

            units_by_type = self._units_from_bundle()

            # ── state: ensure + recovery replay BEFORE any decision ────────
            if self.state is not None and self.state.enabled:
                _LOG.info("Phase: load migration state")
                t_state = time.time()
                self.state.ensure_table()
                self.state.load(force=True)    # count-checked; raises StateLoadError on a gap
                replayed = self.state.recovery_replay(self._all_checkpoint_outcomes())
                _LOG.info(f"Phase complete: load migration state — {len(self.state._cache):,} "
                          f"rows, recovery replay merged {replayed:,} "
                          f"({fmt_elapsed(time.time() - t_state)})")
            else:
                _LOG.info("state store disabled (dry run without state_catalog) — every unit is "
                          "decided from the live existence check alone")

            # ── selection + prerequisite validation ────────────────────────
            selected = list(self.config.imports.selected_families)
            problems = validate_selection(selected, self.state)
            if problems:
                raise PrerequisiteError(
                    "The selected import_assets are missing prerequisites:\n  - "
                    + "\n  - ".join(problems))
            _LOG.info("import starting", run_id=self.config.run_id, dry_run=self.config.dry_run,
                      families=",".join(selected), retry_mode=self.config.imports.retry_mode)

            # Families NOT selected are recorded as deferred work — visible in the report and
            # queryable via retry_mode, never a silent gap.
            self._record_not_selected(units_by_type, selected)

            # ── phases ────────────────────────────────────────────────────
            families = ordered(selected)
            for i, family in enumerate(families, 1):
                _LOG.info(f"family {i}/{len(families)}: {family}")
                self._run_phase(family, units_by_type)

            # ── deleted-in-source detection (report only, D5) ─────────────
            self._report_deleted_in_source(units_by_type, selected)

        except Exception as exc:  # noqa: BLE001
            # A whole-run abort. Still persists what was done (below) and marks the run visibly
            # incomplete, so an aborted run never looks clean.
            self.run_status = "aborted"
            summary["abort_reason"] = str(exc)
            _LOG.error(f"import run aborted: {type(exc).__name__}: {exc}", exc_info=True)
            raise
        finally:
            # Run-level flush (§7a level 3): even a KeyboardInterrupt / job timeout persists the
            # state rows and writes a partial results file marked aborted.
            if self.state is not None:
                _LOG.info("final state flush")
                self.state.flush()
                if getattr(self.state, "flush_failed", False) and self.run_status == "completed":
                    # Every phase ran, but the id map was NOT saved — the job must go red rather
                    # than finish green with target objects the state table doesn't know about.
                    self.run_status = RUN_STATUS_STATE_NOT_SAVED
                    _LOG.error("the final state save FAILED — run_status=completed_state_not_saved. "
                               "The target WAS changed; the checkpoint recovery replay re-merges "
                               "the rows on the next run. Fix the state table, then re-run.")
            summary.update(self._summarize(t0))
            try:
                self._write_reports(summary)
            except Exception as exc:  # noqa: BLE001 — reporting must not mask the real error
                _LOG.warning("report writing failed", error=str(exc), exc_info=True)
            totals = summary.get("totals") or {}
            _LOG.info(f"import run {summary.get('run_status')} — "
                      + fmt_counts({k: totals.get(k, 0) for k in
                                    ("total", "created", "updated", "adopted", "skipped",
                                     "created_with_warning", "manual", "skipped_no_object",
                                     "failed")})
                      + f" ({fmt_elapsed(time.time() - t0)})")
        return summary

    # ── bundle reading ────────────────────────────────────────────────────
    def _units_from_bundle(self) -> dict:
        """`{asset_type: [unit, ...]}` from the per-asset payload files + the index.

        The INDEX is the ledger of every unit (including manual/dab/skip rows with no payload); the
        per-asset files carry the payloads. Joining them means a unit with no payload still gets a
        reported outcome rather than vanishing — "never silently skip" (§1.7).
        """
        from src.exporters.artifact_writer import read_json_or_empty
        from src.exporters.asset_export import ARTIFACT_PATH
        _LOG.info("Phase: read bundle")
        t0 = time.time()
        index = read_json_or_empty(self.aw, BP.EXPORT_INDEX_JSON, "NO units (nothing to import)")
        payloads: dict[tuple, dict] = {}
        for rel in sorted(set(ARTIFACT_PATH.values())):
            # A per-asset payload file is legitimately absent when its family had no auto/content
            # units, so its absence is DEBUG, not a warning; the index is the ledger.
            doc = self.aw.read_json(rel)
            if doc is None:
                _LOG.debug(f"bundle payload file {rel} absent (no payload-bearing units)")
                continue
            for u in (doc or {}).get("units", []) or []:
                payloads[(safe_str(u.get("asset_type")), safe_str(u.get("natural_key")))] = u

        out: dict[str, list] = {}
        for row in index.get("units", []) or []:
            at, nk = safe_str(row.get("asset_type")), safe_str(row.get("natural_key"))
            unit = dict(row)
            payload_unit = payloads.get((at, nk))
            if payload_unit:
                unit["payload"] = payload_unit.get("payload") or {}
                # content_route/classification live on the payload file for content + identity.
                # `kind` and friends MUST be carried too: the identity importer chooses
                # create-vs-assign from `kind`, and a missing one degrades every group to
                # NEEDS_REVIEW (skipping it) — while `members_are_account_owned` is what stops an
                # account group's account-global membership being patched.
                for extra in ImportRunner._PAYLOAD_CARRY_FIELDS:
                    if extra in payload_unit:
                        unit[extra] = payload_unit[extra]
            else:
                unit.setdefault("payload", {})
            out.setdefault(at, []).append(unit)
        _LOG.info(f"Phase complete: read bundle — {sum(len(v) for v in out.values()):,} units, "
                  f"{len(payloads):,} payloads ({fmt_elapsed(time.time() - t0)})",
                  **{at: len(v) for at, v in sorted(out.items())})
        return out

    def _all_checkpoint_outcomes(self) -> dict:
        """Every `import:<family>` checkpoint outcome, keyed `"<asset_type>|<natural_key>"`.

        This is what the recovery replay merges: outcomes durably recorded per-item in the
        checkpoint but lost with an unflushed state batch.
        """
        out: dict = {}
        for family in PHASE_ORDER:
            for key, row in (self.staging_results(f"import:{family}") or {}).items():
                if not isinstance(row, dict):
                    continue
                at = safe_str(row.get("asset_type"))
                if not at:
                    # A row written before asset_type was carried: infer it from the family when
                    # the family maps to exactly ONE type, else skip (better than guessing wrong —
                    # a mis-keyed replay row would write state against the wrong asset_type).
                    types = FAMILY_ASSET_TYPES.get(family, ())
                    if len(types) == 1:
                        at = types[0]
                    elif family in LEGACY_CHECKPOINT_TYPE:
                        at = LEGACY_CHECKPOINT_TYPE[family]
                    else:
                        continue
                out[f"{at}|{key}"] = row
        return out

    def staging_results(self, component: str) -> dict:
        try:
            return self.aw.get_results(component)
        except Exception as exc:  # noqa: BLE001 — degraded: nothing to replay for this family
            _LOG.warning(f"could not read checkpoint outcomes for {component} — none replayed: "
                         f"{exc}")
            return {}

    # ── phases ────────────────────────────────────────────────────────────
    def _importer_for(self, family: str):
        """The importer class for a family (imported lazily so one broken module can't break all)."""
        from src.importers.acl_importer import AclImporter
        from src.importers.compute_importer import ComputeImporter
        from src.importers.dashboards_importer import DashboardsImporter
        from src.importers.dlt_importer import DltImporter
        from src.importers.genie_importer import GenieImporter
        from src.importers.identity_importer import IdentityImporter
        from src.importers.jobs_importer import JobsImporter
        from src.importers.misc_importer import MiscImporter
        from src.importers.secrets_importer import SecretsImporter
        from src.importers.serving_importer import ServingImporter
        from src.importers.sql_importer import SqlImporter
        from src.importers.workspace_importer import WorkspaceImporter
        return {
            "identity": IdentityImporter, "compute": ComputeImporter,
            "workspace": WorkspaceImporter, "secrets": SecretsImporter, "jobs": JobsImporter,
            "sql": SqlImporter, "dlt": DltImporter, "dashboards": DashboardsImporter,
            "genie": GenieImporter, "serving": ServingImporter, "misc": MiscImporter,
            "acls": AclImporter,
        }.get(family)

    def _run_phase(self, family: str, units_by_type: dict) -> None:
        """Run one phase, then flush state — MANDATORY, because the NEXT phase reads the id map."""
        cls = self._importer_for(family)
        if cls is None:
            _LOG.warning("no importer for family", family=family)
            return
        identity_map = (self.state.load_identity_map() if self.state is not None
                        else {"sp_mapping": {}, "group_map": {}, "user_map": {}, "scim_ids": {}})
        importer = cls(self.client, self.config, self.aw, state=self.state,
                       identity_map=identity_map, dbutils=self.dbutils, context=self.context)
        importer.units_by_type = units_by_type
        importer.retry_keys = (self.state.retry_keys(self.config.imports.retry_mode)
                               if self.state is not None else None)
        if importer.retry_keys is not None and hasattr(importer, "retry_keys_following"):
            # PLAN 16.2 §5a: a retry that HEALS an object must also apply its ACL in the same run.
            # The ACL unit sat as `skipped_no_object` (not in the failed bucket), so without this a
            # failed_only retry healed 143 objects and applied 0 of their grants (16.1 QA Run 3).
            extra = importer.retry_keys_following(self._acted_this_run()) - importer.retry_keys
            if extra:
                _LOG.info(f"retry: {len(extra):,} {family} unit(s) of objects acted on this run "
                          f"join the work list")
                importer.retry_keys = set(importer.retry_keys) | extra
        try:
            self.results.append(importer.run())
        finally:
            # §7a level 2: a phase-level abort flushes BOTH tables before the exception continues
            # upward, and a normal phase end flushes so the next phase sees a complete id map.
            importer.flush_checkpoint()
            if self.state is not None:
                t_flush = time.time()
                _LOG.debug(f"state flush after phase {family} started")
                written = self.state.flush()
                _LOG.info(f"state flush after phase {family} — {written:,} rows "
                          f"({fmt_elapsed(time.time() - t_flush)})"
                          + (" — FAILED, rows still pending"
                             if getattr(self.state, "flush_failed", False) else ""))

    def _acted_this_run(self) -> set:
        """`{(asset_type, natural_key)}` of every unit this run created / updated / adopted (incl.
        created_with_warning) — the objects whose dependents follow them on a retry (§5a)."""
        out: set = set()
        for res in self.results:
            for row in res.units:
                if row.get("retry_out_of_scope"):
                    continue
                if safe_str(row.get("import_status")) in ACTED_STATUSES:
                    out.add((safe_str(row.get("asset_type")), safe_str(row.get("natural_key"))))
        return out

    def _record_not_selected(self, units_by_type: dict, selected: list) -> None:
        """Record deferred families as `not_selected` — deferred work must be visible, not absent.

        These rows are what make `retry_mode=skipped_only` able to pick the family up later: a unit
        with no state row is invisible to a retry.
        """
        if self.state is None or not self.state.enabled:
            return
        deferred = [f for f in PHASE_ORDER if f not in selected]
        if deferred:
            _LOG.info(f"recording {len(deferred)} deselected families as not_selected",
                      families=",".join(deferred))
        for family in deferred:
            for at in FAMILY_ASSET_TYPES.get(family, ()):
                for unit in units_by_type.get(at, []) or []:
                    key = safe_str(unit.get("natural_key"))
                    row = self.state.row(at, key)
                    # Don't overwrite a real outcome from an earlier session with "not selected".
                    if row and safe_str(row.get("last_action")) not in (ACTION_NOT_SELECTED, ""):
                        continue
                    self.state.record(at, key, action=ACTION_NOT_SELECTED,
                                      fingerprint=safe_str(unit.get("fingerprint")),
                                      source_object_id=safe_str(unit.get("source_id")),
                                      error=f"family `{family}` was not selected in this run "
                                            f"(import_assets); re-run with import_assets={family} "
                                            f"or retry_mode=skipped_only")
        if deferred:
            self.state.flush()

    def _report_deleted_in_source(self, units_by_type: dict, selected: list) -> None:
        """Flag state rows whose natural_key is gone from the bundle. REPORT ONLY (D5).

        Only for families actually imported this run — a family that was skipped has no bundle
        keys to compare against, and calling everything "deleted" there would be nonsense.
        """
        if self.state is None or not self.state.enabled:
            return
        gone: dict[str, list] = {}
        dashboards_in_bundle = bool(units_by_type.get("lakeview_dashboard"))
        for at in asset_types_for(selected):
            present = {safe_str(u.get("natural_key")) for u in units_by_type.get(at, []) or []}
            # A dashboard's publish/schedule units are legitimately ALL gone when every source
            # dashboard was unpublished — still compared, as long as the bundle has dashboards
            # (PLAN 16.2 §4: a source unpublish is flagged, never acted on).
            if not present and not (at in _DASHBOARD_CHILD_TYPES and dashboards_in_bundle):
                continue
            missing = self.state.mark_missing_in_source(at, present)
            if missing:
                gone[at] = missing
        if gone:
            self.state.flush()
            _LOG.info("deleted-in-source detected (reported, NOT deleted on target)", **{
                k: len(v) for k, v in gone.items()})
        else:
            _LOG.info("deleted-in-source: none")
        self.context["deleted_in_source"] = gone

    # ── summary + reports ─────────────────────────────────────────────────
    def _summarize(self, t0: float) -> dict:
        totals: dict[str, int] = {}
        for res in self.results:
            for k, v in res.as_dict().items():
                if isinstance(v, int):
                    totals[k] = totals.get(k, 0) + v
        return {
            "run_id": self.config.run_id,
            "source_workspace_id": self.config.source_workspace_id,
            "connectivity_mode": self.config.connectivity_mode,
            "dry_run": self.config.dry_run,
            "run_status": self.run_status,
            "generated_utc": now_iso(),
            "elapsed_sec": round(time.time() - t0, 3),
            "families": [r.component for r in self.results],
            "totals": totals,
            "per_phase": [r.as_dict() for r in self.results],
            "output_path": self.aw.root,
        }

    def _write_reports(self, summary: dict) -> None:
        from src.reports.import_report import write_import_reports
        # PLAN 11 Finding-4: hand the report the CUMULATIVE outstanding items from the STATE TABLE
        # for this pair (across ALL runs), so the "Outstanding" sheet shows everything not yet
        # migrated — old carry-overs AND new — regardless of whether this run touched it.
        if self.state is not None and self.state.enabled:
            try:
                outstanding = self.state.outstanding_rows()
                self.context["outstanding"] = outstanding
                buckets: dict = {}
                for r in outstanding:
                    a = safe_str(r.get("last_action"))
                    buckets[a] = buckets.get(a, 0) + 1
                _LOG.info("outstanding (cumulative, from state table)",
                          total=len(outstanding), **buckets)
            except Exception as exc:  # noqa: BLE001 — reporting must not fail the run
                _LOG.warning("could not read outstanding rows from state", error=str(exc))
        # Record the ACTUAL report paths written (they vary: dry_run / retry_<ts> / canonical) so
        # the notebook reads back the files this run produced rather than a stale canonical name.
        _LOG.info("Phase: write import reports")
        t0 = time.time()
        summary["reports"] = write_import_reports(
            self.aw, self.config, summary, self.results, self.context)
        _LOG.info(f"Phase complete: write import reports ({fmt_elapsed(time.time() - t0)})",
                  **{k: v for k, v in (summary["reports"] or {}).items() if isinstance(v, str)})
