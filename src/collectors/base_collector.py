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

from src.utils.logger import get_logger


class BaseCollector(ABC):
    object_type: str = "unknown"
    # Which field on each raw object is its stable natural key (name/path/appId).
    natural_key_field: str = "name"
    # map_parallel emits progress lines only when a pass has at least this many items (keeps the
    # common small-collector case quiet; big passes like the 10K-object ACL fetch get ~20 ticks).
    _PROGRESS_MIN: int = 50

    def __init__(self, client, config, dbutils=None) -> None:
        self.client = client   # auth.ApiClient bound to THIS (source) workspace
        self.config = config
        self.dbutils = dbutils
        self.log = get_logger(self.__class__.__name__)
        self._objects: list[dict] = []
        self._elapsed: float = 0.0
        self._errors: list[str] = []
        # B6 Scope 1: bounded thread-pool size for per-object enrichment (esp. the ~852K-call ACL
        # fetch at customer scale — the ~11h hotspot). 1 = today's exact serial behaviour (the safe
        # default / kill-switch). The per-call 429/5xx backoff is inherited unchanged.
        self.parallel_threads = max(1, int(getattr(config, "parallel_threads", 1) or 1))

    def map_parallel(self, items, fn) -> None:
        """Run `fn(item)` over `items` on the bounded pool (B6 Scope 1), fail-soft per item.

        `fn` MUST mutate the item in place (or a shared, lock-guarded structure) — results are
        discarded here, so order never matters and the bundle stays identical regardless of
        completion order. A per-item failure is logged (never raised). `parallel_threads=1` runs
        serially (byte-identical to the old loop).

        Emits PROGRESS lines so a long enrichment pass — the per-object ACL/permission GETs that
        dominate inventory at scale (10K+ objects, the ~852K-call customer hotspot) — is
        monitorable instead of silent: a start line, ~20 "N/total" ticks, and a done line. Quiet
        for small sets (< _PROGRESS_MIN items) so the common case stays uncluttered."""
        from src.exporters.parallel import parallel_map
        items = list(items)
        total = len(items)
        if total == 0:
            return
        noisy = total >= self._PROGRESS_MIN
        step = max(1, total // 20)   # ~20 progress ticks over the whole pass
        if noisy:
            self.log.info("enriching", object_type=self.object_type, total=total,
                          threads=self.parallel_threads)
        done = failed = 0
        for _item, _result, error in parallel_map(items, fn, self.parallel_threads):
            done += 1
            if error is not None:
                failed += 1
                self.log.warning("parallel enrichment failed", object_type=self.object_type,
                                 error=str(error)[:200])
            if noisy and (done % step == 0 or done == total):
                self.log.info("enrich progress", object_type=self.object_type,
                              done=done, total=total, failed=failed)
        if noisy:
            self.log.info("enriched", object_type=self.object_type, total=total, failed=failed)

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
        t0 = time.time()
        # The client's `warnings` list is SHARED across all collectors; snapshot its length so
        # this collector only attributes warnings raised DURING its own run (else one truncation
        # warning gets duplicated onto every collector's stats).
        warn_start = len(getattr(self.client, "warnings", []))
        try:
            raw = self.discover()
            self.log.info("discovered", object_type=self.object_type, count=len(raw))
            enriched = self.enrich(raw)
            self.validate(enriched)
            self._objects = self._tag_natural_keys(enriched)
        except Exception as exc:  # noqa: BLE001 — a collector must never abort the pipeline
            self.log.error("collector failed", object_type=self.object_type, error=str(exc))
            self._errors.append(f"{self.object_type}: {exc}")
            self._objects = []
        finally:
            self._elapsed = time.time() - t0
        # Surface only the client-side warnings raised during THIS collector's run.
        for w in getattr(self.client, "warnings", [])[warn_start:]:
            if w not in self._errors:
                self._errors.append(f"INCOMPLETE — {w}")
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
        try:
            data = self.client.get(f"api/2.0/permissions/{object_type}/{object_id}")
            return data.get("access_control_list") if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001
            self.log.warning("acl fetch failed", type=object_type, id=object_id, error=str(exc))
            return None
