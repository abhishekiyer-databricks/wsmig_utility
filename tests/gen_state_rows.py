"""Synthetic migration-state rows for the PLAN 16.1 live load test (plan §7 / §8.7).

The fail-loud drill needs a state table holding tens of thousands of rows for ONE source workspace,
so the count-checked `StateStore.load()` can be proven to load ALL of them (and log
`state loaded | rows=N expected=N`). This builds MERGE-ready rows in the exact column shape the store
writes, plus batched INSERT statements for either SQL backend.

Use from a notebook on the target (classic cluster):

    from tests.gen_state_rows import generate_rows, insert_statements
    rows = generate_rows(25_000, source_workspace_id="<a TEST id, never a real pair's>")
    for stmt in insert_statements(rows, "<catalog>.<schema>.wsmig_migration_state"):
        spark.sql(stmt)

Use a dedicated `source_workspace_id` (the table is shared by every pair, keyed by that column), and
clean up with `DELETE FROM <table> WHERE source_workspace_id = '<that id>'`.
"""
from __future__ import annotations

from src.state.state_store import StateStore

# A realistic spread of asset types (roughly the shape of a large customer workspace).
_MIX = (("notebook", 0.55), ("workspace_file", 0.15), ("directory", 0.12), ("acl", 0.10),
        ("job", 0.04), ("cluster", 0.02), ("user", 0.01), ("group", 0.01))


def generate_rows(n: int, source_workspace_id: str, run_id: str = "synthetic_load_test") -> list:
    """`n` unique state rows for one pair, every column of `StateStore._STATE_COLS` present."""
    rows: list[dict] = []
    i = 0
    for asset_type, share in _MIX:
        count = int(n * share) if asset_type != _MIX[-1][0] else n - len(rows)
        for _ in range(count):
            rows.append({
                "source_workspace_id": source_workspace_id,
                "asset_type": asset_type,
                "natural_key": f"/Synthetic/{asset_type}/{i:07d}",
                "source_object_id": f"src-{i}",
                "target_object_id": f"tgt-{i}",
                "last_source_fingerprint": f"sha256:{i:064x}"[:71],
                "last_action": "created",
                "last_error": "",
                "last_error_raw": "",
                "failure_category": "",
                "last_run_id": run_id,
                "connectivity_mode": "direct",
                "tool_version": "synthetic",
                "last_source_detail": "",
                "first_seen": "2026-10-04T00:00:00Z",
                "last_seen": "2026-10-04T00:00:00Z",
            })
            i += 1
    return rows[:n]


def insert_statements(rows: list, table_fqn: str, batch: int = 500) -> list:
    """`INSERT INTO <table> (cols) SELECT … UNION ALL …` statements, `batch` rows each."""
    cols = StateStore._STATE_COLS
    out = []
    for start in range(0, len(rows), batch):
        src = StateStore._values_clause(rows[start:start + batch], cols)
        out.append(f"INSERT INTO {table_fqn} ({', '.join(cols)}) {src}")
    return out


if __name__ == "__main__":   # pragma: no cover - documentation entry point
    print(__doc__)
