"""
HTML report generator.

`render_inventory` produces the SAME clickable single-page inventory app the customer's
existing script (`workspace_inventory_nb.ipynb`) emits — sidebar nav, summary cards, and
searchable / sortable / paginated per-asset detail tables — so the output matches what the
customer is already used to. The rendering layer (icons, labels, columns, cell formatters,
CSS, JS) is a faithful port of that script; asset metadata lives in `inventory_view.py`.

Differences from the reference (deliberate, see CLAUDE.md / PLAN_0 §6a):
  • UC / MLflow cards are omitted (out of scope for this migration utility).
  • An extra "Identity classification" summary section is added (this utility's core
    enhancement) — it does not disturb the reference look.

Other reports (later plans):
  transform diff (03), import results (04), validation (05).
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlparse

from src.reports.inventory_view import (
    _COLUMNS,
    _ICONS,
    _LABELS,
    _SUMMARY_CARD_KEYS,
    _deep_get,
    _esc,
    _resolve_items,
    adapt,
    build_counts,
)


# ---------------------------------------------------------------------------
# Cell rendering (HTML) — ported verbatim from the reference script.
# ---------------------------------------------------------------------------

def _cell_html(value, fmt: str) -> str:
    from datetime import datetime

    # Tri-state columns are checked BEFORE the empty guard: for them `None` is meaningful
    # ("could not check"), not missing, so it must not fall through to the "—" placeholder.
    if fmt == "badge_bool_unknown":
        if value is True or str(value).lower() in ("true", "1", "yes"):
            return '<span class="badge badge-green">Yes</span>'
        if value is False or str(value).lower() in ("false", "0", "no"):
            return '<span class="badge badge-red">No</span>'
        return ('<span class="badge badge-yellow" title="insufficient privilege to check — '
                'needs account_admin">Could not check</span>')

    if value is None or value == "":
        return '<span class="na">—</span>'

    if fmt == "plain":
        return _esc(str(value))
    if fmt == "mono":
        return f'<code class="mono">{_esc(str(value))}</code>'
    if fmt == "short_mono":
        return f'<code class="mono">{_esc(str(value)[:8])}</code>'
    if fmt == "path":
        return f'<span class="path">{_esc(str(value))}</span>'
    if fmt == "trunc":
        s = str(value)
        if len(s) > 80:
            s = s[:77] + "…"
        return _esc(s)
    if fmt == "badge_bool":
        if value is True or str(value).lower() in ("true", "1", "yes"):
            return '<span class="badge badge-green">Yes</span>'
        return '<span class="badge badge-red">No</span>'
    if fmt == "badge_state":
        state = str(value).upper()
        color = {
            "RUNNING": "green", "ACTIVE": "green", "PUBLISHED": "green",
            "STARTED": "green", "READY": "green", "SUCCEEDED": "green", "ONLINE": "green",
            "STOPPED": "gray", "TERMINATED": "gray", "IDLE": "gray", "OFFLINE": "gray",
            "STARTING": "blue", "PENDING": "blue", "RESIZING": "blue", "PROVISIONING": "blue",
            "FAILED": "red", "ERROR": "red", "DELETED": "red", "CRASHED": "red",
            "DRAFT": "yellow", "DEPLOYING": "yellow", "INITIALIZING": "yellow",
        }.get(state, "gray")
        return f'<span class="badge badge-{color}">{_esc(state)}</span>'
    if fmt == "badge_type":
        return f'<span class="badge badge-blue">{_esc(str(value))}</span>'
    if fmt == "badge_managed":
        # "Databricks-managed" (recreated on target) → purple; account/Entra/built-in → green.
        s = str(value)
        color = "purple" if "Databricks" in s else ("green" if s else "gray")
        return f'<span class="badge badge-{color}">{_esc(s)}</span>' if s else '<span class="na">—</span>'
    if fmt == "cls_managed":
        # Map a raw kind/classification to a friendly "Managed By" badge. The label map is shared
        # with the Excel formatter so the two can't drift (they used to keep separate copies, and
        # both knew only the pre-Plan-6 vocabulary).
        from src.reports.inventory_view import managed_by_label
        label = managed_by_label(value)
        color = ("purple" if "Databricks" in label else
                 "yellow" if label == "Needs review" else
                 "green" if label else "gray")
        return f'<span class="badge badge-{color}">{_esc(label)}</span>' if label else '<span class="na">—</span>'
    if fmt == "badge_lang":
        lang = str(value).upper() if value else ""
        color = {"PYTHON": "blue", "SCALA": "red", "SQL": "green",
                 "R": "yellow", "AUTO": "gray"}.get(lang, "gray")
        return (f'<span class="badge badge-{color}">{_esc(lang or "—")}</span>'
                if lang else '<span class="na">—</span>')
    if fmt == "count":
        n = len(value) if isinstance(value, list) else 0
        return f'<span class="count">{n}</span>'
    if fmt == "first_email":
        if isinstance(value, list) and value:
            return _esc(value[0].get("value", ""))
        return '<span class="na">—</span>'
    if fmt == "list_vals":
        if isinstance(value, list) and value:
            vals = ", ".join(str(v.get("value", v)) for v in value[:3])
            if len(value) > 3:
                vals += f" (+{len(value)-3})"
            return _esc(vals)
        return '<span class="na">—</span>'
    if fmt == "schedule":
        if isinstance(value, dict):
            cron = value.get("quartz_cron_expression", "")
            tz = value.get("timezone_id", "")
            if cron:
                return f'<span class="schedule">{_esc(cron)}<br><small>{_esc(tz)}</small></span>'
        return '<span class="na">Manual</span>'
    if fmt == "epoch_ms":
        try:
            return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return _esc(str(value))
    if fmt == "iso_ts":
        try:
            return _esc(str(value)[:16].replace("T", " "))
        except Exception:
            return _esc(str(value))
    if fmt == "url_link":
        url = str(value)
        display = url.replace("https://", "").replace("http://", "")
        if len(display) > 50:
            display = display[:47] + "…"
        return f'<a href="{_esc(url)}" target="_blank">{_esc(display)}</a>'
    if fmt == "kv_dns":
        if isinstance(value, dict):
            return _esc(value.get("dns_name", ""))
        return '<span class="na">—</span>'
    return _esc(str(value))


# ---------------------------------------------------------------------------
# Identity classification badge (this utility's enhancement).
# ---------------------------------------------------------------------------

# What the utility DOES for each kind. Shown next to the badge because the kind alone doesn't tell
# an operator whether they have work to do — and only ONE row here ever means they might.
_KIND_ACTION = {
    "account": "Adopted / assigned automatically (users and SPNs keep their key; account groups "
               "must already exist in the target account)",
    "workspace_local": "Recreated on the target, with members and entitlements",
    "system": "Already exists on the target; membership is re-applied",
    "needs_review": "Kind could not be determined — confirm before importing",
    # legacy vocabulary, so an older report still renders
    "entra_user": "Adopted / assigned automatically",
    "umi_or_entra_sp": "Adopted / assigned automatically (applicationId preserved)",
    "db_managed_sp": "Adopted / assigned automatically (applicationId preserved)",
    "account_group": "Assigned to the workspace; must already exist in the target account",
    "db_managed_group": "Recreated on the target, with members and entitlements",
    "builtin_group": "Already exists on the target; membership is re-applied",
}


def _cls_badge(name: str) -> str:
    """A readable "Managed By"-style badge for an identity kind.

    Renders the friendly label, never the raw internal value: the summary keys are `kind` values
    like `account`/`workspace_local`, which mean nothing to a reader. Green = the utility does it
    unattended; blue = it recreates the object; amber = a human must look.
    """
    from src.reports.inventory_view import managed_by_label
    if name == "needs_review":
        color = "yellow"
    elif name in ("workspace_local", "db_managed_group", "db_managed_sp"):
        color = "blue"     # recreated on target
    else:
        color = "green"    # adopted/assigned, or already present
    return f'<span class="badge badge-{color}">{_esc(managed_by_label(name))}</span>'


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_inventory(
    counts: dict = None,
    collector_stats: list = None,
    identity_summary: dict = None,
    warnings: list = None,
    workspace_url: str = "",
    generated_at: str = "",
    output_path: Optional[str] = None,
    objects_by_type: dict = None,
) -> str:
    """Build the clickable inventory HTML app (reference-style).

    `objects_by_type` is our collectors' output ({object_type: [records...]}); it is adapted
    to the reference renderer's per-asset shape internally. The legacy `counts` arg is now
    derived from the data, so passing it is optional (kept for signature compatibility).
    """
    data = adapt(objects_by_type or {})
    counts = build_counts(data)
    identity_summary = identity_summary or {}
    warnings = warnings or []
    hostname = urlparse(workspace_url).hostname or workspace_url or "workspace"

    summary_card_keys = _SUMMARY_CARD_KEYS

    # ── Summary cards ────────────────────────────────────────────────────
    def _card(key: str, count: int) -> str:
        icon, color = _ICONS.get(key, ("📦", "#6b7280"))
        label = _LABELS.get(key, key.replace("_", " ").title())
        return f"""
        <div class="card" onclick="showTab('{key}')" data-tab="{key}">
          <div class="card-icon" style="background:{color}18;color:{color}">{icon}</div>
          <div class="card-body">
            <div class="card-count" style="color:{color}">{count}</div>
            <div class="card-label">{label}</div>
          </div>
        </div>"""

    cards_html = "".join(_card(k, counts.get(k, 0)) for k in summary_card_keys)

    # ── Nav tabs ─────────────────────────────────────────────────────────
    def _nav_item(key: str, count: int) -> str:
        icon, color = _ICONS.get(key, ("📦", "#6b7280"))
        label = _LABELS.get(key, key.replace("_", " ").title())
        return f"""<li class="nav-item" id="nav-{key}" onclick="showTab('{key}')">
          <span class="nav-icon">{icon}</span>
          <span class="nav-label">{label}</span>
          <span class="nav-badge" style="background:{color}">{count}</span>
        </li>"""

    nav_html = ('<li class="nav-item active" id="nav-summary" onclick="showTab(\'summary\')">'
                '<span class="nav-icon">🏠</span><span class="nav-label">Summary</span></li>')
    for k in summary_card_keys:
        nav_html += _nav_item(k, counts.get(k, 0))

    # ── Detail panels ────────────────────────────────────────────────────
    def _detail_panel(key: str, items: List[dict], cols: List[tuple]) -> str:
        icon, color = _ICONS.get(key, ("📦", "#6b7280"))
        label = _LABELS.get(key, key.replace("_", " ").title())
        n = len(items)
        th = "".join(
            f"<th onclick=\"sortTable('{key}',{i})\">{c[1]} <span class='sort-icon'>↕</span></th>"
            for i, c in enumerate(cols))
        rows = []
        for idx, item in enumerate(items):
            tds = "".join(f"<td>{_cell_html(_deep_get(item, col[0]), col[2])}</td>" for col in cols)
            rows.append(f'<tr data-idx="{idx}">{tds}</tr>')
        tbody = "\n".join(rows) if rows else \
            f'<tr><td colspan="{len(cols)}" class="empty-row">No {label.lower()} found</td></tr>'
        return f"""
  <div class="panel" id="panel-{key}">
    <div class="panel-header" style="border-left:4px solid {color}">
      <span class="panel-icon">{icon}</span>
      <h2 class="panel-title">{label}</h2>
      <span class="panel-count" style="background:{color}18;color:{color}">{n} item{'s' if n!=1 else ''}</span>
      <div class="panel-controls">
        <div class="panel-search">
          <span class="search-icon">🔍</span>
          <input type="text" id="search-{key}" placeholder="Search {label.lower()}…"
                 oninput="onSearch('{key}', this.value)">
        </div>
        <div class="page-size-wrap">
          <label for="ps-{key}">Rows per page</label>
          <input type="number" id="ps-{key}" class="page-size-input" value="25" min="1" max="1000"
                 onchange="onPageSizeChange('{key}', this.value)">
        </div>
      </div>
    </div>
    <div class="table-wrap">
      <table id="table-{key}">
        <thead><tr>{th}</tr></thead>
        <tbody id="tbody-{key}">{tbody}</tbody>
      </table>
    </div>
    <div class="pagination-bar" id="pager-{key}">
      <span class="pager-info" id="pager-info-{key}"></span>
      <div class="pager-buttons">
        <button class="pager-btn" id="btn-first-{key}"  onclick="goPage('{key}','first')"  title="First page">«</button>
        <button class="pager-btn" id="btn-prev-{key}"   onclick="goPage('{key}','prev')"   title="Previous page">‹</button>
        <span class="pager-pages" id="pager-pages-{key}"></span>
        <button class="pager-btn" id="btn-next-{key}"   onclick="goPage('{key}','next')"   title="Next page">›</button>
        <button class="pager-btn" id="btn-last-{key}"   onclick="goPage('{key}','last')"   title="Last page">»</button>
      </div>
    </div>
  </div>"""

    panels_html = ""
    for key in summary_card_keys:
        items = _resolve_items(data, key)
        cols = _COLUMNS.get(key, [("path", "Path", "plain")])
        panels_html += _detail_panel(key, items, cols)

    # ── Identity classification section (our enhancement) ────────────────
    if identity_summary:
        id_rows = "".join(
            f"<tr><td>{_cls_badge(k)}</td><td><span class='count'>{v}</span></td>"
            f"<td>{_KIND_ACTION.get(k, '')}</td></tr>"
            for k, v in sorted(identity_summary.items()))
        classification_html = f"""
      <div class="classification-box">
        <h3>Identity classification</h3>
        <p>How each source identity will be treated on the target. Users and service principals are
        handled automatically; only an <b>account group missing from the target account</b> needs a
        human (an account admin or Entra SCIM).</p>
        <table class="cls-table"><thead><tr><th>Managed By</th><th>Count</th>
        <th>What the utility does</th></tr></thead>
        <tbody>{id_rows}</tbody></table>
      </div>"""
    else:
        classification_html = ""

    # ── Errors / warnings section ────────────────────────────────────────
    errors_html = ""
    if warnings:
        items_html = "".join(f"<li>{_esc(e)}</li>" for e in warnings)
        errors_html = f'<div class="errors-box"><strong>⚠ Fetch warnings:</strong><ul>{items_html}</ul></div>'

    total_items = sum(counts.values())

    doc = _PAGE.format(
        hostname=_esc(hostname),
        generated_at=_esc(generated_at),
        total_items=total_items,
        n_components=len(summary_card_keys),
        nav_html=nav_html,
        cards_html=cards_html,
        classification_html=classification_html,
        errors_html=errors_html,
        panels_html=panels_html,
    )

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(doc)
    return doc


# ---------------------------------------------------------------------------
# Page template (CSS + shell + JS) — ported from the reference script.
# Doubled braces are literal CSS/JS braces (this is a str.format template).
# ---------------------------------------------------------------------------

_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Databricks Inventory – {hostname}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0 }}
  :root {{
    --bg:#f8fafc; --surface:#ffffff; --sidebar:#0f172a; --sidebar-hover:#1e293b;
    --sidebar-active:#1e40af; --text:#1e293b; --text-muted:#64748b; --border:#e2e8f0;
    --radius:10px; --shadow:0 1px 3px 0 rgb(0 0 0/0.1), 0 1px 2px -1px rgb(0 0 0/0.1);
    --shadow-lg:0 10px 15px -3px rgb(0 0 0/0.1), 0 4px 6px -4px rgb(0 0 0/0.1);
  }}
  html, body {{ height:100%; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',Arial,sans-serif; background:var(--bg); color:var(--text); font-size:14px; line-height:1.5 }}
  a {{ color:#2563eb; text-decoration:none }} a:hover {{ text-decoration:underline }}
  .app {{ display:flex; height:100vh; overflow:hidden }}
  .sidebar {{ width:240px; min-width:240px; background:var(--sidebar); color:#e2e8f0; display:flex; flex-direction:column; overflow-y:auto; flex-shrink:0 }}
  .sidebar-brand {{ padding:20px 16px 12px; border-bottom:1px solid #1e293b }}
  .sidebar-brand h1 {{ font-size:13px; font-weight:700; color:#f1f5f9; letter-spacing:.5px; text-transform:uppercase }}
  .sidebar-brand p {{ font-size:11px; color:#64748b; margin-top:2px; word-break:break-all }}
  .sidebar-brand .ts {{ font-size:10px; color:#475569; margin-top:6px }}
  nav ul {{ list-style:none; padding:8px 0 }}
  .nav-item {{ display:flex; align-items:center; gap:8px; padding:7px 14px; cursor:pointer; border-radius:6px; margin:1px 6px; transition:background .15s; font-size:12.5px; color:#94a3b8 }}
  .nav-item:hover {{ background:var(--sidebar-hover); color:#e2e8f0 }}
  .nav-item.active {{ background:var(--sidebar-active); color:#fff; font-weight:600 }}
  .nav-icon {{ font-size:15px; flex-shrink:0 }}
  .nav-label {{ flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }}
  .nav-badge {{ font-size:10px; font-weight:700; color:#fff; padding:1px 6px; border-radius:20px; flex-shrink:0; min-width:20px; text-align:center }}
  .main {{ flex:1; overflow-y:auto; padding:24px }}
  #panel-summary {{ display:block }}
  .summary-header {{ margin-bottom:24px }}
  .summary-header h2 {{ font-size:22px; font-weight:700; color:var(--text) }}
  .summary-header p {{ color:var(--text-muted); margin-top:4px }}
  .summary-stats {{ display:flex; gap:16px; margin-top:12px; flex-wrap:wrap }}
  .stat-pill {{ background:var(--surface); border:1px solid var(--border); border-radius:20px; padding:4px 14px; font-size:12px; color:var(--text-muted) }}
  .stat-pill strong {{ color:var(--text) }}
  .cards-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(168px,1fr)); gap:12px; margin-bottom:24px }}
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:16px; cursor:pointer; transition:all .2s; display:flex; align-items:center; gap:14px; box-shadow:var(--shadow) }}
  .card:hover {{ transform:translateY(-2px); box-shadow:var(--shadow-lg); border-color:#93c5fd }}
  .card-icon {{ font-size:24px; width:44px; height:44px; border-radius:10px; display:flex; align-items:center; justify-content:center; flex-shrink:0 }}
  .card-count {{ font-size:24px; font-weight:800; line-height:1 }}
  .card-label {{ font-size:11.5px; color:var(--text-muted); margin-top:2px; line-height:1.3 }}
  .classification-box {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:16px 18px; margin-bottom:20px; box-shadow:var(--shadow) }}
  .classification-box h3 {{ font-size:15px; font-weight:700; margin-bottom:2px }}
  .classification-box p {{ color:var(--text-muted); font-size:12px; margin-bottom:10px }}
  .cls-table {{ width:auto; min-width:320px }}
  .errors-box {{ background:#fef2f2; border:1px solid #fecaca; border-radius:var(--radius); padding:12px 16px; color:#b91c1c; font-size:12.5px; margin-top:16px }}
  .errors-box ul {{ margin-top:6px; padding-left:16px }}
  .panel {{ display:none }} .panel.active {{ display:block }}
  .panel-header {{ display:flex; align-items:center; gap:12px; padding:16px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius) var(--radius) 0 0; flex-wrap:wrap }}
  .panel-icon {{ font-size:22px }}
  .panel-title {{ font-size:18px; font-weight:700; flex-shrink:0 }}
  .panel-count {{ font-size:12px; font-weight:700; padding:3px 10px; border-radius:20px; flex-shrink:0 }}
  .panel-controls {{ margin-left:auto; display:flex; align-items:center; gap:12px; flex-wrap:wrap }}
  .panel-search {{ position:relative; display:flex; align-items:center }}
  .search-icon {{ position:absolute; left:9px; font-size:13px; pointer-events:none }}
  .panel-search input {{ border:1px solid var(--border); border-radius:6px; padding:6px 12px 6px 30px; font-size:13px; outline:none; width:220px; background:var(--bg) }}
  .panel-search input:focus {{ border-color:#93c5fd; background:#fff }}
  .page-size-wrap {{ display:flex; align-items:center; gap:6px; white-space:nowrap; color:var(--text-muted); font-size:12px }}
  .page-size-input {{ width:62px; border:1px solid var(--border); border-radius:6px; padding:5px 8px; font-size:13px; text-align:center; outline:none; background:var(--bg) }}
  .page-size-input:focus {{ border-color:#93c5fd; background:#fff }}
  .table-wrap {{ overflow-x:auto; background:var(--surface); border:1px solid var(--border); border-top:none; border-bottom:none }}
  .pagination-bar {{ display:flex; align-items:center; justify-content:space-between; padding:10px 16px; background:var(--surface); border:1px solid var(--border); border-top:1px solid #f1f5f9; border-radius:0 0 var(--radius) var(--radius); flex-wrap:wrap; gap:8px }}
  .pager-info {{ font-size:12.5px; color:var(--text-muted) }} .pager-info strong {{ color:var(--text) }}
  .pager-buttons {{ display:flex; align-items:center; gap:4px }}
  .pager-btn {{ border:1px solid var(--border); background:var(--surface); color:var(--text); border-radius:6px; width:32px; height:32px; font-size:15px; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all .15s; padding:0 }}
  .pager-btn:hover:not(:disabled) {{ background:#dbeafe; border-color:#93c5fd; color:#1d4ed8 }}
  .pager-btn:disabled {{ opacity:.35; cursor:default }}
  .pager-pages {{ display:flex; gap:3px; align-items:center }}
  .page-num {{ border:1px solid var(--border); background:var(--surface); color:var(--text-muted); border-radius:6px; min-width:32px; height:32px; font-size:12.5px; cursor:pointer; display:flex; align-items:center; justify-content:center; padding:0 6px; transition:all .15s; font-weight:500 }}
  .page-num:hover {{ background:#eff6ff; border-color:#93c5fd }}
  .page-num.current {{ background:#1d4ed8; border-color:#1d4ed8; color:#fff; font-weight:700 }}
  .page-ellipsis {{ color:var(--text-muted); padding:0 4px; font-size:12px; user-select:none }}
  table {{ width:100%; border-collapse:collapse; font-size:13px }}
  thead {{ position:sticky; top:0; z-index:2 }}
  th {{ background:#f1f5f9; color:var(--text-muted); font-weight:600; padding:10px 14px; text-align:left; white-space:nowrap; border-bottom:2px solid var(--border); cursor:pointer; user-select:none }}
  th:hover {{ background:#e2e8f0; color:var(--text) }}
  .sort-icon {{ opacity:.4; font-size:10px }}
  td {{ padding:9px 14px; border-bottom:1px solid #f1f5f9; vertical-align:top }}
  tr:last-child td {{ border-bottom:none }}
  tr:hover td {{ background:#f8fafc }}
  .empty-row {{ text-align:center; color:var(--text-muted); padding:32px; font-style:italic }}
  .badge {{ display:inline-block; font-size:11px; font-weight:700; padding:2px 7px; border-radius:4px; white-space:nowrap; letter-spacing:.3px }}
  .badge-green {{ background:#dcfce7; color:#15803d }}
  .badge-red {{ background:#fee2e2; color:#b91c1c }}
  .badge-blue {{ background:#dbeafe; color:#1d4ed8 }}
  .badge-yellow {{ background:#fef9c3; color:#854d0e }}
  .badge-gray {{ background:#f1f5f9; color:#475569 }}
  .badge-purple {{ background:#f3e8ff; color:#6d28d9 }}
  .na {{ color:#cbd5e1; font-style:italic }}
  .mono {{ font-family:'SF Mono','Fira Code',monospace; font-size:11.5px; color:#0f766e; background:#f0fdfa; padding:1px 5px; border-radius:3px }}
  .path {{ font-family:'SF Mono','Fira Code',monospace; font-size:11.5px; color:#7c3aed }}
  .count {{ font-weight:700; color:#1d4ed8 }}
  .schedule {{ font-size:11.5px; font-family:monospace }}
  small {{ color:var(--text-muted); font-size:11px }}
</style>
</head>
<body>
<div class="app">
<aside class="sidebar">
  <div class="sidebar-brand">
    <h1>Workspace Inventory</h1>
    <p>{hostname}</p>
    <div class="ts">Generated {generated_at}</div>
  </div>
  <nav><ul id="nav-list">{nav_html}</ul></nav>
</aside>
<main class="main" id="main">
  <div id="panel-summary" class="panel active">
    <div class="summary-header">
      <h2>Workspace Inventory</h2>
      <p>Complete inventory of all migratable resources in <strong>{hostname}</strong></p>
      <div class="summary-stats">
        <span class="stat-pill"><strong>{total_items}</strong> total resources</span>
        <span class="stat-pill"><strong>{n_components}</strong> component types</span>
        <span class="stat-pill">Snapshot: <strong>{generated_at}</strong></span>
      </div>
    </div>
    <div class="cards-grid">{cards_html}</div>
    {classification_html}
    {errors_html}
  </div>
  {panels_html}
</main>
</div>
<script>
const _state = {{}};
function _getState(id) {{ if (!_state[id]) _state[id] = {{ page:1, pageSize:25, query:'' }}; return _state[id]; }}
function _renderTable(id) {{
  const st=_getState(id); const tbody=document.getElementById('tbody-'+id); if(!tbody) return;
  const allRows=Array.from(tbody.querySelectorAll('tr')); if(!allRows.length) return;
  const q=st.query.toLowerCase().trim();
  const visible=allRows.filter(r=>!q||r.textContent.toLowerCase().includes(q));
  const ps=Math.max(1,st.pageSize); const totalPgs=Math.max(1,Math.ceil(visible.length/ps));
  if(st.page>totalPgs) st.page=totalPgs; if(st.page<1) st.page=1;
  const start=(st.page-1)*ps; const end=start+ps;
  allRows.forEach(r=>{{r.style.display='none';}});
  visible.forEach((r,i)=>{{r.style.display=(i>=start&&i<end)?'':'none';}});
  const infoEl=document.getElementById('pager-info-'+id);
  if(infoEl){{ const from=visible.length?start+1:0; const to=Math.min(end,visible.length);
    const fn=q?` (filtered from ${{allRows.length}})`:'';
    infoEl.innerHTML=visible.length?`Showing <strong>${{from}}–${{to}}</strong> of <strong>${{visible.length}}</strong>${{fn}}`:`<span style="color:#b91c1c">No results match your search</span>`; }}
  _renderPageButtons(id,st.page,totalPgs);
  ['first','prev'].forEach(d=>{{const b=document.getElementById(`btn-${{d}}-${{id}}`); if(b) b.disabled=st.page<=1;}});
  ['next','last'].forEach(d=>{{const b=document.getElementById(`btn-${{d}}-${{id}}`); if(b) b.disabled=st.page>=totalPgs;}});
}}
function _renderPageButtons(id,current,total) {{
  const c=document.getElementById('pager-pages-'+id); if(!c) return; c.innerHTML='';
  const pages=new Set(); pages.add(1); pages.add(total);
  for(let p=Math.max(1,current-2);p<=Math.min(total,current+2);p++) pages.add(p);
  const sorted=[...pages].sort((a,b)=>a-b); let prev=0;
  sorted.forEach(p=>{{ if(p-prev>1){{const el=document.createElement('span'); el.className='page-ellipsis'; el.textContent='…'; c.appendChild(el);}}
    const b=document.createElement('button'); b.className='page-num'+(p===current?' current':''); b.textContent=p; b.onclick=()=>goPage(id,p); c.appendChild(b); prev=p; }});
}}
function goPage(id,target) {{
  const st=_getState(id); const tbody=document.getElementById('tbody-'+id);
  const allRows=tbody?Array.from(tbody.querySelectorAll('tr')):[];
  const q=st.query.toLowerCase().trim();
  const visible=allRows.filter(r=>!q||r.textContent.toLowerCase().includes(q));
  const totalPgs=Math.max(1,Math.ceil(visible.length/st.pageSize));
  if(target==='first') st.page=1; else if(target==='prev') st.page=Math.max(1,st.page-1);
  else if(target==='next') st.page=Math.min(totalPgs,st.page+1); else if(target==='last') st.page=totalPgs;
  else st.page=Number(target);
  _renderTable(id); document.getElementById('table-'+id)?.scrollIntoView({{block:'nearest'}});
}}
function onSearch(id,query) {{ const st=_getState(id); st.query=query; st.page=1; _renderTable(id); }}
function onPageSizeChange(id,val) {{ const n=parseInt(val,10); if(!n||n<1) return; const st=_getState(id); st.pageSize=n; st.page=1; _renderTable(id); }}
const _sortState={{}};
function sortTable(tableId,colIdx) {{
  const table=document.getElementById('table-'+tableId); const tbody=table.querySelector('tbody');
  const rows=Array.from(tbody.querySelectorAll('tr')); const key=tableId+'_'+colIdx;
  const asc=_sortState[key]!==true; _sortState[key]=asc;
  rows.sort((a,b)=>{{ const ta=a.cells[colIdx]?.textContent.trim()??''; const tb=b.cells[colIdx]?.textContent.trim()??'';
    const na=parseFloat(ta), nb=parseFloat(tb); if(!isNaN(na)&&!isNaN(nb)) return asc?na-nb:nb-na; return asc?ta.localeCompare(tb):tb.localeCompare(ta); }});
  rows.forEach(r=>tbody.appendChild(r));
  table.querySelectorAll('th .sort-icon').forEach((ic,i)=>{{ ic.textContent=i===colIdx?(asc?'↑':'↓'):'↕'; ic.style.opacity=i===colIdx?'1':'0.4'; }});
  _getState(tableId).page=1; _renderTable(tableId);
}}
function showTab(id) {{
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  const panel=document.getElementById('panel-'+id); const nav=document.getElementById('nav-'+id);
  if(panel) panel.classList.add('active'); if(nav){{ nav.classList.add('active'); nav.scrollIntoView({{block:'nearest'}}); }}
  if(id!=='summary') _renderTable(id);
  const s=document.getElementById('search-'+id); if(s) setTimeout(()=>s.focus(),50);
}}
document.addEventListener('DOMContentLoaded',()=>{{ document.querySelectorAll('.panel[id^="panel-"]').forEach(panel=>{{ const id=panel.id.replace('panel-',''); if(id!=='summary') _renderTable(id); }}); }});
document.addEventListener('keydown',e=>{{ if(e.target.tagName==='INPUT') return; const ap=document.querySelector('.panel.active'); if(!ap) return; const id=ap.id.replace('panel-',''); if(id==='summary') return;
  if(e.key==='/'){{ e.preventDefault(); document.getElementById('search-'+id)?.focus(); }} else if(e.key==='ArrowRight'||e.key===']'){{ goPage(id,'next'); }} else if(e.key==='ArrowLeft'||e.key==='['){{ goPage(id,'prev'); }} }});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Placeholders for later plans (unchanged).
# ---------------------------------------------------------------------------

def render_transform_diff(before: dict, after: dict, output_path: str) -> str:
    raise NotImplementedError  # Plan 8 (target side)


def render_import_results(results: list, manual_actions: list, output_path: str) -> str:
    raise NotImplementedError  # Plan 8 (target side)


def render_validation(reconciliation: dict, output_path: str) -> str:
    raise NotImplementedError  # Plan 8 (target side)
