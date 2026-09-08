"""
content_fetcher — notebook / workspace-file CONTENT bytes (Plan 2 §5a, §8).

Inventory captured metadata only; the actual bytes are Export's job and its slowest step (one
GET per object), so this runs under the parallel pool (§7c). One `fetch(unit)` call returns a
`FetchResult` the runner folds into the unit — it NEVER raises (fail-soft): any error becomes a
`failure` result, and an oversize condition a non-alarming `skipped_oversize` result.

Route behaviour — VERIFIED LIVE on fvm1 2026-08-01:
  • `GET /api/2.0/workspace/export?path=…&direct_download=true` returns RAW bytes.
    - For a NOTEBOOK, add `format=SOURCE` (git-friendly). base64/notebook body is capped at
      ~10 MB → the API raises `MAX_NOTEBOOK_SIZE_EXCEEDED` for a larger notebook.
    - For a FILE, `direct_download=true` streams the WHOLE file with NO 10 MB cap (verified an
      11 MB CSV round-trips in one call) up to the 500 MB workspace-files ceiling.

So the ladder is deliberately simple (the earlier "tier-2 streaming rescues big notebooks"
idea was WRONG — a >10 MB notebook has NO API round-trip: base64 import rejects it and the
streaming route stores it as a plain FILE, not a notebook):
  • FILE          → single `workspace/export?direct_download=true` call (≤500 MB) → success;
                    >500 MB (Content-Length guard) → `skipped_oversize` (out-of-band copy).
  • NOTEBOOK ≤10MB → `workspace/export?format=SOURCE&direct_download=true` → success.
  • NOTEBOOK >10MB → `skipped_oversize`, **no bytes exported** (per decision): it cannot be
    recreated as a notebook via any API, so we record the skip + reason rather than emit bytes
    that import can only land as a file.

Notebooks land as SOURCE (`.py`/`.sql`/`.scala`/`.r`); files verbatim (`.bin`). Byte writes are
per-file (no cross-thread contention).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Optional

from src.auth.token_manager import DownloadHTTPError, OversizeError
from src.exporters import bundle_paths as BP
from src.utils.helpers import safe_str
from src.utils.logger import get_logger

_LOG = get_logger("content_fetcher")

# API size caps (verified live fvm1 2026-08-01; project memory export-api-size-limits).
NOTEBOOK_CAP = 10 * 1024 * 1024     # base64 notebook route hard cap (MAX_NOTEBOOK_SIZE_EXCEEDED)
FILE_CAP = 500 * 1024 * 1024        # workspace-files ceiling (raisable via account team)

# Server error substrings that mean "notebook too big for the base64 route".
_NOTEBOOK_OVERSIZE_MARKERS = ("MAX_NOTEBOOK_SIZE_EXCEEDED", "exceeded max size",
                              "notebook size", "RequestEntityTooLarge")

# Language → SOURCE file extension (notebooks). Unknown language → .py (SOURCE default).
_LANG_EXT = {"PYTHON": ".py", "SQL": ".sql", "SCALA": ".scala", "R": ".r"}


@dataclass
class FetchResult:
    status: str                          # success | skipped_oversize | failure
    content_ref: Optional[str] = None    # bundle-relative path to the written bytes
    content_route: str = ""              # direct_download
    content_kind: str = ""               # notebook | file
    size_bytes: int = 0
    note: str = ""
    oversize: dict = field(default_factory=dict)   # {path,size,type,reason,recommended} for §5a
    # sha256 of the fetched BYTES. The metadata payload of a notebook/file is only
    # {path, object_type, language}, so without this the fingerprint cannot move when a
    # notebook's CODE changes — editing a notebook on source would re-export to the same
    # fingerprint and the target-side upsert would SKIP it, leaving the OLD code on target
    # (Plan 3 §7c-audit GAP 1). The runner folds this into the unit's fingerprint after the
    # content pass. Free to compute: the bytes are already in memory.
    content_sha256: str = ""


def _is_notebook_oversize(exc: Exception) -> bool:
    if isinstance(exc, OversizeError):
        return True
    msg = str(exc)
    return any(m.lower() in msg.lower() for m in _NOTEBOOK_OVERSIZE_MARKERS)


def mangle_path(path: str, kind: str, language: str, taken: set) -> str:
    """Deterministic, collision-safe, case-insensitive-unique bundle filename for a source path.

    `/Users/a@x.com/My Notebook` (PYTHON) → `Users__a@x.com__My Notebook.py`. Slashes → `__`;
    a notebook gets its language extension, a file `.bin`. If two source paths mangle to the same
    (case-insensitively) name, a numeric suffix keeps them distinct. `taken` is the set of
    already-used lower-cased names (mutated here).
    """
    stem = safe_str(path).strip("/").replace("/", "__") or "root"
    ext = _LANG_EXT.get(safe_str(language).upper(), ".py") if kind == "notebook" else ".bin"
    candidate = f"{stem}{ext}"
    low = candidate.lower()
    if low in taken:
        i = 1
        while f"{stem}__{i}{ext}".lower() in taken:
            i += 1
        candidate = f"{stem}__{i}{ext}"
        low = candidate.lower()
    taken.add(low)
    return candidate


class ContentFetcher:
    """Fetches + writes content bytes for `notebook`/`workspace_file` units. Thread-safe:
    `fetch()` may run concurrently for different units (the ApiClient's Session is pooled; the
    only shared mutable state — the mangled-name set — is guarded by an internal lock)."""

    def __init__(self, client, artifact_writer) -> None:
        self.client = client
        self.aw = artifact_writer
        import threading
        self._name_lock = threading.Lock()
        self._taken: set = set()

    def _reserve_name(self, path: str, kind: str, language: str) -> str:
        with self._name_lock:
            return mangle_path(path, kind, language, self._taken)

    def fetch(self, unit: dict) -> FetchResult:
        """Fetch one content unit's bytes and write them under export/workspace/content/.

        Returns a FetchResult (never raises). The runner applies it to the unit.
        """
        path = safe_str(unit.get("natural_key"))       # the workspace path IS the natural key
        kind = "notebook" if unit.get("asset_type") == "notebook" else "file"
        language = safe_str((unit.get("payload") or {}).get("language"))

        params = {"path": path, "direct_download": "true"}
        cap = FILE_CAP
        if kind == "notebook":
            params["format"] = "SOURCE"
            cap = NOTEBOOK_CAP
        try:
            data = self.client.download_bytes("api/2.0/workspace/export", params=params,
                                              max_bytes=cap)
        except OversizeError as exc:
            return self._oversize(path, kind, getattr(exc, "size", 0))
        except Exception as exc:  # noqa: BLE001 — fail-soft
            if kind == "notebook" and _is_notebook_oversize(exc):
                # A >10 MB notebook has no API recreate path → skip outright, NO bytes (decision).
                return self._oversize(path, kind, 0)
            _LOG.warning("content fetch failed", path=path, error=str(exc))
            return FetchResult(status="failure", content_kind=kind, note=f"content fetch: {exc}")

        rel_name = self._reserve_name(path, kind, language)
        rel = f"{BP.EXPORT_CONTENT_DIR}/{rel_name}"
        try:
            self.aw.write_bytes(rel, data)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("content write failed", path=path, error=str(exc))
            return FetchResult(status="failure", content_kind=kind, note=f"content write: {exc}")
        return FetchResult(status="success", content_ref=rel, content_route="direct_download",
                          content_kind=kind, size_bytes=len(data),
                          content_sha256=hashlib.sha256(data).hexdigest())

    def _oversize(self, path: str, kind: str, size: int) -> FetchResult:
        if kind == "notebook":
            reason = (f"notebook exceeds the {NOTEBOOK_CAP // (1024*1024)} MB base64 API limit — "
                      "no API recreates a >10 MB notebook (streaming lands it as a plain file), "
                      "so it is skipped, not exported")
            recommended = ("split the notebook, or recreate manually on target; a >10 MB notebook "
                           "cannot be migrated as a notebook via the workspace API")
        else:
            reason = f"file exceeds the {FILE_CAP // (1024*1024)} MB workspace-files API limit"
            recommended = "copy via UC Volume / cloud object storage (out-of-band)"
        _LOG.warning("content skipped (oversize)", path=path, size=size, kind=kind)
        return FetchResult(
            status="skipped_oversize", content_kind=kind, size_bytes=size,
            note=reason,
            oversize={"path": path, "size": size, "type": kind, "reason": reason,
                      "recommended": recommended})
