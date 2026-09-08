"""
Shared helpers.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable


def now_iso() -> str:
    """UTC ISO-8601 timestamp (e.g. 2026-07-29T10:11:12.345678Z)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_compact() -> str:
    """Compact UTC stamp for run_ids / filenames: YYYYMMDD_HHMMSS."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def safe_str(v: Any) -> str:
    """str-coerce; None -> ''."""
    return "" if v is None else str(v)


def normalize_ws_path(path: Any) -> str:
    """Normalise a workspace path for keying/matching: strip the optional `/Workspace` mount
    prefix and any trailing slash. `"/Workspace/Users/a/"` → `"/Users/a"`; `""` → `""`.

    Shared so the SOURCE-side natural_key (collector) and the TARGET-side existence match
    (importer) build the SAME string — a mismatch there is exactly what produces a duplicate."""
    p = safe_str(path)
    if p.startswith("/Workspace/"):
        p = p[len("/Workspace"):]
    return p.rstrip("/")


def folder_natural_key(parent_path: Any, name: Any) -> str:
    """The full-path natural key for a folder-placed asset (Finding-9): `<parent>/<name>`.

    Falls back to the bare `name` when `parent_path` is genuinely absent, so an object with no
    recorded folder still gets a stable key (an id-anchor keeps it unique on re-run). Distinct
    same-named objects in DIFFERENT folders get DIFFERENT keys, so they no longer collapse onto one
    target object (the silent data-loss this fixes)."""
    parent = normalize_ws_path(parent_path)
    nm = safe_str(name)
    if not parent:
        return nm
    return f"{parent}/{nm}"


def home_owner(path: Any) -> str:
    """The `<principal>` segment of a `/Users/<principal>[/...]` path (mount-prefix aware), else "".

    A service principal's home is `/Users/<applicationId>` (a bare UUID), a user's is
    `/Users/<email>`. Shared by the workspace importer AND the folder-placed importers so both
    resolve an orphaned/recreated home the same way (Finding-8)."""
    parts = [seg for seg in normalize_ws_path(path).split("/") if seg]
    return parts[1] if len(parts) >= 2 and parts[0] == "Users" else ""


def looks_like_app_id(owner: Any) -> bool:
    """A UUID-shaped owner is a service-principal home; an email is a user home."""
    o = safe_str(owner)
    return "@" not in o and o.count("-") == 4 and len(o) >= 32


# The default DAB bundle-root indicator — the CLI's standard `.bundle` folder. Used when no
# `dab_bundle_roots` config is provided, so behaviour is byte-identical to the pre-Finding-12 tool.
DEFAULT_DAB_BUNDLE_ROOTS = (".bundle",)


def _resolve_dab_roots(roots) -> tuple:
    """Normalise the matcher list, defaulting to `.bundle` when unset/empty."""
    if not roots:
        return DEFAULT_DAB_BUNDLE_ROOTS
    out = tuple(safe_str(r).strip() for r in roots if safe_str(r).strip())
    return out or DEFAULT_DAB_BUNDLE_ROOTS


def dab_path_info(path: Any, roots=None) -> dict:
    """Classify a workspace path by DAB (Databricks Asset Bundle) deployment.

    Returns {"deployed_by_dab": bool, "dab_scope": "shared"|"user"|"", "bundle_root": str}.

    A bundle-deployed asset lives under a bundle ROOT in the workspace tree. The root is a per-team
    convention (PLAN 11 Finding-12): the CLI default is a `.bundle` folder, but a team can hand
    `databricks bundle deploy` an explicit `root_path` (e.g. a dummy user's home) and then there is
    NO `.bundle` segment at all. So the roots are configurable — `roots` is a list of matchers, each
    EITHER:
      • a folder-name glob (no leading `/`, e.g. `.bundle`, `*.bundle`) — matches when a NON-LAST
        path segment matches the glob; the bundle root is the path through that segment; OR
      • an absolute directory prefix (leading `/`, e.g. `/Users/dab@corp.com/prod`) — matches when
        the path is at/under it; the bundle root IS that prefix.
    Default = `.bundle` (exact segment) → byte-for-byte the pre-Finding-12 behaviour.

    The scope is decided by the FIRST path segment (after stripping an optional leading
    `/Workspace`): "Shared" → shared; anything else (Users/<email>, a uuid) → user.
    """
    import fnmatch
    p = safe_str(path)
    if p.startswith("/Workspace/"):
        p = p[len("/Workspace"):]   # normalize the optional /Workspace mount prefix
    segments = [s for s in p.split("/") if s]
    scope_top = segments[0] if segments else ""
    scope = "shared" if scope_top == "Shared" else "user"
    for matcher in _resolve_dab_roots(roots):
        if matcher.startswith("/"):
            pref = matcher.rstrip("/")
            if p == pref or p.startswith(pref + "/"):
                psegs = [s for s in pref.split("/") if s]
                pscope = "shared" if (psegs and psegs[0] == "Shared") else "user"
                return {"deployed_by_dab": True, "dab_scope": pscope, "bundle_root": pref}
        else:
            # A folder-name glob. Require a NON-LAST match (content under the root) so a path that IS
            # the root folder itself is not classified — this preserves the old `/.bundle/`
            # (content-after) semantics exactly for the default `.bundle`.
            for i, seg in enumerate(segments[:-1]):
                if fnmatch.fnmatch(seg, matcher):
                    root = "/" + "/".join(segments[:i + 1])
                    return {"deployed_by_dab": True, "dab_scope": scope, "bundle_root": root}
    return {"deployed_by_dab": False, "dab_scope": "", "bundle_root": ""}


def is_bundle_root_path(path: Any, roots=None) -> bool:
    """Whether `path` is at/under a configured DAB bundle root (Finding-12). Used by the bundle
    state-file discovery so the authoritative pathless-asset registry finds `<root>/**/state/…`
    regardless of whether the root is `.bundle` or a plain directory."""
    return dab_path_info(path, roots)["deployed_by_dab"]


def dab_deploy_label(deployed_by_dab: Any, dab_scope: Any) -> str:
    """Human label for the 'Deployed by DAB' column: Manual / DAB (Shared) / DAB (User)."""
    if not deployed_by_dab:
        return "Manual"
    return {"shared": "DAB (Shared)", "user": "DAB (User)"}.get(safe_str(dab_scope), "DAB")


def strip_fields(d: dict, keys: Iterable[str]) -> dict:
    """Return a shallow copy of dict `d` without any key in `keys`."""
    ks = set(keys)
    return {k: v for k, v in d.items() if k not in ks}


def parse_kv_list(raw: str) -> dict:
    """Parse a widget string like 'a=b, c=d' into {'a': 'b', 'c': 'd'}.

    Blank input -> {}. Whitespace around keys/values is stripped. Entries without '=' are
    ignored. Later duplicates win.
    """
    out: dict = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def parse_csv(raw: str) -> list:
    """Parse a widget string like 'x, y ,z' into ['x', 'y', 'z']. Blank input -> []."""
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def parse_bool(raw: Any, default: bool = False) -> bool:
    """Parse a widget/string boolean ('true'/'false', case-insensitive).

    None or blank -> default (so an absent/empty widget keeps its default).
    """
    if isinstance(raw, bool):
        return raw
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("true", "1", "yes", "y")
