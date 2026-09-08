"""
parallel — a small bounded-`ThreadPoolExecutor` map, fail-soft per task (Plan 2 §7c).

The content-fetch pass (one `workspace/export` GET per notebook/file) is Export's slowest step
and the only part worth parallelizing (everything else is in-memory transform). The codebase has
NO concurrency otherwise, so this is a single, contained, tested primitive.

Contract:
  parallel_map(items, fn, max_workers) YIELDS (item, result, error) in COMPLETION order, where
  exactly one of result/error is set per item. `fn(item)` runs on a worker thread; any exception
  it raises is CAPTURED into `error` (never propagated) so one bad item can't kill the pool or
  the run — the caller records it as that unit's failure.

  It is a GENERATOR, deliberately: it used to build the whole list and return it only once the
  pool had drained, which left the caller no point mid-pass at which to persist progress. The
  content pass needs one (checkpoint every N files, so a crash at file 4,800 of 5,000 doesn't
  re-download all 5,000). Callers wanting the old eager behaviour wrap it in `list(...)`.

This is a SHARED, reusable helper (not export-specific): the same "N independent GETs" shape
appears in inventory's per-object enrichment — flagged as a Plan 1 follow-up (Plan 2 §7c note),
NOT wired here.

Thread-safety note: `fn` must be safe to call concurrently. The ApiClient wraps a
`requests.Session` (connection-pooled — concurrent GETs are safe) and retry/429-backoff wraps
every call. Any SHARED mutable state a callback touches (index list, checkpoint, oversize list)
must be guarded by the caller; `Locked` below is the tiny helper the runner uses for that.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, Iterator


def parallel_map(items: Iterable[Any], fn: Callable[[Any], Any],
                 max_workers: int = 8) -> Iterator[tuple[Any, Any, Exception | None]]:
    """Run `fn` over `items` on a bounded thread pool, YIELDING (item, result, error) as each
    completes.

    • `max_workers` is clamped to ≥1. With ≤1 worker or ≤1 item we run SERIALLY (no pool
      overhead, deterministic, and easy to debug) — behaviour is otherwise identical.
    • Results come back in completion order; callers key by natural_key, never by position.
    • Fail-soft: a raised exception becomes the tuple's `error`; other tasks keep running.
    • Being a generator, work starts on FIRST ITERATION, not at call time. All submitted tasks
      still run to completion even if the consumer stops early — the pool is drained by the
      `with` block on generator close, so no worker is orphaned mid-write.
    """
    items = list(items)
    workers = max(1, int(max_workers or 1))

    if workers == 1 or len(items) <= 1:
        for it in items:
            try:
                yield (it, fn(it), None)
            except Exception as exc:  # noqa: BLE001 — captured, never propagated (fail-soft)
                yield (it, None, exc)
        return

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, it): it for it in items}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                yield (it, fut.result(), None)
            except Exception as exc:  # noqa: BLE001
                yield (it, None, exc)


class Locked:
    """A mutex-guarded box for shared mutable state updated from worker threads.

    Usage:
        state = Locked({"units": [], "oversize": []})
        with state as s:      # acquires the lock, yields the wrapped value
            s["units"].append(...)
    """

    def __init__(self, value: Any) -> None:
        self._value = value
        self._lock = threading.Lock()

    def __enter__(self) -> Any:
        self._lock.acquire()
        return self._value

    def __exit__(self, *exc) -> None:
        self._lock.release()

    @property
    def value(self) -> Any:
        """Direct access WITHOUT the lock — only safe after all workers have joined."""
        return self._value
