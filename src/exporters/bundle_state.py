"""
bundle_state — run-isolated bundle discovery + the run_id resolution logic (Plan 2 §2b, §7a).

Pure filesystem helpers over the staging tree `<staging>/wsmig/<src_ws_id>/<run_id>/`, plus the
two resolvers that decide WHICH run a notebook acts on. Kept dependency-free (only Config +
helpers) and side-effect-light so it can run BEFORE a run_id / ArtifactWriter exists and be
unit-tested offline against a temp dir.

Key facts these encode:
  • A bundle is COMPLETE only once `manifest.json` is written (Export's very last step). A run
    dir with a checkpoint but no manifest is provably an INTERRUPTED run → resumable (§7a).
  • `01_Inventory` drops `LATEST_INVENTORY.json` (a 3-field pointer, not data) at the wsmig root
    so `02_Export`, run separately, can find the newest inventory's run_id with no mtime reliance.
  • run_ids default to `now_compact()` (YYYYMMDD_HHMMSS), which sorts lexicographically =
    chronologically, so "latest" is a deterministic name sort — no FUSE mtime dependency (D6/C).
"""
from __future__ import annotations

import os
from typing import Optional

from src.exporters import bundle_paths as BP
from src.utils.helpers import now_iso


# ── path helpers ───────────────────────────────────────────────────────────

def wsmig_root(config) -> str:
    """`<staging>/wsmig/<src_ws_id>` — the per-workspace bundle parent (no run_id needed)."""
    staging = config.staging_location
    if not staging:
        raise ValueError("staging_location is empty for role=%r" % config.role)
    if not config.source_workspace_id:
        raise ValueError("source_workspace_id is required to locate the bundle root")
    return f"{staging}/wsmig/{config.source_workspace_id}"


def run_dir(config, run_id: str) -> str:
    return f"{wsmig_root(config)}/{run_id}"


def _pointer_path(config) -> str:
    return f"{wsmig_root(config)}/LATEST_INVENTORY.json"


def _export_pointer_path(config) -> str:
    return f"{wsmig_root(config)}/LATEST_EXPORT.json"


# ── the inventory pointer (§2b path D) ──────────────────────────────────────

