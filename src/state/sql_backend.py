"""
sql_backend — the two ways this tool runs SQL, behind one interface.

In production the target-side notebooks have a `spark` session, so the state store just calls
`spark.sql`. But the LIVE TEST HARNESS runs from a laptop, where there is no Spark and
`/Volumes` isn't FUSE-mounted — and a state store that could only be exercised inside a notebook
would be the least-tested, highest-consequence component in the tool (losing the source→target id
map is its worst failure mode). So the backend is pluggable: the same `StateStore` code is driven
either by Spark or by the **SQL Statement Execution API** against a serverless warehouse.

Both backends must behave identically for the store's needs: DDL, MERGE, INSERT, and SELECT
returning plain Python rows. Nothing here is Delta-specific beyond the SQL the caller writes.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from src.utils.logger import get_logger

_LOG = get_logger("sql_backend")


class SqlBackend:
    """Interface: run a statement, optionally returning rows as list[dict]."""

    def sql(self, statement: str) -> list[dict]:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return type(self).__name__


class SparkSqlBackend(SqlBackend):
    """`spark.sql` — the production path inside a Databricks notebook."""

    def __init__(self, spark) -> None:
        self._spark = spark

    def sql(self, statement: str) -> list[dict]:
        df = self._spark.sql(statement)
        # A DDL/DML statement returns a df with no useful rows; collecting it is cheap and
        # uniform, and lets MERGE/INSERT metrics come back the same shape as a SELECT.
        try:
            return [row.asDict() for row in df.collect()]
        except Exception:  # noqa: BLE001 — some DDL returns nothing collectable
            return []


class StatementApiBackend(SqlBackend):
    """SQL Statement Execution API against a serverless warehouse — the test/off-cluster path.

    Polls to completion (the API returns PENDING/RUNNING for anything non-trivial) and maps the
    column-oriented response into list[dict] so callers can't tell which backend they got.
    """

    def __init__(self, client, warehouse_id: str, timeout_s: int = 300) -> None:
        self._client = client
        self._warehouse_id = warehouse_id
        self._timeout_s = timeout_s

    def sql(self, statement: str) -> list[dict]:
        doc = self._client.post("api/2.0/sql/statements", {
            "warehouse_id": self._warehouse_id,
            "statement": statement,
            "wait_timeout": "30s",
            # INLINE+JSON_ARRAY keeps small result sets in the response body (the state table is
            # thousands of rows, so no external links to fetch).
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
        })
        deadline = time.time() + self._timeout_s
        while (doc.get("status", {}) or {}).get("state") in ("PENDING", "RUNNING"):
            if time.time() > deadline:
                raise RuntimeError(f"SQL statement timed out after {self._timeout_s}s: "
                                   f"{statement[:200]}")
            time.sleep(1.5)
            doc = self._client.get(f"api/2.0/sql/statements/{doc['statement_id']}")

        status = doc.get("status", {}) or {}
        state = status.get("state")
        if state != "SUCCEEDED":
            err = (status.get("error") or {}).get("message", "")
            raise RuntimeError(f"SQL {state}: {err[:500]} :: {statement[:300]}")

        manifest = doc.get("manifest") or {}
        cols = [c["name"] for c in ((manifest.get("schema") or {}).get("columns") or [])]
        rows = ((doc.get("result") or {}).get("data_array")) or []
        return [dict(zip(cols, r)) for r in rows]


def build_sql_backend(config, spark=None, client=None) -> Optional[SqlBackend]:
    """Pick a backend: Spark when a session exists, else the Statement API if a warehouse is set.

    Returns None when neither is available, which the caller treats as "state store disabled"
    (a valid state only for a `dry_run` rehearsal with no catalog configured — `Config.validate()`
    already refuses `dry_run=false` without a state catalog).
    """
    if spark is not None:
        return SparkSqlBackend(spark)
    warehouse = getattr(config.imports, "state_warehouse_id", "")
    if client is not None and warehouse:
        return StatementApiBackend(client, warehouse)
    return None
