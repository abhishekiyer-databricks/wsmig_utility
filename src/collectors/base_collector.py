"""
BaseCollector — abstract interface all source-reading collectors implement.

Mirrors uc-inventory-migration's BaseCollector: discover → enrich → validate → run, with
per-collector stats. A collector failure must NEVER stop the pipeline — it is caught,
recorded in stats, and the run continues (the `_safe` behaviour from the inventory script).

Every collected object must carry a stable `natural_key` (master §9) so later Export can
fingerprint it and Import can upsert. `set_natural_key()` is a helper subclasses call.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Optional

from src.utils.logger import fmt_elapsed, get_logger


class BaseCollector(ABC):
    object_type: str = "unknown"
    # Which field on each raw object is its stable natural key (name/path/appId).
    natural_key_field: str = "name"

    def __init__(self, client, config, dbutils=None) -> None:
        self.client = client   # auth.ApiClient bound to THIS (source) workspace
        self.config = config
        self.dbutils = dbutils
        self.log = get_logger(self.__class__.__name__)
        self._objects: list[dict] = []
        self._elapsed: float = 0.0
        self._errors: list[str] = []
        self._acl_fetches = 0          # per-object ACL GETs this run → progress lines
        self._t0 = time.time()

    # ── abstract interface ────────────────────────────────────────────────
    @abstractmethod
    def discover(self) -> list[dict]:
        """List raw objects from the source workspace via REST."""

    def enrich(self, objects: list[dict]) -> list[dict]:
        """Fetch per-object detail / ACLs / entitlements as needed. Default: no-op."""
        return objects

    def validate(self, objects: list[dict]) -> bool:
        """Optional sanity check; default accepts anything."""
        return True

    # ── natural key ───────────────────────────────────────────────────────
    def natural_key(self, obj: dict) -> str:
        """Stable identity of an object across runs/workspaces (master §9)."""
        return str(obj.get(self.natural_key_field, "") or "")

    def _tag_natural_keys(self, objects: list[dict]) -> list[dict]:
        for o in objects:
            if isinstance(o, dict) and "natural_key" not in o:
                o["natural_key"] = self.natural_key(o)
        return objects

    # ── pipeline runner (never raises) ────────────────────────────────────
    def run(self) -> list[dict]:
        """discover → enrich → validate → tag natural keys. Records errors; never raises."""
        t0 = self._t0 = time.time()
        self._acl_fetches = 0
        # The client's `warnings` list is SHARED across all collectors; snapshot its length so
        # this collector only attributes warnings raised DURING its own run (else one truncation
        # warning gets duplicated onto every collector's stats).
        warn_start = len(getattr(self.client, "warnings", []))
        # START line before the work: a long discovery (the workspace walk) must never be silent.
        self.log.info(f"Phase: collect {self.object_type}")
        try:
            raw = self.discover()
            self.log.info(f"collect {self.object_type}: discovered {len(raw):,}",
                          acl_fetches=self._acl_fetches)
            self.log.debug(f"enriching {self.object_type}", count=len(raw))
            enriched = self.enrich(raw)
            self.validate(enriched)
            self._objects = self._tag_natural_keys(enriched)
            self.log.debug(f"enriched {self.object_type}", count=len(self._objects))
        except Exception as exc:  # noqa: BLE001 — a collector must never abort the pipeline
            self.log.error(f"collector failed: {self.object_type}: {exc}", exc_info=True)
            self._errors.append(f"{self.object_type}: {exc}")
            self._objects = []
        finally:
            self._elapsed = time.time() - t0
        # Surface only the client-side warnings raised during THIS collector's run.
        for w in getattr(self.client, "warnings", [])[warn_start:]:
            if w not in self._errors:
                self._errors.append(f"INCOMPLETE — {w}")
        self.log.info(f"Phase complete: collect {self.object_type} — {len(self._objects):,} "
                      f"objects, {len(self._errors)} errors ({fmt_elapsed(self._elapsed)})")
        return self._objects

    @property
    def objects(self) -> list[dict]:
        return self._objects

    def stats(self) -> dict:
        """Per-collector summary for the run report."""
        return {
            "object_type": self.object_type,
            "count": len(self._objects),
            "elapsed_sec": round(self._elapsed, 3),
            "errors": list(self._errors),
        }

    # ── shared enrichment ─────────────────────────────────────────────────
    def fetch_acl(self, object_type: str, object_id: str) -> Optional[dict]:
        """Fetch object permissions (ACLs) via /api/2.0/permissions/<type>/<id>.

        Best-effort: returns the access_control_list or None; never raises (a missing/failed
        ACL fetch must not abort the collector). `object_type` is the permissions API's type
        segment, e.g. 'clusters', 'jobs', 'instance-pools', 'cluster-policies', 'sql/warehouses',
        'pipelines', 'notebooks', 'directories', 'repos', 'serving-endpoints'.
        """
        if not object_id:
            return None
        self._acl_fetches += 1
        self.log.debug(f"fetching ACL {object_type} {object_id}")
        try:
            data = self.client.get(f"api/2.0/permissions/{object_type}/{object_id}")
            acl = data.get("access_control_list") if isinstance(data, dict) else None
            self.log.debug(f"ACL {object_type} {object_id} → {len(acl or [])} grants")
            return acl
        except Exception as exc:  # noqa: BLE001 — degraded: the object is kept without its ACL
            self.log.warning(f"ACL {object_type} {object_id} → FAILED (kept without ACL): {exc}")
            return None
        finally:
            # Total is unknown mid-discovery, so a line every PROGRESS_EVERY fetches.
            self.log.progress(f"collect {self.object_type}: ACLs fetched", self._acl_fetches,
                              started=self._t0)

    def _detail_get(self, what: str, ident: str, path: str, params: Optional[dict] = None) -> dict:
        """One GET-by-id enrichment with a start + end line (DEBUG) and a WARNING on failure.

        Returns {} on failure — the object is kept with its list-call fields only (a degraded but
        visible result; the collector carries on)."""
        self.log.debug(f"fetching {what} {ident}")
        try:
            data = self.client.get(path, params=params) if params else self.client.get(path)
            self.log.debug(f"fetched {what} {ident}")
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001 — degraded: list-call fields only
            self.log.warning(f"{what} {ident} → FAILED (kept with list fields only): {exc}")
            return {}