def write_latest_pointer(config, run_id: str, counts: dict) -> str:
    """Write `LATEST_INVENTORY.json` (run_id + timestamp + counts). Returns its path.

    Best-effort at the CALLER's discretion — inventory wraps this so a pointer-write hiccup
    never fails the read-only inventory (it's a convenience, the operator can still pass run_id).
    """
    import json
    root = wsmig_root(config)
    os.makedirs(root, exist_ok=True)
    payload = {"run_id": run_id, "generated_utc": now_iso(), "counts": counts}
    with open(_pointer_path(config), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return _pointer_path(config)


def read_latest_pointer(config) -> Optional[dict]:
    """Read `LATEST_INVENTORY.json` → dict, or None if absent/unreadable."""
    import json
    p = _pointer_path(config)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — a garbled pointer is treated as absent
        return None


# ── the EXPORT pointer (Plan 3 §3) ──────────────────────────────────────────
# The inventory pointer is NOT usable for import: it names the run whose INVENTORY is newest,
# which is not necessarily one whose EXPORT completed. Import needs "the newest run whose bundle
# is finished", so Export writes this pointer as its very last action — AFTER manifest.json — and
# its mere existence proves the bundle it names is complete.

def write_latest_export_pointer(config, run_id: str, manifest: dict, counts: dict) -> str:
    """Write `LATEST_EXPORT.json` naming the just-completed bundle. Returns its path.

    `manifest_checksum` ties the pointer to THIS bundle's manifest, which is how import detects a
    pointer left over from a DIFFERENT upload in `airgap` mode (ops copies both files; if the
    pointer says run X but the run X dir on the target has a different manifest, import refuses and
    names both rather than importing a mismatched bundle).
    """
    import hashlib
    import json
    root = wsmig_root(config)
    os.makedirs(root, exist_ok=True)
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    payload = {
        "run_id": run_id,
        "generated_utc": now_iso(),
        "source_workspace_id": config.source_workspace_id,
        "connectivity_mode": getattr(config, "connectivity_mode", ""),
        "tool_version": manifest.get("tool_version", ""),
        "manifest_checksum": f"sha256:{digest}",
        "counts": counts,
    }
    with open(_export_pointer_path(config), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return _export_pointer_path(config)


def read_latest_export_pointer(config) -> Optional[dict]:
    """Read `LATEST_EXPORT.json` → dict, or None if absent/unreadable (garbled = absent)."""
    import json
    p = _export_pointer_path(config)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def manifest_checksum(manifest: dict) -> str:
    """The same checksum `write_latest_export_pointer` records — so import can compare."""
    import hashlib
    import json
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ── completion / resume state (§7a) ─────────────────────────────────────────

def bundle_is_complete(config, run_id: str) -> bool:
    """A bundle is complete once `manifest.json` exists (written last)."""
    return os.path.isfile(os.path.join(run_dir(config, run_id), BP.MANIFEST_JSON))


def bundle_has_checkpoint(config, run_id: str) -> bool:
    return os.path.isfile(os.path.join(run_dir(config, run_id), BP.CHECKPOINT_JSON))


def list_run_ids(config) -> list[str]:
    """All run_id subdirs under the wsmig root, newest-first (lexicographic desc)."""
    root = wsmig_root(config)
    if not os.path.isdir(root):
        return []
    ids = [name for name in os.listdir(root)
           if os.path.isdir(os.path.join(root, name))]
    return sorted(ids, reverse=True)


def find_latest_incomplete_run(config) -> Optional[str]:
    """Newest run dir that has started but not finished (checkpoint present, no manifest).

    This is the run a whole-job re-run should RESUME (§7a). A run dir with neither a checkpoint
    nor a manifest is an empty/aborted-before-first-write shell → ignored (nothing to resume).
    """
    for rid in list_run_ids(config):
        if not bundle_is_complete(config, rid) and bundle_has_checkpoint(config, rid):
            return rid
    return None


def has_inventory(config, run_id: str) -> bool:
    """Whether `inventory.json` exists for this run (Export's required input)."""
    return os.path.isfile(os.path.join(run_dir(config, run_id), BP.INVENTORY_JSON))


# ── the resolvers (§2b, §7a) ────────────────────────────────────────────────

def resolve_inventory_run_id(config, explicit_run_id: str, force_full: bool) -> tuple[str, str]:
    """Decide the run_id `01_Inventory` writes into, returning (run_id, how).

    Precedence (§7a):
      1. explicit widget/task-value run_id      → ("<id>", "widget")
      2. else, unless force_full: newest INCOMPLETE bundle → resume it → ("<id>", "resume")
      3. else: a fresh snapshot (the caller's already-minted run_id) → ("<id>", "fresh")

    `explicit_run_id` is the raw widget value ("" if blank). The caller passes
    `config.run_id` already defaulted to now_compact() for the fresh case.
    """
    explicit = (explicit_run_id or "").strip()
    if explicit:
        return explicit, "widget"
    if not force_full:
        incomplete = find_latest_incomplete_run(config)
        if incomplete:
            return incomplete, "resume"
    return config.run_id, "fresh"


def resolve_export_run_id(config, explicit_run_id: str, force_full: bool) -> tuple[str, str]:
    """Decide which run `02_Export` acts on, returning (run_id, how). Raises if none resolvable.

    Precedence (§2b + §7a):
      1. explicit run_id (widget, or task-value the notebook copied in) → ("<id>", "widget")
      2. else, unless force_full: newest INCOMPLETE bundle             → ("<id>", "resume")
      3. else: LATEST_INVENTORY.json pointer                            → ("<id>", "pointer")
      4. else: fail loudly — never invent a run_id (would export an empty bundle)
    """
    explicit = (explicit_run_id or "").strip()
    if explicit:
        return explicit, "widget"
    if not force_full:
        incomplete = find_latest_incomplete_run(config)
        if incomplete:
            return incomplete, "resume"
    pointer = read_latest_pointer(config)
    if pointer and pointer.get("run_id"):
        return str(pointer["run_id"]), "pointer"
    raise RuntimeError(
        "Cannot resolve run_id for Export: no run_id widget, no incomplete bundle, and no "
        f"LATEST_INVENTORY.json under {wsmig_root(config)}. Run 01_Inventory first, or pass "
        "an explicit run_id.")
